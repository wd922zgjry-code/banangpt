# -*- coding: utf-8 -*-
"""Пользователи бота: статусы, заявки на доступ, баны, дневные лимиты. Без привязки к Telegram."""
import re
import threading
import time
from datetime import datetime, timedelta, timezone

STATUS_ACTIVE, STATUS_PENDING, STATUS_BANNED, STATUS_MUTED = "active", "pending", "banned", "muted"

_UNITS = {"m": 60, "м": 60, "мин": 60, "min": 60, "h": 3600, "ч": 3600, "час": 3600, "d": 86400, "д": 86400,
          "дн": 86400, "w": 604800, "н": 604800, "нед": 604800}


def parse_duration(token):
    """'30m', '2h', '1d', '1w', '30м', '2ч', '1д', '1н' -> секунды; иначе None."""
    m = re.match(r"^(\d{1,4})\s*([a-zA-Zа-яА-Я]{1,4})$", (token or "").strip())
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return int(m.group(1)) * _UNITS[m.group(2).lower()]


def fmt_duration(sec):
    sec = int(sec)
    if sec < 60:
        return "меньше минуты"
    if sec < 3600:
        return "%d мин" % (sec // 60)
    if sec < 86400:
        h, m = sec // 3600, sec % 3600 // 60
        return "%d ч %d мин" % (h, m) if m else "%d ч" % h
    d, h = sec // 86400, sec % 86400 // 3600
    return "%d дн %d ч" % (d, h) if h else "%d дн" % d


class UserRegistry:
    def __init__(self, store=None, admin_ids=(), max_users=10, default_limit=30, tz_offset=3, max_pending=20):
        self.store = store
        self.admin_ids = set(admin_ids)
        self.max_users = max_users
        self.default_limit = default_limit  # 0 = без лимита
        self.tz_offset = tz_offset
        self.max_pending = max_pending
        self.users = {}
        self.lock = threading.RLock()

    # ── загрузка ──
    def load(self, seed_ids=()):
        """Читает базу; ID из seed_ids (переменная ALLOWED_IDS) становятся активными, если их ещё нет в базе."""
        if self.store is not None:
            for row in self.store.load_users():
                self.users[int(row["id"])] = row
        for uid in set(seed_ids) | self.admin_ids:
            if uid not in self.users:
                self.users[uid] = self._new(uid, None, None, STATUS_ACTIVE)
                self._save(self.users[uid])

    def _new(self, uid, username, first_name, status):
        now = int(time.time())
        return {"id": uid, "username": username, "first_name": first_name, "status": status,
                "daily_limit": None, "model": None, "mode": None, "total": 0, "created_at": now,
                "last_seen": now, "usage_date": None, "usage_count": 0, "note": None, "until_ts": None}

    def _save(self, u):
        if self.store is not None:
            self.store.save_user(u)

    # ── доступ ──
    def is_admin(self, uid):
        return uid in self.admin_ids

    def _refresh(self, u):
        """Мут/временный бан закончились — возвращаем в активные."""
        until = u.get("until_ts")
        if u["status"] in (STATUS_MUTED, STATUS_BANNED) and until and until <= time.time():
            u["status"], u["until_ts"], u["note"] = STATUS_ACTIVE, None, None
            self._save(u)

    def status(self, uid):
        if uid in self.admin_ids:
            return "admin"
        u = self.users.get(uid)
        if u is None:
            return None
        self._refresh(u)
        return u["status"]

    def remaining(self, uid):
        """Сколько секунд осталось до конца мута/бана; None — бессрочно или не применимо."""
        u = self.users.get(uid)
        if u is None or self.status(uid) not in (STATUS_MUTED, STATUS_BANNED) or not u.get("until_ts"):
            return None
        return max(0, int(u["until_ts"] - time.time()))

    def is_allowed(self, uid):
        return self.status(uid) in ("admin", STATUS_ACTIVE)

    def touch(self, from_user):
        """Обновляет имя/ник и время последней активности (для уже известных)."""
        uid = from_user.id
        with self.lock:
            u = self.users.get(uid)
            if u is None:
                return None
            u["username"] = getattr(from_user, "username", None) or u.get("username")
            u["first_name"] = getattr(from_user, "first_name", None) or u.get("first_name")
            u["last_seen"] = int(time.time())
            return u

    def register_pending(self, from_user):
        """Новая заявка на доступ. Возвращает (user | None, создана_ли_новая)."""
        uid = from_user.id
        with self.lock:
            u = self.users.get(uid)
            if u is not None:
                return u, False
            if sum(1 for x in self.users.values() if x["status"] == STATUS_PENDING) >= self.max_pending:
                return None, False
            u = self._new(uid, getattr(from_user, "username", None), getattr(from_user, "first_name", None), STATUS_PENDING)
            self.users[uid] = u
            self._save(u)
            return u, True

    def active_count(self):
        """Занятые места: активные и замученные (админы не считаются)."""
        return sum(1 for uid in list(self.users) if self.status(uid) in (STATUS_ACTIVE, STATUS_MUTED))

    def approve(self, uid):
        with self.lock:
            u = self.users.get(uid)
            if u is None:
                u = self._new(uid, None, None, STATUS_PENDING)
                self.users[uid] = u
            if self.status(uid) not in (STATUS_ACTIVE, STATUS_MUTED) and self.active_count() >= self.max_users:
                return False, "Достигнут лимит: %d активных пользователей. Забань или удали кого-то (/ban, /removeuser) либо подними MAX_USERS." % self.max_users
            u["status"], u["note"], u["until_ts"] = STATUS_ACTIVE, None, None
            self._save(u)
            return True, "ok"

    def ban(self, uid, reason=None, seconds=None):
        with self.lock:
            if uid in self.admin_ids:
                return False, "Админа забанить нельзя."
            u = self.users.get(uid) or self._new(uid, None, None, STATUS_BANNED)
            self.users[uid] = u
            u["status"], u["note"] = STATUS_BANNED, (reason or None)
            u["until_ts"] = int(time.time() + seconds) if seconds else None
            self._save(u)
            return True, "ok"

    def mute(self, uid, seconds=None, reason=None):
        """Мут: человек остаётся в списке, но бот ему не отвечает (seconds=None — пока не снимут)."""
        with self.lock:
            if uid in self.admin_ids:
                return False, "Админа замутить нельзя."
            u = self.users.get(uid)
            if u is None or self.status(uid) not in (STATUS_ACTIVE, STATUS_MUTED):
                return False, "Мутить можно только активных пользователей."
            u["status"], u["note"] = STATUS_MUTED, (reason or None)
            u["until_ts"] = int(time.time() + seconds) if seconds else None
            self._save(u)
            return True, "ok"

    def unmute(self, uid):
        with self.lock:
            u = self.users.get(uid)
            if u is None or self.status(uid) != STATUS_MUTED:
                return False
            u["status"], u["note"], u["until_ts"] = STATUS_ACTIVE, None, None
            self._save(u)
            return True

    def remove(self, uid):
        with self.lock:
            if uid in self.admin_ids:
                return False
            existed = self.users.pop(uid, None) is not None
            if existed and self.store is not None:
                self.store.delete_user(uid)
            return existed

    def find_username(self, name):
        name = name.lstrip("@").lower()
        for u in self.users.values():
            if (u.get("username") or "").lower() == name:
                return u["id"]
        return None

    # ── лимиты ──
    def today(self):
        return (datetime.now(timezone.utc) + timedelta(hours=self.tz_offset)).strftime("%Y-%m-%d")

    def limit_for(self, uid):
        """0 = без ограничений."""
        if uid in self.admin_ids:
            return 0
        u = self.users.get(uid)
        if u is not None and u.get("daily_limit") is not None:
            return int(u["daily_limit"])
        return self.default_limit

    def usage_today(self, uid):
        u = self.users.get(uid)
        if not u or u.get("usage_date") != self.today():
            return 0
        return int(u.get("usage_count") or 0)

    def check_quota(self, uid):
        """(можно_ли, использовано, лимит)"""
        limit, used = self.limit_for(uid), self.usage_today(uid)
        return (limit == 0 or used < limit), used, limit

    def count_usage(self, uid):
        with self.lock:
            u = self.users.get(uid)
            if u is None:
                return
            today = self.today()
            if u.get("usage_date") != today:
                u["usage_date"], u["usage_count"] = today, 0
            u["usage_count"] = int(u.get("usage_count") or 0) + 1
            u["total"] = int(u.get("total") or 0) + 1
            self._save(u)

    def set_limit(self, uid, n):
        with self.lock:
            u = self.users.get(uid)
            if u is None:
                return False
            u["daily_limit"] = n
            self._save(u)
            return True

    def set_pref(self, uid, model="__keep__", mode="__keep__"):
        with self.lock:
            u = self.users.get(uid)
            if u is None:
                return
            if model != "__keep__":
                u["model"] = model
            if mode != "__keep__":
                u["mode"] = mode
            self._save(u)

    def listing(self):
        order = {"admin": 0, STATUS_ACTIVE: 1, STATUS_MUTED: 2, STATUS_PENDING: 3, STATUS_BANNED: 4}
        return sorted(self.users.values(), key=lambda u: (order.get(self.status(u["id"]), 9), -(u.get("last_seen") or 0)))
