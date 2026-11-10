FROM python:3.12-slim
# Deno is the JavaScript runtime yt-dlp needs for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
# One worker with several threads, so the rate limiter and info cache are shared. Long timeout for big downloads.
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:8000", "-w", "1", "--threads", "8", "-t", "900"]
