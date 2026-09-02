from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

# Must exceed the worker's own render timeout (render_defaults.timeout_seconds,
# 60s default) — a styled /render can ALSO trigger a cold-cache subtitle
# extraction inline before encoding starts, so this must cover
# extraction_timeout_seconds + render timeout in the worst case, not just
# the encode step (the original 90s value missed this entirely). Keeps the
# worker's clean error response winning the race instead of a generic
# client-side timeout.
RENDER_TIMEOUT_SECONDS = 480.0

# Must exceed subtitle_defaults.extraction_timeout_seconds (300s default)
# — a cold-cache /resolve-quote demuxes an embedded subtitle stream, which
# has no fast seek. 300s isn't a guess: a real 39GB 2160p HDR remux
# measured at ~252s. extraction_timeout_seconds is operator-configurable
# (config.yaml.example invites raising it for large files) — if you raise
# it, raise this and RENDER_TIMEOUT_SECONDS too, since neither can read
# the configured value.
RESOLVE_QUOTE_TIMEOUT_SECONDS = 480.0

# Must exceed subtitle_defaults.extraction_timeout_seconds (300s default),
# same reasoning as RESOLVE_QUOTE_TIMEOUT_SECONDS above — GET /subtitles can
# trigger a full cold subtitle extraction (e.g. the first time ClipEditView's
# Duration/Subtitles category is opened for a title that was rendered from a
# bare timecode, which never fetched subtitles up front).
SUBTITLES_TIMEOUT_SECONDS = 480.0

# /search-episodes-quote may serially cold-extract several never-touched
# episodes in one request, each up to extraction_timeout_seconds in the
# worst case. Generous headroom above that (mostly theoretical in practice
# — TV episode files are far smaller than a feature-length UHD remux).
SEARCH_EPISODES_TIMEOUT_SECONDS = 900.0

# Must exceed subtitle_defaults.extraction_timeout_seconds (300s default),
# same reasoning as RESOLVE_QUOTE_TIMEOUT_SECONDS above — a single extend
# batch does up to library_extend_cap cold extractions sequentially, but
# httpx's read timeout applies per-chunk (time between NDJSON lines), not
# to the whole request, so this only needs to cover one title's worst case.
SEARCH_QUOTE_EXTEND_TIMEOUT_SECONDS = 480.0

# /random-line/{media_id} (/snip movie's random-pick path) can trigger the
# same cold-cache single-title extraction as /resolve-quote.
RANDOM_LINE_TIMEOUT_SECONDS = 480.0

# /random-line-show/{show_media_id} (/snip tv's whole-show random-pick
# path) mirrors /search-episodes-quote's serial per-episode extraction.
RANDOM_LINE_SHOW_TIMEOUT_SECONDS = 900.0


@dataclass
class MovieResult:
    media_id: str
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


@dataclass
class ResolveResult:
    media_id: str
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


@dataclass
class SubtitleStatusResult:
    media_id: str
    likely_slow: bool


@dataclass
class SubtitleEntryResult:
    index: int
    start: float
    end: float
    text: str


@dataclass
class QuoteMatchResult:
    start: float
    end: float
    timecode: str
    text: str
    score: float
    entry_indices: list[int]
    context_before: list[str]
    context_after: list[str]


@dataclass
class ResolveQuoteResult:
    media_id: str
    title: str
    subtitle_source: str
    confident_score: float
    min_score: float
    # True when the worker's fetch hit quote_match.fetch_limit and this
    # result was cut off there — see ResolveQuoteResponse.truncated
    # (app/worker/api.py) for how it's computed.
    truncated: bool
    matches: list[QuoteMatchResult]


@dataclass
class RenderResult:
    content: bytes
    format: str
    style: str
    start: float
    duration: float


@dataclass
class LibraryQuoteMatchResult:
    media_id: str
    title: str
    library_name: str
    start: float
    end: float
    timecode: str
    text: str
    score: float
    context_before: list[str]
    context_after: list[str]


@dataclass
class LibrarySearchResult:
    matches: list[LibraryQuoteMatchResult]
    confident_score: float
    min_score: float
    # Same truncation signal as ResolveQuoteResult.truncated, see there.
    truncated: bool


@dataclass
class RandomQuoteResult:
    media_id: str
    title: str
    library_name: str
    start: float
    end: float
    timecode: str
    text: str
    entry_id: int
    pool_size: int
    exhausted: bool


@dataclass
class LibrarySearchExtendEvent:
    type: str  # "cached" | "scanning" | "progress" | "final"
    matches: list[LibraryQuoteMatchResult] | None = None
    confident_score: float | None = None
    min_score: float | None = None
    index: int | None = None
    total: int | None = None
    title: str | None = None
    remaining_uncached: int | None = None
    # Present (True/False) alongside matches on "cached"/"final" events;
    # absent on "scanning"/"progress" events, same as confident_score/
    # min_score above.
    truncated: bool | None = None


