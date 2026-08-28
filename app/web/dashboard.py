from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.runtime import SettingsHolder
from app.worker import quote_index
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
    plex = settings_holder.plex_client
    sections_by_name = dict(plex.library_sections()) if plex is not None else {}

    sidecar = embedded = no_subtitle = library_total = 0
    for library in settings.libraries:
        coverage = quote_index.library_coverage(settings.quote_index_db_path, library.name)
        sidecar += coverage.sidecar_count
        embedded += coverage.embedded_count
        no_subtitle += coverage.no_subtitle_count
        section = sections_by_name.get(library.name)
        if plex is not None and section is not None:
            library_total += len(plex.enumerate_section(section))

    return CoverageStats(
        cached_total=sidecar + embedded,
        library_total=library_total,
        sidecar_count=sidecar,
        embedded_count=embedded,
        no_subtitle_count=no_subtitle,
        library_count=len(settings.libraries),
    )


def _sync_panel_html(templates: Jinja2Templates, request: Request, settings_holder: SettingsHolder) -> str:
    settings = settings_holder.settings
    progress = quote_index.get_sync_progress(settings.quote_index_db_path)
    log_lines = quote_index.tail_sync_log(settings.quote_index_db_path)
    return templates.env.get_template("panel_dashboard_sync.html").render(
        {
            "request": request,
            "progress": progress,
            "log_lines": log_lines,
            "sync_enabled": settings.library_sync.enabled,
        }
    )


def register_dashboard_routes(app: FastAPI, templates: Jinja2Templates, settings_holder: SettingsHolder) -> None:
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        stats = await run_in_threadpool(_coverage_stats, settings_holder)
        settings = settings_holder.settings
        progress = quote_index.get_sync_progress(settings.quote_index_db_path)
        log_lines = quote_index.tail_sync_log(settings.quote_index_db_path)
        context = {
            "request": request,
            "stats": stats,
            "progress": progress,
            "log_lines": log_lines,
            "sync_enabled": settings.library_sync.enabled,
            "content_template": "panel_dashboard.html",
            "page_title": "Dashboard",
            "current_page": "dashboard",
        }
        return templates.TemplateResponse(request, "shell.html", context)

    @app.post("/sync/run", response_class=HTMLResponse)
    async def sync_run(request: Request):
        # start_sync_run's atomic guard (Task 1/2) makes this safe to call
        # unconditionally — if a run is already in progress (scheduled or a
        # previous manual click), run_library_sync_once no-ops immediately
        # rather than racing it, so no separate check is needed here.
        settings = settings_holder.settings
        plex = settings_holder.plex_client
        if plex is not None:
            asyncio.create_task(run_library_sync_once(settings, plex))
        return HTMLResponse(_sync_panel_html(templates, request, settings_holder))

    @app.get("/dashboard/sync-stream")
    async def dashboard_sync_stream(request: Request):
        async def event_source():
            last_payload = None
            while True:
                if await request.is_disconnected():
                    break
                settings = settings_holder.settings
                progress = quote_index.get_sync_progress(settings.quote_index_db_path)
                log_seq = quote_index.latest_sync_log_seq(settings.quote_index_db_path)
                payload = (progress.status, progress.current_title, progress.processed, progress.total, log_seq)
                if payload != last_payload:
                    last_payload = payload
                    html = _sync_panel_html(templates, request, settings_holder).replace("\n", "")
                    yield f"data: {html}\n\n"
                await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(event_source(), media_type="text/event-stream")
