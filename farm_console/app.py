"""Routes of the farm console. Reads go through farm_db, derivation through views."""

import asyncio
import json
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import farm_db
import reward_farmer
import reward_screener
from farm_db import CommandKind, EventKind

from . import charts, forms, glossary, remote, security, views
from .scan import ScanPhase, ScanRunner

HERE = Path(__file__).parent
KEEPALIVE_S = 15
DEFAULT_PORT = 8787
# The farmer's built-in config, for settings with no override and no farmer run.
FARMER_DEFAULTS = reward_farmer.config_from(reward_farmer.build_parser().parse_args([]))._asdict()
# htmx refreshes the regions listening for this event after a market control.
CONTROLS_EVENT = "controls"
# How often an open Review page re-reads the live book and re-plans the orders.
LIVE_POLL_S = 5
# The naked budget a new review starts with, and what the quotable check assumes.
DEFAULT_NAKED_BUDGET = 10.0
# Skipping to the next quotable candidate reads at most QUOTABLE_SCAN books,
# QUOTABLE_BATCH at a time.
QUOTABLE_SCAN = 40
QUOTABLE_BATCH = 8

OVERVIEW_REGIONS = {"kpis": "partials/kpis.html", "alerts": "partials/alerts.html",
                    "tracked": "partials/tracked.html", "events": "partials/events.html"}
CHROME_REGIONS = {"status": "partials/status.html", "banner": "partials/banner.html"}
MARKET_REGIONS = {"summary": "partials/market_summary.html",
                  "chart": "partials/market_chart.html",
                  "live": "partials/market_live.html",
                  "history": "partials/market_history.html"}
ACTIVITY_TEMPLATES = {tab: f"partials/activity_{tab}.html" for tab in views.TABS}


# Windows takes .js types from the registry, which often says text/plain.
mimetypes.add_type("text/javascript", ".js")


def utc_now():
    return datetime.now(timezone.utc)


