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
- **Confirmed working (2026-08-25)**: a Claude Code session in this WSL2 distro has real, direct access — `.env`/`config.yaml` are already populated with live values (not just the `.example` templates), both media drives are mounted and browsable at `/mnt/d/Plex Additional/Movies` and `/mnt/e/Media/Video/Movies`, and `docker compose` can build/run the real container against the real Plex library. This means new worker-layer features should be verified end-to-end against real media during development, not just unit-tested — that's how the V2 subtitle-extraction work below caught two real bugs synthetic test data never would have.
- **Docker group gotcha**: the WSL2 user (`jaypw`) is not always in the `docker` group and there's no passwordless `sudo`, so a fresh session may get "permission denied" talking to the Docker daemon. Fix: have the user run `sudo usermod -aG docker jaypw` themselves (needs their password, so Claude Code can't run it directly) — the new group membership then works immediately in the *same* session via `sg docker -c '<command>'`, without needing to restart the whole session.
- **The worker API is loopback-only inside the container by design** (Section 9 — no inbound public endpoint). To manually hit a worker endpoint (e.g. the `/subtitles/{rating_key}` diagnostic route) from a dev session, use `docker compose exec <service> <command>` to run the request *from inside* the container's network namespace — a plain `curl` from the host/WSL2 side won't reach it, since no port is published in `docker-compose.yml`.

## Workflow decisions already made (apply these, don't re-litigate)

1. **Output format defaults to MP4/WebM, not GIF.** Discord renders both inline like a GIF, at much smaller file sizes for the same quality. Offer GIF as an explicit `format:gif` option, not the default.
2. **One container, one process to start.** Discord bot layer and worker layer (Plex, subtitles, ffmpeg) run in a single Python process inside a single Docker container. No message queue or microservices for v1.
3. **Structure the worker as a small internal HTTP API from the start** (e.g. FastAPI), even though the Discord bot is its only client initially. This makes a future local web app (setup wizard + manual GIF-generation UI) a thin second client rather than a rebuild.
4. **"Confirm the film/show" and "confirm the timestamp" are separate, cheap-to-redo steps** in the Discord flow — users can back up one step without restarting `/cinesnip` from scratch.
5. **Config split**: `.env` for secrets (Discord bot token, Plex token), `config.yaml` for everything else (per-library path mappings, style presets, GPU toggle, feature flags). Standard self-hosted-app practice — keeps secrets out of anything that might get shared or hand-edited casually.
6. **Setup wizard and manual-generation UI are the same small local web app**, not two separate builds — a `/setup` route for first-run/reconfiguration, a `/generate` route for browsing/making clips outside Discord. Build the Discord bot and prove the core pipeline first; build this web app as a fast-follow, not alongside. Full spec for the wizard itself: see Section 16.

## 1. Overall architecture

