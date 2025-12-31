FROM python:3.12-slim

ENV TZ=Asia/Tehran

WORKDIR /app
COPY . .

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir aiogram cryptography asyncssh paramiko python-dotenv

CMD ["python", "bot.py"]