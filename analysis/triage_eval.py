"""
Evaluate Claude's screen before trusting it (triage.auto_top_n stays 0 until
these gates pass).

  screen     the markets in --ids (one condition id per line) and the top --n
             queued candidates: screens each with no ok row of this prompt
             version, WORKERS at a time
  outcomes   screened markets now closed: each verdict against whether the
             favorite won
  grounding  stored screens: rule quotes found in the rules, news quotes
             found on their pages, news citing a prediction market
  retro      rejected markets still paying and open: which screen near
             impossible or quiet, to reconsider
  hindsight  rejected markets now closed: whether the favorite won, with an
             approximate reward missed and loss avoided

Each reuses the stored ok rows of the current triage.PROMPT_VERSION, writes a
CSV to --out and prints a summary; outcomes and grounding print PASS or FAIL
per gate.
"""

import argparse
import csv
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from typing import NamedTuple, Optional

import httpx
from dotenv import load_dotenv

import data_dir
from farm import farm_db, triage
from console import remote
from polymarket.gamma import request_markets

WORKERS = 4
NEAR = triage.Verdict.NEAR_IMPOSSIBLE.value
QUIET = triage.Verdict.QUIET.value
HUMAN = triage.Verdict.NEEDS_HUMAN.value
# The quiet gate: at most QUIET_MAX_LOST of resolved quiet markets lost by the
# favorite, judged once QUIET_MIN_RESOLVED have resolved.
QUIET_MIN_RESOLVED = 20
QUIET_MAX_LOST = 0.05
LABELS = {NEAR: "near impossible", QUIET: "quiet"}


class Report(NamedTuple):
    rows: list
    lines: list


class Resolution(NamedTuple):
    closed: bool
    winner: Optional[str]  # the outcome it resolved to; None while unresolved


def fetch_resolution(condition_id):
    """The market's Resolution. Gamma lists a closed market only when asked
    for closed ones, and then only closed ones."""
    with httpx.Client() as http:
        entries = request_markets(http, [condition_id], True, remote.TIMEOUT_S, limit=False,
                                  retries=0)
    entry = next((e for e in entries if e.get("conditionId") == condition_id), None)
    if entry is None:
        return Resolution(False, None)
    for outcome, price in zip(json.loads(entry.get("outcomes") or "[]"),
                              json.loads(entry.get("outcomePrices") or "[]")):
        if float(price) >= 0.99:
            return Resolution(True, outcome)
    return Resolution(True, None)


def gate(name, passed, detail):
    return f"{'PASS' if passed else 'FAIL'} {name}: {detail}"


def share(k, n):
    return f"{k / n:.0%}" if n else "n/a"


def current_screens(path, ids, screen_one):
    """{condition_id: (TriageRecord, screened now)}: each market's ok row of
    this prompt version, screening the markets with none WORKERS at a time
    through screen_one(conn, condition_id), each on its own connection."""
    def one(cid):
        with closing(farm_db.open_db(path)) as conn:
            found = farm_db.read_triage(conn, (cid,), triage.PROMPT_VERSION, "ok", limit=1)
            return (found[0], False) if found else (screen_one(conn, cid), True)
    with ThreadPoolExecutor(WORKERS) as pool:
        return dict(zip(ids, pool.map(one, ids)))


def in_latest_scan(conn, statuses):
    latest = farm_db.latest_scan(conn)
    return [m for m in farm_db.read_markets(conn, statuses=statuses)
            if farm_db.stamp(m.last_scanned) == latest]


def news_text(s):
    if not s.news:
        return "skipped"
    return f"{sum(n.found or bool(n.items) for n in s.news)}/{len(s.news)} found"


def screen(conn, path, ids, n, screen_one):
    wanted = list(dict.fromkeys([*ids, *farm_db.candidate_queue(conn)[:n]]))
    markets = {m.condition_id: m for m in farm_db.read_markets(conn, wanted)}
    lines = [f"Unknown condition id: {i}" for i in wanted if i not in markets]
    rows = []
    for cid, (t, fresh) in current_screens(path, [i for i in wanted if i in markets],
                                           screen_one).items():
        s = triage.from_result(t.result) if t.status == "ok" else None
        rows.append({"condition_id": cid, "question": markets[cid].question,
                     "verdict": t.verdict or "error",
                     "background": "/".join(r.answer for r in s.runs) if s else "",
                     "news": news_text(s) if s else "", "why": "; ".join(s.why) if s else "",
                     "error": t.error or "", "screened_now": fresh,
                     "duration_s": None if t.duration_s is None else round(t.duration_s),
                     "cost_usd": t.cost_usd})
    for r in rows:
        lines.append(f"{r['verdict']:15} {r['background'] or '-':45} news {r['news'] or '-':9} "
                     f"{r['question'][:70]} | {r['why'] or r['error']}")
    fresh = [r for r in rows if r["screened_now"]]
    seconds = [r["duration_s"] for r in fresh if r["duration_s"] is not None]
    lines.append(
        f"{len(rows)} markets: {sum(r['verdict'] == NEAR for r in rows)} near impossible, "
        f"{sum(r['verdict'] == QUIET for r in rows)} quiet, "
        f"{sum(r['verdict'] == HUMAN for r in rows)} needs human, "
        f"{sum(r['verdict'] == 'error' for r in rows)} errors. Screened {len(fresh)} now for "
        f"${sum(r['cost_usd'] or 0 for r in fresh):.2f}"
        + (f", median {statistics.median(seconds):.0f}s each." if seconds else "."))
    return Report(rows, lines)


