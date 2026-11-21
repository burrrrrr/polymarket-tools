#!/usr/bin/env python3
"""
Trades Dump

Read-only. Shows how the CLOB lists this account's trades, beside the fills
polymarket/trades.py reads from them, so the layout that module assumes can
be checked against real trades. The live farmer tells its own fills from
yours by those fills, and Polymarket's docs show only a trade the account
took, not one a resting order made.

It asks for the trades matched in the last --hours twice, with the account as
maker_address and with no address, and prints:

  - how many trades each listing holds, by trader_side and status, and how
    many are in one listing only;
  - how many read as fills, and each trade that does not, with the reason;
  - how many of the maker fills are on an order farm.db names as the
    farmer's, in farmer_orders or in a FILL or CANCEL event;
  - the first --show MAKER trades and the first --show TAKER trades as the
    CLOB sent them, each followed by the fills read from it.

The printed trades are safe to paste: every address is replaced by ME or
OTHER, `owner` (an API key) and `transaction_hash` are removed, and order ids
are cut to ten characters. It places and cancels nothing, opens farm.db
read-only, and writes no file.

The layout holds when no trade is unreadable, every MAKER trade shows at
least one maker_orders entry with maker_address ME, the fill under a trade
you recognise has that order's side and price and its size in shares, and,
if the farmer filled in those hours, some maker fills are on the farmer's
orders.

Usage:
  python -m analysis.trades_dump                     # 72 hours, 3 of each
  python -m analysis.trades_dump --hours 24 --show 5
"""

import argparse
import getpass
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv
from py_clob_client_v2.clob_types import TradeParams

import data_dir
from farm import farm_db
from polymarket import client as polymarket_client
from polymarket import keys
from polymarket.trades import MAKER, TAKER, UnreadableTrade, parse_trades

ADDRESS = re.compile(r"0x[0-9a-f]{40}", re.IGNORECASE)
REMOVED = ("owner", "transaction_hash")
ORDER_IDS = ("order_id", "taker_order_id")
ID_SHOWN = 10


def short(order_id):
    return order_id if len(order_id) <= ID_SHOWN else order_id[:ID_SHOWN] + "..."


def redacted(value, account, key=None):
    """The trade entry with what names the account or its counterparties
    taken out."""
    if isinstance(value, dict):
        return {k: redacted(v, account, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v, account) for v in value]
    if not isinstance(value, str):
        return value
    if key in REMOVED:
        return "REMOVED"
    if ADDRESS.fullmatch(value):
        return "ME" if value.casefold() == account.casefold() else "OTHER"
    return short(value) if key in ORDER_IDS else value


def fills_of(entry, account):
    """(the Fills read from one trade, why it could not be read)."""
    try:
        return parse_trades([entry], account), None
    except UnreadableTrade as e:
        return [], str(e)


def field(entry, key):
    return entry.get(key) if isinstance(entry, dict) else None


def counts(entries):
    by = Counter((str(field(e, "trader_side")), str(field(e, "status"))) for e in entries)
    return ", ".join(f"{side}/{status} {n}" for (side, status), n in sorted(by.items())) or "none"


def farmer_ids(explicit):
    """The order ids farm.db names as the farmer's, or None when it cannot
    be read. explicit is the --db path, or None for the data directory's
    file. Opens the file read-only."""
    try:
        path = Path(data_dir.farm_db_path(explicit)).absolute()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return farm_db.farmer_ids_named(conn)
    except (data_dir.NoDataDir, sqlite3.Error):
        return None


def a_farmed_market(explicit):
    """One condition id farm.db approves, or None. Opens the file read-only."""
    try:
        path = Path(data_dir.farm_db_path(explicit)).absolute()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return min(farm_db.condition_ids(conn, "approved"), default=None)
    except (data_dir.NoDataDir, sqlite3.Error):
        return None


def probe(client, account, market):
    """One line for each way of asking the trades listing, first page only,
    with the open-orders listing as a control that the key works."""
    signer = client.get_address()

    def masked(text):
        for address, name in ((account, "ME"), (signer, "SIGNER")):
            text = re.sub(re.escape(address), name, text, flags=re.IGNORECASE)
        return text

    asks = [("trades, no parameters", TradeParams()),
            ("trades, maker_address = funder", TradeParams(maker_address=account)),
            ("trades, maker_address = funder in lowercase",
             TradeParams(maker_address=account.lower())),
            ("trades, maker_address = signer", TradeParams(maker_address=signer))]
    if market:
        asks.append(("trades, market = a farmed market", TradeParams(market=market)))
    lines = []
    try:
        lines.append(f"open orders: ok, {len(client.get_open_orders())} orders")
    except Exception as e:
        lines.append(f"open orders: failed ({masked(str(e))})")
    for name, params in asks:
        try:
            lines.append(f"{name}: ok, {len(client.get_trades(params, only_first_page=True))} "
                         f"trades")
        except Exception as e:
            lines.append(f"{name}: failed ({masked(str(e))})")
    return lines


