# -*- coding: utf-8 -*-
"""Тесты локального режима и миграции. Запуск: python test_localmode.py (bot.py и telebot не нужны)."""
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import localmode
import migrate_to_postgres as M
from store import Store


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_dotenv_parses_and_does_not_override(self):
        f = self.tmp / ".env"
        f.write_text('# c\nA=1\nexport B = "two words"\nC=\'x\'\nD=C:\\bananagpt-data\nE=a=b\n\nBAD\nKEEP=new\n', encoding="utf-8")
        os.environ["KEEP"] = "old"
        for k in "ABCDE":
            os.environ.pop(k, None)
        localmode.load_dotenv(f)
        self.assertEqual([os.environ[k] for k in "ABCDE"], ["1", "two words", "x", "C:\\bananagpt-data", "a=b"])
        self.assertEqual(os.environ["KEEP"], "old")

    def test_dotenv_missing_file_is_noop(self):
        self.assertEqual(localmode.load_dotenv(self.tmp / "nope.env"), 0)

    def test_render_mode_untouched(self):
        os.environ.pop("DATA_DIR", None)
        os.environ.pop("USE_POLLING", None)
        os.environ.pop("GEMINI_COOKIE_PATH", None)
        self.assertIsNone(localmode.data_dir())
        localmode.prepare(None)
        self.assertNotIn("GEMINI_COOKIE_PATH", os.environ)
        self.assertTrue(localmode.acquire_single_instance(None))
        self.assertIsNone(localmode.setup_log_file(None))

    def test_data_dir_rules(self):
        os.environ.pop("DATA_DIR", None)
        os.environ["USE_POLLING"] = "1"
        self.assertEqual(localmode.data_dir(), localmode.HERE / "data")
        os.environ["DATA_DIR"] = str(self.tmp / "d")
        self.assertEqual(localmode.data_dir(), self.tmp / "d")

    def test_prepare_creates_folders_and_cookie_path(self):
        os.environ.pop("GEMINI_COOKIE_PATH", None)
        d = self.tmp / "x" / "data"
        localmode.prepare(d)
        for sub in ("work", "backup", "gemini_cookies"):
            self.assertTrue((d / sub).is_dir())
        self.assertEqual(os.environ["GEMINI_COOKIE_PATH"], str(d / "gemini_cookies"))

    def test_log_file_rotates_handler(self):
        h = localmode.setup_log_file(self.tmp, "INFO")
        try:
            logging.getLogger("t").warning("привет")
            h.flush()
            self.assertIn("привет", (self.tmp / "bot.log").read_text(encoding="utf-8"))
            self.assertEqual(h.backupCount, 5)
        finally:
            logging.getLogger().removeHandler(h)
            h.close()

    def test_second_process_cannot_take_lock(self):
        self.assertTrue(localmode.acquire_single_instance(self.tmp))
        code = ("import localmode,sys,pathlib;"
                "sys.exit(0 if localmode.acquire_single_instance(pathlib.Path(sys.argv[1])) else 7)")
        r = subprocess.run([sys.executable, "-c", code, str(self.tmp)], cwd=str(localmode.HERE))
        self.assertEqual(r.returncode, 7)

    def test_backup_one_per_day_keeps_seven(self):
        db = self.tmp / "b.db"
        c = sqlite3.connect(str(db))
        c.execute("create table t(x)"); c.execute("insert into t values (1)"); c.commit(); c.close()
        bdir = self.tmp / "backup"
        bdir.mkdir()
        for i in range(10):
            (bdir / ("bananbot-2020-01-%02d.db" % (i + 1))).write_bytes(b"old")
        made = localmode.backup_sqlite(db, bdir, keep=7)
        self.assertIsNotNone(made)
        files = sorted(p.name for p in bdir.glob("bananbot-*.db"))
        self.assertEqual(len(files), 7)
        self.assertIn(made.name, files)
        self.assertEqual(sqlite3.connect(str(made)).execute("select x from t").fetchone()[0], 1)
        self.assertIsNone(localmode.backup_sqlite(db, bdir, keep=7))  # второй старт за день — не перезаписывает
        self.assertIsNone(localmode.backup_sqlite(self.tmp / "none.db", bdir))


