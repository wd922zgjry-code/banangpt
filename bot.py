# -*- coding: utf-8 -*-
"""BananGPT — Telegram-бот поверх личного веб-аккаунта Gemini (по кукам).

Умеет: диалог с контекстом, переключение моделей (кнопка и /model), авто-графики matplotlib
(выполняются в изолированном процессе), генерацию картинок, выдачу ответа файлом,
разбор присланных фото/файлов. Весь ответ чистится от Markdown/LaTeX, знаки умножения сохраняются.
"""
import asyncio
import concurrent.futures
import inspect
import io
import logging
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
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

from chartbox import ChartError, harden_process, render_charts
from textfmt import (
    clean_formatting,
    find_chart_code,
    remove_chart_blocks,
    split_text,
    strip_service_markers,
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
BOT_THREADS = int(os.getenv("BOT_THREADS", "8"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана переменная окружения TELEGRAM_TOKEN!")


def _parse_ids(raw):
    ids = set()
    for part in re.split(r"[,\s;]+", raw or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# Кому можно пользоваться ботом (Telegram user id через запятую). Пусто = никому.
ALLOWED_IDS = _parse_ids(os.getenv("ALLOWED_IDS", ""))

WORKDIR = Path(tempfile.gettempdir()) / "bananbot_work"
WORKDIR.mkdir(parents=True, exist_ok=True)

bot = telebot.TeleBot(TELEGRAM_TOKEN, num_threads=BOT_THREADS)

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

user_selected_model = {}
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


def default_model():
    """Первый доступный пресет; если список моделей неизвестен — модель по умолчанию у Google (None)."""
    if _account_names is not None:
        for _key, _label, _emo, name in MODEL_PRESETS:
            if _norm(name) in _account_names:
                return name
    return None


def get_user_model(chat_id):
    return user_selected_model.get(chat_id, default_model())


def get_user_model_short_name(chat_id):
    model = get_user_model(chat_id)
    if model is None:
        return "авто"
    return _SHORT_NAMES.get(model, model)


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
            await client.init(timeout=60, auto_close=False, auto_refresh=True)
            _gemini_client = client
            await _discover_models(client)
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


def ask_gemini_sync(prompt, file_path=None, prefix="0", chat_id=None):
    _ensure_loop()
    model = get_user_model(chat_id) if chat_id is not None else default_model()
    fut = asyncio.run_coroutine_threadsafe(_ask_async(prompt, file_path, prefix, model), bot_loop)
    try:
        return fut.result(timeout=REQUEST_TIMEOUT)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        log.error("Таймаут %d с: Gemini не ответил (модель=%s). Смотри строки выше.", REQUEST_TIMEOUT, model or "авто")
        raise TimeoutError("Gemini не ответил за %d с" % REQUEST_TIMEOUT)


def ensure_client_sync(timeout=90):
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(_get_client(), bot_loop).result(timeout=timeout)


# ───────────────────────── контекст диалога ─────────────────────────
user_context = {}
_ctx_lock = threading.Lock()
MAX_HISTORY = 10
MAX_ITEM_CHARS = 1500


def add_to_context(chat_id, role, text):
    with _ctx_lock:
        items = user_context.setdefault(chat_id, [])
        items.append({"role": role, "text": (text or "")[:MAX_ITEM_CHARS]})
        del items[:-MAX_HISTORY]


def get_context_as_prompt(chat_id):
    with _ctx_lock:
        items = list(user_context.get(chat_id, []))
    labels = {"user": "Пользователь", "model": "Ассистент"}
    return "\n".join("%s: %s" % (labels.get(i["role"], i["role"]), i["text"]) for i in items)


def clear_context(chat_id):
    """Чистит только историю. Выбранная модель остаётся."""
    with _ctx_lock:
        user_context[chat_id] = []


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
    "напиши метку [SEND_AS_FILE:имя_файла.расширение], ниже — содержимое файла. Только текстовые форматы "
    "(txt, md, py, csv, json, html…).\n"
)
RULES_IMAGE = (
    "4. АВТО-ФОТО: если пользователь просит нарисовать художественную картинку, иллюстрацию или фото "
    "(а не математический график) — напиши метку [GENERATE_IMAGE:подробное описание картинки на английском].\n"
)


def build_text_prompt(history, user_text):
    return (
        "Ты умный универсальный ассистент BananGPT. Отвечай на языке пользователя.\n"
        "--- ИСТОРИЯ ДИАЛОГА ---\n%s\n--- КОНЕЦ ИСТОРИИ ---\n\n"
        "Запрос или задача пользователя: %s\n\n%s%s" % (history or "(пусто)", user_text, RULES_TEXT, RULES_IMAGE)
    )


def build_file_prompt(history, caption):
    return (
        "Ты умный универсальный ассистент BananGPT. Отвечай на языке пользователя.\n"
        "--- ИСТОРИЯ ДИАЛОГА ---\n%s\n--- КОНЕЦ ИСТОРИИ ---\n\n"
        "Запрос пользователя к прикреплённому файлу: %s\n\n%s" % (history or "(пусто)", caption, RULES_TEXT)
    )


# ───────────────────────── клавиатуры и сообщения ─────────────────────────
def get_main_keyboard(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(KeyboardButton("📝 Модель: %s" % get_user_model_short_name(chat_id)))
    markup.row(KeyboardButton("🔄 Новый диалог"), KeyboardButton("⚙️ Настройки"))
    return markup


def send_settings_menu(chat_id):
    current = get_user_model(chat_id) or "по умолчанию (аккаунт)"
    text = (
        "⚙️ *Панель настроек*\n\n"
        "🤖 Активная модель: `%s`\n"
        "🧠 Память контекста: *Включена* (до %d реплик)\n\n"
        "Выберите необходимое действие:" % (current, MAX_HISTORY)
    )
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(InlineKeyboardButton("🤖 Выбрать модель", callback_data="open_model_selector"))
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
CODE_LANG_EXT = {
    "python": "py", "py": "py", "json": "json", "html": "html", "js": "js", "javascript": "js",
    "cpp": "cpp", "c": "c", "csv": "csv", "sql": "sql", "xml": "xml", "md": "md", "txt": "txt",
}


def send_as_file(message, reply_text, prefix, file_marker):
    chat_id = message.chat.id
    clean_reply = re.sub(r"\[SEND_AS_FILE:[^\]]+\]", "", reply_text).strip()
    code_block = re.search(r"```([a-zA-Z0-9_+-]+)?[ \t]*\n(.*?)```", clean_reply, re.DOTALL)
    explanation = ""
    note = ""

    if file_marker:
        raw = os.path.basename(file_marker.group(1).strip())
        name = re.sub(r"[^\w.\-]+", "_", raw).strip("._")[:80] or "document.txt"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in TEXT_EXTS:
            # docx/pdf/xlsx из текста не сделать — отдаём честный .txt вместо битого файла
            stem = name.rsplit(".", 1)[0] if "." in name else name
            note = "ℹ️ Формат .%s из текста не собрать, отправляю как .txt." % ext if ext else ""
            name, ext = stem + ".txt", "txt"
    else:
        lang = ((code_block.group(1) if code_block else None) or "txt").lower()
        ext = CODE_LANG_EXT.get(lang, "txt")
        name = "result_%s.%s" % (prefix, ext)

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


def deliver(message, reply, prefix, force_file=False):
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
        if file_marker or (force_file and not chart_code):
            send_as_file(message, text, prefix, file_marker)
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


# ───────────────────────── доступ ─────────────────────────
def is_allowed(user_id):
    return user_id in ALLOWED_IDS


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    bot.reply_to(message, "Твой Telegram ID: %s" % message.from_user.id)


_ALL_TYPES = ["text", "photo", "document", "voice", "audio", "video", "sticker", "video_note", "animation"]


@bot.message_handler(func=lambda m: not is_allowed(m.from_user.id), content_types=_ALL_TYPES)
def deny_message(message):
    log.warning("Отказ в доступе: user_id=%s", message.from_user.id)
    if not ALLOWED_IDS:
        bot.reply_to(
            message,
            "⛔ Бот ещё не настроен. Твой ID: %s — впиши его в переменную ALLOWED_IDS на сервере." % message.from_user.id,
        )


@bot.callback_query_handler(func=lambda c: not is_allowed(c.from_user.id))
def deny_callback(call):
    bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)


# ───────────────────────── команды ─────────────────────────
HELP_TEXT = (
    "BananGPT 🍌\n\n"
    "• Пиши любой вопрос или задачу — бот помнит последние %d реплик.\n"
    "• Фото/файл с подписью — разберу и решу.\n"
    "• «нарисуй картинку …» — сгенерирую изображение.\n"
    "• «сделай файл …» / «отправь файлом» — пришлю ответ файлом.\n"
    "• Графики и эпюры строю сам, если они нужны по задаче.\n\n"
    "Команды:\n"
    "/model — выбор модели (/model pro, /model flash, /model argon, /model auto)\n"
    "/models — какие модели доступны на твоём аккаунте\n"
    "/new — очистить контекст\n"
    "/status — состояние бота\n"
    "/ping — проверка связи с Gemini\n"
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
        user_selected_model[chat_id] = None
        safe_reply(message, "✅ Модель: авто (Google выбирает по умолчанию для аккаунта)")
        return
    name, asked = resolve_model_choice(parts[1])
    if name is None:
        keys = ", ".join(PRESET_KEYS)
        safe_reply(message, "⚠️ Модели «%s» нет на твоём аккаунте. Ключи: %s. Весь список: /models" % (asked, keys))
        return
    user_selected_model[chat_id] = name
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
            reply = ask_gemini_sync("Ответь одним словом: ок", prefix="ping_%s" % chat_id, chat_id=chat_id)
            out = "✅ Gemini ответил за %.1f с: %s" % (time.time() - t0, (reply.text or "")[:100])
            if reply.notice:
                out += "\n" + reply.notice
        except Exception as e:
            log.exception("ping")
            out = "❌ %s через %.1f с: %s" % (type(e).__name__, time.time() - t0, str(e)[:300])
        safe_reply(message, out, quote=False)


@bot.message_handler(commands=["status"])
def cmd_status(message):
    state = "подключён" if _gemini_client is not None else "ещё не подключался"
    models = ", ".join(n for n, _ in _account_list) or "список неизвестен"
    safe_reply(
        message,
        "Gemini: %s\nМодели: %s\nТвоя модель: %s\nPython: %s\nРазрешённых пользователей: %d"
        % (state, models, get_user_model_short_name(message.chat.id), sys.version.split()[0], len(ALLOWED_IDS)),
    )


# ───────────────────────── inline-кнопки ─────────────────────────
HELP_INLINE = (
    "💡 *Справка по боту*\n\n"
    "1. *Контекст диалога:*\nБот помнит суть текущего разговора. Чтобы начать новую тему — «🔄 Новый диалог».\n\n"
    "2. *Выбор модели:*\nПереключайтесь между моделями через настройки, кнопку на клавиатуре или /model.\n\n"
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
    elif data == "back_to_settings":
        _try_delete(chat_id, call.message.message_id)
        send_settings_menu(chat_id)
        bot.answer_callback_query(call.id)
    elif data.startswith("set_model_"):
        key = data[len("set_model_"):]
        if key == "auto":
            user_selected_model[chat_id] = None
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
        user_selected_model[chat_id] = name
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
FILE_TRIGGERS = ("сохрани в файл", "сделай файл", "выгрузи в документе", "отправь файлом", "создай файл", "скинь файлом")


def _report_error(chat_id, wait_msg, e, what="Ошибка"):
    log.exception("%s: %s", what, e)
    text = "❌ %s" % friendly_error(e)
    try:
        bot.edit_message_text(text, chat_id, wait_msg.message_id)
    except Exception:
        bot.send_message(chat_id, text)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    low = text.lower()

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

    prefix = "%s_%s" % (chat_id, message.message_id)
    is_direct_image = any(kw in low for kw in IMAGE_TRIGGERS)
    force_file = any(kw in low for kw in FILE_TRIGGERS)

    with busy_guard(chat_id) as free:
        if not free:
            safe_reply(message, "⏳ Ещё обрабатываю предыдущий запрос — подожди секунду.")
            return
        wait_msg = safe_reply(message, "⏳ Думаю...")
        log.info("Сообщение chat=%s модель=%s", chat_id, get_user_model(chat_id))
        try:
            with keep_chat_action(chat_id, "upload_photo" if is_direct_image else "typing"):
                if is_direct_image:
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
                    return

                history = get_context_as_prompt(chat_id)  # до добавления текущего сообщения — без дубля
                reply = ask_gemini_sync(build_text_prompt(history, text), prefix=prefix, chat_id=chat_id)
                _try_delete(chat_id, wait_msg.message_id)
                deliver(message, reply, prefix, force_file=force_file)
            add_to_context(chat_id, "user", text)
            add_to_context(chat_id, "model", strip_service_markers(reply.text))
        except Exception as e:
            _report_error(chat_id, wait_msg, e)


@bot.message_handler(content_types=["photo", "document"])
def handle_files(message):
    chat_id = message.chat.id
    prefix = "%s_%s" % (chat_id, message.message_id)

    if message.document and (message.document.file_size or 0) > 20 * 1024 * 1024:
        safe_reply(message, "⚠️ Файл больше 20 МБ — Telegram не отдаёт такие боту. Сожми или порежь файл.")
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
            force_file = any(kw in caption.lower() for kw in ("в файл", "файлом", "документом"))

            with keep_chat_action(chat_id, "typing"):
                history = get_context_as_prompt(chat_id)
                reply = ask_gemini_sync(
                    build_file_prompt(history, caption), file_path=str(temp_path), prefix=prefix, chat_id=chat_id,
                )
                _try_delete(chat_id, wait_msg.message_id)
                deliver(message, reply, prefix, force_file=force_file)
            add_to_context(chat_id, "user", "[Отправлен файл]: %s" % caption)
            add_to_context(chat_id, "model", strip_service_markers(reply.text))
        except Exception as e:
            _report_error(chat_id, wait_msg, e, "Ошибка файла")
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


# ───────────────────────── запуск ─────────────────────────
def _warmup():
    """Подключаемся к Gemini заранее: ошибки кук видно сразу в логах, а не на первом сообщении."""
    try:
        ensure_client_sync()
        log.info("Gemini подключён. Модель по умолчанию: %s", default_model() or "авто (аккаунт)")
    except Exception as e:
        log.error("Gemini не подключился (проверь GEMINI_SECURE_1PSID / 1PSIDTS): %s", e)


def main():
    if harden_process():
        log.info("Процесс защищён от чтения /proc/<pid>/environ дочерними процессами")
    start_health_server()
    start_keepalive()
    if not ALLOWED_IDS:
        log.warning("ALLOWED_IDS пуст — бот никому не отвечает, кроме /myid. Задай ALLOWED_IDS.")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        bot.set_my_commands([
            telebot.types.BotCommand("model", "Выбор модели"),
            telebot.types.BotCommand("models", "Модели аккаунта"),
            telebot.types.BotCommand("new", "Новый диалог"),
            telebot.types.BotCommand("status", "Состояние бота"),
            telebot.types.BotCommand("ping", "Проверка связи с Gemini"),
            telebot.types.BotCommand("help", "Помощь"),
            telebot.types.BotCommand("myid", "Мой Telegram ID"),
        ])
    except Exception as e:
        log.warning("set_my_commands: %s", e)
    threading.Thread(target=_warmup, daemon=True).start()
    log.info("🍌 BananGPT запущен")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            log.error("Перезапуск polling после сбоя: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
