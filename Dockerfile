FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DENO_INSTALL=/usr/local \
    PATH="/usr/local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git curl ca-certificates unzip \
    && curl -fsSL https://deno.land/install.sh | sh \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip wheel \
    && pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY . .

CMD ["python", "-m", "safe_repo"]
