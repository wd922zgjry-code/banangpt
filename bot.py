# -*- coding: utf-8 -*-
"""BananGPT — Telegram-бот поверх личного веб-аккаунта Gemini (по кукам), до ~10 пользователей.

Что умеет: диалог с контекстом, режимы (/mode), модели (/model), авто-графики matplotlib (в изолированном
процессе), генерация картинок, ответы файлами (txt, py, csv… и настоящие PDF/DOCX), разбор присланных фото/файлов.
Для админа: список пользователей, заявки на доступ, баны, дневные лимиты, статистика, рассылка, алерты по кукам.
Весь ответ чистится от Markdown/LaTeX, знаки умножения сохраняются.
"""
import asyncio
import concurrent.futures
import inspect
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from gemini_webapi import GeminiClient

import docgen
from chartbox import ChartError, harden_process, render_charts
from store import Store
from textfmt import (
    clean_formatting,
    find_chart_code,
    remove_chart_blocks,
    split_blocks,
    split_text,
    strip_service_markers,
)
from users import (
    STATUS_ACTIVE,
    STATUS_BANNED,
    STATUS_MUTED,
    STATUS_PENDING,
    UserRegistry,
    fmt_duration,
    parse_duration,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("banan")

# ───────────────────────── настройки ─────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
SECURE_1PSID = os.getenv("GEMINI_SECURE_1PSID", "").strip()
SECURE_1PSIDTS = os.getenv("GEMINI_SECURE_1PSIDTS", "").strip() or None
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "150"))
# Если выбранная модель молчит дольше этого — бот сам повторяет запрос в режиме «авто»
MODEL_TIMEOUT = int(os.getenv("MODEL_TIMEOUT", "60"))
# Модель по умолчанию для новых пользователей: пусто/auto = «авто» (самый быстрый и надёжный режим)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "").strip()
BOT_THREADS = int(os.getenv("BOT_THREADS", "8"))
GEMINI_CONCURRENCY = max(1, int(os.getenv("GEMINI_CONCURRENCY", "2")))
HEALTH_INTERVAL = int(os.getenv("HEALTH_INTERVAL", "3600"))  # проверка Gemini раз в N секунд, 0 = выкл.

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана переменная окружения TELEGRAM_TOKEN!")


