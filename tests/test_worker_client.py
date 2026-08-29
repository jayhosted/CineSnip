import asyncio
import json

import httpx
import pytest

from app.bot.worker_client import LibraryQuoteMatchResult, LibrarySearchExtendEvent, WorkerClient


def _client_with_mock(handler) -> WorkerClient:
    client = WorkerClient("http://worker.test")
    client._client = httpx.AsyncClient(base_url="http://worker.test", transport=httpx.MockTransport(handler))
    return client


def _ndjson_response(events: list[dict]) -> httpx.Response:
    body = "\n".join(json.dumps(e) for e in events) + "\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})


async def _collect_events(quote: str, client: WorkerClient) -> list[LibrarySearchExtendEvent]:
    """Helper to collect all events from the stream."""
    return [event async for event in client.search_quote_extend(quote)]


def test_search_quote_extend_parses_all_event_types():
    events = [
        {
            "type": "cached",
            "matches": [
                {
                    "rating_key": 1, "title": "Film", "library_name": "Movies",
                    "start": 1.0, "end": 2.0, "timecode": "0:00:01", "text": "Hi",
                    "score": 90.0, "context_before": [], "context_after": [],
                }
            ],
            "confident_score": 85.0, "min_score": 50.0,
        },
        {"type": "progress", "index": 1, "total": 3, "title": "Uncached Film"},
        {
            "type": "final",
            "matches": [],
            "confident_score": 85.0, "min_score": 50.0,
            "remaining_uncached": 2,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search-quote-extend"
        assert request.url.params["quote"] == "hello"
        return _ndjson_response(events)

    client = _client_with_mock(handler)

    received = asyncio.run(_collect_events("hello", client))

    assert [e.type for e in received] == ["cached", "progress", "final"]
    assert isinstance(received[0].matches[0], LibraryQuoteMatchResult)
    assert received[0].matches[0].title == "Film"
    assert received[1].index == 1 and received[1].total == 3 and received[1].title == "Uncached Film"
    assert received[2].remaining_uncached == 2
    assert received[2].matches == []


def test_search_quote_extend_final_remaining_uncached_null_when_sync_disabled():
    events = [
        {"type": "cached", "matches": [], "confident_score": 85.0, "min_score": 50.0},
        {"type": "final", "matches": [], "confident_score": 85.0, "min_score": 50.0, "remaining_uncached": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(events)

    client = _client_with_mock(handler)

    received = asyncio.run(_collect_events("hello", client))

    assert received[-1].remaining_uncached is None
