# Built: the subtitle cache is consolidated into SQLite + FTS5

**Status: built and running against the real ~10,695-title library.**
Originally recorded here as a design proposal before implementation — kept
below largely as written (prototype numbers, rejected alternatives, the
recall tradeoff) since that reasoning is still exactly why the shipped
design looks the way it does. The one thing the prototype's numbers got
wrong at real full-library scale — an entry-vs-title `LIMIT` bug — is
called out in its own section near the bottom, with the real before/after
measurements. `scripts/build_full_cache.py` and `library_sync.py` both now
write into this index directly; there is no more JSON-based candidates
cache to keep in sync.

**Where the time actually goes now** (measured per-title, mirroring
`search_cached_library()`'s real loop, against the real 10,679-title cache
— 7,511,478 subtitle entries, 13,971,474 candidates):

| Step | Time | Share |
| --- | --- | --- |
| Raw subtitle JSON read | 15.8s | 14% |
| Candidates JSON read (deserialising 14M objects) | 49.1s | 42% |
| Fuzzy scoring (14M `WRatio` calls) | 50.9s | 44% |

(That 115.8s is scanning *everything*; the observed ~40s for `/snip-search`
is because it filters to movie-type libraries only, ~35% of the corpus. TV
is unaffected in practice — `/snip-tv`'s whole-show search only ever scans
one show, measured at 0.68s.)

**The non-obvious finding: the disk-based candidates cache inverts at
scale.** It was a clear win at 216 titles (4.78s → 2.94s), because it
traded CPU-bound normalise/build work for a disk read. But at 10,679
titles, deserialising 14M candidate objects out of JSON costs *as much as
the fuzzy scoring itself*. Both of those costs are O(corpus) and neither
can be cached away by doing more of the same thing — the only fix is to
stop touching the whole corpus per search.

## Design: one SQLite DB with an FTS5 inverted index as a pre-filter

```sql
CREATE TABLE titles(title_id INTEGER PRIMARY KEY, guid TEXT UNIQUE NOT NULL, ...);
CREATE TABLE entries(
  id INTEGER PRIMARY KEY, title_id INTEGER NOT NULL, idx INTEGER NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL, display_text TEXT NOT NULL);
CREATE VIRTUAL TABLE entries_fts USING fts5(normalized_text, content='');
```

Query flow: normalise the quote → `MATCH` its OR'd tokens against
`entries_fts`, `ORDER BY rank LIMIT ~4000` (BM25 does the coarse ranking) →
fetch only those rows → recompute `normalized_text` and build adjacent-cue
windows *for the survivors only* → run the existing `find_quote_matches()`
scoring/literal-boost/overlap-cap logic unchanged on that narrow set.