class GeminiInit(unittest.TestCase):
    """patch_gemini_init_rpc: зависший служебный запрос не должен блокировать старт. bot.py не импортируем — копируем логику через exec."""

    @staticmethod
    def _load_patch():
        import asyncio
        import re
        src = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8")
        m = re.search(r"_INIT_RPC_EXTRA = .*?\npatch_gemini_init_rpc\(GeminiClient\)", src, re.S)
        ns = {"asyncio": asyncio, "log": logging.getLogger("t")}
        exec(m.group(0).replace("patch_gemini_init_rpc(GeminiClient)", ""), ns)
        return ns["patch_gemini_init_rpc"]

    def test_hanging_step_is_skipped(self):
        import asyncio
        calls = []

        class C:
            async def _fetch_user_status(self): calls.append("status")
            async def _fetch_preferences(self): await asyncio.sleep(60)   # виснет
            async def _sync_activity(self): raise RuntimeError("boom")    # падает
            async def _fetch_quota(self): calls.append("quota")           # работает
            async def _init_rpc(self): raise AssertionError("старый метод не должен вызываться")

        self.assertTrue(self._load_patch()(C, step_timeout=0.2))
        t = time.time()
        asyncio.run(C()._init_rpc())
        self.assertLess(time.time() - t, 3)
        self.assertEqual(calls, ["status", "quota"])

    def test_user_status_error_still_raises(self):
        import asyncio

        class C:
            async def _fetch_user_status(self): raise ConnectionError("net")
            async def _init_rpc(self): pass

        self._load_patch()(C, step_timeout=0.2)
        with self.assertRaises(ConnectionError):
            asyncio.run(C()._init_rpc())

    def test_unknown_library_version_untouched(self):
        class Old:
            pass
        self.assertFalse(self._load_patch()(Old))


class CookieHeader(unittest.TestCase):
    @staticmethod
    def _funcs():
        import re
        src = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8")
        m = re.search(r"def parse_cookie_header\(raw\):.*?\n\n\ndef load_extra_cookies\(\):.*?return parse_cookie_header\(raw\)\n", src, re.S)
        ns = {"os": os, "Path": Path, "log": logging.getLogger("t")}
        exec(m.group(0), ns)
        return ns

    def test_parse(self):
        f = self._funcs()["parse_cookie_header"]
        self.assertEqual(f("Cookie: a=1; b=x=y;  c = 3 ;;bad;\n d=4"), {"a": "1", "b": "x=y", "c": "3", "d": "4"})
        self.assertEqual(f(""), {})
        self.assertEqual(f(None), {})

    def test_file_and_env(self):
        ns = self._funcs()
        tmp = Path(tempfile.mkdtemp()) / "c.txt"
        tmp.write_text("\ufeffa=1; b=2\n", encoding="utf-8")
        saved = dict(os.environ)
        try:
            os.environ.pop("GEMINI_COOKIE_HEADER", None)
            os.environ["GEMINI_COOKIES_FILE"] = str(tmp)
            self.assertEqual(ns["load_extra_cookies"](), {"a": "1", "b": "2"})
            os.environ["GEMINI_COOKIE_HEADER"] = "z=9"          # строка важнее файла
            self.assertEqual(ns["load_extra_cookies"](), {"z": "9"})
            os.environ.pop("GEMINI_COOKIE_HEADER")
            os.environ["GEMINI_COOKIES_FILE"] = str(tmp) + ".нет"   # нет файла — пусто, без падения
            self.assertEqual(ns["load_extra_cookies"](), {})
        finally:
            os.environ.clear()
            os.environ.update(saved)


class Migrate(unittest.TestCase):
    def test_merge_and_dry_run(self):
        tmp = Path(tempfile.mkdtemp())
        src, dst = str(tmp / "local.db"), str(tmp / "remote.db")
        s, d = Store(path=src), Store(path=dst)
        s.init(); d.init()
        base = dict(username=None, first_name="A", status="active", daily_limit=None, model=None, mode=None,
                    usage_date=None, usage_count=0, note=None, until_ts=None, last_seen=100)
        d._upsert_user(dict(base, id=1, total=500, created_at=10))
        d._upsert_user(dict(base, id=2, total=7, created_at=10))
        s._upsert_user(dict(base, id=1, total=3, created_at=999, username="ivan", status="muted", last_seen=200))
        s._upsert_user(dict(base, id=3, total=1, created_at=50))
        s._upsert_context(1, json.dumps([{"role": "user", "text": "hi"}]))
        self.assertEqual(M.main(["--from", src, "--to", dst]), 0)
        self.assertEqual({u["id"] for u in d.load_users()}, {1, 2})  # без --apply ничего не записано
        self.assertEqual(M.main(["--from", src, "--to", dst, "--apply"]), 0)
        users = {u["id"]: u for u in d.load_users()}
        self.assertEqual(set(users), {1, 2, 3})
        self.assertEqual((users[1]["total"], users[1]["created_at"], users[1]["status"], users[1]["username"]), (503, 10, "muted", "ivan"))
        self.assertEqual(users[2]["total"], 7)
        self.assertEqual(d.load_contexts()[1][0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