def _parse_ids(raw):
    ids = set()
    for part in re.split(r"[,\s;]+", raw or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# ALLOWED_IDS — стартовый список пользователей (становятся активными, если их нет в базе).
# ADMIN_IDS — админы; если не задано, админом считается наименьший ID из ALLOWED_IDS.
ALLOWED_IDS = _parse_ids(os.getenv("ALLOWED_IDS", ""))
ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", "")) or ({min(ALLOWED_IDS)} if ALLOWED_IDS else set())
MAX_USERS = int(os.getenv("MAX_USERS", "10"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "30"))  # запросов в день на человека, 0 = без лимита
ALLOW_REQUESTS = os.getenv("ALLOW_REQUESTS", "1") != "0"  # можно ли чужим подавать заявку на доступ
TZ_OFFSET = int(os.getenv("TZ_OFFSET_HOURS", "3"))  # часовой пояс для «дня» (3 = Москва)

WORKDIR = Path(tempfile.gettempdir()) / "bananbot_work"
WORKDIR.mkdir(parents=True, exist_ok=True)

store = Store(os.getenv("DATABASE_URL", ""), os.getenv("DB_PATH", "bananbot.db"))
registry = UserRegistry(store, ADMIN_IDS, MAX_USERS, DAILY_LIMIT, TZ_OFFSET)

bot = telebot.TeleBot(TELEGRAM_TOKEN, num_threads=BOT_THREADS)
STARTED_AT = time.time()

# ───────────────────────── сервер для Render ─────────────────────────


class HealthHandler(BaseHTTPRequestHandler):
    def _reply(self, with_body):
        body = b"Bot is alive!"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self):
        self._reply(True)

    def do_HEAD(self):
        self._reply(False)

    def log_message(self, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    except OSError as e:
        log.warning("Не удалось занять порт %s: %s", port, e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Health-сервер на порту %s", port)


def start_keepalive():
    """Бесплатный Render усыпляет сервис без входящих запросов — пингуем сами себя."""
    url = os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    interval = int(os.getenv("KEEPALIVE_INTERVAL", "600"))

    def loop():
        while True:
            time.sleep(interval)
            try:
                urllib.request.urlopen(url, timeout=20).read(16)
            except Exception as e:
                log.warning("keepalive: %s", e)

    threading.Thread(target=loop, daemon=True).start()
    log.info("Keepalive-пинг %s каждые %s с", url, interval)


# ───────────────────────── статистика ─────────────────────────
STATS = {"requests": 0, "errors": 0, "timeouts": 0, "fallbacks": 0, "by_model": {}}
_stats_lock = threading.Lock()


def bump_stat(name, model=None):
    with _stats_lock:
        STATS[name] = STATS.get(name, 0) + 1
        if model:
            STATS["by_model"][model] = STATS["by_model"].get(model, 0) + 1
        snapshot = json.dumps(STATS, ensure_ascii=False)
    if registry.store is not None:
        registry.store.save_kv("stats", snapshot)


# ───────────────────────── алерты админам ─────────────────────────
def notify_admins(text, markup=None):
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as e:
            log.warning("Не удалось написать админу %s: %s", admin_id, e)


def notify_admins_async(text):
    threading.Thread(target=notify_admins, args=(text,), daemon=True).start()


# ───────────────────────── слежение за куками ─────────────────────────
cookie_watch = {"unauth": False, "ok": None, "last_alert": 0.0, "detail": ""}
COOKIE_REALERT_SEC = 6 * 3600


def _cookie_sink(message):
    """Приёмник логов gemini_webapi (loguru): ловим «UNAUTHENTICATED» — Google не принял куки."""
    text = str(message)
    if "UNAUTHENTICATED" in text or "cookies have expired" in text:
        cookie_watch["unauth"] = True


def install_cookie_watch():
    try:
        from loguru import logger as _lg
    except Exception:
        log.warning("loguru не найден — слежение за куками по логам библиотеки отключено")
        return False
    _lg.add(_cookie_sink, level="WARNING", filter=lambda r: str(r["name"]).startswith("gemini_webapi"))
    return True


def update_cookie_state(ok, detail=""):
    prev = cookie_watch["ok"]
    cookie_watch["ok"], cookie_watch["detail"] = ok, detail
    now = time.time()
    if not ok:
        if prev is not False or now - cookie_watch["last_alert"] > COOKIE_REALERT_SEC:
            cookie_watch["last_alert"] = now
            notify_admins_async(
                "🚨 Gemini не принимает куки (гостевой режим)%s.\n\nОбнови GEMINI_SECURE_1PSID и GEMINI_SECURE_1PSIDTS "
                "в Render → Environment (куки из Firefox, приватное окно, закрыть без выхода из аккаунта)." % (" — " + detail if detail else ""))
    elif prev is False:
        notify_admins_async("✅ Куки Gemini снова работают.")


# ───────────────────────── Gemini (отдельный event loop) ─────────────────────────
bot_loop = asyncio.new_event_loop()
_loop_lock = threading.Lock()
_loop_started = False
_gemini_client = None
_client_lock = asyncio.Lock()


def _ensure_loop():
    global _loop_started
    with _loop_lock:
        if not _loop_started:
            threading.Thread(target=bot_loop.run_forever, daemon=True).start()
            _loop_started = True


class GeminiNotConfigured(RuntimeError):
    pass


# Пресеты: ключ для /model, подпись на кнопке, эмодзи, имя модели у Gemini.
# Имена взяты из реального списка аккаунта (лог «Модели аккаунта»).
MODEL_PRESETS = [
    ("argon", "4 Argon", "⚡", "gemini-argon"),
    ("flash", "Flash", "🚀", "gemini-flash"),
    ("lite", "Flash Lite", "🪶", "gemini-flash-lite"),
    ("pro", "Pro", "🧠", "gemini-pro"),
]
AVAILABLE_MODELS = {key: name for key, _label, _emo, name in MODEL_PRESETS}
# старые ключи из прежней версии бота продолжают работать
AVAILABLE_MODELS.update({"3.8": "gemini-flash", "3.5": "gemini-flash", "3.1": "gemini-pro"})
PRESET_KEYS = [p[0] for p in MODEL_PRESETS]
_SHORT_NAMES = {name: label for _key, label, _emo, name in MODEL_PRESETS}

user_selected_model = {}  # chat_id -> имя модели | None («авто»); нет ключа = по умолчанию
_account_names = None  # None — модели аккаунта ещё не узнали; иначе множество нормализованных имён
_account_list = []  # [(model_name, display_name)]


def _norm(s):
    return re.sub(r"[^a-z0-9.]+", "-", (s or "").lower()).strip("-")


async def _discover_models(client):
    """Узнаём, какие модели реально есть на аккаунте (в gemini-webapi они подгружаются динамически)."""
    global _account_names, _account_list
    try:
        models = client.list_models()
        if inspect.isawaitable(models):
            models = await models
    except Exception as e:
        log.warning("Не удалось получить список моделей: %s", e)
        return
    if not models:
        return
    names, lst = set(), []
    for m in models:
        if getattr(m, "is_available", True) is False:
            continue
        name = getattr(m, "model_name", None) or str(m)
        disp = getattr(m, "display_name", None) or name
        lst.append((name, disp))
        names.update((_norm(name), _norm(disp)))
    if lst:
        _account_list, _account_names = lst, names
        log.info("Модели аккаунта: %s", ", ".join(n for n, _ in lst))


def model_known(name):
    return _account_names is None or _norm(name) in _account_names


def resolve_model_choice(token):
    """Возвращает (имя_модели | None, как_просили). None — такой модели нет на аккаунте."""
    t = (token or "").strip()
    if t.lower() in AVAILABLE_MODELS:
        name = AVAILABLE_MODELS[t.lower()]
    else:
        name, n = t, _norm(t)
        for _key, label, _emo, preset in MODEL_PRESETS:
            if n in (_norm(label), _norm(preset)):
                name = preset
        for acc_name, disp in _account_list:
            if n in (_norm(acc_name), _norm(disp)):
                name = acc_name
    return (name if model_known(name) else None), name


def default_model():
    """По умолчанию — «авто» (модель не передаётся). Можно задать переменной DEFAULT_MODEL, например gemini-flash."""
    if DEFAULT_MODEL and DEFAULT_MODEL.lower() not in ("auto", "авто"):
        name, _ = resolve_model_choice(DEFAULT_MODEL)
        return name
    return None


def get_user_model(chat_id):
    return user_selected_model.get(chat_id, default_model())


def get_user_model_short_name(chat_id):
    model = get_user_model(chat_id)
    if model is None:
        return "авто"
    return _SHORT_NAMES.get(model, model)


def set_user_model(chat_id, value):
    """value: имя модели или None («авто»). Запоминается и в базе."""
    user_selected_model[chat_id] = value
    registry.set_pref(chat_id, model="auto" if value is None else value)


async def _get_client(force=False):
    global _gemini_client
    async with _client_lock:
        if not SECURE_1PSID:
            raise GeminiNotConfigured("Не задана переменная GEMINI_SECURE_1PSID.")
        if _gemini_client is None or force:
            if _gemini_client is not None:
                try:
                    await _gemini_client.close()
                except Exception:
                    pass
                _gemini_client = None
            # ВАЖНО: без proxies= — в новых gemini-webapi аргумент называется proxy (и он необязателен)
            client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS)
            cookie_watch["unauth"] = False
            await client.init(timeout=60, auto_close=False, auto_refresh=True)
            _gemini_client = client
            await _discover_models(client)
            update_cookie_state(not cookie_watch["unauth"])
        return _gemini_client


@dataclass
class GeminiReply:
    text: str = ""
    images: list = field(default_factory=list)  # пути к сохранённым картинкам
    notice: str = ""  # служебное замечание для пользователя


async def _generate(client, prompt, files, model):
    kw = {"files": files} if files else {}
    notice = ""
    if model:
        try:
            return await client.generate_content(prompt, model=model, **kw), notice
        except (ValueError, LookupError, TypeError, AttributeError) as e:
            log.warning("Модель %s не принята библиотекой (%s) — беру модель по умолчанию", model, e)
            notice = "⚠️ Модель «%s» недоступна — ответила модель по умолчанию." % model
    return await client.generate_content(prompt, **kw), notice


async def _ask_async(prompt, file_path, prefix, model):
    client = await _get_client()
    files = [file_path] if (file_path and os.path.exists(file_path)) else None
    t0 = time.time()
    log.info("→ Gemini: модель=%s, файлов=%d, промпт %d симв.", model or "авто", len(files or []), len(prompt))
    try:
        response, notice = await _generate(client, prompt, files, model)
    except Exception as first_err:
        log.warning("Переподключение к Gemini после ошибки: %s", first_err)
        client = await _get_client(force=True)
        response, notice = await _generate(client, prompt, files, model)

    saved = []
    for idx, img in enumerate((getattr(response, "images", None) or [])[:4]):
        fname = "gen_%s_%d.png" % (prefix, idx)
        try:
            res = await img.save(path=str(WORKDIR), filename=fname)
            path = res if (isinstance(res, (str, os.PathLike)) and os.path.exists(res)) else WORKDIR / fname
            if os.path.exists(path):
                saved.append(str(path))
        except Exception as e:
            log.warning("Не удалось сохранить картинку %d: %s", idx, e)
    log.info("← Gemini ответил за %.1f с (текст %d симв., картинок %d)", time.time() - t0, len(response.text or ""), len(saved))
    return GeminiReply(text=response.text or "", images=saved, notice=notice)


def _run_gemini(prompt, file_path, prefix, model, timeout):
    fut = asyncio.run_coroutine_threadsafe(_ask_async(prompt, file_path, prefix, model), bot_loop)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        log.error("Таймаут %d с: Gemini не ответил (модель=%s)", timeout, model or "авто")
        raise TimeoutError("Gemini не ответил за %d с" % timeout)


def ask_gemini_sync(prompt, file_path=None, prefix="0", chat_id=None):
    _ensure_loop()
    model = get_user_model(chat_id) if chat_id is not None else default_model()
    if not model:
        return _run_gemini(prompt, file_path, prefix, None, REQUEST_TIMEOUT)
    try:
        return _run_gemini(prompt, file_path, prefix, model, MODEL_TIMEOUT)
    except TimeoutError:
        log.warning("Модель %s молчит — повторяю запрос в режиме «авто»", model)
        bump_stat("fallbacks")
        reply = _run_gemini(prompt, file_path, prefix + "_auto", None, REQUEST_TIMEOUT)
        note = "⚠️ Модель «%s» не ответила за %d с — ответил режим «авто»." % (get_user_model_short_name(chat_id), MODEL_TIMEOUT)
        reply.notice = note + ("\n" + reply.notice if reply.notice else "")
        return reply


def ensure_client_sync(timeout=90, force=False):
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(_get_client(force=force), bot_loop).result(timeout=timeout)


# ───────────────────────── очередь к Gemini ─────────────────────────
class GeminiQueue:
    """Не больше N одновременных запросов к одному аккаунту; остальные ждут и видят свою позицию."""

    def __init__(self, n):
        self.n, self.sem = n, threading.Semaphore(n)
        self.lock, self.waiting, self.active = threading.Lock(), 0, 0

    @contextmanager
    def slot(self, on_wait=None):
        with self.lock:
            position = (self.waiting + 1) if self.active >= self.n else 0
            self.waiting += 1
        if position and on_wait:
            try:
                on_wait(position)
            except Exception:
                pass
        self.sem.acquire()
        with self.lock:
            self.waiting -= 1
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1
            self.sem.release()


QUEUE = GeminiQueue(GEMINI_CONCURRENCY)


# ───────────────────────── проверка здоровья Gemini ─────────────────────────
_health = {"fails": 0, "last_ok": 0.0, "alerted": False}


def health_check_once():
    """Минимальный запрос раз в час: если Gemini молчит два раза подряд — алерт админу."""
    try:
        with QUEUE.slot():
            ask_gemini_sync("Ответь одним словом: ок", prefix="health")
        _health["fails"], _health["last_ok"] = 0, time.time()
        if _health["alerted"]:
            _health["alerted"] = False
            notify_admins_async("✅ Gemini снова отвечает.")
        return True
    except Exception as e:
        _health["fails"] += 1
        log.warning("health-check: %s (подряд: %d)", e, _health["fails"])
        if _health["fails"] >= 2 and not _health["alerted"]:
            _health["alerted"] = True
            notify_admins_async("🚨 Gemini не отвечает уже %d проверки подряд: %s\nПроверь куки (/cookies) и логи Render." % (_health["fails"], type(e).__name__))
        return False


def start_health_loop():
    if HEALTH_INTERVAL <= 0:
        return

    def loop():
        time.sleep(120)
        while True:
            health_check_once()
            time.sleep(HEALTH_INTERVAL)

    threading.Thread(target=loop, daemon=True).start()


# ───────────────────────── контекст диалога ─────────────────────────
user_context = {}
_ctx_lock = threading.Lock()
MAX_HISTORY = 10
MAX_ITEM_CHARS = 1500


def _persist_context(chat_id, items):
    if registry.store is not None:
        registry.store.save_context(chat_id, items)


def add_to_context(chat_id, role, text):
    with _ctx_lock:
        items = user_context.setdefault(chat_id, [])
        items.append({"role": role, "text": (text or "")[:MAX_ITEM_CHARS]})
        del items[:-MAX_HISTORY]
        snapshot = list(items)
    _persist_context(chat_id, snapshot)


def get_context_as_prompt(chat_id):
    with _ctx_lock:
        items = list(user_context.get(chat_id, []))
    labels = {"user": "Пользователь", "model": "Ассистент"}
    return "\n".join("%s: %s" % (labels.get(i["role"], i["role"]), i["text"]) for i in items)


def clear_context(chat_id):
    """Чистит только историю. Выбранные модель и режим остаются."""
    with _ctx_lock:
        user_context[chat_id] = []
    _persist_context(chat_id, [])


# ───────────────────────── режимы ─────────────────────────
MODES = {
    "default": ("🧩 Универсальный", ""),
    "solver": ("📐 Решатель задач",
               "РЕЖИМ РЕШАТЕЛЯ ЗАДАЧ: оформляй решение блоками «Дано», «Решение», «Ответ»; приводи формулы, подстановку чисел и "
               "единицы измерения; если нужен график или рисунок — код matplotlib."),
    "coder": ("💻 Программист",
              "РЕЖИМ ПРОГРАММИСТА: отвечай по делу, давай рабочий код с минимальными пояснениями, указывай язык и как запускать."),
    "translator": ("🌐 Переводчик",
                   "РЕЖИМ ПЕРЕВОДЧИКА: переводи присланный текст между русским и английским (если язык не указан — на тот, "
                   "которого нет в тексте), без комментариев и пояснений."),
    "short": ("⚡ Кратко", "РЕЖИМ КРАТКИХ ОТВЕТОВ: не больше 5–7 предложений, без вступлений и повторов."),
}
user_mode = {}  # chat_id -> ключ режима


def get_mode(chat_id):
    m = user_mode.get(chat_id, "default")
    return m if m in MODES else "default"


def set_user_mode(chat_id, key):
    user_mode[chat_id] = key
    registry.set_pref(chat_id, mode=key)


# ───────────────────────── защита от параллельных запросов ─────────────────────────
_busy = set()
_busy_lock = threading.Lock()


@contextmanager
def busy_guard(chat_id):
    with _busy_lock:
        taken = chat_id in _busy
        if not taken:
            _busy.add(chat_id)
    try:
        yield not taken
    finally:
        if not taken:
            with _busy_lock:
                _busy.discard(chat_id)


@contextmanager
def keep_chat_action(chat_id, action="typing"):
    """Telegram гасит «печатает…» через 5 секунд — поддерживаем, пока ждём Gemini."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                bot.send_chat_action(chat_id, action)
            except Exception:
                pass
            stop.wait(4)

    threading.Thread(target=loop, daemon=True).start()
    try:
        yield
    finally:
        stop.set()


# ───────────────────────── промпты ─────────────────────────
RULES_TEXT = (
    "СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ И ДЕЙСТВИЙ:\n"
    "1. ЧИСТЫЙ ТЕКСТ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО жирное выделение и звёздочки-разметка (** или ***), "
    "решётки (#), знаки доллара ($) и LaTeX. Формулы пиши обычными символами, знак умножения — * или ·, "
    "например: B = F / (I * l).\n"
    "2. АВТО-ГРАФИК: если по смыслу задачи нужен математический или инженерный график, диаграмма, кривая "
    "охлаждения, эпюра (или пользователь сам просит график) — дай текстовое решение И ровно один блок "
    "```python ... ``` с кодом matplotlib. В коде: только numpy и matplotlib, без чтения/записи файлов, "
    "без plt.show() и без сохранения (график сохранит бот), подписи осей с единицами, сетка, подписи на русском. "
    "Если график не нужен — код не пиши.\n"
    "3. АВТО-ФАЙЛ: если пользователь просит ответ файлом, документом, скриптом или таблицей — в самом начале "
    "напиши метку [SEND_AS_FILE:имя_файла.расширение], ниже — содержимое файла. Форматы: txt, md, py, csv, json, html… "
    "а также pdf и docx (для них пиши обычный текст с абзацами и списками; блок с кодом графика, если он есть, "
    "бот вставит в файл как рисунок).\n"
)
RULES_IMAGE = (
    "4. АВТО-ФОТО: если пользователь просит нарисовать художественную картинку, иллюстрацию или фото "
    "(а не математический график) — напиши метку [GENERATE_IMAGE:подробное описание картинки на английском].\n"
)


def build_text_prompt(history, user_text, mode_text=""):
    return (
        "Ты умный универсальный ассистент BananGPT. Отвечай на языке пользователя.\n%s"
        "--- ИСТОРИЯ ДИАЛОГА ---\n%s\n--- КОНЕЦ ИСТОРИИ ---\n\n"
        "Запрос или задача пользователя: %s\n\n%s%s"
        % ((mode_text + "\n") if mode_text else "", history or "(пусто)", user_text, RULES_TEXT, RULES_IMAGE)
    )


def build_file_prompt(history, caption, mode_text=""):
    return (
        "Ты умный универсальный ассистент BananGPT. Отвечай на языке пользователя.\n%s"
        "--- ИСТОРИЯ ДИАЛОГА ---\n%s\n--- КОНЕЦ ИСТОРИИ ---\n\n"
        "Запрос пользователя к прикреплённому файлу: %s\n\n%s"
        % ((mode_text + "\n") if mode_text else "", history or "(пусто)", caption, RULES_TEXT)
    )


# ───────────────────────── клавиатуры и сообщения ─────────────────────────
def get_main_keyboard(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(KeyboardButton("📝 Модель: %s" % get_user_model_short_name(chat_id)))
    markup.row(KeyboardButton("🔄 Новый диалог"), KeyboardButton("⚙️ Настройки"))
    if registry.is_admin(chat_id):
        markup.row(KeyboardButton("👑 Админ-панель"))
    return markup


def send_settings_menu(chat_id):
    current = get_user_model(chat_id) or "по умолчанию (аккаунт)"
    text = (
        "⚙️ *Панель настроек*\n\n"
        "🤖 Активная модель: `%s`\n"
        "🎭 Режим: *%s*\n"
        "🧠 Память контекста: *Включена* (до %d реплик)\n\n"
        "Выберите необходимое действие:" % (current, MODES[get_mode(chat_id)][0], MAX_HISTORY)
    )
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(InlineKeyboardButton("🤖 Выбрать модель", callback_data="open_model_selector"),
               InlineKeyboardButton("🎭 Режим", callback_data="open_mode_selector"))
    markup.row(InlineKeyboardButton("🔄 Очистить контекст", callback_data="reset_context_inline"))
    markup.row(
        InlineKeyboardButton("❓ Помощь", callback_data="show_help"),
        InlineKeyboardButton("❌ Закрыть", callback_data="close_settings"),
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


def send_model_selection_menu(chat_id):
    current = get_user_model(chat_id)
    text = "🤖 *Выбор модели ИИ*\n\nТекущая модель: `%s`\nВыберите нужную версию:" % (current or "по умолчанию")
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for key, label, emo, name in MODEL_PRESETS:
        if not model_known(name):
            mark = "🚫 "
        elif name == current:
            mark = "✅ "
        else:
            mark = emo + " "
        buttons.append(InlineKeyboardButton(mark + label, callback_data="set_model_" + key))
    markup.add(*buttons)
    markup.add(InlineKeyboardButton(("✅ " if current is None else "🤖 ") + "Авто (модель аккаунта по умолчанию)", callback_data="set_model_auto"))
    markup.add(InlineKeyboardButton("⬅️ Назад в настройки", callback_data="back_to_settings"))
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


def send_mode_menu(chat_id):
    current = get_mode(chat_id)
    text = "🎭 *Режим ответов*\n\nТекущий: *%s*\nРежим меняет стиль и формат ответов бота." % MODES[current][0]
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(*[InlineKeyboardButton(("✅ " if key == current else "") + label, callback_data="set_mode_" + key)
                 for key, (label, _instr) in MODES.items()])
    markup.add(InlineKeyboardButton("⬅️ Назад в настройки", callback_data="back_to_settings"))
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


def safe_reply(message, text, quote=True):
    kb = get_main_keyboard(message.chat.id)
    if quote:
        try:
            return bot.reply_to(message, text, reply_markup=kb)
        except Exception:
            pass
    return bot.send_message(message.chat.id, text, reply_markup=kb)


def send_long_message(message, text):
    cleaned = clean_formatting(text) or "Пустой ответ от модели."
    for i, chunk in enumerate(split_text(cleaned)):
        safe_reply(message, chunk, quote=(i == 0))


def send_bytes_photo(chat_id, data, caption=None):
    bio = io.BytesIO(data)
    bio.name = "chart.png"
    bot.send_photo(chat_id, bio, caption=caption, reply_markup=get_main_keyboard(chat_id))


def send_image_files(chat_id, paths, caption):
    """Отправляет сохранённые Gemini картинки и удаляет их с диска."""
    for idx, path in enumerate(paths):
        try:
            with open(path, "rb") as photo:
                bot.send_photo(
                    chat_id, photo,
                    caption=(caption[:1024] if idx == 0 and caption else None),
                    reply_markup=get_main_keyboard(chat_id),
                )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def friendly_error(e):
    if isinstance(e, GeminiNotConfigured):
        return "Бот не настроен: %s" % e
    if isinstance(e, TimeoutError):
        return "Gemini не ответил вовремя. Попробуй ещё раз или выбери модель полегче."
    return "Что-то пошло не так (%s). Подробности — в логах сервера." % type(e).__name__


# ───────────────────────── графики и файлы ─────────────────────────
def build_charts(chat_id, code, prefix):
    """Строит график; при ошибке один раз просит Gemini исправить код."""
    err = ""
    for attempt in range(2):
        try:
            return render_charts(code), ""
        except ChartError as e:
            err = str(e)
            log.warning("График не построился (попытка %d): %s", attempt + 1, err)
            if attempt:
                break
            try:
                fix = ask_gemini_sync(
                    "Твой код matplotlib не выполнился: %s\nИсправь и верни ТОЛЬКО исправленный код "
                    "в одном блоке ```python```. Только numpy и matplotlib, без файлов, plt.show() и сохранения." % err,
                    prefix=prefix + "_fix", chat_id=chat_id,
                )
            except Exception as ex:
                log.warning("Исправление кода не удалось: %s", ex)
                break
            new_code = find_chart_code(fix.text)
            if not new_code:
                break
            code = new_code
    return None, err


TEXT_EXTS = {
    "txt", "md", "py", "json", "html", "js", "ts", "css", "cpp", "c", "h", "java", "csv", "sql",
    "xml", "yaml", "yml", "ini", "cfg", "toml", "sh", "bat", "tex", "log",
}
DOC_EXTS = {"pdf", "docx"}
CODE_LANG_EXT = {
    "python": "py", "py": "py", "json": "json", "html": "html", "js": "js", "javascript": "js",
    "cpp": "cpp", "c": "c", "csv": "csv", "sql": "sql", "xml": "xml", "md": "md", "txt": "txt",
}


def detect_doc_format(low):
    if re.search(r"\bpdf\b|пдф", low):
        return "pdf"
    if re.search(r"\bdocx?\b|\bword\b|ворд", low):
        return "docx"
    return None


def send_as_file(message, reply_text, prefix, file_marker, doc_hint=None, chart_code=None):
    chat_id = message.chat.id
    clean_reply = re.sub(r"\[SEND_AS_FILE:[^\]]+\]", "", reply_text).strip()
    if chart_code:
        clean_reply = remove_chart_blocks(clean_reply)
    code_block = re.search(r"```([a-zA-Z0-9_+-]+)?[ \t]*\n(.*?)```", clean_reply, re.DOTALL)
    explanation = ""
    note = ""

    if file_marker:
        raw = os.path.basename(file_marker.group(1).strip())
        name = re.sub(r"[^\w.\-]+", "_", raw).strip("._")[:80] or "document.txt"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in TEXT_EXTS and ext not in DOC_EXTS:
            stem = name.rsplit(".", 1)[0] if "." in name else name
            note = "ℹ️ Формат .%s из текста не собрать, отправляю как .txt." % ext if ext else ""
            name, ext = stem + ".txt", "txt"
    elif doc_hint in DOC_EXTS:
        ext, name = doc_hint, "result_%s.%s" % (prefix, doc_hint)
    else:
        lang = ((code_block.group(1) if code_block else None) or "txt").lower()
        ext = CODE_LANG_EXT.get(lang, "txt")
        name = "result_%s.%s" % (prefix, ext)

    if ext in DOC_EXTS:
        images = []
        if chart_code:
            charts, err = build_charts(chat_id, chart_code, prefix)
            if charts:
                images = charts
            else:
                note = "⚠️ График не построился (%s) — файл без рисунка." % err
        blocks = split_blocks(clean_reply) or [("text", "(пустой ответ)")]
        try:
            data = docgen.make_pdf(blocks, images) if ext == "pdf" else docgen.make_docx(blocks, images)
        except Exception as e:
            log.exception("Сборка %s не удалась: %s", ext, e)
            stem = name.rsplit(".", 1)[0]
            name, data = stem + ".txt", clean_formatting(clean_reply).encode("utf-8")
            note = (note + "\n" if note else "") + "⚠️ %s собрать не удалось, отправляю как .txt." % ext.upper()
        bio = io.BytesIO(data)
        bio.name = name
        bot.send_document(chat_id, bio, caption="📄 Готово: %s" % name, reply_markup=get_main_keyboard(chat_id))
        if note:
            safe_reply(message, note, quote=False)
        return

    if code_block:
        content = code_block.group(2).strip()
        explanation = clean_formatting(re.sub(r"```.*?```", "", clean_reply, flags=re.DOTALL))
    elif ext in ("txt", "md"):
        content = clean_formatting(clean_reply)
    else:
        content = clean_reply  # код/таблицы без ограждений — как есть, без вычистки $ и *

    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = name
    caption = explanation[:1024] if explanation else "📂 Готовый файл по вашему запросу:"
    bot.send_document(chat_id, bio, caption=caption, reply_markup=get_main_keyboard(chat_id))
    if len(explanation) > 1024:
        send_long_message(message, explanation[1024:])
    if note:
        safe_reply(message, note, quote=False)


def deliver(message, reply, prefix, force_file=False, doc_hint=None):
    """Выбирает, как показать ответ: картинки → файл → график → обычный текст."""
    chat_id = message.chat.id
    text = reply.text or ""
    images = list(reply.images)

    marker = re.search(r"\[GENERATE_IMAGE:(.*?)\]", text, re.DOTALL)
    if marker and not images:
        description = marker.group(1).strip() or (message.text or "image")
        extra = ask_gemini_sync(
            "Generate a high-quality photo or image of: %s. Return the generated image directly." % description,
            prefix=prefix + "_art", chat_id=chat_id,
        )
        images = extra.images
        text = re.sub(r"\[GENERATE_IMAGE:.*?\]", "", text, flags=re.DOTALL).strip()

    if images:
        caption = clean_formatting(re.sub(r"\[GENERATE_IMAGE:.*?\]", "", text, flags=re.DOTALL))
        send_image_files(chat_id, images, caption or "🎨 Сгенерировано по вашему запросу")
        if len(caption) > 1024:
            send_long_message(message, caption[1024:])
    else:
        chart_code = find_chart_code(text)
        file_marker = re.search(r"\[SEND_AS_FILE:([^\]]+)\]", text)
        want_doc = doc_hint in DOC_EXTS
        if file_marker or (force_file and (not chart_code or want_doc)):
            send_as_file(message, text, prefix, file_marker, doc_hint=doc_hint, chart_code=chart_code)
        elif chart_code:
            prose = clean_formatting(remove_chart_blocks(text))
            charts, err = build_charts(chat_id, chart_code, prefix)
            if charts:
                send_bytes_photo(chat_id, charts[0], (prose[:1024] if prose else "📈 График к задаче:"))
                for extra_png in charts[1:]:
                    send_bytes_photo(chat_id, extra_png)
                if len(prose) > 1024:
                    send_long_message(message, prose[1024:])
            else:
                send_long_message(message, prose or "Ответ без текста.")
                safe_reply(message, "⚠️ График построить не удалось: %s" % err, quote=False)
        else:
            send_long_message(message, text)

    if reply.notice:
        safe_reply(message, reply.notice, quote=False)


# ───────────────────────── доступ и заявки ─────────────────────────
def is_allowed(user_id):
    return registry.is_allowed(user_id)


def _who(u):
    name = u.get("first_name") or ""
    nick = ("@%s" % u["username"]) if u.get("username") else ""
    return (name + " " + nick).strip() or "без имени"


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    bot.reply_to(message, "Твой Telegram ID: %s" % message.from_user.id)


_ALL_TYPES = ["text", "photo", "document", "voice", "audio", "video", "sticker", "video_note", "animation"]


def _request_markup(uid):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("✅ Одобрить", callback_data="acc_ok_%d" % uid),
               InlineKeyboardButton("🚫 Отклонить", callback_data="acc_no_%d" % uid))
    markup.add(InlineKeyboardButton("👤 Открыть профиль", callback_data="adm_u_%d" % uid))
    return markup


@bot.message_handler(func=lambda m: not registry.is_allowed(m.from_user.id), content_types=_ALL_TYPES)
def deny_message(message):
    uid = message.from_user.id
    status = registry.status(uid)
    if status == STATUS_BANNED:
        rem = registry.remaining(uid)
        if rem:
            bot.reply_to(message, "🚫 Доступ закрыт ещё на %s." % fmt_duration(rem))
        else:
            log.info("Бан: сообщение от %s проигнорировано", uid)
        return
    if status == STATUS_MUTED:
        rem = registry.remaining(uid)
        bot.reply_to(message, "🔇 Ты в муте%s." % ((" ещё %s" % fmt_duration(rem)) if rem is not None else ": админ пока не снял ограничение"))
        return
    if not ADMIN_IDS:
        bot.reply_to(message, "⛔ Бот ещё не настроен. Твой ID: %s — впиши его в переменную ALLOWED_IDS на сервере." % uid)
        return
    if status == STATUS_PENDING:
        bot.reply_to(message, "⏳ Твоя заявка ждёт одобрения админа. Как только он ответит, я напишу.")
        return
    if not ALLOW_REQUESTS:
        bot.reply_to(message, "⛔ Нет доступа. Твой ID: %s" % uid)
        return
    user, is_new = registry.register_pending(message.from_user)
    if user is None:
        bot.reply_to(message, "⛔ Заявки сейчас не принимаются.")
        return
    if is_new:
        notify_admins("🆕 Заявка на доступ: %s\nID: %d" % (_who(user), uid), markup=_request_markup(uid))
    bot.reply_to(message, "📨 Заявка отправлена админу. Я напишу, когда доступ откроют.")


@bot.callback_query_handler(func=lambda c: not registry.is_allowed(c.from_user.id))
def deny_callback(call):
    bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)


# ───────────────────────── команды пользователя ─────────────────────────
HELP_TEXT = (
    "BananGPT 🍌\n\n"
    "• Пиши любой вопрос или задачу — бот помнит последние %d реплик.\n"
    "• Фото/файл с подписью — разберу и решу.\n"
    "• «нарисуй картинку …» — сгенерирую изображение.\n"
    "• «ответь в pdf» / «в word» / «отправь файлом» — пришлю ответ файлом (PDF и DOCX настоящие, с графиком).\n"
    "• Графики и эпюры строю сам, если они нужны по задаче.\n\n"
    "Команды:\n"
    "/mode — режим (решатель задач, программист, переводчик, кратко)\n"
    "/model — выбор модели (/model pro, /model flash, /model argon, /model auto)\n"
    "/models — какие модели доступны на аккаунте\n"
    "/me — мой доступ и лимит на сегодня\n"
    "/new — очистить контекст\n"
    "/ping — проверка связи с Gemini\n"
    "/status — состояние бота\n"
    "/myid — твой Telegram ID" % MAX_HISTORY
)


@bot.message_handler(commands=["start"])
def cmd_start(message):
    clear_context(message.chat.id)
    bot.send_message(
        message.chat.id,
        "Добро пожаловать в BananGPT 🍌\nПишите любой запрос или скидывайте задачу — я сам пойму, нужен ли график, фото или файл!",
        reply_markup=get_main_keyboard(message.chat.id),
    )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    safe_reply(message, HELP_TEXT)


@bot.message_handler(commands=["new", "reset"])
def cmd_new(message):
    clear_context(message.chat.id)
    safe_reply(message, "✅ Контекст диалога очищен! Можем начать новую тему.")


@bot.message_handler(commands=["me"])
def cmd_me(message):
    uid = message.from_user.id
    u = registry.touch(message.from_user) or {}
    limit, used = registry.limit_for(uid), registry.usage_today(uid)
    role = "админ" if registry.is_admin(uid) else "пользователь"
    safe_reply(
        message,
        "👤 %s (%s)\nСегодня: %d/%s запросов\nВсего запросов: %s\nРежим: %s\nМодель: %s"
        % (_who(u) if u else uid, role, used, limit or "∞", u.get("total", 0), MODES[get_mode(message.chat.id)][0],
           get_user_model_short_name(message.chat.id)),
    )


@bot.message_handler(commands=["mode"])
def cmd_mode(message):
    chat_id = message.chat.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        send_mode_menu(chat_id)
        return
    key = parts[1].strip().lower()
    if key not in MODES:
        safe_reply(message, "Нет такого режима. Доступны: %s" % ", ".join(MODES))
        return
    set_user_mode(chat_id, key)
    safe_reply(message, "✅ Режим: %s" % MODES[key][0])


@bot.message_handler(commands=["models"])
def cmd_models(message):
    try:
        ensure_client_sync()
    except Exception as e:
        safe_reply(message, "Не удалось подключиться к Gemini: %s" % friendly_error(e))
        return
    lines = ["Модели твоего аккаунта:"]
    lines += ["• %s (%s)" % (name, disp) if disp != name else "• %s" % name for name, disp in _account_list]
    if not _account_list:
        lines.append("(библиотека не вернула список — используется модель по умолчанию)")
    lines.append("\nПресеты бота:")
    for key, label, _emo, name in MODEL_PRESETS:
        if _account_names is None:
            mark = "❔"
        else:
            mark = "✅" if model_known(name) else "🚫"
        lines.append("• %s → %s %s" % (key, name, mark))
    lines.append("\nВыбор: /model <ключ или имя>")
    safe_reply(message, "\n".join(lines))


@bot.message_handler(commands=["model"])
def cmd_model(message):
    chat_id = message.chat.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        send_model_selection_menu(chat_id)
        return
    if parts[1].strip().lower() in ("auto", "авто", "default"):
        set_user_model(chat_id, None)
        safe_reply(message, "✅ Модель: авто (Google выбирает по умолчанию для аккаунта)")
        return
    name, asked = resolve_model_choice(parts[1])
    if name is None:
        keys = ", ".join(PRESET_KEYS)
        safe_reply(message, "⚠️ Модели «%s» нет на твоём аккаунте. Ключи: %s. Весь список: /models" % (asked, keys))
        return
    set_user_model(chat_id, name)
    bot.reply_to(message, "✅ Активная модель: `%s`" % name, parse_mode="Markdown", reply_markup=get_main_keyboard(chat_id))


@bot.message_handler(commands=["ping"])
def cmd_ping(message):
    """Диагностика: минимальный запрос к Gemini без правил и контекста, с показом реальной ошибки."""
    chat_id = message.chat.id
    with busy_guard(chat_id) as free:
        if not free:
            safe_reply(message, "⏳ Ещё обрабатываю предыдущий запрос — подожди секунду.")
            return
        safe_reply(message, "🏓 Проверяю Gemini (модель: %s)..." % get_user_model_short_name(chat_id))
        t0 = time.time()
        try:
            with QUEUE.slot():
                reply = ask_gemini_sync("Ответь одним словом: ок", prefix="ping_%s" % chat_id, chat_id=chat_id)
            out = "✅ Gemini ответил за %.1f с: %s" % (time.time() - t0, (reply.text or "")[:100])
            if reply.notice:
                out += "\n" + reply.notice
        except Exception as e:
            log.exception("ping")
            out = "❌ %s через %.1f с: %s" % (type(e).__name__, time.time() - t0, str(e)[:300])
        safe_reply(message, out, quote=False)


def _cookie_text():
    state = {True: "✅ приняты", False: "🚨 НЕ приняты (гостевой режим)", None: "❔ ещё не проверялись"}[cookie_watch["ok"]]
    return "Куки Google: %s" % state


@bot.message_handler(commands=["status"])
def cmd_status(message):
    state = "подключён" if _gemini_client is not None else "ещё не подключался"
    models = ", ".join(n for n, _ in _account_list) or "список неизвестен"
    db = ("%s, %s" % ("Postgres" if store.kind == "pg" else "SQLite", "ок" if store.ok else "нет связи")) if registry.store else "выключена"
    safe_reply(
        message,
        "Gemini: %s\n%s\nМодели: %s\nТвоя модель: %s\nБаза: %s\nПользователей активных: %d/%d\nPython: %s"
        % (state, _cookie_text(), models, get_user_model_short_name(message.chat.id), db,
           registry.active_count(), MAX_USERS, sys.version.split()[0]),
    )


# ───────────────────────── админ-команды ─────────────────────────
def _is_admin_msg(message):
    return registry.is_admin(message.from_user.id)


def _ago(ts):
    if not ts:
        return "никогда"
    d = max(0, int(time.time() - ts))
    if d < 90:
        return "только что"
    if d < 3600:
        return "%d мин назад" % (d // 60)
    if d < 86400:
        return "%d ч назад" % (d // 3600)
    return "%d дн назад" % (d // 86400)


def _target(message):
    """Достаёт ID пользователя из аргумента команды: число или @username."""
    parts = (message.text or "").split()
    if len(parts) < 2:
        return None
    arg = parts[1]
    if arg.lstrip("-").isdigit():
        return int(arg)
    return registry.find_username(arg)


_ICONS = {"admin": "👑", STATUS_ACTIVE: "✅", STATUS_MUTED: "🔇", STATUS_PENDING: "⏳", STATUS_BANNED: "🚫"}


def _fmt_dt(ts):
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=TZ_OFFSET)).strftime("%d.%m %H:%M")


def _until_text(u):
    until = u.get("until_ts")
    return (" до %s" % _fmt_dt(until)) if until else " (бессрочно)"


def _status_label(uid):
    st, u = registry.status(uid), registry.users.get(uid, {})
    if st == "admin":
        return "👑 админ"
    if st == STATUS_ACTIVE:
        return "✅ активен"
    if st == STATUS_PENDING:
        return "⏳ ждёт одобрения"
    if st == STATUS_MUTED:
        return "🔇 в муте" + _until_text(u)
    if st == STATUS_BANNED:
        return "🚫 в бане" + _until_text(u)
    return "❔ неизвестно"


def _user_line(u):
    uid = u["id"]
    status = registry.status(uid)
    s = "%s %s (ID %d)" % (_ICONS.get(status, "❔"), _who(u), uid)
    if status in ("admin", STATUS_ACTIVE):
        s += " — сегодня %d/%s · всего %s · был %s" % (registry.usage_today(uid), registry.limit_for(uid) or "∞", u.get("total", 0), _ago(u.get("last_seen")))
    elif status == STATUS_MUTED:
        s += " — мут" + _until_text(u) + ((" · %s" % u["note"]) if u.get("note") else "")
    elif status == STATUS_PENDING:
        s += " — ждёт одобрения, заявка %s" % _ago(u.get("created_at"))
    elif status == STATUS_BANNED:
        s += " — бан" + _until_text(u) + ((" · %s" % u["note"]) if u.get("note") else "")
    return s


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not _is_admin_msg(message):
        return
    send_admin_menu(message.chat.id)


@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not _is_admin_msg(message):
        return
    text, markup = admin_list("all", 0)
    bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["user"])
def cmd_user(message):
    if not _is_admin_msg(message):
        return
    uid = _target(message)
    if uid is None or uid not in registry.users:
        safe_reply(message, "Использование: /user <id|@ник> (пользователь должен быть в списке)")
        return
    bot.send_message(message.chat.id, user_card(uid), reply_markup=user_card_markup(uid))


@bot.message_handler(commands=["adduser"])
def cmd_adduser(message):
    if not _is_admin_msg(message):
        return
    uid = _target(message)
    if uid is None:
        safe_reply(message, "Использование: /adduser <id>")
        return
    ok, msg = registry.approve(uid)
    safe_reply(message, ("✅ Пользователь %d добавлен." % uid) if ok else "⚠️ " + msg)
    if ok:
        _tell_user(uid, "✅ Тебе открыли доступ к BananGPT. Нажми /start")


@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if not _is_admin_msg(message):
        return
    uid = _target(message)
    if uid is None:
        safe_reply(message, "Использование: /ban <id|@ник> [время: 30м, 2ч, 1д, 1н] [причина]\nБез времени — навсегда.")
        return
    rest = (message.text or "").split()[2:]
    seconds = parse_duration(rest[0]) if rest else None
    reason = " ".join(rest[1:] if seconds else rest) or None
    ok, msg = registry.ban(uid, reason, seconds)
    safe_reply(message, ("🚫 Пользователь %d забанен%s." % (uid, (" на " + fmt_duration(seconds)) if seconds else " навсегда")) if ok else "⚠️ " + msg)
    if ok:
        _tell_user(uid, "🚫 Доступ к боту закрыт%s." % ((" на " + fmt_duration(seconds)) if seconds else ""))


@bot.message_handler(commands=["mute"])
def cmd_mute(message):
    if not _is_admin_msg(message):
        return
    uid = _target(message)
    rest = (message.text or "").split()[2:]
    if uid is None or not rest:
        safe_reply(message, "Использование: /mute <id|@ник> <время: 30м, 2ч, 1д, 1н | навсегда> [причина]")
        return
    forever = rest[0].lower() in ("навсегда", "inf", "forever")
    seconds = None if forever else parse_duration(rest[0])
    if not forever and seconds is None:
        safe_reply(message, "Не понял время «%s». Примеры: 30м, 2ч, 1д, 1н или «навсегда»." % rest[0])
        return
    reason = " ".join(rest[1:]) or None
    ok, msg = registry.mute(uid, seconds, reason)
    safe_reply(message, ("🔇 Пользователь %d в муте %s." % (uid, ("на " + fmt_duration(seconds)) if seconds else "пока не снимешь")) if ok else "⚠️ " + msg)
    if ok:
        _tell_user(uid, "🔇 Тебя заглушили%s%s." % ((" на " + fmt_duration(seconds)) if seconds else "", (": " + reason) if reason else ""))


@bot.message_handler(commands=["unmute"])
def cmd_unmute(message):
    if not _is_admin_msg(message):
        return
    uid = _target(message)
    if uid is None:
        safe_reply(message, "Использование: /unmute <id|@ник>")
        return
    ok = registry.unmute(uid)
    safe_reply(message, ("🔊 Мут снят с %d." % uid) if ok else "Этот пользователь не в муте.")
    if ok:
        _tell_user(uid, "🔊 Мут снят, можно писать.")


@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if not _is_admin_msg(message):
        return
    uid = _target(message)
    if uid is None:
        safe_reply(message, "Использование: /unban <id|@ник>")
        return
    ok, msg = registry.approve(uid)
    safe_reply(message, ("✅ Пользователь %d разбанен." % uid) if ok else "⚠️ " + msg)
    if ok:
        _tell_user(uid, "✅ Доступ к боту снова открыт.")


@bot.message_handler(commands=["removeuser"])
def cmd_removeuser(message):
    if not _is_admin_msg(message):
        return
    uid = _target(message)
    if uid is None:
        safe_reply(message, "Использование: /removeuser <id|@ник>")
        return
    safe_reply(message, "🗑 Удалён." if registry.remove(uid) else "Не нашёл такого пользователя (или это админ).")


@bot.message_handler(commands=["limit"])
def cmd_limit(message):
    if not _is_admin_msg(message):
        return
    parts = (message.text or "").split()
    uid = _target(message)
    if uid is None or len(parts) < 3:
        safe_reply(message, "Использование: /limit <id|@ник> <число|0|default>")
        return
    arg = parts[2].lower()
    if arg == "default":
        value = None
    elif arg.isdigit():
        value = int(arg)
    else:
        safe_reply(message, "Нужно число, 0 или default.")
        return
    if not registry.set_limit(uid, value):
        safe_reply(message, "Нет такого пользователя.")
        return
    safe_reply(message, "📊 Лимит для %d: %s" % (uid, "по умолчанию (%s)" % (DAILY_LIMIT or "∞") if value is None else (value or "без ограничений")))


def stats_text():
    with _stats_lock:
        by_model = ", ".join("%s: %d" % kv for kv in sorted(STATS["by_model"].items(), key=lambda kv: -kv[1])) or "—"
        st = dict(STATS)
    uptime = int(time.time() - STARTED_AT)
    today = sum(registry.usage_today(u["id"]) for u in registry.users.values())
    return (
        "📈 Статистика (с учётом сохранённой)\n"
        "Запросов всего: %d, сегодня: %d\nОшибок: %d, таймаутов моделей: %d\nПо моделям: %s\n"
        "Очередь к Gemini: активно %d, ждут %d (параллельно до %d)\n%s\nАптайм: %d ч %d мин"
        % (st["requests"], today, st["errors"], st["fallbacks"], by_model, QUEUE.active, QUEUE.waiting, QUEUE.n,
           _cookie_text(), uptime // 3600, uptime % 3600 // 60)
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not _is_admin_msg(message):
        return
    safe_reply(message, stats_text())


@bot.message_handler(commands=["cookies"])
def cmd_cookies(message):
    if not _is_admin_msg(message):
        return
    safe_reply(message, "🔄 Переподключаюсь к Gemini…\n" + _cookie_text())
    try:
        ensure_client_sync(force=True)
    except Exception as e:
        safe_reply(message, "❌ %s: %s" % (type(e).__name__, str(e)[:200]), quote=False)
        return
    safe_reply(message, _cookie_text() + "\nМоделей на аккаунте: %d" % len(_account_list), quote=False)


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not _is_admin_msg(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        safe_reply(message, "Использование: /broadcast <текст>")
        return
    ids = [u["id"] for u in registry.users.values() if registry.is_allowed(u["id"])]
    sent = 0
    for uid in ids:
        if _tell_user(uid, "📢 " + parts[1].strip()):
            sent += 1
    safe_reply(message, "Отправлено: %d из %d" % (sent, len(ids)))


def _tell_user(uid, text):
    try:
        bot.send_message(uid, text)
        return True
    except Exception as e:
        log.warning("Не удалось написать %s: %s", uid, e)
        return False


# ───────────────────────── админ-панель на кнопках ─────────────────────────
PAGE_SIZE = 8
MUTE_OPTIONS = [("15 мин", 900), ("1 час", 3600), ("6 часов", 21600), ("1 день", 86400), ("7 дней", 604800), ("Пока не сниму", 0)]
BAN_OPTIONS = [("1 час", 3600), ("1 день", 86400), ("7 дней", 604800), ("30 дней", 2592000), ("Навсегда", 0)]
LIMIT_OPTIONS = [("10", "10"), ("30", "30"), ("50", "50"), ("100", "100"), ("Без лимита", "0"), ("По умолчанию", "d")]
FILTERS = {
    "all": ("👥 Все пользователи", lambda st: True),
    "pend": ("⏳ Заявки", lambda st: st == STATUS_PENDING),
    "act": ("✅ Активные", lambda st: st in ("admin", STATUS_ACTIVE, STATUS_MUTED)),
    "ban": ("🚫 Баны", lambda st: st == STATUS_BANNED),
}
admin_input = {}  # id админа -> id пользователя, которому он сейчас пишет сообщение


def _count(filt):
    return sum(1 for u in registry.users.values() if FILTERS[filt][1](registry.status(u["id"])))


def admin_menu_text():
    muted = sum(1 for u in registry.users.values() if registry.status(u["id"]) == STATUS_MUTED)
    return (
        "👑 Админ-панель\n\nАктивных: %d/%d · в муте: %d · заявок: %d · в бане: %d\n\n"
        "Открой пользователя из списка — там кнопки: одобрить, мут, бан, лимит, написать, удалить.\n"
        "Команды: /user, /mute, /unmute, /ban, /unban, /limit, /adduser, /removeuser, /stats, /cookies, /broadcast"
        % (registry.active_count(), MAX_USERS, muted, _count("pend"), _count("ban"))
    )


def admin_menu_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("👥 Все (%d)" % _count("all"), callback_data="adm_list_all_0"),
        InlineKeyboardButton("⏳ Заявки (%d)" % _count("pend"), callback_data="adm_list_pend_0"),
        InlineKeyboardButton("✅ Активные (%d)" % _count("act"), callback_data="adm_list_act_0"),
        InlineKeyboardButton("🚫 Баны (%d)" % _count("ban"), callback_data="adm_list_ban_0"),
        InlineKeyboardButton("📈 Статистика", callback_data="adm_stats"),
        InlineKeyboardButton("🔑 Куки Gemini", callback_data="adm_cookies"),
    )
    markup.add(InlineKeyboardButton("❌ Закрыть", callback_data="adm_close"))
    return markup


def send_admin_menu(chat_id):
    bot.send_message(chat_id, admin_menu_text(), reply_markup=admin_menu_markup())


def admin_list(filt, page):
    title, pred = FILTERS[filt]
    rows = [u for u in registry.listing() if pred(registry.status(u["id"]))]
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = "%s (%d)\n\n%s" % (title, len(rows), "\n".join(_user_line(u) for u in chunk) or "Пока никого.")
    if pages > 1:
        text += "\n\nСтраница %d из %d" % (page + 1, pages)
    markup = InlineKeyboardMarkup(row_width=1)
    for u in chunk:
        markup.add(InlineKeyboardButton("%s %s" % (_ICONS.get(registry.status(u["id"]), "❔"), _who(u)[:40]), callback_data="adm_u_%d" % u["id"]))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="adm_list_%s_%d" % (filt, page - 1)))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="adm_list_%s_%d" % (filt, page + 1)))
    if nav:
        markup.row(*nav)
    markup.add(InlineKeyboardButton("⬅️ В меню", callback_data="adm_menu"))
    return text, markup


def user_card(uid):
    u = registry.users.get(uid)
    if not u:
        return "Пользователь не найден."
    limit, used = registry.limit_for(uid), registry.usage_today(uid)
    lines = ["👤 %s" % _who(u), "ID: %d" % uid, "Статус: %s" % _status_label(uid)]
    if u.get("note"):
        lines.append("Причина: %s" % u["note"])
    lines.append("В боте с: %s · был: %s" % (_fmt_dt(u["created_at"]) if u.get("created_at") else "—", _ago(u.get("last_seen"))))
    lines.append("Сегодня: %d/%s · всего запросов: %s" % (used, limit or "∞", u.get("total", 0)))
    lines.append("Модель: %s · режим: %s" % (get_user_model_short_name(uid), MODES[get_mode(uid)][0]))
    return "\n".join(lines)


def user_card_markup(uid):
    st = registry.status(uid)
    markup = InlineKeyboardMarkup(row_width=2)
    if st == STATUS_PENDING:
        markup.add(InlineKeyboardButton("✅ Одобрить", callback_data="adm_ok_%d" % uid),
                   InlineKeyboardButton("🚫 Отклонить", callback_data="adm_no_%d" % uid))
    elif st in (STATUS_ACTIVE, STATUS_MUTED):
        if st == STATUS_MUTED:
            first = InlineKeyboardButton("🔊 Снять мут", callback_data="adm_unmute_%d" % uid)
        else:
            first = InlineKeyboardButton("🔇 Мут", callback_data="adm_mute_%d" % uid)
        markup.add(first, InlineKeyboardButton("🚫 Бан", callback_data="adm_ban_%d" % uid),
                   InlineKeyboardButton("📊 Лимит", callback_data="adm_lim_%d" % uid),
                   InlineKeyboardButton("✉️ Написать", callback_data="adm_msg_%d" % uid),
                   InlineKeyboardButton("🗑 Удалить", callback_data="adm_del_%d" % uid))
    elif st == STATUS_BANNED:
        markup.add(InlineKeyboardButton("♻️ Разбанить", callback_data="adm_unban_%d" % uid),
                   InlineKeyboardButton("✉️ Написать", callback_data="adm_msg_%d" % uid),
                   InlineKeyboardButton("🗑 Удалить", callback_data="adm_del_%d" % uid))
    markup.add(InlineKeyboardButton("⬅️ К списку", callback_data="adm_list_all_0"))
    return markup


def _duration_menu(uid, title, options, prefix):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(*[InlineKeyboardButton(label, callback_data="adm_%s_%d_%s" % (prefix, uid, value)) for label, value in options])
    markup.add(InlineKeyboardButton("⬅️ Назад", callback_data="adm_u_%d" % uid))
    return "%s\n\n%s" % (title, user_card(uid)), markup


def _show(call, text, markup=None):
    chat_id = call.message.chat.id
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)
    except Exception as e:
        if "not modified" in str(e):
            return
        bot.send_message(chat_id, text, reply_markup=markup)


def _show_card(call, uid):
    _show(call, user_card(uid), user_card_markup(uid))


def handle_admin_callback(call):
    """Все кнопки админ-панели (callback_data начинается с adm_)."""
    chat_id = call.message.chat.id
    if not registry.is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Только для админа", show_alert=True)
        return
    p = (call.data or "").split("_")
    act = p[1] if len(p) > 1 else ""
    try:
        uid = int(p[2]) if len(p) > 2 and p[2].lstrip("-").isdigit() else None
        arg = p[3] if len(p) > 3 else None
        if act == "menu":
            _show(call, admin_menu_text(), admin_menu_markup())
        elif act == "list":
            text, markup = admin_list(p[2], int(arg or 0))
            _show(call, text, markup)
        elif act == "u":
            _show_card(call, uid)
        elif act == "ok":
            ok, msg = registry.approve(uid)
            bot.answer_callback_query(call.id, "Одобрено" if ok else msg[:150], show_alert=not ok)
            if ok:
                _tell_user(uid, "✅ Тебе открыли доступ к BananGPT. Нажми /start")
            _show_card(call, uid)
            return
        elif act == "no":
            registry.ban(uid, "заявка отклонена")
            _show_card(call, uid)
        elif act == "unban":
            ok, msg = registry.approve(uid)
            bot.answer_callback_query(call.id, "Разбанен" if ok else msg[:150], show_alert=not ok)
            if ok:
                _tell_user(uid, "✅ Доступ к боту снова открыт.")
            _show_card(call, uid)
            return
        elif act == "ban":
            text, markup = _duration_menu(uid, "🚫 На сколько забанить?", BAN_OPTIONS, "bt")
            _show(call, text, markup)
        elif act == "bt":
            seconds = int(arg or 0) or None
            ok, msg = registry.ban(uid, None, seconds)
            bot.answer_callback_query(call.id, "Забанен" if ok else msg[:150], show_alert=not ok)
            if ok:
                _tell_user(uid, "🚫 Доступ к боту закрыт%s." % ((" на " + fmt_duration(seconds)) if seconds else ""))
            _show_card(call, uid)
            return
        elif act == "mute":
            text, markup = _duration_menu(uid, "🔇 На сколько замутить?", MUTE_OPTIONS, "mt")
            _show(call, text, markup)
        elif act == "mt":
            seconds = int(arg or 0) or None
            ok, msg = registry.mute(uid, seconds)
            bot.answer_callback_query(call.id, "В муте" if ok else msg[:150], show_alert=not ok)
            if ok:
                _tell_user(uid, "🔇 Тебя заглушили%s." % ((" на " + fmt_duration(seconds)) if seconds else ""))
            _show_card(call, uid)
            return
        elif act == "unmute":
            if registry.unmute(uid):
                _tell_user(uid, "🔊 Мут снят, можно писать.")
            _show_card(call, uid)
        elif act == "lim":
            text, markup = _duration_menu(uid, "📊 Лимит запросов в день:", LIMIT_OPTIONS, "ls")
            _show(call, text, markup)
        elif act == "ls":
            registry.set_limit(uid, None if arg == "d" else int(arg))
            _show_card(call, uid)
        elif act == "del":
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(InlineKeyboardButton("🗑 Да, удалить", callback_data="adm_delok_%d" % uid),
                       InlineKeyboardButton("⬅️ Нет", callback_data="adm_u_%d" % uid))
            _show(call, "Удалить пользователя совсем (вместе с историей)?\n\n" + user_card(uid), markup)
        elif act == "delok":
            registry.remove(uid)
            text, markup = admin_list("all", 0)
            _show(call, text, markup)
        elif act == "msg":
            admin_input[call.from_user.id] = uid
            bot.send_message(chat_id, "✉️ Напиши сообщение для %s одним сообщением (или «отмена»)." % _who(registry.users.get(uid, {})))
        elif act == "stats":
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("⬅️ В меню", callback_data="adm_menu"))
            _show(call, stats_text(), markup)
        elif act == "cookies":
            bot.answer_callback_query(call.id, "Переподключаюсь…")
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("⬅️ В меню", callback_data="adm_menu"))
            try:
                ensure_client_sync(force=True)
                _show(call, "%s\nМоделей на аккаунте: %d" % (_cookie_text(), len(_account_list)), markup)
            except Exception as e:
                _show(call, "❌ %s: %s\n%s" % (type(e).__name__, str(e)[:200], _cookie_text()), markup)
            return
        elif act == "close":
            _try_delete(chat_id, call.message.message_id)
        bot.answer_callback_query(call.id)
    except Exception as e:
        log.exception("admin callback %s: %s", call.data, e)
        try:
            bot.answer_callback_query(call.id, "Ошибка: %s" % type(e).__name__, show_alert=True)
        except Exception:
            pass


# ───────────────────────── inline-кнопки ─────────────────────────
HELP_INLINE = (
    "💡 *Справка по боту*\n\n"
    "1. *Контекст диалога:*\nБот помнит суть текущего разговора. Чтобы начать новую тему — «🔄 Новый диалог».\n\n"
    "2. *Модель и режим:*\nПереключайтесь через настройки, кнопку на клавиатуре, /model и /mode.\n\n"
    "3. *Поддержка:* @isrustoy"
)


def _try_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    data = call.data or ""

    if data.startswith("adm_"):
        handle_admin_callback(call)
        return

    if data.startswith("acc_ok_") or data.startswith("acc_no_"):
        if not registry.is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "Только для админа", show_alert=True)
            return
        uid = int(data.rsplit("_", 1)[1])
        if data.startswith("acc_ok_"):
            ok, msg = registry.approve(uid)
            bot.answer_callback_query(call.id, "Одобрено" if ok else msg[:150], show_alert=not ok)
            if ok:
                _tell_user(uid, "✅ Тебе открыли доступ к BananGPT. Нажми /start")
                _try_delete(chat_id, call.message.message_id)
        else:
            registry.ban(uid, "заявка отклонена")
            bot.answer_callback_query(call.id, "Отклонено")
            _try_delete(chat_id, call.message.message_id)
        return

    if data == "show_help":
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Назад в настройки", callback_data="back_to_settings"))
        try:
            bot.edit_message_text(HELP_INLINE, chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            bot.send_message(chat_id, HELP_INLINE, parse_mode="Markdown", reply_markup=markup)
        bot.answer_callback_query(call.id)
    elif data == "close_settings":
        _try_delete(chat_id, call.message.message_id)
        bot.answer_callback_query(call.id)
    elif data == "reset_context_inline":
        clear_context(chat_id)
        bot.answer_callback_query(call.id, "Контекст диалога очищен!")
        bot.send_message(chat_id, "🔄 Контекст диалога очищен. Можем начать новую тему!", reply_markup=get_main_keyboard(chat_id))
    elif data == "open_model_selector":
        _try_delete(chat_id, call.message.message_id)
        send_model_selection_menu(chat_id)
        bot.answer_callback_query(call.id)
    elif data == "open_mode_selector":
        _try_delete(chat_id, call.message.message_id)
        send_mode_menu(chat_id)
        bot.answer_callback_query(call.id)
    elif data == "back_to_settings":
        _try_delete(chat_id, call.message.message_id)
        send_settings_menu(chat_id)
        bot.answer_callback_query(call.id)
    elif data.startswith("set_mode_"):
        key = data[len("set_mode_"):]
        if key not in MODES:
            bot.answer_callback_query(call.id, "Неизвестный режим", show_alert=True)
            return
        set_user_mode(chat_id, key)
        bot.answer_callback_query(call.id, "Режим: %s" % MODES[key][0])
        _try_delete(chat_id, call.message.message_id)
        bot.send_message(chat_id, "✅ Режим: %s" % MODES[key][0], reply_markup=get_main_keyboard(chat_id))
    elif data.startswith("set_model_"):
        key = data[len("set_model_"):]
        if key == "auto":
            set_user_model(chat_id, None)
            bot.answer_callback_query(call.id, "Модель: авто")
            _try_delete(chat_id, call.message.message_id)
            bot.send_message(chat_id, "✅ Модель: авто (по умолчанию аккаунта)", reply_markup=get_main_keyboard(chat_id))
            return
        if key not in AVAILABLE_MODELS:
            bot.answer_callback_query(call.id, "Неизвестная модель", show_alert=True)
            return
        name = AVAILABLE_MODELS[key]
        if not model_known(name):
            bot.answer_callback_query(call.id, "Этой модели нет на твоём аккаунте. Список: /models", show_alert=True)
            return
        set_user_model(chat_id, name)
        bot.answer_callback_query(call.id, "Модель изменена на %s!" % get_user_model_short_name(chat_id))
        _try_delete(chat_id, call.message.message_id)
        bot.send_message(
            chat_id, "✅ Активная модель успешно изменена на: `%s`" % name,
            parse_mode="Markdown", reply_markup=get_main_keyboard(chat_id),
        )
    else:
        bot.answer_callback_query(call.id)


# ───────────────────────── обработка сообщений ─────────────────────────
IMAGE_TRIGGERS = (
    "нарисуй фото", "сгенерируй фото", "создай фото", "сгенерируй картинку",
    "нарисуй картинку", "сделай фото", "сделай картинку", "нарисуй арт",
)
FILE_TRIGGERS = (
    "сохрани в файл", "сделай файл", "выгрузи в документе", "отправь файлом", "создай файл", "скинь файлом",
    "в pdf", "в пдф", "pdf файл", "pdf-файл", "в ворд", "в word", "в docx", "word файл", "ворд файл", "в формате pdf", "в формате docx",
)


def _wants_file(low):
    return any(kw in low for kw in FILE_TRIGGERS)


def _edit_wait(chat_id, wait_msg, position):
    bot.edit_message_text("⏳ Ты %d-й в очереди к Gemini, подожди немного…" % position, chat_id, wait_msg.message_id)


def _report_error(chat_id, wait_msg, e, what="Ошибка"):
    log.exception("%s: %s", what, e)
    bump_stat("errors")
    text = "❌ %s" % friendly_error(e)
    try:
        bot.edit_message_text(text, chat_id, wait_msg.message_id)
    except Exception:
        bot.send_message(chat_id, text)


def _quota_gate(message):
    """True — можно работать; False — лимит исчерпан (пользователю уже ответили)."""
    ok, used, limit = registry.check_quota(message.from_user.id)
    if not ok:
        safe_reply(message, "📊 Дневной лимит исчерпан: %d/%d. Новый день начнётся в 00:00 (UTC%+d). Лимит можно попросить у админа." % (used, limit, TZ_OFFSET))
    return ok


@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    uid = message.from_user.id
    registry.touch(message.from_user)
    text = (message.text or "").strip()
    low = text.lower()

    if registry.is_admin(uid) and text == "👑 Админ-панель":
        send_admin_menu(chat_id)
        return
    if uid in admin_input:
        target = admin_input.pop(uid)
        if low in ("отмена", "/cancel"):
            safe_reply(message, "Отменено.")
        elif _tell_user(target, "✉️ Сообщение от админа:\n" + text):
            safe_reply(message, "✅ Отправлено.")
        else:
            safe_reply(message, "⚠️ Не получилось отправить (пользователь мог заблокировать бота).")
        return
    if low.startswith("📝 модель:"):
        send_model_selection_menu(chat_id)
        return
    if text.endswith("Настройки"):
        send_settings_menu(chat_id)
        return
    if text.endswith("Новый диалог"):
        clear_context(chat_id)
        bot.send_message(chat_id, "✅ Контекст диалога очищен! Можем начать новую тему.", reply_markup=get_main_keyboard(chat_id))
        return
    if not text:
        return
    if text.startswith("/"):
        safe_reply(message, "Не знаю такую команду. Список: /help")
        return
    if not _quota_gate(message):
        return

    prefix = "%s_%s" % (chat_id, message.message_id)
    is_direct_image = any(kw in low for kw in IMAGE_TRIGGERS)
    force_file = _wants_file(low)
    doc_hint = detect_doc_format(low) if force_file else None

    with busy_guard(chat_id) as free:
        if not free:
            safe_reply(message, "⏳ Ещё обрабатываю предыдущий запрос — подожди секунду.")
            return
        wait_msg = safe_reply(message, "⏳ Думаю...")
        model_label = get_user_model(chat_id) or "auto"
        log.info("Сообщение chat=%s модель=%s", chat_id, model_label)
        keep_context = True
        try:
            with QUEUE.slot(on_wait=lambda pos: _edit_wait(chat_id, wait_msg, pos)):
                with keep_chat_action(chat_id, "upload_photo" if is_direct_image else "typing"):
                    if is_direct_image:
                        keep_context = False
                        reply = ask_gemini_sync(
                            "Generate a high-quality image based on this description: %s. "
                            "Please generate and return an image directly." % text,
                            prefix=prefix, chat_id=chat_id,
                        )
                        _try_delete(chat_id, wait_msg.message_id)
                        if reply.images:
                            send_image_files(chat_id, reply.images, "🎨 Сгенерировано по запросу: %s" % text[:200])
                            if reply.notice:
                                safe_reply(message, reply.notice, quote=False)
                        else:
                            send_long_message(message, reply.text)
                    else:
                        history = get_context_as_prompt(chat_id)  # до добавления текущего сообщения — без дубля
                        mode_text = MODES[get_mode(chat_id)][1]
                        reply = ask_gemini_sync(build_text_prompt(history, text, mode_text), prefix=prefix, chat_id=chat_id)
                        _try_delete(chat_id, wait_msg.message_id)
                        deliver(message, reply, prefix, force_file=force_file, doc_hint=doc_hint)
            registry.count_usage(uid)
            bump_stat("requests", model_label)
            if keep_context:
                add_to_context(chat_id, "user", text)
                add_to_context(chat_id, "model", strip_service_markers(reply.text))
        except Exception as e:
            if isinstance(e, TimeoutError):
                bump_stat("timeouts")
            _report_error(chat_id, wait_msg, e)


@bot.message_handler(content_types=["photo", "document"])
def handle_files(message):
    chat_id = message.chat.id
    uid = message.from_user.id
    registry.touch(message.from_user)
    prefix = "%s_%s" % (chat_id, message.message_id)

    if message.document and (message.document.file_size or 0) > 20 * 1024 * 1024:
        safe_reply(message, "⚠️ Файл больше 20 МБ — Telegram не отдаёт такие боту. Сожми или порежь файл.")
        return
    if not _quota_gate(message):
        return

    with busy_guard(chat_id) as free:
        if not free:
            safe_reply(message, "⏳ Ещё обрабатываю предыдущий запрос — подожди секунду.")
            return
        wait_msg = safe_reply(message, "⏳ Анализирую задачу и файл...")
        temp_path = None
        try:
            if message.photo:
                file_info = bot.get_file(message.photo[-1].file_id)
                temp_path = WORKDIR / ("upload_%s.jpg" % prefix)
            else:
                file_info = bot.get_file(message.document.file_id)
                ext = os.path.splitext(message.document.file_name or "")[1] or ".bin"
                temp_path = WORKDIR / ("upload_%s%s" % (prefix, re.sub(r"[^\w.]", "", ext)[:10]))
            temp_path.write_bytes(bot.download_file(file_info.file_path))

            caption = message.caption or "Реши задачу или проанализируй этот файл подробно и по делу."
            low = caption.lower()
            force_file = _wants_file(low) or "в файл" in low or "файлом" in low or "документом" in low
            doc_hint = detect_doc_format(low) if force_file else None

            with QUEUE.slot(on_wait=lambda pos: _edit_wait(chat_id, wait_msg, pos)):
                with keep_chat_action(chat_id, "typing"):
                    history = get_context_as_prompt(chat_id)
                    mode_text = MODES[get_mode(chat_id)][1]
                    reply = ask_gemini_sync(
                        build_file_prompt(history, caption, mode_text), file_path=str(temp_path), prefix=prefix, chat_id=chat_id,
                    )
                    _try_delete(chat_id, wait_msg.message_id)
                    deliver(message, reply, prefix, force_file=force_file, doc_hint=doc_hint)
            registry.count_usage(uid)
            bump_stat("requests", get_user_model(chat_id) or "auto")
            add_to_context(chat_id, "user", "[Отправлен файл]: %s" % caption)
            add_to_context(chat_id, "model", strip_service_markers(reply.text))
        except Exception as e:
            if isinstance(e, TimeoutError):
                bump_stat("timeouts")
            _report_error(chat_id, wait_msg, e, "Ошибка файла")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


# ───────────────────────── запуск ─────────────────────────
def init_state():
    """Читает базу в память: пользователи, их модель/режим, контекст диалогов, статистика."""
    try:
        store.init()
    except Exception as e:
        log.error("База недоступна (%s) — работаю без сохранения, всё сотрётся при перезапуске", e)
        registry.store = None
    registry.load(ALLOWED_IDS)
    for uid, u in registry.users.items():
        if u.get("model"):
            user_selected_model[uid] = None if u["model"] == "auto" else u["model"]
        if u.get("mode") in MODES:
            user_mode[uid] = u["mode"]
    if registry.store is not None:
        try:
            user_context.update(registry.store.load_contexts())
            saved = registry.store.get_kv("stats")
            if saved:
                loaded = json.loads(saved)
                STATS.update({k: loaded[k] for k in loaded if k in STATS})
        except Exception as e:
            log.warning("Не удалось загрузить сохранённое состояние: %s", e)
    log.info("Пользователей в базе: %d (активных %d), админы: %s, хранилище: %s",
             len(registry.users), registry.active_count(), sorted(ADMIN_IDS) or "не заданы",
             ("Postgres" if store.kind == "pg" else "SQLite %s" % store.path) if registry.store else "нет")


def _warmup():
    """Подключаемся к Gemini заранее: ошибки кук видно сразу в логах и у админа, а не на первом сообщении."""
    try:
        ensure_client_sync()
        log.info("Gemini подключён. Модель по умолчанию: %s", default_model() or "авто (аккаунт)")
    except Exception as e:
        log.error("Gemini не подключился (проверь GEMINI_SECURE_1PSID / 1PSIDTS): %s", e)
        update_cookie_state(False, type(e).__name__)


def _register_commands():
    common = [
        ("model", "Выбор модели"), ("mode", "Режим ответов"), ("me", "Мой доступ и лимит"),
        ("new", "Новый диалог"), ("ping", "Проверка связи с Gemini"), ("help", "Помощь"),
    ]
    admin_extra = [
        ("users", "Пользователи"), ("stats", "Статистика"), ("cookies", "Состояние кук"),
        ("admin", "Команды админа"),
    ]
    try:
        bot.set_my_commands([telebot.types.BotCommand(c, d) for c, d in common])
        for admin_id in ADMIN_IDS:
            bot.set_my_commands(
                [telebot.types.BotCommand(c, d) for c, d in common + admin_extra],
                scope=telebot.types.BotCommandScopeChat(admin_id),
            )
    except Exception as e:
        log.warning("set_my_commands: %s", e)


def main():
    if harden_process():
        log.info("Процесс защищён от чтения /proc/<pid>/environ дочерними процессами")
    init_state()
    install_cookie_watch()
    start_health_server()
    start_keepalive()
    if not ADMIN_IDS:
        log.warning("Не задан ALLOWED_IDS/ADMIN_IDS — бот никому не отвечает, кроме /myid. Задай ALLOWED_IDS.")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    _register_commands()
    threading.Thread(target=_warmup, daemon=True).start()
    start_health_loop()
    log.info("🍌 BananGPT запущен")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            log.error("Перезапуск polling после сбоя: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
