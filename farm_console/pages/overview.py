from contextlib import closing

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from farm import farm_db

from .. import views
from ..context import as_json, region_template

OVERVIEW_REGIONS = {"kpis": "partials/kpis.html", "alerts": "partials/alerts.html",
                    "tracked": "partials/tracked.html", "events": "partials/events.html"}
CHROME_REGIONS = {"status": "partials/status.html", "banner": "partials/banner.html",
                  "worker-banner": "partials/worker_banner.html"}


def routes(ctx):
    router = APIRouter()

    def overview_vm(conn, now):
        snap = ctx.snapshot(conn, now)
        return snap, views.overview(snap, farm_db.read_events(conn, limit=views.FEED_EVENTS),
                                    now)

    @router.get("/", response_class=HTMLResponse)
    def overview_page(request: Request):
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = overview_vm(conn, now)
        return ctx.render(request, "overview.html", now, snap, vm=vm, page="overview")

    @router.get("/partials/overview/{region}", response_class=HTMLResponse)
    def overview_partial(request: Request, region: str):
        template = region_template(OVERVIEW_REGIONS, region)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = overview_vm(conn, now)
        return ctx.render(request, template, now, snap, vm=vm)

    @router.get("/partials/chrome/{region}", response_class=HTMLResponse)
    def chrome_partial(request: Request, region: str):
        template = region_template(CHROME_REGIONS, region)
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap = ctx.snapshot(conn, now)
        return ctx.render(request, template, now, snap)

    @router.get("/api/overview")
    def overview_api():
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap, vm = overview_vm(conn, now)
        return as_json({"chrome": ctx.chrome(snap, now), "overview": vm})

    return router
