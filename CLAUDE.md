# CineSnip — Technical Architecture & Development Plan
*Living reference for Claude Code sessions — also usable as the project's CLAUDE.md*

## Project summary

A self-hosted Discord bot + local web app that lets you search your own Plex library (films and TV shows) and generate a short GIF or MP4/WebM clip from a spoken quote or timestamp, posted back into Discord. Designed to be installable by other self-hosted Plex users, not just for personal use — but every installation is fully independent: no central service, no shared infrastructure, no data of any kind passing through anyone but the installer's own Plex/Discord.

Inspired by conversation with the developer of [tvgif](https://github.com/warmans/tvgif), a similar but structurally different tool (tvgif pre-converts a whole library to webm+srt ahead of time; this project integrates live with Plex and extracts on demand). **No code should be copied from tvgif** — standard/boilerplate ffmpeg or HTTP patterns are fine since they're not anyone's IP, but the implementation should be built independently out of respect for that developer.

## This developer's own setup (used as the reference/test environment)

- Plex runs **natively on Windows** (not Dockerized) on a separate **server PC** from the one used for development.
- Docker (Docker Desktop, WSL2 backend) also runs on that server PC, alongside other unrelated services already running in Docker there.
- Server PC has an **RTX 3070** — usable for NVENC encode/decode and for speeding up local Whisper transcription, but must remain optional in the base install since not every installer will have a GPU.
- Four Plex libraries: **Movies**, **TV Shows**, a separate **4K Movies** library, and a separate **3D Movies** library.
- Media folders span two drives:
  - `D:\Plex Additional\Movies` and `E:\Media\Video\Movies`
  - `D:\Plex Additional\TV Shows` and `E:\Media\Video\TV Shows`
- Uses **Bazarr**, so the large majority of media has a separate `.srt` subtitle file sitting in the same folder as the video, rather than embedded subtitles. Some older/less-common titles may still only have embedded subs or none at all.
- Development happens via **Claude Code Desktop, connected over SSH into a Ubuntu WSL2 distro on the server PC** (not the internal `docker-desktop` distro), so that Claude Code can directly run `docker compose`, reach Plex over `host.docker.internal`, and access the mounted media drives, all against the real environment rather than a stand-in.
- Because the install docs need to work for other people too, **both** a native-host Plex setup (like this one) and a Dockerized-Plex setup must be documented and supported — the only thing that changes between them is how the container reaches Plex on the network.
- **A Claude Code session in this WSL2 distro has real, direct access**: `.env`/`config.yaml` are populated with live values (not just the `.example` templates), both media drives are mounted and browsable at `/mnt/d/Plex Additional/Movies` and `/mnt/e/Media/Video/Movies`, and `docker compose` can build/run the real container against the real Plex library. **Verify worker-layer changes end-to-end against real media during development, not just with unit tests** — real subtitle/media files surface bugs synthetic test data doesn't.
- **Docker group gotcha**: the WSL2 user (`jaypw`) is not always in the `docker` group and there's no passwordless `sudo`, so a fresh session may get "permission denied" talking to the Docker daemon. Fix: have the user run `sudo usermod -aG docker jaypw` themselves (needs their password, so Claude Code can't run it directly) — the new group membership then works immediately in the *same* session via `sg docker -c '<command>'`, without needing to restart the whole session.
- **The worker API is loopback-only inside the container by design** (Section 9 — no inbound public endpoint). To manually hit a worker endpoint (e.g. the `/subtitles/{rating_key}` diagnostic route) from a dev session, use `docker compose exec <service> <command>` to run the request *from inside* the container's network namespace — a plain `curl` from the host/WSL2 side won't reach it, since no port is published in `docker-compose.yml`.

## Workflow decisions already made (apply these, don't re-litigate)

1. **✅ Output format defaults to GIF — reversed back from an MP4/WebM default that shipped, then failed real Discord testing.** MP4/WebM were tried as the default for their much smaller file size at the same visual quality (~55KB mp4 / ~27KB webm vs. ~5.7MB gif for an identical 4s/480px clip, confirmed against the real library) — the assumption being "Discord renders both inline like a GIF." **That assumption was wrong.** Live testing in a real server showed mp4/webm attachments render as an actual video player (play button, scrubber, volume slider) with no autoplay/loop, and — the deciding factor — can't be added to Discord's GIF-picker favorites the way a real `.gif` attachment can. Both were the entire point of defaulting away from GIF in the first place, so the default reverted. `format:mp4`/`format:webm` on `/cinesnip`/`/snip` remain fully available as explicit opt-ins for anyone who'd rather trade autoplay/favoriting for a much smaller file (e.g. saving/reposting outside Discord).
2. **One container, one process to start.** Discord bot layer and worker layer (Plex, subtitles, ffmpeg) run in a single Python process inside a single Docker container. No message queue or microservices for v1.
3. **Structure the worker as a small internal HTTP API from the start** (e.g. FastAPI), even though the Discord bot is its only client initially. This makes a future local web app (setup wizard + manual GIF-generation UI) a thin second client rather than a rebuild.
4. **No separate "confirm the film" step for movies** — reversed from the original decision after real usage showed it was pure friction: autocomplete already pins an exact `rating_key` (title + year shown right in the picker), and CineSnip currently only searches one Plex library, so there's nothing left to disambiguate by the time the command runs. **Revisit once multi-library search ships** (Section 3) — a same-titled result from a different library would need surfacing again at that point, likely back as a lightweight non-interactive note rather than a click-gated step. **"Confirm the timestamp" still stands** as its own step (`QuoteMatchView` in `app/bot/cogs/gif.py`) — for a quote match this is real disambiguation (which candidate line, at what confidence), not restating a choice already made. Its Cancel currently ends the whole interaction rather than "backing up" to anything — with the film-confirm step gone, there's nothing before it to back up to; a full `/cinesnip` restart is what backing out means today.
5. **Config split**: `.env` for secrets (Discord bot token, Plex token), `config.yaml` for everything else (per-library path mappings, style presets, GPU toggle, feature flags). Standard self-hosted-app practice — keeps secrets out of anything that might get shared or hand-edited casually.
6. **Setup wizard and manual-generation UI are the same small local web app**, not two separate builds — a `/setup` route for first-run/reconfiguration, a `/generate` route for browsing/making clips outside Discord. Build the Discord bot and prove the core pipeline first; build this web app as a fast-follow, not alongside. Full spec for the wizard itself: see Section 14.

