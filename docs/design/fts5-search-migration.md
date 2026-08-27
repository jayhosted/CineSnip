# Planned (not yet built): consolidate the subtitle cache into SQLite + FTS5

Design agreed after prototyping against the real fully-built cache. **Not
implemented** — recorded here so the measurements and the reasoning aren't
lost. Sequence it before or alongside any future full-library manual cache
build (`scripts/build_full_cache.py`) — that's exactly what makes today's
design hurt (see `docs/build-notes/subtitles-and-search.md`'s "instant
regardless of library size" finding).

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
