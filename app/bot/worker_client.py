from __future__ import annotations

from dataclasses import dataclass

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

# /search-episodes-quote may serially cold-extract several never-touched
# episodes in one request, each up to extraction_timeout_seconds in the
# worst case. Generous headroom above that (mostly theoretical in practice
# — TV episode files are far smaller than a feature-length UHD remux).
SEARCH_EPISODES_TIMEOUT_SECONDS = 900.0


@dataclass
class MovieResult:
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


@dataclass
class ResolveResult:
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


@dataclass
class SubtitleStatusResult:
    rating_key: int
    likely_slow: bool


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
    rating_key: int
    title: str
    subtitle_source: str
    confident_score: float
    min_score: float
    matches: list[QuoteMatchResult]


@dataclass
class RenderResult:
    content: bytes
    format: str
    style: str


@dataclass
class LibraryQuoteMatchResult:
    rating_key: int
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

    async def resolve(self, rating_key: int) -> ResolveResult:
        response = await self._client.get(f"/resolve/{rating_key}")
        response.raise_for_status()
        return ResolveResult(**response.json())

    async def subtitle_status(self, rating_key: int) -> SubtitleStatusResult:
        response = await self._client.get(f"/subtitle-status/{rating_key}")
        response.raise_for_status()
        return SubtitleStatusResult(**response.json())

    async def search_shows(self, query: str) -> list[MovieResult]:
        response = await self._client.get("/search-shows", params={"query": query})
        response.raise_for_status()
        return [MovieResult(**r) for r in response.json()["results"]]

    async def resolve_episode(self, show_rating_key: int, season: int, episode: int) -> ResolveResult:
        response = await self._client.get(
            f"/resolve-episode/{show_rating_key}",
            params={"season": season, "episode": episode},
        )
        response.raise_for_status()
        return ResolveResult(**response.json())

    async def search_episodes_quote(self, show_rating_key: int, quote: str) -> LibrarySearchResult:
        response = await self._client.get(
            f"/search-episodes-quote/{show_rating_key}",
            params={"quote": quote},
            timeout=SEARCH_EPISODES_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        payload["matches"] = [LibraryQuoteMatchResult(**m) for m in payload["matches"]]
        return LibrarySearchResult(**payload)

    async def resolve_quote(self, rating_key: int, quote: str) -> ResolveQuoteResult:
        response = await self._client.get(
            f"/resolve-quote/{rating_key}",
            params={"quote": quote},
            timeout=RESOLVE_QUOTE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        payload["matches"] = [QuoteMatchResult(**m) for m in payload["matches"]]
        return ResolveQuoteResult(**payload)

    async def search_quote(self, quote: str) -> LibrarySearchResult:
        response = await self._client.get("/search-quote", params={"quote": quote})
        response.raise_for_status()
        payload = response.json()
        payload["matches"] = [LibraryQuoteMatchResult(**m) for m in payload["matches"]]
        return LibrarySearchResult(**payload)

    async def render(
        self,
        rating_key: int,
        timecode: str,
        duration: float | None = None,
        end_timecode: str | None = None,
        format: str | None = None,
        style: str | None = None,
    ) -> RenderResult:
        response = await self._client.post(
            "/render",
            json={
                "rating_key": rating_key,
                "timecode": timecode,
                "duration": duration,
                "end_timecode": end_timecode,
                "format": format,
                "style": style,
            },
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return RenderResult(
            content=response.content,
            format=response.headers["X-Clip-Format"],
            style=response.headers["X-Clip-Style"],
        )
