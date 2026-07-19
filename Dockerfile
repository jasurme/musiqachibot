# Deterministic build for Railway (or any Docker host).
# Using a Dockerfile sidesteps Railway's Nixpacks-vs-Railpack builder ambiguity
# and guarantees Python 3.12 + ffmpeg (shazamio-core needs 3.12 wheels).
FROM python:3.12-slim

# ffmpeg = audio extraction, samples for recognition, round video-notes
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# long-polling worker — no web server / port needed
CMD ["python", "-m", "bot.main"]
