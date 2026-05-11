FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY solana_bot.py .
COPY bot_final.py .
COPY coinbase.py .
COPY signals.py .
COPY trader.py .

CMD ["python", "solana_bot.py"]
