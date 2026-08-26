from __future__ import annotations

from dataclasses import dataclass

import httpx

# Must exceed the worker's own render timeout (config.yaml's
# render_defaults.timeout_seconds, 60s by default) — but a styled /render
# request can ALSO trigger a cold-cache subtitle extraction inline, before
# encoding even starts (app/worker/api.py's /render handler calls
# get_subtitles() first whenever a style is requested), so this must cover
# extraction_timeout_seconds + render_defaults.timeout_seconds in the worst
# case, not just the encode step alone — a gap the original 90s value
# missed entirely (didn't even cover the old 180s extraction default on its
# own). So the worker's clean error response wins the race instead of this
# client timing out first with a generic, harder-to-explain exception.
RENDER_TIMEOUT_SECONDS = 480.0

# Must exceed the worker's subtitle extraction timeout (config.yaml's
# subtitle_defaults.extraction_timeout_seconds, 300s by default) — on a
# cold cache /resolve-quote demuxes an embedded subtitle stream, which has
# no fast seek. The shared 30s client default would abort first. 300s isn't
# a guess: a real 39GB 2160p HDR remux (Akira) measured at 251.6s for a full
# embedded extraction on this developer's real library.
#
# extraction_timeout_seconds is operator-configurable (config.yaml.example
# explicitly invites raising it for very large files), and this constant
# has no way to know that value — the worker's own timeout-and-clean-error
# mechanism should always win the race, so this is set with generous
# headroom above the *default*, not tightly matched to it. If you raise
# extraction_timeout_seconds past this in config.yaml, raise this too (and
# RENDER_TIMEOUT_SECONDS above, which has the same dependency).
RESOLVE_QUOTE_TIMEOUT_SECONDS = 480.0

# /search-episodes-quote may serially cold-extract several never-touched
# episodes in one request, each up to subtitle_defaults.extraction_timeout_seconds
# (300s default) in the worst case. Generous headroom above that for a
# show with many uncached episodes, mirroring RESOLVE_QUOTE_TIMEOUT_SECONDS's
# reasoning — the worker's own per-episode timeout-and-skip should always
# win the race, not this client timing out first. In practice TV episode
# files are far smaller than a feature-length UHD remux, so this worst case
# is mostly theoretical, but the headroom costs nothing to keep generous.
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
