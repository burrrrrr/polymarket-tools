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
class RenderedCard:
    condition_id: str
    html: Markup
    v: str


def fingerprint(text):
    return hashlib.blake2b(text.encode(), digest_size=4).hexdigest()


def order_of(cards):
    return fingerprint(",".join(c.condition_id for c in cards))


def parse_phase(phase):
    if not phase:
        return None
    if phase not in (*views.PHASES, views.WAITING):
        raise HTTPException(400, f"no phase {phase!r}")
    return phase


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

    def markets_vm(conn, now, phase):
        snap = ctx.snapshot(conn, now)
        return snap, views.markets_page(snap, parse_phase(phase), now)

    def rendered(vm):
        card = ctx.templates.env.get_template("partials/card.html").module.card
        return [RenderedCard(c.condition_id, html, fingerprint(html))
                for c in vm.cards for html in (card(c),)]

    @router.get("/markets", response_class=HTMLResponse)
    def markets_page(request: Request, phase: Optional[str] = None):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = markets_vm(conn, now, phase)
        cards = rendered(vm)
        return ctx.render(request, "markets.html", now, snap, vm=vm, page="markets",
                          cards=cards, order=order_of(cards))

    @router.get("/partials/markets", response_class=HTMLResponse)
    def markets_partial(request: Request, phase: Optional[str] = None,
                        order: Optional[str] = None, have: Optional[str] = None):
        """All the cards, or when the page holds the same cards in the same
        order (order), only those whose version differs from the page's, in
        have, joined by dots. A farmer cycle changes a few of hundreds."""
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = markets_vm(conn, now, phase)
        cards = rendered(vm)
        held = have.split(".") if have else []
        if order != order_of(cards) or len(held) != len(cards):
            return ctx.render(request, "partials/cards.html", now, snap, vm=vm,
                              cards=cards, order=order_of(cards))
        changed = [c for c, v in zip(cards, held) if c.v != v]
        if not changed:
            return Response(status_code=204)
        response = ctx.render(request, "partials/cards.html", now, snap, vm=vm,
                              cards=changed, oob=True)
        response.headers["HX-Reswap"] = "none"
        return response

    @router.get("/api/markets")
    def markets_api(phase: Optional[str] = None):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            _, vm = markets_vm(conn, now, phase)
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
