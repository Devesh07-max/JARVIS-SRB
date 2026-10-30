FROM python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip wheel && pip install --no-cache-dir -r requirements.txt
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1

# Single long-running process. The bot starts Flask internally in a daemon
# thread, so the platform does not need a shell/background process.
CMD ["python", "-m", "safe_repo"]
