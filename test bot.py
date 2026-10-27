# -*- coding: utf-8 -*-
"""Автотесты без сети: Telegram и Gemini подменены заглушками.
Запуск:  python test_bot.py        (нужны только numpy и matplotlib)
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

os.environ.update(TELEGRAM_TOKEN="1:test", GEMINI_SECURE_1PSID="psid", GEMINI_SECURE_1PSIDTS="psidts", ALLOWED_IDS="42")

PNG = b"\x89PNG\r\n\x1a\n"


# ───────────── заглушка Gemini ─────────────
class FakeState:
    reply = "ok"                # str | callable(prompt, model, files) -> str | (str, n_images)
    valid_models = ["gemini-flash", "gemini-pro"]
    list_models_result = "default"
    strict_model = True
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

    async def close(self):
        pass

    def list_models(self):
        if FakeState.list_models_result == "default":
            return [FakeModel(n, n.replace("gemini-", "").title()) for n in FakeState.valid_models]
        return FakeState.list_models_result

    async def generate_content(self, prompt, model=None, files=None, **kw):
        FakeState.calls.append(SimpleNamespace(prompt=prompt, model=model, files=files))
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
sys.modules.update({"telebot": tb, "telebot.types": tb.types})
gw = types.ModuleType("gemini_webapi")
gw.GeminiClient = FakeClient
sys.modules["gemini_webapi"] = gw

import bot as B                       # noqa: E402
import chartbox as CB                 # noqa: E402
from textfmt import clean_formatting as cf, split_text, strip_service_markers  # noqa: E402

GOOD_CHART = "```python\nimport numpy as np\nimport matplotlib.pyplot as plt\nx = np.linspace(0, 10, 50)\nplt.plot(x, x**2)\nplt.grid(True)\n```"


def msg(text=None, user=42, chat=42, mid=1, **kw):
    return SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=user), chat=SimpleNamespace(id=chat),
        message_id=mid, photo=kw.get("photo"), document=kw.get("document"), caption=kw.get("caption"),
    )


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
        B.user_context.clear()
        B._busy.clear()
        B.ALLOWED_IDS = {42}
        FakeState.reply, FakeState.calls, FakeState.inits = "ok", [], 0
        FakeState.valid_models = ["gemini-flash", "gemini-pro"]
        FakeState.list_models_result, FakeState.strict_model = "default", True

    def names(self, kind):
        return [c for c in B.bot.calls if c.name == kind]

    # ── доступ ──
    def test_stranger_gets_nothing_when_whitelist_set(self):
        B.deny_message(msg("hi", user=7))
        self.assertEqual(B.bot.calls, [])
        self.assertFalse(B.is_allowed(7))
        self.assertTrue(B.is_allowed(42))

    def test_unconfigured_whitelist_shows_user_id_and_denies(self):
        B.ALLOWED_IDS = set()
        B.deny_message(msg("hi", user=7))
        self.assertIn("ID: 7", sent_texts()[0])
        self.assertFalse(B.is_allowed(7))

    def test_myid(self):
        B.cmd_myid(msg("/myid", user=7))
        self.assertIn("7", sent_texts()[0])

    def test_stranger_callback_denied(self):
        B.deny_callback(SimpleNamespace(id="c1", from_user=SimpleNamespace(id=7)))
        self.assertTrue(self.names("answer_callback_query")[0].kw.get("show_alert"))

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
        self.assertEqual(B.default_model(), "gemini-flash")
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
    def test_docx_marker_becomes_honest_txt(self):
        FakeState.reply = "[SEND_AS_FILE:report.docx]\nИтог: $100 и **жирный**"
        B.handle_text(msg("сделай файл отчёт"))
        doc = self.names("send_document")[0]
        self.assertEqual(doc.kw["_name"], "report.txt")
        self.assertEqual(doc.data.decode(), "Итог: $100 и жирный")
        self.assertTrue(any("не собрать" in t for t in sent_texts()))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
