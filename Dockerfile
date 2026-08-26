FROM mwader/static-ffmpeg:7.1 AS ffmpeg

FROM python:3.12-slim-bookworm

# fonts-liberation ships Liberation Sans/Serif/Mono — metric-compatible
# Arial/Times/Courier substitutes with fontconfig aliases already wired up,
# so subtitle style presets that ask for "Arial" (app/worker/subtitle_render.py)
# actually get an Arial-like face burned in instead of silently falling
# back to the base image's only font (DejaVu Sans) — confirmed via
# `fc-match` and a real burned-in test frame during development.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Static ffmpeg/ffprobe binaries instead of Debian's apt package — the apt
# package pulls in the full shared-library ecosystem for every codec/
# protocol ffmpeg supports (~463MB), most of which CineSnip never touches.
# These two static binaries bundle only what's linked directly into the
# executables (~258MB combined), no separate runtime shared-lib tree.
# Confirmed via `ffmpeg -buildconf` before switching that this build
# includes libass (required for subtitle burn-in, Section 7 in CLAUDE.md)
# and libx264/libx265/libvpx (required for mp4/webm output) — re-verify
# with any future version bump, and always confirm with a real rendered
# frame afterward (CLAUDE.md's standing rule for any ffmpeg change: a
# build succeeding proves nothing about the actual output).
COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

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
