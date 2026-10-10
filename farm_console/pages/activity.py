from contextlib import closing
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import farm_db
from farm_db import EventKind

from .. import charts, views
from ..context import as_json

ACTIVITY_TEMPLATES = {tab: f"partials/activity_{tab}.html" for tab in views.TABS}


def activity_context(snap, vm, tab, now):
    context = {"vm": vm, "tab": tab}
    if tab == "rewards":
        names = {r.condition_id: r.name for r in vm.rows}
        ids = {cid for _, cid, _ in vm.daily}
        context["chart"] = charts.rewards_bars(vm.daily, names, now)
        context["legend"] = charts.legend(names, ids)
    return context


def parse_activity(tab, kind):
    if tab not in views.TABS:
        raise HTTPException(404, f"no tab {tab!r}")
    if kind and kind not in {k.value for k in EventKind}:
        raise HTTPException(400, f"no event kind {kind!r}")


def routes(ctx):
    router = APIRouter()

    def activity_vm(conn, now, tab, kind, condition_id, before):
        snap = ctx.snapshot(conn, now)
        if tab == "rewards":
            vm = views.rewards_tab(snap, farm_db.read_reward_totals(conn), now)
        elif tab == "fills":
            vm = views.fills_tab(snap, farm_db.read_events(conn, kinds=(EventKind.FILL,),
                                                           limit=500))
        elif tab == "inventory":
            vm = views.inventory_tab(snap)
        else:
            kinds = (EventKind(kind),) if kind else ()
            vm = views.log_page(snap, farm_db.read_events(
                conn, kinds=kinds, condition_id=condition_id or None, before_id=before,
                limit=views.LOG_PAGE + 1), kind, condition_id)
        return snap, vm

    @router.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, tab: str = "rewards", kind: Optional[str] = None,
                      condition_id: Optional[str] = None, before: Optional[int] = None):
        parse_activity(tab, kind)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = activity_vm(conn, now, tab, kind, condition_id, before)
        return ctx.render(request, "activity.html", now, snap, page="activity",
                          before=before, **activity_context(snap, vm, tab, now))

    @router.get("/partials/activity", response_class=HTMLResponse)
    def activity_partial(request: Request, tab: str = "rewards", kind: Optional[str] = None,
                         condition_id: Optional[str] = None, before: Optional[int] = None):
        parse_activity(tab, kind)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = activity_vm(conn, now, tab, kind, condition_id, before)
        return ctx.render(request, ACTIVITY_TEMPLATES[tab], now, snap, before=before,
                          **activity_context(snap, vm, tab, now))

    @router.get("/api/activity")
    def activity_api(tab: str = "rewards", kind: Optional[str] = None,
                     condition_id: Optional[str] = None, before: Optional[int] = None):
        parse_activity(tab, kind)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            _, vm = activity_vm(conn, now, tab, kind, condition_id, before)
        return as_json({"tab": tab, "activity": vm})

    @router.get("/api/events")
    def events_api(after: Optional[int] = None, before: Optional[int] = None,
                   limit: int = 100, condition_id: Optional[str] = None):
        with closing(ctx.connect()) as conn:
            events = farm_db.read_events(conn, condition_id=condition_id, after_id=after,
                                         before_id=before, limit=max(1, min(limit, 1000)))
        return as_json([views.event_view(e) for e in events])

    return router
