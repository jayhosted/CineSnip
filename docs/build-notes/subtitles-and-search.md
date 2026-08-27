# Build notes: subtitle extraction, quote ranking, caching, library sync

Non-obvious bugs for `app/worker/subtitles.py`, `app/worker/quotes.py`,
`app/worker/library_search.py`, and `app/worker/library_sync.py`. Read the
relevant section before touching any of those files.

## Subtitle extraction (read before touching `app/worker/subtitles.py`)

- **✅ Fixed: the subtitle cache used to never invalidate when the sidecar
  changed on disk.** `get_subtitles()` used to key the cache purely on the
  Plex media GUID, so a later edit or replacement of a title's `.srt` (or,
  for the embedded path, the video itself) was invisible to CineSnip
  **forever** — this is not theoretical, it bit this project twice in one
  session (a Bazarr re-fetch, then an `alass` re-sync of the same file),
  each time needing the cache file deleted by hand, and it actively
  confused the video-sync diagnosis (see `docs/build-notes/ffmpeg-rendering.md`).
  Fixed by recording the relevant source file's `(mtime, size)`
  fingerprint in the cached payload — `_fingerprint()` — and re-extracting
  on mismatch or on removal. Which file actually needs to be fresh is
  decided by the cached payload's own recorded source (`SIDECAR` checks
  the sidecar, `EMBEDDED` checks the video), so an unrelated file
  appearing doesn't wrongly invalidate a good cache entry.
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
  exists. Fix: parse only the *first* dot-separated segment as the
  language code, and treat any later segment matching `hi`/`sdh`/`cc` as a
  hearing-impaired flag — prefer non-hearing-impaired sidecars when both
  exist. See `_parse_sidecar_suffix()`.
- **Embedded-subtitle extraction (`ffmpeg -map 0:s:N -f srt`) has no
  equivalent of clip rendering's `-ss` fast seek** — it must read
  sequentially through the whole container to demux the subtitle packets,
  even though the actual subtitle data is tiny. On this environment's
  WSL2-bridged NTFS drives, extracting from an 11GB remux took ~73
  seconds — comfortably past a naive 30s timeout, even though nothing was
  actually hung. Fixed by making it configurable
  (`subtitle_defaults.extraction_timeout_seconds`, default 180s, later
  raised — see below). Any new endpoint that triggers extraction on a
  cold cache needs its own client-side timeout set higher than this value,
  or the client aborts before the worker's clean error does — see
  `RESOLVE_QUOTE_TIMEOUT_SECONDS` in `app/bot/worker_client.py`.
- **✅ 180s proved too tight against a real file: raised to 300s.** A real
  39GB 2160p HDR remux (Akira) measured at **251.6s** for a full embedded
  extraction — past the old 180s default, but the extraction itself
  wasn't broken or hung, just genuinely slow (raw sequential read on the
  same mount benchmarks ~250MB/s; the extraction's effective throughput
  was ~156MB/s, the gap being real ffmpeg demux/parsing overhead, not
  disk I/O alone). Raised `extraction_timeout_seconds`'s default to 300s
  (real headroom over the measured 251.6s, not a guess). **Also fixed a
  related gap**: `RENDER_TIMEOUT_SECONDS` in `app/bot/worker_client.py`
  was only 90s — it never accounted for `/render` also triggering the
  same cold-cache subtitle extraction inline whenever a style is
  requested (before encoding even starts). Raised to 480s, matching
  `RESOLVE_QUOTE_TIMEOUT_SECONDS` (also raised, to 480s). This developer's
  near-universal Bazarr sidecar coverage means the slow embedded path
  essentially never triggers in real production use here — the fix is
  still correct defense-in-depth for a title Bazarr missed, or another
  self-hoster with thinner sidecar coverage.
- **PGS/bitmap subtitle streams (`hdmv_pgs_subtitle`) are bitmap, not
  text — ffmpeg can't mux them to SRT.** `choose_subtitle_stream()`
  filters these out; a title with only bitmap embedded streams and no
  sidecar correctly resolves to "no subtitles available" rather than a
  garbage extraction. This is a documented permanent gap, not a bug —
  don't try to "fix" a title that hits this path.

