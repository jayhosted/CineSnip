# Plex GIF/Clip Discord Bot — Technical Architecture & Development Plan
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

## Workflow decisions already made (apply these, don't re-litigate)

1. **Output format defaults to MP4/WebM, not GIF.** Discord renders both inline like a GIF, at much smaller file sizes for the same quality. Offer GIF as an explicit `format:gif` option, not the default.
2. **One container, one process to start.** Discord bot layer and worker layer (Plex, subtitles, ffmpeg) run in a single Python process inside a single Docker container. No message queue or microservices for v1.
3. **Structure the worker as a small internal HTTP API from the start** (e.g. FastAPI), even though the Discord bot is its only client initially. This makes a future local web app (setup wizard + manual GIF-generation UI) a thin second client rather than a rebuild.
4. **"Confirm the film/show" and "confirm the timestamp" are separate, cheap-to-redo steps** in the Discord flow — users can back up one step without restarting `/gif` from scratch.
5. **Config split**: `.env` for secrets (Discord bot token, Plex token), `config.yaml` for everything else (per-library path mappings, style presets, GPU toggle, feature flags). Standard self-hosted-app practice — keeps secrets out of anything that might get shared or hand-edited casually.
6. **Setup wizard and manual-generation UI are the same small local web app**, not two separate builds — a `/setup` route for first-run/reconfiguration, a `/generate` route for browsing/making clips outside Discord. Build the Discord bot and prove the core pipeline first; build this web app as a fast-follow, not alongside.

## 1. Overall architecture

