# -*- coding: utf-8 -*-
"""Перенос локальной SQLite-базы BananGPT в Postgres (Neon), который использует Render.

Запуск (из папки бота, в venv):
    venv\\Scripts\\python migrate_to_postgres.py --from C:\\bananagpt-data\\bananbot.db --to "postgresql://...?sslmode=require"
Без --apply только показывает, что будет сделано (ничего не пишет). Перед --apply остановите локального бота.

Слияние: пользователь есть и там и там → берётся локальная запись (статус, лимиты, модель, ник, активность),
но «всего запросов» складывается, а «в боте с» — самая ранняя дата. Только в Postgres → не трогается.
Диалоги: локальные перезаписывают одноимённые. Куки Gemini и статистика НЕ переносятся
(куки привязаны к переменным окружения Render — после возврата обнови GEMINI_SECURE_1PSID/1PSIDTS в Render)."""
import argparse
import json
import sys

from store import Store, USER_FIELDS


def open_store(target):
    if target.startswith(("postgres://", "postgresql://")):
        st = Store(url=target)
    else:
        st = Store(path=target)
    st.init()
    return st


def merge_user(local, remote):
    if remote is None:
        return dict(local)
    out = dict(local)
    out["total"] = int(local.get("total") or 0) + int(remote.get("total") or 0)
    created = [c for c in (local.get("created_at"), remote.get("created_at")) if c]
    out["created_at"] = min(created) if created else None
    for f in ("username", "first_name"):
        out[f] = local.get(f) or remote.get(f)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", required=True, help="путь к локальной SQLite-базе")
    ap.add_argument("--to", dest="dst", required=True, help="строка Postgres (DATABASE_URL с Render) или путь к SQLite")
    ap.add_argument("--apply", action="store_true", help="реально записать (без флага — только отчёт)")
    a = ap.parse_args(argv)

    src, dst = open_store(a.src), open_store(a.dst)
    remote = {u["id"]: u for u in dst.load_users()}
    users = src.load_users()
    new = sum(1 for u in users if u["id"] not in remote)
    print("Пользователей локально: %d (новых для Postgres: %d, обновится: %d)" % (len(users), new, len(users) - new))
    contexts = src.load_contexts()
    print("Диалогов локально: %d" % len(contexts))
    if not a.apply:
        print("Это пробный прогон. Добавь --apply, чтобы записать.")
        return 0
    for u in users:
        dst._upsert_user(merge_user(u, remote.get(u["id"])))
    for chat_id, items in contexts.items():
        dst._upsert_context(chat_id, json.dumps(items, ensure_ascii=False))
    print("Готово: перенесено пользователей %d, диалогов %d." % (len(users), len(contexts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