Three consequences worth being explicit about:
- **The candidates cache disappears entirely** — no precomputed candidates
  stored at all (2.5GB of today's 3.6GB). Windows get built on the fly for
  a few thousand rows, which is trivial.
- **`normalized_text` is never stored** — contentless FTS5 (`content=''`)
  keeps only the inverted index, and normalisation is recomputed for
  survivors, which is cheap at that size.
- **The existing `quote_index.db` folds into the same DB** as the
  `titles` table, rather than being a separate SQLite file alongside JSON.

**Measured on a real 1,000-title / 1.3M-entry prototype built from the
actual cache:**

| | Today (JSON) | Prototype (SQLite+FTS5) |
| --- | --- | --- |
| Size | 3.6 GB (22,121 files) | **1.57 GB** (1 file, extrapolated from 146.9 KB/title) |
| Search | ~40s (movies) / ~116s (all) | **2ms – 740ms end-to-end** |
| Narrowing | scores all 14M candidates | 0.016%–0.3% of entries reach scoring |

Correctness held on the prototype: "May the Force be with you" → 100.0,
"Here's Johnny!" → 100.0, "Assistant to the Regional Manager" → 95.0.
Normalising `guid` into a `title_id` FK is worth a full 1GB on its own
(2.57GB → 1.57GB) — a ~40-char GUID repeated across 7.5M rows is not free.

## The real risk: an index pre-filter changes recall, not just speed

This app's whole promise is *fuzzy* matching. FTS5 matches whole tokens, so
a typo'd word simply is not in the index. Measured recall:

| Query | Indexed hits |
| --- | --- |
| `here's johnny` (exact) | 2,289 |
| `here's jonny` (1-word typo, multi-word) | 1,348 — fine, other tokens carry it |
| `hitler` (single word, exact) | 170 |
| `hitlr` (single word, typo) | **0 — total miss** |
| `may the forse be with you` | 509,137 (common tokens match broadly; BM25 + LIMIT handles it) |

So multi-word queries tolerate typos well, but a **single-word typo'd
query returns nothing**, where today's full fuzzy scan would still find
it. Mitigation: **fall back to the existing full scan when FTS5 returns
zero/near-zero hits** — costs the old ~40s only in the rare case the fast
path found nothing, and preserves 100% of current recall. This fallback is
required, not optional — two other ways to avoid needing it were tested
directly and both ruled out:

- **The `trigram` tokenizer does not help.** Tested directly against the
  real cache expecting it to be more typo-tolerant — it isn't. It does
  exact substring matching (finds `"manag"` inside `"manager"`), not fuzzy
  matching: `"hitlr"` still returns 0 hits with a trigram-tokenized index,
  identical to the word tokenizer. Confirmed via a real 1,000-title
  prototype (187 KB/title, ~2GB extrapolated — also larger than the
  word-tokenized index for no typo-tolerance benefit). Don't reach for it
  to solve this problem; it solves a different one (mid-word substring
  search).
- **Parallelizing the existing full scan across threads does not help
  either.** Tested with `ThreadPoolExecutor` at 4/8/16 workers against the
  real 10,679-title scoring step (a 16-core host): zero speedup, slightly
  negative from overhead — `rapidfuzz`'s batch scoring doesn't release the
  GIL enough for this to matter. Real multiprocessing would parallelize it
  (separate processes, no shared GIL) and is worth naming as a genuine
  complementary idea if FTS5 + fallback is ever not enough on its own —
  but it only divides the constant factor by core count, it doesn't
  change the O(corpus) growth curve the way an index does, and it's real
  added complexity (serializing candidate data across a process pool) for
  a fix that stops working again the moment the library grows further.
  Not pursued for that reason.

Also unvalidated and worth checking before shipping: whether `LIMIT 4000`
on BM25 rank can ever exclude the true best match for pathological
common-word queries — it did not for any query tested, but "did not on
five queries" is not "cannot".

## No external search engine

SQLite FTS5 is in the stdlib's bundled SQLite — verified present in the
running container (SQLite 3.40.1, FTS5 and trigram tokenizer both
available), so this adds **zero new dependencies**. Whoosh (pure-Python,
slower), Tantivy (new Rust dep), and Elasticsearch/Meilisearch/Typesense (a
whole separate service) were all considered and rejected: the last group
in particular would contradict the single-container self-hosted model
(CLAUDE.md Section 11) and undo the image-size work (Section 8,
`docs/build-notes/docker-image.md`) for a problem the stdlib already
solves.

## Migration

One-off rebuild from the existing JSON cache (prototype built 1,000 titles
in ~23s → roughly 4 minutes for the full library), then delete the JSON
files. Freshness must carry over — the existing `(mtime, size)`
fingerprint logic and the "candidates are stale if the raw cache is newer"
mtime comparison both need porting to columns on `titles`, not dropped.
Worth keeping the JSON reader around for one release to migrate lazily
rather than forcing a rebuild.

Shipped as `scripts/migrate_to_fts5.py` — a one-off, idempotent/resumable
backfill from the legacy `cached_titles` table + per-title JSON cache into
`search_index`'s schema, non-destructive (leaves the legacy table and JSON
files untouched on disk). See the script's own docstring for the exact
fingerprint-handling tradeoffs. `README.md`'s "Upgrading an existing
install" section tells existing installers to run it once after upgrading.

## Real-scale finding: the entry-vs-title `LIMIT` bug

The 1,000-title prototype above reported "0.016%–0.3% of entries reach
scoring" — that number **did not reproduce** at the real 10,695-title
scale once this was actually built, and the reason is a design/
implementation gap worth recording so it isn't reintroduced.

The query flow section above says the FTS5 pre-filter should `MATCH` →
`ORDER BY rank LIMIT ~4000` → fetch **only those rows** → build adjacent-
cue windows for the survivors. The first shipped implementation instead
capped the pre-filter to `SELECT DISTINCT title_id ... LIMIT 4000` — 4,000
distinct *titles*, not 4,000 entry *rows*. Every entry of every one of
those survivor titles then got fuzzy-scored in full, not just the entries
near an actual FTS5 hit. Measured against the real library before the fix:

| Query | Entries scored | Time |
| --- | --- | --- |
| `hitler` | 245,779 | ~2.5s |
| `here's johnny` | up to ~3,462,510 (46% of the 7.5M-entry corpus) | 31–92s |
| `may the forse be with you` | similarly corpus-scale | ~90s |

A ~700x amplification over the intended ~4,000-entry budget — the coarse
pre-filter was doing its job (narrowing to a few thousand *titles* out of
10,695), but the expensive step downstream (fuzzy-scoring) was still
effectively O(corpus) because "narrow the titles" and "narrow the entries
actually scored" were conflated.

**Fix**: `search_index.search_entry_ids()` now caps individual FTS5 entry
rows directly (`SELECT entries.id, entries.title_id, entries.idx ...
ORDER BY rank LIMIT 4000`), matching the original design's intent exactly.
`library_search.py` expands each surviving entry into a small adjacent-cue
window (idx ± a pad, at least `context_lines + 1`) via the new
`search_index.fetch_entry_windows()` — which fetches only the rows inside
those windows, not each survivor title's full entry list (an early version
of the fix still called a "fetch this whole title's entries" helper once a
title had any hit, which independently dominated real-query time; fixed by
adding the windowed fetch instead). Multiple windows within the same title
are merged and re-limited to `per_title_limit` before the shared
diversity-ranking step, so one title with many scattered hits can't flood
the results at another title's expense.

Post-fix, measured against the same real library and the same three
queries:

| Query | Entries scored | Time |
| --- | --- | --- |
| `hitler` | 1,331 | 0.04s |
| `here's johnny` | 19,666 | 0.63s |
| `may the forse be with you` | 26,607 | 3.97s |

All three land in the range the original design intended (well under 5s),
consistent with an independent prototype validated separately during the
review that caught this bug (0.11s / 0.79s / 4.08s on a similar scan
scale — the small remaining differences are query-set/corpus-slice
differences, not a different fix shape). `may the forse be with you`
stays the slowest of the three because its tokens (`may`, `the`, `with`,
`you`) are individually common enough that the FTS5 `MATCH ... OR ...`
itself has to rank a very large candidate set to find the top 4,000 by
BM25 — this is inherent to how common the words are, not something the
entry-level `LIMIT` fix changes further; scoping the pre-filter to a
narrower title set (see below) does not measurably help this query either,
since the cost is in the FTS5 ranking step itself, before scoping is
applied.

## Real-scale finding: pre-filter/fallback scope must follow the caller, not the whole corpus

A related gap, also only visible at real scale with more than one caller:
`search_cached_library()` is shared by both `/search-quote` (library-wide,
deliberately unscoped — every cached movie-library title) and
`/search-episodes-quote` (`/snip-tv`'s whole-show search, deliberately
scoped to one show's handful of episodes). The FTS5 pre-filter and the
zero-hit fallback's full scan both originally ran over the *entire* corpus
regardless of which caller was asking, including all TV episodes indexed
via the same shared DB. Two consequences, both confirmed against the real
library:
- A global top-4000 *entry* cap (post the fix above) could still starve a
  narrow-scoped show search of its own episodes if enough of the rest of
  the corpus ranked higher for the same query. Confirmed directly: for
  "that's what she said" against a real 12-episode "The Office" (cached in
  this library), an **unscoped** top-4000 cap left only **1 of 12**
  episodes with any surviving entry at all; scoped to just that show's
  guids, all 12 are searched and the show-search returns 8 correct,
  diverse results.
- The fallback (triggered on a typo'd query that misses FTS5 entirely)
  streamed the whole ~7.5M-entry corpus even for a whole-show search.

**Fix**: `search_entry_ids()` and `iter_all_entries()` both take an
optional `title_ids` scope. `search_cached_library()` resolves its
caller-supplied `cached_titles` to `title_id`s up front and always passes
that as the scope — for `/search-quote`, `cached_titles` already IS "every
cached movie-library title", so scoping to it changes nothing about what's
searched, it just also stops TV episodes from consuming the entry budget;
for whole-show search, it narrows correctly. No caller-type flag needed —
the scope falls naturally out of whatever `cached_titles` list the caller
already builds.
