FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt python-telegram-bot

COPY . .
RUN pip install --no-cache-dir -e .