def latest_screens(conn):
    latest = {}
    for t in farm_db.read_triage(conn, prompt_version=triage.PROMPT_VERSION, status="ok"):
        latest.setdefault(t.condition_id, t)
    return latest


def outcomes(conn, resolve=fetch_resolution):
    rows = []
    for cid, t in latest_screens(conn).items():
        winner = resolve(cid).winner
        if winner is None:
            continue
        m = farm_db.read_markets(conn, (cid,))[0]
        fav = m.metrics.get("market", {}).get("fav_outcome")
        rows.append({"condition_id": cid, "question": m.question, "verdict": t.verdict,
                     "favorite": fav, "winner": winner, "favorite_won": winner == fav,
                     "book_mid": t.book_mid})
    lines = [f"{len(rows)} screened markets have closed."]
    for v in triage.Verdict:
        won = sum(r["favorite_won"] for r in rows if r["verdict"] == v.value)
        lost = sum(not r["favorite_won"] for r in rows if r["verdict"] == v.value)
        lines.append(f"  {v.value:16} favorite won {won}, lost {lost}")
    missed = sum(r["favorite_won"] for r in rows if r["verdict"] == HUMAN)
    near = [r for r in rows if r["verdict"] == NEAR]
    lost = sum(not r["favorite_won"] for r in near)
    quiet = [r for r in rows if r["verdict"] == QUIET]
    quiet_lost = sum(not r["favorite_won"] for r in quiet)
    quiet_gate = (
        gate("quiet favorite lost", quiet_lost <= QUIET_MAX_LOST * len(quiet),
             f"{quiet_lost} of {len(quiet)} quiet markets ({share(quiet_lost, len(quiet))}, "
             f"at most {QUIET_MAX_LOST:.0%})")
        if len(quiet) >= QUIET_MIN_RESOLVED else
        f"quiet favorite lost: not enough resolved, {len(quiet)} of the {QUIET_MIN_RESOLVED} "
        "quiet markets it needs")
    lines += [f"{missed} needs-human markets resolved for the favorite: farmable markets the "
              "screen left to you.",
              gate("near impossible favorite lost", bool(near) and lost == 0,
                   f"{lost} of {len(near)} near-impossible markets"),
              quiet_gate]
    return Report(rows, lines)


CHECKS = {True: "verified", False: "unverified", None: "unreadable"}


def grounding(conn):
    rows = []
    for t in latest_screens(conn).values():
        s = triage.from_result(t.result)
        base = {"condition_id": t.condition_id, "screened_at": farm_db.stamp(t.at)}
        for p in (p for r in s.runs for p in r.yes_paths if p.rule_quote):
            rows.append({**base, "kind": "rule quote", "text": p.path, "quote": p.rule_quote,
                         "url": "", "check": "verbatim" if p.quote_ok else "not in the rules",
                         "circular": False})
        for item in (item for n in s.news for item in n.items):
            rows.append({**base, "kind": "news", "text": item.text, "quote": item.quote,
                         "url": item.url, "check": CHECKS[item.verified],
                         "circular": item.circular})
    quotes = [r for r in rows if r["kind"] == "rule quote"]
    news = [r for r in rows if r["kind"] == "news"]
    verbatim = sum(r["check"] == "verbatim" for r in quotes)
    counts = {c: sum(r["check"] == c for r in news) for c in CHECKS.values()}
    circular = sum(r["circular"] for r in news)
    lines = [f"{len(quotes)} rule quotes over {len({r['condition_id'] for r in rows})} "
             f"screens; {share(verbatim, len(quotes))} verbatim.",
             f"{len(news)} news items: "
             + ", ".join(f"{share(k, len(news))} {c}" for c, k in counts.items()) + ".",
             gate("circular sources", bool(rows) and circular == 0,
                  f"{circular} news items cite a prediction market or forecast")]
    return Report(rows, lines)


