# Build notes: Plex integration, path mapping, TV support

Non-obvious bugs for `app/worker/plex_client.py` and
`app/worker/path_mapper.py`. Read before touching either file.

## Redundant Plex fetches (fixed)

A single `/snip film:X quote:Y` invocation used to re-fetch the same Plex
movie metadata via `PlexClient.get_movie()` three separate times (once
each in `/resolve`, `/resolve-quote`, `/render` — each a real network
round-trip to Plex). Fixed with a short-lived (30s) in-process cache in
`PlexClient` keyed by `rating_key` — long enough to dedupe the handful of
seconds between one command's own three calls, short enough that a
retitled/deleted item doesn't linger. Verified against the real library:
3x `get_movie()` calls on the same `rating_key` now hit Plex once, not
three times.

## Windows extended-length path prefix (fixed)

14 real titles (a Borat film, several multi-episode *Avatar: The Last
Airbender* files) failed with "No path mapping configured" despite an
otherwise-correct `path_mappings` entry — found via the full-library cache
build (`docs/build-notes/subtitles-and-search.md`). Root cause: Plex
reports the `\\?\` prefix (a Win32-API-only escape sequence letting a path
exceed the 260-char MAX_PATH limit) verbatim for titles whose combined
path/filename is long enough — `resolve_container_path()`'s `_normalize()`
only converted backslashes to forward slashes, so `\\?\D:\...` became
`//?/D:/...` and no longer started with the configured `D:/...` prefix.
Fixed by stripping `//?/` (and the `//?/UNC/` variant, for an
extended-length real network path) after backslash normalization, before
prefix-matching. Verified against both real failing titles: Borat now
resolves and renders correctly; a real multi-episode Avatar file (S03E10)
now resolves via `/resolve-episode` and its subtitles extract correctly.

## Cross-media `quote_index.db` leak (fixed)

`/snip-search` (movie-only per its own docs) started leaking TV episodes
into its results once TV support shipped. Root cause: `/render` and
`/resolve-quote` write into the same shared `cache/quote_index.db`
regardless of media type (by design — those endpoints are 100% generic
over a Plex `rating_key`, see CLAUDE.md Section 4), so once `/snip-tv` was
exercised against a real episode, that episode's
`guid → rating_key/title/library_name` row landed in the exact same table
`/snip-search`'s Tier 1 reads from. Confirmed on the real library:
searching a phrase from a cached *Office* episode came back listed as a
`/snip-search` movie result, library-tagged `TV Shows`. Fixed by exposing
`PlexClient.movie_library_names` (the set of section titles whose type is
`"movie"`) and filtering `/search-quote`'s handler to just those library
names at read time — no schema change/migration needed, since the index
itself staying shared is harmless as long as the movie-only endpoint
filters itself back down.

**Lesson**: reusing a generic pipeline across two media types means any
*shared, implicit* state that pipeline touches needs an explicit audit —
this one wasn't in the original design plan, only surfaced by actually
running `/snip-search` against real TV+movie data together, not by code
review alone.

Verified end-to-end against the real library: `/resolve-episode` on a real
show/season/episode resolves correctly; a rendered clip from a specific
episode was confirmed via an extracted still frame (not just "it
rendered") to actually be that episode; whole-show quote search correctly
extracted subtitles inline for several not-yet-cached episodes of a real
show in ~1s (sidecar `.srt` files, no ffmpeg needed) and ranked matches
across episodes; invalid season/episode and invalid show rating_keys
return clean 404s, not stack traces; the post-fix `/snip-search` no longer
returns TV episodes.
