import asyncio
import json

import httpx

from app.bot.worker_client import (
    LibraryQuoteMatchResult,
    LibrarySearchExtendEvent,
    RandomQuoteResult,
    RenderResult,
    SubtitleEntryResult,
    WorkerClient,
)


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
                    "media_id": "1", "title": "Film", "library_name": "Movies",
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


def test_random_quote_sends_quote_and_media_params_and_parses_result():
    body = {
        "media_id": "101", "title": "Monty Python", "library_name": "Movies",
        "start": 1.0, "end": 2.0, "timecode": "0:00:01", "text": "Nobody expects it.",
        "entry_id": 5, "pool_size": 3, "exhausted": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/random-quote"
        assert request.url.params["quote"] == "nobody"
        assert request.url.params["media"] == "movie"
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)

    result = asyncio.run(client.random_quote("nobody", "movie"))

    assert isinstance(result, RandomQuoteResult)
    assert result.media_id == "101"
    assert result.text == "Nobody expects it."
    assert result.entry_id == 5
    assert result.pool_size == 3
    assert result.exhausted is False


def test_random_quote_omits_quote_param_when_none():
    body = {
        "media_id": "202", "title": "Some Show", "library_name": "TV Shows",
        "start": 1.0, "end": 2.0, "timecode": "0:00:01", "text": "Anything.",
        "entry_id": 9, "pool_size": 1, "exhausted": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "quote" not in request.url.params
        assert request.url.params["media"] == "all"
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)

    result = asyncio.run(client.random_quote(None, "all"))

    assert result.media_id == "202"


def test_random_quote_sends_exclude_and_most_recent_params():
    body = {
        "media_id": "202", "title": "Some Show", "library_name": "TV Shows",
        "start": 1.0, "end": 2.0, "timecode": "0:00:01", "text": "Anything.",
        "entry_id": 9, "pool_size": 3, "exhausted": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("exclude") == ["3", "7"]
        assert request.url.params["most_recent"] == "7"
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)

    result = asyncio.run(
        client.random_quote(None, "all", exclude_entry_ids=frozenset({3, 7}), most_recent_entry_id=7)
    )

    assert result.exhausted is True


def test_random_line_hits_media_id_scoped_endpoint():
    body = {
        "media_id": "101", "title": "Monty Python", "library_name": "Movies",
        "start": 1.0, "end": 2.0, "timecode": "0:00:01", "text": "Nobody expects it.",
        "entry_id": 5, "pool_size": 3, "exhausted": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/random-line/101"
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)

    result = asyncio.run(client.random_line("101"))

    assert result.media_id == "101"


def test_random_line_show_sends_season_and_episode_when_given():
    body = {
        "media_id": "501", "title": "The Office — S01E01 — Pilot", "library_name": "TV Shows",
        "start": 1.0, "end": 2.0, "timecode": "0:00:01", "text": "Line.",
        "entry_id": 1, "pool_size": 1, "exhausted": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/random-line-show/900"
        assert request.url.params["season"] == "1"
        assert request.url.params["episode"] == "1"
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)

    result = asyncio.run(client.random_line_show("900", season=1, episode=1))

    assert result.media_id == "501"


def test_random_line_show_omits_season_episode_when_whole_show():
    body = {
        "media_id": "501", "title": "The Office — S01E01 — Pilot", "library_name": "TV Shows",
        "start": 1.0, "end": 2.0, "timecode": "0:00:01", "text": "Line.",
        "entry_id": 1, "pool_size": 1, "exhausted": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "season" not in request.url.params
        assert "episode" not in request.url.params
        return httpx.Response(200, json=body)

    client = _client_with_mock(handler)

    result = asyncio.run(client.random_line_show("900"))

    assert result.media_id == "501"


def test_subtitles_parses_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "media_id": "1", "guid": "g", "source": "sidecar",
                "sidecar_path": "x.srt", "stream_index": None, "entry_count": 2,
                "entries": [
                    {"index": 1, "start": 0.0, "end": 2.0, "text": "hi"},
                    {"index": 2, "start": 2.0, "end": 4.0, "text": "there"},
                ],
            },
        )

    client = _client_with_mock(handler)
    result = asyncio.run(client.subtitles("1"))

    assert result == [
        SubtitleEntryResult(index=1, start=0.0, end=2.0, text="hi"),
        SubtitleEntryResult(index=2, start=2.0, end=4.0, text="there"),
    ]


def test_render_sends_start_end_and_subtitle_overrides_when_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"clip-bytes",
            headers={
                "X-Clip-Format": "gif", "X-Clip-Style": "classic",
                "X-Clip-Start": "10.0", "X-Clip-Duration": "5.0",
            },
        )

    client = _client_with_mock(handler)
    result = asyncio.run(
        client.render(
            media_id="1", start=10.0, end=15.0, style="classic",
            subtitle_overrides={3: None, 4: "edited text"},
        )
    )

    assert captured["body"]["start"] == 10.0
    assert captured["body"]["end"] == 15.0
    assert captured["body"]["subtitle_overrides"] == {"3": None, "4": "edited text"}
    assert result == RenderResult(
        content=b"clip-bytes", format="gif", style="classic", start=10.0, duration=5.0
    )


def test_render_coerces_int_media_id_to_str_in_json_body():
    """Bare-timecode calls (both /snip movie and /snip tv) still pass an int
    rating_key through gif.py's int(film) parsing. WorkerClient.render() must
    coerce it to str before building the request body, since RenderRequest.media_id
    is typed str and pydantic rejects a raw JSON number outright."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"clip-bytes",
            headers={
                "X-Clip-Format": "gif", "X-Clip-Style": "classic",
                "X-Clip-Start": "0.0", "X-Clip-Duration": "4.0",
            },
        )

    client = _client_with_mock(handler)
    asyncio.run(client.render(media_id=123, timecode="0:00:01"))

    assert captured["body"]["media_id"] == "123"
    assert isinstance(captured["body"]["media_id"], str)
