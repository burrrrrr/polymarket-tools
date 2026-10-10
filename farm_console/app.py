import mimetypes
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import reward_screener
import triage

from . import remote, security
from .context import HERE, Context
from .pages import (activity, discover, markets, orders, overview, review, settings, sniper,
                    stream)

DEFAULT_PORT = 8787

# Windows takes .js types from the registry, which often says text/plain.
mimetypes.add_type("text/javascript", ".js")


def utc_now():
    return datetime.now(timezone.utc)


def create_app(db_path, port=DEFAULT_PORT, poll_s=1.0, max_polls=None, clock=utc_now,
               scan=reward_screener.run_scan, fetch_rules=remote.fetch_rules,
               fetch_position=remote.fetch_position, fetch_info=remote.fetch_info,
               fetch_book=remote.fetch_book, fetch_total_rates=remote.fetch_total_rates,
               run_triage=triage.run_claude, fetch_page=triage.fetch_page, funder=None,
               tools_db=None, search_markets=sniper.search_markets,
               fetch_books=sniper.fetch_books):
    """The console over db_path, served on 127.0.0.1:port.

    max_polls ends /stream after that many polls, so a test can read it to the
    end; None streams until the client leaves. scan and the fetch_ functions
    reach the network, run_triage calls Claude and fetch_page reads the pages
    its news check cites; funder is the wallet address whose holdings the
    Review page shows. tools_db is the path of the tools.db the console shares
    with the worker; with None the order tools say they are not configured.
    search_markets and fetch_books are the Sniper page's reads of Gamma and
    the CLOB.
    """
    app = FastAPI(title="Farm console", docs_url=None, redoc_url=None)
    app.state.token = token = security.new_token()
    app.add_middleware(security.Guard, port=port, token=token)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    ctx = Context(db_path, token, clock, poll_s=poll_s, max_polls=max_polls, scan=scan,
                  fetch_rules=fetch_rules, fetch_position=fetch_position, fetch_info=fetch_info,
                  fetch_book=fetch_book, fetch_total_rates=fetch_total_rates,
                  run_triage=run_triage, fetch_page=fetch_page, funder=funder,
                  tools_db=tools_db, search_markets=search_markets, fetch_books=fetch_books)
    app.state.triage = ctx.triager
    app.state.total_rates = ctx.total_rates
    app.include_router(overview.routes(ctx))
    app.include_router(markets.routes(ctx))
    app.include_router(activity.routes(ctx))
    app.include_router(review.routes(ctx))
    app.include_router(discover.routes(ctx))
    app.include_router(orders.routes(ctx))
    app.include_router(sniper.routes(ctx))
    app.include_router(settings.routes(ctx))
    app.include_router(stream.routes(ctx))
    return app
