import contextlib
import csv
import io
import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from analysis import triage_eval
from analysis.triage_eval import Resolution
from farm import farm_db, triage
from console.remote import Rules
from polymarket.hosts import GAMMA_HOST
from test_console_writes import BUDGET_BOOK, FLOOR_BOOK, V3_RESULT, assessed_row
from test_triage import background, envelope, news

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
QUOTED ={"path": "It happens", "rule_quote": "Yes if it happens", "realistic": False}
CLEAN = triage.NewsCheck((), False, (), "q")


class StubClaude:
    """run_triage answering by question: near-impossible questions get three
    agreeing runs and quiet news, the rest an unsure run among them."""

    def __init__(self, near):
        self.near = set(near)
        self.asked = []
        self._lock = threading.Lock()

    def __call__(self, step, prompt):
        question = re.search(r"Question: (.+)", prompt).group(1)
        with self._lock:
            self.asked.append((step.name, question))
        if step is triage.NEWS:
            return envelope(news())
        return envelope(background("near_impossible" if question in self.near else "unsure",
                                   paths=(QUOTED,)))

    def questions(self):
        return {q for _, q in self.asked}


def rules(condition_id):
    return Rules("Resolves Yes if it happens.", None, None)


def run(answer="near_impossible", quote_ok=True):
    return triage.BackgroundRun(answer, True, (triage.YesPath("p", "q", False, quote_ok),), "r")


