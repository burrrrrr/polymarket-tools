import hashlib
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup

from farm import farm_db
from farm.farm_db import CommandKind, EventKind

from .. import charts, views
from ..context import CONTROLS_EVENT, as_json, form_of, region_template

MARKET_REGIONS = {"summary": "partials/market_summary.html",
                  "chart": "partials/market_chart.html",
                  "live": "partials/market_live.html",
                  "history": "partials/market_history.html"}


@dataclass(frozen=True)
class RenderedRow:
    """A <tr> of the Live markets table: a market's row, or its detail row
    while it is open. v fingerprints what it shows."""

    condition_id: str
    detail: bool
    html: Markup
    v: str


def fingerprint(text):
    return hashlib.blake2b(text.encode(), digest_size=4).hexdigest()


def ids_of(cards):
    return fingerprint(",".join(sorted(c.condition_id for c in cards)))


def parse_phase(phase):
    if not phase:
        return None
    if phase not in (*views.PHASES, views.WAITING):
        raise HTTPException(400, f"no phase {phase!r}")
    return phase


def parse_query(phase, flag, q, sort, limit):
    if flag and flag not in views.FLAGS:
        raise HTTPException(400, f"no flag {flag!r}")
    if sort and sort not in views.SORTS:
        raise HTTPException(400, f"no sort {sort!r}")
    if limit and not (limit.isdigit() and int(limit) > 0):
        raise HTTPException(400, f"limit {limit!r} is not a positive whole number")
    return views.MarketsQuery(parse_phase(phase), flag or None, (q or "").strip(),
                              sort or views.MarketsQuery.sort,
                              int(limit) if limit else views.MARKETS_PAGE)


def chart_of(vm, now):
    return charts.mid_chart(vm.mids, vm.v, vm.markers, now)


def changed():
    return Response(headers={"HX-Trigger": CONTROLS_EVENT})


def level(form):
    value = form.get("value")
    if value not in ("0", "1"):
        raise HTTPException(400, "value must be 0 or 1")
    return value == "1"