- **Discord layer** — Gateway connection, slash commands, buttons, select menus, modals. Talks to the worker via local HTTP calls to its internal API.
- **Worker layer** — Plex API calls, subtitle lookup/extraction, fuzzy quote search, optional Whisper transcription (cached), ffmpeg orchestration. Exposes a small internal REST API (e.g. `/search`, `/resolve-quote`, `/render`).
- **Local config store** — SQLite or a flat file for anything not covered by `.env`/`config.yaml` that needs to persist at runtime (e.g. per-guild allowlists once that's built).
- **Temp processing directory** — scratch space for in-progress renders, cleared aggressively; the `/render` endpoint should stream ffmpeg's output directly rather than always writing a file to disk first.
- **Transcript/subtitle cache** — persisted per Plex media GUID, so a film's subtitles (or a Whisper transcript) are only ever extracted/generated once. This cache doubles as the corpus for library-wide quote search (Section 5) — it's built up lazily/opportunistically as titles get touched by any flow, never by proactively indexing the whole library up front.

No central database, no cloud API, no message broker.

## 2. Discord bot UX

- **Renamed from `/gif` to `/cinesnip`** (decided going into V2). Reasoning: Discord slash commands are namespaced per-application, so there's no actual technical collision risk between bots — but if a server has multiple bots that both register `/gif`, Discord shows a disambiguation picker (bot icon next to the command) before the user can act, which is real friction. `/cinesnip` avoids that and reinforces the product name. Applies everywhere below and to `/gif-diagnose` → `/cinesnip-diagnose` (Section 3).
- `/cinesnip film:<text> quote:<text-optional> timecode:<text-optional>` for films; an equivalent show/season/episode-aware flow for TV (see Section 4). `film` is required — this is the film-first flow: confirm the title, then confirm a timestamp within it (via quote search or direct timecode).
- **`/cinesnip-search quote:<text>`** — a separate, dedicated command for **library-wide** quote search (added in V2, see Section 5 for the indexing/caching design behind it). Deliberately a different command, not a "no film given" fallback on `/cinesnip`: the result shape is different (candidates span many titles, each needs its own title/library/quality label, not just surrounding-line context within one film) and the performance profile is different (see Section 5). Selecting a result from `/cinesnip-search` funnels into the same "confirm the film" → "confirm the timestamp" cheap-redo steps as the normal flow (decision #4) — it's just a different on-ramp into the same two steps, not a separate confirmation UI.
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
- **Diagnostics**: worth building a `/cinesnip-diagnose` (or web-app equivalent) command that reports what paths the container sees vs. what Plex reports, since path-mapping issues are the single most likely install-time failure for other users.

## 4. TV show support

- Plex structures TV as Show → Season → Episode; treat this as one extra layer on top of the film flow, not a separate system.
- Two supported patterns:
  - User specifies an episode directly (`/cinesnip The Office S02E01 "that's what she said"`) — same narrow, fast flow as films.
  - User gives a show + quote with no episode — search across that show's episodes for the line. Slower (more subtitle files to check) and should be on-demand only, never pre-indexing an entire show's library proactively.

## 5. Finding dialogue automatically

- **Subtitle source priority** (deliberately generic — must work for installs that don't use Bazarr and/or have only embedded subs, not just this developer's setup):
  1. Look for a separate subtitle file matching the video's filename in the same folder — this covers Bazarr's naming convention (this developer's setup) but is really just "does a sidecar `.srt` exist," which works identically for anyone who drops subtitle files next to their videos by any means, manual or tool-managed. No ffmpeg extraction needed, just read the file.
  2. If none found, check for and extract an embedded subtitle stream (`ffmpeg -map 0:s:N`) — this is the *primary* path for installs without a sidecar-subtitle tool, not a rare fallback, so it needs to be genuinely well-supported in V2, not an afterthought bolted on later.
  3. If neither exists, fall back to local Whisper transcription (`faster-whisper`, **V3** — see Section 14) — genuinely slow (minutes for a full film on CPU, much faster with the 3070 via GPU-enabled build), so **transcribe once per title and cache the result** keyed by the Plex media GUID; never re-run per query. Until V3 ships, a title with neither a sidecar file nor an embedded stream simply isn't quote-searchable — timecode input still works via `/cinesnip`, this is an expected, documented V2 gap, not a bug.
- **Matching**: parse whichever subtitle source into `(start, end, text)` entries; fuzzy-match the typed quote (`rapidfuzz`) after normalizing case/punctuation; return top 2–3 candidates with surrounding context and a confidence score rather than committing silently to the top hit.
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
- **Disable "Public Bot" in the Discord Developer Portal (Bot → Authorization Flow).** This is a self-hosted, single-owner bot tied to one person's Plex library (Section 10's "one Docker install = one Plex owner = one bot application" model) — it must never be self-service-invitable by a stranger clicking "Add to Server" on the bot's profile, since that would hand them browse/generate access to this installer's personal media. With Public Bot off, only the application owner can generate a working OAuth2 invite URL; the owner can still invite it to as many servers as they choose (Section 10 is unaffected), it just stops anyone else from doing so. This is cheap, one-click, and should be step one of the Discord setup — documented in the README today and folded into the Section 16 wizard's Discord step once that's built. Combine with the allowlist above as defense-in-depth: Public Bot off stops unwanted *invites*, the allowlist limits misuse *within* servers the owner did invite it to.

## 10. Multiple servers/users — v1 model

One Docker install = one Plex owner = one bot application, invited to whichever Discord server(s) chosen. All users in those servers share access through the bot. Single global admin allowlist for v1; skip per-server/per-user permission granularity.

## 11. Hosting model

Only Option A (bot + processing entirely on the installer's own machine) fits the stated privacy goal and is the only one worth building for — see prior discussion for why centrally-hosted variants don't actually reduce exposure and add ongoing maintenance burden.

## 12. Costs

Effectively $0 for the end user beyond hardware they likely already own to run Plex: Discord, Plex, python-plexapi, ffmpeg, and Docker are all free; local Whisper costs only electricity; storage/bandwidth is negligible (temp files plus a small transcript cache).

## 13. Recommended stack

**Python**: `discord.py`/`Pycord` for the bot layer, `python-plexapi` for Plex, `subprocess` calls to the ffmpeg CLI directly, `faster-whisper` for optional transcription, `rapidfuzz` for subtitle matching, FastAPI for the internal worker API, SQLite for the small config/cache store.

## 14. Staged development plan

**MVP — ✅ complete**
- Single `/gif` command (renamed `/cinesnip` going into V2 — see Section 2), timecode input only, films only
- Plex search + confirm via buttons (single library/mapping to start — this developer's primary Movies library)
- Direct file access only
- One fixed GIF style, no options menu
- One Docker container, tested against one Discord server
- Goal: prove the full pipeline end-to-end

Built and verified end-to-end against real Plex libraries on both `D:` and
`E:` (both Movies path mappings exercised). See "MVP build notes" below for
non-obvious bugs hit along the way — worth reading before touching
`app/worker/ffmpeg.py` again, since V2's quote-search rendering reuses the
same seek/duration logic.

**V2**
- Rename `/gif` → `/cinesnip` (and `/gif-diagnose` → `/cinesnip-diagnose`)
- Sidecar-subtitle-file + embedded-subtitle-stream extraction (generic — not Bazarr-specific, see Section 5) + fuzzy quote search within a confirmed film
- `/cinesnip-search` — library-wide quote search across the (lazily-built) subtitle cache, with an explicit opt-in to extend a search into not-yet-cached titles (Section 5)
- Style preset select menu, MP4/WebM output option
- Remaining library/drive path mappings (4K, 3D, TV, second drive)
- Document + confirm "Public Bot" disabled as part of Discord setup (Section 9)

**V3**
- TV show support (episode-specific and whole-show search)
- Whisper fallback (cached, lazy)
- NVENC/GPU hardware acceleration option
- Local web app: setup wizard + manual generation UI (see Section 16 for the onboarding wizard spec)
- Allowlists/rate-limiting, multi-server polish, distribution docs for other self-hosters (both Plex-hosting patterns documented)

### MVP build notes (real bugs hit, fixed, and worth knowing about)

- **ffmpeg `-t` must be an input option, not an output option.** Placing
  `-t <duration>` *after* `-i` only bounds the output stream's
  timestamps — which does nothing for filters like `palettegen`/
  `paletteuse` that emit a single frame at the very end of the filter
  graph. With `-t` as an output option, ffmpeg keeps decoding and feeding
  frames into the filter for the rest of the file, since there's no
  rolling output PTS for it to cut off against (confirmed via `-loglevel
  verbose`: a 4-second clip request decoded 6354 frames — the whole rest
  of the file — instead of ~100). Fix: put `-ss` and `-t` **together,
  both before `-i`** — as input options, `-t` bounds how much is actually
  *read*, which is what stops it. See `app/worker/ffmpeg.py`'s
  `build_seek_args()` and its docstring/regression test.
- **The `image2` muxer needs `-update 1`** to write a single still image
  (e.g. the GIF palette PNG) rather than expecting a `%d` sequence
  pattern. This was masked for a while by the bug above, since the
  runaway decode never reached the point of finalizing the file.
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

### V2 subtitle-extraction build notes (real bugs hit, fixed, and worth knowing about)

Found via manual end-to-end verification against the real library (not
synthetic test data) — see the confirmed-access note above for why that
step matters.

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
  actually hung (the timeout-and-kill mechanism itself worked exactly as
  designed; the *default value* was just wrong for real file sizes over
  this I/O path). Fixed by making it a configurable
  `subtitle_defaults.extraction_timeout_seconds` (default 180s) in
  `config.yaml`, mirroring how `render_defaults.timeout_seconds` already
  works — raise it further if you see timeouts on very large files.
- **Real confirmation of the documented PGS/bitmap-codec gap**: a title in
  this library (`A Wounded Fawn`) has three embedded subtitle streams —
  two Chinese, one English — that are *all* `hdmv_pgs_subtitle` (bitmap,
  not text), so all three are correctly filtered out and the title
  resolves to "no subtitles available" rather than a garbage extraction.
  Not a bug, just confirmation the documented limitation is real and
  already-handled, not hypothetical.

## 15. Honest difficulty note

This spans async programming, Docker networking across host/container/WSL2 boundaries, third-party API auth, and optionally local ML inference — genuinely multi-domain, not a first project. Given hands-on experience with Discord bots/webhooks, Docker, and Plex already, the ops-side debugging (Docker networking, Plex connectivity, testing) should feel familiar; the main friction point will be application-logic bugs surfaced through Claude Code's own error output, which is a normal and manageable iterative loop, not a blocker. Build and prove the MVP before adding TV support, Whisper, or the web app — each of those is a real, separable stage, not a detail to bolt on early.

## 16. Onboarding Wizard (Setup UX)

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
   the token automatically) rather than the manual "View XML" trick this
   developer used during MVP testing — that trick is unintuitive, and
   during MVP development this developer also discovered that
   Plex "sign out" in the web UI doesn't reliably invalidate/rotate a
   token, which made manual token hygiene confusing even for a technical
   user. A wizard-driven PIN flow sidesteps all of that. Manual token
   paste stays as an advanced-user fallback only.
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
  UI. This is a real, concrete risk, not a theoretical one: during MVP
  development, an unrelated file-change-tracking mechanism in the coding
  assistant being used ended up displaying the contents of a freshly
  edited `.env` (including both live tokens) back into that session's
  transcript, purely as a side effect of the assistant having previously
  touched that file path — an unintended, avoidable leak that a
  carelessly-logged wizard backend could reproduce just as easily. Design
  the wizard so no code path — logging, error handling, debug tooling —
  ever has a reason to echo a submitted token back out anywhere other than
  the config file it belongs in.
- Once written, tokens are read from `.env`/`config.yaml` exactly like the
  MVP does today (`app/settings.py`) — the wizard is purely a friendlier
  way to produce those same two files, not a new runtime secret-handling
  path.
