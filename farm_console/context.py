from contextlib import closing
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import farm_db
import reward_farmer
from farm_db import EventKind
from tools import db as tools_db

from . import glossary, remote, security, views
from .scan import ScanRunner
from .triage_runner import TriageRunner

HERE = Path(__file__).parent
FARMER_DEFAULTS = reward_farmer.config_from(reward_farmer.build_parser().parse_args([]))._asdict()
CONTROLS_EVENT = "controls"
LIVE_POLL_S = 5


class Context:
    """One console's database, clock, templates, caches and background runners.

    create_app builds one and hands it to each page's routes(ctx). token is the
    console token the templates put in every form. The other arguments are
    create_app's own; tools_db is the path of tools.db, or None.
    """

    def __init__(self, db_path, token, clock, *, poll_s, max_polls, scan, fetch_rules,
                 fetch_position, fetch_info, fetch_book, fetch_total_rates, run_triage,
                 fetch_page, funder, tools_db, search_markets, fetch_books):
        self.db_path = db_path
        self.tools_db = tools_db
        self.clock = clock
        self.poll_s = poll_s
        self.max_polls = max_polls
        self.fetch_position = fetch_position
        self.search_markets = search_markets
        self.fetch_books = fetch_books
        self.funder = funder
        self.templates = Jinja2Templates(directory=HERE / "templates")
        versions = {f.name: int(f.stat().st_mtime) for f in (HERE / "static").iterdir()}
        for name in ("cents", "usd", "shares", "score", "clock", "stamp_local", "span",
                     "number_text", "human_volume"):
            self.templates.env.filters[name] = getattr(views, name)
        self.templates.env.globals.update(
            PHASE_TONE=views.PHASE_TONE, SLOT_TONE=views.SLOT_TONE, JOB_TONE=views.JOB_TONE,
            token=token, tools_db=tools_db,
            TOKEN_FIELD=security.TOKEN_FIELD, TOKEN_HEADER=security.TOKEN_HEADER,
            CONTROLS_EVENT=CONTROLS_EVENT, tip=glossary.tip,
            polymarket_url=views.polymarket_url, LIVE_POLL_S=LIVE_POLL_S,
            review_url=views.review_url, MODES=views.MODES, glossary=glossary.GLOSSARY,
            REJECT_REASONS=list(farm_db.RejectReason),
            static=lambda name: f"/static/{name}?v={versions[name]}")
        self.rules = remote.TtlCache(fetch_rules, remote.RULES_TTL, clock)
        self.triager = TriageRunner(db_path, clock, self.rules.get, run_triage, fetch_page)
        self.scanner = ScanRunner(db_path, clock, scan, after_store=lambda conn: (
            self.triager.enqueue_top(conn, farm_db.load_triage_auto(conn))))
        self.infos = remote.TtlCache(fetch_info, remote.INFO_TTL, clock)
        self.books = remote.TtlCache(fetch_book, remote.BOOK_TTL, clock)
        self.total_rates = remote.TotalRates(
            fetch_total_rates, remote.TOTAL_RATES_TTL, clock, on_rates=self._record_paying)

    def connect(self):
        return farm_db.open_db(self.db_path)

    def connect_tools(self):
        """A connection to tools.db; 404 for a console started without one."""
        if self.tools_db is None:
            raise HTTPException(404, views.NO_WORKER.message)
        return tools_db.open_db(self.tools_db)

    def worker(self, now):
        if self.tools_db is None:
            return views.NO_WORKER
        with closing(self.connect_tools()) as conn:
            return views.worker_liveness(tools_db.read_worker_status(conn), now)

    def chrome(self, snap, now):
        return views.chrome(snap, now, self.worker(now))

    def _record_paying(self, rates):
        with closing(self.connect()) as conn:
            farm_db.set_candidates_rewards_active(
                conn, {cid for cid, rate in rates.items() if rate > 0})

    def snapshot(self, conn, now):
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

    def render(self, request, template, now, snap, status_code=200, **context):
        return self.templates.TemplateResponse(request, template, {
            "chrome": self.chrome(snap, now), "now": now, **context}, status_code=status_code)

    def render_tool_page(self, request, template, status_code=200, *, page, **context):
        """A page of the worker's tools, at the clock's now and over a fresh
        snapshot: its banner is the worker's, not the farmer's."""
        now = self.clock()
        with closing(self.connect()) as conn:
            snap = self.snapshot(conn, now)
        return self.render(request, template, now, snap, status_code, page=page,
                           process="worker", **context)

    def find(self, conn, condition_id):
        found = farm_db.read_markets(conn, (condition_id,))
        if not found:
            raise HTTPException(404, f"no market {condition_id} in {self.db_path}")
        return found[0]

    def effective(self, conn, key):
        overrides, _ = farm_db.load_settings(conn)
        status = farm_db.read_farmer_status(conn)
        return views.setting_value(key, status, overrides, FARMER_DEFAULTS)[0]


async def form_of(request):
    return security.form_fields(await request.body())


def see_other(url):
    return RedirectResponse(url, status_code=303)


def region_template(regions, region):
    template = regions.get(region)
    if template is None:
        raise HTTPException(404, f"no region {region!r}")
    return template


def as_json(vm):
    return JSONResponse(jsonable_encoder(vm))