def fill_line(fill, farmer):
    whose = ""
    if farmer is not None and not fill.taker:
        whose = ", the farmer's order" if fill.order_id in farmer else ", not a farmer order"
    return (f"  -> {'taker' if fill.taker else 'maker'} fill: {fill.side.value} "
            f"{fill.shares:g} shares @ {fill.price:g}, token {short(fill.token_id)}, "
            f"order {short(fill.order_id)}, {fill.state.value}{whose}")


def report(with_address, without, account, show, farmer=None):
    """The lines to print for the two listings. farmer is farmer_ids' answer."""
    ids = [{field(e, "id") for e in listing} for listing in (with_address, without)]
    trades = list({field(e, "id"): e for e in [*with_address, *without]}.values())
    read = [(entry, *fills_of(entry, account)) for entry in trades]
    unreadable = [(entry, why) for entry, _, why in read if why]
    made = [f for _, fills, _ in read for f in fills if not f.taker]
    own_orders = [sum(not f.taker for f in fills) for entry, fills, why in read
                  if field(entry, "trader_side") == MAKER and not why]
    lines = [
        f"With maker_address: {len(with_address)} trades ({counts(with_address)})",
        f"With no address:    {len(without)} trades ({counts(without)})",
        f"In one listing only: {len(ids[0] - ids[1])} with maker_address, "
        f"{len(ids[1] - ids[0])} with no address",
        f"Read as fills: {sum(len(fills) for _, fills, _ in read)} fills from "
        f"{len(trades) - len(unreadable)} trades; {len(unreadable)} trades unreadable",
    ]
    if own_orders:
        lines.append(f"MAKER trades read: {len(own_orders)}, each filling "
                     f"{min(own_orders)} to {max(own_orders)} of the account's orders")
    if farmer is None:
        lines.append("Maker fills on the farmer's orders: not checked, farm.db could not be read")
    else:
        lines.append(f"Maker fills on the farmer's orders: "
                     f"{sum(f.order_id in farmer for f in made)} of {len(made)} "
                     f"(farm.db names {len(farmer)} order ids)")
    lines += [f"  unreadable: {why}" for _, why in unreadable]
    for side in (MAKER, TAKER):
        shown = [row for row in read if field(row[0], "trader_side") == side][:show]
        for n, (entry, fills, why) in enumerate(shown, 1):
            lines += ["", f"{side} trade {n} of {len(shown)} shown",
                      json.dumps(redacted(entry, account), indent=1)]
            lines += [fill_line(fill, farmer) for fill in fills] or [f"  -> unreadable: {why}"]
    return lines


def main(argv=None):
    load_dotenv(data_dir.REPO / ".env")
    parser = argparse.ArgumentParser(
        prog="python -m analysis.trades_dump",
        description="Print how the CLOB lists this account's trades, beside the fills read "
                    "from them. Read-only.")
    parser.add_argument("--hours", type=float, default=72.0, help="how far back to ask")
    parser.add_argument("--show", type=int, default=3,
                        help="how many MAKER and how many TAKER trades to print")
    parser.add_argument("--probe", action="store_true",
                        help="only try each way of asking the trades listing and say which work")
    parser.add_argument("--db", help="the farmer's SQLite file, opened read-only (default: "
                                     f"farm.db {data_dir.LOCATION_HELP})")
    args = parser.parse_args(argv)
    try:
        key = keys.load_private_key(lambda: getpass.getpass("Bitwarden master password: "))
        client = polymarket_client.connect(key)
    except (keys.KeyUnavailable, polymarket_client.ConfigError) as e:
        print(f"Error: {e}")
        return 1
    account = os.environ["POLYMARKET_FUNDER_ADDRESS"]
    if args.probe:
        print("\n".join(probe(client, account, a_farmed_market(args.db))))
        return 0
    after = int(time.time() - args.hours * 3600)

    def listed(params, only_first_page=False):
        try:
            return client.get_trades(params, only_first_page=only_first_page), None
        except Exception as e:
            return None, re.sub(re.escape(account), "ME", str(e), flags=re.IGNORECASE)

    with_address, why = listed(TradeParams(maker_address=account, after=after))
    if with_address is None:
        print(f"With maker_address and after: not listed ({why})")
        with_address, unbounded = listed(TradeParams(maker_address=account), only_first_page=True)
        if with_address is None:
            print(f"Error: the trades could not be listed ({unbounded})")
            return 1
        print("With maker_address and no after: listed, so the CLOB refused the time bound. "
              "Showing the first page, any age.")
    without, why = listed(TradeParams(after=after))
    if without is None:
        print(f"With no address: not listed ({why})")
        without = []
    print(f"Trades matched in the last {args.hours:g} hours")
    print("\n".join(report(with_address, without, account, args.show, farmer_ids(args.db))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
