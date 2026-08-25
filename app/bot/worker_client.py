from __future__ import annotations

from dataclasses import dataclass

import httpx

# Must exceed the worker's own render timeout (config.yaml's
# render_defaults.timeout_seconds, 60s by default) so the worker's clean
# error response wins the race instead of this client timing out first
# with a generic, harder-to-explain exception.
RENDER_TIMEOUT_SECONDS = 90.0

# Must exceed the worker's subtitle extraction timeout (config.yaml's
# subtitle_defaults.extraction_timeout_seconds, 180s by default) — on a
# cold cache /resolve-quote demuxes an embedded subtitle stream, which has
# no fast seek. The shared 30s client default would abort first.
RESOLVE_QUOTE_TIMEOUT_SECONDS = 200.0


@dataclass
class MovieResult:
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None


@dataclass
class ResolveResult:
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None


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
    matches: list[QuoteMatchResult]


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

    async def resolve_quote(self, rating_key: int, quote: str) -> ResolveQuoteResult:
        response = await self._client.get(
            f"/resolve-quote/{rating_key}",
            params={"quote": quote},
            timeout=RESOLVE_QUOTE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        return ResolveQuoteResult(
            rating_key=payload["rating_key"],
            title=payload["title"],
            subtitle_source=payload["subtitle_source"],
            confident_score=payload["confident_score"],
            matches=[QuoteMatchResult(**m) for m in payload["matches"]],
        )

    async def render(self, rating_key: int, timecode: str) -> bytes:
        response = await self._client.post(
            "/render",
            json={"rating_key": rating_key, "timecode": timecode},
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.content
