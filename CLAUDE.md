# CineSnip — Technical Architecture & Development Plan
*Living reference for Claude Code sessions — also usable as the project's CLAUDE.md*

## Project summary

A self-hosted Discord bot + local web app that lets you search your own Plex library (films and TV shows) and generate a short GIF or MP4/WebM clip from a spoken quote or timestamp, posted back into Discord. Designed to be installable by other self-hosted Plex users, not just for personal use — but every installation is fully independent: no central service, no shared infrastructure, no data of any kind passing through anyone but the installer's own Plex/Discord.

Inspired by conversation with the developer of [tvgif](https://github.com/warmans/tvgif), a similar but structurally different tool (tvgif pre-converts a whole library to webm+srt ahead of time; this project integrates live with Plex and extracts on demand). **No code should be copied from tvgif** — standard/boilerplate ffmpeg or HTTP patterns are fine since they're not anyone's IP, but the implementation should be built independently out of respect for that developer.

## This developer's own setup (used as the reference/test environment)

- Plex runs **natively on Windows** (not Dockerized) on a separate **server PC** from the one used for development.
- Docker (Docker Desktop, WSL2 backend) also runs on that server PC, alongside other unrelated services already running in Docker there.
- Server PC has an **RTX 3070** — usable for NVENC encode/decode and for speeding up local Whisper transcription, but must remain optional in the base install since not every installer will have a GPU.
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
5. **Config split**: `.env` for secrets (Discord bot token, Plex token), `config.yaml` for everything else (per-library path mappings, style presets, GPU toggle, feature flags). Standard self-hosted-app practice — keeps secrets out of anything that might get shared or hand-edited casually.
6. **Setup wizard and manual-generation UI are the same small local web app**, not two separate builds — a `/setup` route for first-run/reconfiguration, a `/generate` route for browsing/making clips outside Discord. Build the Discord bot and prove the core pipeline first; build this web app as a fast-follow, not alongside. Full spec for the wizard itself: see Section 14.

## 1. Overall architecture

- **Discord layer** — Gateway connection, slash commands, buttons, select menus, modals. Talks to the worker via local HTTP calls to its internal API.
- **Worker layer** — Plex API calls, subtitle lookup/extraction, fuzzy quote search, optional Whisper transcription (cached), ffmpeg orchestration. Exposes a small internal REST API (e.g. `/search`, `/resolve-quote`, `/render`).
- **Local config store** — SQLite or a flat file for anything not covered by `.env`/`config.yaml` that needs to persist at runtime (e.g. per-guild allowlists once that's built).
- **Temp processing directory** — scratch space for in-progress renders, cleared aggressively; the `/render` endpoint should stream ffmpeg's output directly rather than always writing a file to disk first.
- **Transcript/subtitle cache** — persisted per Plex media GUID, so a film's subtitles (or a Whisper transcript) are only ever extracted/generated once. This cache doubles as the corpus for library-wide quote search (Section 5) — it's built up lazily/opportunistically as titles get touched by any flow, never by proactively indexing the whole library up front. A small SQLite index (`cache/quote_index.db`, `app/worker/quote_index.py`) tracks which titles are cached (`guid → rating_key/title/library_name`) so `/snip-search` can enumerate them directly instead of scanning/parsing every cache file — purely a derived, rebuildable pointer table; the JSON cache files remain the only place subtitle text itself lives. Shared with TV episodes too (both flow through the same generic `/render`/`/resolve-quote` — Section 4), so `/snip-search`'s own handler filters back down to movie-only libraries at read time rather than the index itself being movie-only.

No central database, no cloud API, no message broker.

## 2. Discord bot UX

- **Renamed from `/gif` to `/cinesnip`, then consolidated onto `/snip`** (the former decided going into V2, the latter decided going into V3 Phase 2 alongside TV support). `/gif` → `/cinesnip` reasoning still applies to the *shape* of the names (Discord slash commands are namespaced per-application, so there's no technical collision risk between bots — but a server with two bots that both register the same short command name shows a disambiguation picker before the user can act, which is real friction). The further consolidation reasoning: `/snip` had existed alongside `/cinesnip` since V2 as a same-behavior shorter alternative (see below); once real daily use showed the shorter name was what actually got typed, keeping both names registered was pure redundancy rather than genuine choice — so `/cinesnip` was dropped outright (not kept as a deprecated alias) and `/snip` became the sole primary command. `/cinesnip-search` → `/snip-search` for the same reason. (`/cinesnip-diagnose`, mentioned in Section 3, was never actually built, so there was nothing to rename there — any future diagnostic command should launch directly as `/snip-diagnose`.)
- `/snip film:<text> quote:<text-optional> timecode:<text-optional> end_timecode:<text-optional>` for films. `film` is required — this is the film-first flow: confirm the title, then confirm a timestamp within it (via quote search or direct timecode). `end_timecode` only applies with `timecode` (not `quote`, which always uses the matched line's own span — Section 7) and lets the user pick a custom clip length instead of the fixed default; requests outside `render_defaults.min/max_duration_seconds` get a clear error rather than a silent clamp.
- **✅ `/snip-tv show:<text> season:<int-optional> episode:<int-optional> quote:<text-optional> timecode:<text-optional> end_timecode:<text-optional>`** (V3 Phase 2) — the TV equivalent, one layer deeper: `show` (autocomplete) is required; `season`/`episode` must be given together or not at all. With both given, behaves exactly like `/snip` (decision #4's no-confirm-step reasoning applies identically — autocomplete + explicit season/episode already pins an exact episode) by resolving straight to a rating_key via the worker's `/resolve-episode` and handing off to the *same* `GifCog._generate()` films already use — see Section 4 for why this needed almost no new render/quote-search code. Without `season`/`episode`, `quote` searches across the whole show instead (a bare `timecode` with no episode is rejected — there's no "the show's timeline" to seek within); see Section 4 for the on-demand search design.
- **Timecode input accepts more than `HH:MM:SS`**: colon forms (`1:23:45`, `83`) still work, and so do unit-suffixed forms in any subset of hours/minutes/seconds — `1h22m12s`, `22min 12sec`, `1hr22min2sec`, etc. (`parse_timecode()` in `app/worker/ffmpeg.py`). Applies everywhere a timecode string is accepted (`timecode`, `end_timecode`).
- **✅ `/snip-search quote:<text>`** — a separate, dedicated command for **library-wide** quote search (added in V2, see Section 5 for the indexing/caching design behind it; Tier 1 only — see Section 5 for what's still not built). Deliberately a different command, not a "no film given" fallback on `/snip`: the result shape is different (candidates span many titles, each needs its own title/library/quality label, not just surrounding-line context within one film) and the performance profile is different (see Section 5). The dropdown itself shows each candidate's actual matched line as the option label (`Title — Library · timecode · score%` as the description underneath) — picking a line to look for, not just a film, is the point of a quote search. Selecting a result from `/snip-search` (`LibrarySearchView` in `app/bot/cogs/gif.py`) funnels into the same "confirm the film" → "confirm the timestamp" cheap-redo steps as the normal flow (decision #4) by calling straight into `GifCog._generate()` — it's just a different on-ramp into the same two steps, not a separate confirmation UI. **✅ Fixed: the specific line picked in the dropdown wasn't actually what got confirmed.** `_generate()`'s quote branch re-runs a fresh per-film search on the raw quote text (by design — a real per-film re-search, not just a rubber-stamp) and used to always default `QuoteMatchView` to *that fresh search's* top-ranked candidate — which isn't necessarily the same line the library search ranked top, since the two searches use different per-title breadth (library search's diversity cap vs. the per-film search's full candidate list). Confirmed on the real library: picking Snatch's "He'll do you proud, governor." (09:41) from the search dropdown silently rendered the unrelated top-ranked "Hello?" line (03:38) instead, because the user only sees this if they read the confirm screen closely — clicking Confirm on the shown default is the whole point of that step. Fixed by threading the selected match's `start` time through as `_generate(..., preferred_start=...)`, which now picks the closest-matching line in the fresh per-film results as `QuoteMatchView`'s initial selection instead of defaulting to index 0 (`initial_index` param, `app/bot/cogs/gif.py`). `LibrarySearchView` is now also reused as-is by `/snip-tv`'s whole-show search (an episode's already-formatted "Show — S02E01 — Title" display title needed no separate label format).
- **Autocomplete on `film`/`show`**: as the user types, query the local Plex library (via the worker's `/search` for films, `/search-shows` for TV) and return up to 25 matches — Discord's autocomplete cap.
- **No film-confirmation embed** (see decision #4) — the picked film's title is folded into the "Searching subtitles…"/"Generating…" status message instead of a separate click-gated step. Revisit alongside multi-library search.
- **Quote search results**: top match plus surrounding subtitle context and a confidence indicator; Confirm or "show other matches" (select menu of alternatives) if confidence is borderline.
- **✅ Options step, applied after generation rather than gating it**: select menu of style presets (Classic / Boxed / Cinematic / Meme / Original / No Subtitles) rather than exposing every parameter. Originally specced as a step *before* generating (pick a style, then hit Generate) — real usage showed that added a click to every single command for no benefit when the pre-picked default was already right most of the time. Now: generate immediately with a sensible default (Classic for a quote match, No Subtitles for a bare timecode), show the result, and the same style dropdown sits *below* it — changing it re-renders in place (swaps the attachment on the same message) without restarting the command, and "Post to channel" always posts whatever's currently shown. A modal for advanced overrides (duration/fps/crop) is still a separate, not-yet-built idea (Section 7's interactive clip editor).
- **Progress handling**: acknowledge within Discord's 3-second window with a deferred "Generating…" response, then edit that message once ffmpeg finishes.
- **Visibility**: default ephemeral for the search/confirm/options steps, explicit "Post to channel" button on the final result.
- **✅ Upfront warning before a likely-slow cold subtitle extraction**, closing a real UX gap: a single-title quote search or a styled render could silently take several minutes on a cold cache with no sidecar (Section 5/6's no-fast-seek finding — a real 251.6s case on this developer's library), with the status message never hinting that was coming, unlike `/snip-tv`'s whole-show search (which already warns "this can take a moment for episodes not seen before"). New worker endpoint `GET /subtitle-status/{rating_key}` (`app/worker/api.py`) answers cheaply — no ffmpeg involved, just a sidecar-file check plus a cache lookup — whether a request is about to fall through to a cold embedded extraction. `_slow_subtitle_warning()` (`app/bot/cogs/gif.py`) calls it best-effort (a failure never blocks the real search/render, just means no warning shows) and folds the result into the existing status text. Wired into both places this cost can actually be triggered: `GifCog._generate()`'s quote branch (covers `/snip`, `/snip-tv`'s direct-episode quote path, and both search-result selection flows, since they all funnel through `_generate()`) and `ClipResultView`'s style-change handler (a style picked *after* an initial bare-timecode render can be the first request that actually needs that title's subtitles — same risk, different route in). Verified against the real library: a title with a sidecar (Snatch) correctly gets no warning; the slow-detection logic itself confirmed correct against synthetic no-sidecar/no-cache inputs (real-library sampling kept finding sidecars, per Section 8's build notes on this library's near-universal Bazarr coverage, so a genuine real-world "likely_slow: true" case proved hard to find here specifically — the logic is verified, even though a fully organic real trigger wasn't).

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
- **✅ 3D format handling (V3 Phase 1).** `LibraryConfig.three_d_format` (`app/settings.py`, default `"none"`) tags a library's packing (`side_by_side` | `over_under`); `ClipRenderer` (`app/worker/ffmpeg.py`) inserts a crop-to-one-eye step (`crop=iw/2:ih:0:0` for side-by-side-left, `crop=iw:ih/2:0:0` for over-under-top) into the filter graph before scaling/subtitles whenever a library is so tagged, and `_write_ass_file` adjusts the source dimensions it computes `PlayResY` from to match the post-crop single-eye frame, not the raw packed one — otherwise burned-in subtitle text would be the wrong size for a 3D clip specifically. **Real per-file packing varies within one library, confirmed against this developer's own `3D` library**: a "Full-SBS" *Ready Player One* release carries an actual Matroska `StereoMode` tag (`ffprobe`'s `stream_side_data_list`, type `"side by side"`), but *Dune (2021)* in the same library has no such tag at all despite being (visually confirmed via a cropped still frame) over/under. A single library-wide config value can't be right for both, so `probe_stereo_format()` checks each file's own tag first and overrides the library default when present, falling back to the configured default only for untagged files (which are common) — solving the mixed-packing problem without a per-file config entry. The probe only runs for libraries already tagged 3D at all, to avoid an extra `ffprobe` call on every render for normal libraries. There's still no per-request override for an untagged file whose actual packing disagrees with its library's default — not needed yet since this developer's only untagged title matches the configured default, but a real gap if that stops being true. Verified end-to-end against both real files: rendered frames for both are correctly proportioned, not squished/doubled.
  - **✅ Fixed real bug found in live Discord testing right after shipping the above: no longer doubled, but stretched.** The *Ready Player One* Full-SBS file tags its packed 3840x1080 frame with sample aspect ratio 2:1 (`ffprobe` confirmed) — describing the combined stereo pair, not a single eye. ffmpeg's `crop` filter carries that SAR over unchanged onto the cropped 1920x1080 single eye, which then reports (and plays back) a stretched 32:9 DAR instead of the correct 16:9; a single eye's own pixels are actually square once isolated. Fixed with `setsar=1` inserted immediately after the crop filter, before scale — `_scale_and_subtitle_filter()` in `app/worker/ffmpeg.py`. Confirmed via `ffprobe` on a real rendered frame (SAR 2:1/DAR 32:9 → SAR 1:1/DAR 16:9) and visually. **Lesson for next time**: "not squished/doubled anymore" was necessary but not sufficient verification for a 3D fix — check the actual DAR of a rendered frame with `ffprobe`, not just that the framing looks roughly right by eye, since a stretched-but-still-single-eye frame can look deceptively close to correct in a quick visual scan (this one did, in the original verification pass).
  - **✅ Fixed a second, more fundamental real bug reported right after the SAR fix: still stretched, but only for Dune, not Ready Player One.** The SAR fix made the *packed frame's own tag* stop lying about the cropped eye's aspect ratio, but that's a different bug from the one actually affecting Dune: a 3D pack comes in two flavors per axis — "full" (each eye stored at native resolution, so the packed frame is simply double-width/double-height, and a crop is all that's needed) and "half"/squeezed (each eye compressed to fit within a normal single-frame canvas — the far more common space-saving rip format — needing a 2x unsqueeze stretch back to native size *after* cropping, which the original fix never did). Neither real file self-tags which flavor it is, but `ffprobe -vf cropdetect` on Dune's cropped top half found a `1920x402` content box (ratio 4.78, impossible for any real film) that becomes `1920x804` (2.39:1, Cinemascope) once doubled — proof it's squeezed. Ready Player One's Full-SBS pack, by contrast, is genuinely native resolution per eye (double-width 3840x1080 packed frame) and needs no unsqueeze. `_three_d_plan()` in `app/worker/ffmpeg.py` now distinguishes the two purely from the packed frame's own raw pixel aspect ratio — a real single flat frame is never as wide (side-by-side) or as tall/square (over-under) as a full pack, so crossing that ratio can only mean two native-resolution eyes, never two squeezed ones — and inserts a `scale=iw*2:ih` (side-by-side) / `scale=iw:ih*2` (over-under) unsqueeze step between crop and `setsar=1` for the squeezed case. Verified against both real files: Ready Player One still classifies as full (crop only), Dune now classifies as squeezed (crop + unsqueeze) and its rendered output's own content box (via `cropdetect` on the *output*, not just the source) reads 2.4:1, not the previous 4.78:1. **Lesson for next time, sharper than the one above**: two consecutive "looks basically right" visual checks on Dune's cropped frame (once in the original 3D verification pass, once implicitly when only Ready Player One was suspected) both missed a 2x vertical squish — `ffprobe cropdetect`'s numeric content-box ratio is what actually caught it, not eyeballing a still frame. Trust the numbers over the glance for anything aspect-ratio-related in this pipeline going forward.
- **Diagnostics**: worth building a `/snip-diagnose` (or web-app equivalent) command that reports what paths the container sees vs. what Plex reports, since path-mapping issues are the single most likely install-time failure for other users.
- **✅ Fixed: redundant Plex fetches.** A single `/snip film:X quote:Y` invocation used to re-fetch the same Plex movie metadata via `PlexClient.get_movie()` three separate times (once each in `/resolve`, `/resolve-quote`, `/render` — each a real network round-trip to Plex). Fixed with a short-lived (30s) in-process cache in `PlexClient` keyed by `rating_key` — long enough to dedupe the handful of seconds between one command's own three calls, short enough that a retitled/deleted item doesn't linger. Kept inside the worker (no new fields on the HTTP API), matching the Section 9 principle of keeping media access confined to the worker. Verified against the real library: 3x `get_movie()` calls on the same `rating_key` now hit Plex once, not three times.

## 4. TV show support

- Plex structures TV as Show → Season → Episode; treat this as one extra layer on top of the film flow, not a separate system.
- Two supported patterns:
  - User specifies an episode directly (`/snip-tv show:The Office season:2 episode:1 quote:"that's what she said"`) — same narrow, fast flow as films.
  - User gives a show + quote with no episode — search across that show's episodes for the line. Slower (more subtitle files to check) and should be on-demand only, never pre-indexing an entire show's library proactively.
- **✅ Built (V3 Phase 2).** The key finding that shaped the implementation: the worker's `/resolve`, `/render`, and `/resolve-quote` endpoints (`app/worker/api.py`) were already 100% generic over a Plex `rating_key` — they call `PlexServer.fetchItem()` (type-agnostic) and only ever touch fields (`title`, `guid`, `plex_path`, `library_name`, `duration_ms`) that exist identically on a plexapi `Episode` as on a `Movie`. So TV support didn't need a parallel render/quote-search pipeline — it needed a thin layer that resolves `show + season + episode` down to a `rating_key`, after which the entire existing pipeline (including 3D handling, subtitle degrade-to-none, the quote_index cache, style presets) works completely unchanged.
  - `PlexClient` (`app/worker/plex_client.py`): `_to_result()` now branches on `item.TYPE` — an Episode gets its `MovieResult.title` formatted as `"Show — S02E01 — Episode Title"` (folding show/season/episode into the one field every downstream consumer already displays, since `MovieResult` has no separate show/season/episode fields to draw on) and `year=None` (episodes have no year of their own). New `get_episode(show_rating_key, season, episode)` and `list_episodes(show_rating_key)` wrap plexapi's `show.episode(season=, episode=)` / `show.episodes()` (season 0 specials included natively, no filtering needed); both raise a clean `EpisodeNotFoundError`/`ShowNotFoundError` on an invalid combination rather than propagating plexapi's `NotFound`.
  - Worker API: `GET /search-shows` (autocomplete), `GET /resolve-episode/{show_rating_key}?season=&episode=` (the show+season+episode → rating_key resolve step — the only genuinely new thing the render/quote pipeline needed), and `GET /search-episodes-quote/{show_rating_key}?quote=` (whole-show search: lists the show's episodes live via Plex, extracts+caches subtitles inline for any not-yet-cached episode — sequentially, catching and skipping a broken/unmapped episode file rather than failing the whole request — then calls `search_cached_library()` **completely unchanged**, since that function was already generic over any `list[CachedTitle]`). Whole-show search is deliberately inline/synchronous rather than needing Section 5's still-unbuilt Tier 2 progress UI, since a show's episode count is small compared to a whole library.
  - **✅ Fixed a real bug found during verification against the real library, not caught by design review: `/snip-search` (movie-only per its own docs — "every film") started leaking TV episodes into its results.** Root cause: `/render` and `/resolve-quote` write into the same shared `cache/quote_index.db` regardless of media type (by design, per the point above — they don't know or care that they're generic), so once `/snip-tv` was exercised against a real episode, that episode's `guid → rating_key/title/library_name` row landed in the exact same table `/snip-search`'s Tier 1 reads from. Confirmed on the real library: searching a phrase from a cached *Office* episode came back listed as a `/snip-search` movie result, library-tagged `TV Shows`. Fixed by exposing `PlexClient.movie_library_names` (the set of section titles whose type is `"movie"`) and filtering `/search-quote`'s handler to just those library names at read time — no schema change/migration needed, since the index itself staying shared is harmless (and may be useful for a future combined search) as long as the movie-only endpoint filters itself back down. **Lesson for next time**: reusing a generic pipeline across two media types means any *shared, implicit* state that pipeline touches (here, an index table neither `/render` nor `/resolve-quote` was ever told to scope by media type) needs an explicit audit — this one wasn't in the original design plan, only surfaced by actually running `/snip-search` against real TV+movie data together, not by code review alone.
  - Verified end-to-end against the real library: `/resolve-episode` on a real show/season/episode resolves correctly; a rendered clip from a specific episode was confirmed via an extracted still frame (not just "it rendered") to actually be that episode; whole-show quote search correctly extracted subtitles inline for several not-yet-cached episodes of a real show in ~1s (sidecar `.srt` files, no ffmpeg needed) and ranked matches across episodes; invalid season/episode and invalid show rating_keys return clean 404s, not stack traces; the post-fix `/snip-search` no longer returns TV episodes.

## 5. Finding dialogue automatically

- **Subtitle source priority** (deliberately generic — must work for installs that don't use Bazarr and/or have only embedded subs, not just this developer's setup):
  1. Look for a separate subtitle file matching the video's filename in the same folder — this covers Bazarr's naming convention (this developer's setup) but is really just "does a sidecar `.srt` exist," which works identically for anyone who drops subtitle files next to their videos by any means, manual or tool-managed. No ffmpeg extraction needed, just read the file.
  2. If none found, check for and extract an embedded subtitle stream (`ffmpeg -map 0:s:N`) — this is the *primary* path for installs without a sidecar-subtitle tool, not a rare fallback, so it needs to be genuinely well-supported in V2, not an afterthought bolted on later.
  3. If neither exists, fall back to local Whisper transcription (`faster-whisper`, **V3** — see Section 13) — genuinely slow (minutes for a full film on CPU, much faster with the 3070 via GPU-enabled build), so **transcribe once per title and cache the result** keyed by the Plex media GUID; never re-run per query. Until V3 ships, a title with neither a sidecar file nor an embedded stream simply isn't quote-searchable — timecode input still works via `/snip`/`/snip-tv`, this is an expected, documented V2 gap, not a bug.
- **Matching**: parse whichever subtitle source into `(start, end, text)` entries; fuzzy-match the typed quote (`rapidfuzz`) after normalizing case/punctuation; return the top candidates (`quote_match.candidate_limit`, 8 by default) with surrounding context and a confidence score rather than committing silently to the top hit.
- **✅ Fixed a real ranking bug found via `/snip-tv`'s whole-show search on the real library: a literal match didn't reliably outrank a non-matching line.** Searching "Hitler" across Peep Show returned several lines that don't contain the word ranked ahead of ones that do — `fuzz.WRatio`'s length-normalized scoring can dilute a short quote buried in a longer line below an unrelated same-length line that merely shares similar letters (single short-word quotes are the case this bites hardest, since there's little other text to anchor the ratio). Fixed in `find_quote_matches()` (`app/worker/quotes.py`): after the normal WRatio pass, any candidate containing the normalized quote as a whole-word substring (`\bquote\b` on the same normalized text WRatio already scores) is force-scored to 100, guaranteeing every literal match sorts ahead of every fuzzy-only one — this also picks up literal matches WRatio itself had scored below `min_score`. Word-boundary-checked deliberately (not a raw substring check) so a quote like "cat" doesn't get force-scored off appearing inside "concatenated". Verified against the real library: re-running the exact reported search now returns every line actually containing "Hitler" first (all scoring 100), with the one genuinely non-matching fuzzy result last.
- **✅ Follow-on polish: a directional partial word-overlap bonus, added deliberately without `rapidfuzz.fuzz.token_set_ratio`.** The literal-match fix above only catches a quote that appears as a contiguous phrase — it misses a multi-word quote whose words are all present in a candidate but out of order or interleaved with other words (e.g. searching a scrambled "manager regional the to assistant" should still find "Assistant to the Regional Manager"). The obvious fix — blend in `token_set_ratio`, which is word-set-aware — was tried and rejected: it's symmetric, so it scores a *short candidate that's a strict word-subset of a much longer quote* as a perfect 100 too (confirmed: `token_set_ratio("i am", "i am your father") == 100.0`), which would rank an incomplete/truncated match as good as a real one — reintroducing the exact class of bug the literal-match fix exists to prevent, just from the opposite direction. `find_quote_matches()` instead scores what fraction of the *quote's* words appear as whole words in the candidate (never the reverse), and only awards a bonus (scaling 60→95 as overlap goes 50%→100%) once a majority of the quote's words are present — deliberately capped below the literal fix's 100 in the results, since a real contiguous phrase is stronger evidence than scattered words. Guarded by a regression test asserting a truncated candidate can never tie or beat a genuine full match. Verified against the real library: the scrambled Office search now correctly ranks both real "Assistant to the Regional Manager" lines (95.0) above unrelated lines (85.5), with no change to the existing Hitler search's ranking.
- **✅ Real correctness bug only found by testing against the fully-built library cache — invisible at small scale.** `scripts/build_full_cache.py` (a one-off utility, see its docstring — not part of the running app, and effectively a working prototype of Phase 6's "manual cache build" idea below) extracted and cached subtitles for all 11,463 titles in this developer's real library (10,672 sidecar, 13 embedded, 764 with no usable subtitles, 14 skipped on a path-mapping issue affecting a handful of titles with `\\?\`-prefixed UNC paths — a real, separate gap worth fixing in `path_mapper.py`, not yet done — 0 errors, 29.5 minutes total). Searching that fully-built cache for "Assistant to the Regional Manager" (a real Office quote, but `/snip-search` is movie-only) returned nonsense at the top: `"to the"`, `"manage."`, `"MANAGER"` — bare fragments sharing at most one real word with the quote — all scoring 90, ahead of every genuine partial match. Root cause: `fuzz.WRatio` *itself* (not the overlap bonus above) can score a short candidate sharing only a word or two with a much longer quote surprisingly high, via its internal partial-ratio weighting for large length-ratio pairs. Confirmed directly: `WRatio("assistant to the regional manager", "to the") == 90.0`. This was always present but invisible below full-library scale — a small cache simply doesn't contain enough short, coincidentally-overlapping fragments to expose it; scale is what turned a latent quirk into visible, ranking-dominating noise. Fixed by extending the same directional word-overlap fraction already computed for the bonus above into a **cap**, not just a bonus: below the 50%-overlap threshold, a candidate's score is now capped at `overlap * 60` instead of trusting WRatio's raw number, pushing any candidate missing most of the quote's real words below `min_score` and out of results entirely, regardless of what WRatio's character-level score claims. Two existing tests had unknowingly encoded this exact bug as "expected" (a zero-real-word-overlap "weak match" that only appeared because of it) and needed updating once the bug was understood, not just made to pass. Verified against the real 11,463-title cache: the nonsense fragments are gone; remaining results now all genuinely share "manager"/"assistant" as real words; the actual literal Office line (via `/search-episodes-quote`, the correct movies-vs-TV path for this quote) still scores 100 and ranks first, unaffected.
- **Confirmed, not just theorized: `/snip-search`'s "instant regardless of library size" promise genuinely breaks down at real full-library scale.** A single search against the fully-built 11,463-title cache took **~40 seconds** — dominated by the fuzzy-scoring step against every cached title, which is inherently proportional to corpus size and can't be cached away (unlike the normalize/build-candidates step fixed above). Not an urgent problem today — this developer's library isn't kept fully built this way day-to-day, the cache above was a deliberate one-off stress test — but real, confirmed evidence for whoever picks up Phase 6: a smarter pre-filter/index ahead of the expensive per-candidate fuzzy scoring is a real, not hypothetical, requirement at this scale, not merely a caching problem.

### Planned (not yet built): consolidate the subtitle cache into SQLite + FTS5

Design agreed after prototyping against the real fully-built cache. **Not implemented** — recorded here so the measurements and the reasoning aren't lost. Sequence it before or alongside Phase 6, since Phase 6's manual full-cache build is exactly what makes today's design hurt.

**Where the time actually goes now** (measured per-title, mirroring `search_cached_library()`'s real loop, against the real 10,679-title cache — 7,511,478 subtitle entries, 13,971,474 candidates):

| Step | Time | Share |
| --- | --- | --- |
| Raw subtitle JSON read | 15.8s | 14% |
| Candidates JSON read (deserialising 14M objects) | 49.1s | 42% |
| Fuzzy scoring (14M `WRatio` calls) | 50.9s | 44% |

(That 115.8s is scanning *everything*; the observed ~40s for `/snip-search` is because it filters to movie-type libraries only, ~35% of the corpus. TV is unaffected in practice — `/snip-tv`'s whole-show search only ever scans one show, measured at 0.68s.)

**The non-obvious finding: the disk-based candidates cache inverts at scale.** It was a clear win at 216 titles (4.78s → 2.94s, verified above), because it traded CPU-bound normalise/build work for a disk read. But at 10,679 titles, deserialising 14M candidate objects out of JSON costs *as much as the fuzzy scoring itself*. Both of those costs are O(corpus) and neither can be cached away by doing more of the same thing — the only fix is to stop touching the whole corpus per search.

**Design: one SQLite DB with an FTS5 inverted index as a pre-filter.**

```sql
CREATE TABLE titles(title_id INTEGER PRIMARY KEY, guid TEXT UNIQUE NOT NULL, ...);
CREATE TABLE entries(
  id INTEGER PRIMARY KEY, title_id INTEGER NOT NULL, idx INTEGER NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL, display_text TEXT NOT NULL);
CREATE VIRTUAL TABLE entries_fts USING fts5(normalized_text, content='');
```

Query flow: normalise the quote → `MATCH` its OR'd tokens against `entries_fts`, `ORDER BY rank LIMIT ~4000` (BM25 does the coarse ranking) → fetch only those rows → recompute `normalized_text` and build adjacent-cue windows *for the survivors only* → run the existing `find_quote_matches()` scoring/literal-boost/overlap-cap logic unchanged on that narrow set.

Three consequences worth being explicit about:
- **The candidates cache disappears entirely** — no precomputed candidates stored at all (2.5GB of today's 3.6GB). Windows get built on the fly for a few thousand rows, which is trivial.
- **`normalized_text` is never stored** — contentless FTS5 (`content=''`) keeps only the inverted index, and normalisation is recomputed for survivors, which is cheap at that size.
- **The existing `quote_index.db` folds into the same DB** as the `titles` table, rather than being a separate SQLite file alongside JSON.

**Measured on a real 1,000-title / 1.3M-entry prototype built from the actual cache:**

| | Today (JSON) | Prototype (SQLite+FTS5) |
| --- | --- | --- |
| Size | 3.6 GB (22,121 files) | **1.57 GB** (1 file, extrapolated from 146.9 KB/title) |
| Search | ~40s (movies) / ~116s (all) | **2ms – 740ms end-to-end** |
| Narrowing | scores all 14M candidates | 0.016%–0.3% of entries reach scoring |

Correctness held on the prototype: "May the Force be with you" → 100.0, "Here's Johnny!" → 100.0, "Assistant to the Regional Manager" → 95.0. Normalising `guid` into a `title_id` FK is worth a full 1GB on its own (2.57GB → 1.57GB) — a ~40-char GUID repeated across 7.5M rows is not free.

**The real risk, and it is a genuine behaviour change, not just a speedup:** an index pre-filter changes *which* results come back, and this app's whole promise is *fuzzy* matching. FTS5 matches whole tokens, so a typo'd word simply is not in the index. Measured recall:

| Query | Indexed hits |
| --- | --- |
| `here's johnny` (exact) | 2,289 |
| `here's jonny` (1-word typo, multi-word) | 1,348 — **fine**, other tokens carry it |
| `hitler` (single word, exact) | 170 |
| `hitlr` (single word, typo) | **0 — total miss** |
| `may the forse be with you` | 509,137 (common tokens match broadly; BM25 + LIMIT handles it) |

So multi-word queries tolerate typos well, but a **single-word typo'd query returns nothing**, where today's full fuzzy scan would still find it. Mitigation: **fall back to the existing full scan when FTS5 returns zero/near-zero hits** — costs the old ~40s only in the rare case the fast path found nothing, and preserves 100% of current recall. This fallback is required, not optional — two other ways to avoid needing it were tested directly and both ruled out:
  - **The `trigram` tokenizer does not help.** Tested directly against the real cache expecting it to be more typo-tolerant — it isn't. It does exact substring matching (finds `"manag"` inside `"manager"`), not fuzzy matching: `"hitlr"` still returns 0 hits with a trigram-tokenized index, identical to the word tokenizer. Confirmed via a real 1,000-title prototype (187 KB/title, ~2GB extrapolated — also larger than the word-tokenized index for no typo-tolerance benefit). Don't reach for it to solve this problem; it solves a different one (mid-word substring search).
  - **Parallelizing the existing full scan across threads does not help either.** Tested with `ThreadPoolExecutor` at 4/8/16 workers against the real 10,679-title scoring step (a 16-core host): zero speedup, slightly negative from overhead — `rapidfuzz`'s batch scoring doesn't release the GIL enough for this to matter. Real multiprocessing would parallelize it (separate processes, no shared GIL), and is worth naming as a genuine complementary idea if FTS5 + fallback is ever not enough on its own — but it only divides the constant factor by core count, it doesn't change the O(corpus) growth curve the way an index does, and it's a real added complexity (serializing candidate data across a process pool) for a fix that stops working again the moment the library grows further. Not pursued for that reason.

  Also unvalidated and worth checking before shipping: whether `LIMIT 4000` on BM25 rank can ever exclude the true best match for pathological common-word queries — it did not for any query tested, but "did not on five queries" is not "cannot".

**No external search engine.** SQLite FTS5 is in the stdlib's bundled SQLite — verified present in the running container (SQLite 3.40.1, FTS5 and trigram tokenizer both available), so this adds **zero new dependencies**. Whoosh (pure-Python, slower), Tantivy (new Rust dep), and Elasticsearch/Meilisearch/Typesense (a whole separate service) were all considered and rejected: the last group in particular would contradict Section 11's single-container self-hosted model and undo the image-size work in Section 8 for a problem the stdlib already solves.

**Migration**: one-off rebuild from the existing JSON cache (prototype built 1,000 titles in ~23s → roughly 4 minutes for the full library), then delete the JSON files. Freshness must carry over — the existing `(mtime, size)` fingerprint logic and the "candidates are stale if the raw cache is newer" mtime comparison both need porting to columns on `titles`, not dropped. Worth keeping the JSON reader around for one release to migrate lazily rather than forcing a rebuild.
- **Multiple subtitle tracks**: default to original-language, non-SDH; let the user pick if ambiguous.
- **Realistic expectations**: strong hit rate for well-known lines with a good subtitle track; harder cases include paraphrased quotes, dubbing/translation drift, and lines split across subtitle entries — design the UI around "likely candidates," not guaranteed exact matches.
- **Library-wide quote search (`/snip-search`, V2)**: searches the subtitle cache described in Section 1, not the live filesystem, so its scope is exactly "titles CineSnip has already parsed via any flow" — which starts small and grows with normal use. Two tiers, so a `/snip-search` call never surprises the user with a multi-minute wait:
  1. **✅ Built — default**: search only already-cached titles. Fast (it's just fuzzy-matching against parsed text already in the cache/DB), returns instantly regardless of library size. Implementation: `GET /search-quote` (`app/worker/api.py`) enumerates cached titles via the SQLite index (Section 1), then `search_cached_library()` (`app/worker/library_search.py`) reuses `find_quote_matches()` unchanged against each title's cached JSON — skipping the sidecar/video freshness check that a single-film flow does, trading a small staleness risk for zero Plex/filesystem calls, which is what keeps this tier instant. **Diversity-first ranking, not a hard one-per-title cap**: every title's best-scoring line competes for a results slot (ranked by score) before any title's second-best line does — which is what keeps results spread across films once the cache has real breadth — but unfilled slots backfill with each title's next-best line (up to `quote_match.library_per_title_limit`, default 3), so a title with several strong hits can surface more than once when there isn't enough breadth elsewhere to fill the list, never at the expense of a better match from another title. (Originally shipped as a hard one-match-per-title cap; revised after real usage on a small/early cache made that feel needlessly restrictive — see `search_cached_library()`'s docstring.) Verified against the real library (`/search-quote` correctly returned a 100%-confidence match with the right title/library/timecode).
  2. **Not yet built — explicit opt-in** (a button/flag on the "no more matches in what I've indexed so far" result, not automatic): extend the search to the rest of the library by parsing every not-yet-cached title's subtitles (sidecar → embedded, per the priority above) on the spot. This is the slow path — communicate that plainly in the UI (progress/ETA, not a silent multi-minute hang) — and whatever it parses along the way gets cached, so the corpus is strictly bigger for next time. Never triggered silently; matches the "never pre-indexing proactively" principle already applied to TV search in Section 4, now generalized to the whole library. Deliberately deferred rather than shipped alongside Tier 1: it needs a real progress/ETA UX that nothing in this app has built yet (no streaming/websocket path between worker and bot), which is its own design pass, not a checkbox alongside the fast path.
  - Results list each candidate's film title, library/quality (Section 3), matched line, and confidence — since results span titles, this label is mandatory here even though it's optional context in the single-film quote search UI.
- **✅ Precomputed, disk-cached match candidates — closing a real scaling gap found while stress-testing this design against the developer's actual library size.** Measured on the real library: normalizing every subtitle line and building the single-line/adjacent-window match candidates (`_build_candidates()`) — entirely deterministic given a title's raw entries, with no dependency on the search query — was **53% of total search time** (3.57s of 6.77s across 216 real cached titles), dwarfing both the disk read (7%) and the actual per-query fuzzy scoring (26%, the one part that genuinely can't be cached). `find_quote_matches()` used to redo this from scratch on *every single search*. Extrapolated to this developer's full library (11,463 titles, if ever fully cached — a real question raised while scoping Phase 6's manual-cache-build idea), the unavoidable scoring step alone would still take ~94s per search; the redundant normalize+build step would have added a further ~189s *on top* of that, every single time.
  - Fixed with a new disk-based cache in `app/worker/quotes.py`: `get_or_build_candidates()` persists the built candidates (plus the per-entry display text needed for context) to `cache/<guid-digest>.candidates.json`, co-located with the existing raw subtitle cache file. `find_quote_matches()` gained an optional `precomputed` parameter that skips its internal normalize+build step entirely when supplied — existing callers and all pre-existing tests are unaffected (defaults to the old inline-computation behavior). Wired into both `search_cached_library()` (`/snip-search`, `/snip-tv` show-wide search) and `/resolve-quote` (single-title search, which benefits on repeat searches of the same film).
  - **An in-memory cache was considered and deliberately rejected in favor of this disk-based design** — real trade-offs weighed: in-memory would have delivered a slightly bigger per-search speedup (no disk read at all) but at real, unbounded RAM cost that scales with how much of the library gets touched (measured: holding just the raw parsed entries for 216 titles cost ~57MB RSS — a full-library scenario could run into multiple GB), plus a genuinely new staleness risk class (a title re-extracted while the worker keeps running would serve stale in-memory results until a restart — the same *kind* of bug this project already hit twice in one session with the original subtitle cache, per Section 5's subtitle-extraction build notes below). The disk-based version has neither problem: it costs disk space only (already established as a non-issue — a full-library candidates cache would run ~4.4GB, still trivial next to the media library itself), and it's invalidated by the same mtime-comparison principle as the existing fingerprint check — if the raw subtitle cache file has been rewritten more recently than the candidates file, the candidates are treated as stale and rebuilt automatically, with zero new invalidation logic to get wrong.
  - Verified against the real library: a cold `/search-quote` call (building candidates for the first time) took 4.78s; the identical search repeated immediately after (warm candidates cache) took 2.94s, and a *different* quote against the same warm cache took 2.89s — consistent, real speedup with identical result correctness across all three runs.

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
- **✅ Image size: swapped Debian's `apt-get install ffmpeg` for static `ffmpeg`/`ffprobe` binaries via a multi-stage `COPY --from=mwader/static-ffmpeg`.** Debian's `ffmpeg` apt package pulls in the full shared-library ecosystem for every codec/protocol ffmpeg supports (~463MB) — most of which this project never touches (it only decodes common film/TV containers, encodes GIF/H.264/VP9, and burns subtitles via libass). The static binaries bundle only what's linked into the two executables (~258MB combined for both — confirmed via `ffmpeg -buildconf` before switching that this build includes `libass`, `libx264`, `libx265`, `libvpx`, everything Section 6/7 actually needs). Total image: **960MB → 706MB**. Verified against real media before trusting the swap (per this file's standing rule that a successful build proves nothing about ffmpeg output): a real subtitle-burned-in render (Peep Show) checked as an extracted still frame, and a real 3D clip (Dune 2021, the squeezed-pack title from Section 3's V3 Phase 1 writeup) checked both visually and via its output dimensions (480×270, correct 16:9 — no doubling/stretch regression). Full existing test suite unaffected (128 tests — they test ffmpeg *command construction*, not real execution, so this class of change is never caught by them; only real-media verification catches it). **Embedded-subtitle-stream extraction (`-map 0:s:N -f srt`) also verified**, closing the gap from the first pass: this developer's library is heavily sidecar-based (Bazarr), so `/subtitles/{rating_key}`'s normal sidecar-first flow never reaches the embedded path on real titles here — worked around by calling `probe_subtitle_streams()`/`extract_embedded_subtitle()` directly against real files, bypassing sidecar preference. First attempt (Akira, a 2160p HDR remux) hit the documented 180s extraction timeout — expected given Section 6's "no fast-seek for embedded extraction" finding on a large file over this I/O path, not a regression. A smaller file (10 Things I Hate About You, 5.4GB) succeeded: real `subrip` stream demuxed to 1,402 correctly-timed SRT entries in 47.5s. All three ffmpeg-dependent code paths (subtitle burn-in, 3D crop/unsqueeze, embedded subtitle extraction) now confirmed working identically against real media.
- **✅ Further trim, and a real correctness find alongside it: `uvicorn[standard]` → plain `uvicorn` + explicit `uvloop`/`httptools`, and `uvloop` wasn't actually active until this change.** `[standard]` bundles four extras; per-package sizes measured in the real container: `uvloop` 16MB, `httptools` 1.8MB (both genuine performance pieces, kept), `websockets` 1.9MB and `watchfiles` 1.2MB (both genuinely unused — no WebSocket routes exist anywhere in this app, and `app/main.py` never runs uvicorn with `--reload` — dropped). While checking this, found that `uvloop` had never actually been active: `uvicorn.Server.serve()` is awaited directly inside `asyncio.gather()` (`app/main.py`, needed so the Discord bot and worker API share one event loop) rather than via `uvicorn.run()`/`Server.run()`, and uvicorn's automatic uvloop activation only fires when uvicorn owns the top-level loop itself — confirmed with a real check (`asyncio.get_running_loop()` under the app's actual `asyncio.run()` pattern returned the standard asyncio loop, not uvloop). So the 16MB was being paid for with zero benefit. Fixed by switching the entry point to `uvloop.run(main())` (`app/main.py`) — a drop-in `asyncio.run()` replacement that does activate it; confirmed via the same live check now returning `uvloop.Loop`. Verified against real usage after the swap, not just "the container started": `/healthz`, `/search`, and a real render (rating_key 813, Snatch) all succeeded — the render specifically exercises `asyncio.create_subprocess_exec` (`app/worker/subprocess_utils.py`), the actual risk surface for an event-loop implementation swap, since that's where a uvloop/child-process-watcher incompatibility would show up if there were one. Image: 706MB → 701MB (small — the real value here was fixing an inert dependency, not the size).

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
- ✅ `/snip-search` (**Tier 1 only**) — library-wide quote search across the (lazily-built) subtitle cache, backed by a small SQLite index (`cache/quote_index.db`, `app/worker/quote_index.py`) mapping `guid → rating_key/title/library_name` so cached titles can be enumerated without a live Plex call. Matching lives in `app/worker/library_search.py`; selecting a result funnels into the existing quote-confirm → render flow (decision #4, `LibrarySearchView` in `app/bot/cogs/gif.py`). Verified against the real library. **The explicit opt-in to extend a search into not-yet-cached titles (Section 5's Tier 2) is not yet built** — deliberately deferred until it can show real progress/ETA rather than a silent hang.
- ✅ Subtitle burn-in + style preset select menu (Section 7)
- ✅ MP4/WebM output option (`format:` on `/snip`/`/snip-tv`, default gif — see decision #1)
- ✅ Multi-library support: `config.yaml`'s `libraries` list replaces the old single `movies_library_name`/flat `path_mappings`, each library owning its own mappings (`app/settings.py`'s `LibraryConfig`/`path_mappings_for()`). `PlexClient` searches every configured movie-type section and tags each result with `library_name` (`movie.librarySectionTitle`, populated natively by plexapi — no extra lookup needed); `/resolve`, `/render`, and subtitle extraction all resolve container paths against that specific library's mappings instead of one global list. Surfaced to the user as a lightweight label in the autocomplete picker and the "Searching.../Generating..." status text (decision #4), not a new confirmation step. This developer's real `config.yaml` now covers all three of their actual libraries (Movies, 3D, TV Shows — see above; no 4K library exists here to configure). TV episode search itself is still V3 (Section 4) — only its path mappings are wired up now.
- ✅ Document + confirm "Public Bot" disabled as part of Discord setup (Section 9)

**V3 — phased, each phase ordered by what it depends on, not just priority**
- **✅ Phase 1 — 3D crop/format handling** (Section 3). `three_d_format` on `LibraryConfig` plus a crop step in `ClipRenderer`'s filter graph, with per-file tag auto-detection overriding the library default (see Section 3 for the full writeup and the mixed-packing finding). Verified against this developer's real `3D` library.
- **✅ Phase 2 — TV show support** (episode-specific and whole-show search, `/snip-tv` — Section 4 for the full writeup, including a real cross-media quote_index leak this surfaced and fixed). Also folded in the `/cinesnip` → `/snip` command consolidation (Section 2, decision list). Verified against this developer's real `TV Shows` library.
- **Phase 3 — Whisper fallback** (cached, lazy, Section 5 tier 3). Sequenced after Phase 2 so it covers both films and TV episodes from the start rather than needing a second pass for TV later.
- **Phase 4 — NVENC/GPU hardware acceleration option**. Per Section 6, this "matters most for full-film Whisper transcription speed" — no real motivation or benchmark target until Phase 3 exists, so it follows directly after.
- **Phase 5 — Local web app: setup wizard + manual generation UI** (decision #6, Section 14 for the wizard spec). By this point the core pipeline (films + TV + Whisper + GPU) is feature-complete, so the wizard/generation UI expose the full feature set rather than a partial one. Also the natural home for a real progress/ETA UI (a web page can show a progress bar; Discord can only edit a message) — which Phase 6 depends on.
- **Phase 6 — `/snip-search` Tier 2** (Section 5: extend search to not-yet-cached titles). Deliberately deferred until a real progress/ETA UX exists rather than a silent multi-minute hang — Phase 5 is what builds that, so Tier 2 reuses it instead of inventing a throwaway Discord-only progress hack.
- **Phase 7 — Allowlists/rate-limiting, multi-server polish, distribution docs** for other self-hosters (both Plex-hosting patterns documented). Final readiness pass, once the wizard (the actual onboarding path for other installers) and the full feature set both exist to document.

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
- **✅ 180s proved too tight against a real file, found while verifying the static-ffmpeg swap (Section 8): raised to 300s.** A real 39GB 2160p HDR remux (Akira) measured at **251.6s** for a full embedded extraction on this developer's real library — past the old 180s default (confirmed: it timed out there first), but the extraction itself wasn't broken or hung, just genuinely slow (raw sequential read on the same mount benchmarks ~250MB/s; the extraction's effective throughput was ~156MB/s, the gap being real ffmpeg demux/parsing overhead, not disk I/O alone). Raised `extraction_timeout_seconds`'s default to 300s (real headroom over the measured 251.6s, not a guess) in `app/settings.py` and `config.yaml.example`. **Also found and fixed a related gap this surfaced**: `RENDER_TIMEOUT_SECONDS` in `app/bot/worker_client.py` was only 90s — its original justification only accounted for `render_defaults.timeout_seconds` (the encode step), missing that `/render` *also* triggers the same cold-cache subtitle extraction inline whenever a style is requested (before encoding even starts), so it never had enough headroom even under the old 180s extraction default, let alone 300s. Raised to 480s, matching `RESOLVE_QUOTE_TIMEOUT_SECONDS` (also raised, to 480s). **Practical note found in the process**: this specific library's near-universal Bazarr sidecar coverage means the slow embedded path essentially never triggers in real production use here — every real title checked that has a genuine embedded text stream also turned out to have a sidecar, which always wins first. The fix is still correct defense-in-depth (matters for a title Bazarr missed, or another self-hoster with thinner sidecar coverage), just rarely exercised on this particular install.
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