## Quote ranking bugs (read before touching `find_quote_matches()` in `app/worker/quotes.py`)

- **✅ Fixed: a literal match didn't reliably outrank a non-matching
  line.** Searching "Hitler" across Peep Show returned several lines that
  don't contain the word ranked ahead of ones that do —
  `fuzz.WRatio`'s length-normalized scoring can dilute a short quote
  buried in a longer line below an unrelated same-length line that merely
  shares similar letters. Fixed: after the normal WRatio pass, any
  candidate containing the normalized quote as a whole-word substring
  (`\bquote\b`) is force-scored to 100. Word-boundary-checked deliberately
  (not a raw substring check) so a quote like "cat" doesn't get
  force-scored off appearing inside "concatenated".
- **✅ Follow-on: a directional partial word-overlap bonus, deliberately
  without `rapidfuzz.fuzz.token_set_ratio`.** The literal-match fix above
  only catches a quote that appears as a contiguous phrase — it misses a
  multi-word quote whose words are all present but out of order (e.g. a
  scrambled "manager regional the to assistant" should still find
  "Assistant to the Regional Manager"). The obvious fix —
  `token_set_ratio`, which is word-set-aware — was tried and rejected:
  it's symmetric, so it scores a *short candidate that's a strict
  word-subset of a much longer quote* as a perfect 100 too (confirmed:
  `token_set_ratio("i am", "i am your father") == 100.0`), reintroducing
  the exact class of bug the literal-match fix exists to prevent, from
  the opposite direction. Instead: score what fraction of the *quote's*
  words appear as whole words in the candidate (never the reverse), award
  a bonus (scaling 60→95 as overlap goes 50%→100%) only once a majority
  of the quote's words are present — capped below the literal fix's 100,
  since a real contiguous phrase is stronger evidence than scattered
  words. Guarded by a regression test asserting a truncated candidate can
  never tie or beat a genuine full match.
- **✅ Fixed: a real correctness bug only found by testing against the
  fully-built library cache — invisible at small scale.**
  `scripts/build_full_cache.py` extracted and cached subtitles for all
  11,463 titles in this developer's real library (10,672 sidecar, 13
  embedded, 764 with no usable subtitles, 0 errors, 29.5 minutes total).
  Searching that cache for "Assistant to the Regional Manager" returned
  nonsense at the top: `"to the"`, `"manage."`, `"MANAGER"` — bare
  fragments sharing at most one real word with the quote — all scoring 90.
  Root cause: `fuzz.WRatio` *itself* can score a short candidate sharing
  only a word or two with a much longer quote surprisingly high, via its
  internal partial-ratio weighting for large length-ratio pairs.
  Confirmed directly: `WRatio("assistant to the regional manager", "to
  the") == 90.0`. Always present but invisible below full-library scale —
  a small cache simply doesn't contain enough short, coincidentally-
  overlapping fragments to expose it. Fixed by extending the same
  directional word-overlap fraction already computed for the bonus above
  into a **cap**, not just a bonus: below the 50%-overlap threshold, a
  candidate's score is now capped at `overlap * 60` instead of trusting
  WRatio's raw number, pushing any candidate missing most of the quote's
  real words below `min_score` and out of results entirely. Two existing
  tests had unknowingly encoded this exact bug as "expected" and needed
  updating once the bug was understood, not just made to pass.
- **Confirmed, not just theorized: `/snip-search`'s "instant regardless
  of library size" promise genuinely breaks down at real full-library
  scale.** A single search against the fully-built 11,463-title cache
  took ~40 seconds — dominated by fuzzy-scoring, inherently proportional
  to corpus size. Not urgent (this developer's library isn't kept fully
  built this way day-to-day), but real evidence that a pre-filter/index
  ahead of the expensive per-candidate scoring is a real requirement at
  scale — see `docs/design/fts5-search-migration.md` for the planned fix.

## Precomputed candidates cache (`get_or_build_candidates()` in `app/worker/quotes.py`)

