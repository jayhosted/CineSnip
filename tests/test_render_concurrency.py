"""Covers app.state.render_semaphore (issue #17) — the global cap on
simultaneous ffmpeg+gifsicle render work added on top of the two CPU
optimizations (single-pass GIF encoding, bounded-parallelism gifsicle
search) that same benchmarking produced. Drives /render concurrently via
httpx.AsyncClient over an ASGI transport (not the sync TestClient used
elsewhere) since these tests need to observe in-flight concurrency, not
just a single request/response.
"""

import asyncio

import httpx
import pytest

from app.settings import LibraryConfig, PathMapping, Settings
from app.worker import api as api_module
from app.worker.plex_client import MovieResult


class _FakePlexClient:
    def __init__(self, settings: Settings, movie: MovieResult) -> None:
        self._movie = movie

    def get_movie(self, rating_key: int) -> MovieResult:
        return self._movie


class _ControllableRenderer:
    """Tracks how many render_clip calls are concurrently in flight (i.e.
    already past the semaphore and doing the "expensive" work), and blocks
    each one until the test hands out a "permit" via release() — lets a
    test observe exactly how many requests are inside the gated section at
    once, and release them one at a time rather than all-or-nothing (a
    plain shared asyncio.Event can't do that: once set it stays set, so a
    later call that reaches the same wait() sails straight through)."""

    def __init__(self) -> None:
        self.active = 0
        self.peak_active = 0
        self.completed = 0
        self._permits: asyncio.Queue[None] = asyncio.Queue()
        self.fail_next = False

    def release(self, n: int = 1) -> None:
        for _ in range(n):
            self._permits.put_nowait(None)

    async def render_clip(self, *args, **kwargs) -> bytes:
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("simulated render failure")
            await self._permits.get()
            return b"clip-bytes"
        finally:
            self.active -= 1
            self.completed += 1


def _settings(tmp_path, max_concurrent_renders: int = 3) -> Settings:
    settings = Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
        libraries=[
            LibraryConfig(
                name="Movies",
                path_mappings=[
                    PathMapping(path_prefix="/media", container_path=str(tmp_path))
                ],
            )
        ],
    )
    settings.render_defaults.max_concurrent_renders = max_concurrent_renders
    return settings


def _movie() -> MovieResult:
    # plex_path matches the LibraryConfig path_mapping in _settings()
    # (path_prefix "/media" -> container_path=tmp_path) — the real file on
    # disk is written at tmp_path / "movie.mkv" by each test.
    return MovieResult(
        rating_key=1, title="The Matrix", year=1999, duration_ms=8_160_000,
        thumb_url=None, plex_path="/media/movie.mkv", guid="guid-1",
        library_name="Movies",
    )


def _make_app(settings: Settings, monkeypatch, renderer):
    monkeypatch.setattr(api_module, "PlexClient", lambda s: _FakePlexClient(s, _movie()))
    app = api_module.create_app(settings)
    app.state.renderer = renderer
    return app


async def _render(client: httpx.AsyncClient, timecode: str = "62") -> httpx.Response:
    return await client.post("/render", json={"rating_key": 1, "timecode": timecode})


def test_default_max_concurrent_renders_is_three(tmp_path):
    assert _settings(tmp_path).render_defaults.max_concurrent_renders == 3


def test_up_to_the_limit_can_proceed_concurrently(tmp_path, monkeypatch):
    renderer = _ControllableRenderer()
    (tmp_path / "movie.mkv").write_bytes(b"fake")
    app = _make_app(_settings(tmp_path, max_concurrent_renders=3), monkeypatch, renderer)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tasks = [asyncio.create_task(_render(client)) for _ in range(3)]
            # Give the event loop a beat to let all 3 acquire the semaphore
            # and reach the (currently blocked) render call.
            await asyncio.sleep(0.05)
            assert renderer.active == 3
            renderer.release(3)
            responses = await asyncio.gather(*tasks)
            for response in responses:
                assert response.status_code == 200

    asyncio.run(scenario())
    assert renderer.peak_active == 3


