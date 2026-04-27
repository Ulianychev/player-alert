FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_PATH=/app/data/state.json

WORKDIR /app
COPY player_tracker.py /app/player_tracker.py
COPY telegram_player_bot.py /app/telegram_player_bot.py

RUN useradd --create-home --uid 10001 tracker
USER tracker

CMD ["python", "/app/telegram_player_bot.py"]