- **Discord layer** — Gateway connection, slash commands, buttons, select menus, modals. Talks to the worker via local HTTP calls to its internal API.
- **Worker layer** — Plex API calls, subtitle lookup/extraction, fuzzy quote search, optional Whisper transcription (cached), ffmpeg orchestration. Exposes a small internal REST API (e.g. `/search`, `/resolve-quote`, `/render`).
- **Local config store** — SQLite or a flat file for anything not covered by `.env`/`config.yaml` that needs to persist at runtime (e.g. per-guild allowlists once that's built).
- **Temp processing directory** — scratch space for in-progress renders, cleared aggressively; the `/render` endpoint should stream ffmpeg's output directly rather than always writing a file to disk first.
- **Transcript/subtitle cache** — persisted per Plex media GUID, so a film's subtitles (or a Whisper transcript) are only ever extracted/generated once.

No central database, no cloud API, no message broker.

## 2. Discord bot UX

- `/gif film:<text> quote:<text-optional> timecode:<text-optional>` for films; an equivalent show/season/episode-aware flow for TV (see Section 4).
- **Autocomplete on `film`/`show`**: as the user types, query the local Plex library (via the worker's `/search`) and return up to 25 matches — Discord's autocomplete cap.
- **Confirmation embed**: poster thumbnail, title, year, runtime, and — since multiple libraries can hold the same title — which library/quality it's from, with Confirm/Cancel buttons.
- **Quote search results**: top match plus surrounding subtitle context and a confidence indicator; Confirm or "show other matches" (select menu of alternatives) if confidence is borderline.
- **Options step**: select menu of style presets (Classic / Boxed / Cinematic / Meme / Original / No Subtitles) rather than exposing every parameter; a modal for advanced overrides (duration/fps/crop/format).
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
- **Diagnostics**: worth building a `/gif-diagnose` (or web-app equivalent) command that reports what paths the container sees vs. what Plex reports, since path-mapping issues are the single most likely install-time failure for other users.

## 4. TV show support

- Plex structures TV as Show → Season → Episode; treat this as one extra layer on top of the film flow, not a separate system.
- Two supported patterns:
  - User specifies an episode directly (`/gif The Office S02E01 "that's what she said"`) — same narrow, fast flow as films.
  - User gives a show + quote with no episode — search across that show's episodes for the line. Slower (more subtitle files to check) and should be on-demand only, never pre-indexing an entire show's library proactively.

## 5. Finding dialogue automatically

- **Subtitle source priority** (matches this Bazarr-based setup, and is the sensible default generally):
  1. Look for a separate subtitle file matching the video's filename in the same folder (Bazarr's naming convention is predictable) — no ffmpeg extraction needed, just read the file.
  2. If none found, check for and extract an embedded subtitle stream (`ffmpeg -map 0:s:N`).
  3. If neither exists, fall back to local Whisper transcription (`faster-whisper`) — genuinely slow (minutes for a full film on CPU, much faster with the 3070 via GPU-enabled build), so **transcribe once per title and cache the result** keyed by the Plex media GUID; never re-run per query.
- **Matching**: parse whichever subtitle source into `(start, end, text)` entries; fuzzy-match the typed quote (`rapidfuzz`) after normalizing case/punctuation; return top 2–3 candidates with surrounding context and a confidence score rather than committing silently to the top hit.
- **Multiple subtitle tracks**: default to original-language, non-SDH; let the user pick if ambiguous.
- **Realistic expectations**: strong hit rate for well-known lines with a good subtitle track; harder cases include paraphrased quotes, dubbing/translation drift, and lines split across subtitle entries — design the UI around "likely candidates," not guaranteed exact matches.

## 6. Video extraction

- Standard two-step ffmpeg seek: fast `-ss` before `-i` for keyframe positioning, then a short precisely-timed re-encode of just the needed span.
- Direct file access preferred over Plex stream to avoid double-transcoding; Direct Play if falling back to a stream.
- No audio needed for GIF/short-clip output.
- Subtitle burn-in via ffmpeg's `subtitles`/`ass` filter (libass), not `drawtext` — gives real font styling/positioning matching the style presets.
- **Hardware acceleration (NVENC, via the 3070)**: optional `docker-compose.gpu.yml` override requiring the Nvidia Container Toolkit inside WSL2 — build and test this after the CPU-only path works, not before. Matters most for full-film Whisper transcription speed; the GIF-cut step itself is already fast on CPU alone.
- Typical timing: a few seconds for a 4s/480px clip on CPU; Whisper transcription of a full film is the genuinely slow step, mitigated entirely by caching.

## 7. GIF/clip generation & subtitle styles

- Two-pass palette generation (`palettegen`/`paletteuse`) for actual GIF output.
- Defaults: 4s duration, 15fps, 480px width, no crop, subtitles on when triggered by a quote search.
- Auto-downscale resolution/fps if the estimated output would exceed Discord's attachment size limit — much more forgiving with MP4/WebM than GIF, reinforcing the format default in decision #1 above.
- Style presets: Classic (white/black outline), Boxed (white on black box), Cinematic (yellow), Meme (large bold caps), Original (mirrors source subtitle styling).

## 8. Docker design

- Single image, single container to start (Discord bot + worker in one Python process).
- `docker-compose.yml`: env vars for `DISCORD_TOKEN`, `PLEX_URL`, `PLEX_TOKEN`; a config volume; a temp volume/tmpfs for renders, cleared on startup; **one read-only bind mount per media folder** (four, in this setup) rather than a single media mount; clear docs for both Plex-hosting patterns from Section 3.
- `docker-compose.gpu.yml` as an optional override for NVENC/GPU Whisper.
- Install goal: clone repo → copy `.env.example` to `.env` → run the setup wizard (or hand-edit `config.yaml` before the wizard exists) → `docker compose up`.

## 9. Security/privacy

- Plex token and media access stay confined to the worker module even though it's in the same container as the bot, for future auditability if the layers are ever split.
- No inbound public endpoint needed — outbound-only connection to Discord's Gateway.
- Anyone with bot access in a server it's added to can browse the library and generate clips from it — recommend an admin-configurable allowlist (roles/users permitted to use the bot) and basic rate-limiting.

## 10. Multiple servers/users — v1 model

One Docker install = one Plex owner = one bot application, invited to whichever Discord server(s) chosen. All users in those servers share access through the bot. Single global admin allowlist for v1; skip per-server/per-user permission granularity.

## 11. Hosting model

Only Option A (bot + processing entirely on the installer's own machine) fits the stated privacy goal and is the only one worth building for — see prior discussion for why centrally-hosted variants don't actually reduce exposure and add ongoing maintenance burden.

## 12. Costs

Effectively $0 for the end user beyond hardware they likely already own to run Plex: Discord, Plex, python-plexapi, ffmpeg, and Docker are all free; local Whisper costs only electricity; storage/bandwidth is negligible (temp files plus a small transcript cache).

## 13. Recommended stack

**Python**: `discord.py`/`Pycord` for the bot layer, `python-plexapi` for Plex, `subprocess` calls to the ffmpeg CLI directly, `faster-whisper` for optional transcription, `rapidfuzz` for subtitle matching, FastAPI for the internal worker API, SQLite for the small config/cache store.

## 14. Staged development plan

**MVP**
- Single `/gif` command, timecode input only, films only
- Plex search + confirm via buttons (single library/mapping to start — this developer's primary Movies library)
- Direct file access only
- One fixed GIF style, no options menu
- One Docker container, tested against one Discord server
- Goal: prove the full pipeline end-to-end

**V2**
- Bazarr-first subtitle extraction + fuzzy quote search
- Style preset select menu, MP4/WebM output option
- Remaining library/drive path mappings (4K, 3D, TV, second drive)

**V3**
- TV show support (episode-specific and whole-show search)
- Whisper fallback (cached, lazy)
- NVENC/GPU hardware acceleration option
- Local web app: setup wizard + manual generation UI
- Allowlists/rate-limiting, multi-server polish, distribution docs for other self-hosters (both Plex-hosting patterns documented)

## 15. Honest difficulty note

This spans async programming, Docker networking across host/container/WSL2 boundaries, third-party API auth, and optionally local ML inference — genuinely multi-domain, not a first project. Given hands-on experience with Discord bots/webhooks, Docker, and Plex already, the ops-side debugging (Docker networking, Plex connectivity, testing) should feel familiar; the main friction point will be application-logic bugs surfaced through Claude Code's own error output, which is a normal and manageable iterative loop, not a blocker. Build and prove the MVP before adding TV support, Whisper, or the web app — each of those is a real, separable stage, not a detail to bolt on early.
