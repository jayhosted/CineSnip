# CineSnip — Technical Architecture & Development Plan
*Living reference for Claude Code sessions — also usable as the project's CLAUDE.md*

## Project summary

A self-hosted Discord bot + local web app that lets you search your own Plex library (films and TV shows) and generate a short GIF or MP4/WebM clip from a spoken quote or timestamp, posted back into Discord. Designed to be installable by other self-hosted Plex users, not just for personal use — but every installation is fully independent: no central service, no shared infrastructure, no data of any kind passing through anyone but the installer's own Plex/Discord.

Inspired by conversation with the developer of [tvgif](https://github.com/warmans/tvgif), a similar but structurally different tool (tvgif pre-converts a whole library to webm+srt ahead of time; this project integrates live with Plex and extracts on demand). **No code should be copied from tvgif** — standard/boilerplate ffmpeg or HTTP patterns are fine since they're not anyone's IP, but the implementation should be built independently out of respect for that developer.

## Further reading (only when touching the relevant code)

This file holds standing decisions, architecture, and specs — read it in
full. The forensic "here's the bug, here's the fix, here's how it was
verified" narratives that used to live inline here have moved out into
linked files, since they're only useful when actually touching the file
they're about:

- **`docs/build-notes/ffmpeg-rendering.md`** — ffmpeg subprocess gotchas, MP4/WebM stream-mapping bugs, subtitle burn-in/font/ASS bugs, 3D crop/aspect-ratio bugs, video sync diagnosis. Read before touching `app/worker/ffmpeg.py` or `app/worker/subtitle_render.py`.
- **`docs/build-notes/subtitles-and-search.md`** — subtitle extraction/cache invalidation, quote-ranking bugs, the candidates cache, library auto-sync. Read before touching `app/worker/subtitles.py`, `app/worker/quotes.py`, `app/worker/library_search.py`, or `app/worker/library_sync.py`.
- **`docs/build-notes/plex-integration.md`** — path-mapping bugs, redundant Plex fetches, the cross-media index leak. Read before touching `app/worker/plex_client.py` or `app/worker/path_mapper.py`.
- **`docs/build-notes/discord-bot-ux.md`** — search-result confirmation bug, slow-extraction warning, command naming history. Read before touching `app/bot/cogs/gif.py`.
- **`docs/build-notes/docker-image.md`** — image-size trimming (ffmpeg binaries, uvicorn extras, the uvloop activation bug).
- **`docs/design/fts5-search-migration.md`** — full design + measurements for the planned SQLite+FTS5 search migration (not yet built).

## This developer's own setup (used as the reference/test environment)

- Plex runs **natively on Windows** (not Dockerized) on a separate **server PC** from the one used for development.
- Docker (Docker Desktop, WSL2 backend) also runs on that server PC, alongside other unrelated services already running in Docker there.
- Server PC has an **RTX 3070**, but CineSnip doesn't use it — no Whisper, no GPU-accelerated encode (see decision #7 below for why).
- Three Plex libraries: **Movies**, **TV Shows**, and a separate 3D library (named plainly **`3D`** in Plex, not "3D Movies"). No separate 4K library exists here — 4K files just live inside the regular Movies library — but a distinct 4K library is a common enough setup elsewhere that CineSnip's multi-library support (Section 3) is still designed to support one generically, not this developer's setup specifically.
- Media folders span two drives:
  - `D:\Plex Additional\Movies` and `E:\Media\Video\Movies`
  - `D:\Plex Additional\TV Shows` and `E:\Media\Video\TV Shows`
  - `D:\Plex Additional\3D` (3D library, one folder only)
- Uses **Bazarr**, so the large majority of media has a separate `.srt` subtitle file sitting in the same folder as the video, rather than embedded subtitles. Some older/less-common titles may still only have embedded subs or none at all.
- Development happens via **Claude Code Desktop, connected over SSH into a Ubuntu WSL2 distro on the server PC** (not the internal `docker-desktop` distro), so that Claude Code can directly run `docker compose`, reach Plex over `host.docker.internal`, and access the mounted media drives, all against the real environment rather than a stand-in.
- Because the install docs need to work for other people too, **both** a native-host Plex setup (like this one) and a Dockerized-Plex setup must be documented and supported — the only thing that changes between them is how the container reaches Plex on the network.
- **A Claude Code session in this WSL2 distro has real, direct access**: `.env`/`config.yaml` are populated with live values (not just the `.example` templates), both media drives are mounted and browsable at `/mnt/d/Plex Additional/Movies` and `/mnt/e/Media/Video/Movies`, and `docker compose` can build/run the real container against the real Plex library. **Verify worker-layer changes end-to-end against real media during development, not just with unit tests** — real subtitle/media files surface bugs synthetic test data doesn't.
- **Docker group gotcha**: the WSL2 user (`jaypw`) is not always in the `docker` group and there's no passwordless `sudo`, so a fresh session may get "permission denied" talking to the Docker daemon. Fix: have the user run `sudo usermod -aG docker jaypw` themselves (needs their password, so Claude Code can't run it directly) — the new group membership then works immediately in the *same* session via `sg docker -c '<command>'`, without needing to restart the whole session.
- **The worker API is loopback-only inside the container by design** (Section 9 — no inbound public endpoint). To manually hit a worker endpoint (e.g. the `/subtitles/{rating_key}` diagnostic route) from a dev session, use `docker compose exec <service> <command>` to run the request *from inside* the container's network namespace — a plain `curl` from the host/WSL2 side won't reach it, since no port is published in `docker-compose.yml`.

