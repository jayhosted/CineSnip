# Build notes: Docker image size

## Static ffmpeg binaries instead of Debian's apt package

Debian's `ffmpeg` apt package pulls in the full shared-library ecosystem
for every codec/protocol ffmpeg supports (~463MB) — most of which this
project never touches (it only decodes common film/TV containers, encodes
GIF/H.264/VP9, and burns subtitles via libass). Swapped for static
`ffmpeg`/`ffprobe` binaries via a multi-stage
`COPY --from=mwader/static-ffmpeg`, which bundle only what's linked into
the two executables (~258MB combined — confirmed via `ffmpeg -buildconf`
before switching that this build includes `libass`, `libx264`, `libx265`,
`libvpx`, everything the rendering pipeline actually needs). Total image:
**960MB → 706MB**.

Verified against real media before trusting the swap (a successful build
proves nothing about ffmpeg output): a real subtitle-burned-in render
(Peep Show) checked as an extracted still frame, and a real 3D clip (Dune
2021, the squeezed-pack title — see
`docs/build-notes/ffmpeg-rendering.md`) checked both visually and via its
output dimensions (480×270, correct 16:9 — no doubling/stretch
regression). Full existing test suite unaffected (128 tests at the time —
they test ffmpeg *command construction*, not real execution, so this
class of change is never caught by them; only real-media verification
catches it). Embedded-subtitle-stream extraction (`-map 0:s:N -f srt`)
also verified: this developer's library is heavily sidecar-based
(Bazarr), so the normal sidecar-first flow never reaches the embedded
path on real titles here — worked around by calling
`probe_subtitle_streams()`/`extract_embedded_subtitle()` directly against
real files, bypassing sidecar preference. First attempt (Akira, a 2160p
HDR remux) hit the documented 180s extraction timeout — expected, not a
regression (see `docs/build-notes/subtitles-and-search.md`). A smaller
file (10 Things I Hate About You, 5.4GB) succeeded: real `subrip` stream
demuxed to 1,402 correctly-timed SRT entries in 47.5s.

## `uvicorn[standard]` → plain `uvicorn` + explicit `uvloop`/`httptools`, and a real correctness find alongside it

`[standard]` bundles four extras; per-package sizes measured in the real
container: `uvloop` 16MB, `httptools` 1.8MB (both genuine performance
pieces, kept), `websockets` 1.9MB and `watchfiles` 1.2MB (both genuinely
unused — no WebSocket routes exist anywhere in this app, and
`app/main.py` never runs uvicorn with `--reload` — dropped).

While checking this, found that `uvloop` had never actually been active:
`uvicorn.Server.serve()` is awaited directly inside `asyncio.gather()`
(`app/main.py`, needed so the Discord bot and worker API share one event
loop) rather than via `uvicorn.run()`/`Server.run()`, and uvicorn's
automatic uvloop activation only fires when uvicorn owns the top-level
loop itself — confirmed with a real check (`asyncio.get_running_loop()`
under the app's actual `asyncio.run()` pattern returned the standard
asyncio loop, not uvloop). So the 16MB was being paid for with zero
benefit. Fixed by switching the entry point to `uvloop.run(main())` — a
drop-in `asyncio.run()` replacement that does activate it; confirmed via
the same live check now returning `uvloop.Loop`.

Verified against real usage after the swap, not just "the container
started": `/healthz`, `/search`, and a real render (Snatch) all
succeeded — the render specifically exercises
`asyncio.create_subprocess_exec` (`app/worker/subprocess_utils.py`), the
actual risk surface for an event-loop implementation swap, since that's
where a uvloop/child-process-watcher incompatibility would show up if
there were one. Image: 706MB → 701MB (small — the real value here was
fixing an inert dependency, not the size).