## 1. Overall architecture

- **Discord layer** — Gateway connection, slash commands, buttons, select menus, modals. Talks to the worker via local HTTP calls to its internal API.
- **Worker layer** — Plex API calls, subtitle lookup/extraction, fuzzy quote search, optional Whisper transcription (cached), ffmpeg orchestration. Exposes a small internal REST API (e.g. `/search`, `/resolve-quote`, `/render`).
- **Local config store** — SQLite or a flat file for anything not covered by `.env`/`config.yaml` that needs to persist at runtime (e.g. per-guild allowlists once that's built).
- **Temp processing directory** — scratch space for in-progress renders, cleared aggressively; the `/render` endpoint should stream ffmpeg's output directly rather than always writing a file to disk first.
- **Transcript/subtitle cache** — persisted per Plex media GUID, so a film's subtitles (or a Whisper transcript) are only ever extracted/generated once. This cache doubles as the corpus for library-wide quote search (Section 5) — it's built up lazily/opportunistically as titles get touched by any flow, never by proactively indexing the whole library up front.

No central database, no cloud API, no message broker.

## 2. Discord bot UX

- **Renamed from `/gif` to `/cinesnip`** (decided going into V2). Reasoning: Discord slash commands are namespaced per-application, so there's no actual technical collision risk between bots — but if a server has multiple bots that both register `/gif`, Discord shows a disambiguation picker (bot icon next to the command) before the user can act, which is real friction. `/cinesnip` avoids that and reinforces the product name. Applies everywhere below and to `/gif-diagnose` → `/cinesnip-diagnose` (Section 3).
- `/cinesnip film:<text> quote:<text-optional> timecode:<text-optional> end_timecode:<text-optional>` for films; an equivalent show/season/episode-aware flow for TV (see Section 4). `film` is required — this is the film-first flow: confirm the title, then confirm a timestamp within it (via quote search or direct timecode). `end_timecode` only applies with `timecode` (not `quote`, which always uses the matched line's own span — Section 7) and lets the user pick a custom clip length instead of the fixed default; requests outside `render_defaults.min/max_duration_seconds` get a clear error rather than a silent clamp.
- **`/snip`** — a second, shorter command registered alongside `/cinesnip` with an identical signature and the same underlying handler (`GifCog._generate`); not a Discord "alias" (slash commands don't have those, each name is a fully separate registration) but functionally one. Purely a QOL shortcut, kept in sync with `/cinesnip` by construction since they share one implementation.
- **Timecode input accepts more than `HH:MM:SS`**: colon forms (`1:23:45`, `83`) still work, and so do unit-suffixed forms in any subset of hours/minutes/seconds — `1h22m12s`, `22min 12sec`, `1hr22min2sec`, etc. (`parse_timecode()` in `app/worker/ffmpeg.py`). Applies everywhere a timecode string is accepted (`timecode`, `end_timecode`).
- **`/cinesnip-search quote:<text>`** — a separate, dedicated command for **library-wide** quote search (added in V2, see Section 5 for the indexing/caching design behind it). Deliberately a different command, not a "no film given" fallback on `/cinesnip`: the result shape is different (candidates span many titles, each needs its own title/library/quality label, not just surrounding-line context within one film) and the performance profile is different (see Section 5). Selecting a result from `/cinesnip-search` funnels into the same "confirm the film" → "confirm the timestamp" cheap-redo steps as the normal flow (decision #4) — it's just a different on-ramp into the same two steps, not a separate confirmation UI.
- **Autocomplete on `film`/`show`**: as the user types, query the local Plex library (via the worker's `/search`) and return up to 25 matches — Discord's autocomplete cap.
- **No film-confirmation embed** (see decision #4) — the picked film's title is folded into the "Searching subtitles…"/"Generating…" status message instead of a separate click-gated step. Revisit alongside multi-library search.
- **Quote search results**: top match plus surrounding subtitle context and a confidence indicator; Confirm or "show other matches" (select menu of alternatives) if confidence is borderline.
- **✅ Options step, applied after generation rather than gating it**: select menu of style presets (Classic / Boxed / Cinematic / Meme / Original / No Subtitles) rather than exposing every parameter. Originally specced as a step *before* generating (pick a style, then hit Generate) — real usage showed that added a click to every single command for no benefit when the pre-picked default was already right most of the time. Now: generate immediately with a sensible default (Classic for a quote match, No Subtitles for a bare timecode), show the result, and the same style dropdown sits *below* it — changing it re-renders in place (swaps the attachment on the same message) without restarting the command, and "Post to channel" always posts whatever's currently shown. A modal for advanced overrides (duration/fps/crop) is still a separate, not-yet-built idea (Section 7's interactive clip editor).
- **Progress handling**: acknowledge within Discord's 3-second window with a deferred "Generating…" response, then edit that message once ffmpeg finishes.
- **Visibility**: default ephemeral for the search/confirm/options steps, explicit "Post to channel" button on the final result.

## 3. Plex integration

- **Auth**: Plex's PIN-based flow (request PIN, user approves at plex.tv, poll for token) for a first-run wizard; manual token entry as a fallback.
- **Library access**: `python-plexapi` — mature, covers search, metadata, and media-part resolution for both films and TV episodes.
- **Multiple libraries**: enumerate all Plex sections via the API; search spans all of them by default. Because the same title can exist in more than one library (e.g. Movies and 4K Movies here), the confirmation step must show which library/quality a result came from rather than silently picking one.
- **Two supported Plex-hosting patterns** (document both, let the setup wizard ask which applies):
  - **Plex native on the same host as Docker** (this developer's setup): container reaches Plex via `host.docker.internal:32400` (Docker Desktop, Windows/Mac) — not `network_mode: host`, which doesn't apply on Windows.
  - **Plex itself running in Docker**: container reaches it via Docker's internal networking — shared docker-compose network, or Plex's container name/IP on a shared Docker network.
- **Direct file access vs. Plex stream**: direct file access is strongly preferred for accurate, fast ffmpeg seeking. Requires **per-library path mappings** (not one global mapping), since libraries can live on different drives/folders — e.g. this setup needs four separate mappings (`D:\Plex Additional\Movies`, `E:\Media\Video\Movies`, `D:\Plex Additional\TV Shows`, `E:\Media\Video\TV Shows`), each mounted read-only into the container and each with its own Plex-path → container-path prefix.
- **Windows-specific note**: Docker Desktop's WSL2 backend handles Windows-path bind mounts (`D:\...` → `/media/...`) cleanly; Docker Desktop only shares the `C:` drive by default, so `D:` and `E:` must be explicitly added under Settings → Resources → File Sharing.
- **Stream fallback**: if path mapping isn't configured, request Direct Play (not a transcode) through the Plex API.
- **3D content caveat**: 3D encodes typically store both eyes in a single frame (side-by-side or top/bottom). Naive extraction produces a squished/doubled image — needs explicit handling (crop to one eye) rather than being treated like a normal video file.
- **Diagnostics**: worth building a `/cinesnip-diagnose` (or web-app equivalent) command that reports what paths the container sees vs. what Plex reports, since path-mapping issues are the single most likely install-time failure for other users.
- **✅ Fixed: redundant Plex fetches.** A single `/cinesnip film:X quote:Y` invocation used to re-fetch the same Plex movie metadata via `PlexClient.get_movie()` three separate times (once each in `/resolve`, `/resolve-quote`, `/render` — each a real network round-trip to Plex). Fixed with a short-lived (30s) in-process cache in `PlexClient` keyed by `rating_key` — long enough to dedupe the handful of seconds between one command's own three calls, short enough that a retitled/deleted item doesn't linger. Kept inside the worker (no new fields on the HTTP API), matching the Section 9 principle of keeping media access confined to the worker. Verified against the real library: 3x `get_movie()` calls on the same `rating_key` now hit Plex once, not three times.

## 4. TV show support

- Plex structures TV as Show → Season → Episode; treat this as one extra layer on top of the film flow, not a separate system.
- Two supported patterns:
  - User specifies an episode directly (`/cinesnip The Office S02E01 "that's what she said"`) — same narrow, fast flow as films.
  - User gives a show + quote with no episode — search across that show's episodes for the line. Slower (more subtitle files to check) and should be on-demand only, never pre-indexing an entire show's library proactively.

## 5. Finding dialogue automatically

- **Subtitle source priority** (deliberately generic — must work for installs that don't use Bazarr and/or have only embedded subs, not just this developer's setup):
  1. Look for a separate subtitle file matching the video's filename in the same folder — this covers Bazarr's naming convention (this developer's setup) but is really just "does a sidecar `.srt` exist," which works identically for anyone who drops subtitle files next to their videos by any means, manual or tool-managed. No ffmpeg extraction needed, just read the file.
  2. If none found, check for and extract an embedded subtitle stream (`ffmpeg -map 0:s:N`) — this is the *primary* path for installs without a sidecar-subtitle tool, not a rare fallback, so it needs to be genuinely well-supported in V2, not an afterthought bolted on later.
  3. If neither exists, fall back to local Whisper transcription (`faster-whisper`, **V3** — see Section 13) — genuinely slow (minutes for a full film on CPU, much faster with the 3070 via GPU-enabled build), so **transcribe once per title and cache the result** keyed by the Plex media GUID; never re-run per query. Until V3 ships, a title with neither a sidecar file nor an embedded stream simply isn't quote-searchable — timecode input still works via `/cinesnip`, this is an expected, documented V2 gap, not a bug.
- **Matching**: parse whichever subtitle source into `(start, end, text)` entries; fuzzy-match the typed quote (`rapidfuzz`) after normalizing case/punctuation; return the top candidates (`quote_match.candidate_limit`, 8 by default) with surrounding context and a confidence score rather than committing silently to the top hit.
- **Multiple subtitle tracks**: default to original-language, non-SDH; let the user pick if ambiguous.
- **Realistic expectations**: strong hit rate for well-known lines with a good subtitle track; harder cases include paraphrased quotes, dubbing/translation drift, and lines split across subtitle entries — design the UI around "likely candidates," not guaranteed exact matches.
- **Library-wide quote search (`/cinesnip-search`, V2)**: searches the subtitle cache described in Section 1, not the live filesystem, so its scope is exactly "titles CineSnip has already parsed via any flow" — which starts small and grows with normal use. Two tiers, so a `/cinesnip-search` call never surprises the user with a multi-minute wait:
  1. **Default**: search only already-cached titles. Fast (it's just fuzzy-matching against parsed text already in the cache/DB), returns instantly regardless of library size.
  2. **Explicit opt-in** (a button/flag on the "no more matches in what I've indexed so far" result, not automatic): extend the search to the rest of the library by parsing every not-yet-cached title's subtitles (sidecar → embedded, per the priority above) on the spot. This is the slow path — communicate that plainly in the UI (progress/ETA, not a silent multi-minute hang) — and whatever it parses along the way gets cached, so the corpus is strictly bigger for next time. Never triggered silently; matches the "never pre-indexing proactively" principle already applied to TV search in Section 4, now generalized to the whole library.
  - Results list each candidate's film title, library/quality (Section 3), matched line, and confidence — since results span titles, this label is mandatory here even though it's optional context in the single-film quote search UI.

## 6. Video extraction

- Standard two-step ffmpeg seek: fast `-ss` before `-i` for keyframe positioning, then a short precisely-timed re-encode of just the needed span.
- Direct file access preferred over Plex stream to avoid double-transcoding; Direct Play if falling back to a stream.
- No audio needed for GIF/short-clip output.
- **✅ Subtitle burn-in via ffmpeg's `subtitles`/`ass` filter (libass), not `drawtext`** — gives real font styling/positioning matching the style presets. See Section 7 for the style catalog and build notes.
- **Hardware acceleration (NVENC, via the 3070)**: optional `docker-compose.gpu.yml` override requiring the Nvidia Container Toolkit inside WSL2 — build and test this after the CPU-only path works, not before. Matters most for full-film Whisper transcription speed; the GIF-cut step itself is already fast on CPU alone.
- Typical timing: a few seconds for a 4s/480px clip on CPU; Whisper transcription of a full film is the genuinely slow step, mitigated entirely by caching.
- **✅ Diagnosed: CineSnip's seek is accurate; the reported "sync drift" was always per-title subtitle desync.** Settled definitively against ground truth after two earlier wrong turns — read the method below before re-investigating anything like this.
  - **Seek is proven correct**: a fast `-ss`-before-`-i` seek to a timestamp and a fully-accurate linear decode to that same timestamp (`-vf select='between(t,X,X+0.05)'` from the start of the file) produce **pixel-identical frames**. Confirmed on this project's own library. Nothing to fix in `ffmpeg.py`.
  - **Ground truth for "when is this line actually spoken" is the file's own embedded subtitle stream**, not an audio heuristic and not a player's playback impression. A remux's embedded PGS track was authored against that exact encode, so its cue times are definitionally correct for it: `ffprobe -v error -select_streams s:N -show_entries packet=pts_time -read_intervals "%+90" -of csv=p=0 <file>` (packets pair up as display/clear). A **contact sheet** of the opening is the cheap visual cross-check: `-vf "fps=1/2,scale=320:-2,drawtext=...,tile=4x5"`.
  - **Worked example (Snatch)**: embedded PGS put the first real cue at **34.327s** (frames confirm: 12s is still the Screen Gems logo, 34s is the "A Film by Guy Ritchie" card over Turkish/Tommy). The original sidecar claimed 37.704s (**~3.4s late** — the originally-reported symptom, which was real), a Bazarr "99% match" re-fetch claimed 12.559s (**~22s early**, i.e. over a studio ident), and `alass` corrected it to 34.501s (**within 174ms of ground truth**).
  - **Full-runtime confirmation, not just the opening**: correlating both the alass-fixed file and the Bazarr file against embedded PGS at five points spanning the whole film (5/25/50/75/93 min) showed alass holding within ±0.25s throughout (no drift — a single global shift really was the correct fix) while the Bazarr file held a near-constant ~+22s error throughout. Worth doing before trusting any single-point measurement — a fix (or a bug) that only holds at one timestamp isn't confirmed yet.
  - **✅ The real trap, and the one that actually cost the most time: Plex's client-side "Auto Sync Subtitles" toggle silently caches a computed offset per item, independent of the visible "Subtitles Offset: 0 ms" field, and survives a server restart.** Sequence that played out: Bazarr's bad re-fetch was ~22s early → Plex's auto-sync silently computed and cached ~+22s to compensate → the file *looked* fine in Plex, which is what first made a correct diagnosis look like a false positive → `alass` then fixed the underlying file to true (confirmed: file content, Plex's served copy, and Plex's own duration/timeline all checked out identical and correct by direct HTTP/ffprobe inspection) → Plex kept applying its stale cached +22s on top of the now-correct file → subtitle rendered **~22s late**, the opposite direction from before, on a file that was actually right. The fix is disabling "Auto Sync Subtitles" in the Plex client's playback settings (not the "Subtitles Offset" field, which stays 0 throughout and tells you nothing). **Lesson for next time**: a player's *rendered* timing is not ground truth for whether a sidecar file itself is correct — verify the file directly (embedded-track cross-correlation above, or Plex's raw stream URL fetched and byte-compared to disk) before trusting what any client displays, since a client-side auto-sync feature can mask a bad file or corrupt a good one with no visible indicator either way.
  - **Bazarr's match score is text/hash similarity, not verified timing** — a "99% match" shipped a subtitle 22s out for this exact remux. `alass <video> <bad.srt> <fixed.srt>` fixed it in ~3 minutes on a 26GB remux (it reported a single constant shift, no splits, later confirmed accurate across the full runtime); it also handles non-uniform drift, which `ffsubsync`'s single-offset correction does not. Given this project's library relies on Plex's own auto-sync as a safety net, consider recommending it stay **off** once a title's subtitle is independently verified — it has no way to distinguish "correcting a bad file" from "breaking a good one," and both look identical to it.

## 7. GIF/clip generation & subtitle styles

- Two-pass palette generation (`palettegen`/`paletteuse`) for GIF output; mp4 (`libx264`)/webm (`libvpx-vp9`) are a single-pass encode straight to a scratch file instead (not `pipe:1` — mp4's `+faststart` needs to seek back and rewrite the moov atom after encoding, which a stdout pipe can't do; webm doesn't strictly need this but shares the same code path for simplicity). No audio in any format (`-an`).
- **Every non-GIF encode explicitly sets `-map 0:v:0 -map_chapters -1 -map_metadata -1`** — do not remove these, even though the command "works" without them on many files. Confirmed via real bugs on this project's own library (see build notes below): without `-map 0:v:0`, ffmpeg's default stream selection can silently pull in a subtitle track, which has no fast-seek and can hang; without `-map_chapters -1`, the mp4 muxer copies the source's chapter list into the output as a stray data track regardless of `-map`, leaking the full film's duration into a supposedly-short clip's metadata.
- Defaults: 15fps, 480px width, no crop, subtitles on when triggered by a quote search. **Duration**: a bare timecode uses the fixed `render_defaults.duration_seconds` (4s, silently clamped to `[min_duration_seconds, max_duration_seconds]`, 1s–15s by default); a quote-driven clip uses the matched line's own start/end instead (same silent clamp — a UX nicety, not something the user typed); a timecode **with** an explicit `end_timecode` uses exactly that span, but *rejects* (clear error, not a silent clamp) a span outside `[min_duration_seconds, max_duration_seconds]` — the user chose that span deliberately, so giving them something shorter/longer than asked for without saying so would be worse than just telling them the bounds.
- Auto-downscale resolution/fps if the estimated output would exceed Discord's attachment size limit — much more forgiving with MP4/WebM than GIF, reinforcing the format default in decision #1 above.
- **✅ Style presets** (Discord options step, Section 2): Classic (white, black outline), Boxed (white on solid black box), Cinematic (yellow), Meme (bold caps), Original, No Subtitles. Presets are Python constants in `app/worker/subtitle_render.py`'s `STYLE_PRESETS` (ASS style fields — font, size, colours, outline/box, margins), not `config.yaml` — same precedent as `ffmpeg.py`'s `_VIDEO_CODEC_ARGS` for a multi-field per-preset catalog. **Original doesn't actually mirror source subtitle styling yet** — a plain sidecar/embedded SRT carries no style data to mirror, and embedded ASS/SSA style extraction isn't built (same class of V2 gap as the rest of Section 5); it currently falls back to the same neutral look as Classic. A style requested on a title with no usable subtitles for the clip's own window degrades to a plain render (no burn-in) rather than erroring — the worker echoes what was *actually* used via an `X-Clip-Style` response header (mirrors the existing `X-Clip-Format` pattern) so the bot can tell the user when their choice got silently downgraded.
- **Future: interactive clip editor, not yet built.** `/render`'s explicit `end_timecode` (above) covers picking an exact span up front for a *timecode-driven* clip, and `QuoteMatchView` lets you pick among precomputed quote-match candidates — but there's still no way to *adjust* a clip after seeing it (nudge start/end in small increments once you've got a result, or merge in the next/previous subtitle line for a longer multi-line clip from a quote match). tvgif's UI is a useful reference point (`Previous Sub`/`Next Sub`/`Merge Next Sub`/`Set Num Merged` buttons, time-nudge buttons, format toggles), though CineSnip doesn't need to copy it exactly — worth a real design pass on what's most intuitive here, not a bolt-on to `QuoteMatchView`. This is a bigger scope than picking among precomputed candidates: it needs a stateful edit session and a live preview re-render on each adjustment (cheap per Section 6's "a few seconds for a 4s/480px clip on CPU" timing, but real request volume against the worker). Natural fit alongside decision #6's local web app and Section 2's "modal for advanced overrides" idea — may make more sense there than as a Discord button grid.
- **Future: audio-only clip output, not yet built.** Idea: an audio-only `format` (mp3/ogg) reusing the exact same quote/timecode resolution pipeline, just `-vn` + an audio codec instead of `-an` + a video one — small, self-contained, no new Discord permissions, and works as a normal attachment through the existing flow. Two genuinely different features hide under this idea, though — don't conflate them:
  1. **Audio clip as attachment** — the small version above. No duration cap beyond the existing `render_defaults` bounds.
  2. **Actually populating a server's Discord Soundboard** (`PUT` a sound onto a guild via Discord's Soundboard API, so it shows up in the picker for everyone, not just as a one-off attachment) — a materially bigger feature: hard hard-coded limits unrelated to `render_defaults` (**5.2s max duration, 512KB max file size, mp3/ogg only**), a **new bot permission** (`Create Expressions`) that every existing install would need to re-invite the bot to grant, and a **per-guild sound-count cap** tied to the server's boost level, which raises a real UX question (what happens when the board's full) that nothing else in this project has an analogue for. Needs its own design pass — not a `format:` option, more like a new command.

## 8. Docker design

- Single image, single container to start (Discord bot + worker in one Python process).
- `docker-compose.yml`: env vars for `DISCORD_TOKEN`, `PLEX_URL`, `PLEX_TOKEN`; a config volume; a temp volume/tmpfs for renders, cleared on startup; **one read-only bind mount per media folder** (four, in this setup) rather than a single media mount; clear docs for both Plex-hosting patterns from Section 3.
- `docker-compose.gpu.yml` as an optional override for NVENC/GPU Whisper.
- Install goal: clone repo → copy `.env.example` to `.env` → run the setup wizard (or hand-edit `config.yaml` before the wizard exists) → `docker compose up`.

## 9. Security/privacy

- Plex token and media access stay confined to the worker module even though it's in the same container as the bot, for future auditability if the layers are ever split.
- No inbound public endpoint needed — outbound-only connection to Discord's Gateway.
- Anyone with bot access in a server it's added to can browse the library and generate clips from it — recommend an admin-configurable allowlist (roles/users permitted to use the bot) and basic rate-limiting.
- **Disable "Public Bot" in the Discord Developer Portal (Bot → Authorization Flow).** This is a self-hosted, single-owner bot tied to one person's Plex library (Section 10's "one Docker install = one Plex owner = one bot application" model) — it must never be self-service-invitable by a stranger clicking "Add to Server" on the bot's profile, since that would hand them browse/generate access to this installer's personal media. With Public Bot off, only the application owner can generate a working OAuth2 invite URL; the owner can still invite it to as many servers as they choose (Section 10 is unaffected), it just stops anyone else from doing so. ✅ Documented in the README as step one of Discord setup; also fold into the Section 14 wizard's Discord step once that's built. Combine with the allowlist above as defense-in-depth: Public Bot off stops unwanted *invites*, the allowlist limits misuse *within* servers the owner did invite it to.

## 10. Multiple servers/users — v1 model

One Docker install = one Plex owner = one bot application, invited to whichever Discord server(s) chosen. All users in those servers share access through the bot. Single global admin allowlist for v1; skip per-server/per-user permission granularity.

## 11. Hosting model

Bot + processing run entirely on the installer's own machine — no centrally-hosted variant. A shared/hosted option would reintroduce the third-party data exposure this project exists to avoid, plus ongoing maintenance burden, for no real benefit.

## 12. Recommended stack

**Python**: `discord.py`/`Pycord` for the bot layer, `python-plexapi` for Plex, `subprocess` calls to the ffmpeg CLI directly, `faster-whisper` for optional transcription, `rapidfuzz` for subtitle matching, FastAPI for the internal worker API, SQLite for the small config/cache store.

## 13. Staged development plan

**MVP — ✅ complete**
- Single `/gif` command (renamed `/cinesnip` going into V2 — see Section 2), timecode input only, films only
- Plex search + confirm via buttons (single library/mapping to start — this developer's primary Movies library)
- Direct file access only
- One fixed GIF style, no options menu
- One Docker container, tested against one Discord server

See "MVP build notes" below before touching `app/worker/ffmpeg.py` — the
quote-search rendering path reuses the same seek/duration logic.

**V2**
- ✅ Rename `/gif` → `/cinesnip` (and `/gif-diagnose` → `/cinesnip-diagnose`)
- ✅ Sidecar-subtitle-file + embedded-subtitle-stream extraction (generic — not Bazarr-specific, see Section 5) + fuzzy quote search within a confirmed film
- `/cinesnip-search` — library-wide quote search across the (lazily-built) subtitle cache, with an explicit opt-in to extend a search into not-yet-cached titles (Section 5)
- ✅ Subtitle burn-in + style preset select menu (Section 7)
- ✅ MP4/WebM output option (`format:` on `/cinesnip`/`/snip`, default gif — see decision #1)
- Remaining library/drive path mappings (4K, 3D, TV — both Movies drives are done)
- ✅ Document + confirm "Public Bot" disabled as part of Discord setup (Section 9)

**V3**
- TV show support (episode-specific and whole-show search)
- Whisper fallback (cached, lazy)
- NVENC/GPU hardware acceleration option
- Local web app: setup wizard + manual generation UI (see Section 14 for the onboarding wizard spec)
- Allowlists/rate-limiting, multi-server polish, distribution docs for other self-hosters (both Plex-hosting patterns documented)

### MVP build notes (non-obvious bugs — read before touching `app/worker/ffmpeg.py`)

- **ffmpeg `-t` must be an input option, not an output option.** Placing
  `-t <duration>` *after* `-i` only bounds the output stream's
  timestamps — which does nothing for filters like `palettegen`/
  `paletteuse` that emit a single frame at the very end of the filter
  graph. With `-t` as an output option, ffmpeg keeps decoding and feeding
  frames into the filter for the rest of the file, since there's no
  rolling output PTS for it to cut off against. Fix: put `-ss` and `-t`
  **together, both before `-i`** — as input options, `-t` bounds how much
  is actually *read*, which is what stops it. See `app/worker/ffmpeg.py`'s
  `build_seek_args()` and its docstring/regression test.
- **The `image2` muxer needs `-update 1`** to write a single still image
  (e.g. the GIF palette PNG) rather than expecting a `%d` sequence pattern.
- **Always drain both `stdout` and `stderr`** from an ffmpeg subprocess
  (`communicate()`, not a manual read loop on one pipe) — ffmpeg writes
  verbose progress to `stderr`, and reading only `stdout` risks a
  deadlock once `stderr`'s OS pipe buffer fills and ffmpeg blocks trying
  to write to it.
- **Always wrap ffmpeg subprocess calls in a timeout that kills the
  process** on expiry. A pathological or unusually slow-to-seek source
  file should fail cleanly with a clear error, never hang the request
  indefinitely.
- **Docker auto-creates missing bind-mount source directories as
  `root`.** The scratch/render temp dir (`./scratch:/app/scratch` in
  `docker-compose.yml`) needs to exist and be owned by the same UID as
  the container's non-root user *before* first `docker compose up`,
  or the container can't write to it. Documented as an explicit setup
  step in `README.md` and the troubleshooting section.

### V2 subtitle-extraction build notes (non-obvious bugs — read before touching `app/worker/subtitles.py`)

- **✅ Fixed: the subtitle cache used to never invalidate when the sidecar
  changed on disk.** `get_subtitles()` used to key the cache purely on the
  Plex media GUID, so a later edit or replacement of a title's `.srt`
  (or, for the embedded path, the video itself) was invisible to CineSnip
  **forever** — this is not theoretical, it bit this project twice in one
  session (a Bazarr re-fetch, then an `alass` re-sync of the same file),
  each time needing the cache file deleted by hand, and it actively
  confused the Section 6 sync diagnosis. Fixed by recording the relevant
  source file's `(mtime, size)` fingerprint in the cached payload —
  `_fingerprint()` in `app/worker/subtitles.py` — and re-extracting on
  mismatch or on removal. `read_cached_subtitles()`/`write_cached_subtitles()`
  take the *current* sidecar/video path candidates; which one actually
  needs to be fresh is decided by the cached payload's own recorded source
  (`SIDECAR` checks the sidecar, `EMBEDDED` checks the video), so an
  unrelated file appearing doesn't wrongly invalidate a good cache entry.
  `SubtitleSource.NONE` results still cache without a freshness check (no
  single file backs a "no subtitles" result) — a title getting a sidecar
  for the first time after being cached as NONE is a known, narrower,
  unhandled case; workaround is still manual cache deletion for that one
  scenario.
- **Bazarr sidecar filenames chain multiple dot-separated markers, not
  just a language code** — e.g. `Film.en.hi.srt` (English,
  hearing-impaired/SDH) as a distinct file from a plain `Film.en.srt`.
  Treating the whole `en.hi` suffix as an opaque language string breaks
  the "prefer English" match and, worse, can let alphabetical tie-breaking
  silently prefer the `.hi.srt` (SDH) file over a plain one that also
  exists, contradicting the non-SDH-by-default design in Section 5. Fix:
  parse only the *first* dot-separated segment as the language code, and
  treat any later segment matching `hi`/`sdh`/`cc` as a hearing-impaired
  flag — prefer non-hearing-impaired sidecars when both exist, same
  priority the embedded-stream heuristic already applies. See
  `app/worker/subtitles.py`'s `_parse_sidecar_suffix()`.
- **Embedded-subtitle extraction (`ffmpeg -map 0:s:N -f srt`) has no
  equivalent of clip rendering's `-ss` fast seek** — it must read
  sequentially through the whole container to demux the subtitle packets,
  even though the actual subtitle data is tiny. On this environment's
  WSL2-bridged NTFS drives, extracting from an 11GB remux took ~73
  seconds — comfortably past a naive 30s timeout, even though nothing was
  actually hung; the *default value* was just wrong for real file sizes
  over this I/O path. Fixed by making it a configurable
  `subtitle_defaults.extraction_timeout_seconds` (default 180s) in
  `config.yaml`, mirroring how `render_defaults.timeout_seconds` already
  works — raise it further if you see timeouts on very large files. (Any
  new endpoint that triggers extraction on a cold cache — e.g.
  `/resolve-quote` — needs its own client-side timeout set higher than
  this value, or the client aborts before the worker's clean error does;
  see `RESOLVE_QUOTE_TIMEOUT_SECONDS` in `app/bot/worker_client.py`.)
- **PGS/bitmap subtitle streams (`hdmv_pgs_subtitle`) are bitmap, not
  text — ffmpeg can't mux them to SRT.** `choose_subtitle_stream()`
  filters these out; a title with only bitmap embedded streams and no
  sidecar correctly resolves to "no subtitles available" rather than a
  garbage extraction. This is the documented V2 gap in Section 5, not a
  bug — don't try to "fix" a title that hits this path.

### V2 MP4/WebM output build notes (real bugs — read before touching `_render_video` in `app/worker/ffmpeg.py`)

- **Without explicit `-map 0:v:0`, ffmpeg's default stream selection can
  silently include a source subtitle track in the output — and demuxing
  it has no fast-seek, so it can hang the whole render.** Reproduced on
  this project's own library: a webm request for a title with a forced
  German subtitle track hung past the 60s render timeout; the *video*
  portion actually finished in under a second, but ffmpeg kept blocking
  trying to read subtitle packets across the whole remaining file before
  it would finalize the output. mp4 didn't reproduce the *hang* on this
  same file (its muxer's default stream selection is less willing to
  auto-include a subtitle track than webm/Matroska's is) — but relying on
  that container-specific behavior would be fragile, so `-map 0:v:0` is
  unconditional for every non-GIF encode, not just webm.
- **`-map 0:v:0` alone doesn't stop the mp4 muxer copying the source's
  chapter list into the output.** Chapters aren't a "stream" `-map`
  controls, so even with clean video-only stream mapping, a 2-4 second
  mp4 clip request still produced a second "data" track whose duration
  matched the *source film's* full runtime — harmless in that it didn't
  hang (chapter metadata is cheap to copy, unlike an actual subtitle
  stream), but real, confirmed output pollution: it leaked the full
  film's duration into a clip that should have no idea how long its
  source is. Fixed with `-map_chapters -1 -map_metadata -1` alongside
  `-map 0:v:0` — the latter also strips title/encoder tags so the output
  file doesn't carry the source film's metadata either. Verify any future
  change to this ffmpeg command with `ffprobe -show_streams` on the
  actual output, not just "did it complete without erroring" — both bugs
  here produced a file that looked superficially fine (non-empty,
  playable) while carrying data it shouldn't have.

### V2 subtitle burn-in build notes (real bugs — read before touching `app/worker/subtitle_render.py` or the container's font setup)

- **The base `python:3.12-slim-bookworm` image has exactly one font family
  (DejaVu) — neither "Arial" nor "Impact" (the fonts the style presets
  originally asked for) are actually installed.** libass doesn't error on
  an unresolvable font name, it silently substitutes via fontconfig — and
  that substitution isn't necessarily even the right *kind* of font:
  confirmed on this project's own container, `fc-match Impact` resolved to
  **a monospace face**, which would have rendered the "Meme" preset in a
  typewriter font with no error or warning anywhere. Fixed by installing
  `fonts-liberation` in the Dockerfile (Liberation Sans is a metric-compatible
  Arial substitute with the fontconfig alias already wired up) and pointing
  every preset directly at the real installed family name (`"Liberation
  Sans"`) rather than a name that only resolves via an alias — verified via
  `fc-match`/`fc-list` inside the built container, then confirmed visually
  (see below). No true Impact-alike is installed (proprietary, not in
  Debian's repos); Meme uses Liberation Sans + bold + all-caps instead.
- **ASS alpha is inverted from the usual convention — `00` is fully opaque,
  `FF` is fully transparent.** The Boxed preset's first cut used
  `&H80000000` (roughly 50%) expecting a visibly translucent box; against
  real footage with a light background it was nearly invisible in a
  rendered test frame. Fixed by using `&H00000000` (fully opaque) for a
  real "black box," matching what the preset name promises.
- **Burned-in text size depends on `PlayResX`/`PlayResY` matching the
  clip's actual output frame size, not the source file's.** libass scales
  a style's font size/margins relative to these two fields; get them wrong
  and the text is the wrong size for the frame, not just mispositioned.
  `_write_ass_file()` in `app/worker/ffmpeg.py` probes the source's
  width/height via a new `probe_video_dimensions()` ffprobe call and
  computes the *actual* scaled output height itself — which required
  switching the scale filter from `scale={width}:-1` to `scale={width}:-2`
  (guarantees an even height) so the manual calculation and ffmpeg's own
  scaling agree. Do not reintroduce `-1` here without re-deriving this.
- **Verification for this feature meant actually looking at rendered
  pixels, not just checking the render succeeded.** Both bugs above
  (wrong font, invisible box) produced a 200 response and a playable file
  — "it rendered" told us nothing. Caught by rendering real quote-driven
  clips against this project's own library at a known subtitle line,
  extracting a still frame (`ffmpeg -update 1 -frames:v 1 out.png` — same
  `image2` muxer gotcha as the MVP's palette-PNG step, see MVP build notes
  above), and visually inspecting it. Repeat this check for any future
  change to `STYLE_PRESETS` or the ASS-building code — a `ffprobe` pass
  can't catch "the text is the wrong font/color/size," only a rendered
  frame can.

## 14. Onboarding Wizard (Setup UX)

**Why this matters**: the MVP's setup path (README steps: hand-create a
Discord app, hand-fetch a Plex token, hand-edit `.env`/`config.yaml`,
`docker compose up`) is fine for this developer, but is a real barrier for
a less technical person installing CineSnip fresh from GitHub. The whole
point of building this as a distributable self-hosted tool (see Project
summary) is undermined if only technical users can actually get it running.
This section is the spec for the `/setup` route of the local web app named
in decision #6 — not a new component, the detailed design for one already
planned.

### Goal

Someone who has never used a terminal beyond `docker compose up` should be
able to go from "cloned the repo" to "bot working in my server" by
following an on-screen wizard — no hand-editing YAML, no hunting through
Plex's UI for a token via undocumented tricks, no manually reasoning about
Docker path mappings.

### Flow

1. **First-run detection**: if `.env`/`config.yaml` are missing or
   incomplete at container startup, serve *only* the setup wizard on a
   local port (not the Discord bot or the full worker API) until setup is
   validated complete. This reuses the "fail fast with actionable errors"
   behavior already in `app/settings.py` — the wizard is the friendly
   front-end for exactly the same validation.
2. **Discord step**: inline instructions (with links, not just prose) for
   creating the application in the Developer Portal, enabling the bot,
   **confirming "Public Bot" is disabled** (Bot → Authorization Flow — see
   Section 9; this is a single-owner bot tied to one person's Plex library
   and must not be self-service-invitable by strangers), generating the
   invite URL with the right scopes/permissions, and inviting it to a
   server. A single token input field, masked like a password field. The
   wizard validates the token live (attempt a login) before saving, and
   shows a clear pass/fail — don't let someone finish setup with a token
   that doesn't actually work.
3. **Plex step**: **lead with the PIN-based auth flow already specced in
   Section 3** (request PIN, user approves at plex.tv, wizard polls for
   the token automatically) rather than manually viewing Plex's XML API
   response for a token — that trick is unintuitive, and Plex's web UI
   "sign out" doesn't reliably invalidate/rotate a token, which makes
   manual token hygiene confusing even for a technical user. A
   wizard-driven PIN flow sidesteps both problems. Manual token paste
   stays as an advanced-user fallback only.
4. **Library/path-mapping step**: once Plex is authenticated, auto-enumerate
   libraries via the API and let the user pick which one(s) to enable.
   For each, auto-suggest path mappings by comparing the Plex-reported
   file path (from a sample media item) against what's actually visible
   under the container's mounted volumes — effectively automating the
   `/gif-diagnose` diagnostic from Section 3 into the setup flow itself,
   instead of requiring the user to manually read XML and hand-edit YAML.
5. **Validation step**: before declaring setup complete, confirm: Plex is
   reachable, at least one sample file resolves through the chosen path
   mapping and actually exists on disk from the container's point of view,
   and the Discord bot can log in. Only then start the real bot/worker.
6. Manual `.env`/`config.yaml` editing (the current README flow) stays
   documented as the advanced/fallback path for technical users who'd
   rather skip the UI — the wizard doesn't replace it, it's the
   friendlier default for everyone else.

### Security requirements (non-negotiable, not just nice-to-have)

- Tokens are written **directly to local `.env`/`config.yaml` on disk by
  the wizard's own backend** — never transmitted anywhere external, never
  logged, never included in any error reporting (there is none — no
  telemetry exists or should exist in this project).
- The wizard's web UI **binds to localhost only by default**; exposing it
  on the LAN is an explicit opt-in, never the default, since it's handling
  raw secrets during setup.
- Token input fields are masked in the UI, and **the wizard's own
  backend must never log full request/response bodies** for the
  token-submission endpoints, even in debug/verbose logging modes — a
  logged request body is just as much a leak as printing the token to the
  UI. This is a real, concrete risk: a coding assistant's own
  file-change-tracking can echo a previously-touched file's full contents
  (e.g. a live `.env`) back into its transcript as a side effect of having
  touched that path before, with no code in this project doing so
  intentionally — design the wizard so no code path (logging, error
  handling, debug tooling) ever has a reason to echo a submitted token
  back out anywhere other than the config file it belongs in.
- Once written, tokens are read from `.env`/`config.yaml` exactly like
  `app/settings.py` already does — the wizard is purely a friendlier way
  to produce those same two files, not a new runtime secret-handling path.
