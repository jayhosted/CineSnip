FROM python:3.12-slim-bookworm

# fonts-liberation ships Liberation Sans/Serif/Mono — metric-compatible
# Arial/Times/Courier substitutes with fontconfig aliases already wired up,
# so subtitle style presets that ask for "Arial" (app/worker/subtitle_render.py)
# actually get an Arial-like face burned in instead of silently falling
# back to the base image's only font (DejaVu Sans) — confirmed via
# `fc-match` and a real burned-in test frame during development.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd -m appuser \
    && mkdir -p /app/scratch /app/cache \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "-m", "app.main"]