class WorkerClient:
    """Talks to the worker's FastAPI app over real loopback HTTP.

    Bot and worker run in the same process/container, but the bot must
    still go through this HTTP boundary rather than importing worker code
    directly — this is what lets a future local web app become a second
    client of the same API without a rebuild.
    """

    def __init__(self, base_url: str):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str) -> list[MovieResult]:
        response = await self._client.get("/search", params={"query": query})
        response.raise_for_status()
        return [MovieResult(**r) for r in response.json()["results"]]

    async def resolve(self, media_id: str) -> ResolveResult:
        response = await self._client.get(f"/resolve/{media_id}")
        response.raise_for_status()
        return ResolveResult(**response.json())

    async def subtitle_status(self, media_id: str) -> SubtitleStatusResult:
        response = await self._client.get(f"/subtitle-status/{media_id}")
        response.raise_for_status()
        return SubtitleStatusResult(**response.json())

    async def subtitles(self, media_id: str) -> list[SubtitleEntryResult]:
        response = await self._client.get(
            f"/subtitles/{media_id}", timeout=SUBTITLES_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return [SubtitleEntryResult(**e) for e in response.json()["entries"]]

    async def search_shows(self, query: str) -> list[MovieResult]:
        response = await self._client.get("/search-shows", params={"query": query})
        response.raise_for_status()
        return [MovieResult(**r) for r in response.json()["results"]]

    async def resolve_episode(self, show_media_id: str, season: int, episode: int) -> ResolveResult:
        response = await self._client.get(
            f"/resolve-episode/{show_media_id}",
            params={"season": season, "episode": episode},
        )
        response.raise_for_status()
        return ResolveResult(**response.json())

    async def search_episodes_quote(self, show_media_id: str, quote: str) -> LibrarySearchResult:
        response = await self._client.get(
            f"/search-episodes-quote/{show_media_id}",
            params={"quote": quote},
            timeout=SEARCH_EPISODES_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        payload["matches"] = [LibraryQuoteMatchResult(**m) for m in payload["matches"]]
        return LibrarySearchResult(**payload)

    async def resolve_quote(self, media_id: str, quote: str) -> ResolveQuoteResult:
        response = await self._client.get(
            f"/resolve-quote/{media_id}",
            params={"quote": quote},
            timeout=RESOLVE_QUOTE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        payload["matches"] = [QuoteMatchResult(**m) for m in payload["matches"]]
        return ResolveQuoteResult(**payload)

    async def random_quote(
        self,
        quote: str | None,
        media: str,
        exclude_entry_ids: frozenset[int] = frozenset(),
        most_recent_entry_id: int | None = None,
    ) -> RandomQuoteResult:
        params: dict[str, object] = {"media": media, "exclude": list(exclude_entry_ids)}
        if quote is not None:
            params["quote"] = quote
        if most_recent_entry_id is not None:
            params["most_recent"] = most_recent_entry_id
        response = await self._client.get("/random-quote", params=params)
        response.raise_for_status()
        return RandomQuoteResult(**response.json())

    async def random_line(
        self,
        media_id: str,
        exclude_entry_ids: frozenset[int] = frozenset(),
        most_recent_entry_id: int | None = None,
    ) -> RandomQuoteResult:
        params: dict[str, object] = {"exclude": list(exclude_entry_ids)}
        if most_recent_entry_id is not None:
            params["most_recent"] = most_recent_entry_id
        response = await self._client.get(
            f"/random-line/{media_id}", params=params, timeout=RANDOM_LINE_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return RandomQuoteResult(**response.json())

    async def random_line_show(
        self,
        show_media_id: str,
        season: int | None = None,
        episode: int | None = None,
        exclude_entry_ids: frozenset[int] = frozenset(),
        most_recent_entry_id: int | None = None,
    ) -> RandomQuoteResult:
        params: dict[str, object] = {"exclude": list(exclude_entry_ids)}
        if season is not None:
            params["season"] = season
        if episode is not None:
            params["episode"] = episode
        if most_recent_entry_id is not None:
            params["most_recent"] = most_recent_entry_id
        response = await self._client.get(
            f"/random-line-show/{show_media_id}",
            params=params,
            timeout=RANDOM_LINE_SHOW_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return RandomQuoteResult(**response.json())

    async def search_quote_extend(self, quote: str) -> AsyncIterator[LibrarySearchExtendEvent]:
        async with self._client.stream(
            "GET",
            "/search-quote-extend",
            params={"quote": quote},
            timeout=SEARCH_QUOTE_EXTEND_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                payload = json.loads(line)
                if payload.get("matches") is not None:
                    payload["matches"] = [LibraryQuoteMatchResult(**m) for m in payload["matches"]]
                yield LibrarySearchExtendEvent(**payload)

    async def render(
        self,
        media_id: str,
        timecode: str | None = None,
        duration: float | None = None,
        end_timecode: str | None = None,
        format: str | None = None,
        style: str | None = None,
        start: float | None = None,
        end: float | None = None,
        subtitle_overrides: dict[int, str | None] | None = None,
    ) -> RenderResult:
        response = await self._client.post(
            "/render",
            json={
                "media_id": str(media_id),
                "timecode": timecode,
                "duration": duration,
                "end_timecode": end_timecode,
                "format": format,
                "style": style,
                "start": start,
                "end": end,
                "subtitle_overrides": subtitle_overrides,
            },
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return RenderResult(
            content=response.content,
            format=response.headers["X-Clip-Format"],
            style=response.headers["X-Clip-Style"],
            start=float(response.headers["X-Clip-Start"]),
            duration=float(response.headers["X-Clip-Duration"]),
        )
