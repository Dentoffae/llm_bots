FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py database.py ./

# Директория для персистентной БД (монтируется как volume)
RUN mkdir -p /data

ENV DB_PATH=/data/nemo.db

CMD ["python", "bot.py"]