def create_app(db_path, port=DEFAULT_PORT, poll_s=1.0, max_polls=None, clock=utc_now,
               scan=reward_screener.run_scan, fetch_rules=remote.fetch_rules,
               fetch_position=remote.fetch_position, fetch_info=remote.fetch_info,
               fetch_book=remote.fetch_book, fetch_total_rates=remote.fetch_total_rates,
               funder=None):
    """The console over db_path, served on 127.0.0.1:port.

    max_polls ends /stream after that many polls, so a test can read it to the
    end; None streams until the client leaves. scan and the fetch_ functions
    reach the network; funder is the wallet address whose holdings the Review
    page shows.
    """
    app = FastAPI(title="Farm console", docs_url=None, redoc_url=None)
    app.state.token = token = security.new_token()
    app.add_middleware(security.Guard, port=port, token=token)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    for name in ("cents", "usd", "shares", "score", "clock", "stamp_local", "span",
                 "number_text"):
        templates.env.filters[name] = getattr(views, name)
    templates.env.globals.update(PHASE_TONE=views.PHASE_TONE, SLOT_TONE=views.SLOT_TONE,
                                 token=token, TOKEN_FIELD=security.TOKEN_FIELD,
                                 TOKEN_HEADER=security.TOKEN_HEADER,
                                 CONTROLS_EVENT=CONTROLS_EVENT, tip=glossary.tip,
                                 polymarket_url=views.polymarket_url, LIVE_POLL_S=LIVE_POLL_S,
                                 review_url=views.review_url, MODES=views.MODES,
                                 glossary=glossary.GLOSSARY)
    runner = ScanRunner(db_path, clock, scan)
    rules = remote.TtlCache(fetch_rules, remote.RULES_TTL, clock)
    infos = remote.TtlCache(fetch_info, remote.INFO_TTL, clock)
    books = remote.TtlCache(fetch_book, remote.BOOK_TTL, clock)
    app.state.total_rates = total_rates = remote.TotalRates(
        fetch_total_rates, remote.TOTAL_RATES_TTL, clock)

    def connect():
        return farm_db.open_db(db_path)

    def snapshot(conn, now):
        states = farm_db.read_farm_states(conn)
        markets = farm_db.read_markets(conn, states, ("approved",))
        return views.Snapshot(
            status=farm_db.read_farmer_status(conn),
            states=states,
            markets={m.condition_id: m for m in markets},
            fills=farm_db.read_events(conn, kinds=(EventKind.FILL,),
                                      since=now - timedelta(hours=24)),
            rewards=farm_db.read_rewards(conn, (now - timedelta(days=29)).strftime("%Y-%m-%d")),
            counts=farm_db.status_counts(conn),
        )

    def render(request, template, now, snap, **context):
        return templates.TemplateResponse(request, template, {
            "chrome": views.chrome(snap, now), "now": now, **context})

    # -- overview ----------------------------------------------------------

    def overview_vm(conn, now):
        snap = snapshot(conn, now)
        return snap, views.overview(snap, farm_db.read_events(conn, limit=views.FEED_EVENTS),
                                    now)

    @app.get("/", response_class=HTMLResponse)
    def overview_page(request: Request):
        now = clock()
        with closing(connect()) as conn:
            snap, vm = overview_vm(conn, now)
        return render(request, "overview.html", now, snap, vm=vm, page="overview")

    @app.get("/partials/overview/{region}", response_class=HTMLResponse)
    def overview_partial(request: Request, region: str):
        template = region_template(OVERVIEW_REGIONS, region)
        now = clock()
        with closing(connect()) as conn:
            snap, vm = overview_vm(conn, now)
        return render(request, template, now, snap, vm=vm)

    @app.get("/partials/chrome/{region}", response_class=HTMLResponse)
    def chrome_partial(request: Request, region: str):
        template = region_template(CHROME_REGIONS, region)
        now = clock()
        with closing(connect()) as conn:
            snap = snapshot(conn, now)
        return render(request, template, now, snap)

    @app.get("/api/overview")
    def overview_api():
        now = clock()
        with closing(connect()) as conn:
            snap, vm = overview_vm(conn, now)
        return as_json({"chrome": views.chrome(snap, now), "overview": vm})

    # -- live markets ------------------------------------------------------

    def markets_vm(conn, now, phase):
        snap = snapshot(conn, now)
        return snap, views.markets_page(snap, parse_phase(phase), now)

    @app.get("/markets", response_class=HTMLResponse)
    def markets_page(request: Request, phase: Optional[str] = None):
        now = clock()
        with closing(connect()) as conn:
            snap, vm = markets_vm(conn, now, phase)
        return render(request, "markets.html", now, snap, vm=vm, page="markets")

    @app.get("/partials/markets", response_class=HTMLResponse)
    def markets_partial(request: Request, phase: Optional[str] = None):
        now = clock()
        with closing(connect()) as conn:
            snap, vm = markets_vm(conn, now, phase)
        return render(request, "partials/cards.html", now, snap, vm=vm)

    @app.get("/api/markets")
    def markets_api(phase: Optional[str] = None):
        now = clock()
        with closing(connect()) as conn:
            _, vm = markets_vm(conn, now, phase)
        return as_json(vm)

    # -- market detail -----------------------------------------------------

    def market_vm(conn, now, condition_id):
        snap = snapshot(conn, now)
        found = farm_db.read_markets(conn, (condition_id,))
        if not found:
            raise HTTPException(404, f"no market {condition_id} in {db_path}")
        market = found[0]
        events = (farm_db.read_events(conn, condition_id=condition_id, limit=500)
                  + farm_db.read_events(conn, kinds=(EventKind.START,), limit=50))
        events.sort(key=lambda e: -e.id)
        mids = farm_db.read_mids(conn, condition_id, now - timedelta(hours=24))
        vm = views.market_detail(market, snap.states.get(condition_id), snap.status,
                                 events, mids, now)
        return snap, vm

    def chart_of(vm, now):
        return charts.mid_chart(vm.mids, vm.v, vm.markers, now)

    @app.get("/markets/{condition_id}", response_class=HTMLResponse)
    def market_page(request: Request, condition_id: str):
        now = clock()
        with closing(connect()) as conn:
            snap, vm = market_vm(conn, now, condition_id)
        return render(request, "market.html", now, snap, vm=vm, chart=chart_of(vm, now),
                      page="markets")

    @app.get("/partials/markets/{condition_id}/{region}", response_class=HTMLResponse)
    def market_partial(request: Request, condition_id: str, region: str):
        template = region_template(MARKET_REGIONS, region)
        now = clock()
        with closing(connect()) as conn:
            snap, vm = market_vm(conn, now, condition_id)
        return render(request, template, now, snap, vm=vm, chart=chart_of(vm, now))

    @app.get("/api/markets/{condition_id}")
    def market_api(condition_id: str):
        now = clock()
        with closing(connect()) as conn:
            _, vm = market_vm(conn, now, condition_id)
        return as_json(vm)

    # -- activity ----------------------------------------------------------

    def activity_vm(conn, now, tab, kind, condition_id, before):
        snap = snapshot(conn, now)
        if tab == "rewards":
            vm = views.rewards_tab(snap, now)
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

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, tab: str = "rewards", kind: Optional[str] = None,
                      condition_id: Optional[str] = None, before: Optional[int] = None):
        parse_activity(tab, kind)
        now = clock()
        with closing(connect()) as conn:
            snap, vm = activity_vm(conn, now, tab, kind, condition_id, before)
        return render(request, "activity.html", now, snap, page="activity",
                      before=before, **activity_context(snap, vm, tab, now))

    @app.get("/partials/activity", response_class=HTMLResponse)
    def activity_partial(request: Request, tab: str = "rewards", kind: Optional[str] = None,
                         condition_id: Optional[str] = None, before: Optional[int] = None):
        parse_activity(tab, kind)
        now = clock()
        with closing(connect()) as conn:
            snap, vm = activity_vm(conn, now, tab, kind, condition_id, before)
        return render(request, ACTIVITY_TEMPLATES[tab], now, snap, before=before,
                      **activity_context(snap, vm, tab, now))

    @app.get("/api/activity")
    def activity_api(tab: str = "rewards", kind: Optional[str] = None,
                     condition_id: Optional[str] = None, before: Optional[int] = None):
        parse_activity(tab, kind)
        now = clock()
        with closing(connect()) as conn:
            _, vm = activity_vm(conn, now, tab, kind, condition_id, before)
        return as_json({"tab": tab, "activity": vm})

    @app.get("/api/events")
    def events_api(after: Optional[int] = None, before: Optional[int] = None,
                   limit: int = 100, condition_id: Optional[str] = None):
        with closing(connect()) as conn:
            events = farm_db.read_events(conn, condition_id=condition_id, after_id=after,
                                         before_id=before, limit=max(1, min(limit, 1000)))
        return as_json([views.event_view(e) for e in events])

    # -- shared by the write paths -----------------------------------------

    def find(conn, condition_id):
        found = farm_db.read_markets(conn, (condition_id,))
        if not found:
            raise HTTPException(404, f"no market {condition_id} in {db_path}")
        return found[0]

    def effective(conn, key):
        overrides, _ = farm_db.load_settings(conn)
        status = farm_db.read_farmer_status(conn)
        return views.setting_value(key, status, overrides, FARMER_DEFAULTS)[0]

    def farmer_config(conn):
        """The FarmConfig the farmer runs with now, as effective() reads each field."""
        overrides, _ = farm_db.load_settings(conn)
        status = farm_db.read_farmer_status(conn)
        return reward_farmer.FarmConfig(**{
            key: views.setting_value(key, status, overrides, FARMER_DEFAULTS)[0]
            for key in reward_farmer.FarmConfig._fields})

    async def form_of(request):
        return security.form_fields(await request.body())

    def see_other(url):
        return RedirectResponse(url, status_code=303)

    def changed():
        """Empty reply to an htmx control; the regions showing it re-render."""
        return Response(headers={"HX-Trigger": CONTROLS_EVENT})

    # -- market controls ---------------------------------------------------

    def level(form):
        value = form.get("value")
        if value not in ("0", "1"):
            raise HTTPException(400, "value must be 0 or 1")
        return value == "1"

    @app.post("/markets/{condition_id}/paused")
    async def set_paused(request: Request, condition_id: str):
        paused = level(await form_of(request))
        with closing(connect()) as conn:
            find(conn, condition_id)
            farm_db.set_paused(conn, condition_id, paused)
        return changed()

    @app.post("/markets/{condition_id}/naked-paused")
    async def set_naked_paused(request: Request, condition_id: str):
        paused = level(await form_of(request))
        with closing(connect()) as conn:
            find(conn, condition_id)
            farm_db.set_naked_paused(conn, condition_id, paused)
        return changed()

    @app.post("/markets/{condition_id}/retire")
    async def retire(request: Request, condition_id: str):
        with closing(connect()) as conn:
            find(conn, condition_id)
            farm_db.retire_market(conn, condition_id, "retired from the console")
        return changed()

    # -- review ------------------------------------------------------------

    def review_context(conn, market, now, values=None, errors=None, mode=None, skipped=0):
        status = farm_db.read_farmer_status(conn)
        ev_margin = effective(conn, "ev_margin")
        values = values or {
            "p_true": views.number_text(market.p_true * 100) if market.p_true is not None else "",
            "naked_budget": views.number_text(market.naked_budget_usd
                                              if market.naked_budget_usd is not None
                                              else DEFAULT_NAKED_BUDGET),
            "capital": views.number_text(market.capital_usd)
            if market.capital_usd is not None else "",
            "note": "",
        }
        cutoff = views.expiry_cutoff(market, status)
        return {
            "market": market, "m": market.metrics.get("market", {}),
            "plan": market.metrics.get("plan"),
            "queue": views.queue(farm_db.candidate_queue(conn, mode), market.condition_id),
            "queue_mode": mode, "skipped": skipped,
            "stats": views.review_stats(market.metrics),
            "cutoff_days": (cutoff - now).total_seconds() / 86400 if cutoff else None,
            "ev": views.ev_check(market.metrics, forms.parse_p_true(values["p_true"]),
                                 values["naked_budget"], ev_margin),
            "p_true_start": views.p_true_start(market.metrics),
            "ev_margin": ev_margin, "farmer_capital": effective(conn, "capital"),
            "values": values, "errors": errors or {},
        }

    def review_page(request, conn, condition_id, status_code=200, **kw):
        now = clock()
        market = find(conn, condition_id)
        return templates.TemplateResponse(request, "review.html", {
            "chrome": views.chrome(snapshot(conn, now), now), "now": now, "page": "review",
            **review_context(conn, market, now, **kw)}, status_code=status_code)

    def parse_mode(mode):
        if mode and mode not in views.MODES:
            raise HTTPException(422, f"no mode {mode!r}")
        return mode or None

    def queue_url(mode):
        return "/review" + (f"?mode={mode}" if mode else "")

    @app.get("/review")
    def review_first(request: Request, mode: Optional[str] = None):
        now = clock()
        mode = parse_mode(mode)
        with closing(connect()) as conn:
            ids = farm_db.candidate_queue(conn, mode)
            if ids:
                return see_other(views.review_url(ids[0], mode))
            return render(request, "review_empty.html", now, snapshot(conn, now),
                          page="review", queue_mode=mode)

    @app.get("/review/{condition_id}", response_class=HTMLResponse)
    def review(request: Request, condition_id: str, mode: Optional[str] = None,
               skipped: int = 0):
        mode = parse_mode(mode)
        with closing(connect()) as conn:
            return review_page(request, conn, condition_id, mode=mode, skipped=skipped)

    def plan_now(row, cfg, p_true=None, budget=DEFAULT_NAKED_BUDGET, capital=None):
        """(views.live_review of a market row with these terms against the live
        book, its views.rewards_state); p_true None plans without the EV cap."""
        terms = {"p_true": 1.0 if p_true is None else p_true, "naked_budget_usd": budget,
                 "capital_usd": capital, "paused": 0, "naked_paused": 0}
        cid = row["condition_id"]
        info, rates = infos.get(cid), total_rates.get()
        fav_token = json.loads(row["metrics"])["market"]["fav_token_id"]
        live = views.live_review({**dict(row), **terms}, info, books.get(fav_token), rates, cfg,
                                 clock(), row["status"] == "approved")
        return live, views.rewards_state(info, cid, rates)

    def quotable(row, cfg):
        """(whether the farmer would quote this candidate now, its rewards state).
        A market whose book cannot be read counts, so its page can show why."""
        try:
            live, active = plan_now(row, cfg)
        except Exception:
            return True, None
        return bool(live.orders), active

    def record_rewards(checked):
        """Store the rewards state read for each (row, state) in checked, so a
        candidate whose rewards ended leaves the queue and one whose rewards came
        back returns. None is unknown. The farmer owns the markets it manages."""
        changed = [(row["condition_id"], active) for row, active in checked
                   if row["status"] == "candidate" and active is not None
                   and active != bool(row["rewards_active"])]
        if changed:
            with closing(connect()) as conn:
                for cid, active in changed:
                    farm_db.set_rewards_active(conn, cid, active)

    @app.get("/review/{condition_id}/next-quotable")
    def next_quotable(condition_id: str, mode: Optional[str] = None):
        mode = parse_mode(mode)
        with closing(connect()) as conn:
            ids = farm_db.candidate_queue(conn, mode, after=condition_id)
            cfg = farmer_config(conn)
            rows = [farm_db.find_market(conn, cid) for cid in ids[:QUOTABLE_SCAN]]
        skipped, checked, found = 0, [], None
        with ThreadPoolExecutor(QUOTABLE_BATCH) as pool:
            for i in range(0, len(rows), QUOTABLE_BATCH):
                batch = rows[i:i + QUOTABLE_BATCH]
                for row, (ok, active) in zip(batch, pool.map(lambda r: quotable(r, cfg), batch)):
                    checked.append((row, active))
                    if ok:
                        found = row
                        break
                    skipped += 1
                if found is not None:
                    break
        record_rewards(checked)
        if found is not None:
            return see_other(views.review_url(found["condition_id"], mode, skipped))
        rest = ids[len(rows):]
        return see_other(views.review_url(rest[0], mode, skipped) if rest else queue_url(mode))

    @app.get("/partials/review/{condition_id}/ev", response_class=HTMLResponse)
    def review_ev(request: Request, condition_id: str, p_true: str = "",
                  naked_budget: str = ""):
        with closing(connect()) as conn:
            market = find(conn, condition_id)
            ev = views.ev_check(market.metrics, forms.parse_p_true(p_true), naked_budget,
                                effective(conn, "ev_margin"))
        return templates.TemplateResponse(request, "partials/review_ev.html", {"ev": ev})

    @app.get("/partials/review/{condition_id}/live", response_class=HTMLResponse)
    def review_live(request: Request, condition_id: str, p_true: str = "",
                    naked_budget: str = "", capital: str = "", mode: Optional[str] = None):
        mode = parse_mode(mode)
        p, budget, cap = forms.preview_terms(
            {"p_true": p_true, "naked_budget": naked_budget, "capital": capital})
        with closing(connect()) as conn:
            row = farm_db.find_market(conn, condition_id)
            if row is None:
                raise HTTPException(404, f"no market {condition_id!r}")
            cfg = farmer_config(conn)
        try:
            (live, active), error = plan_now(row, cfg, p, 0.0 if budget is None else budget,
                                             cap), None
        except Exception as e:
            live, active, error = None, None, f"{type(e).__name__}: {e}"
        record_rewards([(row, active)])
        candidate = row["status"] == "candidate"
        return templates.TemplateResponse(request, "partials/review_live.html", {
            "live": live, "error": error, "p_true_missing": p is None,
            "budget_missing": budget is None, "candidate": candidate,
            "left_queue": candidate and active is False,
            "skip_url": f"/review/{condition_id}/next-quotable"
                        + (f"?mode={mode}" if mode else "")})

    @app.get("/partials/review/{condition_id}/rules", response_class=HTMLResponse)
    def review_rules(request: Request, condition_id: str):
        try:
            found, error = rules.get(condition_id), None
        except Exception as e:
            found, error = None, f"{type(e).__name__}: {e}"
        return templates.TemplateResponse(request, "partials/review_rules.html", {
            "condition_id": condition_id, "rules": found, "error": error})

    @app.get("/partials/review/{condition_id}/holdings", response_class=HTMLResponse)
    def review_holdings(request: Request, condition_id: str):
        with closing(connect()) as conn:
            market = find(conn, condition_id)
        m = market.metrics.get("market", {})
        try:
            position = fetch_position(funder, condition_id, m.get("fav_token_id"))
            error = None
        except Exception as e:
            position, error = None, f"{type(e).__name__}: {e}"
        return templates.TemplateResponse(request, "partials/review_holdings.html", {
            "condition_id": condition_id, "market": market, "m": m, "position": position,
            "long_outcome": views.OTHER_OUTCOME.get(m.get("fav_outcome"), "longshot"),
            "error": error})

    def next_review(ids, condition_id, mode):
        following = views.after_decision(ids, condition_id)
        return views.review_url(following, mode) if following else queue_url(mode)

    @app.post("/review/{condition_id}/approve")
    async def approve(request: Request, condition_id: str):
        form = await form_of(request)
        decision, errors = forms.parse_decision(form)
        mode = parse_mode(form.get("queue_mode"))
        with closing(connect()) as conn:
            market = find(conn, condition_id)
            if errors:
                return review_page(request, conn, condition_id, 422,
                                   values={**form, "note": form.get("note", "")},
                                   errors=errors, mode=mode)
            ids = farm_db.candidate_queue(conn, mode)
            farm_db.set_approval(conn, condition_id, decision.p_true, decision.naked_budget,
                                 decision.capital, decision.start_paused)
        if market.status == "candidate" and not decision.start_paused:
            return see_other(next_review(ids, condition_id, mode))
        return see_other(f"/markets/{condition_id}")

    @app.post("/review/{condition_id}/reject")
    async def reject(request: Request, condition_id: str):
        form = await form_of(request)
        note, errors = forms.parse_note(form)
        mode = parse_mode(form.get("queue_mode"))
        with closing(connect()) as conn:
            find(conn, condition_id)
            if errors:
                return review_page(request, conn, condition_id, 422,
                                   values={**form, "p_true": form.get("p_true", ""),
                                           "naked_budget": form.get("naked_budget", ""),
                                           "capital": form.get("capital", "")},
                                   errors=errors, mode=mode)
            ids = farm_db.candidate_queue(conn, mode)
            farm_db.reject_market(conn, condition_id, note)
        return see_other(next_review(ids, condition_id, mode))

    @app.post("/review/{condition_id}/retire")
    async def retire_reviewed(request: Request, condition_id: str):
        with closing(connect()) as conn:
            find(conn, condition_id)
            farm_db.retire_market(conn, condition_id, "retired from the console")
        return see_other(f"/markets/{condition_id}")

    # -- discover ----------------------------------------------------------

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
        """The scan form's values: the last ones run, else the screener's defaults."""
        return {**reward_screener.ScreenConfig()._asdict(), **farm_db.load_scan_params(conn)}

    def discover_response(request, conn, filters, status_code=200, values=None, errors=None,
                          refused=None):
        now = clock()
        snap = snapshot(conn, now)
        markets, total = farm_db.search_markets(
            conn, statuses=() if filters.status == "all" else (filters.status,),
            mode=filters.mode, text=filters.q, min_days=filters.min_days,
            buffer_days=effective(conn, "expiry_buffer_days"), now=now,
            limit=views.DISCOVER_PAGE, offset=(filters.page - 1) * views.DISCOVER_PAGE)
        values = values or {k: views.number_text(v) for k, v in scan_values(conn).items()}
        return templates.TemplateResponse(request, "discover.html", {
            "chrome": views.chrome(snap, now), "now": now, "page": "discover",
            "vm": views.discover_page(filters, markets, total, snap.status, now),
            "funnel": farm_db.load_scan_funnel(conn), "job": runner.job,
            "fields": forms.SCAN_FIELDS, "values": values, "errors": errors or {},
            "refused": refused, "modes": views.MODES, "statuses": views.STATUS_FILTERS,
        }, status_code=status_code)

    @app.get("/discover", response_class=HTMLResponse)
    def discover(request: Request, mode: Optional[str] = None, q: str = "",
                 status: str = "candidate", min_days: Optional[str] = None, page: int = 1):
        filters = parse_filters(mode, q, status, min_days, page)
        with closing(connect()) as conn:
            return discover_response(request, conn, filters)

    @app.post("/discover/scan")
    async def start_scan(request: Request):
        form = await form_of(request)
        config, errors = forms.parse_scan(form)
        with closing(connect()) as conn:
            if runner.job.phase is ScanPhase.RUNNING:
                return discover_response(request, conn, views.DiscoverFilters(), 409,
                                         values=form, refused="A scan is already in progress.")
            if errors:
                return discover_response(request, conn, views.DiscoverFilters(), 422,
                                         values=form, errors=errors)
            if not runner.start(config):
                return discover_response(request, conn, views.DiscoverFilters(), 409,
                                         values=form, refused="A scan is already in progress.")
            farm_db.put_scan_params(conn, config._asdict(), clock())
        return see_other("/discover")

    @app.get("/partials/discover/scan", response_class=HTMLResponse)
    def scan_status(request: Request):
        job = runner.job
        response = templates.TemplateResponse(request, "partials/scan_status.html",
                                              {"job": job, "now": clock()})
        if job.phase is not ScanPhase.RUNNING:
            # Only a running scan polls this; reload so the table shows the result.
            response.headers["HX-Refresh"] = "true"
        return response

    # -- settings ----------------------------------------------------------

    def settings_context(conn, now, group=None, submitted=None, errors=None):
        overrides, bad_rows = farm_db.load_settings(conn)
        status = farm_db.read_farmer_status(conn)
        return {
            "groups": [(g, views.setting_rows(
                forms.SETTING_FIELDS, g, status, overrides, bad_rows, FARMER_DEFAULTS,
                submitted if g == group else {}, errors if g == group else {}))
                for g in forms.SETTING_GROUPS],
            "status": status, "live": views.liveness(status, now),
            "stop": farm_db.latest_command(conn, CommandKind.STOP), "db_path": db_path,
        }

    def settings_response(request, conn, status_code=200, notice=None, cancel_error=None,
                          **kw):
        now = clock()
        return templates.TemplateResponse(request, "settings.html", {
            "chrome": views.chrome(snapshot(conn, now), now), "now": now, "page": "settings",
            "notice": notice, "cancel_error": cancel_error,
            **settings_context(conn, now, **kw)}, status_code=status_code)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, saved: Optional[str] = None,
                 cancelled: Optional[int] = None, reset: Optional[str] = None):
        notice = None
        if saved:
            notice = f"Saved {saved}. The farmer applies it on its next cycle."
        elif reset:
            notice = f"Reset {reset} to the farmer's own value."
        elif cancelled is not None:
            notice = (f"Paused {cancelled} approved market{'s' if cancelled != 1 else ''}. "
                      f"The farmer cancels the orders on its next cycle.")
        with closing(connect()) as conn:
            return settings_response(request, conn, notice=notice)

    @app.post("/settings/group/{group}")
    async def save_settings(request: Request, group: str):
        if group not in forms.SETTING_GROUPS:
            raise HTTPException(404, f"no settings group {group!r}")
        form = await form_of(request)
        values, errors = forms.parse_settings(form, group)
        with closing(connect()) as conn:
            if errors:
                return settings_response(request, conn, 422, group=group, submitted=form,
                                         errors=errors)
            overrides, _ = farm_db.load_settings(conn)
            status = farm_db.read_farmer_status(conn)
            farm_db.put_settings(conn, {
                key: value for key, value in values.items()
                if value != views.setting_value(key, status, overrides, FARMER_DEFAULTS)[0]
            }, clock())
        return see_other(f"/settings?saved={group}")

    @app.post("/settings/reset/{key}")
    async def reset_setting(request: Request, key: str):
        if key not in farm_db.SETTINGS:
            raise HTTPException(404, f"no setting {key!r}")
        with closing(connect()) as conn:
            farm_db.delete_setting(conn, key)
        return see_other(f"/settings?reset={key}")

    @app.get("/partials/settings/farmer", response_class=HTMLResponse)
    def farmer_partial(request: Request):
        now = clock()
        with closing(connect()) as conn:
            return templates.TemplateResponse(request, "partials/settings_farmer.html", {
                "now": now, **settings_context(conn, now)})

    @app.post("/farmer/stop", response_class=HTMLResponse)
    async def stop_farmer(request: Request):
        now = clock()
        with closing(connect()) as conn:
            farm_db.send_command(conn, CommandKind.STOP, now)
            return templates.TemplateResponse(request, "partials/settings_farmer.html", {
                "now": now, **settings_context(conn, now)})

    @app.post("/farmer/cancel-all")
    async def cancel_all(request: Request):
        errors = forms.parse_cancel_all(await form_of(request))
        with closing(connect()) as conn:
            if errors:
                return settings_response(request, conn, 422, cancel_error=errors["confirm"])
            paused = farm_db.cancel_all(conn)
        return see_other(f"/settings?cancelled={paused}")

    # -- real time ---------------------------------------------------------

    @app.get("/stream")
    async def stream(request: Request):
        return StreamingResponse(signals(request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    async def signals(request):
        """Named events only, no HTML: `cycle` when the farmer finishes a cycle,
        `farm-event` when a farm_events row appears, `status` when the farmer
        starts or stops running."""
        conn = connect()
        try:
            def read():
                status = farm_db.read_farmer_status(conn)
                return (status and status.last_cycle_at, farm_db.last_event_id(conn),
                        views.liveness(status, clock()).running)

            cycle, event_id, running = read()
            yield ": connected\n\n"
            polls, quiet = 0, 0.0
            while max_polls is None or polls < max_polls:
                await asyncio.sleep(poll_s)
                polls += 1
                if await request.is_disconnected():
                    break
                new_cycle, new_event_id, new_running = read()
                out = []
                if new_cycle != cycle:
                    out.append(f"event: cycle\ndata: {farm_db.stamp(new_cycle)}\n\n")
                if new_event_id != event_id:
                    out.append(f"event: farm-event\ndata: {new_event_id}\n\n")
                if new_running != running:
                    out.append(f"event: status\ndata: {'running' if new_running else 'stopped'}"
                               f"\n\n")
                cycle, event_id, running = new_cycle, new_event_id, new_running
                quiet = 0.0 if out else quiet + poll_s
                if quiet >= KEEPALIVE_S:
                    out.append(": keepalive\n\n")
                    quiet = 0.0
                for message in out:
                    yield message
        finally:
            conn.close()

    return app


def region_template(regions, region):
    template = regions.get(region)
    if template is None:
        raise HTTPException(404, f"no region {region!r}")
    return template


def parse_phase(phase):
    if not phase:
        return None
    if phase not in (*views.PHASES, views.WAITING):
        raise HTTPException(400, f"no phase {phase!r}")
    return phase


def as_json(vm):
    return JSONResponse(jsonable_encoder(vm))
