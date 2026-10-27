# -*- coding: utf-8 -*-
"""Автотесты без сети: Telegram и Gemini подменены заглушками.
Запуск:  python test_bot.py        (нужны только numpy и matplotlib)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP_DB = os.path.join(tempfile.mkdtemp(), "boot.db")
os.environ.update(TELEGRAM_TOKEN="1:test", GEMINI_SECURE_1PSID="psid", GEMINI_SECURE_1PSIDTS="psidts",
                  ALLOWED_IDS="42", DB_PATH=_TMP_DB, HEALTH_INTERVAL="0")

PNG = b"\x89PNG\r\n\x1a\n"


# ───────────── заглушка Gemini ─────────────
class FakeState:
    reply = "ok"                # str | callable(prompt, model, files) -> str | (str, n_images)
    valid_models = ["gemini-flash", "gemini-pro"]
    list_models_result = "default"
    strict_model = True
    hang_models = set()
    on_init = None
    calls = []
    inits = 0


class FakeImage:
    async def save(self, path=".", filename="x.png", **kw):
        p = Path(path) / filename
        p.write_bytes(PNG + b"fake")
        return str(p)


class FakeModel:
    def __init__(self, name, disp):
        self.model_name, self.display_name, self.is_available = name, disp, True


class FakeClient:
    def __init__(self, secure_1psid=None, secure_1psidts=None, proxy=None):  # как в новой gemini-webapi: proxy, а не proxies
        self.psid, self.psidts = secure_1psid, secure_1psidts

    async def init(self, **kw):
        FakeState.inits += 1
        if FakeState.on_init:
            FakeState.on_init()

    async def close(self):
        pass

    def list_models(self):
        if FakeState.list_models_result == "default":
            return [FakeModel(n, n.replace("gemini-", "").title()) for n in FakeState.valid_models]
        return FakeState.list_models_result

    async def generate_content(self, prompt, model=None, files=None, **kw):
        FakeState.calls.append(SimpleNamespace(prompt=prompt, model=model, files=files))
        if model in FakeState.hang_models:
            await asyncio.sleep(60)
        if model and FakeState.strict_model and model not in FakeState.valid_models:
            raise ValueError("unknown model %s" % model)
        r = FakeState.reply(prompt, model, files) if callable(FakeState.reply) else FakeState.reply
        n_img = 0
        if isinstance(r, tuple):
            r, n_img = r
        return SimpleNamespace(text=r, images=[FakeImage() for _ in range(n_img)])


# ───────────── заглушка Telegram ─────────────
class Markup:
    def __init__(self, *a, **k):
        self.rows = []

    def row(self, *b):
        self.rows.append(b)

    def add(self, *b):
        self.rows.append(b)


class FakeTeleBot:
    def __init__(self, token, **kw):
        self.calls = []

    def message_handler(self, *a, **k):
        return lambda f: f

    callback_query_handler = message_handler

    def _rec(self, name, *a, **k):
        data = None
        for x in a[1:2] if name in ("send_photo", "send_document") else []:
            data = x.getvalue() if hasattr(x, "getvalue") else x.read()
            if hasattr(x, "seek"):
                x.seek(0)
            k["_name"] = getattr(x, "name", None)
        self.calls.append(SimpleNamespace(name=name, args=a, kw=k, data=data))
        return SimpleNamespace(message_id=len(self.calls), chat=SimpleNamespace(id=a[0] if a and isinstance(a[0], int) else 0))

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: self._rec(name, *a, **k)

    def reply_to(self, message, text, **k):
        return self._rec("reply_to", message.chat.id, text, **k)

    def get_file(self, fid):
        return SimpleNamespace(file_path="p/" + fid)

    def download_file(self, path):
        return b"binary-upload"


tb = types.ModuleType("telebot")
tb.TeleBot = FakeTeleBot
tb.types = types.ModuleType("telebot.types")
for n in ("InlineKeyboardButton", "KeyboardButton"):
    setattr(tb.types, n, lambda *a, **k: SimpleNamespace(args=a, kw=k))
tb.types.InlineKeyboardMarkup = tb.types.ReplyKeyboardMarkup = Markup
tb.types.BotCommand = lambda *a: a
tb.types.BotCommandScopeChat = lambda *a: a
sys.modules.update({"telebot": tb, "telebot.types": tb.types})
gw = types.ModuleType("gemini_webapi")
gw.GeminiClient = FakeClient
sys.modules["gemini_webapi"] = gw

import bot as B                       # noqa: E402
from store import Store               # noqa: E402
from users import UserRegistry        # noqa: E402
import chartbox as CB                 # noqa: E402
from textfmt import clean_formatting as cf, split_text, strip_service_markers  # noqa: E402

GOOD_CHART = "```python\nimport numpy as np\nimport matplotlib.pyplot as plt\nx = np.linspace(0, 10, 50)\nplt.plot(x, x**2)\nplt.grid(True)\n```"


def msg(text=None, user=42, chat=None, mid=1, **kw):
    return SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=user, username=kw.get("username"), first_name=kw.get("first_name", "U%d" % user)),
        chat=SimpleNamespace(id=user if chat is None else chat),
        message_id=mid, photo=kw.get("photo"), document=kw.get("document"), caption=kw.get("caption"),
    )


def to_chat(chat_id):
    """Тексты, отправленные конкретному чату через send_message."""
    return [c.args[1] for c in B.bot.calls if c.name == "send_message" and c.args and c.args[0] == chat_id and len(c.args) > 1]


def buttons_of(call_rec):
    """[(текст, callback_data)] из reply_markup записанного вызова."""
    mk = call_rec.kw.get("reply_markup")
    out = []
    for row in getattr(mk, "rows", []):
        for b in row:
            if hasattr(b, "kw"):
                out.append((b.args[0] if b.args else "", b.kw.get("callback_data")))
    return out


def last_buttons():
    for c in reversed(B.bot.calls):
        if c.name in ("send_message", "edit_message_text") and getattr(c.kw.get("reply_markup"), "rows", None):
            return buttons_of(c)
    return []


def last_panel_text():
    for c in reversed(B.bot.calls):
        if c.name == "edit_message_text":
            return c.args[0]
        if c.name == "send_message" and isinstance(c.args[1], str) and c.kw.get("reply_markup") is not None:
            return c.args[1]
    return ""


def click(data, user=42, chat=None, mid=5):
    call = SimpleNamespace(id="c", data=data, from_user=SimpleNamespace(id=user),
                           message=SimpleNamespace(chat=SimpleNamespace(id=user if chat is None else chat), message_id=mid))
    B.handle_callbacks(call)
    return call


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def sent_texts(calls=None):
    out = []
    for c in (calls or B.bot.calls):
        if c.name == "edit_message_text" and c.args:      # edit_message_text(text, chat_id, message_id)
            out.append(c.args[0])
        elif c.name in ("reply_to", "send_message") and len(c.args) > 1 and isinstance(c.args[1], str):
            out.append(c.args[1])
    return out


# ═════════════════ форматирование ═════════════════
class TextFormat(unittest.TestCase):
    def test_bold_and_stars_removed(self):
        self.assertEqual(cf("**Важно:** значение *равно* 5"), "Важно: значение равно 5")
        self.assertEqual(cf("***ж-к***"), "ж-к")
        self.assertEqual(cf("* пункт\n* пункт 2"), "• пункт\n• пункт 2")

    def test_multiplication_signs_survive(self):
        for s in ("B = F / (I * l)", "2*3*4", "m*g*h", "(a+b)*(c+d)", "F * m * a", "3 * (x + 1) * 2"):
            self.assertEqual(cf(s), s, s)

    def test_powers_keep_their_stars_as_caret(self):
        self.assertEqual(cf("x**2 + y**2"), "x^2 + y^2")
        self.assertEqual(cf("(a+b)**2"), "(a+b)^2")

    def test_latex_converted(self):
        self.assertEqual(cf("$B = \\frac{F}{I \\cdot l}$"), "B = F/(I · l)")
        self.assertEqual(cf("\\[ E = \\frac{1}{2} m v^{2} \\]"), "E = 1/2 m v²")
        self.assertEqual(cf("$\\sqrt{x^2+y^2}$"), "√(x²+y²)")
        self.assertEqual(cf("$\\mu_0 I_1$"), "μ₀ I₁")
        self.assertEqual(cf("угол $45^{\\circ}$ ок"), "угол 45° ок")
        self.assertEqual(cf("$$\\vec{B} = \\mu_0 \\cdot \\frac{I}{2 \\pi r}$$"), "B⃗ = μ₀ · I/(2 π r)")

    def test_prices_hashtags_and_code_untouched(self):
        self.assertEqual(cf("Цена $100 за штуку и $200 за две"), "Цена $100 за штуку и $200 за две")
        self.assertEqual(cf("#1 в рейтинге"), "#1 в рейтинге")
        self.assertEqual(cf("## Заголовок"), "Заголовок")
        self.assertEqual(cf("```python\nx = 2**3  # $100\n```"), "x = 2**3  # $100")

    def test_split_text(self):
        text = ("абв " * 30 + "\n") * 100
        parts = split_text(text, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""), text.replace("\n", "").replace(" ", ""))

    def test_service_markers_stripped_for_history(self):
        t = "[SEND_AS_FILE:a.txt] привет " + GOOD_CHART
        self.assertEqual(strip_service_markers(t), "привет [построен график]")


# ═════════════════ песочница графиков ═════════════════
class Chartbox(unittest.TestCase):
    def test_renders_png_and_ignores_savefig_path(self):
        target = Path(tempfile.gettempdir()) / "banan_escape_test.png"
        target.unlink(missing_ok=True)
        code = "import numpy as np\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.savefig('%s')\nplt.show()" % target
        out = CB.render_charts(code)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(PNG))
        self.assertFalse(target.exists(), "savefig не должен писать куда хочет")

    def test_two_figures_two_pngs(self):
        out = CB.render_charts("import matplotlib.pyplot as plt\nplt.figure(); plt.plot([1])\nplt.figure(); plt.plot([2])")
        self.assertEqual(len(out), 2)

    def test_malicious_code_rejected(self):
        bad = [
            "import os\nos.system('id')",
            "import subprocess",
            "open('/etc/passwd').read()",
            "import matplotlib\nmatplotlib.os.system('id')",
            "x = ().__class__.__bases__",
            "from numpy import *",
            "import numpy as np\nnp.load('x.npy')",
            "print('{0.__class__}'.format(1))",
            "exec('1')",
            "from matplotlib.image import imread",
            "import matplotlib.pyplot as plt\nplt.imread('/etc/hostname')",
        ]
        for code in bad:
            with self.assertRaises(CB.ChartError, msg=code):
                CB.render_charts(code, timeout=5)

    def test_infinite_loop_times_out(self):
        with self.assertRaises(CB.ChartError) as cm:
            CB.render_charts("while True:\n    pass", timeout=3)
        self.assertIn("долго", str(cm.exception))

    def test_memory_bomb_stopped(self):
        with self.assertRaises(CB.ChartError):
            CB.render_charts("import numpy as np\nnp.zeros(10**11)", timeout=15)

    def test_child_env_has_no_secrets(self):
        os.environ["TELEGRAM_TOKEN"], os.environ["GEMINI_SECURE_1PSID"] = "SECRET1", "SECRET2"
        env = CB._child_env(Path(tempfile.gettempdir()))
        self.assertNotIn("SECRET1", env.values())
        self.assertNotIn("SECRET2", env.values())
        os.environ["TELEGRAM_TOKEN"], os.environ["GEMINI_SECURE_1PSID"] = "1:test", "psid"

    @unittest.skipUnless(sys.platform.startswith("linux"), "только Linux")
    def test_child_cannot_read_parent_environ_after_hardening(self):
        """Дочерний процесс (того же пользователя) не должен читать /proc/<бот>/environ."""
        helper = textwrap.dedent("""
            import os, subprocess, sys
            sys.path.insert(0, %r)
            import chartbox
            if sys.argv[1] == "harden":
                chartbox.harden_process()
            r = subprocess.run([sys.executable, "-c",
                "import sys\\ntry:\\n print('READ' if b'BOT_SECRET' in open('/proc/%%s/environ' %% sys.argv[1],'rb').read() else 'EMPTY')\\nexcept Exception: print('DENIED')",
                str(os.getpid())], capture_output=True, text=True)
            print(r.stdout.strip())
        """ % str(HERE))
        kw = {}
        if os.getuid() == 0:
            kw["user"] = 65534  # иначе root читает всё, и проверка бессмысленна
        env = dict(os.environ, BOT_SECRET="1")
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o755)
            f = Path(d) / "h.py"
            f.write_text(helper)
            os.chmod(f, 0o644)
            run = lambda mode: subprocess.run([sys.executable, str(f), mode], capture_output=True, text=True, env=env, cwd=d, **kw)
            plain, hard = run("plain"), run("harden")
        if plain.stdout.strip() != "READ":
            self.skipTest("в этой среде не удалось воспроизвести чтение environ (%s %s)" % (plain.stdout.strip(), plain.stderr[-120:]))
        self.assertEqual(hard.stdout.strip(), "DENIED", hard.stderr)


# ═════════════════ бот ═════════════════
class BotFlow(unittest.TestCase):
    def setUp(self):
        B.bot.calls.clear()
        B._gemini_client = None
        B._account_names, B._account_list = None, []
        B.user_selected_model.clear()
        B.user_mode.clear()
        B.user_context.clear()
        B._busy.clear()
        B.store = Store(path=os.path.join(tempfile.mkdtemp(), "t.db"))
        B.store.init()
        B.registry = UserRegistry(B.store, {42}, 10, 3, 3)
        B.registry.load({42})
        B.ADMIN_IDS, B.ALLOW_REQUESTS = {42}, True
        B.QUEUE = B.GeminiQueue(2)
        B.cookie_watch.update(unauth=False, ok=None, last_alert=0.0, detail="")
        B._health.update(fails=0, last_ok=0.0, alerted=False)
        B.STATS.update(requests=0, errors=0, timeouts=0, fallbacks=0)
        B.STATS["by_model"] = {}
        FakeState.reply, FakeState.calls, FakeState.inits = "ok", [], 0
        FakeState.valid_models = ["gemini-flash", "gemini-pro"]
        FakeState.list_models_result, FakeState.strict_model = "default", True
        FakeState.hang_models, FakeState.on_init = set(), None
        B.MODEL_TIMEOUT, B.DEFAULT_MODEL = 60, ""

    def add_user(self, uid, **kw):
        B.registry.register_pending(SimpleNamespace(id=uid, username=kw.get("username"), first_name=kw.get("first_name", "U%d" % uid)))
        assert B.registry.approve(uid)[0]

    def names(self, kind):
        return [c for c in B.bot.calls if c.name == kind]

    # ── доступ ──
    def test_stranger_becomes_pending_and_admin_gets_request(self):
        B.deny_message(msg("hi", user=7, username="ivan"))
        self.assertEqual(B.registry.status(7), "pending")
        self.assertTrue(any("Заявка на доступ" in x and "ID: 7" in x for x in to_chat(42)))
        self.assertTrue(any("Заявка отправлена" in x for x in sent_texts()))
        B.bot.calls.clear()
        B.deny_message(msg("ещё раз", user=7))
        self.assertEqual(to_chat(42), [])  # админа не спамим
        self.assertTrue(any("ждёт одобрения" in x for x in sent_texts()))

    def test_banned_user_is_ignored(self):
        B.registry.ban(7, "спам")
        B.deny_message(msg("hi", user=7))
        self.assertEqual(B.bot.calls, [])

    def test_unconfigured_admins_show_user_id(self):
        B.ADMIN_IDS = set()
        B.deny_message(msg("hi", user=7))
        self.assertIn("ID: 7", sent_texts()[0])
        self.assertFalse(B.is_allowed(7))

    def test_requests_can_be_disabled(self):
        B.ALLOW_REQUESTS = False
        B.deny_message(msg("hi", user=7))
        self.assertIn("Нет доступа", sent_texts()[0])
        self.assertIsNone(B.registry.status(7))

    def test_admin_approves_request_via_button(self):
        B.deny_message(msg("hi", user=7))
        B.handle_callbacks(SimpleNamespace(id="c", data="acc_ok_7", from_user=SimpleNamespace(id=42),
                                           message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=9)))
        self.assertTrue(B.is_allowed(7))
        self.assertTrue(any("открыли доступ" in x for x in to_chat(7)))

    def test_non_admin_cannot_approve(self):
        self.add_user(43)
        B.registry.register_pending(SimpleNamespace(id=7, username=None, first_name="X"))
        B.handle_callbacks(SimpleNamespace(id="c", data="acc_ok_7", from_user=SimpleNamespace(id=43),
                                           message=SimpleNamespace(chat=SimpleNamespace(id=43), message_id=9)))
        self.assertFalse(B.is_allowed(7))
        self.assertTrue(self.names("answer_callback_query")[-1].kw.get("show_alert"))

    def test_reject_button_bans(self):
        B.deny_message(msg("hi", user=7))
        B.handle_callbacks(SimpleNamespace(id="c", data="acc_no_7", from_user=SimpleNamespace(id=42),
                                           message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=9)))
        self.assertEqual(B.registry.status(7), "banned")

    def test_user_cap(self):
        B.registry.max_users = 2
        self.add_user(43), self.add_user(44)
        B.registry.register_pending(SimpleNamespace(id=45, username=None, first_name="X"))
        ok, text = B.registry.approve(45)
        self.assertFalse(ok)
        self.assertIn("лимит", text)

    def test_myid(self):
        B.cmd_myid(msg("/myid", user=7))
        self.assertIn("7", sent_texts()[0])

    def test_stranger_callback_denied(self):
        B.deny_callback(SimpleNamespace(id="c1", from_user=SimpleNamespace(id=7)))
        self.assertTrue(self.names("answer_callback_query")[0].kw.get("show_alert"))

    # ── админ-панель ──
    def test_users_list_for_admin_only(self):
        self.add_user(43, username="petya", first_name="Петя")
        B.registry.register_pending(SimpleNamespace(id=7, username="ivan", first_name="Иван"))
        B.registry.ban(8, "спам")
        B.cmd_users(msg("/users", user=42))
        text = "\n".join(sent_texts())
        for needle in ("Петя", "@petya", "ID 43", "Иван", "ID 7", "ждёт одобрения", "ID 8", "спам", "👑"):
            self.assertIn(needle, text)
        B.bot.calls.clear()
        B.cmd_users(msg("/users", user=43))
        self.assertEqual(B.bot.calls, [])

    def test_ban_unban_by_id_and_username(self):
        self.add_user(43, username="petya")
        B.cmd_ban(msg("/ban @petya хватит спамить", user=42))
        self.assertEqual(B.registry.status(43), "banned")
        self.assertEqual(B.registry.users[43]["note"], "хватит спамить")
        self.assertTrue(any("закрыт" in x for x in to_chat(43)))
        B.cmd_unban(msg("/unban 43", user=42))
        self.assertEqual(B.registry.status(43), "active")
        B.bot.calls.clear()
        B.cmd_ban(msg("/ban 43", user=43))  # не админ
        self.assertEqual(B.registry.status(43), "active")
        self.assertEqual(B.bot.calls, [])

    def test_admin_cannot_be_banned(self):
        B.cmd_ban(msg("/ban 42", user=42))
        self.assertTrue(B.is_allowed(42))

    def test_banned_user_cannot_use_bot_but_data_kept(self):
        self.add_user(43)
        B.cmd_ban(msg("/ban 43", user=43 if False else 42))
        B.bot.calls.clear()
        B.deny_message(msg("привет", user=43))
        self.assertEqual(B.bot.calls, [])
        self.assertEqual(FakeState.calls, [])

    def test_removeuser(self):
        self.add_user(43)
        B.cmd_removeuser(msg("/removeuser 43", user=42))
        self.assertIsNone(B.registry.status(43))

    def test_daily_limit_enforced_and_adjustable(self):
        self.add_user(43)
        B.cmd_limit(msg("/limit 43 2", user=42))
        B.handle_text(msg("раз", user=43))
        B.handle_text(msg("два", user=43, mid=2))
        self.assertEqual(len(FakeState.calls), 2)
        B.bot.calls.clear()
        B.handle_text(msg("три", user=43, mid=3))
        self.assertEqual(len(FakeState.calls), 2)  # Gemini не трогали
        self.assertTrue(any("лимит исчерпан" in x for x in sent_texts()))
        B.cmd_limit(msg("/limit 43 0", user=42))
        B.handle_text(msg("четыре", user=43, mid=4))
        self.assertEqual(len(FakeState.calls), 3)

    def test_default_limit_applies_and_admin_unlimited(self):
        self.add_user(43)
        for i in range(3):
            B.handle_text(msg("q%d" % i, user=43, mid=i))
        B.bot.calls.clear()
        B.handle_text(msg("q4", user=43, mid=9))
        self.assertTrue(any("лимит исчерпан" in x for x in sent_texts()))
        for i in range(5):
            B.handle_text(msg("admin q%d" % i, user=42, mid=20 + i))
        self.assertEqual(len(FakeState.calls), 8)

    def test_failed_request_does_not_burn_quota(self):
        self.add_user(43)
        FakeState.reply = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        B.handle_text(msg("q", user=43))
        self.assertEqual(B.registry.usage_today(43), 0)

    def test_stats_and_me(self):
        self.add_user(43)
        B.handle_text(msg("вопрос", user=43))
        B.cmd_stats(msg("/stats", user=42))
        out = "\n".join(sent_texts())
        self.assertIn("Запросов всего: 1", out)
        self.assertIn("сегодня: 1", out)
        B.bot.calls.clear()
        B.cmd_me(msg("/me", user=43))
        self.assertIn("1/3", sent_texts()[0])
        B.bot.calls.clear()
        B.cmd_stats(msg("/stats", user=43))
        self.assertEqual(B.bot.calls, [])

    def test_broadcast_reaches_active_users_only(self):
        self.add_user(43)
        B.registry.register_pending(SimpleNamespace(id=7, username=None, first_name="X"))
        B.cmd_broadcast(msg("/broadcast завтра пары отменили", user=42))
        self.assertTrue(any("завтра пары отменили" in x for x in to_chat(43)))
        self.assertEqual(to_chat(7), [])

    # ── мут, временный бан ──
    def test_mute_blocks_user_and_expires(self):
        self.add_user(43)
        B.cmd_mute(msg("/mute 43 2ч шумишь", user=42))
        self.assertEqual(B.registry.status(43), "muted")
        self.assertTrue(any("заглушили" in x and "шумишь" in x for x in to_chat(43)))
        B.bot.calls.clear()
        B.deny_message(msg("привет", user=43))
        self.assertTrue(any("в муте ещё" in x for x in sent_texts()))
        self.assertEqual(FakeState.calls, [])
        B.registry.users[43]["until_ts"] = time.time() - 1  # время вышло
        self.assertTrue(B.is_allowed(43))
        B.handle_text(msg("снова я", user=43))
        self.assertEqual(len(FakeState.calls), 1)

    def test_mute_forever_and_unmute(self):
        self.add_user(43)
        B.cmd_mute(msg("/mute 43 навсегда", user=42))
        B.bot.calls.clear()
        B.deny_message(msg("hi", user=43))
        self.assertTrue(any("не снял" in x for x in sent_texts()))
        B.cmd_unmute(msg("/unmute 43", user=42))
        self.assertTrue(B.is_allowed(43))
        self.assertTrue(any("Мут снят" in x for x in to_chat(43)))

    def test_mute_rejects_unclear_time(self):
        self.add_user(43)
        B.cmd_mute(msg("/mute 43 2 часа", user=42))
        self.assertEqual(B.registry.status(43), "active")
        self.assertTrue(any("Не понял время" in x for x in sent_texts()))

    def test_temp_ban_with_time_and_reason(self):
        self.add_user(43)
        B.cmd_ban(msg("/ban 43 1д оскорбления", user=42))
        self.assertEqual(B.registry.status(43), "banned")
        self.assertEqual(B.registry.users[43]["note"], "оскорбления")
        self.assertTrue(B.registry.remaining(43) > 80000)
        B.bot.calls.clear()
        B.deny_message(msg("hi", user=43))
        self.assertTrue(any("Доступ закрыт ещё" in x for x in sent_texts()))
        B.registry.users[43]["until_ts"] = time.time() - 1
        self.assertTrue(B.is_allowed(43))  # срок бана вышел

    def test_mute_survives_restart(self):
        self.add_user(43)
        B.cmd_mute(msg("/mute 43 1д", user=42))
        B.store.flush()
        B.store = Store(path=B.store.path)
        B.registry = UserRegistry(B.store, {42}, 10, 3, 3)
        B.init_state()
        self.assertEqual(B.registry.status(43), "muted")
        self.assertTrue(B.registry.remaining(43) > 80000)

    # ── админ-панель на кнопках ──
    def test_admin_menu_buttons_and_keyboard(self):
        self.add_user(43)
        B.handle_text(msg("👑 Админ-панель", user=42))
        cbs = [cb for _t, cb in last_buttons()]
        for needle in ("adm_list_all_0", "adm_list_pend_0", "adm_list_act_0", "adm_list_ban_0", "adm_stats", "adm_cookies"):
            self.assertIn(needle, cbs)
        admin_row_texts = [b.args[0] for row in B.get_main_keyboard(42).rows for b in row]
        user_row_texts = [b.args[0] for row in B.get_main_keyboard(43).rows for b in row]
        self.assertIn("👑 Админ-панель", admin_row_texts)
        self.assertNotIn("👑 Админ-панель", user_row_texts)
        B.bot.calls.clear()
        B.cmd_admin(msg("/admin", user=43))
        self.assertEqual(B.bot.calls, [])

    def test_request_notification_has_open_profile_button(self):
        B.deny_message(msg("hi", user=7, username="ivan"))
        req = [c for c in B.bot.calls if c.name == "send_message" and c.args[0] == 42][0]
        self.assertIn("adm_u_7", [cb for _t, cb in buttons_of(req)])
        self.assertIn("acc_ok_7", [cb for _t, cb in buttons_of(req)])

    def test_open_pending_user_review_and_approve_by_buttons(self):
        B.deny_message(msg("hi", user=7, username="ivan", first_name="Иван"))
        click("adm_list_pend_0")
        self.assertIn("Иван", last_panel_text())
        self.assertIn("adm_u_7", [cb for _t, cb in last_buttons()])
        click("adm_u_7")
        card = last_panel_text()
        for needle in ("Иван", "@ivan", "ID: 7", "ждёт одобрения"):
            self.assertIn(needle, card)
        cbs = [cb for _t, cb in last_buttons()]
        self.assertIn("adm_ok_7", cbs)
        self.assertIn("adm_no_7", cbs)
        self.assertFalse(B.is_allowed(7))        # пока не подтвердил — доступа нет
        click("adm_ok_7")
        self.assertTrue(B.is_allowed(7))
        self.assertTrue(any("открыли доступ" in x for x in to_chat(7)))
        self.assertIn("активен", last_panel_text())
        cbs = [cb for _t, cb in last_buttons()]
        for needle in ("adm_mute_7", "adm_ban_7", "adm_lim_7", "adm_msg_7", "adm_del_7"):
            self.assertIn(needle, cbs)

    def test_reject_by_button(self):
        B.deny_message(msg("hi", user=7))
        click("adm_no_7")
        self.assertEqual(B.registry.status(7), "banned")
        self.assertIn("adm_unban_7", [cb for _t, cb in last_buttons()])

    def test_mute_by_buttons(self):
        self.add_user(43)
        click("adm_mute_43")
        self.assertIn("adm_mt_43_3600", [cb for _t, cb in last_buttons()])
        click("adm_mt_43_3600")
        self.assertEqual(B.registry.status(43), "muted")
        self.assertIn("adm_unmute_43", [cb for _t, cb in last_buttons()])
        self.assertTrue(any("заглушили" in x for x in to_chat(43)))
        click("adm_unmute_43")
        self.assertEqual(B.registry.status(43), "active")

    def test_ban_by_buttons_with_duration_then_unban(self):
        self.add_user(43)
        click("adm_ban_43")
        self.assertIn("adm_bt_43_86400", [cb for _t, cb in last_buttons()])
        click("adm_bt_43_86400")
        self.assertEqual(B.registry.status(43), "banned")
        self.assertTrue(80000 < B.registry.remaining(43) <= 86400)
        click("adm_unban_43")
        self.assertEqual(B.registry.status(43), "active")

    def test_permanent_ban_by_button(self):
        self.add_user(43)
        click("adm_bt_43_0")
        self.assertEqual(B.registry.status(43), "banned")
        self.assertIsNone(B.registry.remaining(43))

    def test_limit_by_buttons(self):
        self.add_user(43)
        click("adm_lim_43")
        self.assertIn("adm_ls_43_d", [cb for _t, cb in last_buttons()])
        click("adm_ls_43_5")
        self.assertEqual(B.registry.limit_for(43), 5)
        click("adm_ls_43_0")
        self.assertEqual(B.registry.limit_for(43), 0)
        click("adm_ls_43_d")
        self.assertEqual(B.registry.limit_for(43), 3)

    def test_delete_with_confirmation(self):
        self.add_user(43)
        click("adm_del_43")
        self.assertIsNotNone(B.registry.status(43))       # пока только спросили
        self.assertIn("adm_delok_43", [cb for _t, cb in last_buttons()])
        click("adm_delok_43")
        self.assertIsNone(B.registry.status(43))

    def test_write_message_to_user_from_card(self):
        self.add_user(43)
        click("adm_msg_43")
        B.handle_text(msg("завтра сдаём лабу", user=42))
        self.assertTrue(any("Сообщение от админа" in x and "завтра сдаём лабу" in x for x in to_chat(43)))
        self.assertNotIn(42, B.admin_input)
        self.assertEqual(FakeState.calls, [])             # админский текст не ушёл в Gemini
        click("adm_msg_43")
        B.handle_text(msg("отмена", user=42))
        self.assertNotIn(42, B.admin_input)

    def test_non_admin_cannot_use_panel_buttons(self):
        self.add_user(43)
        self.add_user(44)
        click("adm_bt_44_0", user=43)
        self.assertEqual(B.registry.status(44), "active")
        self.assertTrue(self.names("answer_callback_query")[-1].kw.get("show_alert") or self.names("answer_callback_query")[-1].args[1:])

    def test_admin_cannot_mute_or_ban_himself(self):
        click("adm_mt_42_3600")
        click("adm_bt_42_0")
        self.assertTrue(B.is_allowed(42))
        self.assertEqual(B.registry.status(42), "admin")

    def test_stats_and_cookies_buttons(self):
        click("adm_stats")
        self.assertIn("Статистика", last_panel_text())
        click("adm_cookies")
        self.assertIn("Куки Google", last_panel_text())

    def test_list_pagination(self):
        B.registry.max_users = 50
        for i in range(100, 120):
            self.add_user(i)
        click("adm_list_act_0")
        self.assertIn("Страница 1 из", last_panel_text())
        self.assertIn("adm_list_act_1", [cb for _t, cb in last_buttons()])
        click("adm_list_act_2")
        self.assertIn("Страница 3 из", last_panel_text())

    def test_user_command_opens_card(self):
        self.add_user(43, username="petya", first_name="Петя")
        B.cmd_user(msg("/user @petya", user=42))
        self.assertIn("Петя", last_panel_text())
        self.assertIn("adm_ban_43", [cb for _t, cb in last_buttons()])

    # ── режимы ──
    def test_mode_changes_prompt_and_is_saved(self):
        B.cmd_mode(msg("/mode solver"))
        B.handle_text(msg("найди силу"))
        self.assertIn("«Дано», «Решение», «Ответ»", FakeState.calls[-1].prompt)
        self.assertEqual(B.registry.users[42]["mode"], "solver")
        B.cmd_mode(msg("/mode default"))
        B.handle_text(msg("ещё", mid=2))
        self.assertNotIn("РЕЖИМ РЕШАТЕЛЯ", FakeState.calls[-1].prompt)

    # ── сохранение состояния ──
    def test_state_survives_restart(self):
        B.cmd_mode(msg("/mode coder"))
        B.ensure_client_sync()
        B.cmd_model(msg("/model pro"))
        B.handle_text(msg("запомни это"))
        B.cmd_limit(msg("/limit 42 5", user=42))
        self.add_user(43, username="petya")
        B.store.flush()
        path = B.store.path
        # «перезапуск»: новая память, та же база
        B.user_selected_model.clear(); B.user_mode.clear(); B.user_context.clear()
        B.store = Store(path=path)
        B.registry = UserRegistry(B.store, {42}, 10, 3, 3)
        B.init_state()
        self.assertEqual(B.user_mode[42], "coder")
        self.assertEqual(B.user_selected_model[42], "gemini-pro")
        self.assertIn("запомни это", B.get_context_as_prompt(42))
        self.assertTrue(B.is_allowed(43))
        self.assertEqual(B.registry.users[43]["username"], "petya")
        self.assertGreaterEqual(B.registry.users[42]["total"], 1)

    def test_ban_survives_restart(self):
        self.add_user(43)
        B.registry.ban(43, "спам")
        B.store.flush()
        B.store = Store(path=B.store.path)
        B.registry = UserRegistry(B.store, {42}, 10, 3, 3)
        B.init_state()
        self.assertEqual(B.registry.status(43), "banned")

    # ── очередь ──
    def test_queue_limits_concurrency_and_reports_position(self):
        q = B.GeminiQueue(1)
        order, positions = [], []
        gate = threading.Event()

        def first():
            with q.slot():
                order.append("a-start")
                gate.wait(5)
                order.append("a-end")

        def second():
            with q.slot(on_wait=positions.append):
                order.append("b")

        ta = threading.Thread(target=first); ta.start()
        self.assertTrue(wait_for(lambda: "a-start" in order))
        tb_ = threading.Thread(target=second); tb_.start()
        self.assertTrue(wait_for(lambda: positions == [1]))
        self.assertEqual(order, ["a-start"])  # второй ждёт
        gate.set(); ta.join(5); tb_.join(5)
        self.assertEqual(order, ["a-start", "a-end", "b"])

    # ── куки ──
    def test_bad_cookies_alert_admin_and_recovery(self):
        FakeState.on_init = lambda: B._cookie_sink("2026 | WARNING | gemini_webapi.client:_fetch_user_status:559 - Account status: UNAUTHENTICATED")
        B.ensure_client_sync()
        self.assertTrue(wait_for(lambda: any("не принимает куки" in x for x in to_chat(42))))
        self.assertIs(B.cookie_watch["ok"], False)
        n = len(to_chat(42))
        FakeState.on_init = None
        B.ensure_client_sync(force=True)
        self.assertTrue(wait_for(lambda: any("снова работают" in x for x in to_chat(42))))
        self.assertGreater(len(to_chat(42)), n)

    def test_good_cookies_no_alert(self):
        B.ensure_client_sync()
        time.sleep(0.3)
        self.assertEqual(to_chat(42), [])
        self.assertIs(B.cookie_watch["ok"], True)

    def test_health_check_alerts_after_two_failures(self):
        FakeState.reply = "ок"
        self.assertTrue(B.health_check_once())
        FakeState.reply = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
        self.assertFalse(B.health_check_once())
        self.assertEqual(to_chat(42), [])
        self.assertFalse(B.health_check_once())
        self.assertTrue(wait_for(lambda: any("не отвечает" in x for x in to_chat(42))))
        FakeState.reply = "ок"
        self.assertTrue(B.health_check_once())
        self.assertTrue(wait_for(lambda: any("снова отвечает" in x for x in to_chat(42))))

    # ── Gemini-клиент ──
    def test_client_created_without_proxies_kwarg(self):
        B.handle_text(msg("привет"))
        self.assertEqual(FakeState.inits, 1)
        self.assertIsInstance(B._gemini_client, FakeClient)  # со старым proxies=None был бы TypeError

    def test_plain_answer_cleaned(self):
        FakeState.reply = "**Важно** B = F / (I * l) и x**2"
        B.handle_text(msg("формула"))
        self.assertEqual(sent_texts()[-1], "Важно B = F / (I * l) и x^2")

    # ── контекст ──
    def test_context_has_no_duplicate_of_current_message(self):
        B.handle_text(msg("первый вопрос", mid=1))
        B.handle_text(msg("второй вопрос", mid=2))
        p1, p2 = FakeState.calls[0].prompt, FakeState.calls[1].prompt
        self.assertEqual(p1.count("первый вопрос"), 1)
        self.assertEqual(p2.count("второй вопрос"), 1)
        self.assertEqual(p2.count("первый вопрос"), 1)
        self.assertIn("(пусто)", p1)

    def test_new_dialog_keeps_model(self):
        B.user_selected_model[42] = "gemini-pro"
        B.add_to_context(42, "user", "x")
        B.handle_text(msg("🔄 Новый диалог"))
        self.assertEqual(B.get_context_as_prompt(42), "")
        self.assertEqual(B.user_selected_model[42], "gemini-pro")

    # ── модели ──
    def test_model_switch_validated_against_account(self):
        B.ensure_client_sync()
        self.assertIsNone(B.default_model())
        B.cmd_model(msg("/model pro"))
        self.assertEqual(B.user_selected_model[42], "gemini-pro")
        B.cmd_model(msg("/model lite"))
        self.assertIn("нет на твоём аккаунте", sent_texts()[-1])
        self.assertEqual(B.user_selected_model[42], "gemini-pro")
        B.handle_callbacks(SimpleNamespace(id="c", data="set_model_lite", message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=5)))
        self.assertTrue(self.names("answer_callback_query")[-1].kw.get("show_alert"))
        B.handle_callbacks(SimpleNamespace(id="c", data="set_model_3.5", message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=5)))
        self.assertEqual(B.user_selected_model[42], "gemini-flash")

    def test_auto_model_sends_no_model(self):
        B.ensure_client_sync()
        B.cmd_model(msg("/model auto"))
        self.assertIsNone(B.user_selected_model[42])
        B.handle_text(msg("вопрос"))
        self.assertIsNone(FakeState.calls[-1].model)
        B.handle_callbacks(SimpleNamespace(id="c", data="set_model_flash", message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=5)))
        B.handle_callbacks(SimpleNamespace(id="c", data="set_model_auto", message=SimpleNamespace(chat=SimpleNamespace(id=42), message_id=5)))
        self.assertIsNone(B.get_user_model(42))

    def test_hanging_model_falls_back_to_auto(self):
        B.ensure_client_sync()
        B.user_selected_model[42] = "gemini-pro"
        FakeState.hang_models = {"gemini-pro"}
        B.MODEL_TIMEOUT = 1
        FakeState.reply = "ответ от авто"
        B.handle_text(msg("вопрос"))
        self.assertEqual([c.model for c in FakeState.calls], ["gemini-pro", None])
        texts = sent_texts()
        self.assertIn("ответ от авто", texts)
        self.assertTrue(any("не ответила" in t for t in texts), texts)
        self.assertNotIn(42, B._busy)

    def test_default_model_env_override(self):
        B.ensure_client_sync()
        B.DEFAULT_MODEL = "flash"
        self.assertEqual(B.default_model(), "gemini-flash")
        B.DEFAULT_MODEL = "auto"
        self.assertIsNone(B.default_model())

    def test_selected_model_is_sent_to_gemini(self):
        B.ensure_client_sync()
        B.cmd_model(msg("/model pro"))
        B.handle_text(msg("вопрос"))
        self.assertEqual(FakeState.calls[-1].model, "gemini-pro")

    def test_unknown_model_falls_back_with_notice(self):
        FakeState.list_models_result = []  # список моделей неизвестен
        B.user_selected_model[42] = "gemini-argon"
        B.handle_text(msg("вопрос"))
        self.assertEqual([c.model for c in FakeState.calls], ["gemini-argon", None])
        texts = sent_texts()
        self.assertTrue(any("недоступна" in t for t in texts), texts)

    # ── графики ──
    def test_chart_flow_sends_photo_with_clean_caption(self):
        FakeState.reply = "**Решение:** y = x**2\n" + GOOD_CHART
        B.handle_text(msg("построй график"))
        photo = self.names("send_photo")
        self.assertEqual(len(photo), 1)
        self.assertTrue(photo[0].data.startswith(PNG))
        self.assertEqual(photo[0].kw["caption"], "Решение: y = x^2")
        self.assertNotIn("plt.", photo[0].kw["caption"])

    def test_malicious_chart_code_never_runs(self):
        marker = Path(tempfile.gettempdir()) / "BANAN_PWNED"
        marker.unlink(missing_ok=True)
        FakeState.reply = "Вот график\n```python\nimport os\nimport matplotlib.pyplot as plt\nos.system('touch %s')\nplt.plot([1])\n```" % marker
        B.handle_text(msg("график"))
        self.assertFalse(marker.exists())
        self.assertEqual(self.names("send_photo"), [])
        self.assertTrue(any("График построить не удалось" in t for t in sent_texts()))

    def test_broken_chart_code_gets_one_fix_attempt(self):
        def reply(prompt, model, files):
            return "ответ\n" + GOOD_CHART if prompt.startswith("Твой код matplotlib не выполнился") else "ответ\n```python\nimport matplotlib.pyplot as plt\nplt.plot(\n```"
        FakeState.reply = reply
        B.handle_text(msg("график"))
        self.assertEqual(len(self.names("send_photo")), 1)
        self.assertEqual(len(FakeState.calls), 2)

    # ── файлы ──
    def test_unsupported_format_becomes_honest_txt(self):
        FakeState.reply = "[SEND_AS_FILE:slides.pptx]\nИтог: $100 и **жирный**"
        B.handle_text(msg("сделай файл отчёт"))
        doc = self.names("send_document")[0]
        self.assertEqual(doc.kw["_name"], "slides.txt")
        self.assertEqual(doc.data.decode(), "Итог: $100 и жирный")
        self.assertTrue(any("не собрать" in t for t in sent_texts()))

    def test_real_pdf_with_chart_from_marker(self):
        FakeState.reply = "[SEND_AS_FILE:solution.pdf]\n**Дано**\nB = F / (I * l)\nОтвет: 9 Н\n" + GOOD_CHART
        B.handle_text(msg("реши задачу и дай pdf"))
        doc = self.names("send_document")[0]
        self.assertEqual(doc.kw["_name"], "solution.pdf")
        self.assertTrue(doc.data.startswith(b"%PDF"))
        self.assertIn(b"/Image", doc.data)  # график вставлен рисунком

    def test_real_docx_from_user_phrase(self):
        FakeState.reply = "Конспект\n• пункт один\n• пункт два\n" + GOOD_CHART
        B.handle_text(msg("сделай конспект по сопромату в word"))
        doc = self.names("send_document")[0]
        self.assertTrue(doc.kw["_name"].endswith(".docx"))
        z = zipfile.ZipFile(__import__("io").BytesIO(doc.data))
        xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("Конспект", xml)
        self.assertIn("пункт два", xml)
        self.assertNotIn("plt.", xml)
        self.assertTrue(any(n.startswith("word/media/") for n in z.namelist()))

    def test_question_about_pdf_is_not_a_file_request(self):
        FakeState.reply = "PDF — это формат документов."
        B.handle_text(msg("что такое pdf"))
        self.assertEqual(self.names("send_document"), [])

    def test_code_file_is_raw(self):
        FakeState.reply = "[SEND_AS_FILE:calc.py]\n```python\nprice = '$5'\nx = 2**3\n```"
        B.handle_text(msg("скинь файлом"))
        doc = self.names("send_document")[0]
        self.assertEqual(doc.kw["_name"], "calc.py")
        self.assertEqual(doc.data.decode(), "price = '$5'\nx = 2**3")

    def test_path_traversal_in_file_name_neutralised(self):
        FakeState.reply = "[SEND_AS_FILE:../../etc/passwd.txt]\nтекст"
        B.handle_text(msg("отправь файлом"))
        self.assertEqual(self.names("send_document")[0].kw["_name"], "passwd.txt")

    # ── картинки ──
    def test_generate_image_marker_and_cleanup(self):
        def reply(prompt, model, files):
            return ("Готово", 1) if prompt.startswith("Generate a high-quality photo") else "Вот [GENERATE_IMAGE:a red apple]"
        FakeState.reply = reply
        B.handle_text(msg("яблоко"))
        photos = self.names("send_photo")
        self.assertEqual(len(photos), 1)
        self.assertEqual(list(B.WORKDIR.glob("gen_*")), [])

    def test_direct_image_request(self):
        FakeState.reply = ("", 2)
        B.handle_text(msg("нарисуй картинку кота"))
        self.assertEqual(len(self.names("send_photo")), 2)
        self.assertEqual(list(B.WORKDIR.glob("gen_*")), [])

    # ── загрузка фото ──
    def test_photo_upload_passes_file_and_cleans_up(self):
        FakeState.reply = "Решение готово"
        m = msg(None, photo=[SimpleNamespace(file_id="a"), SimpleNamespace(file_id="b")], caption="реши")
        B.handle_files(m)
        self.assertTrue(FakeState.calls[-1].files)
        self.assertEqual(list(B.WORKDIR.glob("upload_*")), [])
        self.assertIn("Решение готово", sent_texts())

    def test_huge_document_rejected_early(self):
        B.handle_files(msg(None, document=SimpleNamespace(file_id="d", file_name="a.pdf", file_size=50 * 1024 * 1024)))
        self.assertEqual(FakeState.calls, [])
        self.assertIn("20 МБ", sent_texts()[0])

    # ── диагностика ──
    def test_ping_ok_and_error(self):
        FakeState.reply = "ок"
        B.cmd_ping(msg("/ping"))
        self.assertTrue(any(t.startswith("✅ Gemini ответил") for t in sent_texts()))
        FakeState.reply = lambda *a: (_ for _ in ()).throw(RuntimeError("quota exceeded"))
        B.bot.calls.clear()
        B.cmd_ping(msg("/ping"))
        out = " ".join(sent_texts())
        self.assertIn("RuntimeError", out)
        self.assertIn("quota exceeded", out)
        self.assertNotIn(42, B._busy)

    # ── нагрузка ──
    def test_second_request_rejected_while_busy(self):
        B._busy.add(42)
        B.handle_text(msg("ещё вопрос"))
        self.assertEqual(FakeState.calls, [])
        self.assertIn("Ещё обрабатываю", sent_texts()[0])

    def test_busy_flag_released_after_error(self):
        FakeState.reply = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        B.handle_text(msg("вопрос"))
        self.assertNotIn(42, B._busy)
        self.assertTrue(any(t.startswith("❌") for t in sent_texts()))
        self.assertNotIn("boom", " ".join(sent_texts()))  # внутренности не светим

    def test_long_reply_is_split(self):
        FakeState.reply = ("строка текста " * 20 + "\n") * 60
        B.handle_text(msg("много"))
        chunks = [t for t in sent_texts() if t.startswith("строка")]
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))

    def test_two_chats_run_in_parallel_threads(self):
        import threading
        FakeState.reply = "ответ"
        ts = [threading.Thread(target=B.handle_text, args=(msg("q%d" % i, user=42, chat=100 + i, mid=i),)) for i in range(4)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(len(FakeState.calls), 4)


# ═════════════════ Postgres-ветка хранилища (через подмену pg8000 на SQLite) ═════════════════
class StorePostgresPath(unittest.TestCase):
    """Настоящий Postgres тут недоступен: подменяем pg8000 обёрткой над SQLite, которая понимает %s.
    Проверяется разбор DATABASE_URL, SSL, подстановка параметров и общий SQL, но не диалект сервера."""

    def test_pg_code_path(self):
        import sqlite3

        seen = {}

        class Cur:
            def __init__(self, c):
                self.c = c.cursor()

            def execute(self, sql, params=()):
                assert "?" not in sql, "в Postgres-ветке остался ?-плейсхолдер: " + sql
                assert sql.count("%s") == len(params), "число %s и параметров не совпало: " + sql
                self.c.execute(sql.replace("%s", "?"), params)

            def fetchall(self):
                return self.c.fetchall()

            def close(self):
                self.c.close()

        class Conn:
            def __init__(self):
                self.c = sqlite3.connect(":memory:", check_same_thread=False)

            def cursor(self):
                return Cur(self.c)

            def commit(self):
                self.c.commit()

            def rollback(self):
                self.c.rollback()

            def close(self):
                pass

        shared = Conn()
        fake = types.ModuleType("pg8000")
        fake.dbapi = types.ModuleType("pg8000.dbapi")

        def connect(**kw):
            seen.update(kw)
            return shared

        fake.dbapi.connect = connect
        sys.modules.update({"pg8000": fake, "pg8000.dbapi": fake.dbapi})
        try:
            s = Store(url="postgresql://neon_user:p%40ss@ep-cool.neon.tech/neondb?sslmode=require")
            self.assertEqual(s.kind, "pg")
            s.init()
            reg = UserRegistry(s, {1}, 10, 30, 3)
            reg.load({1})
            reg.register_pending(SimpleNamespace(id=5, username="a", first_name="A"))
            reg.approve(5)
            reg.count_usage(5)
            s.save_context(5, [{"role": "user", "text": "привет"}])
            s.save_kv("stats", "{}")
            s.flush()
            s2 = Store(url="postgresql://neon_user:p%40ss@ep-cool.neon.tech/neondb?sslmode=require")
            rows = {u["id"]: u for u in s2.load_users()}
            self.assertEqual(rows[5]["status"], "active")
            self.assertEqual(rows[5]["total"], 1)
            self.assertEqual(s2.load_contexts()[5][0]["text"], "привет")
            self.assertEqual(s2.get_kv("stats"), "{}")
            self.assertEqual((seen["user"], seen["password"], seen["host"], seen["database"]), ("neon_user", "p@ss", "ep-cool.neon.tech", "neondb"))
            self.assertIn("ssl_context", seen)
            seen.clear()
            Store(url="postgresql://u:p@h/db?sslmode=disable").execute("SELECT 1", fetch=True)
            self.assertNotIn("ssl_context", seen)
        finally:
            sys.modules.pop("pg8000", None)
            sys.modules.pop("pg8000.dbapi", None)

    def test_broken_database_does_not_break_bot(self):
        B.store = Store(url="postgresql://u:p@127.0.0.1:1/none")  # ничего не слушает; pg8000 может отсутствовать — тоже ошибка
        B.registry = UserRegistry(B.store, {42}, 10, 3, 3)
        B.init_state()
        self.assertIsNone(B.registry.store)           # работаем без базы
        self.assertTrue(B.is_allowed(42))
        B.add_to_context(42, "user", "привет")        # без базы не падает
        self.assertIn("привет", B.get_context_as_prompt(42))


if __name__ == "__main__":
    unittest.main(verbosity=2)
