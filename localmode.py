# -*- coding: utf-8 -*-
"""Локальный запуск на ПК (Windows/Linux): .env, папка данных DATA_DIR, лог-файл, защита от двойного запуска,
резервные копии SQLite. Включается переменными окружения USE_POLLING=1 и/или DATA_DIR.
Если ни одна не задана (Render), ничего из этого не делается — поведение бота прежнее."""
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXIT_CONFIG, EXIT_ALREADY_RUNNING = 2, 3  # run.bat на этих кодах не перезапускает бота
_lock_handle = None  # держим открытым до конца процесса — ОС сама снимает блокировку при любом завершении


def load_dotenv(path=None):
    """Читает .env рядом с ботом. Уже заданные переменные окружения не затирает. Нет файла — ничего не делает."""
    path = Path(path) if path else HERE / ".env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return 0
    n = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
            n += 1
    return n


def use_polling():
    return os.getenv("USE_POLLING", "").strip().lower() in ("1", "true", "yes", "on")


def data_dir():
    """Папка данных или None (режим Render). DATA_DIR из окружения; без неё при USE_POLLING=1 — папка data рядом с ботом."""
    raw = os.getenv("DATA_DIR", "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw)))
    return HERE / "data" if use_polling() else None


def prepare(d):
    """Создаёт папку и подпапки, направляет кэш кук библиотеки Gemini в неё. Вызывать до импорта gemini_webapi."""
    if d is None:
        return
    for sub in ("", "work", "backup", "gemini_cookies"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("GEMINI_COOKIE_PATH", str(d / "gemini_cookies"))


def setup_log_file(d, level="INFO"):
    """bot.log в папке данных с ротацией (5 МБ × 5 файлов)."""
    if d is None:
        return None
    h = logging.handlers.RotatingFileHandler(d / "bot.log", maxBytes=5 << 20, backupCount=5, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    return h


def acquire_single_instance(d):
    """True — мы единственный экземпляр на этом ПК. Блокировка файла снимается сама, даже если процесс убит."""
    global _lock_handle
    if d is None:
        return True
    f = open(d / "bot.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _lock_handle = f
    return True


def backup_sqlite(db_path, backup_dir, keep=7):
    """Копия базы на старте: bananbot-ГГГГ-ММ-ДД.db, одна в день (первая за день), хранится последних `keep`.
    Возвращает путь копии или None. Безопасно при работающей базе (sqlite3 backup API)."""
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / ("bananbot-%s.db" % time.strftime("%Y-%m-%d"))
    made = None
    if not dest.exists():
        tmp = dest.with_suffix(".tmp")
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.replace(str(tmp), str(dest))
        made = dest
    for old in sorted(backup_dir.glob("bananbot-*.db"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return made
