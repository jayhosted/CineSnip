from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.runtime import SettingsHolder
from app.worker import quote_index, search_index
from app.worker.library_sync import run_library_sync_once

# How often the SSE stream re-checks quote_index.db for a change. Progress
# is persisted there (not held in memory), so this is a poll-and-diff loop,
# not true push — the DB stays the single source of truth, meaning a page
# refresh or a second tab mid-sync always sees identical, correct state.
_SSE_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class CoverageStats:
    cached_total: int
    library_total: int
    sidecar_count: int
    embedded_count: int
    no_subtitle_count: int
    library_count: int


def _coverage_stats(settings_holder: SettingsHolder) -> CoverageStats:
    settings = settings_holder.settings

    sidecar = embedded = no_subtitle = library_total = 0
    for library in settings.libraries:
        counts = search_index.coverage_counts(settings.quote_index_db_path, library.name)
        sidecar += counts["sidecar"]
        embedded += counts["embedded"]
        # no_subtitle_titles is unaffected by the search_index migration —
        # keep reading it via quote_index.library_coverage() for that field
        # only, rather than duplicating its query here.
        no_subtitle += quote_index.library_coverage(settings.quote_index_db_path, library.name).no_subtitle_count
        library_total += quote_index.get_library_item_count(settings.quote_index_db_path, library.name) or 0

    return CoverageStats(
        cached_total=sidecar + embedded,
        library_total=library_total,
        sidecar_count=sidecar,
        embedded_count=embedded,
        no_subtitle_count=no_subtitle,
        library_count=len(settings.libraries),
    )


def _format_duration_seconds(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _last_run_duration(progress: quote_index.SyncProgress) -> str | None:
    # started_at is set once at the start of a run and left untouched by
    # finish_sync_run, so once idle it still holds *that* run's start time —
    # paired with last_synced_at (its completion time), that's a duration
    # with no new column needed.
    if not progress.started_at or not progress.last_synced_at:
        return None
    started = datetime.fromisoformat(progress.started_at)
    finished = datetime.fromisoformat(progress.last_synced_at)
    return _format_duration_seconds((finished - started).total_seconds())


def _sync_panel_state(settings_holder: SettingsHolder) -> tuple:
    settings = settings_holder.settings
    progress = quote_index.get_sync_progress(settings.quote_index_db_path)
    log_lines = quote_index.tail_sync_log(settings.quote_index_db_path)
    return progress, log_lines


def _render_sync_panel(
    templates: Jinja2Templates, request: Request, settings_holder: SettingsHolder, progress, log_lines
) -> str:
    settings = settings_holder.settings
    return templates.env.get_template("panel_dashboard_sync.html").render(
        {
            "request": request,
            "progress": progress,
            "last_run_duration": _last_run_duration(progress) if progress.status == "idle" else None,
            "log_lines": log_lines,
            "sync_enabled": settings.library_sync.enabled,
        }
    )


def register_dashboard_routes(app: FastAPI, templates: Jinja2Templates, settings_holder: SettingsHolder) -> None:
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if settings_holder.settings is None:
            # Setup hasn't completed yet — nothing here can work without a
            # running worker. Send them into the wizard instead.
            return RedirectResponse("/")
        stats = await run_in_threadpool(_coverage_stats, settings_holder)
        settings = settings_holder.settings
        progress, log_lines = await run_in_threadpool(_sync_panel_state, settings_holder)
        context = {
            "request": request,
            "stats": stats,
            "progress": progress,
            "last_run_duration": _last_run_duration(progress) if progress.status == "idle" else None,
            "log_lines": log_lines,
            "sync_enabled": settings.library_sync.enabled,
            "content_template": "panel_dashboard.html",
            "page_title": "Dashboard",
            "current_page": "dashboard",
        }
        return templates.TemplateResponse(request, "shell.html", context)

    @app.post("/sync/run", response_class=HTMLResponse)
    async def sync_run(request: Request):
        if settings_holder.settings is None:
            # Setup hasn't completed yet — nothing here can work without a
            # running worker. Send them into the wizard instead.
            return RedirectResponse("/")
        # start_sync_run's atomic guard (Task 1/2) makes this safe to call
        # unconditionally — if a run is already in progress (scheduled or a
        # previous manual click), run_library_sync_once no-ops immediately
        # rather than racing it, so no separate check is needed here.
        settings = settings_holder.settings
        media_client = settings_holder.media_client
        # Works for either backend (issue #25) — media_client is whichever
        # MediaClient the running worker already has (PlexClient or
        # JellyfinClient), so no branching on media_server is needed here.
        if media_client is not None:
            asyncio.create_task(run_library_sync_once(settings, media_client))
        progress, log_lines = await run_in_threadpool(_sync_panel_state, settings_holder)
        return HTMLResponse(_render_sync_panel(templates, request, settings_holder, progress, log_lines))

    @app.get("/dashboard/sync-stream")
    async def dashboard_sync_stream(request: Request):
        if settings_holder.settings is None:
            # Setup hasn't completed yet — nothing here can work without a
            # running worker. Send them into the wizard instead.
            return RedirectResponse("/")

        async def event_source():
            last_payload = None
            while True:
                if await request.is_disconnected():
                    break
                settings = settings_holder.settings
                progress, log_lines = await run_in_threadpool(_sync_panel_state, settings_holder)
                log_seq = log_lines[-1].seq if log_lines else 0
                # last_synced_at is included so a completed run always
                # produces a fresh payload even when nothing else here
                # changed — a "no changes" run (the common case once a
                # library's stored updatedAt is caught up) finishes without
                # ever touching current_title/processed/total/log_seq, so
                # without this a stuck 'running' snapshot (raced from the
                # POST /sync/run response — see sync_run() above) would
                # never get corrected back to idle.
                payload = (
                    progress.status,
                    progress.current_title,
                    progress.processed,
                    progress.total,
                    log_seq,
                    progress.last_synced_at,
                )
                if payload != last_payload:
                    last_payload = payload
                    html = _render_sync_panel(templates, request, settings_holder, progress, log_lines).replace(
                        "\n", ""
                    )
                    yield f"data: {html}\n\n"
                await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(event_source(), media_type="text/event-stream")