def test_a_request_beyond_the_limit_waits_for_a_free_slot(tmp_path, monkeypatch):
    renderer = _ControllableRenderer()
    (tmp_path / "movie.mkv").write_bytes(b"fake")
    app = _make_app(_settings(tmp_path, max_concurrent_renders=3), monkeypatch, renderer)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tasks = [asyncio.create_task(_render(client)) for _ in range(4)]
            await asyncio.sleep(0.05)
            # Only 3 slots exist — the 4th request must still be waiting on
            # the semaphore, never having entered render_clip at all.
            assert renderer.active == 3
            assert renderer.completed == 0
            assert not tasks[3].done()
            renderer.release(4)
            responses = await asyncio.gather(*tasks)
            for response in responses:
                assert response.status_code == 200

    asyncio.run(scenario())
    # Never more than 3 concurrently, even though 4 were in flight.
    assert renderer.peak_active == 3
    assert renderer.completed == 4


def test_waiting_request_proceeds_once_a_slot_is_released(tmp_path, monkeypatch):
    renderer = _ControllableRenderer()
    (tmp_path / "movie.mkv").write_bytes(b"fake")
    app = _make_app(_settings(tmp_path, max_concurrent_renders=1), monkeypatch, renderer)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(_render(client))
            await asyncio.sleep(0.05)
            assert renderer.active == 1

            second = asyncio.create_task(_render(client))
            await asyncio.sleep(0.05)
            # Cap is 1 — the second request must still be waiting on the
            # semaphore itself, not inside render_clip, while the first
            # holds the only slot.
            assert renderer.active == 1
            assert renderer.completed == 0
            assert not second.done()

            # Release exactly one permit: lets the first render_clip call
            # finish, which releases the semaphore and lets the
            # second (already waiting on it) acquire it and enter
            # render_clip in turn.
            renderer.release(1)
            first_response = await first
            assert first_response.status_code == 200

            await asyncio.sleep(0.05)
            assert renderer.active == 1  # the second request made it in
            assert renderer.completed == 1

            renderer.release(1)
            second_response = await second
            assert second_response.status_code == 200

    asyncio.run(scenario())
    assert renderer.completed == 2


def test_semaphore_is_released_after_a_render_failure(tmp_path, monkeypatch):
    renderer = _ControllableRenderer()
    (tmp_path / "movie.mkv").write_bytes(b"fake")
    settings = _settings(tmp_path, max_concurrent_renders=1)
    app = _make_app(settings, monkeypatch, renderer)
    renderer.fail_next = True

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # api.py converts a RuntimeError from the render step into a
            # clean 500 (not a raw exception) — but the point of this test
            # is what happens to the semaphore, not the response shape.
            failing_response = await _render(client)
            assert failing_response.status_code == 500

            # A single-slot semaphore must be fully released after the
            # failing call — `async with` guarantees release even on
            # exception — so a second request must be able to acquire it
            # immediately rather than hanging forever behind a leaked lock.
            second = asyncio.create_task(_render(client))
            await asyncio.sleep(0.05)
            assert renderer.active == 1
            renderer.release(1)
            response = await second
            assert response.status_code == 200

    asyncio.run(scenario())
    assert app.state.render_semaphore._value == settings.render_defaults.max_concurrent_renders
    assert app.state.render_semaphore.locked() is False


def test_render_output_and_headers_unchanged_with_semaphore_in_place(tmp_path, monkeypatch):
    # Confirms the semaphore is purely a concurrency gate — a normal solo
    # request still gets the same response shape/headers as before.
    renderer = _ControllableRenderer()
    renderer.release(1)  # don't block — this test only cares about output
    (tmp_path / "movie.mkv").write_bytes(b"fake")
    app = _make_app(_settings(tmp_path), monkeypatch, renderer)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _render(client, timecode="62")
            assert response.status_code == 200
            assert response.content == b"clip-bytes"
            assert response.headers["X-Clip-Format"] == "gif"
            assert response.headers["X-Clip-Start"] == "62.0"

    asyncio.run(scenario())
