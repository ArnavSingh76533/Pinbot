FROM python:3.12-slim
WORKDIR /app
COPY bot.py /app/bot.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
