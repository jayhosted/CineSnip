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
#
# extraction_timeout_seconds is operator-configurable (config.yaml.example
# explicitly invites raising it for very large files), and this constant
# has no way to know that value — the worker's own timeout-and-clean-error
# mechanism should always win the race, so this is set with generous
# headroom above the *default*, not tightly matched to it. If you raise
# extraction_timeout_seconds past this in config.yaml, raise this too.
RESOLVE_QUOTE_TIMEOUT_SECONDS = 300.0


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
    min_score: float
    matches: list[QuoteMatchResult]


@dataclass
class RenderResult:
    content: bytes
    format: str


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
        payload["matches"] = [QuoteMatchResult(**m) for m in payload["matches"]]
        return ResolveQuoteResult(**payload)

    async def render(
        self,
        rating_key: int,
        timecode: str,
        duration: float | None = None,
        end_timecode: str | None = None,
        format: str | None = None,
    ) -> RenderResult:
        response = await self._client.post(
            "/render",
            json={
                "rating_key": rating_key,
                "timecode": timecode,
                "duration": duration,
                "end_timecode": end_timecode,
                "format": format,
            },
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return RenderResult(
            content=response.content,
            format=response.headers["X-Clip-Format"],
        )
