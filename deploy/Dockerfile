FROM python:3.12-slim

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir -e ".[agents,server,gateway,scheduler,s3]" python-telegram-bot