## Workflow decisions already made (apply these, don't re-litigate)

1. **✅ Output format defaults to GIF — reversed back from an MP4/WebM default that shipped, then failed real Discord testing.** MP4/WebM were tried as the default for their much smaller file size at the same visual quality (~55KB mp4 / ~27KB webm vs. ~5.7MB gif for an identical 4s/480px clip, confirmed against the real library) — the assumption being "Discord renders both inline like a GIF." **That assumption was wrong.** Live testing in a real server showed mp4/webm attachments render as an actual video player (play button, scrubber, volume slider) with no autoplay/loop, and — the deciding factor — can't be added to Discord's GIF-picker favorites the way a real `.gif` attachment can. Both were the entire point of defaulting away from GIF in the first place, so the default reverted. `format:mp4`/`format:webm` on `/snip`/`/snip-tv` remain fully available as explicit opt-ins for anyone who'd rather trade autoplay/favoriting for a much smaller file (e.g. saving/reposting outside Discord).
2. **One container, one process to start.** Discord bot layer and worker layer (Plex, subtitles, ffmpeg) run in a single Python process inside a single Docker container. No message queue or microservices for v1.
3. **Structure the worker as a small internal HTTP API from the start** (e.g. FastAPI), even though the Discord bot is its only client initially. This makes a future local web app (setup wizard + manual GIF-generation UI) a thin second client rather than a rebuild.
4. **No separate "confirm the film" step for movies** — reversed from the original decision after real usage showed it was pure friction: autocomplete already pins an exact `rating_key` (title + year shown right in the picker), and CineSnip currently only searches one Plex library, so there's nothing left to disambiguate by the time the command runs. **Revisit once multi-library search ships** (Section 3) — a same-titled result from a different library would need surfacing again at that point, likely back as a lightweight non-interactive note rather than a click-gated step. **"Confirm the timestamp" still stands** as its own step (`QuoteMatchView` in `app/bot/cogs/gif.py`) — for a quote match this is real disambiguation (which candidate line, at what confidence), not restating a choice already made. Its Cancel currently ends the whole interaction rather than "backing up" to anything — with the film-confirm step gone, there's nothing before it to back up to; a full `/snip` restart is what backing out means today.
5. **Config split**: `.env` for secrets (Discord bot token, Plex token), `config.yaml` for everything else (per-library path mappings, style presets, feature flags). Standard self-hosted-app practice — keeps secrets out of anything that might get shared or hand-edited casually.
6. **Setup wizard and manual-generation UI are the same small local web app**, not two separate builds — a `/setup` route for first-run/reconfiguration, a `/generate` route for browsing/making clips outside Discord. Build the Discord bot and prove the core pipeline first; build this web app as a fast-follow, not alongside. Full spec for the wizard itself: see Section 14.
7. **✅ Whisper transcription fallback dropped entirely, along with the GPU/NVENC accel phase that existed only to speed it up.** Originally planned (Section 5 tier 3, V3 Phase 3) as the last resort when a title has neither a sidecar nor an embedded subtitle track. Reconsidered and cut: on this developer's real library that's only 764/11,463 titles (~6.7%, per Section 5's full-library cache build), and — the deciding factor — CineSnip's existing sidecar-priority check already gives users a free escape hatch for that tail case: run Whisper (or any other tool) externally and drop the resulting `.srt` next to the video, and it's picked up automatically with zero CineSnip-side Whisper code needed. Bundling Whisper would have added a real dependency/image-size/config surface (model-size choice, CPU-vs-GPU inference, its own cache-invalidation class) to cover a case users can already self-serve around. Cutting Whisper also removed the only stated justification for GPU/NVENC accel (Section 6's render step is already fast enough on CPU alone), so that phase was cut too, not just reordered. A no-sub title simply stays timecode-only, same as the documented V2 gap — this developer's own workaround is manual subtitle retrieval, or a one-off standalone Whisper run outside CineSnip, per title, as needed.

## 1. Overall architecture

