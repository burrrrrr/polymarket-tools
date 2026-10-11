import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from farm import farm_db, reward_farmer, reward_screener, triage

from .. import forms, views
from ..context import FARMER_DEFAULTS, form_of, see_other

QUOTABLE_SCAN = 40
QUOTABLE_BATCH = 8


def farmer_config(conn):
    """The FarmConfig the farmer runs with now, as Context.effective reads each field."""
    overrides, _ = farm_db.load_settings(conn)
    status = farm_db.read_farmer_status(conn)
    return reward_farmer.FarmConfig(**{
        key: views.setting_value(key, status, overrides, FARMER_DEFAULTS)[0]
        for key in reward_farmer.FarmConfig._fields})


def parse_mode(mode):
    if mode and mode not in views.MODES:
        raise HTTPException(422, f"no mode {mode!r}")
    return mode or None


def queue_url(mode):
    return "/review" + (f"?mode={mode}" if mode else "")


def next_review(conn, ids, condition_id, mode):
    """Where to go once this market is decided or muted: next to it in the
    queue as it stood (ids), passing over markets that have left it since."""
    kept = set(farm_db.candidate_queue(conn, mode))
    following = views.after_decision([i for i in ids if i in kept or i == condition_id],
                                     condition_id)
    return views.review_url(following, mode) if following else queue_url(mode)


def mute_target(market, form):
    """(MuteKind, value, label) of the event or tag a mute button sent, which
    must be the market's own."""
    m = market.metrics.get("market", {})
    if form.get("event") and form["event"] == m.get("event_slug"):
        return farm_db.MuteKind.EVENT, form["event"], m.get("event_title")
    if form.get("tag") and form["tag"] in m.get("tags", ()):
        return farm_db.MuteKind.TAG, form["tag"], form["tag"]
    raise HTTPException(400, "mute an event or tag of this market")


