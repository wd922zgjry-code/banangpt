# Образ без секретов: токены и куки задаются переменными окружения на хостинге.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

# бот работает НЕ от root
RUN useradd --create-home --uid 10001 bot
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py textfmt.py chartbox.py docgen.py store.py users.py ./

USER bot
CMD ["python", "bot.py"]
