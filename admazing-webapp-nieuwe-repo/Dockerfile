# ADmazing test tool — deploy container.
# Render (and most PaaS providers) auto-detect this file and build from it,
# so no extra "build command" box needs to be filled in by hand.
#
# ffmpeg is a hard dependency of the render pipeline (render/ffmpeg_utils.py
# shells out to the `ffmpeg`/`ffprobe` binaries directly) and is NOT part of
# any pip package, so it has to be installed at the OS level here.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime dirs the webapp writes uploads/results into — created up front so
# the app doesn't depend on write-on-first-request behavior.
RUN mkdir -p webapp/uploads webapp/results

WORKDIR /app/webapp
ENV PYTHONUNBUFFERED=1

# Render (and similar platforms) inject $PORT at runtime; fall back to 8420
# for any other host that doesn't set it.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:${PORT:-8420} app:app"]
