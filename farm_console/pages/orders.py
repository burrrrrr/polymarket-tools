from contextlib import closing
from functools import partial
from typing import NamedTuple, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import farm_db
from tools import db as tools_db
from tools.db import JobKind, JobState, RestoreAction

from .. import forms
from ..context import form_of, region_template, see_other

ORDERS_REGIONS = {"job": "partials/orders_job.html",
                  "snapshots": "partials/orders_snapshots.html"}
PREVIEW_FIRST = "Preview the restore first: placing follows a finished preview of this snapshot."


class LineHeading(NamedTuple):
    """How the page heads the lines of one action: its title and badge tone."""

    action: RestoreAction
    title: str
    tone: str


LINE_HEADINGS = (
    LineHeading(RestoreAction.ERROR, "Error", "t-r"),
    LineHeading(RestoreAction.REJECTED, "Rejected by the exchange", "t-r"),
    LineHeading(RestoreAction.PLACED, "Placed", "t-q"),
    LineHeading(RestoreAction.WOULD_PLACE, "Would place", "t-q"),
    LineHeading(RestoreAction.SKIP_LIVE, "Skipped: already open", "t-n"),
    LineHeading(RestoreAction.SKIP_EXPIRED, "Skipped: expired", "t-n"),
    LineHeading(RestoreAction.SKIP_FARMER, "Skipped: the farmer's market", "t-n"),
    LineHeading(RestoreAction.SKIP_FILLED, "Skipped: nothing left to fill", "t-n"),
)


class LineGroup(NamedTuple):
    """The lines of one action. placed is whether they carry a new order's id."""

    title: str
    tone: str
    lines: list
    placed: bool


class RestoreView(NamedTuple):
    """The newest preview or restore job of a snapshot, as its page shows it.
    to_place is how many orders a finished preview would place, and None for
    any other job: only then does the page offer to place them."""

    job: tools_db.Job
    preview: bool
    log: list
    groups: list
    stored: int
    to_place: Optional[int]


def restore_view(job, log, lines):
    preview = job.kind is JobKind.PREVIEW
    groups = [LineGroup(heading.title, heading.tone, found,
                        heading.action is RestoreAction.PLACED)
              for heading in LINE_HEADINGS
              if (found := [line for line in lines if line.action is heading.action])]
    to_place = None
    if preview and job.state is JobState.DONE:
        to_place = sum(line.action is RestoreAction.WOULD_PLACE for line in lines)
    return RestoreView(job, preview, log, groups, len(lines), to_place)


def placing_refusal(last):
    """Why a snapshot whose newest preview or restore job is `last` cannot be
    placed now, or None when that job is a finished preview."""
    if last is not None and last.state in (JobState.QUEUED, JobState.RUNNING):
        return f"{last.kind.value.capitalize()} job {last.id} is still {last.state.value}."
    if last is None or last.kind is not JobKind.PREVIEW or last.state is not JobState.DONE:
        return PREVIEW_FIRST
    return None


def routes(ctx):
    router = APIRouter()
    render = partial(ctx.render_tool_page, page="orders")

    def orders_context(conn):
        job = tools_db.latest_job(conn, JobKind.SNAPSHOT)
        return {"job": job, "log": tools_db.read_log(conn, job.id) if job else [],
                "snapshots": tools_db.read_snapshots(conn)}

    def orders_response(request, status_code=200, **context):
        with closing(ctx.connect_tools()) as conn:
            return render(request, "orders.html", status_code, **orders_context(conn), **context)

    def find_snapshot(conn, snapshot_id):
        snapshot = tools_db.read_snapshot(conn, snapshot_id)
        if snapshot is None:
            raise HTTPException(404, f"no snapshot {snapshot_id} in {ctx.tools_db}")
        return snapshot

    def restore_context(conn, snapshot):
        job = tools_db.latest_restore_job(conn, snapshot.id)
        restore = job and restore_view(job, tools_db.read_log(conn, job.id),
                                       tools_db.read_restore_lines(conn, job.id))
        return {"snapshot": snapshot, "restore": restore, "confirm_word": forms.RESTORE_WORD,
                "max_wait": tools_db.RESTORE_MAX_WAIT}

    def snapshot_response(request, conn, snapshot, status_code=200, **context):
        return render(request, "orders_snapshot.html", status_code,
                      orders=tools_db.read_snapshot_orders(conn, snapshot.id),
                      **restore_context(conn, snapshot), **context)

    def queue_restore_job(request, conn, snapshot, kind):
        with closing(ctx.connect()) as farm:
            managed = sorted(farm_db.load_approved(farm))
        try:
            tools_db.enqueue_job(conn, kind, {"snapshot_id": snapshot.id,
                                              "managed_markets": managed}, ctx.clock())
        except tools_db.JobOpen as e:
            return snapshot_response(request, conn, snapshot, 409, refused=str(e))
        return see_other(f"/orders/snapshots/{snapshot.id}")

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
            return snapshot_response(request, conn, find_snapshot(conn, snapshot_id))

    @router.post("/orders/snapshots/{snapshot_id}/preview")
    async def preview_restore(request: Request, snapshot_id: int):
        with closing(ctx.connect_tools()) as conn:
            return queue_restore_job(request, conn, find_snapshot(conn, snapshot_id),
                                     JobKind.PREVIEW)

    @router.post("/orders/snapshots/{snapshot_id}/restore")
    async def place_restore(request: Request, snapshot_id: int):
        """Queue the job that places real orders. It follows a finished preview
        of this snapshot and the typed word, both checked here."""
        errors = forms.parse_typed_word(await form_of(request), forms.RESTORE_WORD)
        with closing(ctx.connect_tools()) as conn:
            snapshot = find_snapshot(conn, snapshot_id)
            refusal = placing_refusal(tools_db.latest_restore_job(conn, snapshot_id))
            if refusal:
                return snapshot_response(request, conn, snapshot, 409, refused=refusal)
            if errors:
                return snapshot_response(request, conn, snapshot, 422,
                                         confirm_error=errors["confirm"])
            return queue_restore_job(request, conn, snapshot, JobKind.RESTORE)

    @router.get("/partials/orders/restore/{snapshot_id}", response_class=HTMLResponse)
    def restore_partial(request: Request, snapshot_id: int):
        with closing(ctx.connect_tools()) as conn:
            return render(request, "partials/orders_restore.html",
                          **restore_context(conn, find_snapshot(conn, snapshot_id)))

    @router.get("/partials/orders/{region}", response_class=HTMLResponse)
    def orders_partial(request: Request, region: str):
        template = region_template(ORDERS_REGIONS, region)
        with closing(ctx.connect_tools()) as conn:
            return render(request, template, **orders_context(conn))

    return router
