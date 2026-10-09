#!/usr/bin/env python3
"""
Order Scoring Probe

Read-only. Answers how long an order must rest before Polymarket counts it
for liquidity rewards. Every few seconds it lists this key's open orders and
asks the CLOB's /orders-scoring endpoint which of them are scoring, then
reports, for every order placed while it ran, how long after it first
appeared it first scored. It places and cancels nothing, so run it beside the
live farmer: the farmer's own replacements supply the fresh orders.

Ages count from the first poll that saw the order, on this machine's clock,
so clock skew against the CLOB cannot bias them; an order is at most one poll
older than its age. An order scoring on the first poll that saw it scores
within a poll of being placed.

/orders-scoring answers only for the key's own orders, so this needs the live
key; a dry run rests nothing on the CLOB and other makers' orders cannot be
asked about. An order that never scores while resting is usually outside the
reward band or under the minimum size rather than too young; the summary
reports how long those lived so the two cases can be told apart.

Usage:
  python scoring_probe.py                   # 10 minutes, poll every 2s
  python scoring_probe.py --minutes 30 --csv scoring.csv
"""

import argparse
import csv
import time
from datetime import datetime

from py_clob_client_v2.clob_types import OrdersScoringParams

SCORING_BATCH = 50
# Age buckets, in seconds since first seen, for when a fresh order first scored.
BUCKETS = (5, 15, 30, 60, 120)


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def scoring_of(client, order_ids):
    scoring = {}
    for i in range(0, len(order_ids), SCORING_BATCH):
        scoring.update(client.are_orders_scoring(
            OrdersScoringParams(orderIds=order_ids[i:i + SCORING_BATCH])))
    return scoring


def bucket(age):
    if age == 0:
        return "first poll"
    return next((f"<{b}s" for b in BUCKETS if age < b), f">={BUCKETS[-1]}s")


def summarize(seen, poll_s):
    """Print when the orders placed during the run first scored."""
    fresh = [o for o in seen.values() if o["fresh"]]
    scored = [o for o in fresh if o["scored_at"] is not None]
    never = [o for o in fresh if o["scored_at"] is None]
    print()
    print(f"Orders placed during the run: {len(fresh)} "
          f"(of {len(seen)} seen; the rest were resting when it started)")
    counts = {}
    for o in scored:
        name = bucket(o["scored_at"] - o["first_seen"])
        counts[name] = counts.get(name, 0) + 1
    print(f"First scored, by time since first seen (polls every {poll_s:g}s):")
    for name in ["first poll"] + [f"<{b}s" for b in BUCKETS] + [f">={BUCKETS[-1]}s"]:
        if counts.get(name):
            print(f"  {name:>10}  {counts[name]}")
    if never:
        lives = sorted(o["last_seen"] - o["first_seen"] for o in never)
        print(f"Never scored while seen: {len(never)}, seen for "
              f"{lives[0]:.0f}s to {lives[-1]:.0f}s (median {lives[len(lives) // 2]:.0f}s)")


def run(args):
    from config import create_client, load_config
    from key_loader import cleanup_key_source

    client = create_client(load_config())
    seen = {}
    first_poll = True
    end = time.time() + args.minutes * 60
    log(f"Probing for {args.minutes:g} minutes, polling every {args.poll:g}s; Ctrl+C ends early")
    try:
        while time.time() < end:
            started = time.time()
            orders = {o["id"]: o for o in client.get_open_orders()}
            scoring = scoring_of(client, list(orders)) if orders else {}
            now = time.time()
            for order_id, order in orders.items():
                record = seen.setdefault(order_id, {
                    "id": order_id, "side": order["side"], "price": float(order["price"]),
                    "asset_id": order["asset_id"], "fresh": not first_poll,
                    "first_seen": now, "scored_at": None, "last_seen": now,
                })
                record["last_seen"] = now
                if scoring.get(order_id) and record["scored_at"] is None:
                    record["scored_at"] = now
                    if record["fresh"]:
                        log(f"SCORING {order['side']:4} {record['price']:.3f} {order_id[:10]} "
                            f"{now - record['first_seen']:.0f}s after first seen")
            first_poll = False
            time.sleep(max(0.0, args.poll - (time.time() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_key_source()

    summarize(seen, args.poll)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["order_id", "asset_id", "side", "price", "placed_during_run",
                             "first_seen", "scored_after_s", "seen_for_s"])
            for o in seen.values():
                writer.writerow([
                    o["id"], o["asset_id"], o["side"], o["price"], o["fresh"],
                    datetime.fromtimestamp(o["first_seen"]).isoformat(timespec="seconds"),
                    "" if o["scored_at"] is None else round(o["scored_at"] - o["first_seen"], 1),
                    round(o["last_seen"] - o["first_seen"], 1),
                ])
        log(f"Wrote {len(seen)} orders to {args.csv}")


def main():
    parser = argparse.ArgumentParser(description="Report how soon placed orders start scoring.")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--poll", type=float, default=2.0, help="seconds between polls")
    parser.add_argument("--csv", help="write one row per order seen")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
