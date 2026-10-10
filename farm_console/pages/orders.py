"""The Orders page: the snapshots of the account's open orders, and the job that
asks the worker for a new one."""

from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from tools import db as tools_db
from tools.db import JobKind

from .. import forms
from ..context import form_of, region_template, see_other

ORDERS_REGIONS = {"job": "partials/orders_job.html",
                  "snapshots": "partials/orders_snapshots.html"}


def routes(ctx):
    router = APIRouter()

    def render(request, template, status_code=200, **context):
        """A tool page: its banner is the worker's, not the farmer's."""
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            snap = ctx.snapshot(conn, now)
        return ctx.render(request, template, now, snap, status_code, page="orders",
                          process="worker", **context)

    def orders_context(conn):
        job = tools_db.latest_job(conn, JobKind.SNAPSHOT)
        return {"job": job, "log": tools_db.read_log(conn, job.id) if job else [],
                "snapshots": tools_db.read_snapshots(conn)}

    def orders_response(request, status_code=200, **context):
        with closing(ctx.connect_tools()) as conn:
            return render(request, "orders.html", status_code, **orders_context(conn), **context)

    @router.get("/orders", response_class=HTMLResponse)
    def orders_page(request: Request):
        if ctx.tools_db is None:
            return render(request, "orders.html")
        return orders_response(request)

    @router.post("/orders/snapshot")
    async def take_snapshot(request: Request):
        form = await form_of(request)
        note, errors = forms.parse_snapshot_note(form)
        if errors:
            return orders_response(request, 422, errors=errors, note=form.get("note"))
        with closing(ctx.connect_tools()) as conn:
            try:
                tools_db.enqueue_job(conn, JobKind.SNAPSHOT, {"note": note}, ctx.clock())
            except tools_db.JobOpen as e:
                return orders_response(request, 409, refused=str(e), note=note)
        return see_other("/orders")

    @router.get("/orders/snapshots/{snapshot_id}", response_class=HTMLResponse)
    def snapshot_page(request: Request, snapshot_id: int):
        with closing(ctx.connect_tools()) as conn:
            snapshot = tools_db.read_snapshot(conn, snapshot_id)
            if snapshot is None:
                raise HTTPException(404, f"no snapshot {snapshot_id} in {ctx.tools_db}")
            orders = tools_db.read_snapshot_orders(conn, snapshot_id)
        return render(request, "orders_snapshot.html", snapshot=snapshot, orders=orders)

    @router.get("/partials/orders/{region}", response_class=HTMLResponse)
    def orders_partial(request: Request, region: str):
        template = region_template(ORDERS_REGIONS, region)
        with closing(ctx.connect_tools()) as conn:
            return render(request, template, **orders_context(conn))

    return router
