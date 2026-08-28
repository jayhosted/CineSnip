from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

# How long a rendered clip stays fetchable/postable after generation, and
# the most clips held in memory at once. Both bounds exist for the same
# reason: these are ephemeral session artifacts (the point is letting the
# result panel re-fetch/re-post what it just rendered without re-encoding),
# not a durable store — nothing here needs to survive a container restart,
# and main.py already clears the scratch dir on every startup in that same
# spirit. An unbounded dict would leak memory across a long-running,
# frequently-used container; a plain LRU-by-count cap alone wouldn't bound
# how long a single idle clip lingers, so both apply together.
_TTL_SECONDS = 30 * 60
_MAX_ENTRIES = 50


@dataclass(frozen=True)
class ClipEntry:
    content: bytes
    format: str
    filename: str


class ClipStore:
    """Holds recently-rendered clip bytes server-side, keyed by an
    unguessable id, so the result panel can reference a clip by URL
    (<img src>, Download, Post to Discord) instead of embedding it as a
    multi-megabyte base64 data URI in the page on every render/format
    switch. Not thread-safety-hardened beyond dict operations being atomic
    under the GIL — this process is single-event-loop asyncio, never
    multi-threaded for this path.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[str, tuple[float, ClipEntry]] = OrderedDict()

    def put(self, content: bytes, format: str, filename: str) -> str:
        self._sweep_expired()
        clip_id = uuid.uuid4().hex
        self._entries[clip_id] = (time.monotonic(), ClipEntry(content, format, filename))
        while len(self._entries) > _MAX_ENTRIES:
            self._entries.popitem(last=False)
        return clip_id

    def get(self, clip_id: str) -> ClipEntry | None:
        self._sweep_expired()
        entry = self._entries.get(clip_id)
        return entry[1] if entry else None

    def _sweep_expired(self) -> None:
        cutoff = time.monotonic() - _TTL_SECONDS
        # Insertion-ordered, so the oldest entries are always at the front —
        # stop at the first still-fresh one instead of scanning everything.
        while self._entries:
            clip_id, (created_at, _entry) = next(iter(self._entries.items()))
            if created_at >= cutoff:
                break
            del self._entries[clip_id]
