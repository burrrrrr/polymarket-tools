"""The Discover page: the candidate table and its filters, the scan form and its
progress, and the batch that sends candidates to Claude."""

from contextlib import closing
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import farm_db
import reward_screener
import triage

from .. import forms, views
from ..context import form_of, see_other
from ..scan import ScanPhase


def parse_filters(mode, q, status, min_days, page):
    if mode and mode not in views.MODES:
        raise HTTPException(400, f"no mode {mode!r}")
    if status not in views.STATUS_FILTERS:
        raise HTTPException(400, f"no status {status!r}")
    try:
        days = float(min_days) if min_days not in (None, "") else None
    except ValueError:
        raise HTTPException(400, f"min_days {min_days!r} is not a number") from None
    return views.DiscoverFilters(mode or None, (q or "").strip(), status, days,
                                 max(1, page))


def scan_values(conn):
    """The scan form's {name: text}: the last parameters run, else the screener's defaults."""
    stored = farm_db.load_scan_params(conn)
    # Parameters a scan no longer takes stay in old rows; drop them, and
    # categories the screener no longer offers.
    config = reward_screener.ScreenConfig()._replace(
        **{k: v for k, v in stored.items() if k in reward_screener.ScreenConfig._fields})
    excluded = [c for c in config.excluded_categories if c in reward_screener.CATEGORIES]
    return {**{name: views.number_text(getattr(config, name)) for name in forms.SCAN_FIELDS},
            **{forms.exclude_field(c): "1" for c in excluded}}


def routes(ctx):
    router = APIRouter()

    def triage_status(conn):
        queue = farm_db.candidate_queue(conn)
        done = farm_db.triaged_ids(conn, triage.PROMPT_VERSION)
        return {"triage_running": len(ctx.triager.running()),
                "triage_waiting": len(ctx.triager.pending()),
                "triage_done": sum(c in done for c in queue), "triage_queue": len(queue),
                "prompt_version": triage.PROMPT_VERSION}

    def discover_response(request, conn, filters, status_code=200, values=None, errors=None,
                          refused=None, triage_note=None):
        now = ctx.clock()
        snap = ctx.snapshot(conn, now)
        # A stale map refreshes in the background, sweeping ended rewards from the queue.
        ctx.total_rates.get()
        markets, total = farm_db.search_markets(
            conn, statuses=() if filters.status == "all" else (filters.status,),
            mode=filters.mode, text=filters.q, min_days=filters.min_days,
            buffer_days=ctx.effective(conn, "expiry_buffer_days"), now=now,
            limit=views.DISCOVER_PAGE, offset=(filters.page - 1) * views.DISCOVER_PAGE)
        values = values or scan_values(conn)
        return ctx.templates.TemplateResponse(request, "discover.html", {
            "chrome": ctx.chrome(snap, now), "now": now, "page": "discover",
            "vm": views.discover_page(filters, markets, total, snap.status, now),
            "funnel": farm_db.load_scan_funnel(conn), "job": ctx.scanner.job,
            "fields": forms.SCAN_FIELDS, "values": values, "errors": errors or {},
            "categories": {forms.exclude_field(c): c for c in reward_screener.CATEGORIES},
            "refused": refused, "modes": views.MODES, "statuses": views.STATUS_FILTERS,
            "triage_note": triage_note, "batch_default": forms.TRIAGE_BATCH_DEFAULT,
            **triage_status(conn),
        }, status_code=status_code)

    @router.get("/discover", response_class=HTMLResponse)
    def discover(request: Request, mode: Optional[str] = None, q: str = "",
                 status: str = "candidate", min_days: Optional[str] = None, page: int = 1,
                 triaged: Optional[int] = None):
        filters = parse_filters(mode, q, status, min_days, page)
        note = None
        if triaged is not None:
            note = f"Sent {triaged} candidate{'s' if triaged != 1 else ''} to Claude."
        with closing(ctx.connect()) as conn:
            return discover_response(request, conn, filters, triage_note=note)

    @router.post("/triage/batch")
    async def triage_batch(request: Request):
        n, errors = forms.parse_triage_batch(await form_of(request))
        with closing(ctx.connect()) as conn:
            if errors:
                return discover_response(request, conn, views.DiscoverFilters(), 422,
                                         triage_note=errors["n"])
            sent = ctx.triager.enqueue_top(conn, n)
        return see_other(f"/discover?triaged={sent}")

    @router.get("/partials/discover/triage", response_class=HTMLResponse)
    def discover_triage(request: Request):
        with closing(ctx.connect()) as conn:
            return ctx.templates.TemplateResponse(request, "partials/triage_status.html",
                                                  triage_status(conn))

    @router.post("/discover/scan")
    async def start_scan(request: Request):
        form = await form_of(request)
        config, errors = forms.parse_scan(form)
        with closing(ctx.connect()) as conn:
            if ctx.scanner.job.phase is ScanPhase.RUNNING:
                return discover_response(request, conn, views.DiscoverFilters(), 409,
                                         values=form, refused="A scan is already in progress.")
            if errors:
                return discover_response(request, conn, views.DiscoverFilters(), 422,
                                         values=form, errors=errors)
            if not ctx.scanner.start(config):
                return discover_response(request, conn, views.DiscoverFilters(), 409,
                                         values=form, refused="A scan is already in progress.")
            farm_db.put_scan_params(conn, config._asdict(), ctx.clock())
        return see_other("/discover")

    @router.get("/partials/discover/scan", response_class=HTMLResponse)
    def scan_status(request: Request):
        job = ctx.scanner.job
        response = ctx.templates.TemplateResponse(request, "partials/scan_status.html",
                                                  {"job": job, "now": ctx.clock()})
        if job.phase is not ScanPhase.RUNNING:
            # Only a running scan polls this; reload so the table shows the result.
            response.headers["HX-Refresh"] = "true"
        return response

    return router