def routes(ctx):
    router = APIRouter()

    def review_context(conn, market, now, values=None, errors=None, mode=None, skipped=0):
        status = farm_db.read_farmer_status(conn)
        ev_margin = ctx.effective(conn, "ev_margin")
        values = values or {
            "p_true": views.number_text(market.p_true * 100) if market.p_true is not None else "",
            "naked_budget": views.number_text(market.naked_budget_usd
                                              if market.naked_budget_usd is not None
                                              else reward_screener.DEFAULT_NAKED_BUDGET),
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
            "cutoff_days": (cutoff - now).total_seconds() / 86400 if cutoff else None,
            "ev": views.ev_check(market.metrics, forms.parse_p_true(values["p_true"]),
                                 values["naked_budget"], ev_margin,
                                 views.years_left(market, now)),
            "p_true_start": views.p_true_start(market.metrics),
            "ev_margin": ev_margin, "farmer_capital": ctx.effective(conn, "capital"),
            "values": values, "errors": errors or {},
        }

    def review_page(request, conn, condition_id, status_code=200, **kw):
        now = ctx.clock()
        market = ctx.find(conn, condition_id)
        return ctx.templates.TemplateResponse(request, "review.html", {
            "chrome": ctx.chrome(ctx.snapshot(conn, now), now), "now": now, "page": "review",
            **review_context(conn, market, now, **kw)}, status_code=status_code)

    @router.get("/review")
    def review_first(request: Request, mode: Optional[str] = None):
        now = ctx.clock()
        mode = parse_mode(mode)
        ctx.total_rates.get()
        with closing(ctx.connect()) as conn:
            ids = farm_db.candidate_queue(conn, mode)
            if ids:
                return see_other(views.review_url(ids[0], mode))
            return ctx.render(request, "review_empty.html", now, ctx.snapshot(conn, now),
                              page="review", queue_mode=mode)

    @router.get("/review/{condition_id}", response_class=HTMLResponse)
    def review(request: Request, condition_id: str, mode: Optional[str] = None,
               skipped: int = 0):
        mode = parse_mode(mode)
        with closing(ctx.connect()) as conn:
            return review_page(request, conn, condition_id, mode=mode, skipped=skipped)

    def plan_now(row, cfg, prior, p_true=None, budget=reward_screener.DEFAULT_NAKED_BUDGET,
                 capital=None):
        """(views.live_review of a market row with these terms against the live
        book, its views.rewards_state); p_true None values it at the book midpoint."""
        terms = {"p_true": p_true, "naked_budget_usd": budget,
                 "capital_usd": capital, "paused": 0, "naked_paused": 0}
        cid = row["condition_id"]
        info, rates = ctx.infos.get(cid), ctx.total_rates.get()
        fav_token = json.loads(row["metrics"])["market"]["fav_token_id"]
        live = views.live_review({**dict(row), **terms}, info, ctx.books.get(fav_token), rates,
                                 cfg, ctx.clock(), row["status"] == "approved", prior)
        return live, views.rewards_state(info, cid, rates)

    def quotable(row, cfg, prior):
        """(whether the farmer would quote this candidate now, its rewards state).
        A market whose book cannot be read counts, so its page can show why."""
        try:
            live, active = plan_now(row, cfg, prior)
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
            with closing(ctx.connect()) as conn:
                for cid, active in changed:
                    farm_db.set_rewards_active(conn, cid, active)

    @router.get("/review/{condition_id}/next-quotable")
    def next_quotable(condition_id: str, mode: Optional[str] = None):
        mode = parse_mode(mode)
        with closing(ctx.connect()) as conn:
            ids = farm_db.candidate_queue(conn, mode, after=condition_id)
            cfg, prior = farmer_config(conn), reward_screener.load_prior(conn)
            rows = [farm_db.find_market(conn, cid) for cid in ids[:QUOTABLE_SCAN]]
        skipped, checked, found = 0, [], None
        with ThreadPoolExecutor(QUOTABLE_BATCH) as pool:
            for i in range(0, len(rows), QUOTABLE_BATCH):
                batch = rows[i:i + QUOTABLE_BATCH]
                for row, (ok, active) in zip(batch, pool.map(lambda r: quotable(r, cfg, prior),
                                                             batch)):
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

    @router.get("/partials/review/{condition_id}/ev", response_class=HTMLResponse)
    def review_ev(request: Request, condition_id: str, p_true: str = "",
                  naked_budget: str = ""):
        with closing(ctx.connect()) as conn:
            market = ctx.find(conn, condition_id)
            ev = views.ev_check(market.metrics, forms.parse_p_true(p_true), naked_budget,
                                ctx.effective(conn, "ev_margin"),
                                views.years_left(market, ctx.clock()))
        return ctx.templates.TemplateResponse(request, "partials/review_ev.html", {"ev": ev})

    @router.get("/partials/review/{condition_id}/live", response_class=HTMLResponse)
    def review_live(request: Request, condition_id: str, p_true: str = "",
                    naked_budget: str = "", capital: str = "", mode: Optional[str] = None):
        mode = parse_mode(mode)
        p, budget, cap = forms.preview_terms(
            {"p_true": p_true, "naked_budget": naked_budget, "capital": capital})
        with closing(ctx.connect()) as conn:
            row = farm_db.find_market(conn, condition_id)
            if row is None:
                raise HTTPException(404, f"no market {condition_id!r}")
            cfg, prior = farmer_config(conn), reward_screener.load_prior(conn)
        try:
            (live, active), error = plan_now(row, cfg, prior, p,
                                             0.0 if budget is None else budget, cap), None
        except Exception as e:
            live, active, error = None, None, f"{type(e).__name__}: {e}"
        record_rewards([(row, active)])
        candidate = row["status"] == "candidate"
        scan = json.loads(row["metrics"])
        return ctx.templates.TemplateResponse(request, "partials/review_live.html", {
            "live": live, "error": error, "p_true_missing": p is None,
            "scan_share": scan.get("share"), "scan_daily": scan.get("est_daily_usd"),
            "budget_missing": budget is None, "candidate": candidate,
            "left_queue": candidate and active is False,
            "skip_url": f"/review/{condition_id}/next-quotable"
                        + (f"?mode={mode}" if mode else "")})

    @router.get("/partials/review/{condition_id}/rules", response_class=HTMLResponse)
    def review_rules(request: Request, condition_id: str):
        try:
            found, error = ctx.rules.get(condition_id), None
        except Exception as e:
            found, error = None, f"{type(e).__name__}: {e}"
        return ctx.templates.TemplateResponse(request, "partials/review_rules.html", {
            "condition_id": condition_id, "rules": found, "error": error})

    def triage_panel(request, condition_id):
        """The latest screen of this prompt version, else an older row marked
        stale."""
        now = ctx.clock()
        with closing(ctx.connect()) as conn:
            market = ctx.find(conn, condition_id)
            row = (farm_db.latest_triage(conn, condition_id, triage.PROMPT_VERSION)
                   or farm_db.latest_triage(conn, condition_id))
        stale = row is not None and row.prompt_version != triage.PROMPT_VERSION
        screen = (triage.from_result(row.result)
                  if row is not None and row.status == "ok" and not stale else None)
        end = market.end_date.date() if market.end_date else None
        days_left = triage.days_until(end, now.date())
        return ctx.templates.TemplateResponse(request, "partials/review_triage.html", {
            "condition_id": condition_id, "row": row, "stale": stale, "s": screen,
            "d": screen and triage.digest(screen, days_left), "quiet_days": triage.QUIET_DAYS,
            "now": now, "running": condition_id in ctx.triager.running(),
            "waiting": condition_id in ctx.triager.pending()})

    @router.get("/partials/review/{condition_id}/triage", response_class=HTMLResponse)
    def review_triage(request: Request, condition_id: str):
        return triage_panel(request, condition_id)

    @router.post("/review/{condition_id}/triage", response_class=HTMLResponse)
    def request_triage(request: Request, condition_id: str):
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
        ctx.triager.enqueue(condition_id)
        return triage_panel(request, condition_id)

    @router.get("/partials/review/{condition_id}/holdings", response_class=HTMLResponse)
    def review_holdings(request: Request, condition_id: str):
        with closing(ctx.connect()) as conn:
            market = ctx.find(conn, condition_id)
        m = market.metrics.get("market", {})
        try:
            position = ctx.fetch_position(ctx.funder, condition_id, m.get("fav_token_id"))
            error = None
        except Exception as e:
            position, error = None, f"{type(e).__name__}: {e}"
        return ctx.templates.TemplateResponse(request, "partials/review_holdings.html", {
            "condition_id": condition_id, "market": market, "m": m, "position": position,
            "long_outcome": views.OTHER_OUTCOME.get(m.get("fav_outcome"), "longshot"),
            "error": error})

    @router.post("/review/{condition_id}/approve")
    async def approve(request: Request, condition_id: str):
        form = await form_of(request)
        decision, errors = forms.parse_decision(form)
        mode = parse_mode(form.get("queue_mode"))
        with closing(ctx.connect()) as conn:
            market = ctx.find(conn, condition_id)
            if errors:
                return review_page(request, conn, condition_id, 422,
                                   values={**form, "note": form.get("note", "")},
                                   errors=errors, mode=mode)
            ids = farm_db.candidate_queue(conn, mode)
            farm_db.set_approval(conn, condition_id, decision.p_true, decision.naked_budget,
                                 decision.capital, decision.start_paused, now=ctx.clock())
            if market.status == "candidate" and not decision.start_paused:
                return see_other(next_review(conn, ids, condition_id, mode))
        return see_other(f"/markets/{condition_id}")

    @router.post("/review/{condition_id}/reject")
    async def reject(request: Request, condition_id: str):
        form = await form_of(request)
        rejection, errors = forms.parse_rejection(form)
        mode = parse_mode(form.get("queue_mode"))
        with closing(ctx.connect()) as conn:
            market = ctx.find(conn, condition_id)
            if errors:
                return review_page(request, conn, condition_id, 422,
                                   values={**form, "p_true": form.get("p_true", ""),
                                           "naked_budget": form.get("naked_budget", ""),
                                           "capital": form.get("capital", "")},
                                   errors=errors, mode=mode)
            ids = farm_db.candidate_queue(conn, mode)
            now = ctx.clock()
            farm_db.reject_market(conn, condition_id, rejection.note, rejection.reason, now)
            m = market.metrics.get("market", {})
            if rejection.mute_event and m.get("event_slug"):
                farm_db.mute(conn, farm_db.MuteKind.EVENT, m["event_slug"], m.get("event_title"),
                             now)
            return see_other(next_review(conn, ids, condition_id, mode))

    @router.post("/review/{condition_id}/unreject")
    async def unreject(request: Request, condition_id: str):
        mode = parse_mode((await form_of(request)).get("queue_mode"))
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
            farm_db.unreject_market(conn, condition_id, ctx.clock())
        return see_other(views.review_url(condition_id, mode))

    @router.post("/review/{condition_id}/mute")
    async def mute(request: Request, condition_id: str):
        form = await form_of(request)
        mode = parse_mode(form.get("queue_mode"))
        with closing(ctx.connect()) as conn:
            target = mute_target(ctx.find(conn, condition_id), form)
            ids = farm_db.candidate_queue(conn, mode)
            farm_db.mute(conn, *target, ctx.clock())
            return see_other(next_review(conn, ids, condition_id, mode))

    @router.post("/review/{condition_id}/retire")
    async def retire_reviewed(request: Request, condition_id: str):
        with closing(ctx.connect()) as conn:
            ctx.find(conn, condition_id)
            farm_db.retire_market(conn, condition_id, "retired from the console")
        return see_other(f"/markets/{condition_id}")

    return router
