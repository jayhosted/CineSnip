# CineSnip — Technical Architecture & Development Plan

## Project summary

A self-hosted Discord bot + local web app that lets you search your own Plex library (films and TV shows) and generate a short GIF or MP4/WebM clip from a spoken quote or timestamp, posted back into Discord. Designed to be installable by other self-hosted Plex users, not just for personal use — but every installation is fully independent: no central service, no shared infrastructure, no data of any kind passing through anyone but the installer's own Plex/Discord.

Inspired by conversation with the developer of [tvgif](https://github.com/warmans/tvgif), a similar but structurally different tool (tvgif pre-converts a whole library to webm+srt ahead of time; this project integrates live with Plex and extracts on demand). **No code should be copied from tvgif** — standard/boilerplate ffmpeg or HTTP patterns are fine since they're not anyone's IP, but the implementation should be built independently out of respect for that developer.

## Further reading (only when touching the relevant code)

Forensic "here's the bug, here's the fix, here's how it was verified" narratives live in linked files, not inline here:

- **`docs/build-notes/ffmpeg-rendering.md`** — ffmpeg subprocess gotchas, MP4/WebM stream-mapping bugs, subtitle burn-in/font/ASS bugs, 3D crop/aspect-ratio bugs, video sync diagnosis. Read before touching `app/worker/ffmpeg.py` or `app/worker/subtitle_render.py`.
- **`docs/build-notes/subtitles-and-search.md`** — subtitle extraction/cache invalidation, quote-ranking bugs, library auto-sync. Read before touching `app/worker/subtitles.py`, `app/worker/quotes.py`, `app/worker/library_search.py`, or `app/worker/library_sync.py`.
- **`docs/build-notes/plex-integration.md`** — path-mapping bugs, redundant Plex fetches, the cross-media index leak. Read before touching `app/worker/plex_client.py` or `app/worker/path_mapper.py`.
- **`docs/build-notes/discord-bot-ux.md`** — search-result confirmation bug, slow-extraction warning, command naming history. Read before touching `app/bot/cogs/gif.py`.
- **`docs/build-notes/docker-image.md`** — image-size trimming decisions (static ffmpeg, trimmed uvicorn extras, the uvloop activation bug).
- **`docs/design/fts5-search-migration.md`** — design + real-scale measurements for the SQLite+FTS5 search index (`cache/quote_index.db`). Read before changing quote-search behavior or ranking.
- **`PRODUCT.md`** / **`DESIGN.md`** — product scope and visual design system for the local web app (`app/web/`). Read before UI work there.

## This developer's own setup (used as the reference/test environment)

- Plex runs **natively on Windows** (not Dockerized) on a separate **server PC** from the one used for development. Docker (Docker Desktop, WSL2 backend) runs on that same server PC, alongside unrelated services already in Docker there.
- Three Plex libraries: **Movies**, **TV Shows**, and a separate 3D library (named plainly **`3D`**, not "3D Movies"). No separate 4K library exists here, but CineSnip's multi-library support (Section 3) is designed to support one generically since that's common on other installs.
- Media folders span two drives: `D:\Plex Additional\{Movies,TV Shows,3D}` and `E:\Media\Video\{Movies,TV Shows}`.
- Uses **Bazarr**, so most media has a sidecar `.srt` next to the video rather than embedded subtitles; some older/less-common titles have only embedded subs or none.
- Development happens via **Claude Code Desktop, connected over SSH into an Ubuntu WSL2 distro on the server PC** (not the internal `docker-desktop` distro), so `docker compose`, `host.docker.internal`, and the mounted media drives are all directly reachable against the real environment.
- Because install docs must work for other people too, **both** a native-host Plex setup and a Dockerized-Plex setup are documented and supported — only the network path to Plex changes between them.
- `.env`/`config.yaml` in this session are populated with live values (not just `.example` templates); both media drives are mounted and browsable at `/mnt/d/Plex Additional/...` and `/mnt/e/Media/Video/...`. **Verify worker-layer changes end-to-end against real media, not just unit tests** — real subtitle/media files surface bugs synthetic test data doesn't.
- **Docker group gotcha**: the WSL2 user (`jaypw`) is not always in the `docker` group and there's no passwordless `sudo`. Fix: have the user run `sudo usermod -aG docker jaypw` themselves (needs their password) — the new group membership then works immediately in the *same* session via `sg docker -c '<command>'`, no restart needed.
- **The worker API is loopback-only inside the container by design** (Section 9 — no inbound public endpoint). To manually hit a worker endpoint from a dev session, use `docker compose exec <service> <command>` to run the request *from inside* the container's network namespace — a plain `curl` from the host/WSL2 side won't reach it.

## Workflow decisions already made (apply these, don't re-litigate)

1. **Output format defaults to GIF, not MP4/WebM.** MP4/WebM give a much smaller file at the same visual quality, but real Discord testing showed they render as an actual video player (no autoplay/loop) and can't be added to Discord's GIF-picker favorites — both were the point of trying them as the default, so GIF stays default. `format:mp4`/`format:webm` remain available as explicit opt-ins.
2. **One container, one process.** Discord bot layer and worker layer (Plex, subtitles, ffmpeg) run in a single Python process inside a single Docker container. No message queue or microservices.
3. **The worker is a small internal HTTP API (FastAPI)**, not called directly from the bot's own process logic — this is what let the local web app (`app/web/`) become a second thin client of the same API instead of a rebuild.
4. **No separate "confirm the film" step for movies.** Autocomplete already pins an exact `rating_key`; when multi-library search made the same title possibly exist in more than one library, the resolution was a non-interactive library-name note folded into the status text (`gif.py`'s `library_note`), not a new click-gated step. **"Confirm the timestamp" still stands** as its own step (`QuoteMatchView` in `app/bot/cogs/gif.py`) — for a quote match this is real disambiguation, not restating a choice already made. Its Cancel ends the whole interaction; there's nothing before it to back up to.
5. **Config split**: `.env` for secrets (Discord bot token, Plex token), `config.yaml` for everything else. Keeps secrets out of anything shared or hand-edited casually.
6. **Setup wizard and manual-generation UI are the same local web app** (`app/web/`), not two separate builds — `/setup` for first-run/reconfiguration, `/generate` for browsing/making clips outside Discord, `/settings` for editing an already-configured install. See `PRODUCT.md`/`DESIGN.md` for the full spec and visual system.
7. **No Whisper transcription fallback, and no GPU/NVENC acceleration.** Whisper was considered as a last resort for titles with neither a sidecar nor an embedded subtitle track, but cut: on this developer's real library that's only ~6.7% of titles, and the sidecar-priority check already gives users a free escape hatch (drop a `.srt`, generated by any external tool, next to the video). Cutting Whisper also removed GPU/NVENC's only stated justification, so that was cut too. A no-sub title simply stays timecode-only — a permanent, documented gap, not a temporary one.

## 1. Overall architecture

- **Discord layer** — Gateway connection, slash commands, buttons, select menus, modals. Talks to the worker via local HTTP calls to its internal API.
- **Worker layer** — Plex API calls, subtitle lookup/extraction, fuzzy quote search, ffmpeg orchestration. Exposes a small internal REST API (`/search`, `/resolve-quote`, `/render`, etc.).
- **Temp processing directory** — scratch space for in-progress renders, cleared aggressively; `/render` streams ffmpeg's output directly rather than always writing to disk first.
- **Subtitle cache** — persisted per Plex media GUID in SQLite+FTS5 (`cache/quote_index.db`, `app/worker/search_index.py`), so a title's subtitles are only ever extracted once. This same cache doubles as the corpus for library-wide quote search (Section 5); it's built up lazily as titles get touched by any flow, never by proactively indexing the whole library. Full design/measurements: `docs/design/fts5-search-migration.md`. Shared between movies and TV episodes, so any read path that should be movie-only (e.g. `/snip-search`) must filter at read time — the index itself isn't type-scoped.

No central database, no cloud API, no message broker.

## 2. Discord bot UX

- Primary commands: `/snip film:<text> quote:<text-optional> timecode:<text-optional> end_timecode:<text-optional>` for films, `/snip-tv show:<text> season:<int-optional> episode:<int-optional> ...` for TV (one layer deeper — `season`/`episode` must be given together or not at all; without them, `quote` searches the whole show, and a bare `timecode` with no episode is rejected). `/snip-search quote:<text>` is a separate, dedicated command for **library-wide** search (Section 5) — deliberately not a "no film given" fallback on `/snip`, since the result shape (many titles, each needing its own label) and performance profile differ. `end_timecode` only applies with `timecode` (a quote match always uses the matched line's own span); a span outside `render_defaults.min/max_duration_seconds` is a clear error, never a silent clamp.
- **Timecode parsing accepts more than `HH:MM:SS`**: colon forms (`1:23:45`, `83`) and unit-suffixed forms in any subset of hours/minutes/seconds (`1h22m12s`, `22min 12sec`) both work (`parse_timecode()` in `app/worker/ffmpeg.py`).
- **Autocomplete on `film`/`show`** queries the live Plex library (`/search`, `/search-shows`) and returns up to 25 matches — Discord's autocomplete cap.
- **Style presets are applied after generation, not before**: the bot renders immediately with a sensible default (Classic for a quote match, No Subtitles for a bare timecode), shows the result, and a style select-menu sits below it — changing it re-renders in place (swaps the attachment on the same message) rather than restarting the command. "Post to channel" always posts whatever's currently shown. This was deliberately reversed from an earlier "pick a style, then generate" flow that added a click to every command for no benefit.
- **Progress handling**: acknowledge within Discord's 3-second window with a deferred "Generating…" response, then edit that message once ffmpeg finishes.
- **Visibility**: default ephemeral for search/confirm/options steps; explicit "Post to channel" button on the final result.
- **Upfront slow-extraction warning**: `GET /subtitle-status/{rating_key}` cheaply answers (no ffmpeg) whether a request is about to fall through to a cold embedded extraction, so the bot can warn before a request that could silently take minutes. Details: `docs/build-notes/discord-bot-ux.md`.

## 3. Plex integration

- **Auth**: Plex's PIN-based flow (request PIN, user approves at plex.tv, poll for token), with manual token entry as a fallback.
- **Library access**: `python-plexapi`.
- **Multiple libraries**: search spans every configured Plex section by default. The same title can exist in more than one library (e.g. a separate 4K library, common on other installs) — a result surfaces which library/quality it came from (decision #4).
- **Two supported Plex-hosting patterns** (document both, wizard asks which applies):
  - **Plex native on the same host as Docker**: container reaches Plex via `host.docker.internal:32400` (Docker Desktop) — not `network_mode: host`, which doesn't work on Windows.
  - **Plex itself running in Docker**: reached via Docker's internal networking (shared compose network, or Plex's container name/IP).
- **Direct file access is strongly preferred over streaming** Plex, for accurate/fast ffmpeg seeking. Requires **per-library path mappings** (not one global mapping), since libraries can live on different drives/folders, each read-only bind-mounted with its own Plex-path → container-path prefix. Falls back to Direct Play (never a transcode) if no mapping is configured.
- **Windows-specific**: Docker Desktop's WSL2 backend handles Windows-path bind mounts (`D:\...` → `/media/...`) cleanly, but only shares `C:` by default — `D:`/`E:` must be added under Settings → Resources → File Sharing.
- **3D content**: 3D encodes store both eyes in one frame (side-by-side or top/bottom); naive extraction produces a squished/doubled image. `LibraryConfig.three_d_format` (`app/settings.py`) tags a library's packing, and `ClipRenderer` crops to one eye before scaling/subtitles. **Real per-file packing can vary within one library** even when the library-level tag is the same — `probe_stereo_format()` checks each file's own Matroska `StereoMode` tag first and overrides the library default when present. Full forensic detail (this took two extra rounds to get right — aspect-ratio and squeeze bugs): `docs/build-notes/ffmpeg-rendering.md`.
- A `PlexClient` in-process cache (30s TTL, keyed by `rating_key`) avoids re-fetching the same Plex metadata multiple times within one request. Windows' extended-length path prefix (`\\?\`) is stripped before path-mapping matching.

## 4. TV show support

- Plex structures TV as Show → Season → Episode — one extra layer on top of the film flow, not a separate system. The worker's `/resolve`, `/render`, and `/resolve-quote` endpoints are 100% generic over a Plex `rating_key` (they only ever touch fields that exist identically on an `Episode` as on a `Movie`), so TV support only needed a thin resolution layer (`GET /resolve-episode/{show_rating_key}?season=&episode=`), not a parallel pipeline. `PlexClient._to_result()` formats an Episode's title as `"Show — S02E01 — Episode Title"` since `MovieResult` has no separate show/season/episode fields.
- Whole-show search (`GET /search-episodes-quote/{show_rating_key}?quote=`) is deliberately inline/synchronous — it extracts+caches subtitles for any not-yet-cached episode on the spot, since a show's episode count is small compared to a whole library. It does not need Section 5's progress/ETA UX.
- **Reusing the generic pipeline across media types means any shared, implicit state needs an explicit audit** — TV and movie renders write into the same `cache/quote_index.db`, which once caused `/snip-search` (movie-only by design) to leak TV episodes into results. Fixed by filtering at read time in `/search-quote`'s handler. This class of bug only surfaces from running real TV+movie data together, not code review — worth remembering before adding another shared cache/index. Full writeup: `docs/build-notes/plex-integration.md`.

## 5. Finding dialogue automatically

- **Subtitle source priority** (deliberately generic, not Bazarr-specific):
  1. Sidecar `.srt` matching the video's filename in the same folder — no ffmpeg needed.
  2. Embedded subtitle stream (`ffmpeg -map 0:s:N`) — this is the *primary* path for installs without a sidecar tool, not a rare fallback.
  3. No transcription fallback (decision #7) — a title with neither simply isn't quote-searchable; timecode input still works.
- **Matching**: parse whichever source into `(start, end, text)` entries; fuzzy-match (`rapidfuzz`) after normalizing case/punctuation; return top candidates (`quote_match.candidate_limit`, 8 default) with context and a confidence score, never committing silently to the top hit.
- **Ranking has real, non-obvious invariants** in `find_quote_matches()` (`app/worker/quotes.py`): a whole-word substring match is force-scored to 100 so it always outranks a fuzzy-only one; partial word overlap gets a capped bonus (60→95); and any candidate below 50% word overlap is *capped*, not just bonused — raw `WRatio` alone can score a near-empty fragment like `"to the"` a 90 against a much longer quote, invisible below full-library scale. Do not "simplify" this to a single `WRatio`/`token_set_ratio` call without re-reading `docs/build-notes/subtitles-and-search.md`.
- **Library-wide quote search (`/snip-search`)** searches only the subtitle cache (titles CineSnip has already parsed via any flow), not the live filesystem — this is Tier 1 only. An FTS5 pre-filter (`search_index.search_entry_ids()`) narrows a query to a bounded set of candidate rows before any fuzzy-scoring happens, so search time doesn't grow with library size. Results use **diversity-first ranking, not a hard one-per-title cap**: every title's best-scoring line competes for a slot before any title's second-best line does, and unfilled slots backfill with each title's next-best (up to `quote_match.library_per_title_limit`, default 3) — never at the expense of a better match from another title. Full design and the entry-vs-title `LIMIT` correctness bug this shipped with: `docs/design/fts5-search-migration.md`.
- **Tier 2 (extend search to not-yet-cached titles) is not built** — deliberately deferred until there's a real progress/ETA UX (see Roadmap below), so a search never silently hangs for minutes.
- **Opt-in automatic library sync** (`config.yaml`'s `library_sync.enabled`, default off, `app/worker/library_sync.py`): change detection via Plex's own `section.updatedAt` (no webhook — Plex Pass–gated; no cron — this project avoids proactive background work otherwise). Removal is gated behind a two-layer safety check (mounts reachable + a sample of "still present" titles actually resolve on disk) so automatic deletion can't be triggered by something that only *looks* like removal.
- **Multiple subtitle tracks**: default to original-language, non-SDH; let the user pick if ambiguous.
- **Realistic expectations**: strong hit rate for well-known lines with a good subtitle track; harder cases include paraphrased quotes, dubbing/translation drift, and lines split across subtitle entries.

## 6. Video extraction

- Standard two-step ffmpeg seek: fast `-ss` before `-i` for keyframe positioning, then a short precisely-timed re-encode of just the needed span. Direct file access preferred over Plex stream to avoid double-transcoding. No audio (`-an`).
- **Subtitle burn-in uses ffmpeg's `subtitles`/`ass` filter (libass), not `drawtext`** — gives real font styling/positioning matching the style presets.
- **No GPU/NVENC acceleration** — the CPU render step is already fast enough (a few seconds for a 4s/480px clip) that there's no real motivation once Whisper (its only stated use case) is off the table.
- **CineSnip's seek is accurate; a reported "sync drift" is almost always per-title subtitle desync, not a bug here** — this was proven pixel-identical against a linear decode. The real trap: Plex's client-side "Auto Sync Subtitles" toggle silently caches its own offset, independent of and surviving past a file being fixed on disk. Read `docs/build-notes/ffmpeg-rendering.md` before re-investigating anything that looks like sync drift — two earlier wrong turns are recorded there.

## 7. GIF/clip generation & subtitle styles

- Two-pass palette generation (`palettegen`/`paletteuse`) for GIF; mp4 (`libx264`)/webm (`libvpx-vp9`) are a single-pass encode straight to a scratch file (not `pipe:1` — mp4's `+faststart` needs to seek back and rewrite the moov atom, which a stdout pipe can't do). No audio in any format.
- **Every non-GIF encode must explicitly set `-map 0:v:0 -map_chapters -1 -map_metadata -1`** — do not remove these even though the command "works" without them on many files. Without `-map 0:v:0`, ffmpeg's default stream selection can silently pull in a subtitle track (no fast-seek, can hang); without `-map_chapters -1`, the mp4 muxer copies the source's full chapter list into the output regardless of `-map`, leaking the whole film's duration into a supposedly-short clip's metadata.
- Defaults: 15fps, 480px width, no crop. **Duration**: a bare timecode uses `render_defaults.duration_seconds` (4s, clamped to `[min,max]_duration_seconds`, 1s–15s default) silently; a quote-driven clip uses the matched line's own start/end (same silent clamp); a timecode **with** an explicit `end_timecode` uses exactly that span but *rejects* (clear error) a span outside the clamp bounds — since the user chose it deliberately.
- Auto-downscales resolution/fps if the estimated output would exceed Discord's attachment size limit.
- **Style presets** (`app/worker/subtitle_render.py`'s `STYLE_PRESETS`): Classic, Boxed, Cinematic, Meme, Original, No Subtitles — Python constants, not `config.yaml`, matching the precedent of `ffmpeg.py`'s `_VIDEO_CODEC_ARGS`. **"Original" doesn't actually mirror source subtitle styling** — plain SRT carries no style data, and ASS/SSA style extraction isn't built — it falls back to the same look as Classic. A style requested where no usable subtitles exist for the clip's window degrades to a plain render rather than erroring; the worker echoes what was *actually* used via an `X-Clip-Style` response header so the bot can tell the user their choice was silently downgraded.

## 8. Docker design

- Single image, single container (Discord bot + worker in one Python process).
- `docker-compose.yml`: env vars for `DISCORD_TOKEN`, `PLEX_URL`, `PLEX_TOKEN`; a config volume; a temp volume/tmpfs for renders, cleared on startup; **one read-only bind mount per media folder** rather than a single media mount.
- Install goal: clone repo → copy `.env.example` to `.env` → run the setup wizard (or hand-edit `config.yaml`) → `docker compose up`.
- Image uses static ffmpeg binaries (not Debian's apt package) and a trimmed `uvicorn[standard]`, for size. Full writeup: `docs/build-notes/docker-image.md`.

## 9. Security/privacy

- Plex token and media access stay confined to the worker module even though it's in the same container as the bot, for future auditability if the layers are ever split.
- No inbound public endpoint — outbound-only connection to Discord's Gateway.
- Anyone with bot access in a server it's added to can browse the library and generate clips — an admin-configurable allowlist and rate-limiting are recommended but not yet built (see Roadmap).
- **"Public Bot" must be disabled in the Discord Developer Portal (Bot → Authorization Flow).** This is a self-hosted, single-owner bot tied to one person's Plex library (Section 10) — it must never be self-service-invitable by a stranger clicking "Add to Server," since that would hand them browse/generate access to the installer's personal media. With it off, only the application owner can generate a working invite URL. This is defense-in-depth alongside the allowlist: Public Bot off stops unwanted *invites*, the allowlist limits misuse *within* servers the owner did invite it to.

## 10. Multiple servers/users — v1 model

One Docker install = one Plex owner = one bot application, invited to whichever Discord server(s) chosen. All users in those servers share access through the bot. Single global admin allowlist for v1; no per-server/per-user permission granularity.

## 11. Hosting model

Bot + processing run entirely on the installer's own machine — no centrally-hosted variant. A shared/hosted option would reintroduce the third-party data exposure this project exists to avoid, plus ongoing maintenance burden, for no real benefit.

## 12. Recommended stack

**Python**: `discord.py`/`Pycord` for the bot layer, `python-plexapi` for Plex, `subprocess` calls to the ffmpeg CLI directly, `rapidfuzz` for subtitle matching, FastAPI for the internal worker API and local web app, SQLite (+FTS5) for the cache/search index.

## 13. Roadmap — not yet built

- **`/snip-search` Tier 2**: extend search to not-yet-cached titles by parsing them on the spot. Deliberately deferred until there's a real progress/ETA UX — a silent multi-minute hang is worse than the current "cached titles only" scope.
- **Interactive clip editor**: no way to *adjust* a clip after seeing it (nudge start/end, merge in the next/previous subtitle line) — `QuoteMatchView` only lets you pick among precomputed candidates. tvgif's UI (`Previous Sub`/`Next Sub`/`Merge Next Sub` buttons) is a useful reference, not something to copy exactly. Needs a stateful edit session with live preview re-render; the local web app (`app/web/`) is the more natural home for this than a Discord button grid.
- **Audio-only clip output**: two distinct ideas, don't conflate them — (1) a small `format:mp3/ogg` opt-in reusing the existing render pipeline (`-vn` instead of `-an`), no new permissions needed; (2) actually populating a server's Discord Soundboard via its API, which is a materially bigger feature (hard 5.2s/512KB/mp3-ogg-only limits unrelated to `render_defaults`, a new `Create Expressions` bot permission requiring re-invites, and a per-guild sound-count cap tied to boost level).
- **Allowlists/rate-limiting**: admin-configurable user/role allowlist and basic rate-limiting (Section 9) — not yet implemented.

## 14. Onboarding wizard & local web app

The local web app (`app/web/`) serves `/setup` (first-run/reconfiguration wizard), `/generate` (manual clip generation, a thin client of the same worker API the bot uses), and `/settings`. Product scope and UX flow: `PRODUCT.md`. Visual design system: `DESIGN.md`. Build history/bugs found along the way: see the `app/web/app.py` git history if needed — not duplicated here.

### Security requirements (non-negotiable)

- Tokens are written **directly to local `.env`/`config.yaml` on disk by the wizard's own backend** — never transmitted anywhere external, never logged, never included in any error reporting (there is none — no telemetry exists or should exist in this project).
- The web app **binds to localhost only by default**; LAN exposure is an explicit opt-in, never the default, since it handles raw secrets during setup.
- Token input fields are masked, and **the backend must never log full request/response bodies** for token-submission endpoints, even in debug/verbose logging — a logged request body is as much a leak as printing the token to the UI. Concretely: a coding assistant's own file-change tracking can echo a previously-touched file's full contents (e.g. a live `.env`) back into its transcript as a side effect of having touched that path before — no code path (logging, error handling, debug tooling) should ever have a reason to echo a submitted token anywhere other than the config file it belongs in.
- Once written, tokens are read from `.env`/`config.yaml` exactly like `app/settings.py` already does — the wizard is purely a friendlier way to produce those same two files, not a new runtime secret-handling path.
