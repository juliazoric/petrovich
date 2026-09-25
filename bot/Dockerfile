FROM python:3.12-slim

# Только стандартная библиотека — pip-зависимостей нет вообще.
WORKDIR /app

COPY bot.py persona.txt ./
COPY jokes.txt* ./

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data

CMD ["python3", "bot.py"]