- **Discord layer** — Gateway connection, slash commands, buttons, select menus, modals. Talks to the worker via local HTTP calls to its internal API.
- **Worker layer** — Plex API calls, subtitle lookup/extraction, fuzzy quote search, ffmpeg orchestration. Exposes a small internal REST API (e.g. `/search`, `/resolve-quote`, `/render`).
- **Local config store** — SQLite or a flat file for anything not covered by `.env`/`config.yaml` that needs to persist at runtime (e.g. per-guild allowlists once that's built).
- **Temp processing directory** — scratch space for in-progress renders, cleared aggressively; the `/render` endpoint should stream ffmpeg's output directly rather than always writing a file to disk first.
- **Subtitle cache** — persisted per Plex media GUID, so a film's subtitles are only ever extracted once. This cache doubles as the corpus for library-wide quote search (Section 5) — it's built up lazily/opportunistically as titles get touched by any flow, never by proactively indexing the whole library up front. A small SQLite index (`cache/quote_index.db`, `app/worker/quote_index.py`) tracks which titles are cached (`guid → rating_key/title/library_name`) so `/snip-search` can enumerate them directly instead of scanning/parsing every cache file — purely a derived, rebuildable pointer table; the JSON cache files remain the only place subtitle text itself lives. Shared with TV episodes too (both flow through the same generic `/render`/`/resolve-quote` — Section 4), so `/snip-search`'s own handler filters back down to movie-only libraries at read time rather than the index itself being movie-only.

No central database, no cloud API, no message broker.

## 2. Discord bot UX

- **Renamed from `/gif` to `/cinesnip`, then consolidated onto `/snip`** (the former decided going into V2, the latter decided going into V3 Phase 2 alongside TV support). `/gif` → `/cinesnip` reasoning still applies to the *shape* of the names (Discord slash commands are namespaced per-application, so there's no technical collision risk between bots — but a server with two bots that both register the same short command name shows a disambiguation picker before the user can act, which is real friction). The further consolidation reasoning: `/snip` had existed alongside `/cinesnip` since V2 as a same-behavior shorter alternative (see below); once real daily use showed the shorter name was what actually got typed, keeping both names registered was pure redundancy rather than genuine choice — so `/cinesnip` was dropped outright (not kept as a deprecated alias) and `/snip` became the sole primary command. `/cinesnip-search` → `/snip-search` for the same reason. (`/cinesnip-diagnose`, mentioned in Section 3, was never actually built, so there was nothing to rename there — any future diagnostic command should launch directly as `/snip-diagnose`.)
- `/snip film:<text> quote:<text-optional> timecode:<text-optional> end_timecode:<text-optional>` for films. `film` is required — this is the film-first flow: confirm the title, then confirm a timestamp within it (via quote search or direct timecode). `end_timecode` only applies with `timecode` (not `quote`, which always uses the matched line's own span — Section 7) and lets the user pick a custom clip length instead of the fixed default; requests outside `render_defaults.min/max_duration_seconds` get a clear error rather than a silent clamp.
- **✅ `/snip-tv show:<text> season:<int-optional> episode:<int-optional> quote:<text-optional> timecode:<text-optional> end_timecode:<text-optional>`** (V3 Phase 2) — the TV equivalent, one layer deeper: `show` (autocomplete) is required; `season`/`episode` must be given together or not at all. With both given, behaves exactly like `/snip` (decision #4's no-confirm-step reasoning applies identically — autocomplete + explicit season/episode already pins an exact episode) by resolving straight to a rating_key via the worker's `/resolve-episode` and handing off to the *same* `GifCog._generate()` films already use — see Section 4 for why this needed almost no new render/quote-search code. Without `season`/`episode`, `quote` searches across the whole show instead (a bare `timecode` with no episode is rejected — there's no "the show's timeline" to seek within); see Section 4 for the on-demand search design.
- **Timecode input accepts more than `HH:MM:SS`**: colon forms (`1:23:45`, `83`) still work, and so do unit-suffixed forms in any subset of hours/minutes/seconds — `1h22m12s`, `22min 12sec`, `1hr22min2sec`, etc. (`parse_timecode()` in `app/worker/ffmpeg.py`). Applies everywhere a timecode string is accepted (`timecode`, `end_timecode`).
- **✅ `/snip-search quote:<text>`** — a separate, dedicated command for **library-wide** quote search (added in V2, see Section 5 for the indexing/caching design behind it; Tier 1 only — see Section 5 for what's still not built). Deliberately a different command, not a "no film given" fallback on `/snip`: the result shape is different (candidates span many titles, each needs its own title/library/quality label, not just surrounding-line context within one film) and the performance profile is different (see Section 5). The dropdown itself shows each candidate's actual matched line as the option label (`Title — Library · timecode · score%` as the description underneath) — picking a line to look for, not just a film, is the point of a quote search. Selecting a result from `/snip-search` (`LibrarySearchView` in `app/bot/cogs/gif.py`) funnels into the same "confirm the film" → "confirm the timestamp" cheap-redo steps as the normal flow (decision #4) by calling straight into `GifCog._generate()`, threading the selected match's start time through as `preferred_start` so the confirm step actually defaults to the line the user picked, not just the top hit of a fresh re-search. `LibrarySearchView` is also reused as-is by `/snip-tv`'s whole-show search. Fix history: `docs/build-notes/discord-bot-ux.md`.
- **Autocomplete on `film`/`show`**: as the user types, query the local Plex library (via the worker's `/search` for films, `/search-shows` for TV) and return up to 25 matches — Discord's autocomplete cap.
- **No film-confirmation embed** (see decision #4) — the picked film's title is folded into the "Searching subtitles…"/"Generating…" status message instead of a separate click-gated step. Revisit alongside multi-library search.
- **Quote search results**: top match plus surrounding subtitle context and a confidence indicator; Confirm or "show other matches" (select menu of alternatives) if confidence is borderline.
- **✅ Options step, applied after generation rather than gating it**: select menu of style presets (Classic / Boxed / Cinematic / Meme / Original / No Subtitles) rather than exposing every parameter. Originally specced as a step *before* generating (pick a style, then hit Generate) — real usage showed that added a click to every single command for no benefit when the pre-picked default was already right most of the time. Now: generate immediately with a sensible default (Classic for a quote match, No Subtitles for a bare timecode), show the result, and the same style dropdown sits *below* it — changing it re-renders in place (swaps the attachment on the same message) without restarting the command, and "Post to channel" always posts whatever's currently shown. A modal for advanced overrides (duration/fps/crop) is still a separate, not-yet-built idea (Section 7's interactive clip editor).
- **Progress handling**: acknowledge within Discord's 3-second window with a deferred "Generating…" response, then edit that message once ffmpeg finishes.
- **Visibility**: default ephemeral for the search/confirm/options steps, explicit "Post to channel" button on the final result.
- **✅ Upfront warning before a likely-slow cold subtitle extraction** — a single-title quote search or styled render could silently take several minutes on a cold cache with no sidecar, with no hint that was coming. `GET /subtitle-status/{rating_key}` answers cheaply (no ffmpeg) whether a request is about to fall through to a cold embedded extraction; `_slow_subtitle_warning()` folds the result into the status text, best-effort. Details/verification: `docs/build-notes/discord-bot-ux.md`.

## 3. Plex integration

- **Auth**: Plex's PIN-based flow (request PIN, user approves at plex.tv, poll for token) for a first-run wizard; manual token entry as a fallback.
- **Library access**: `python-plexapi` — mature, covers search, metadata, and media-part resolution for both films and TV episodes.
- **Multiple libraries**: enumerate all Plex sections via the API; search spans all of them by default. Because the same title can exist in more than one library (e.g. a separate 4K library, common on other installs even though this developer's own setup doesn't have one), the confirmation step must show which library/quality a result came from rather than silently picking one.
- **Two supported Plex-hosting patterns** (document both, let the setup wizard ask which applies):
  - **Plex native on the same host as Docker** (this developer's setup): container reaches Plex via `host.docker.internal:32400` (Docker Desktop, Windows/Mac) — not `network_mode: host`, which doesn't apply on Windows.
  - **Plex itself running in Docker**: container reaches it via Docker's internal networking — shared docker-compose network, or Plex's container name/IP on a shared Docker network.
- **Direct file access vs. Plex stream**: direct file access is strongly preferred for accurate, fast ffmpeg seeking. Requires **per-library path mappings** (not one global mapping), since libraries can live on different drives/folders — e.g. this setup needs four separate mappings (`D:\Plex Additional\Movies`, `E:\Media\Video\Movies`, `D:\Plex Additional\TV Shows`, `E:\Media\Video\TV Shows`), each mounted read-only into the container and each with its own Plex-path → container-path prefix.
- **Windows-specific note**: Docker Desktop's WSL2 backend handles Windows-path bind mounts (`D:\...` → `/media/...`) cleanly; Docker Desktop only shares the `C:` drive by default, so `D:` and `E:` must be explicitly added under Settings → Resources → File Sharing.
- **Stream fallback**: if path mapping isn't configured, request Direct Play (not a transcode) through the Plex API.
- **3D content caveat**: 3D encodes typically store both eyes in a single frame (side-by-side or top/bottom). Naive extraction produces a squished/doubled image — needs explicit handling (crop to one eye) rather than being treated like a normal video file.
- **✅ 3D format handling (V3 Phase 1).** `LibraryConfig.three_d_format` (`app/settings.py`, default `"none"`) tags a library's packing (`side_by_side` | `over_under`); `ClipRenderer` (`app/worker/ffmpeg.py`) inserts a crop-to-one-eye step into the filter graph before scaling/subtitles whenever a library is so tagged, and `_write_ass_file` adjusts the source dimensions it computes `PlayResY` from to match the post-crop single-eye frame, not the raw packed one. **Real per-file packing varies within one library** (this developer's own `3D` library has one title with a real Matroska `StereoMode` tag and one without, despite both being 3D) — `probe_stereo_format()` checks each file's own tag first and overrides the library default when present. Getting this fully correct took two more rounds after the initial crop-only fix (a stretched-aspect-ratio bug, then a squeezed-vs-full-pack bug) — full forensic writeup, the exact `ffprobe`/`cropdetect` commands used, and the verification lessons learned: **`docs/build-notes/ffmpeg-rendering.md`**.
- **Diagnostics**: worth building a `/snip-diagnose` (or web-app equivalent) command that reports what paths the container sees vs. what Plex reports, since path-mapping issues are the single most likely install-time failure for other users.
- **✅ Fixed: redundant Plex fetches.** A single `/snip film:X quote:Y` invocation used to re-fetch the same Plex movie metadata three separate times (once each in `/resolve`, `/resolve-quote`, `/render`). Fixed with a short-lived (30s) in-process cache in `PlexClient` keyed by `rating_key`. Details: `docs/build-notes/plex-integration.md`.
- **✅ Fixed: Windows' extended-length path prefix (`\\?\`) wasn't stripped before path-mapping matching**, breaking 14 real titles with otherwise-correct mappings. Details: `docs/build-notes/plex-integration.md`.

## 4. TV show support

- Plex structures TV as Show → Season → Episode; treat this as one extra layer on top of the film flow, not a separate system.
- Two supported patterns:
  - User specifies an episode directly (`/snip-tv show:The Office season:2 episode:1 quote:"that's what she said"`) — same narrow, fast flow as films.
  - User gives a show + quote with no episode — search across that show's episodes for the line. Slower (more subtitle files to check) and should be on-demand only, never pre-indexing an entire show's library proactively.
- **✅ Built (V3 Phase 2).** The key finding that shaped the implementation: the worker's `/resolve`, `/render`, and `/resolve-quote` endpoints (`app/worker/api.py`) were already 100% generic over a Plex `rating_key` — they call `PlexServer.fetchItem()` (type-agnostic) and only ever touch fields (`title`, `guid`, `plex_path`, `library_name`, `duration_ms`) that exist identically on a plexapi `Episode` as on a `Movie`. So TV support didn't need a parallel render/quote-search pipeline — it needed a thin layer that resolves `show + season + episode` down to a `rating_key`, after which the entire existing pipeline (including 3D handling, subtitle degrade-to-none, the quote_index cache, style presets) works completely unchanged.
  - `PlexClient` (`app/worker/plex_client.py`): `_to_result()` now branches on `item.TYPE` — an Episode gets its `MovieResult.title` formatted as `"Show — S02E01 — Episode Title"` (folding show/season/episode into the one field every downstream consumer already displays, since `MovieResult` has no separate show/season/episode fields to draw on) and `year=None` (episodes have no year of their own). New `get_episode(show_rating_key, season, episode)` and `list_episodes(show_rating_key)` wrap plexapi's `show.episode(season=, episode=)` / `show.episodes()` (season 0 specials included natively, no filtering needed); both raise a clean `EpisodeNotFoundError`/`ShowNotFoundError` on an invalid combination rather than propagating plexapi's `NotFound`.
  - Worker API: `GET /search-shows` (autocomplete), `GET /resolve-episode/{show_rating_key}?season=&episode=` (the show+season+episode → rating_key resolve step — the only genuinely new thing the render/quote pipeline needed), and `GET /search-episodes-quote/{show_rating_key}?quote=` (whole-show search: lists the show's episodes live via Plex, extracts+caches subtitles inline for any not-yet-cached episode — sequentially, catching and skipping a broken/unmapped episode file rather than failing the whole request — then calls `search_cached_library()` **completely unchanged**, since that function was already generic over any `list[CachedTitle]`). Whole-show search is deliberately inline/synchronous rather than needing Section 5's still-unbuilt Tier 2 progress UI, since a show's episode count is small compared to a whole library.
  - **✅ Fixed a real bug found during verification against the real library, not caught by design review: `/snip-search` (movie-only per its own docs) started leaking TV episodes into its results**, because `/render`/`/resolve-quote` write into the same shared `cache/quote_index.db` regardless of media type. Fixed by filtering `/search-quote`'s handler to movie-type library names at read time. **Lesson**: reusing a generic pipeline across media types means any shared, implicit state it touches needs an explicit audit — this only surfaced by running real TV+movie data together, not by code review. Full writeup and verification: `docs/build-notes/plex-integration.md`.

## 5. Finding dialogue automatically

- **Subtitle source priority** (deliberately generic — must work for installs that don't use Bazarr and/or have only embedded subs, not just this developer's setup):
  1. Look for a separate subtitle file matching the video's filename in the same folder — this covers Bazarr's naming convention (this developer's setup) but is really just "does a sidecar `.srt` exist," which works identically for anyone who drops subtitle files next to their videos by any means, manual or tool-managed. No ffmpeg extraction needed, just read the file.
  2. If none found, check for and extract an embedded subtitle stream (`ffmpeg -map 0:s:N`) — this is the *primary* path for installs without a sidecar-subtitle tool, not a rare fallback, so it needs to be genuinely well-supported in V2, not an afterthought bolted on later.
  3. **✅ No local transcription fallback — decided against, not just unbuilt.** A Whisper fallback was originally planned here; dropped entirely per decision #7 above (real-library measurement: only ~6.7% of titles lack both a sidecar and an embedded stream, and the sidecar-priority check above already lets a user drop in an externally-generated `.srt` to cover that case). A title with neither a sidecar file nor an embedded stream simply isn't quote-searchable — timecode input still works via `/snip`/`/snip-tv`. This is a permanent, documented gap, not a temporary V2/V3 one.
- **Matching**: parse whichever subtitle source into `(start, end, text)` entries; fuzzy-match the typed quote (`rapidfuzz`) after normalizing case/punctuation; return the top candidates (`quote_match.candidate_limit`, 8 by default) with surrounding context and a confidence score rather than committing silently to the top hit.
- **✅ Ranking fixed twice, then a scale-only bug found via the full-library cache build.** `find_quote_matches()` (`app/worker/quotes.py`) now: (1) force-scores any candidate containing the quote as a whole-word substring to 100, so a literal match always outranks a fuzzy-only one; (2) awards a capped bonus for partial word overlap (scaling 60→95) so a scrambled multi-word quote still finds its target, without using symmetric `token_set_ratio` (which would let a short truncated match tie a real one); (3) *caps* — not just bonuses — any candidate below 50% word overlap, because raw `WRatio` alone can score a near-empty fragment like `"to the"` a 90 against a long quote, a bug invisible below full-library scale (10K+ titles) and confirmed via `WRatio("assistant to the regional manager", "to the") == 90.0`. Full narrative, real numbers, and the rejected alternatives: `docs/build-notes/subtitles-and-search.md`.
- **Confirmed, not just theorized: `/snip-search`'s "instant regardless of library size" promise breaks down at real full-library scale** (~40s against an 11,463-title cache, fuzzy-scoring-bound). Not urgent day-to-day, but real evidence a pre-filter/index is needed eventually — see the planned SQLite+FTS5 design below.

### Planned (not yet built): consolidate the subtitle cache into SQLite + FTS5

Full design, real measurements from a 1,000-title prototype (search time 40s → 2ms–740ms, size 3.6GB → 1.57GB), the FTS5 typo-recall tradeoff and its required fallback, and the alternatives ruled out (trigram tokenizer, thread parallelism, external search engines): **`docs/design/fts5-search-migration.md`**. Not implemented — sequence it before or alongside any future full-library manual cache build (see `scripts/build_full_cache.py`), since that's exactly what makes today's design hurt at scale.

- **✅ `scripts/build_full_cache.py` made incremental.** Re-running the full build after adding new content used to mean a ~30-minute sweep touching every title again; now skips any title already cached unless `--force` is passed (1s to re-check 11,463 titles). This became the shape `library_sync` below actually uses: enumerate → cheap existence/freshness check → do real work only for what's missing.
- **✅ Built: opt-in automatic library sync** (`config.yaml`'s `library_sync.enabled`, default off). `app/worker/library_sync.py`; `scripts/build_full_cache.py` is now a thin CLI wrapper around its shared `sync_one_title()`. Change detection via Plex's own `section.updatedAt` (not a webhook — Plex Pass–gated and would need an inbound connection; not cron — this project avoids proactive background work otherwise). Removal is gated behind a two-layer safety check (mounts reachable + a sample of "still present" titles actually resolve on disk) so automatic deletion can't be triggered by something that only *looks* like removal. Config: `enabled`/`interval_hours` only; safety-guard constants aren't configurable. Two real datetime/object-identity bugs found while building this, and full verification against the real library: `docs/build-notes/subtitles-and-search.md`.
- **Multiple subtitle tracks**: default to original-language, non-SDH; let the user pick if ambiguous.
- **Realistic expectations**: strong hit rate for well-known lines with a good subtitle track; harder cases include paraphrased quotes, dubbing/translation drift, and lines split across subtitle entries — design the UI around "likely candidates," not guaranteed exact matches.
- **Library-wide quote search (`/snip-search`, V2)**: searches the subtitle cache described in Section 1, not the live filesystem, so its scope is exactly "titles CineSnip has already parsed via any flow" — which starts small and grows with normal use. Two tiers, so a `/snip-search` call never surprises the user with a multi-minute wait:
  1. **✅ Built — default**: search only already-cached titles. Fast (it's just fuzzy-matching against parsed text already in the cache/DB), returns instantly regardless of library size. Implementation: `GET /search-quote` (`app/worker/api.py`) enumerates cached titles via the SQLite index (Section 1), then `search_cached_library()` (`app/worker/library_search.py`) reuses `find_quote_matches()` unchanged against each title's cached JSON. **Diversity-first ranking, not a hard one-per-title cap**: every title's best-scoring line competes for a results slot before any title's second-best line does, but unfilled slots backfill with each title's next-best line (up to `quote_match.library_per_title_limit`, default 3) — never at the expense of a better match from another title.
  2. **Not yet built — explicit opt-in**: extend the search to the rest of the library by parsing every not-yet-cached title's subtitles on the spot. Deliberately deferred — it needs a real progress/ETA UX that nothing in this app has built yet, which is its own design pass (V3 Phase 3 below).
  - Results list each candidate's film title, library/quality (Section 3), matched line, and confidence.
- **✅ Precomputed, disk-cached match candidates.** Normalizing subtitle lines and building match candidates was 53% of total search time — entirely deterministic given a title's raw entries, so it's now cached to disk (`cache/<guid-digest>.candidates.json`) instead of rebuilt on every search. An in-memory cache was considered and rejected (unbounded RAM cost at scale, plus a staleness-risk class this project already got bitten by twice). Numbers and the invalidation approach: `docs/build-notes/subtitles-and-search.md`.

## 6. Video extraction

- Standard two-step ffmpeg seek: fast `-ss` before `-i` for keyframe positioning, then a short precisely-timed re-encode of just the needed span.
- Direct file access preferred over Plex stream to avoid double-transcoding; Direct Play if falling back to a stream.
- No audio needed for GIF/short-clip output.
- **✅ Subtitle burn-in via ffmpeg's `subtitles`/`ass` filter (libass), not `drawtext`** — gives real font styling/positioning matching the style presets. See Section 7 for the style catalog; `docs/build-notes/ffmpeg-rendering.md` for the font/ASS-styling build notes.
- **No GPU/NVENC acceleration** (decision #7) — the CPU render step is already fast enough (see timing below) that there's no real motivation for it once Whisper (its only stated use case, per the old Section 13 phase ordering) is off the table.
- Typical timing: a few seconds for a 4s/480px clip on CPU.
- **✅ Diagnosed: CineSnip's seek is accurate; a reported "sync drift" was always per-title subtitle desync, not a bug in this project.** Seek itself is proven pixel-identical against a linear decode. Diagnosis method (embedded PGS as ground truth, `ffprobe`/contact-sheet commands, full-runtime not just single-point confirmation), the real trap that cost the most time (Plex's client-side "Auto Sync Subtitles" toggle silently caches its own offset, independent of and surviving past a file being fixed on disk), and why Bazarr's match score isn't verified timing: full forensic writeup in **`docs/build-notes/ffmpeg-rendering.md`**. Read it before re-investigating anything that looks like sync drift — two earlier wrong turns are recorded there so they don't get repeated.

## 7. GIF/clip generation & subtitle styles

- Two-pass palette generation (`palettegen`/`paletteuse`) for GIF output; mp4 (`libx264`)/webm (`libvpx-vp9`) are a single-pass encode straight to a scratch file instead (not `pipe:1` — mp4's `+faststart` needs to seek back and rewrite the moov atom after encoding, which a stdout pipe can't do; webm doesn't strictly need this but shares the same code path for simplicity). No audio in any format (`-an`).
- **Every non-GIF encode explicitly sets `-map 0:v:0 -map_chapters -1 -map_metadata -1`** — do not remove these, even though the command "works" without them on many files. Confirmed via real bugs on this project's own library (`docs/build-notes/ffmpeg-rendering.md`): without `-map 0:v:0`, ffmpeg's default stream selection can silently pull in a subtitle track, which has no fast-seek and can hang; without `-map_chapters -1`, the mp4 muxer copies the source's chapter list into the output as a stray data track regardless of `-map`, leaking the full film's duration into a supposedly-short clip's metadata.
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
- Install goal: clone repo → copy `.env.example` to `.env` → run the setup wizard (or hand-edit `config.yaml` before the wizard exists) → `docker compose up`.
- **✅ Image size: static ffmpeg binaries instead of Debian's apt package (960MB → 706MB), then trimmed `uvicorn[standard]` down to just its two used pieces + fixed `uvloop` never actually being active (706MB → 701MB).** Both changes verified against real media/usage, not just a successful build. Full writeup: `docs/build-notes/docker-image.md`.

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

**Python**: `discord.py`/`Pycord` for the bot layer, `python-plexapi` for Plex, `subprocess` calls to the ffmpeg CLI directly, `rapidfuzz` for subtitle matching, FastAPI for the internal worker API, SQLite for the small config/cache store.

## 13. Staged development plan

**MVP — ✅ complete**
- Single `/gif` command (renamed `/cinesnip` going into V2 — see Section 2), timecode input only, films only
- Plex search + confirm via buttons (single library/mapping to start — this developer's primary Movies library)
- Direct file access only
- One fixed GIF style, no options menu
- One Docker container, tested against one Discord server

See `docs/build-notes/ffmpeg-rendering.md` before touching
`app/worker/ffmpeg.py` — the quote-search rendering path reuses the same
seek/duration logic.

**V2**
- ✅ Rename `/gif` → `/cinesnip` (and `/gif-diagnose` → `/cinesnip-diagnose`)
- ✅ Sidecar-subtitle-file + embedded-subtitle-stream extraction (generic — not Bazarr-specific, see Section 5) + fuzzy quote search within a confirmed film
- ✅ `/snip-search` (**Tier 1 only**) — library-wide quote search across the (lazily-built) subtitle cache, backed by a small SQLite index (`cache/quote_index.db`, `app/worker/quote_index.py`) mapping `guid → rating_key/title/library_name` so cached titles can be enumerated without a live Plex call. Matching lives in `app/worker/library_search.py`; selecting a result funnels into the existing quote-confirm → render flow (decision #4, `LibrarySearchView` in `app/bot/cogs/gif.py`). Verified against the real library. **The explicit opt-in to extend a search into not-yet-cached titles (Section 5's Tier 2) is not yet built** — deliberately deferred until it can show real progress/ETA rather than a silent hang.
- ✅ Subtitle burn-in + style preset select menu (Section 7)
- ✅ MP4/WebM output option (`format:` on `/snip`/`/snip-tv`, default gif — see decision #1)
- ✅ Multi-library support: `config.yaml`'s `libraries` list replaces the old single `movies_library_name`/flat `path_mappings`, each library owning its own mappings (`app/settings.py`'s `LibraryConfig`/`path_mappings_for()`). `PlexClient` searches every configured movie-type section and tags each result with `library_name` (`movie.librarySectionTitle`, populated natively by plexapi — no extra lookup needed); `/resolve`, `/render`, and subtitle extraction all resolve container paths against that specific library's mappings instead of one global list. Surfaced to the user as a lightweight label in the autocomplete picker and the "Searching.../Generating..." status text (decision #4), not a new confirmation step. This developer's real `config.yaml` now covers all three of their actual libraries (Movies, 3D, TV Shows — see above; no 4K library exists here to configure). TV episode search itself is still V3 (Section 4) — only its path mappings are wired up now.
- ✅ Document + confirm "Public Bot" disabled as part of Discord setup (Section 9)

**V3 — phased, each phase ordered by what it depends on, not just priority**
- **✅ Phase 1 — 3D crop/format handling** (Section 3). `three_d_format` on `LibraryConfig` plus a crop step in `ClipRenderer`'s filter graph, with per-file tag auto-detection overriding the library default (see Section 3 for the full writeup and the mixed-packing finding). Verified against this developer's real `3D` library.
- **✅ Phase 2 — TV show support** (episode-specific and whole-show search, `/snip-tv` — Section 4 for the full writeup, including a real cross-media quote_index leak this surfaced and fixed). Also folded in the `/cinesnip` → `/snip` command consolidation (Section 2, decision list). Verified against this developer's real `TV Shows` library.
- **❌ Whisper fallback and NVENC/GPU acceleration — dropped, not deferred.** Previously planned as Phase 3/Phase 4 here. Cut per decision #7: the sidecar-priority check already gives users a free escape hatch for the no-subtitle tail case (~6.7% of this developer's real library), and cutting Whisper removed GPU's only stated justification too. Not revisited unless that trade-off changes.
- **Phase 3 — Local web app: setup wizard + manual generation UI** (decision #6, Section 14 for the wizard spec). By this point the core pipeline (films + TV) is feature-complete, so the wizard/generation UI expose the full feature set rather than a partial one. Also the natural home for a real progress/ETA UI (a web page can show a progress bar; Discord can only edit a message) — which Phase 4 depends on.
- **Phase 4 — `/snip-search` Tier 2** (Section 5: extend search to not-yet-cached titles). Deliberately deferred until a real progress/ETA UX exists rather than a silent multi-minute hang — Phase 3 is what builds that, so Tier 2 reuses it instead of inventing a throwaway Discord-only progress hack.
- **Phase 5 — Allowlists/rate-limiting, multi-server polish, distribution docs** for other self-hosters (both Plex-hosting patterns documented). Final readiness pass, once the wizard (the actual onboarding path for other installers) and the full feature set both exist to document.

### Non-obvious bugs found building the render/subtitle pipeline

Four rounds of real bugs across the MVP and V2 builds — ffmpeg subprocess
gotchas, subtitle-extraction edge cases, MP4/WebM stream-mapping leaks, and
font/ASS-styling mistakes that a successful render doesn't catch. Read
**`docs/build-notes/ffmpeg-rendering.md`** (ffmpeg/rendering-specific) and
**`docs/build-notes/subtitles-and-search.md`** (extraction-specific) before
touching `app/worker/ffmpeg.py`, `app/worker/subtitle_render.py`, or
`app/worker/subtitles.py` — several of these look like they'd "just work"
and don't.

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
7. **Library auto-sync toggle** (`library_sync` is built — see Section 5 and `docs/build-notes/subtitles-and-search.md`):
   a real on/off switch plus an interval input for `library_sync.enabled`/
   `interval_hours`, not a config-file-only feature forever. Must carry the
   same deletion warning as the README's writeup — this is real background
   work that can delete cache entries, so the wizard shouldn't let someone
   flip it on without understanding that.

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