class EvalTest(unittest.TestCase):
    """delta (budget, rank 30), echo (floor, 20) and foxtrot (floor, 10) in the
    latest scan; golf scanned earlier and since dropped."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "farm.db")
        self.db = farm_db.open_db(self.path)
        self.addCleanup(self.db.close)
        farm_db.store_scan(self.db, [assessed_row("0xggg", "golf", FLOOR_BOOK, NOW, 40.0)],
                           ["0xggg"], NOW - timedelta(days=1))
        farm_db.store_scan(self.db, [
            assessed_row("0xddd", "delta", BUDGET_BOOK, NOW, 30.0),
            assessed_row("0xeee", "echo", FLOOR_BOOK, NOW, 20.0),
            assessed_row("0xfff", "foxtrot", FLOOR_BOOK, NOW, 10.0),
        ], ["0xddd", "0xeee", "0xfff"], NOW)

    def store(self, cid, runs=(run(),) * 3, news=(CLEAN,) * 2, days_left=100):
        screen = triage.Screen(*triage.decide(triage.background_route(runs, days_left), news),
                               runs, news)
        farm_db.store_triage(self.db, cid, NOW, triage.PROMPT_VERSION, "sonnet",
                             result=triage.to_result(screen), book_mid=0.97)

    def fav(self, cid):
        return farm_db.read_markets(self.db, (cid,))[0].metrics["market"]["fav_outcome"]

    def main(self, *argv, run=None, resolve=None, db_flag=True, dotenv=None):
        out = os.path.join(os.path.dirname(self.path), "out.csv")
        printed = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            os.environ.pop("POLYMARKET_DATA_DIR", None)
            self.load_dotenv = stack.enter_context(mock.patch.object(
                triage_eval, "load_dotenv", side_effect=lambda path: os.environ.update(
                    dotenv or {})))
            stack.enter_context(contextlib.redirect_stdout(printed))
            code = triage_eval.main(
                [*argv, *(["--db", self.path] if db_flag else []), "--out", out], run=run,
                fetch_rules=rules, fetch_page=lambda url: "", resolve=resolve,
                clock=lambda: NOW)
        with open(out, encoding="utf-8") as f:
            return code, printed.getvalue(), list(csv.DictReader(f))


class DataDirTest(EvalTest):
    def test_the_data_directory_that_env_names_holds_the_farm_db(self):
        self.store("0xddd")
        _, printed, _ = self.main("grounding", db_flag=False,
                                  dotenv={"POLYMARKET_DATA_DIR": os.path.dirname(self.path)})
        self.assertIn("3 rule quotes over 1 screens", printed)
        self.load_dotenv.assert_called_once_with(Path(ROOT) / ".env")

    def test_with_no_flag_and_no_variable_it_opens_the_repos_farm_db(self):
        opened = []

        def refuse(path):
            opened.append(Path(path))
            raise ConnectionRefusedError

        with mock.patch.object(farm_db, "open_db", refuse), \
                self.assertRaises(ConnectionRefusedError):
            self.main("grounding", db_flag=False)
        self.assertEqual(opened, [Path(ROOT) / "farm.db"])

    def test_a_data_directory_that_does_not_exist_ends_the_run_before_a_database_is_opened(self):
        missing = os.path.join(os.path.dirname(self.path), "missing")
        said = io.StringIO()
        with mock.patch.object(farm_db, "open_db") as open_db, \
                contextlib.redirect_stderr(said), self.assertRaises(SystemExit) as stopped:
            self.main("grounding", db_flag=False, dotenv={"POLYMARKET_DATA_DIR": missing})
        self.assertEqual(stopped.exception.code, 2)
        open_db.assert_not_called()
        self.assertIn(f"POLYMARKET_DATA_DIR is not a directory: {missing}", said.getvalue())
        self.assertFalse(os.path.exists(missing))


class ScreenTest(EvalTest):
    def test_screens_the_listed_and_top_queued_markets_that_lack_a_row(self):
        ids = os.path.join(os.path.dirname(self.path), "ids.txt")
        with open(ids, "w", encoding="utf-8") as f:
            f.write("0xggg\n\n0xnope\n0xddd\n")
        self.store("0xddd")
        claude = StubClaude({"golf?"})
        code, printed, rows = self.main("screen", "--ids", ids, "--n", "2", run=claude)
        self.assertEqual(claude.questions(), {"golf?", "echo?"})
        self.assertEqual([(r["condition_id"], r["verdict"], r["screened_now"]) for r in rows],
                         [("0xggg", "near_impossible", "True"),
                          ("0xddd", "near_impossible", "False"),
                          ("0xeee", "needs_human", "True")])
        self.assertEqual((rows[0]["background"], rows[0]["news"], rows[2]["news"]),
                         ("near_impossible/near_impossible/near_impossible", "0/2 found",
                          "skipped"))
        self.assertIn("Unknown condition id: 0xnope", printed)
        self.assertIn("background 0 of 3 near impossible", printed)
        self.assertIn("news 0/2 found", printed)
        self.assertIn("3 markets: 2 near impossible, 0 quiet, 1 needs human, 0 errors. "
                      "Screened 2 now for $0.32", printed)
        self.assertEqual(code, 0)
        self.main("screen", "--ids", ids, "--n", "2", run=claude)
        self.assertEqual(len(claude.asked), 8)

    def test_the_news_column_counts_the_checks_that_found_something(self):
        ids = os.path.join(os.path.dirname(self.path), "ids.txt")
        with open(ids, "w", encoding="utf-8") as f:
            f.write("0xddd\n")
        self.store("0xddd", news=(CLEAN, CLEAN._replace(found=True)))
        _, printed, rows = self.main("screen", "--ids", ids)
        self.assertEqual(rows[0]["news"], "1/2 found")
        self.assertIn("news 1/2 found", printed)

    def test_a_quiet_screen_shows_on_its_line_and_in_the_count(self):
        ids = os.path.join(os.path.dirname(self.path), "ids.txt")
        with open(ids, "w", encoding="utf-8") as f:
            f.write("0xddd\n")
        self.store("0xddd", runs=(run("possible"),) * 3, days_left=30)
        _, printed, rows = self.main("screen", "--ids", ids)
        self.assertEqual(rows[0]["verdict"], "quiet")
        self.assertIn("quiet           possible/possible/possible", printed)
        self.assertIn("1 markets: 0 near impossible, 1 quiet, 0 needs human, 0 errors.", printed)

    def test_needs_ids_or_n(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.main("screen")


class OutcomesTest(EvalTest):
    def setUp(self):
        super().setUp()
        self.store("0xddd")
        self.store("0xeee")
        self.store("0xfff", runs=(run(), run("possible"), run()))

    def test_a_near_impossible_market_whose_favorite_lost_fails(self):
        resolved = {"0xddd": Resolution(True, self.fav("0xddd")),
                    "0xeee": Resolution(True, "Yes"), "0xfff": Resolution(True, self.fav("0xfff"))}
        code, printed, rows = self.main(
            "outcomes", resolve=lambda cid: resolved.get(cid, Resolution(False, None)))
        self.assertEqual([(r["condition_id"], r["verdict"], r["favorite_won"]) for r in rows],
                         [("0xfff", "needs_human", "True"), ("0xeee", "near_impossible", "False"),
                          ("0xddd", "near_impossible", "True")])
        self.assertIn("near_impossible  favorite won 1, lost 1", printed)
        self.assertIn("1 needs-human markets resolved for the favorite", printed)
        self.assertIn("FAIL near impossible favorite lost: 1 of 2 near-impossible markets", printed)
        self.assertEqual(code, 1)

    def test_passes_when_every_near_impossible_favorite_won_and_skips_open_markets(self):
        resolved = {"0xddd": Resolution(True, self.fav("0xddd")),
                    "0xeee": Resolution(True, None)}
        code, printed, rows = self.main(
            "outcomes", resolve=lambda cid: resolved.get(cid, Resolution(False, None)))
        self.assertEqual([r["condition_id"] for r in rows], ["0xddd"])
        self.assertIn("PASS near impossible favorite lost: 0 of 1", printed)
        self.assertIn("quiet favorite lost: not enough resolved, 0 of the 20", printed)
        self.assertEqual(code, 0)


class QuietOutcomesTest(EvalTest):
    def quiet_markets(self, n):
        cids = [f"0xq{i:02}" for i in range(n)]
        farm_db.store_scan(self.db, [farm_db.ScanRow(
            cid, cid, f"{cid}?", (NOW + timedelta(days=30)).isoformat(), 1.0,
            json.dumps({"market": {"fav_outcome": "No"}})) for cid in cids], cids, NOW)
        for cid in cids:
            self.store(cid, runs=(run("possible"),) * 3, days_left=30)
        return cids

    def outcomes(self, cids, lost):
        return self.main("outcomes", resolve=lambda cid: Resolution(
            True, "Yes" if cid in cids[:lost] else "No") if cid in cids
            else Resolution(False, None))

    def test_counts_quiet_by_favorite_won_or_lost_and_waits_for_20_resolved(self):
        cids = self.quiet_markets(19)
        code, printed, rows = self.outcomes(cids, lost=5)
        self.assertEqual(sum(r["verdict"] == "quiet" for r in rows), 19)
        self.assertIn("quiet            favorite won 14, lost 5", printed)
        self.assertIn("0 needs-human markets resolved for the favorite", printed)
        self.assertIn("quiet favorite lost: not enough resolved, 19 of the 20 quiet markets "
                      "it needs", printed)
        self.assertNotIn("PASS quiet", printed)
        self.assertNotIn("FAIL quiet", printed)

    def test_passes_at_5_percent_lost_and_fails_above_once_20_resolved(self):
        cids = self.quiet_markets(20)
        _, printed, _ = self.outcomes(cids, lost=1)
        self.assertIn("PASS quiet favorite lost: 1 of 20 quiet markets (5%, at most 5%)", printed)
        code, printed, _ = self.outcomes(cids, lost=2)
        self.assertIn("FAIL quiet favorite lost: 2 of 20 quiet markets (10%, at most 5%)",
                      printed)
        self.assertEqual(code, 1)


class GroundingTest(EvalTest):
    def item(self, url, verified):
        return triage.NewsItem("t", "q", url, None, verified)

    def test_counts_quotes_and_items_of_every_news_check_and_fails_on_a_circular_source(self):
        self.store("0xddd", runs=(run(), run(quote_ok=False), run()), news=())
        self.store("0xeee", news=(
            triage.NewsCheck(("s",), True, (self.item("https://a.org", True),
                                            self.item("https://b.org", True)), "s"),
            triage.NewsCheck(("s",), True, (self.item("https://c.org", False),
                                            self.item("https://polymarket.com/e", None)), "s")))
        farm_db.store_triage(self.db, "0xfff", NOW, 3, "sonnet", result=V3_RESULT)
        farm_db.store_triage(self.db, "0xfff", NOW, triage.PROMPT_VERSION, "sonnet",
                             error="boom")
        code, printed, rows = self.main("grounding")
        self.assertIn("6 rule quotes over 2 screens; 83% verbatim.", printed)
        self.assertIn("4 news items: 50% verified, 25% unverified, 25% unreadable.", printed)
        self.assertIn("FAIL circular sources: 1 news items cite a prediction market", printed)
        self.assertEqual(sum(r["circular"] == "True" for r in rows), 1)
        self.assertEqual(code, 1)

    def test_no_circular_source_passes(self):
        self.store("0xddd", news=(CLEAN, triage.NewsCheck((), True, (
            self.item("https://a.org", True),), "s")))
        code, printed, _ = self.main("grounding")
        self.assertIn("PASS circular sources: 0 news items", printed)
        self.assertEqual(code, 0)


class RetroTest(EvalTest):
    def test_lists_open_rejected_markets_still_paying_that_screen_near_impossible_or_quiet(self):
        for cid in ("0xddd", "0xeee", "0xfff", "0xggg"):
            farm_db.reject_market(self.db, cid, None, farm_db.RejectReason.ECONOMICS, NOW)
        self.store("0xfff", runs=(run("possible"),) * 3, days_left=30)
        claude = StubClaude({"delta?"})
        code, printed, rows = self.main(
            "retro", run=claude,
            resolve=lambda cid: Resolution(cid == "0xeee", None))
        self.assertEqual(claude.questions(), {"delta?"})
        self.assertEqual([(r["condition_id"], r["verdict"], r["reconsider"], r["reason"])
                          for r in rows],
                         [("0xddd", "near_impossible", "True", "The economics don't work"),
                          ("0xfff", "quiet", "True", "The economics don't work")])
        apr = json.loads(farm_db.find_market(self.db, "0xddd")["metrics"])["net_apr"]
        self.assertIn("2 rejected markets still paying and open; 1 screen near impossible, "
                      "1 screen quiet:", printed)
        self.assertIn(f"reconsider (near impossible) delta? [The economics don't work] "
                      f"net APR {apr:.1%}", printed)
        self.assertIn("reconsider (quiet) foxtrot? [The economics don't work]", printed)

    def test_does_not_reconsider_a_market_that_needs_a_human(self):
        farm_db.reject_market(self.db, "0xfff", None, farm_db.RejectReason.ECONOMICS, NOW)
        _, printed, rows = self.main("retro", run=StubClaude(set()),
                                     resolve=lambda cid: Resolution(False, None))
        self.assertEqual([(r["verdict"], r["reconsider"]) for r in rows],
                         [("needs_human", "False")])
        self.assertNotIn("reconsider (", printed)


class FetchResolutionTest(unittest.TestCase):
    def fetch(self, answer, condition_id="0xabc"):
        self.asked = []

        def gamma(request):
            self.asked.append((request.method, str(request.url),
                               request.extensions["timeout"]["read"]))
            return answer

        client = httpx.Client(transport=httpx.MockTransport(gamma))
        with mock.patch("httpx.Client", return_value=client):
            return triage_eval.fetch_resolution(condition_id)

    def closed(self, prices, condition_id="0xabc"):
        return httpx.Response(200, json=[{
            "conditionId": condition_id, "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(prices)}])

    def test_it_asks_gamma_once_for_the_closed_market_and_names_the_winner(self):
        self.assertEqual(self.fetch(self.closed(["0.0005", "0.9995"])), Resolution(True, "No"))
        self.assertEqual(self.asked, [
            ("GET", f"{GAMMA_HOST}/markets?condition_ids=0xabc&closed=true", 15)])

    def test_a_closed_market_with_no_outcome_at_99c_has_no_winner_yet(self):
        self.assertEqual(self.fetch(self.closed(["0.5", "0.5"])), Resolution(True, None))

    def test_a_market_gamma_does_not_list_as_closed_is_open(self):
        for answer in (httpx.Response(200, json=[]), httpx.Response(200, content=b"null"),
                       self.closed(["0", "1"], condition_id="0xother")):
            with self.subTest(body=answer.text):
                self.assertEqual(self.fetch(answer), Resolution(False, None))

    def test_a_refused_read_raises_after_one_request(self):
        for status in (429, 500):
            with self.subTest(status=status):
                with self.assertRaises(httpx.HTTPStatusError):
                    self.fetch(httpx.Response(status, headers={"retry-after": "0"}))
                self.assertEqual(len(self.asked), 1)


if __name__ == "__main__":
    unittest.main()