- **✅ Closed a real scaling gap found while stress-testing against the
  developer's actual library size.** Normalizing every subtitle line and
  building match candidates (`_build_candidates()`) — entirely
  deterministic given a title's raw entries — was **53% of total search
  time** (3.57s of 6.77s across 216 real cached titles), dwarfing both the
  disk read (7%) and the actual per-query fuzzy scoring (26%, the one
  part that genuinely can't be cached). `find_quote_matches()` used to
  redo this from scratch on every single search. Fixed with a disk-based
  cache: `get_or_build_candidates()` persists built candidates to
  `cache/<guid-digest>.candidates.json`; `find_quote_matches()` gained an
  optional `precomputed` parameter that skips its internal normalize+build
  step when supplied.
- **An in-memory cache was considered and deliberately rejected.**
  In-memory would have delivered a slightly bigger per-search speedup (no
  disk read) but at unbounded RAM cost that scales with library coverage
  (measured: ~57MB RSS for just 216 titles' raw entries — full-library
  could run into multiple GB), plus a new staleness risk class (a title
  re-extracted while the worker keeps running would serve stale
  in-memory results until a restart — the same kind of bug this project
  already hit twice with the raw subtitle cache, see above). The
  disk-based version costs disk space only (~4.4GB for a full-library
  candidates cache, trivial next to the media library) and is invalidated
  by the same mtime-comparison principle as the raw-cache fingerprint
  check.
- Verified against the real library: a cold `/search-quote` call (building
  candidates for the first time) took 4.78s; the identical search
  repeated immediately after (warm candidates cache) took 2.94s, and a
  different quote against the same warm cache took 2.89s.

## `scripts/build_full_cache.py` made incremental

Re-running the full build after adding new content used to mean a
~30-minute sweep touching every title again. Fixed by skipping any title
whose subtitle cache file already exists unless `--force` is passed.
Verified: re-running against all 11,463 titles took **1 second**,
correctly skipping the 11,451 already cached. This is deliberately the
shape any future "automatic" sync should take: **enumerate → cheap
existence/freshness check → do real work only for what's missing.** This
became the actual `library_sync` mechanism below.

## Automatic library sync (`app/worker/library_sync.py`)

- **Change detection via Plex's own `section.updatedAt`** (not a webhook —
  Plex Pass–gated and would need an inbound connection, cutting against
  the worker's loopback-only design; not a cron job either — this project
  otherwise avoids proactive background work). Verified with four real
  tests against the live server: refresh/analyze on both a movie and a TV
  episode never moved `updatedAt` when nothing actually changed.
- **Real bug found while building this**: getting a *fresh* `updatedAt`
  value needs `section.reload()` on the already-held section object —
  confirmed directly that re-fetching via `PlexServer.library.sections()`
  returns the exact same cached Python object (`is` identity check
  confirmed no network call happens), so a naive "just re-fetch the
  section" implementation would have silently read stale data forever.
- **Second real bug, same investigation**: `section.updatedAt` is a
  `datetime` object, not the raw int the rest of the app needs — Python
  3.12 no longer auto-adapts `datetime` for sqlite3, so storing it
  directly would have failed or silently misbehaved. Fixed by converting
  to `int(updatedAt.timestamp())` in `PlexClient.current_section_updated_ats()`.
- **Removal detection has a two-layer safety guard**, per an explicit
  requirement that automatic deletion must not be triggerable by
  something that only *looks* like removal. Layer 1 (mount check): every
  configured `path_mappings` root for a library must be reachable and
  non-empty before any deletion is considered. Layer 2 (spot check): a
  sample of titles Plex still lists as present must also actually resolve
  to a real file on disk. Either failing skips cleanup for that whole
  library *and leaves its stored `updatedAt` unchanged*, so the next
  cycle still sees "changed" and retries. A Plex enumeration call that
  raises is handled distinctly from "returned zero/few items" — an
  exception never triggers removal logic.
- Verified against the real library: a real first sync cycle against the
  full 10,692-title index correctly reported `removed=0` with the index
  count unchanged before/after; a second immediate run correctly
  short-circuited via the cheap `updatedAt` check in 0.01s; the scheduled
  task itself was confirmed running for real inside the app process
  before being reverted back to disabled for normal development.
