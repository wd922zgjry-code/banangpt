# BananGPT 🍌

Telegram-бот поверх личного веб-аккаунта Gemini (куки `__Secure-1PSID` / `__Secure-1PSIDTS`).

## Файлы
- `bot.py` — сам бот
- `textfmt.py` — очистка текста от Markdown/LaTeX (знаки умножения остаются)
- `chartbox.py` — изолированное построение графиков
- `test_bot.py` — автотесты (`python test_bot.py`, сеть не нужна)
- `requirements.txt`, `Procfile`

## Переменные окружения
| Переменная | Нужна | Что это |
|---|---|---|
| `TELEGRAM_TOKEN` | да | токен от @BotFather |
| `GEMINI_SECURE_1PSID` | да | кука `__Secure-1PSID` |
| `GEMINI_SECURE_1PSIDTS` | нет | кука `__Secure-1PSIDTS` |
| `ALLOWED_IDS` | **да** | твой Telegram ID (несколько — через запятую). Пусто = бот никому не отвечает. ID покажет `/myid` |
| `GEMINI_COOKIE_PATH` | нет | папка для обновляемых кук, например `/tmp/gemini_webapi` |
| `KEEPALIVE_URL` | нет | публичный URL сервиса (на Render подставляется сам из `RENDER_EXTERNAL_URL`) |
| `DEFAULT_MODEL` | нет | модель по умолчанию (`flash`, `pro`…); пусто = «авто» |
| `MODEL_TIMEOUT` | нет | через сколько секунд молчащая выбранная модель заменяется режимом «авто» (60) |
| `BOT_THREADS`, `REQUEST_TIMEOUT`, `LOG_LEVEL` | нет | потоки (8), общий таймаут Gemini в с (150), уровень логов |

## Render
1. **Python 3.11+** (требование gemini-webapi): переменная `PYTHON_VERSION` = полная версия, например `3.12.3`.
2. Build Command: `pip install -r requirements.txt`. Start Command: `python bot.py`
   (Render **игнорирует Procfile** — он нужен только для Heroku/Railway).
3. Бесплатный тариф засыпает без входящих запросов: бот пингует сам себя, но надёжнее добавить внешний пингер (UptimeRobot → URL сервиса).
4. Куки лучше брать из Firefox в приватном окне и сразу закрывать окно; куки Chromium (Device Bound Session Credentials) живут часами.
5. После первого успешного запуска сделай `pip freeze` и зафиксируй версии в `requirements.txt`.

## Модели
Список моделей gemini-webapi подгружает с аккаунта при старте. Пресеты (4 Argon, 3.8 Flash, 3.5 Flash, 3.5 Lite, 3.1 Pro)
проверяются по этому списку: чего нет на аккаунте — на кнопке 🚫 и не выбирается. `/models` покажет реальные имена,
`/model <имя>` выберет любое из них. Если имена пресетов в `MODEL_PRESETS` (bot.py) устарели — поправь их там.

## Безопасность графиков
Код от Gemini проходит AST-фильтр и выполняется в отдельном процессе с пустым окружением, таймаутом и лимитами.
Это защита в глубину, а не настоящая песочница: для полной изоляции запускай бота в контейнере без лишних секретов на диске.
