# Build notes: ffmpeg rendering, 3D handling, subtitle burn-in, video sync

Non-obvious bugs and diagnoses for `app/worker/ffmpeg.py` and
`app/worker/subtitle_render.py`. Read the relevant section before touching
either file — several of these look like they'd "just work" and don't.

## MVP-era ffmpeg gotchas (read before touching `app/worker/ffmpeg.py`)

- **ffmpeg `-t` must be an input option, not an output option.** Placing
  `-t <duration>` *after* `-i` only bounds the output stream's timestamps —
  which does nothing for filters like `palettegen`/`paletteuse` that emit a
  single frame at the very end of the filter graph. With `-t` as an output
  option, ffmpeg keeps decoding and feeding frames into the filter for the
  rest of the file, since there's no rolling output PTS for it to cut off
  against. Fix: put `-ss` and `-t` **together, both before `-i`** — as input
  options, `-t` bounds how much is actually *read*, which is what stops it.
  See `build_seek_args()` and its docstring/regression test.
- **The `image2` muxer needs `-update 1`** to write a single still image
  (e.g. the GIF palette PNG) rather than expecting a `%d` sequence pattern.
- **Always drain both `stdout` and `stderr`** from an ffmpeg subprocess
  (`communicate()`, not a manual read loop on one pipe) — ffmpeg writes
  verbose progress to `stderr`, and reading only `stdout` risks a deadlock
  once `stderr`'s OS pipe buffer fills and ffmpeg blocks trying to write to
  it.
- **Always wrap ffmpeg subprocess calls in a timeout that kills the
  process** on expiry. A pathological or unusually slow-to-seek source file
  should fail cleanly with a clear error, never hang the request
  indefinitely.
- **Docker auto-creates missing bind-mount source directories as `root`.**
  The scratch/render temp dir (`./scratch:/app/scratch` in
  `docker-compose.yml`) needs to exist and be owned by the same UID as the
  container's non-root user *before* first `docker compose up`, or the
  container can't write to it. Documented as an explicit setup step in
  `README.md` and the troubleshooting section.

## MP4/WebM output bugs (read before touching `_render_video`)

