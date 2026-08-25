from __future__ import annotations

from dataclasses import dataclass

import httpx

# Must exceed the worker's own render timeout (config.yaml's
# render_defaults.timeout_seconds, 60s by default) so the worker's clean
# error response wins the race instead of this client timing out first
# with a generic, harder-to-explain exception.
RENDER_TIMEOUT_SECONDS = 90.0


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

    async def render(self, rating_key: int, timecode: str) -> bytes:
        response = await self._client.post(
            "/render",
            json={"rating_key": rating_key, "timecode": timecode},
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.content
