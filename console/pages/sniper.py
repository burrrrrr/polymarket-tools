from contextlib import closing
from functools import partial
from typing import NamedTuple, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from polymarket import books, gamma
from polymarket.orders import TICKS, Side
from tools import db as tools_db
from tools import picker
from tools.db import WatchState

from .. import forms
from ..context import form_of, see_other

TIMEOUT_S = 15
WATCH_LIMIT = 50
WATCH_TONE = {WatchState.ARMED: "t-c", WatchState.FIRING: "t-c", WatchState.FIRED: "t-q",
              WatchState.REHEARSED: "t-q", WatchState.REJECTED: "t-n", WatchState.MISSED: "t-n",
              WatchState.FAILED: "t-r", WatchState.DISARMED: "t-n"}
FORM_DEFAULTS = {"side": "BUY", "price": "0.999", "size": "", "target_tick":
                 tools_db.TARGET_TICK_DEFAULT, "guard": "1",
                 "guard_price": f"{tools_db.GUARD_DEFAULT.price:g}",
                 "guard_tolerance": f"{tools_db.GUARD_DEFAULT.tolerance:g}",
                 "guard_max_age_s": f"{tools_db.GUARD_DEFAULT.max_age_s:g}",
                 "expires_after_s": "0", "dry_run": ""}


def search_markets(query):
    with httpx.Client() as http:
        return gamma.search(http, query, timeout=TIMEOUT_S)


def fetch_books(token_ids):
    with httpx.Client() as http:
        return books.fetch_books(http, token_ids, timeout=TIMEOUT_S)


class WatchView(NamedTuple):
    row: tools_db.WatchRow
    tone: str
    sentence: str
    age: Optional[float]
    log: list


def watch_view(row, keyed, log, now):
    age = None
    if row.seen_at is not None:
        age = ((row.triggered_at or now) - row.seen_at).total_seconds()
    return WatchView(row, WATCH_TONE[row.state], tools_db.describe(row, keyed, now), age, log)


def routes(ctx):
    router = APIRouter()
    render = partial(ctx.render_tool_page, page="sniper", ticks=TICKS,
                     confirm_word=forms.ARM_WORD)

    def watches_context(conn):
        now = ctx.clock()
        status = tools_db.read_worker_status(conn)
        keyed = status.keyed if tools_db.is_running(status, now) else None
        return {"watches": [watch_view(row, keyed, tools_db.read_watch_log(conn, row.id), now)
                            for row in tools_db.watches(conn, limit=WATCH_LIMIT)]}

    def search_context(query):
        """The ranked outcomes of a search, with the live book of each, judged
        for a BUY under the default guard. The arm form can change both."""
        if not query:
            return {"query": "", "results": None}
        try:
            markets = ctx.search_markets(query)
        except Exception as e:
            return {"query": query, "results": None, "search_error": f"{type(e).__name__}: {e}"}
        found = picker.candidates(query, markets)
        book_of, books_error = {}, None
        if found:
            try:
                book_of = ctx.fetch_books([c.token_id for c in found])
            except Exception as e:
                books_error = f"{type(e).__name__}: {e}"
        ranked = picker.rank(found, book_of, Side.BUY, tools_db.GUARD_DEFAULT,
                             tools_db.TARGET_TICK_DEFAULT)
        return {"query": query, "results": ranked, "books_error": books_error}

    def watches_response(request, status_code=200, **context):
        with closing(ctx.connect_tools()) as conn:
            return render(request, "partials/sniper_watches.html", status_code,
                          **watches_context(conn), **context)

    def pick_of(params):
        pick = {name: (params.get(name) or "").strip() for name in ("token_id", "market", "label")}
        if not all(pick.values()):
            raise HTTPException(422, "an arm form needs a token id, a market and a label")
        return pick

    def book_of(token_id):
        try:
            return ctx.fetch_books([token_id]).get(token_id), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def arm_response(request, pick, values, status_code=200, **context):
        book, books_error = book_of(pick["token_id"])
        bid, ask = (books.best_bid(book), books.best_ask(book)) if book else (None, None)
        return render(request, "sniper_arm.html", status_code, pick=pick, values=values, bid=bid,
                      ask=ask, tick=book.tick if book else None, books_error=books_error,
                      **context)

    @router.get("/sniper", response_class=HTMLResponse)
    def sniper_page(request: Request):
        query = (request.query_params.get("q") or "").strip()
        if ctx.tools_db is None:
            return render(request, "sniper.html", query=query, results=None)
        with closing(ctx.connect_tools()) as conn:
            return render(request, "sniper.html", **watches_context(conn),
                          **search_context(query))

    @router.get("/sniper/arm", response_class=HTMLResponse)
    def arm_form(request: Request):
        ctx.connect_tools().close()
        return arm_response(request, pick_of(request.query_params), dict(FORM_DEFAULTS))

    @router.post("/sniper/arm")
    async def arm(request: Request):
        """Arm a watch. A live one makes the worker place a real order later,
        so it needs the typed word; a dry run needs none."""
        form = await form_of(request)
        with closing(ctx.connect_tools()) as conn:
            pick = pick_of(form)
            values = {name: form.get(name) or "" for name in FORM_DEFAULTS}
            parsed, errors = tools_db.parse_watch(form)
            if parsed is not None and not parsed.dry_run:
                errors.update(forms.parse_typed_word(form, forms.ARM_WORD))
            if errors:
                return arm_response(request, pick, values, 422, errors=errors)
            watch_id = tools_db.arm_watch(conn, parsed, ctx.clock())
            if watch_id is None:
                return arm_response(request, pick, values, 409,
                                    refused="This token already has an open watch.")
        return see_other(f"/sniper#watch-{watch_id}")

    @router.post("/sniper/{watch_id}/disarm", response_class=HTMLResponse)
    async def disarm(request: Request, watch_id: int):
        """Ask the worker to disarm the watch, and show the watches again. The
        worker answers with `disarmed`, or with the order it had already sent."""
        with closing(ctx.connect_tools()) as conn:
            if tools_db.read_watch(conn, watch_id) is None:
                raise HTTPException(404, f"no watch {watch_id} in {ctx.tools_db}")
            tools_db.request_disarm(conn, watch_id, ctx.clock())
        return watches_response(request)

    @router.get("/partials/sniper/watches", response_class=HTMLResponse)
    def watches_partial(request: Request):
        return watches_response(request)

    return router
