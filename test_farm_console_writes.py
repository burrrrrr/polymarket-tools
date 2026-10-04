import json
import re
import threading
import time
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

import farm_db
import reward_farmer
import reward_screener
from farm_console import forms, views
from farm_console.app import create_app
from farm_console.remote import Rules
from farm_db import CommandKind
from reward_screener import Levels, RewardMarket, ScreenConfig
from test_farm_console import A, B, C, ORIGIN, Console

# Favorite mid 0.96, tick 0.001: the longshot bid must sit at 1.7c to score (BUDGET),
# and the favorite bid rests at 95.0c.
BUDGET_BOOK = Levels(bids=[(0.955, 5.0), (0.95, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)
# Favorite mid 0.99: a 0.1c longshot bid scores (FLOOR); the favorite bid rests at 98.0c.
FLOOR_BOOK = Levels(bids=[(0.985, 1000.0)], asks=[(0.995, 1000.0)], tick=0.001)


def assessed_row(cid, slug, book, now, rank, question=None, days=100):
    market = RewardMarket(cid, slug, question or f"{slug}?", now + timedelta(days=days), False,
                          0.001, f"tok-{cid}", "No", 50.0, 0.035, 20.0)
    a = reward_screener.assess(market, book, ScreenConfig(), now)
    assert not isinstance(a, reward_screener.Skip), a
    return reward_screener.scan_row(a._replace(rank_score=rank))


def form(client, **fields):
    return {"console_token": client.app_token, **fields}


class Writes(Console):
    """Console with real assessments: D (budget, rank 30), E (floor, rank 20) and
    F (floor, rank 10) are candidates; A and B are approved."""

    def setUp(self):
        super().setUp()
        farm_db.store_scan(self.db, [
            assessed_row("0xddd", "delta", BUDGET_BOOK, self.now, 30.0),
            assessed_row("0xeee", "echo", FLOOR_BOOK, self.now, 20.0),
            assessed_row("0xfff", "foxtrot", FLOOR_BOOK, self.now, 10.0),
            assessed_row(A, "alpha", FLOOR_BOOK, self.now, 5.0),
        ], [A, B, C, "0xddd", "0xeee", "0xfff"], self.now)
        farm_db.reject_market(self.db, C, "not for us")
        self.client.app_token = self.app.state.token

    def post(self, url, **fields):
        return self.client.post(url, data=form(self.client, **fields), follow_redirects=False)

    def row(self, cid):
        return farm_db.find_market(self.db, cid)

    def snapshot_markets(self):
        return [tuple(r) for r in self.db.execute(
            "SELECT condition_id, status, p_true, naked_budget_usd, capital_usd, note, paused, "
            "naked_paused FROM markets ORDER BY condition_id")]


class SecurityTest(Writes):
    def app_kwargs(self):
        return {"poll_s": 0.01, "max_polls": 2}

    def test_foreign_host_is_refused_on_every_kind_of_request(self):
        for host in ("evil.example:8787", "127.0.0.1:9999", "localhost", "attacker.test"):
            for method, url in (("GET", "/"), ("GET", "/stream"), ("GET", "/static/app.css"),
                                ("GET", "/api/overview"), ("POST", f"/markets/{A}/paused")):
                with self.subTest(host=host, url=url):
                    resp = self.client.request(method, url, headers={"Host": host},
                                               data=form(self.client, value="1"))
                    self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.row(A)["paused"], 0)

    def test_localhost_by_name_is_the_console_too(self):
        self.assertEqual(self.client.get("/", headers={"Host": "localhost:8787"}).status_code,
                         200)

    def test_post_without_token_is_refused_and_writes_nothing(self):
        before = self.snapshot_markets()
        for url, data in ((f"/markets/{A}/paused", {"value": "1"}),
                          ("/farmer/cancel-all", {"confirm": "CANCEL"}),
                          ("/farmer/stop", {}),
                          ("/review/0xddd/approve", {"p_true": "0.97", "naked_budget": "25"})):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data=data).status_code, 403)
        self.assertEqual(self.snapshot_markets(), before)
        self.assertIsNone(farm_db.latest_command(self.db, CommandKind.STOP))

    def test_wrong_token_in_header_or_form_is_refused(self):
        resp = self.client.post(f"/markets/{A}/paused", data={"value": "1"},
                                headers={"X-Console-Token": "guess"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(f"/markets/{A}/paused",
                                data={"value": "1", "console_token": "guess"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.row(A)["paused"], 0)

    def test_right_token_in_header_or_form_passes(self):
        resp = self.client.post(f"/markets/{A}/paused", data={"value": "1"},
                                headers={"X-Console-Token": self.app.state.token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.row(A)["paused"], 1)
        self.assertEqual(self.post(f"/markets/{B}/paused", value="1").status_code, 200)
        self.assertEqual(self.row(B)["paused"], 1)

    def test_foreign_origin_is_refused_even_with_the_token(self):
        for origin in ("http://evil.example", "null", "http://127.0.0.1:9999",
                       "https://127.0.0.1:8787"):
            with self.subTest(origin=origin):
                resp = self.client.post(f"/markets/{A}/paused", data=form(self.client, value="1"),
                                        headers={"Origin": origin})
                self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.row(A)["paused"], 0)
        resp = self.client.post(f"/markets/{A}/paused", data=form(self.client, value="1"),
                                headers={"Origin": ORIGIN})
        self.assertEqual((resp.status_code, self.row(A)["paused"]), (200, 1))

    def test_pages_carry_the_token_for_htmx_and_forms(self):
        token = self.app.state.token
        page = self.client.get("/settings").text
        self.assertIn(f'<meta name="console-token" content="{token}">', page)
        self.assertIn(f'hx-headers=\'{{"x-console-token": "{token}"}}\'', page)
        self.assertIn(f'<input type="hidden" name="console_token" value="{token}">', page)

    def test_each_process_has_its_own_token(self):
        self.assertNotEqual(self.app.state.token, create_app(self.path).state.token)
        other = TestClient(create_app(self.path), base_url=ORIGIN)
        resp = other.post(f"/markets/{A}/paused", data=form(self.client, value="1"))
        self.assertEqual(resp.status_code, 403)


class MarketControlTest(Writes):
    def test_pause_and_resume_set_the_level_and_refresh_the_regions(self):
        resp = self.post(f"/markets/{A}/paused", value="1")
        self.assertEqual(resp.headers["HX-Trigger"], "controls")
        self.post(f"/markets/{A}/paused", value="1")
        self.assertEqual(self.row(A)["paused"], 1)
        card = self.client.get("/partials/markets").text
        self.assertRegex(card, rf'hx-post="/markets/{A}/paused" hx-vals=\'{{"value": "0"}}\''
                               r' hx-swap="none">Resume</button>')
        self.post(f"/markets/{A}/paused", value="0")
        self.assertEqual(self.row(A)["paused"], 0)

    def test_naked_leg_pause_and_resume(self):
        self.post(f"/markets/{A}/naked-paused", value="1")
        self.assertEqual((self.row(A)["naked_paused"], self.row(A)["paused"]), (1, 0))
        self.assertIn("Resume naked leg", self.client.get(f"/markets/{A}").text)
        self.post(f"/markets/{A}/naked-paused", value="0")
        self.assertEqual(self.row(A)["naked_paused"], 0)

    def test_retire_asks_first_and_moves_status(self):
        card = self.client.get("/partials/markets").text
        self.assertIn('hx-confirm="Retire &quot;alpha?&quot;? The farmer cancels', card)
        self.post(f"/markets/{A}/retire")
        self.assertEqual((self.row(A)["status"], self.row(A)["note"]),
                         ("retired", "retired from the console"))
        self.assertIn(f'href="/review/{A}">Re-approve</a>', self.client.get("/partials/markets").text)

    def test_bad_value_or_unknown_market_writes_nothing(self):
        before = self.snapshot_markets()
        self.assertEqual(self.post(f"/markets/{A}/paused", value="yes").status_code, 400)
        self.assertEqual(self.post("/markets/0xnope/paused", value="1").status_code, 404)
        self.assertEqual(self.snapshot_markets(), before)


class FakeRemote:
    def __init__(self):
        self.rule_calls = 0
        self.fail_rules = False
        self.position = reward_farmer.Position(12.0, 0.951, 0.0, 0.0)
        self.fail_position = False

    def rules(self, condition_id):
        self.rule_calls += 1
        if self.fail_rules:
            raise ConnectionError("gamma unreachable")
        return Rules("Resolves to Yes if delta happens.", "https://example.org/source",
                     None)

    def holdings(self, user, condition_id, fav_token_id):
        if self.fail_position:
            raise ConnectionError("data api down")
        return self.position


class ReviewTest(Writes):
    def app_kwargs(self):
        self.remote = FakeRemote()
        self.clock = [None]
        from farm_console.app import utc_now

        def clock():
            return self.clock[0] or utc_now()
        return {"fetch_rules": self.remote.rules, "fetch_position": self.remote.holdings,
                "funder": "0xme", "clock": clock}

    def test_review_opens_the_top_ranked_candidate(self):
        resp = self.client.get("/review", follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/review/0xddd"))

    def test_queue_steps_through_candidates_by_rank(self):
        page = self.client.get("/review/0xeee").text
        self.assertIn("2 of 3 undecided", page)
        self.assertIn('href="/review/0xddd">Previous', page)
        self.assertIn('href="/review/0xfff">Next', page)

    def test_page_shows_plan_strip_stats_and_decision(self):
        page = self.client.get("/review/0xddd").text
        for needle in ("BUDGET", "BUY No 95.0c", "BUY Yes 1.7c", "best bid 95.0c",
                       "our bid 95.0c", "Claude triage (planned)", "Approve and start quoting",
                       "Est $/day", "Reject"):
            self.assertIn(needle, page)
        self.assertIn('hx-trigger="input changed delay:300ms"', page)

    def test_approve_candidate_writes_terms_and_goes_to_the_next(self):
        resp = self.post("/review/0xddd/approve", p_true="0.97", naked_budget="30", capital="")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/review/0xeee"))
        row = self.row("0xddd")
        self.assertEqual((row["status"], row["p_true"], row["naked_budget_usd"], row["capital_usd"]),
                         ("approved", 0.97, 30.0, None))

    def test_last_candidate_decided_goes_to_the_previous_then_the_empty_queue(self):
        resp = self.post("/review/0xfff/reject", note="")
        self.assertEqual(resp.headers["location"], "/review/0xeee")
        for cid in ("0xddd", "0xeee"):
            self.post(f"/review/{cid}/reject")
        resp = self.client.get("/review", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No undecided candidates", resp.text)

    def test_editing_an_approved_market_updates_terms_and_goes_to_its_page(self):
        farm_db.set_approval(self.db, A, 0.99, 25.0, 200.0)
        page = self.client.get(f"/review/{A}").text
        self.assertIn('value="200"', page)
        self.assertIn("Save terms", page)
        resp = self.post(f"/review/{A}/approve", p_true="0.985", naked_budget="10", capital="")
        self.assertEqual(resp.headers["location"], f"/markets/{A}")
        row = self.row(A)
        self.assertEqual((row["status"], row["p_true"], row["naked_budget_usd"], row["capital_usd"]),
                         ("approved", 0.985, 10.0, None))

    def test_re_approving_a_retired_market_makes_it_approved(self):
        self.post(f"/markets/{A}/retire")
        self.assertIn("Re-approve and start quoting", self.client.get(f"/review/{A}").text)
        self.post(f"/review/{A}/approve", p_true="0.99", naked_budget="25", capital="150")
        row = self.row(A)
        self.assertEqual((row["status"], row["capital_usd"]), ("approved", 150.0))

    def test_reject_with_note_and_retire_from_review(self):
        self.post("/review/0xeee/reject", note="  resolution risk ")
        self.assertEqual((self.row("0xeee")["status"], self.row("0xeee")["note"]),
                         ("rejected", "resolution risk"))
        resp = self.post(f"/review/{B}/retire")
        self.assertEqual(resp.headers["location"], f"/markets/{B}")
        self.assertEqual(self.row(B)["status"], "retired")

    def test_invalid_decisions_show_inline_errors_and_write_nothing(self):
        before = self.snapshot_markets()
        for fields, message in (
            ({"p_true": "1.2", "naked_budget": "25"}, "strictly between 0 and 1"),
            ({"p_true": "0", "naked_budget": "25"}, "strictly between 0 and 1"),
            ({"p_true": "abc", "naked_budget": "25"}, "strictly between 0 and 1"),
            ({"p_true": "nan", "naked_budget": "25"}, "strictly between 0 and 1"),
            ({"p_true": "0.97", "naked_budget": "-1"}, "0 or more"),
            ({"p_true": "0.97", "naked_budget": ""}, "0 or more"),
            ({"p_true": "0.97", "naked_budget": "25", "capital": "0"}, "positive amount"),
        ):
            with self.subTest(fields=fields):
                resp = self.post("/review/0xddd/approve", **fields)
                self.assertEqual(resp.status_code, 422)
                self.assertIn(f'<span class="field-error" role="alert">', resp.text)
                self.assertIn(message, resp.text)
                self.assertIn(f'value="{fields["p_true"]}"', resp.text)
        resp = self.post("/review/0xddd/reject", note="x" * 501)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("under 500 characters", resp.text)
        self.assertEqual(self.snapshot_markets(), before)

    def ev(self, cid, p_true, budget="25"):
        return self.client.get(f"/partials/review/{cid}/ev",
                               params={"p_true": p_true, "naked_budget": budget}).text

    def test_ev_check_uncapped_capped_and_out_of_band(self):
        self.assertIn("Planned bid 95.0c is at or under your cap of 96.0c", self.ev("0xddd", "0.97"))
        capped = self.ev("0xddd", "0.955")
        self.assertIn("Planned bid 95.0c is above your cap of 94.5c (p_true minus 1.0c). "
                      "The farmer bids 94.5c instead, which scores 0.327", capped)
        self.assertIn('class="ev c-warn"', capped)
        self.assertIn("outside the reward band", self.ev("0xddd", "0.93"))
        self.assertIn("strictly between 0 and 1", self.ev("0xddd", "1.5"))

    def test_ev_check_uses_the_effective_ev_margin(self):
        farm_db.put_setting(self.db, "ev_margin", 0.025, self.now)
        self.assertIn("Planned bid 95.0c is above your cap of 94.5c (p_true minus 2.5c)",
                      self.ev("0xddd", "0.97"))

    def test_ev_check_warns_when_budget_is_below_the_longshot_leg(self):
        plan = json.loads(self.row("0xddd")["metrics"])["plan"]
        cost = plan["low_price"] * plan["low_size"]
        self.assertIn(f"ties up ${cost:,.2f}, above your $25.00 naked budget",
                      self.ev("0xddd", "0.97", "25"))
        self.assertNotIn("BUDGET market", self.ev("0xddd", "0.97", str(cost + 1)))
        self.assertNotIn("BUDGET market", self.ev("0xeee", "0.995", "0"))

    def test_rules_are_cached_for_ten_minutes_and_failures_offer_retry(self):
        from farm_console.app import utc_now
        start = utc_now()
        self.clock[0] = start
        text = self.client.get("/partials/review/0xddd/rules").text
        self.assertIn("Resolves to Yes if delta happens.", text)
        self.assertIn("https://example.org/source", text)
        self.clock[0] = start + timedelta(minutes=9)
        self.client.get("/partials/review/0xddd/rules")
        self.assertEqual(self.remote.rule_calls, 1)
        self.clock[0] = start + timedelta(minutes=11)
        self.remote.fail_rules = True
        text = self.client.get("/partials/review/0xddd/rules").text
        self.assertIn("Could not load the rules from Gamma: ConnectionError: gamma unreachable",
                      text)
        self.assertIn(">Retry</button>", text)
        self.remote.fail_rules = False
        self.assertIn("delta happens", self.client.get("/partials/review/0xddd/rules").text)
        self.assertEqual(self.remote.rule_calls, 3)

    def test_holdings_show_the_baseline_or_say_the_fetch_failed(self):
        text = self.client.get("/partials/review/0xddd/holdings").text
        self.assertIn("The wallet holds 12 No at 95.1c and 0 Yes. These become the baseline",
                      text)
        self.remote.position = reward_farmer.Position()
        self.assertIn("holds none of this market",
                      self.client.get("/partials/review/0xddd/holdings").text)
        self.remote.fail_position = True
        self.assertIn("Could not read the wallet's holdings: ConnectionError: data api down",
                      self.client.get("/partials/review/0xddd/holdings").text)


class FakeScan:
    def __init__(self, now, rows=()):
        self.now = now
        self.release = threading.Event()
        self.configs = []
        self.error = None
        self.rows = rows

    def __call__(self, config, now, on_stage):
        self.configs.append(config)
        on_stage("listing reward markets")
        self.release.wait(5)
        if self.error:
            raise self.error
        funnel = farm_db.ScanFunnel(now, 61.0, 18952, 4664, 1, {"favorite too cheap": 7397},
                                    {"one-sided book": 49})
        return reward_screener.ScanResult(list(self.rows), {"0xnew"}, funnel)


def scan_form(**changes):
    values = {k: views.number_text(v) for k, v in ScreenConfig()._asdict().items()}
    return {**values, **changes}


class ScanTest(Writes):
    def app_kwargs(self):
        market = RewardMarket("0xnew", "novel", "Novel market?", self.now + timedelta(days=100),
                              False, 0.001, "tok-new", "No", 50.0, 0.035, 20.0)
        self.fake = FakeScan(self.now, [reward_screener.assess(market, FLOOR_BOOK, ScreenConfig(),
                                                               self.now)])
        return {"scan": self.fake}

    def wait_for(self, phase):
        for _ in range(200):
            text = self.client.get("/partials/discover/scan")
            if phase in text.text:
                return text
            time.sleep(0.02)
        self.fail(f"scan never reached {phase}: {text.text}")

    def test_scan_runs_once_at_a_time_and_stores_its_result(self):
        resp = self.post("/discover/scan", **scan_form(min_fav="0.92", capital="300"))
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/discover"))
        self.wait_for("Scanning: listing reward markets")
        running = self.client.get("/discover").text
        self.assertIn("Scanning: listing reward markets", running)
        self.assertIn('hx-trigger="every 2s"', running)
        self.assertIn("Scan in progress", running)

        second = self.post("/discover/scan", min_fav="0.9")
        self.assertEqual(second.status_code, 409)
        self.assertIn("A scan is already in progress.", second.text)

        self.fake.release.set()
        done = self.wait_for("Done")
        self.assertEqual(done.headers["HX-Refresh"], "true")
        self.assertIn("18,952 listed, 4,664 passed the prefilter, 1 assessed", done.text)
        self.assertEqual(len(self.fake.configs), 1)
        self.assertEqual(self.fake.configs[0], ScreenConfig(min_fav=0.92, capital=300.0))
        page = self.client.get("/discover").text
        self.assertIn("Novel market?", page)
        self.assertIn("favorite too cheap 7,397", page)
        self.assertIn("one-sided book 49", page)
        self.assertIn('name="min_fav" value="0.92"', page)
        self.assertEqual(farm_db.load_scan_params(self.db)["capital"], 300.0)
        self.assertEqual(farm_db.load_scan_funnel(self.db).listed, 18952)

    def test_a_failed_scan_says_why_and_stores_nothing(self):
        self.fake.error = RuntimeError("clob down")
        self.fake.release.set()
        self.post("/discover/scan", **scan_form())
        text = self.wait_for("Failed").text
        self.assertIn("RuntimeError: clob down", text)
        self.assertIsNone(farm_db.load_scan_funnel(self.db))
        self.assertIsNone(self.row("0xnew"))

    def test_invalid_parameters_start_nothing_and_persist_nothing(self):
        for name, value in (("min_fav", "2"), ("capital", "0"), ("bid_offset", "abc"),
                            ("min_rate", "-1")):
            with self.subTest(name=name):
                resp = self.post("/discover/scan", **scan_form(**{name: value}))
                self.assertEqual(resp.status_code, 422)
                self.assertIn('<span class="field-error" role="alert">Out of range', resp.text)
        self.assertEqual((self.fake.configs, farm_db.load_scan_params(self.db)), ([], {}))

    def test_form_defaults_to_the_screener_defaults(self):
        page = self.client.get("/discover").text
        self.assertIn('name="min_fav" value="0.9"', page)
        self.assertIn('name="horizon_cap_days" value="90"', page)


class DiscoverTest(Writes):
    def setUp(self):
        super().setUp()
        rows = []
        for i in range(120):
            book = BUDGET_BOOK if i % 3 == 0 else FLOOR_BOOK
            question = "Will 50% of x happen?" if i == 7 else f"Market number {i}?"
            rows.append(assessed_row(f"0x{i:04d}", f"m-{i}", book, self.now, 1000.0 - i,
                                     question, days=40 if i % 10 == 0 else 200))
        farm_db.store_scan(self.db, rows, [r.condition_id for r in rows], self.now)

    def ids(self, query=""):
        return re.findall(r'<a href="/review/(0x\w+)">', self.client.get("/discover" + query).text)

    def test_candidates_by_rank_fifty_per_page(self):
        first, third = self.ids(), self.ids("?page=3")
        self.assertEqual(first, [f"0x{i:04d}" for i in range(50)])
        self.assertEqual(third, [f"0x{i:04d}" for i in range(100, 120)]
                         + ["0xddd", "0xeee", "0xfff"])
        self.assertIn("page 3 of 3", self.client.get("/discover?page=3").text)

    def test_mode_filter_and_paging_links_keep_the_filters(self):
        budget = self.ids("?mode=budget")
        self.assertEqual(budget, [f"0x{i:04d}" for i in range(0, 120, 3)] + ["0xddd"])
        floor = self.ids("?mode=floor&page=2")
        self.assertEqual(floor[0], "0x0076")
        self.assertIn('href="/discover?mode=floor&amp;status=candidate&amp;page=2">Next',
                      self.client.get("/discover?mode=floor").text)

    def test_search_matches_question_or_slug_literally(self):
        self.assertEqual(self.ids("?q=50%25"), ["0x0007"])
        self.assertEqual(self.ids("?q=M-11"), ["0x0011", "0x0110", "0x0111", "0x0112",
                                               "0x0113", "0x0114", "0x0115", "0x0116",
                                               "0x0117", "0x0118", "0x0119"])
        self.assertEqual(self.ids("?q=_"), [])

    def test_status_and_min_days_filters(self):
        self.assertEqual(sorted(self.ids("?status=approved")), [A, B])
        self.assertEqual(self.ids("?status=rejected"), [C])
        self.assertEqual(len(self.ids("?status=all")), 50)
        long_enough = self.ids("?min_days=60&page=1")
        self.assertNotIn("0x0000", long_enough)
        self.assertNotIn("0x0010", long_enough)
        self.assertIn("0x0001", long_enough)

    def test_bad_filters_are_refused(self):
        for query in ("?mode=wild", "?status=gone", "?min_days=soon"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/discover" + query).status_code, 400)


class SettingsTest(Writes):
    def test_each_field_shows_its_value_and_where_it_comes_from(self):
        farm_db.put_setting(self.db, "bid_offset", 0.015, self.now)
        page = self.client.get("/settings").text
        self.assertRegex(page, r'name="bid_offset" value="0.015"[^>]*>\s*<button[^>]*'
                               r'formaction="/settings/reset/bid_offset"')
        self.assertRegex(page, r'(?s)name="min_fav" value="0.9".*?farmer&#39;s value')
        self.assertRegex(page, r'(?s)name="cooldown_s" value="600".*?>default<')
        self.assertNotIn("Global caps", page)

    def save(self, group, **changes):
        page = self.client.get("/settings").text
        values = dict(re.findall(r'name="(\w+)" value="([^"]*)" inputmode', page))
        keys = [k for k, f in forms.SETTING_FIELDS.items() if f.group == group]
        return self.post(f"/settings/group/{group}", **{**{k: values[k] for k in keys},
                                                         **changes})

    def test_saving_writes_only_the_changed_fields(self):
        resp = self.save("Quoting", bid_offset="0.02")
        self.assertEqual((resp.status_code, resp.headers["location"]),
                         (303, "/settings?saved=Quoting"))
        self.assertEqual(farm_db.load_settings(self.db), ({"bid_offset": 0.02}, {}))
        self.assertIn("Saved Quoting", self.client.get("/settings?saved=Quoting").text)

    def test_invalid_values_show_inline_and_write_nothing(self):
        resp = self.save("Quoting", bid_offset="0.9", keep_ratio="abc", capital="300")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("bid_offset: 0.9 is out of range", resp.text)
        self.assertIn("keep_ratio: &#39;abc&#39; is not JSON", resp.text)
        self.assertIn('name="capital" value="300"', resp.text)
        self.assertEqual(farm_db.load_settings(self.db), ({}, {}))

    def test_reset_deletes_the_override(self):
        farm_db.put_setting(self.db, "cooldown_s", 30, self.now)
        resp = self.post("/settings/reset/cooldown_s")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(farm_db.load_settings(self.db), ({}, {}))

    def test_stop_sends_a_command_and_shows_when_it_was_consumed(self):
        page = self.client.get("/settings").text
        self.assertIn('hx-confirm="Stop the farmer?', page)
        self.assertIn("python reward_farmer.py --live --db", page)
        text = self.post("/farmer/stop").text
        command = farm_db.latest_command(self.db, CommandKind.STOP)
        self.assertIsNone(command.consumed_at)
        self.assertIn(f"Stop command {command.id} sent", text)
        self.assertIn("is waiting for the farmer", text)
        farm_db.consume_command(self.db, command.id, "stopping", self.now)
        text = self.client.get("/partials/settings/farmer").text
        self.assertIn("was consumed at", text)
        self.assertIn(": stopping.", text)

    def test_cancel_all_needs_the_typed_word_on_the_server(self):
        for typed in ("", "cancel", "CANCEL ALL", "yes"):
            with self.subTest(typed=typed):
                resp = self.post("/farmer/cancel-all", confirm=typed)
                self.assertEqual(resp.status_code, 422)
                self.assertIn("Type CANCEL to confirm.", resp.text)
        self.assertEqual([self.row(c)["paused"] for c in (A, B)], [0, 0])
        resp = self.post("/farmer/cancel-all", confirm="CANCEL")
        self.assertEqual(resp.headers["location"], "/settings?cancelled=2")
        self.assertEqual([self.row(c)["paused"] for c in (A, B, "0xddd")], [1, 1, 0])
        self.assertIn("Paused 2 approved markets", self.client.get(resp.headers["location"]).text)


class NavAndAlertsTest(Writes):
    def test_nav_links_and_queue_count(self):
        page = self.client.get("/").text
        for href in ("/discover", "/review", "/settings"):
            self.assertIn(f'<a href="{href}"', page)
        self.assertIn('Review queue <span class="badge t-n count">3</span>', page)
        self.assertNotIn("Coming in stage 3", page)

    def test_alerts_link_to_their_actions(self):
        self.publish(stopped=self.now)
        alerts = self.client.get("/partials/overview/alerts").text
        self.assertIn('<a href="/settings">Farmer status</a>', alerts)
        farm_db.set_approval(self.db, A, 0.99, 0.5, None)
        self.publish()
        alerts = self.client.get("/partials/overview/alerts").text
        self.assertIn(f'<a href="/review/{A}">Edit budget</a>', alerts)


if __name__ == "__main__":
    unittest.main()
