FROM mwader/static-ffmpeg:7.1 AS ffmpeg

# gifsicle 1.96 built from source — Debian/Ubuntu's apt package is stuck on
# 1.94 (2021), which lacks the --gamma option the GIF compression ladder
# depends on (app/worker/gif_optimize.py). Unlike the ffmpeg swap above,
# this isn't a dependency-bloat problem: gifsicle links only against libc/
# libm (confirmed via ldd against a source build during development), so
# apt's package was never large — it's purely a version problem. Built from
# the official dist tarball (ships a pregenerated ./configure, unlike the
# bare GitHub tag archive which needs autoconf/automake to regenerate one).
FROM debian:bookworm-slim AS gifsicle-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# SHA-256 pinned against the actual dist tarball (checked during
# development) — an unverified curl|build of a third-party source archive
# would otherwise ship whatever bytes the download happened to return.
RUN curl -sL -o /tmp/gifsicle.tar.gz https://www.lcdf.org/gifsicle/gifsicle-1.96.tar.gz \
    && echo "fd23d279681a6dfe3c15264e33f344045b3ba473da4d19f49e67a50994b077fb  /tmp/gifsicle.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/gifsicle.tar.gz -C /tmp \
    && cd /tmp/gifsicle-1.96 \
    && ./configure --disable-gifview \
    && make -j"$(nproc)"

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
COPY --from=gifsicle-builder /tmp/gifsicle-1.96/src/gifsicle /usr/local/bin/gifsicle

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd -m appuser \
    && mkdir -p /app/scratch /app/cache \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000 1919

CMD ["python", "-m", "app.main"]