- **Without explicit `-map 0:v:0`, ffmpeg's default stream selection can
  silently include a source subtitle track in the output — and demuxing it
  has no fast-seek, so it can hang the whole render.** Reproduced on this
  project's own library: a webm request for a title with a forced German
  subtitle track hung past the 60s render timeout; the *video* portion
  actually finished in under a second, but ffmpeg kept blocking trying to
  read subtitle packets across the whole remaining file before it would
  finalize the output. mp4 didn't reproduce the *hang* on this same file
  (its muxer's default stream selection is less willing to auto-include a
  subtitle track than webm/Matroska's is) — but relying on that
  container-specific behavior would be fragile, so `-map 0:v:0` is
  unconditional for every non-GIF encode, not just webm.
- **`-map 0:v:0` alone doesn't stop the mp4 muxer copying the source's
  chapter list into the output.** Chapters aren't a "stream" `-map`
  controls, so even with clean video-only stream mapping, a 2-4 second mp4
  clip request still produced a second "data" track whose duration matched
  the *source film's* full runtime — harmless in that it didn't hang
  (chapter metadata is cheap to copy, unlike an actual subtitle stream),
  but real, confirmed output pollution: it leaked the full film's duration
  into a clip that should have no idea how long its source is. Fixed with
  `-map_chapters -1 -map_metadata -1` alongside `-map 0:v:0` — the latter
  also strips title/encoder tags so the output file doesn't carry the
  source film's metadata either. **Verify any future change to this ffmpeg
  command with `ffprobe -show_streams` on the actual output**, not just
  "did it complete without erroring" — both bugs here produced a file that
  looked superficially fine (non-empty, playable) while carrying data it
  shouldn't have.

## Subtitle burn-in bugs (read before touching `app/worker/subtitle_render.py` or the container's font setup)

- **The base `python:3.12-slim-bookworm` image has exactly one font family
  (DejaVu) — neither "Arial" nor "Impact" (the fonts the style presets
  originally asked for) are actually installed.** libass doesn't error on
  an unresolvable font name, it silently substitutes via fontconfig — and
  that substitution isn't necessarily even the right *kind* of font:
  confirmed on this project's own container, `fc-match Impact` resolved to
  **a monospace face**, which would have rendered the "Meme" preset in a
  typewriter font with no error or warning anywhere. Fixed by installing
  `fonts-liberation` in the Dockerfile (Liberation Sans is a
  metric-compatible Arial substitute with the fontconfig alias already
  wired up) and pointing every preset directly at the real installed
  family name (`"Liberation Sans"`) rather than a name that only resolves
  via an alias — verified via `fc-match`/`fc-list` inside the built
  container, then confirmed visually (see below). No true Impact-alike is
  installed (proprietary, not in Debian's repos); Meme uses Liberation
  Sans + bold + all-caps instead.
- **ASS alpha is inverted from the usual convention — `00` is fully
  opaque, `FF` is fully transparent.** The Boxed preset's first cut used
  `&H80000000` (roughly 50%) expecting a visibly translucent box; against
  real footage with a light background it was nearly invisible in a
  rendered test frame. Fixed by using `&H00000000` (fully opaque) for a
  real "black box," matching what the preset name promises.
- **Burned-in text size depends on `PlayResX`/`PlayResY` matching the
  clip's actual output frame size, not the source file's.** libass scales
  a style's font size/margins relative to these two fields; get them wrong
  and the text is the wrong size for the frame, not just mispositioned.
  `_write_ass_file()` probes the source's width/height via a
  `probe_video_dimensions()` ffprobe call and computes the *actual* scaled
  output height itself — which required switching the scale filter from
  `scale={width}:-1` to `scale={width}:-2` (guarantees an even height) so
  the manual calculation and ffmpeg's own scaling agree. Do not
  reintroduce `-1` here without re-deriving this.
- **Verification for this feature meant actually looking at rendered
  pixels, not just checking the render succeeded.** Both bugs above (wrong
  font, invisible box) produced a 200 response and a playable file — "it
  rendered" told us nothing. Caught by rendering real quote-driven clips
  against this project's own library at a known subtitle line, extracting
  a still frame (`ffmpeg -update 1 -frames:v 1 out.png` — same `image2`
  muxer gotcha as the MVP's palette-PNG step above), and visually
  inspecting it. Repeat this check for any future change to
  `STYLE_PRESETS` or the ASS-building code — a `ffprobe` pass can't catch
  "the text is the wrong font/color/size," only a rendered frame can.

## 3D format handling: two wrong fixes before the right one

`LibraryConfig.three_d_format` (`app/settings.py`, default `"none"`) tags a
library's packing (`side_by_side` | `over_under`); `ClipRenderer`
(`app/worker/ffmpeg.py`) inserts a crop-to-one-eye step into the filter
graph before scaling/subtitles whenever a library is so tagged, and
`_write_ass_file` adjusts the source dimensions it computes `PlayResY` from
to match the post-crop single-eye frame. `probe_stereo_format()` checks
each file's own Matroska `StereoMode` tag first and overrides the library
default when present, falling back to the configured default only for
untagged files.

**Real per-file packing varies within one library**, confirmed against
this developer's own `3D` library: a "Full-SBS" *Ready Player One* release
carries a real `StereoMode` tag, but *Dune (2021)* in the same library has
no such tag at all despite being (visually confirmed) over/under — hence
the per-file probe rather than trusting the library-wide default alone.

- **First fix (crop only) looked right but was stretched.** The *Ready
  Player One* Full-SBS file tags its packed 3840x1080 frame with sample
  aspect ratio 2:1 (`ffprobe` confirmed) — describing the combined stereo
  pair, not a single eye. ffmpeg's `crop` filter carries that SAR over
  unchanged onto the cropped 1920x1080 single eye, which then reports (and
  plays back) a stretched 32:9 DAR instead of the correct 16:9. Fixed with
  `setsar=1` inserted immediately after crop, before scale
  (`_scale_and_subtitle_filter()`). Confirmed via `ffprobe` on a real
  rendered frame (SAR 2:1/DAR 32:9 → SAR 1:1/DAR 16:9) and visually.
  **Lesson**: "not squished/doubled anymore" was necessary but not
  sufficient verification — check the actual DAR of a rendered frame with
  `ffprobe`, not just that the framing looks roughly right by eye, since a
  stretched-but-still-single-eye frame can look deceptively close to
  correct in a quick visual scan (this one did, in the original
  verification pass).
- **Second fix: still stretched, but only for Dune.** The SAR fix made the
  *packed frame's own tag* stop lying about the cropped eye's aspect
  ratio, but that's a different bug from what was actually affecting Dune:
  a 3D pack comes in two flavors per axis — "full" (each eye stored at
  native resolution, so the packed frame is simply double-width/height,
  and a crop is all that's needed) and "half"/squeezed (each eye
  compressed to fit within a normal single-frame canvas — the more common
  space-saving rip format — needing a 2x unsqueeze stretch back to native
  size *after* cropping, which the SAR fix never did). Neither real file
  self-tags which flavor it is, but `ffprobe -vf cropdetect` on Dune's
  cropped top half found a `1920x402` content box (ratio 4.78, impossible
  for any real film) that becomes `1920x804` (2.39:1, Cinemascope) once
  doubled — proof it's squeezed. `_three_d_plan()` distinguishes the two
  purely from the packed frame's own raw pixel aspect ratio — a real
  single flat frame is never as wide (side-by-side) or as tall/square
  (over-under) as a full pack, so crossing that ratio can only mean two
  native-resolution eyes, never two squeezed ones — and inserts a
  `scale=iw*2:ih` (side-by-side) / `scale=iw:ih*2` (over-under) unsqueeze
  step between crop and `setsar=1` for the squeezed case. Verified against
  both real files: Ready Player One still classifies as full (crop only),
  Dune now classifies as squeezed (crop + unsqueeze) and its rendered
  output's own content box (via `cropdetect` on the *output*, not just the
  source) reads 2.4:1, not the previous 4.78:1. **Sharper lesson**: two
  consecutive "looks basically right" visual checks on Dune's cropped
  frame both missed a 2x vertical squish — `ffprobe cropdetect`'s numeric
  content-box ratio is what actually caught it, not eyeballing a still
  frame. Trust the numbers over the glance for anything aspect-ratio-related
  in this pipeline.

## Video sync diagnosis: seek is correct, drift was always subtitle desync

Settled definitively against ground truth after two earlier wrong turns —
read this before re-investigating anything that looks like "sync drift".

- **Seek is proven correct**: a fast `-ss`-before-`-i` seek to a timestamp
  and a fully-accurate linear decode to that same timestamp
  (`-vf select='between(t,X,X+0.05)'` from the start of the file) produce
  **pixel-identical frames**. Confirmed on this project's own library.
  Nothing to fix in `ffmpeg.py`.
- **Ground truth for "when is this line actually spoken" is the file's own
  embedded subtitle stream**, not an audio heuristic and not a player's
  playback impression. A remux's embedded PGS track was authored against
  that exact encode, so its cue times are definitionally correct for it:
  `ffprobe -v error -select_streams s:N -show_entries packet=pts_time
  -read_intervals "%+90" -of csv=p=0 <file>` (packets pair up as
  display/clear). A **contact sheet** of the opening is the cheap visual
  cross-check: `-vf "fps=1/2,scale=320:-2,drawtext=...,tile=4x5"`.
- **Worked example (Snatch)**: embedded PGS put the first real cue at
  **34.327s** (frames confirm: 12s is still the Screen Gems logo, 34s is
  the "A Film by Guy Ritchie" card over Turkish/Tommy). The original
  sidecar claimed 37.704s (~3.4s late — the originally-reported symptom,
  which was real), a Bazarr "99% match" re-fetch claimed 12.559s (~22s
  early, i.e. over a studio ident), and `alass` corrected it to 34.501s
  (within 174ms of ground truth).
- **Full-runtime confirmation, not just the opening**: correlating both
  the alass-fixed file and the Bazarr file against embedded PGS at five
  points spanning the whole film (5/25/50/75/93 min) showed alass holding
  within ±0.25s throughout (no drift — a single global shift really was
  the correct fix) while the Bazarr file held a near-constant ~+22s error
  throughout. Worth doing before trusting any single-point measurement —
  a fix (or a bug) that only holds at one timestamp isn't confirmed yet.
- **The real trap, and the one that actually cost the most time: Plex's
  client-side "Auto Sync Subtitles" toggle silently caches a computed
  offset per item, independent of the visible "Subtitles Offset: 0 ms"
  field, and survives a server restart.** Sequence that played out:
  Bazarr's bad re-fetch was ~22s early → Plex's auto-sync silently
  computed and cached ~+22s to compensate → the file *looked* fine in
  Plex, which is what first made a correct diagnosis look like a false
  positive → `alass` then fixed the underlying file to true (confirmed:
  file content, Plex's served copy, and Plex's own duration/timeline all
  checked out identical and correct by direct HTTP/ffprobe inspection) →
  Plex kept applying its stale cached +22s on top of the now-correct file
  → subtitle rendered ~22s late, the opposite direction from before, on a
  file that was actually right. The fix is disabling "Auto Sync
  Subtitles" in the Plex client's playback settings (not the "Subtitles
  Offset" field, which stays 0 throughout and tells you nothing).
  **Lesson**: a player's *rendered* timing is not ground truth for
  whether a sidecar file itself is correct — verify the file directly
  (embedded-track cross-correlation above, or Plex's raw stream URL
  fetched and byte-compared to disk) before trusting what any client
  displays, since a client-side auto-sync feature can mask a bad file or
  corrupt a good one with no visible indicator either way.
- **Bazarr's match score is text/hash similarity, not verified timing** —
  a "99% match" shipped a subtitle 22s out for this exact remux.
  `alass <video> <bad.srt> <fixed.srt>` fixed it in ~3 minutes on a 26GB
  remux (it reported a single constant shift, no splits, later confirmed
  accurate across the full runtime); it also handles non-uniform drift,
  which `ffsubsync`'s single-offset correction does not. Given this
  project's library relies on Plex's own auto-sync as a safety net,
  consider recommending it stay **off** once a title's subtitle is
  independently verified — it has no way to distinguish "correcting a bad
  file" from "breaking a good one," and both look identical to it.