def routes(ctx):
    router = APIRouter()

    def markets_vm(conn, now, query):
        snap = ctx.snapshot(conn, now)
        return snap, views.markets_page(snap, query, now)

    def row_macros():
        return ctx.templates.env.get_template("partials/market_row.html").module

    def detail_of(macros, c):
        html = macros.detail(c)
        return RenderedRow(c.condition_id, True, html,
                           fingerprint(f"detail {c.condition_id} {html}"))

    def rows_of(vm, expanded):
        """Each row closed, fingerprinted so; an expanded one open, with its
        detail row after it."""
        macros = row_macros()
        rows = []
        for c in vm.rows:
            closed = macros.cells(c, False)
            is_open = c.condition_id in expanded
            rows.append(RenderedRow(c.condition_id, False,
                                    macros.cells(c, True) if is_open else closed,
                                    fingerprint(f"{c.condition_id} {closed}")))
            if is_open:
                rows.append(detail_of(macros, c))
        return rows

    @router.get("/markets", response_class=HTMLResponse)
    def markets_page(request: Request, phase: Optional[str] = None,
                     flag: Optional[str] = None, q: Optional[str] = None,
                     sort: Optional[str] = None, limit: Optional[str] = None):
        query = parse_query(phase, flag, q, sort, limit)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = markets_vm(conn, now, query)
        return ctx.render(request, "markets.html", now, snap, vm=vm, page="markets",
                          look=vm.look, rows=rows_of(vm, set()), ids=ids_of(vm.rows))

    @router.get("/partials/markets", response_class=HTMLResponse)
    def markets_partial(request: Request, phase: Optional[str] = None,
                        flag: Optional[str] = None, q: Optional[str] = None,
                        sort: Optional[str] = None, limit: Optional[str] = None,
                        ids: Optional[str] = None, have: Optional[str] = None,
                        expanded: Optional[str] = None):
        """The whole table, or when the page holds the same rows (ids) only
        the rows it holds no version of (have, dot-joined), each swapped in
        place, so rows never re-sort under the reader. A farmer cycle changes
        a few. expanded, dot-joined, are the rows open on the page."""
        query = parse_query(phase, flag, q, sort, limit)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = markets_vm(conn, now, query)
        rows = rows_of(vm, set(expanded.split(".")) if expanded else set())
        if ids != ids_of(vm.rows):
            return ctx.render(request, "partials/market_table.html", now, snap, vm=vm,
                              rows=rows, ids=ids_of(vm.rows))
        held = set(have.split(".")) if have else set()
        changed = [r for r in rows if r.v not in held]
        if not changed:
            return Response(status_code=204)
        response = ctx.render(request, "partials/market_table.html", now, snap, vm=vm,
                              rows=changed, oob=True)
        response.headers["HX-Reswap"] = "none"
        return response

    @router.get("/partials/markets/look", response_class=HTMLResponse)
    def look_partial(request: Request):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap = ctx.snapshot(conn, now)
        look = views.needs_a_look(views.alert_groups(views.alerts(snap, now)), snap)
        return ctx.render(request, "partials/look.html", now, snap, look=look)

    @router.get("/partials/markets/{condition_id}/card", response_class=HTMLResponse)
    def detail_partial(request: Request, condition_id: str):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap = ctx.snapshot(conn, now)
        c = views.tracked_card(snap, condition_id, now)
        if c is None:
            raise HTTPException(404, f"no tracked market {condition_id}")
        return ctx.render(request, "partials/market_table.html", now, snap,
                          rows=[detail_of(row_macros(), c)], bare=True)

    @router.get("/api/markets")
    def markets_api(phase: Optional[str] = None, flag: Optional[str] = None,
                    q: Optional[str] = None, sort: Optional[str] = None,
                    limit: Optional[str] = None):
        query = parse_query(phase, flag, q, sort, limit)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            _, vm = markets_vm(conn, now, query)
        return as_json(vm)

    def market_vm(conn, now, condition_id):
        snap = ctx.snapshot(conn, now)
        market = ctx.find(conn, condition_id)
        events = (farm_db.read_events(conn, condition_id=condition_id, limit=500)
                  + farm_db.read_events(conn, kinds=(EventKind.START,), limit=50))
        events.sort(key=lambda e: -e.id)
        mids = farm_db.read_mids(conn, condition_id, now - timedelta(hours=24))
        vm = views.market_detail(market, snap.states.get(condition_id), snap.status,
                                 events, mids, now)
        return snap, vm

    @router.get("/markets/{condition_id}", response_class=HTMLResponse)
    def market_page(request: Request, condition_id: str):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = market_vm(conn, now, condition_id)
        return ctx.render(request, "market.html", now, snap, vm=vm, chart=chart_of(vm, now),
                          page="markets")

    @router.get("/partials/markets/{condition_id}/{region}", response_class=HTMLResponse)
    def market_partial(request: Request, condition_id: str, region: str):
        template = region_template(MARKET_REGIONS, region)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = market_vm(conn, now, condition_id)
        return ctx.render(request, template, now, snap, vm=vm, chart=chart_of(vm, now))

    @router.get("/api/markets/{condition_id}")
    def market_api(condition_id: str):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            _, vm = market_vm(conn, now, condition_id)
        return as_json(vm)

    @router.post("/markets/{condition_id}/paused")
    async def set_paused(request: Request, condition_id: str):
        paused = level(await form_of(request))
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
            farm_db.set_paused(conn, condition_id, paused)
        return changed()

    @router.post("/markets/{condition_id}/naked-paused")
    async def set_naked_paused(request: Request, condition_id: str):
        paused = level(await form_of(request))
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
            farm_db.set_naked_paused(conn, condition_id, paused)
        return changed()

    @router.post("/markets/{condition_id}/sole-bid")
    async def set_sole_bid(request: Request, condition_id: str):
        on = level(await form_of(request))
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
            farm_db.set_sole_bid(conn, condition_id, on)
        return changed()

    @router.post("/markets/{condition_id}/end-cooldown")
    async def end_cooldown(request: Request, condition_id: str):
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
            farm_db.send_command(conn, CommandKind.END_COOLDOWN, ctx.clock(),
                                 {"condition_id": condition_id})
        return changed()

    @router.post("/markets/{condition_id}/retire")
    async def retire(request: Request, condition_id: str):
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
            farm_db.retire_market(conn, condition_id, "retired from the console")
        return changed()

    return router
