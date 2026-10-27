# -*- coding: utf-8 -*-
"""Хранилище бота: SQLite (по умолчанию) или Postgres (переменная DATABASE_URL, например бесплатный Neon).

Всё читается в память при старте, запись идёт в фоне, поэтому сбой базы не роняет бота.
ВАЖНО: на бесплатном Render файловая система стирается при каждом рестарте — для постоянного
хранения нужен DATABASE_URL (Postgres), SQLite-файл там живёт только до перезапуска.
"""
import json
import logging
import queue
import sqlite3
import ssl
import threading
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("banan.store")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, status TEXT NOT NULL,
        daily_limit INTEGER, model TEXT, mode TEXT, total INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT, last_seen BIGINT, usage_date TEXT,
        usage_count INTEGER NOT NULL DEFAULT 0, note TEXT, until_ts BIGINT)""",
    "CREATE TABLE IF NOT EXISTS context (chat_id BIGINT PRIMARY KEY, data TEXT)",
    "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
]

USER_FIELDS = ("id", "username", "first_name", "status", "daily_limit", "model", "mode", "total",
               "created_at", "last_seen", "usage_date", "usage_count", "note", "until_ts")


class Store:
    def __init__(self, url="", path="bananbot.db"):
        self.url = (url or "").strip()
        self.kind = "pg" if self.url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self.path = path
        self.ok = False
        self.last_error = ""
        self._conn = None
        self._lock = threading.Lock()
        self._q = queue.Queue()
        self._worker = None
        self._worker_lock = threading.Lock()

    # ── соединение ──
    def _connect(self):
        if self.kind == "sqlite":
            return sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        import pg8000.dbapi as pg  # чистый Python, ставится на любой версии Python

        p = urlparse(self.url)
        sslmode = parse_qs(p.query).get("sslmode", ["require"])[0]
        kw = dict(
            user=unquote(p.username or ""), password=unquote(p.password or ""),
            host=p.hostname, port=p.port or 5432,
            database=(p.path or "/").lstrip("/") or "postgres", timeout=20,
        )
        if sslmode != "disable":
            kw["ssl_context"] = ssl.create_default_context()
        return pg.connect(**kw)

    def _prep(self, sql):
        return sql.replace("?", "%s") if self.kind == "pg" else sql

    def execute(self, sql, params=(), fetch=False):
        sql = self._prep(sql)
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None:
                        self._conn = self._connect()
                    cur = self._conn.cursor()
                    cur.execute(sql, tuple(params))
                    rows = cur.fetchall() if fetch else None
                    self._conn.commit()
                    try:
                        cur.close()
                    except Exception:
                        pass
                    self.ok = True
                    return rows
                except Exception as e:
                    self.ok = False
                    self.last_error = "%s: %s" % (type(e).__name__, e)
                    for action in ("rollback", "close"):
                        try:
                            getattr(self._conn, action)()
                        except Exception:
                            pass
                    self._conn = None
                    if attempt == 2:
                        raise

    def init(self):
        for stmt in SCHEMA:
            self.execute(stmt)
        self._migrate()

    def _migrate(self):
        """Старые базы (до мутов/временных банов) получают новую колонку."""
        try:
            if self.kind == "pg":
                self.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS until_ts BIGINT")
            else:
                cols = [r[1] for r in self.execute("PRAGMA table_info(users)", fetch=True)]
                if "until_ts" not in cols:
                    self.execute("ALTER TABLE users ADD COLUMN until_ts BIGINT")
        except Exception as e:
            log.debug("миграция пропущена: %s", e)

    # ── фоновая запись ──
    def _run_worker(self):
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception as e:
                log.warning("Запись в базу не удалась: %s", e)
            finally:
                self._q.task_done()

    def submit(self, fn, *args):
        with self._worker_lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run_worker, daemon=True)
                self._worker.start()
        self._q.put((fn, args))

    def flush(self):
        self._q.join()

    # ── пользователи ──
    def load_users(self):
        rows = self.execute("SELECT %s FROM users" % ", ".join(USER_FIELDS), fetch=True) or []
        return [dict(zip(USER_FIELDS, r)) for r in rows]

    def _upsert_user(self, u):
        cols = ", ".join(USER_FIELDS)
        marks = ", ".join("?" for _ in USER_FIELDS)
        upd = ", ".join("%s=excluded.%s" % (f, f) for f in USER_FIELDS[1:])
        self.execute(
            "INSERT INTO users (%s) VALUES (%s) ON CONFLICT(id) DO UPDATE SET %s" % (cols, marks, upd),
            [u.get(f) for f in USER_FIELDS],
        )

    def save_user(self, u):
        self.submit(self._upsert_user, dict(u))

    def delete_user(self, uid):
        self.submit(lambda: (self.execute("DELETE FROM users WHERE id = ?", [uid]),
                             self.execute("DELETE FROM context WHERE chat_id = ?", [uid])))

    # ── контекст диалогов ──
    def load_contexts(self):
        rows = self.execute("SELECT chat_id, data FROM context", fetch=True) or []
        out = {}
        for chat_id, data in rows:
            try:
                out[int(chat_id)] = json.loads(data)
            except Exception:
                pass
        return out

    def _upsert_context(self, chat_id, data):
        self.execute(
            "INSERT INTO context (chat_id, data) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data", [chat_id, data])

    def save_context(self, chat_id, items):
        self.submit(self._upsert_context, chat_id, json.dumps(items, ensure_ascii=False))

    # ── прочее ──
    def get_kv(self, k, default=None):
        rows = self.execute("SELECT v FROM kv WHERE k = ?", [k], fetch=True)
        return rows[0][0] if rows else default

    def _upsert_kv(self, k, v):
        self.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", [k, v])

    def save_kv(self, k, v):
        self.submit(self._upsert_kv, k, v)