def retro(conn, path, n, screen_one, resolve=fetch_resolution):
    markets = []
    for m in in_latest_scan(conn, ("rejected",)):
        if len(markets) == n:
            break
        if m.rewards_active and not resolve(m.condition_id).closed:
            markets.append(m)
    screens = current_screens(path, [m.condition_id for m in markets], screen_one)
    rows = []
    for m in markets:
        t, _ = screens[m.condition_id]
        decided = farm_db.read_decisions(conn, (m.condition_id,), limit=1)
        reason = decided[0].reason if decided and decided[0].reason else None
        rows.append({"condition_id": m.condition_id, "question": m.question,
                     "reason": reason.label if reason else "",
                     "net_apr": m.metrics.get("net_apr"), "verdict": t.verdict or "error",
                     "reconsider": t.verdict in LABELS, "error": t.error or ""})
    reconsider = [r for r in rows if r["reconsider"]]
    lines = [f"{len(rows)} rejected markets still paying and open; "
             + ", ".join(f"{sum(r['verdict'] == v for r in rows)} screen {label}"
                         for v, label in LABELS.items()) + ":"]
    for r in reconsider:
        apr = "unknown" if r["net_apr"] is None else f"{r['net_apr']:.1%}"
        lines.append(f"  reconsider ({LABELS[r['verdict']]}) {r['question']} "
                     f"[{r['reason'] or 'no reason logged'}] net APR {apr}")
    return Report(rows, lines)


def days_listed(m):
    if m.first_seen is None or m.last_scanned is None:
        return 0.0
    return (m.last_scanned - m.first_seen).total_seconds() / 86400


def hindsight(conn, resolve=fetch_resolution):
    rows = []
    for m in farm_db.read_markets(conn, statuses=("rejected",)):
        winner = resolve(m.condition_id).winner
        if winner is None:
            continue
        fav = m.metrics.get("market", {}).get("fav_outcome")
        won = winner == fav
        rows.append({"condition_id": m.condition_id, "question": m.question,
                     "favorite": fav, "winner": winner, "favorite_won": won,
                     "approx_reward_usd": round((m.metrics.get("est_daily_usd") or 0)
                                                * days_listed(m), 2),
                     "approx_loss_usd": None if won else m.metrics.get("capital_usd")})
    won = [r for r in rows if r["favorite_won"]]
    lines = [f"{len(rows)} rejected markets have closed; the favorite won {len(won)}.",
             f"Approximate reward missed on those: "
             f"${sum(r['approx_reward_usd'] for r in won):,.2f} (est. $/day x days listed).",
             f"Approximate loss avoided on the rest: "
             f"${sum(r['approx_loss_usd'] or 0 for r in rows if not r['favorite_won']):,.2f} "
             f"(the planned capital)."]
    return Report(rows, lines)


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def read_ids(path):
    if path is None:
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m analysis.triage_eval",
                                     description=__doc__.split("\n\n")[0].strip())
    sub = parser.add_subparsers(dest="command", required=True)
    for name, n in (("screen", 0), ("outcomes", None), ("grounding", None), ("retro", 50),
                    ("hindsight", None)):
        p = sub.add_parser(name)
        p.add_argument("--db", help=f"SQLite file (default: farm.db {data_dir.LOCATION_HELP})")
        p.add_argument("--out", required=True, help="CSV path")
        if n is not None:
            p.add_argument("--n", type=int, default=n)
        if name == "screen":
            p.add_argument("--ids", help="a file of condition ids, one per line")
    return parser


def main(argv=None, run=triage.run_claude, fetch_rules=remote.fetch_rules,
         fetch_page=triage.fetch_page, resolve=fetch_resolution,
         clock=lambda: datetime.now(timezone.utc)):
    load_dotenv(data_dir.REPO / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "screen" and not args.ids and not args.n:
        parser.error("screen needs --ids, --n or both")
    try:
        args.db = data_dir.farm_db_path(args.db)
    except data_dir.NoDataDir as e:
        parser.error(str(e))
    now = clock()

    def screen_one(conn, condition_id):
        return triage.screen_and_store(conn, condition_id, now, fetch_rules, run, fetch_page)

    with closing(farm_db.open_db(args.db)) as conn:
        report = {
            "screen": lambda: screen(conn, args.db, read_ids(args.ids), args.n, screen_one),
            "outcomes": lambda: outcomes(conn, resolve),
            "grounding": lambda: grounding(conn),
            "retro": lambda: retro(conn, args.db, args.n, screen_one, resolve),
            "hindsight": lambda: hindsight(conn, resolve),
        }[args.command]()
    write_csv(args.out, report.rows)
    for line in report.lines:
        print(line)
    print(f"Wrote {len(report.rows)} rows to {args.out}.")
    return 1 if any(line.startswith("FAIL") for line in report.lines) else 0


if __name__ == "__main__":
    sys.exit(main())
