# Build notes: Discord bot UX

Non-obvious bugs for `app/bot/cogs/gif.py`. Read before touching
`_generate()`, `QuoteMatchView`, or `LibrarySearchView`.

## Library search selection didn't confirm the line the user actually picked (fixed)

`_generate()`'s quote branch re-runs a fresh per-film search on the raw
quote text (by design — a real per-film re-search, not just a
rubber-stamp) and used to always default `QuoteMatchView` to *that fresh
search's* top-ranked candidate — which isn't necessarily the same line the
library search ranked top, since the two searches use different
per-title breadth (library search's diversity cap vs. the per-film
search's full candidate list). Confirmed on the real library: picking
Snatch's "He'll do you proud, governor." (09:41) from the `/snip-search`
dropdown silently rendered the unrelated top-ranked "Hello?" line (03:38)
instead — the user only sees this if they read the confirm screen
closely, since clicking Confirm on the shown default is the whole point
of that step. Fixed by threading the selected match's `start` time through
as `_generate(..., preferred_start=...)`, which now picks the
closest-matching line in the fresh per-film results as `QuoteMatchView`'s
initial selection (`initial_index` param) instead of defaulting to index
0. `LibrarySearchView` is reused as-is by `/snip-tv`'s whole-show search.

## Upfront warning before a likely-slow cold subtitle extraction

A single-title quote search or a styled render could silently take
several minutes on a cold cache with no sidecar (see
`docs/build-notes/subtitles-and-search.md` — a real 251.6s case on this
developer's library), with the status message never hinting that was
coming, unlike `/snip-tv`'s whole-show search (which already warns "this
can take a moment for episodes not seen before"). New worker endpoint
`GET /subtitle-status/{rating_key}` answers cheaply — no ffmpeg involved,
just a sidecar-file check plus a cache lookup — whether a request is
about to fall through to a cold embedded extraction.
`_slow_subtitle_warning()` calls it best-effort (a failure never blocks
the real search/render, just means no warning shows) and folds the result
into the existing status text. Wired into both `GifCog._generate()`'s
quote branch and `ClipResultView`'s style-change handler (a style picked
*after* an initial bare-timecode render can be the first request that
actually needs that title's subtitles).

Verified against the real library: a title with a sidecar (Snatch)
correctly gets no warning; the slow-detection logic itself confirmed
correct against synthetic no-sidecar/no-cache inputs — this library's
near-universal Bazarr coverage kept finding sidecars in real sampling, so
a genuine organic "likely_slow: true" real-world trigger proved hard to
find here specifically, even though the logic is verified.

## Command naming history

`/gif` → `/cinesnip` (V2) → consolidated onto `/snip` alone (V3 Phase 2),
dropping `/cinesnip` outright rather than keeping it as a deprecated
alias. Reasoning: Discord slash commands are namespaced per-application
(no technical collision risk between bots), but a server with two bots
that both register the same short command name shows a disambiguation
picker before the user can act — real friction, hence moving off the
generic `/gif`. `/snip` had existed alongside `/cinesnip` since V2 as a
same-behavior shorter alternative; once real daily use showed the shorter
name was what actually got typed, keeping both registered was pure
redundancy. `/cinesnip-search` → `/snip-search` for the same reason.
(`/cinesnip-diagnose` was never actually built, so there was nothing to
rename — a future diagnostic command should launch directly as
`/snip-diagnose`.)

`/snip`, `/snip-tv`, `/snip-search` → `/snip movie`, `/snip tv`, `/snip
search` (V3 Phase 3): three flat top-level commands folded into one
`app_commands.Group` so they show up together under a single `/snip` in
Discord's command picker instead of as unrelated entries. Purely
organizational — no shared parameters were introduced between the
subcommands, keeping the movie/TV/search parameter shapes exactly as
distinct as they were before (see CLAUDE.md Section 2's rationale for why
`/snip search` stays a separate signature rather than a "no film given"
fallback on `/snip movie`).
