import json
import re
import threading
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest import mock

from fastapi.testclient import TestClient

from farm import farm_db, fill_cost, reward_farmer, reward_screener, triage
from farm.farm_db import CommandKind
from farm.reward_screener import RewardMarket, ScreenConfig
from console import forms, remote, views
from console.app import create_app, utc_now
from console.remote import Rules
from polymarket.books import Levels
from test_console import (
    A,
    B,
    C,
    ORIGIN,
    Console,
    quoting_slots,
    settle_total_rates,
    state,
)
from test_triage import brief_output, envelope, judge_output

# Favorite mid 0.96, tick 0.001: no longshot bid under 0.6c scores, which 40 days out
# offers a favorite buyer over 5% a year, so the longshot bid sits at the cheapest price
# covering the favorite bid within the budget (BUDGET), and the favorite bid rests at
# the 95.5c ceiling. Each book's deepest bid sits outside the
# band, so it scores nothing, but if the best bid left it would keep the midpoint
# above min_fav and less than 3c down (not fragile).
BUDGET_BOOK = Levels(bids=[(0.955, 5.0), (0.95, 1000.0), (0.90, 1000.0)],
                     asks=[(0.97, 1000.0)], tick=0.001)
# Favorite mid 0.99: a 0.1c longshot bid scores, in the free zone (FLOOR); the favorite
# bid rests at the 99.0c ceiling.
FLOOR_BOOK = Levels(bids=[(0.985, 1000.0), (0.95, 1000.0)], asks=[(0.995, 1000.0)],
                    tick=0.001)
# Its favorite fell since the scan: mid 0.885 is under min_fav, so the farmer would not quote.
FALLEN_BOOK = Levels(bids=[(0.80, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)
# No other bid supports a favorite bid here. A sole bid at 96.1c, two ticks over the
# lowest that keeps a 0.1c longshot bid in the 3.5c band, makes a 96.55c midpoint.
SOLE_BOOK = Levels(bids=[(0.80, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)


def assessed_row(cid, slug, book, now, rank, question=None, days=100, **topic):
    market = RewardMarket(cid, slug, question or f"{slug}?", now + timedelta(days=days), False,
                          0.001, f"tok-{cid}", "No", 50.0, 0.035, 20.0, **topic)
    a = reward_screener.assess(market, book, ScreenConfig(), now, fill_cost.DEFAULT_PRIOR)
    assert not isinstance(a, reward_screener.Skip), a
    return reward_screener.scan_row(a._replace(rank_score=rank))


def sole_row(cid, slug, now, rank):
    """The scan row of a market only a sole bid can quote, read on SOLE_BOOK:
    a plan of mode sole that earns the whole pool, with no fills valued."""
    row = assessed_row(cid, slug, FLOOR_BOOK, now, rank)
    capital = 0.961 * 520.29 + 0.001 * 10000.0
    return row._replace(metrics=json.dumps({
        **json.loads(row.metrics),
        "plan": {"mid": 0.9655, "fav_bid_price": 0.961, "fav_bid_size": 520.29,
                 "fav_bid_score": 0.7594, "low_price": 0.001, "low_size": 10000.0,
                 "low_score": 0.0018, "mode": "sole"},
        "mid": 0.9655, "book_mid": 0.885, "best_bid": 0.80, "best_ask": 0.97,
        "comp_q": 0.0, "share": 1.0, "est_daily_usd": 50.0, "net_daily_usd": 50.0,
        "capital_usd": capital, "apr": 50.0 / capital * 365, "net_apr": 50.0 / capital * 365,
        "fills_per_day": None, "fill_cost_usd": None, "loss_per_share": None,
        "fill_source": None, "history": None}))


def form(client, **fields):
    return {"console_token": client.app_token, **fields}


class Writes(Console):
    """Console with real assessments: D (budget, rank 30), E (floor, rank 20) and
    F (floor, rank 10) are candidates; A and B are approved. D ends in 40 days,
    where the prices that score on its book offer a favorite buyer over 5% a
    year, and the others in 100. G (rank 50) and H (rank 40) are sole
    candidates, which outrank the rest and list only under the sole filter."""

    def setUp(self):
        super().setUp()
        farm_db.store_scan(self.db, [
            assessed_row("0xddd", "delta", BUDGET_BOOK, self.now, 30.0, days=40),
            assessed_row("0xeee", "echo", FLOOR_BOOK, self.now, 20.0),
            assessed_row("0xfff", "foxtrot", FLOOR_BOOK, self.now, 10.0),
            assessed_row(A, "alpha", FLOOR_BOOK, self.now, 5.0),
            sole_row("0x777", "golf", self.now, 50.0),
            sole_row("0x888", "hotel", self.now, 40.0),
        ], [A, B, C, "0xddd", "0xeee", "0xfff", "0x777", "0x888"], self.now)
        farm_db.reject_market(self.db, C, "not for us", farm_db.RejectReason.OTHER, self.now)
        self.client.app_token = self.app.state.token

    def post(self, url, **fields):
        return self.client.post(url, data=form(self.client, **fields), follow_redirects=False)

    def row(self, cid):
        return farm_db.find_market(self.db, cid)

    def snapshot_markets(self):
        return [tuple(r) for r in self.db.execute(
            "SELECT condition_id, status, p_true, naked_budget_usd, capital_usd, note, paused, "
            "naked_paused, sole_bid, bid_multiple FROM markets ORDER BY condition_id")]


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
                          ("/review/0xddd/approve", {"p_true": "97", "naked_budget": "25"})):
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


FR = "2027-french-presidential-election"


class TopicTest(Writes):
    """D and E are in the FR event; E and F carry the French Election tag."""

    def setUp(self):
        super().setUp()
        farm_db.store_scan(self.db, [
            assessed_row("0xddd", "delta", BUDGET_BOOK, self.now, 30.0, event_slug=FR,
                         event_title="2027 French presidential election",
                         tags=("Politics", "France")),
            assessed_row("0xeee", "echo", FLOOR_BOOK, self.now, 20.0, event_slug=FR,
                         event_title="2027 French presidential election",
                         tags=("Politics", "French Election")),
            assessed_row("0xfff", "foxtrot", FLOOR_BOOK, self.now, 10.0,
                         tags=("French Election",)),
        ], [A, B, C, "0xddd", "0xeee", "0xfff"], self.now)
        self.seeded = {d.id for d in farm_db.read_decisions(self.db)}

    def decisions(self):
        """The decisions made since the setup, newest first."""
        return [(d.condition_id, d.decision, d.reason) for d in farm_db.read_decisions(self.db)
                if d.id not in self.seeded]

    def discover_ids(self):
        return re.findall(r'<a href="/review/(0x\w+)">', self.client.get("/discover").text)

    def test_review_shows_the_event_and_tags_each_with_a_mute_button(self):
        page = self.client.get("/review/0xeee").text
        self.assertIn('<span class="small">2027 French presidential election</span>', page)
        self.assertIn(f'name="event" value="{FR}"', page)
        for tag in ("Politics", "French Election"):
            self.assertIn(f'<span class="badge t-n">{tag}</span><button class="btn small-btn" '
                          f'type="submit" name="tag" value="{tag}"', page)
        self.assertIn("Also mute this event: 2027 French presidential election", page)
        page = self.client.get("/review/0xfff").text
        self.assertIn('name="tag" value="French Election"', page)
        self.assertNotIn('name="event"', page)
        self.assertNotIn("Also mute this event", page)
        self.assertNotIn(f'action="/review/{C}/mute"', self.client.get(f"/review/{C}").text)

    def test_reject_refuses_an_unknown_reason_and_logs_the_metrics_it_saw(self):
        resp = self.post("/review/0xddd/reject", note="meh", reason="bored")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Pick a reason from the list, or none.", resp.text)
        self.assertIn('value="meh"', resp.text)
        self.assertEqual((self.row("0xddd")["status"], self.decisions()), ("candidate", []))
        metrics = json.loads(self.row("0xddd")["metrics"])
        resp = self.post("/review/0xddd/reject", reason="unfamiliar_topic", note="")
        self.assertEqual(resp.headers["location"], "/review/0xeee")
        d = farm_db.read_decisions(self.db, limit=1)[0]
        self.assertEqual((d.condition_id, d.decision, d.reason, d.note, d.metrics),
                         ("0xddd", "rejected", farm_db.RejectReason.UNFAMILIAR_TOPIC, None,
                          metrics))

    def test_reject_without_a_reason_logs_none(self):
        for fields in ({}, {"reason": ""}):
            with self.subTest(fields=fields):
                farm_db.unreject_market(self.db, "0xddd", utc_now())
                resp = self.post("/review/0xddd/reject", note="", **fields)
                self.assertEqual(resp.status_code, 303)
                self.assertEqual(self.row("0xddd")["status"], "rejected")
                d = farm_db.read_decisions(self.db, limit=1)[0]
                self.assertEqual((d.decision, d.reason), ("rejected", None))

    def test_reject_can_mute_the_event_and_goes_past_its_siblings(self):
        resp = self.post("/review/0xddd/reject", reason="unfamiliar_topic", mute_event="1")
        self.assertEqual(resp.headers["location"], "/review/0xfff")
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xfff"])
        self.assertEqual([(m.kind, m.value, m.label) for m in farm_db.read_mutes(self.db)],
                         [(farm_db.MuteKind.EVENT, FR, "2027 French presidential election")])

    def test_mute_buttons_hide_the_topic_from_the_queue_and_discover(self):
        self.assertEqual(self.discover_ids(), ["0xddd", "0xeee", "0xfff"])
        resp = self.post("/review/0xddd/mute", tag="Politics")
        self.assertEqual(resp.headers["location"], "/review/0xfff")
        self.assertEqual((farm_db.candidate_queue(self.db), self.discover_ids()),
                         (["0xfff"], ["0xfff"]))
        self.assertEqual(self.row("0xddd")["status"], "candidate")
        resp = self.post("/review/0xfff/mute", tag="French Election", queue_mode="floor")
        self.assertEqual(resp.headers["location"], "/review?mode=floor")
        self.assertEqual(self.discover_ids(), [])
        self.assertEqual(self.decisions(), [])

    def test_mute_takes_only_the_markets_own_event_or_tags(self):
        for fields in ({"tag": "Sports"}, {"event": "other-event"}, {"tag": ""}, {}):
            with self.subTest(fields=fields):
                self.assertEqual(self.post("/review/0xfff/mute", **fields).status_code, 400)
        self.assertEqual(farm_db.read_mutes(self.db), [])

    def test_unreject_puts_a_rejected_market_back_in_the_queue(self):
        page = self.client.get(f"/review/{C}").text
        self.assertIn(f'formaction="/review/{C}/unreject" formnovalidate>'
                      f'Un-reject (back to the queue)</button>', page)
        self.assertIn('<button class="btn" type="submit">Approve and start quoting</button>', page)
        self.assertNotIn("Un-reject", self.client.get("/review/0xddd").text)
        resp = self.post(f"/review/{C}/unreject", queue_mode="floor")
        self.assertEqual(resp.headers["location"], f"/review/{C}?mode=floor")
        self.assertEqual(self.row(C)["status"], "candidate")
        self.assertIn(C, farm_db.candidate_queue(self.db))
        self.post("/review/0xddd/unreject")
        self.assertEqual(self.row("0xddd")["status"], "candidate")
        self.assertEqual(self.decisions(), [(C, "unrejected", None)])

    def test_settings_lists_mutes_with_what_each_hides_and_unmutes(self):
        self.assertIn("Nothing is muted.", self.client.get("/settings").text)
        farm_db.mute(self.db, farm_db.MuteKind.EVENT, FR, "2027 French presidential election",
                     self.now)
        farm_db.mute(self.db, farm_db.MuteKind.TAG, "France", "France", self.now)
        page = self.client.get("/settings").text
        self.assertRegex(page, r'(?s)2027 French presidential election</td>\s*'
                               r'<td class="mono">2 candidates</td>')
        self.assertRegex(page, r'(?s)>France</td>\s*<td class="mono">1 candidate</td>')
        resp = self.post("/settings/unmute", kind="event", value=FR,
                         label="2027 French presidential election")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Unmuted 2027 French presidential election. Its candidates are back",
                      self.client.get(resp.headers["location"]).text)
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xeee", "0xfff"])
        self.assertEqual(self.post("/settings/unmute", kind="bogus", value="France").status_code,
                         400)
        self.assertEqual([m.value for m in farm_db.read_mutes(self.db)], ["France"])


class MarketControlTest(Writes):
    def test_pause_and_resume_set_the_level_and_refresh_the_regions(self):
        resp = self.post(f"/markets/{A}/paused", value="1")
        self.assertEqual(resp.headers["HX-Trigger"], "controls")
        self.post(f"/markets/{A}/paused", value="1")
        self.assertEqual(self.row(A)["paused"], 1)
        card = self.client.get(f"/partials/markets/{A}/card").text
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

    def test_sole_bid_allow_and_stop(self):
        badge = '<span class="badge t-c">sole bid allowed'
        pages = ("/partials/markets", f"/markets/{A}", f"/review/{A}")
        expanded = f"/partials/markets/{A}/card"

        def button(value, label):
            return (f'hx-post="/markets/{A}/sole-bid" hx-vals=\'{{"value": "{value}"}}\''
                    f' hx-swap="none">{label} sole bid</button>')

        self.assertIn(button(1, "Allow"), self.client.get(expanded).text)
        for url in pages:
            self.assertNotIn(badge, self.client.get(url).text)
        resp = self.post(f"/markets/{A}/sole-bid", value="1")
        self.assertEqual((resp.status_code, resp.headers["HX-Trigger"]), (200, "controls"))
        self.post(f"/markets/{A}/sole-bid", value="1")
        self.assertEqual([self.row(c)["sole_bid"] for c in (A, B)], [1, 0])
        self.assertEqual((self.row(A)["paused"], self.row(A)["naked_paused"]), (0, 0))
        for url in pages:
            self.assertEqual(self.client.get(url).text.count(badge), 1)
        for url in (expanded, f"/markets/{A}"):
            self.assertIn(button(0, "Stop"), self.client.get(url).text)
        self.post(f"/markets/{A}/sole-bid", value="0")
        self.assertEqual(self.row(A)["sole_bid"], 0)

    def test_retire_asks_first_and_moves_status(self):
        card = self.client.get(f"/partials/markets/{A}/card").text
        self.assertIn('hx-confirm="Retire &quot;alpha?&quot;? The farmer cancels', card)
        self.post(f"/markets/{A}/retire")
        self.assertEqual((self.row(A)["status"], self.row(A)["note"]),
                         ("retired", "retired from the console"))
        self.assertIn(f'href="/review/{A}">Re-approve</a>',
                      self.client.get(f"/partials/markets/{A}/card").text)

    def test_bad_value_or_unknown_market_writes_nothing(self):
        before = self.snapshot_markets()
        self.assertEqual(self.post(f"/markets/{A}/paused", value="yes").status_code, 400)
        self.assertEqual(self.post("/markets/0xnope/paused", value="1").status_code, 404)
        self.assertEqual(self.post(f"/markets/{A}/sole-bid", value="yes").status_code, 400)
        self.assertEqual(self.post("/markets/0xnope/sole-bid", value="1").status_code, 404)
        self.assertEqual(self.snapshot_markets(), before)

    def test_end_cooldown_sends_the_farmer_a_command_naming_the_market(self):
        resp = self.post(f"/markets/{A}/end-cooldown")
        self.assertEqual((resp.status_code, resp.headers["HX-Trigger"]), (200, "controls"))
        self.assertEqual([(c.kind, c.payload) for c in farm_db.pending_commands(self.db)],
                         [("end_cooldown", {"condition_id": A})])
        self.assertEqual(self.post("/markets/0xnope/end-cooldown").status_code, 404)
        self.assertEqual(len(farm_db.pending_commands(self.db)), 1)

    def test_the_end_cooldown_button_shows_only_while_the_market_cools_down(self):
        button = f'hx-post="/markets/{A}/end-cooldown" hx-swap="none">End cooldown</button>'
        pages = (f"/partials/markets/{A}/card", f"/markets/{A}", "/partials/markets/look")
        for url in pages:
            self.assertNotIn("end-cooldown", self.client.get(url).text)
        cooling = state(A, "COOLDOWN", "ask drop", until=self.now + timedelta(minutes=10))
        self.publish(states=[cooling._replace(updated_at=self.now)])
        for url in pages:
            self.assertEqual(self.client.get(url).text.count(button), 1)


class FakeRemote:
    def __init__(self):
        self.rule_calls = 0
        self.fail_rules = False
        self.position = reward_farmer.Position(12.0, 0.951, 0.0, 0.0)
        self.fail_position = False
        self.book = BUDGET_BOOK
        self.books = {}
        self.book_calls = 0
        self.fail_book = False
        self.total_rates = {}
        self.totals_gate = threading.Event()
        self.totals_gate.set()

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

    def info(self, condition_id):
        return reward_farmer.MarketInfo((f"tok-{condition_id}", "tok-long"), 0.035, 20.0,
                                        0.001, False, True, False)

    def totals(self):
        self.totals_gate.wait(5)
        return dict(self.total_rates)

    def levels(self, token_id):
        self.book_calls += 1
        if self.fail_book:
            raise ConnectionError("clob unreachable")
        return self.books.get(token_id, self.book)


class ReviewTest(Writes):
    def app_kwargs(self):
        self.remote = FakeRemote()
        self.clock = [None]
        from console.app import utc_now

        def clock():
            return self.clock[0] or utc_now()
        return {"fetch_rules": self.remote.rules, "fetch_position": self.remote.holdings,
                "fetch_info": self.remote.info, "fetch_book": self.remote.levels,
                "fetch_total_rates": self.remote.totals, "funder": "0xme", "clock": clock}

    def test_review_opens_the_top_ranked_candidate(self):
        resp = self.client.get("/review", follow_redirects=False)
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/review/0xddd"))

    def test_queue_steps_through_candidates_by_rank(self):
        page = self.client.get("/review/0xeee").text
        self.assertIn("2 of 3 undecided", page)
        self.assertIn('href="/review/0xddd">Previous', page)
        self.assertIn('href="/review/0xfff">Next', page)

    def test_page_shows_live_region_and_decision(self):
        page = self.client.get("/review/0xddd").text
        self.assertIn('name="naked_budget" value="10"', page)
        self.assertIn('type="number" min="1" max="99" step="1"', page)
        mid = json.loads(self.row("0xddd")["metrics"])["book_mid"]
        self.assertIn(f'data-start="{round(mid * 100)}"', page)
        for needle in ("BUDGET", 'hx-get="/partials/review/0xddd/triage"',
                       "Approve and start quoting",
                       "Reject", 'hx-get="/partials/review/0xddd/live"',
                       "every 5s [document.visibilityState === 'visible']",
                       'hx-include="#d-p_true, #d-follow_market, #d-naked_budget, #d-capital, '
                       '#d-bid_multiple, #d-sole_bid"'):
            self.assertIn(needle, page)
        self.assertIn('hx-trigger="input changed delay:300ms"', page)

    def live(self, cid="0xddd", **terms):
        terms = {"p_true": "97", "naked_budget": "25", "capital": "", **terms}
        return self.client.get(f"/partials/review/{cid}/live", params=terms).text

    def set_metrics(self, cid, **changes):
        metrics = {**json.loads(self.row(cid)["metrics"]), **changes}
        self.db.execute("UPDATE markets SET metrics = ? WHERE condition_id = ?",
                        (json.dumps(metrics), cid))
        self.db.commit()

    def test_page_header_carries_the_market_facts_and_no_scan_stats(self):
        page = self.client.get("/review/0xddd").text
        self.assertIn("ahead 1.4x", page)
        self.assertIn("holding rewards unknown", page)
        self.assertNotIn("vol ", page)
        self.assertNotIn('class="panel pad stats"', page)
        self.assertIn("Planning against the live book...", page)
        self.set_metrics("0xddd", volume24hr=12345.0, holding_rewards=False)
        page = self.client.get("/review/0xddd").text
        self.assertIn("vol 12.3k", page)
        self.assertNotIn("holding rewards", page)

    def projection(self, text):
        """Label to value text of each figure in the projection fragment."""
        fragment = re.search(r'<div id="projection" class="projection note-box small" '
                             r'hx-swap-oob="true">(.*?)</div>', text, re.S)
        self.assertIsNotNone(fragment, "no out-of-band projection")
        return fragment.group(1), dict(re.findall(
            r'<dt class="lbl">([^<]+)<button[^>]*></button></dt>\s*<dd class="mono[^"]*">'
            r'([^<]*?)\s*<', fragment.group(1)))

    def planned(self, p_true, capital, prior=fill_cost.DEFAULT_PRIOR):
        cfg = reward_farmer.config_from(reward_farmer.build_parser().parse_args([]))._replace(
            **farm_db.load_settings(self.db)[0])
        row = {**dict(self.row("0xddd")), "p_true": p_true, "naked_budget_usd": 25.0,
               "capital_usd": capital}
        return views.live_review(row, self.remote.info("0xddd"), BUDGET_BOOK, None, cfg,
                                 self.now, False, prior)

    def test_the_preview_prices_the_longshot_bid_by_the_time_to_the_end_date(self):
        # 40 days from the end date the longshot bid rests at 1.7c, the
        # cheapest price scoring 0.10. 20 days out that offers the favorite
        # buyer 37% a year, and nothing over 0.9c is allowed. 100 days out
        # 1c offers 3.7% a year, the free zone, at $0.49 for each unit of
        # score, and the bid rests there whatever the budget.
        cfg = reward_farmer.config_from(reward_farmer.build_parser().parse_args([]))

        def longshot_bid(days):
            row = {**dict(self.row("0xddd")), "p_true": 0.97, "naked_budget_usd": 25.0,
                   "end_date": (self.now + timedelta(days=days)).isoformat()}
            live = views.live_review(row, self.remote.info("0xddd"), BUDGET_BOOK, None, cfg,
                                     self.now, False, fill_cost.DEFAULT_PRIOR)
            return next(o.price for o in live.orders if o.slot == "LONG_BID")

        self.assertEqual(longshot_bid(40), 0.017)
        self.assertEqual(longshot_bid(20), 0.009)
        self.assertEqual(longshot_bid(100), 0.01)

    def test_live_partial_projects_the_plan_out_of_band(self):
        fragment, figures = self.projection(self.live())
        live = self.planned(0.97, None)
        self.assertEqual(figures["Est share"], f"{live.share * 100:.1f}%")
        self.assertEqual(figures["Net/day"], views.usd(live.value.net_per_day))
        self.assertEqual(figures["Capital"], views.usd(sum(o.cost for o in live.orders)))
        scan = json.loads(self.row("0xddd")["metrics"])
        self.assertIn(f"scan {scan['share'] * 100:.1f}%", fragment)
        self.assertIn(f"scan {views.usd(scan['est_daily_usd'])}", fragment)
        self.assertIn("Fills from the built-in default prior: run a scan.", fragment)

    def test_projection_names_the_scan_prior_it_values_fills_on(self):
        rates = tuple(r * 3 for r in fill_cost.DEFAULT_PRIOR.rates)
        prior = fill_cost.DEFAULT_PRIOR._replace(rates=rates, markets=37, at=self.now)
        farm_db.store_scan(self.db, [], [A, B, C, "0xddd", "0xeee", "0xfff"], self.now,
                           prior=fill_cost.prior_to_json(prior))
        # 100 days out a fill ties capital up long enough to cost something.
        self.db.execute("UPDATE markets SET end_date = ? WHERE condition_id = '0xddd'",
                        ((self.now + timedelta(days=100)).isoformat(),))
        self.db.commit()
        fragment, figures = self.projection(self.live())
        self.assertIn(f"Fills from the cross-market prior alone (37 markets, scan of "
                      f"{views.stamp_local(self.now)})", fragment)
        self.assertEqual(figures["Net/day"],
                         views.usd(self.planned(0.97, None, prior).value.net_per_day))
        self.assertNotEqual(figures["Net/day"],
                            views.usd(self.planned(0.97, None).value.net_per_day))

    def at_the_cap(self):
        """The farmer's bid multiple set to 1 and its tie band to 0, so the
        bid rests where the typed terms cap it. At the defaults a bid whose
        longshot cover the $25 budget limits rests 1,470 shares at 93.7c,
        under any cap these tests type, and the band rests a tick or more
        under the cap."""
        farm_db.put_settings(self.db, {"bid_multiple": 1.0, "tie_band": 0.0}, self.now)

    def test_projection_follows_the_terms_being_typed(self):
        self.at_the_cap()
        full = self.projection(self.live())[1]
        for terms, p_true, capital in (({"capital": "100"}, 0.97, 100.0),
                                       ({"p_true": "96"}, 0.96, None)):
            with self.subTest(**terms):
                figures = self.projection(self.live(**terms))[1]
                self.assertNotEqual(figures["Net/day"], full["Net/day"])
                self.assertNotEqual(figures["Capital"], full["Capital"])
                self.assertEqual(figures["Est share"],
                                 f"{self.planned(p_true, capital).share * 100:.1f}%")

    def test_projection_says_why_it_has_no_figures(self):
        self.remote.fail_book = True
        fragment, figures = self.projection(self.live())
        self.assertIn("No projection while the live book can't be read", fragment)
        self.assertEqual(figures, {})
        self.remote.fail_book = False
        fragment, figures = self.projection(self.live(A))
        self.assertIn("The farmer manages this market", fragment)
        self.assertEqual(figures, {})

    def test_share_is_our_score_against_the_competing_score(self):
        live, target = self.preview()
        my_q = reward_farmer.q_min(*reward_farmer.q_totals(target.intents), target.mid)
        comp_q = reward_screener.competing_score(BUDGET_BOOK, target.mid, 0.035, 20.0)
        self.assertEqual(live.pool_per_day, 50.0)
        self.assertAlmostEqual(live.share, live.value.reward_per_day / live.pool_per_day)
        self.assertAlmostEqual(live.share, my_q / (my_q + comp_q))
        self.assertIsNone(replace(live, pool_per_day=0.0).share)
        self.assertIsNone(replace(live, value=None).share)

    def test_live_orders_are_the_farmers_plan_against_the_live_book(self):
        text = self.live()
        cfg = reward_farmer.config_from(reward_farmer.build_parser().parse_args([]))
        fm = reward_farmer.with_book_mid(reward_farmer.farm_market(
            {**dict(self.row("0xddd")), "p_true": 0.97, "naked_budget_usd": 25.0,
             "capital_usd": None}, self.remote.info("0xddd"), cfg, self.now), BUDGET_BOOK)
        target = reward_farmer.target_orders(
            fm, BUDGET_BOOK, reward_farmer.Position(), cfg, True,
            reward_farmer.market_outlook(fm, None, None, fill_cost.DEFAULT_PRIOR, cfg, self.now),
            bid_without_history=True)
        self.assertEqual([i.slot.name for i in target.intents], ["FAV_BID", "LONG_BID"])
        for intent in target.intents:
            self.assertIn(f"{intent.slot.name} {views.shares(intent.size)}</td>", text)
        bid = views.cents(target.intents[0].price)
        for needle in (f"Buy No {bid}", "Buy Yes 1.7c", f"bid at {bid}", "ask at 98.3c",
                       f"our bid {bid}", "best bid 95.5c", "best ask 97.0c",
                       "longshot bid (mirror) 98.3c"):
            self.assertIn(needle, text)

    def test_live_plan_follows_the_terms_being_typed(self):
        self.at_the_cap()
        full = self.live()
        capped = self.live(capital="100")
        self.assertNotEqual(re.findall(r"FAV_BID ([\d,.]+)</td>", full),
                            re.findall(r"FAV_BID ([\d,.]+)</td>", capped))
        self.assertIn("Enter your p_true or tick Follow the market.", self.live(p_true=""))
        self.assertIn("Enter a naked budget. Until then a longshot bid outside the free zone "
                      "is planned with $0", self.live(naked_budget="x"))
        self.assertIn("Buy No 94.0c", self.live(p_true="96"))
        # Capped at 93.0c the bid scores 0.02, and at a 300% hurdle its fills
        # cost less than that earns. A 2.4c margin caps it at 92.6c, where it
        # scores 0.0008, and at a 1,000% hurdle its fills cost more.
        farm_db.put_settings(self.db, {"hurdle_rate": 3.0}, self.now)
        self.assertIn("Buy No 93.0c", self.live(p_true="95"))
        farm_db.put_settings(self.db, {"hurdle_rate": 10.0, "ev_margin": 0.024}, self.now)
        self.assertRegex(self.live(p_true="95"), r"The farmer would place nothing: no favorite "
                                                 r"bid pays: best net -\$\d+\.\d\d/day at 92\.6c\.")

    def test_live_plan_rests_the_bid_at_the_typed_multiple_of_the_capital(self):
        # On echo's floor book the longshot cover is free, so nothing but the
        # farmer's $500 capital, times the multiple, limits the favorite bid.
        self.remote.books["tok-0xeee"] = FLOOR_BOOK

        def bid_cost(multiple):
            text = self.live("0xeee", p_true="99", bid_multiple=multiple)
            found = re.search(r'>FAV_BID</span>(?:(?!</tr>).)*?<td class="mono">\$([\d,.]+)</td>',
                              text, re.S)
            self.assertIsNotNone(found, f"no FAV_BID planned at bid_multiple={multiple!r}")
            return float(found.group(1).replace(",", ""))
        self.assertLessEqual(bid_cost("1"), 500.0)
        self.assertGreater(bid_cost("4"), 3 * 500.0)
        self.assertLessEqual(bid_cost("4"), 4 * 500.0)
        self.assertEqual(bid_cost(""), bid_cost("4"))
        self.assertEqual(bid_cost("x"), bid_cost("4"))

    def test_live_plan_follows_the_sole_bid_box_when_no_other_bid_supports_the_market(self):
        # The only bid left is 80c under a 97c ask: an 88.5c midpoint.
        self.remote.books["tok-0xddd"] = BUDGET_BOOK._replace(bids=[(0.80, 1000.0)],
                                                              asks=[(0.97, 1000.0)])
        without = self.live(p_true="99")
        self.assertIn("The farmer would not quote: the midpoint 88.5c is under min_fav", without)
        self.assertNotIn("Buy No", without)
        allowed = self.live(p_true="99", sole_bid="1")
        self.assertIn("the farmer would hold a sole bid", allowed)
        # 96.1c is two ticks over the lowest bid that keeps a 0.1c longshot bid in the 3.5c band.
        self.assertIn("Buy No 96.1c", allowed)
        self.assertIn("Buy Yes 0.1c", allowed)
        self.assertIn('id="d-sole_bid"', self.client.get("/review/0xddd").text)

    def test_live_book_is_cached_briefly_and_a_failed_read_says_so(self):
        from console.app import utc_now
        start = utc_now()
        self.clock[0] = start
        self.live()
        self.live(capital="100")
        self.assertEqual(self.remote.book_calls, 1)
        self.clock[0] = start + timedelta(seconds=3)
        self.remote.fail_book = True
        self.assertIn("Could not read the live book: ConnectionError: clob unreachable",
                      self.live())
        self.remote.fail_book = False
        text = self.live()
        self.assertIn("Buy No", text)
        self.assertNotIn("Could not read the live book", text)

    def test_live_plan_without_p_true_follows_the_market(self):
        # The 96.0c book midpoint caps the bid with no margin, over the 95.5c
        # that max_lead allows above the 95.0c best bid of the reward minimum.
        # A view of 96% keeps the 2c margin and caps it at 94.0c.
        self.at_the_cap()
        text = self.live(p_true="", follow_market="1")
        self.assertIn("Buy No 95.5c", text)
        self.assertIn("Following the market: the favorite bid is capped at the book midpoint, "
                      "with no EV margin", text)
        self.assertIn("Buy No 94.0c", self.live(p_true="96"))

    def preview(self, **stored):
        """(live_review, the farmer's own target) of 0xddd with these metrics keys stored."""
        cfg = reward_farmer.config_from(reward_farmer.build_parser().parse_args([]))
        row = dict(self.row("0xddd"))
        metrics = json.loads(row["metrics"])
        row.update(p_true=0.97, naked_budget_usd=25.0, capital_usd=None,
                   metrics=json.dumps({**metrics, **stored}))
        live = views.live_review(row, self.remote.info("0xddd"), BUDGET_BOOK, None, cfg,
                                 self.now, False, fill_cost.DEFAULT_PRIOR)
        fm = reward_farmer.with_book_mid(
            reward_farmer.farm_market(row, self.remote.info("0xddd"), cfg, self.now), BUDGET_BOOK)
        history = fill_cost.history_from_json(stored.get("history"))
        outlook = reward_farmer.market_outlook(fm, None, history, fill_cost.DEFAULT_PRIOR, cfg,
                                               self.now)
        return live, reward_farmer.target_orders(fm, BUDGET_BOOK, reward_farmer.Position(),
                                                 cfg, True, outlook, bid_without_history=True)

    def test_live_plan_prices_fills_from_the_history_the_scan_stored(self):
        busy = fill_cost.History((40, 30, 25, 20, 15, 10, 6, 3, 1, 0, 0, 0), 60.0, self.now, 0.90,
                                 12, 60.0)
        live, target = self.preview(history=fill_cost.history_to_json(busy))
        self.assertEqual(live.fill_source, "history")
        self.assertEqual(live.value, target.fav.best)
        default, _ = self.preview()
        self.assertEqual(default.fill_source, "default")
        self.assertGreater(live.value.fills_per_day, default.value.fills_per_day)

    def test_a_row_holding_only_an_old_tape_previews_on_the_prior(self):
        old = {"sweeps": [[self.now.timestamp() - 60, 5000.0]], "span_days": 30.0,
               "observed": True, "capped": False, "fetched_at": self.now.isoformat()}
        live, target = self.preview(tape=old)
        self.assertEqual(live.fill_source, "default")
        self.assertEqual(live.value, target.fav.best)
        self.set_metrics("0xddd", tape=old)
        text = self.live()
        self.assertNotIn("Could not read the live book", text)
        self.assertIn("Fills from the built-in default prior", text)

    def test_live_region_of_a_managed_market_shows_the_book_without_a_plan(self):
        text = self.live(A)
        self.assertIn("The farmer manages this market", text)
        self.assertNotIn("Buy No", text)
        self.assertIn("midpoint", text)

    def test_approve_candidate_writes_terms_and_goes_to_the_next(self):
        resp = self.post("/review/0xddd/approve", p_true="97", naked_budget="30", capital="")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/review/0xeee"))
        row = self.row("0xddd")
        self.assertEqual((row["status"], row["p_true"], row["naked_budget_usd"], row["capital_usd"]),
                         ("approved", 0.97, 30.0, None))

    def test_approve_following_the_market_stores_no_p_true(self):
        self.assertIn("No p_true: the farmer follows the market.",
                      self.ev("0xddd", "", follow_market="1"))
        self.assertIn("Enter a whole percent from 1 to 99, or tick Follow the market.",
                      self.ev("0xddd", ""))
        resp = self.post("/review/0xddd/approve", naked_budget="30", capital="",
                         follow_market="1")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/review/0xeee"))
        row = self.row("0xddd")
        self.assertEqual((row["status"], row["p_true"], row["naked_budget_usd"]),
                         ("approved", None, 30.0))
        self.assertEqual([d.p_true for d in farm_db.read_decisions(self.db)
                          if d.condition_id == "0xddd"], [None])
        for url in ("/partials/markets/0xddd/card", "/markets/0xddd"):
            with self.subTest(url=url):
                self.assertIn("p_true market", self.client.get(url).text)
        # Its edit form opens with the box ticked and the p_true field empty and off.
        page = self.client.get("/review/0xddd").text
        self.assertRegex(page, r'name="follow_market" value="1"[^>]* checked')
        self.assertIn('name="p_true" value="" disabled', page)

    def test_a_sole_bid_needs_a_p_true(self):
        resp = self.post("/review/0xddd/approve", naked_budget="30", capital="",
                         follow_market="1", sole_bid="1")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("A sole bid needs your p_true. Untick Follow the market or Allow sole "
                      "bid.", resp.text)
        self.assertEqual(self.row("0xddd")["status"], "candidate")
        sole = {"market": {"tick": 0.001}, "plan": {"mode": "sole", "fav_bid_price": 0.961}}
        self.assertEqual(views.ev_check(sole, "", True, "25", 0.02, 0.25).lines,
                         ["A sole bid needs your p_true. Following the market, the farmer "
                          "holds none."])

    def test_approve_start_paused_pauses_and_goes_to_the_market(self):
        self.assertIn('name="start_paused"', self.client.get("/review/0xddd").text)
        resp = self.post("/review/0xddd/approve", p_true="97", naked_budget="30", capital="",
                         start_paused="1")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/markets/0xddd"))
        self.assertEqual((self.row("0xddd")["status"], self.row("0xddd")["paused"]), ("approved", 1))
        self.assertEqual(self.row("0xeee")["paused"], 0)
        self.assertNotIn('name="start_paused"', self.client.get("/review/0xddd").text)

    def test_approve_sets_the_sole_bid_flag_to_the_checkbox_and_shows_it_again(self):
        self.assertNotIn(' checked> Allow sole bid', self.client.get("/review/0xddd").text)
        self.post("/review/0xddd/approve", p_true="97", naked_budget="30", capital="",
                  sole_bid="1")
        self.assertEqual((self.row("0xddd")["status"], self.row("0xddd")["sole_bid"]),
                         ("approved", 1))
        self.assertIn(' checked> Allow sole bid', self.client.get("/review/0xddd").text)
        # Saving the terms with the box cleared turns it off again.
        self.post("/review/0xddd/approve", p_true="97", naked_budget="30", capital="")
        self.assertEqual(self.row("0xddd")["sole_bid"], 0)

    def test_approve_stores_the_typed_bid_multiple_and_blank_is_the_farmers(self):
        farm_db.put_setting(self.db, "bid_multiple", 3.0, self.now)
        self.assertRegex(self.client.get("/review/0xddd").text,
                         r'id="d-bid_multiple" name="bid_multiple" value=""[^>]*'
                         r'placeholder="default 3"')
        self.post("/review/0xddd/approve", p_true="97", naked_budget="30", capital="",
                  bid_multiple="2")
        self.assertEqual(self.row("0xddd")["bid_multiple"], 2.0)
        self.assertIn('name="bid_multiple" value="2"', self.client.get("/review/0xddd").text)
        self.post("/review/0xddd/approve", p_true="97", naked_budget="30", capital="",
                  bid_multiple="")
        self.assertIsNone(self.row("0xddd")["bid_multiple"])

    def test_a_rejected_form_keeps_the_bid_multiple_as_it_was_typed(self):
        for url, fields in (("/review/0xddd/approve", {"p_true": ""}),
                            ("/review/0xddd/reject", {"note": "x" * 501})):
            with self.subTest(url=url):
                resp = self.post(url, naked_budget="30", bid_multiple="7", **fields)
                self.assertEqual(resp.status_code, 422)
                self.assertIn('name="bid_multiple" value="7"', resp.text)

    def test_a_rejected_form_keeps_the_sole_bid_box_as_it_was_sent(self):
        resp = self.post("/review/0xddd/approve", p_true="", naked_budget="30", capital="",
                         sole_bid="1")
        self.assertEqual(resp.status_code, 422)
        self.assertIn(' checked> Allow sole bid', resp.text)
        self.assertEqual(self.row("0xddd")["sole_bid"], 0)

    def test_queue_filters_by_mode_and_keeps_the_mode_through_decisions(self):
        resp = self.client.get("/review?mode=floor", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/review/0xeee?mode=floor")
        page = self.client.get("/review/0xeee?mode=floor").text
        self.assertIn("1 of 2 undecided floor candidates", page)
        self.assertIn('href="/review/0xfff?mode=floor">Next', page)
        self.assertIn('<input type="hidden" name="queue_mode" value="floor">', page)
        self.assertIn('hx-get="/partials/review/0xeee/live?mode=floor"', page)
        self.assertIn("undecided floor candidates · this market is candidate, budget",
                      self.client.get("/review/0xddd?mode=floor").text)
        resp = self.post("/review/0xeee/reject", reason="other", note="", queue_mode="floor")
        self.assertEqual(resp.headers["location"], "/review/0xfff?mode=floor")
        resp = self.post("/review/0xfff/reject", reason="other", note="", queue_mode="floor")
        self.assertEqual(resp.headers["location"], "/review?mode=floor")
        self.assertEqual(self.client.get("/review?mode=bogus").status_code, 422)

    def test_sole_candidates_queue_apart_under_the_sole_filter(self):
        sole_link = 'href="/review?mode=sole">Sole&nbsp;<span class="mono">2</span></a>'
        self.assertIn(sole_link, self.client.get("/review/0xddd").text)
        resp = self.client.get("/review?mode=sole", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/review/0x777?mode=sole")
        page = self.client.get("/review/0x777?mode=sole").text
        self.assertIn("1 of 2 undecided sole candidates", page)
        self.assertIn('href="/review/0x888?mode=sole">Next', page)
        # Opened from the normal queue it is outside it, like a decided market.
        self.assertIn("3 undecided candidates · this market is candidate, sole",
                      self.client.get("/review/0x777").text)
        resp = self.post("/review/0x777/reject", reason="other", note="", queue_mode="sole")
        self.assertEqual(resp.headers["location"], "/review/0x888?mode=sole")
        resp = self.post("/review/0x888/reject", reason="other", note="", queue_mode="sole")
        self.assertEqual(resp.headers["location"], "/review?mode=sole")
        self.assertIn("1 of 3 undecided candidates", self.client.get("/review/0xddd").text)

    def test_an_empty_queue_points_to_the_sole_candidates(self):
        for cid in ("0xddd", "0xeee", "0xfff"):
            self.post(f"/review/{cid}/reject", reason="other")
        page = self.client.get("/review").text
        self.assertIn("No undecided candidates", page)
        self.assertIn('2 sole candidates list under <a href="/review?mode=sole">Sole</a>.', page)
        self.post("/review/0x777/reject", reason="other")
        self.assertIn("1 sole candidate lists under", self.client.get("/review").text)
        self.post("/review/0x888/reject", reason="other")
        self.assertNotIn("sole candidate", self.client.get("/review?mode=sole").text)

    def test_a_sole_candidate_opens_with_the_sole_bid_allowed_and_no_fill_figures(self):
        page = self.client.get("/review/0x777?mode=sole").text
        self.assertIn('<span class="badge t-c">SOLE<button', page)
        self.assertIn('data-tip="sole"', page)
        self.assertIn(' checked> Allow sole bid', page)
        self.assertNotIn("None", page)

        def ev(p_true):
            return self.client.get("/partials/review/0x777/ev",
                                   params={"p_true": p_true, "naked_budget": "5"}).text
        self.assertIn("Sole bid 96.1c is within your 97.0c cap", ev("99"))
        # Under the scanned bid the cap moves the sole bid or ends it, and the
        # longshot bid is sized to any budget, so neither is re-scored here.
        capped = ev("97")
        self.assertIn("Sole bid 96.1c is over your 95.0c cap", capped)
        self.assertNotIn("scores", capped)
        self.assertNotIn("budget", capped)
        # Approved with the box cleared, the page shows the stored flag again.
        self.post("/review/0x777/approve", p_true="99", naked_budget="10", capital="",
                  queue_mode="sole")
        self.assertEqual((self.row("0x777")["status"], self.row("0x777")["sole_bid"]),
                         ("approved", 0))
        self.assertNotIn(' checked> Allow sole bid', self.client.get("/review/0x777").text)
        self.assertIn('<span class="badge t-c">SOLE<button',
                      self.client.get("/markets/0x777").text)

    def test_a_sole_candidate_previews_its_sole_bid_before_a_p_true_is_typed(self):
        self.remote.books["tok-0x777"] = SOLE_BOOK
        text = self.live("0x777", p_true="", sole_bid="1")
        self.assertIn("the farmer would hold a sole bid", text)
        self.assertIn("Buy No 96.1c", text)
        self.assertIn("Buy Yes 0.1c", text)
        self.assertIn("A sole bid needs your p_true. Until you enter one only the ask caps it, "
                      "as in the scan.", text)
        self.assertNotIn("Wouldn't quote now", text)
        self.assertNotIn("None", text)
        # A p_true caps it as it caps the farmer's: 97% less the 2c margin is under 96.1c.
        self.assertNotIn("Buy No 96.1c", self.live("0x777", p_true="97", sole_bid="1"))

    def test_skip_counts_a_sole_candidate_no_other_bid_supports_as_quotable(self):
        self.remote.books["tok-0x888"] = SOLE_BOOK
        resp = self.client.get("/review/0x777/next-quotable?mode=sole", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/review/0x888?mode=sole")

    def test_a_candidate_that_would_not_quote_now_offers_to_skip(self):
        self.remote.books["tok-0xddd"] = FALLEN_BOOK
        text = self.live(mode="budget")
        self.assertIn("Wouldn't quote now", text)
        self.assertIn("the midpoint 88.5c is under min_fav 90.0c", text)
        self.assertIn('href="/review/0xddd/next-quotable?mode=budget">Skip to next quotable',
                      text)
        self.assertNotIn("Wouldn't quote now", self.live("0xeee"))

    def test_skip_goes_to_the_next_candidate_the_farmer_would_quote(self):
        from console.app import utc_now
        start = utc_now()
        self.clock[0] = start
        self.remote.books["tok-0xeee"] = FALLEN_BOOK
        resp = self.client.get("/review/0xddd/next-quotable", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/review/0xfff?skipped=1")
        self.assertIn("Skipped 1 candidate that wouldn't quote now.",
                      self.client.get("/review/0xfff?skipped=1").text)
        self.remote.books["tok-0xfff"] = FALLEN_BOOK
        self.clock[0] = start + timedelta(seconds=3)
        resp = self.client.get("/review/0xddd/next-quotable", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/review")

    def rewards_end_for(self, *cids):
        """A loaded total listing that drops cids."""
        from console.app import utc_now
        every = [r[0] for r in self.db.execute("SELECT condition_id FROM markets")]
        self.remote.total_rates = {cid: 5.0 for cid in every
                                   if cid not in cids}
        settle_total_rates()
        self.clock[0] = (self.clock[0] or utc_now()) + remote.TOTAL_RATES_TTL
        self.app.state.total_rates.refresh()

    def discover_ids(self):
        return re.findall(r'<a href="/review/(0x\w+)">', self.client.get("/discover").text)

    def test_a_candidate_whose_rewards_ended_leaves_the_queue_and_discover(self):
        self.assertIn("0xeee", self.discover_ids())
        self.rewards_end_for("0xeee")
        text = self.live("0xeee")
        self.assertIn("Wouldn't quote now", text)
        self.assertIn("The farmer would not quote: pool under $1/day. "
                      "It has left the review queue.", text)
        self.assertIn('href="/review/0xeee/next-quotable">Skip to next quotable', text)
        self.assertEqual(self.row("0xeee")["rewards_active"], 0)
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xddd", "0xfff"])
        self.assertNotIn("0xeee", self.discover_ids())
        resp = self.client.get("/review/0xeee/next-quotable", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/review/0xfff")

    def test_returning_rewards_restore_a_candidate(self):
        farm_db.set_rewards_active(self.db, "0xeee", False)
        self.rewards_end_for()
        text = self.live("0xeee")
        self.assertNotIn("pool under $1/day", text)
        self.assertIn("Buy No", text)
        self.assertEqual(self.row("0xeee")["rewards_active"], 1)
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xddd", "0xeee", "0xfff"])

    def test_a_totals_refresh_sweeps_the_queue_with_no_market_opened(self):
        self.rewards_end_for("0xeee")
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xddd", "0xfff"])
        self.rewards_end_for()
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xddd", "0xeee", "0xfff"])
        self.assertEqual(self.row(A)["rewards_active"], 1)

    def test_listing_the_queue_refreshes_stale_totals(self):
        from console.app import utc_now
        start = utc_now()
        for i, (url, ended, queue) in enumerate((("/review", "0xddd", ["0xeee", "0xfff"]),
                                                 ("/discover", "0xeee", ["0xddd", "0xfff"]))):
            self.clock[0] = start + i * remote.TOTAL_RATES_TTL
            self.remote.total_rates = {cid: 5.0 for cid in ("0xddd", "0xeee", "0xfff")
                                       if cid != ended}
            self.client.get(url, follow_redirects=False)
            settle_total_rates()
            self.assertEqual(farm_db.candidate_queue(self.db), queue)

    def test_rewards_unknown_until_the_totals_land_writes_nothing(self):
        farm_db.set_rewards_active(self.db, "0xfff", False)
        self.remote.total_rates = {"0xelsewhere": 1.0}
        self.remote.totals_gate.clear()
        self.addCleanup(self.remote.totals_gate.set)
        self.assertIsNone(self.app.state.total_rates.get())
        text = self.live("0xeee")
        self.assertNotIn("pool under $1/day", text)
        self.assertIn("Buy No", text)
        self.live("0xfff")
        self.assertEqual((self.row("0xeee")["rewards_active"], self.row("0xfff")["rewards_active"]),
                         (1, 0))
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xddd", "0xeee"])

    def test_skip_passes_over_a_candidate_whose_rewards_ended_and_records_it(self):
        self.rewards_end_for("0xeee")
        # A scan since the listing landed has put it back in the queue.
        farm_db.set_rewards_active(self.db, "0xeee", True)
        resp = self.client.get("/review/0xddd/next-quotable", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/review/0xfff?skipped=1")
        self.assertEqual(self.row("0xeee")["rewards_active"], 0)
        self.assertEqual(farm_db.candidate_queue(self.db), ["0xddd", "0xfff"])

    def test_the_console_never_writes_rewards_of_a_market_the_farmer_manages(self):
        self.rewards_end_for(A, "0xeee")
        self.live(A)
        self.live("0xeee")
        self.assertEqual((self.row(A)["rewards_active"], self.row("0xeee")["rewards_active"]),
                         (1, 0))

    def test_last_candidate_decided_goes_to_the_previous_then_the_empty_queue(self):
        resp = self.post("/review/0xfff/reject", reason="other", note="")
        self.assertEqual(resp.headers["location"], "/review/0xeee")
        for cid in ("0xddd", "0xeee"):
            self.post(f"/review/{cid}/reject", reason="other")
        resp = self.client.get("/review", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("No undecided candidates", resp.text)

    def test_editing_an_approved_market_updates_terms_and_goes_to_its_page(self):
        farm_db.set_approval(self.db, A, 0.99, 25.0, 200.0, now=self.now)
        page = self.client.get(f"/review/{A}").text
        self.assertIn('value="200"', page)
        self.assertIn('name="p_true" value="99"', page)
        self.assertNotRegex(page, r'name="follow_market" value="1"[^>]* checked')
        self.assertIn("Save terms", page)
        resp = self.post(f"/review/{A}/approve", p_true="98", naked_budget="10", capital="")
        self.assertEqual(resp.headers["location"], f"/markets/{A}")
        row = self.row(A)
        self.assertEqual((row["status"], row["p_true"], row["naked_budget_usd"], row["capital_usd"]),
                         ("approved", 0.98, 10.0, None))

    def test_re_approving_a_retired_market_makes_it_approved(self):
        self.post(f"/markets/{A}/retire")
        self.assertIn("Re-approve and start quoting", self.client.get(f"/review/{A}").text)
        self.post(f"/review/{A}/approve", p_true="99", naked_budget="25", capital="150")
        row = self.row(A)
        self.assertEqual((row["status"], row["capital_usd"]), ("approved", 150.0))

    def test_reject_with_note_and_retire_from_review(self):
        self.post("/review/0xeee/reject", reason="resolution_risk", note="  resolution risk ")
        self.assertEqual((self.row("0xeee")["status"], self.row("0xeee")["note"]),
                         ("rejected", "resolution risk"))
        resp = self.post(f"/review/{B}/retire")
        self.assertEqual(resp.headers["location"], f"/markets/{B}")
        self.assertEqual(self.row(B)["status"], "retired")

    def test_invalid_decisions_show_inline_errors_and_write_nothing(self):
        before = self.snapshot_markets()
        for fields, message in (
            ({"p_true": "100", "naked_budget": "25"}, "whole percent from 1 to 99"),
            ({"p_true": "0", "naked_budget": "25"}, "whole percent from 1 to 99"),
            ({"p_true": "0.97", "naked_budget": "25"}, "whole percent from 1 to 99"),
            ({"p_true": "97.5", "naked_budget": "25"}, "whole percent from 1 to 99"),
            ({"p_true": "abc", "naked_budget": "25"}, "whole percent from 1 to 99"),
            ({"p_true": "nan", "naked_budget": "25"}, "whole percent from 1 to 99"),
            ({"p_true": "", "naked_budget": "25"}, "or tick Follow the market"),
            ({"p_true": "97", "naked_budget": "25", "follow_market": "1"},
             "Clear p_true or untick Follow the market."),
            ({"p_true": "97", "naked_budget": "-1"}, "0 or more"),
            ({"p_true": "97", "naked_budget": ""}, "0 or more"),
            ({"p_true": "97", "naked_budget": "25", "capital": "0"}, "positive amount"),
            *(({"p_true": "97", "naked_budget": "25", "bid_multiple": bad},
               "Enter a multiple from 1 to 20, or leave blank for the farmer&#39;s.")
              for bad in ("0.5", "21", "x", "nan")),
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

    def ev(self, cid, p_true, budget="25", **choice):
        return self.client.get(f"/partials/review/{cid}/ev",
                               params={"p_true": p_true, "naked_budget": budget, **choice}).text

    def test_ev_check_uncapped_capped_and_out_of_band(self):
        uncapped = self.ev("0xddd", "98")
        self.assertIn('class="ev c-good"', uncapped)
        self.assertRegex(uncapped, r"95\.5c.*96\.0c cap \(p_true − 2\.0c\)")
        capped = self.ev("0xddd", "96")
        self.assertIn('class="ev c-warn"', capped)
        self.assertIn("capped at 94.0c (p_true − 2.0c): scores 0.184, not 0.617", capped)
        self.assertRegex(self.ev("0xddd", "93"), r"capped at 91\.0c .*outside the band.*no reward")
        self.assertIn("whole percent from 1 to 99", self.ev("0xddd", "150"))

    def test_ev_check_uses_the_effective_ev_margin(self):
        farm_db.put_setting(self.db, "ev_margin", 0.025, self.now)
        self.assertIn("capped at 94.5c (p_true − 2.5c)", self.ev("0xddd", "97"))

    def test_ev_check_scores_the_capped_bid_against_the_mid_it_creates(self):
        # Book 93.4c / 99.0c with v 8.5c. The old plan's 97.0c bid created a 98.0c
        # midpoint; the 95.0c cap creates (95.0 + 99.0) / 2 = 97.0c instead.
        metrics = {"market": {"tick": 0.001, "v": 0.085}, "best_bid": 0.934, "best_ask": 0.99,
                   "plan": {"fav_bid_price": 0.97, "mid": 0.98, "fav_bid_score": 0.779,
                            "low_price": 0.001, "low_size": 100.0, "mode": "floor"}}
        text = " ".join(views.ev_check(metrics, "96", False, "25", 0.01, 0.25).lines)
        self.assertIn("capped at 95.0c (p_true − 1.0c): scores 0.585, not 0.779", text)
        self.assertNotIn("0.419", text)
        del metrics["best_bid"], metrics["best_ask"]
        self.assertIn("scores 0.419, not 0.779",
                      " ".join(views.ev_check(metrics, "96", False, "25", 0.01, 0.25).lines))

    def test_ev_check_warns_when_budget_is_below_the_longshot_leg(self):
        plan = json.loads(self.row("0xddd")["metrics"])["plan"]
        cost = plan["low_price"] * plan["low_size"]
        self.assertIn(f"Longshot ${cost:,.2f} is over your $5.00 budget",
                      self.ev("0xddd", "97", "5"))
        self.assertNotIn("Longshot", self.ev("0xddd", "97", str(cost)))

    def test_ev_check_does_not_warn_of_a_budget_under_a_free_bid(self):
        # 0xeee plans its longshot bid at 0.1c, which 100 days out offers a
        # favorite buyer 0.4% a year. No budget limits it.
        plan = json.loads(self.row("0xeee")["metrics"])["plan"]
        self.assertEqual(plan["low_price"], 0.001)
        self.assertNotIn("Longshot", self.ev("0xeee", "99", "0"))
        farm_db.set_approval(self.db, "0xeee", 0.99, 0.0, None, now=self.now)
        self.assertNotIn("is over your", self.client.get("/review/0xeee").text)

    def test_ev_check_warns_once_the_end_date_takes_the_bid_out_of_the_free_zone(self):
        # Five days from its end date the same 0.1c bid offers 7.6% a year,
        # and the budget limits it. The page and the partial both say so.
        self.db.execute("UPDATE markets SET end_date = ? WHERE condition_id = '0xeee'",
                        ((self.now + timedelta(days=5)).isoformat(),))
        self.db.commit()
        plan = json.loads(self.row("0xeee")["metrics"])["plan"]
        warning = f"Longshot ${plan['low_price'] * plan['low_size']:,.2f} is over your $0.00"
        self.assertIn(warning, self.ev("0xeee", "99", "0"))
        farm_db.set_approval(self.db, "0xeee", 0.99, 0.0, None, now=self.now)
        self.assertIn(warning, self.client.get("/review/0xeee").text)

    def test_rules_are_cached_for_ten_minutes_and_failures_offer_retry(self):
        from console.app import utc_now
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
        self.assertIn("holds 12 No at 95.1c, 0 Yes: the baseline", text)
        self.remote.position = reward_farmer.Position()
        self.assertIn("holds none, so every fill is farm inventory",
                      self.client.get("/partials/review/0xddd/holdings").text)
        self.remote.fail_position = True
        self.assertIn("Could not read the wallet's holdings: ConnectionError: data api down",
                      self.client.get("/partials/review/0xddd/holdings").text)


class FakeScan:
    def __init__(self, now, rows=()):
        self.now = now
        self.release = threading.Event()
        self.configs = []
        self.priors = []
        self.rejecteds = []
        self.error = None
        self.rows = rows
        self.fitted = fill_cost.DEFAULT_PRIOR._replace(markets=25, at=now)

    def __call__(self, config, now, prior, rejected, on_stage):
        self.configs.append(config)
        self.priors.append(prior)
        self.rejecteds.append(rejected)
        on_stage("listing reward markets")
        self.release.wait(5)
        if self.error:
            raise self.error
        funnel = farm_db.ScanFunnel(now, 61.0, 18952, 4664, 1, {"favorite too cheap": 7397},
                                    {"one-sided book": 49})
        return reward_screener.ScanResult(list(self.rows), {"0xnew"}, funnel, self.fitted)


def scan_form(**changes):
    config = ScreenConfig()
    values = {k: views.number_text(getattr(config, k)) for k in forms.SCAN_FIELDS}
    excluded = {forms.exclude_field(c): "1" for c in config.excluded_categories}
    return {**values, **excluded, **changes}


class ScanTest(Writes):
    def app_kwargs(self):
        market = RewardMarket("0xnew", "novel", "Novel market?", self.now + timedelta(days=100),
                              False, 0.001, "tok-new", "No", 50.0, 0.035, 20.0)
        self.fake = FakeScan(self.now, [reward_screener.assess(market, FLOOR_BOOK, ScreenConfig(),
                                                               self.now, fill_cost.DEFAULT_PRIOR)])
        return {"scan": self.fake}

    def wait_for(self, phase):
        for _ in range(200):
            text = self.client.get("/partials/discover/scan")
            if phase in text.text:
                return text
            time.sleep(0.02)
        self.fail(f"scan never reached {phase}: {text.text}")

    def test_scan_runs_once_at_a_time_and_stores_its_result(self):
        farm_db.reject_market(self.db, "0xeee", "", farm_db.RejectReason.OTHER, self.now)
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
        self.assertEqual(self.fake.priors, [fill_cost.DEFAULT_PRIOR])
        self.assertEqual(self.fake.rejecteds, [frozenset({C, "0xeee"})])
        self.assertEqual(reward_screener.load_prior(self.db), self.fake.fitted)

    def test_a_failed_scan_says_why_and_stores_nothing(self):
        self.fake.error = RuntimeError("clob down")
        self.fake.release.set()
        self.post("/discover/scan", **scan_form())
        text = self.wait_for("Failed").text
        self.assertIn("RuntimeError: clob down", text)
        self.assertIsNone(farm_db.load_scan_funnel(self.db))
        self.assertIsNone(self.row("0xnew"))
        self.assertEqual(reward_screener.load_prior(self.db), fill_cost.DEFAULT_PRIOR)

    def test_invalid_parameters_start_nothing_and_persist_nothing(self):
        for name, value in (("min_fav", "2"), ("capital", "0"), ("max_lead", "abc"),
                            ("hurdle_rate", "-0.1"), ("tape_markets", "5000"),
                            ("min_rate", "-1"), ("fragile_drop", "-0.01")):
            with self.subTest(name=name):
                resp = self.post("/discover/scan", **scan_form(**{name: value}))
                self.assertEqual(resp.status_code, 422)
                self.assertIn('<span class="field-error" role="alert">Out of range', resp.text)
        self.assertEqual((self.fake.configs, farm_db.load_scan_params(self.db)), ([], {}))

    def test_form_defaults_to_the_screener_defaults(self):
        page = self.client.get("/discover").text
        self.assertIn('name="min_fav" value="0.9"', page)
        self.assertIn('name="horizon_cap_days" value="90"', page)
        self.assertIn('name="fragile_drop" value="0.03"', page)
        self.assertIn('name="naked_budget" value="10"', page)

    def test_form_excludes_sports_crypto_and_esports_by_default(self):
        page = self.client.get("/discover").text
        for category in reward_screener.CATEGORIES:
            checked = category in ("sports", "esports", "crypto")
            with self.subTest(category=category):
                self.assertEqual(f'name="exclude_{category}" value="1" checked' in page, checked)
                self.assertIn(f'name="exclude_{category}" value="1"', page)

    def test_excluded_categories_are_what_is_checked_and_are_remembered(self):
        form = {**scan_form(exclude_politics="1"), "exclude_sports": ""}
        self.fake.release.set()
        self.post("/discover/scan", **form)
        self.wait_for("Done")
        self.assertEqual(self.fake.configs[0].excluded_categories,
                         ("esports", "crypto", "politics"))
        page = self.client.get("/discover").text
        self.assertIn('name="exclude_politics" value="1" checked', page)
        self.assertNotIn('name="exclude_sports" value="1" checked', page)

    def test_stored_parameters_from_an_older_screener_still_load(self):
        farm_db.put_scan_params(self.db, {"min_fav": 0.92, "capital": 300.0, "bid_offset": 0.01,
                                          "fav_target_score": 0.45, "depth_haircut": 0.5,
                                          "fill_lookback_days": 30.0,
                                          "sweep_prior_weight": 0.25, "stuck_share": 0.25,
                                          "excluded_categories": ["politics", "golf"]}, self.now)
        resp = self.client.get("/discover")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="exclude_politics" value="1" checked', resp.text)
        self.assertNotIn('name="exclude_sports" value="1" checked', resp.text)
        self.assertNotIn("golf", resp.text)
        self.assertIn('name="capital" value="300"', resp.text)
        self.assertIn('name="hurdle_rate" value="0.4"', resp.text)
        self.assertIn('name="max_lead" value="0.005"', resp.text)
        self.assertIn('name="fragile_drop" value="0.03"', resp.text)
        self.assertIn('name="naked_budget" value="10"', resp.text)
        for retired in ("bid_offset", "fav_target_score", "depth_haircut",
                        "fill_lookback_days", "sweep_prior_weight", "stuck_share"):
            self.assertNotIn(retired, resp.text)


class DiscoverTest(Writes):
    def setUp(self):
        super().setUp()
        rows = []
        for i in range(120):
            book = BUDGET_BOOK if i % 3 == 0 else FLOOR_BOOK
            question = "Will 50% of x happen?" if i == 7 else f"Market number {i}?"
            rows.append(assessed_row(f"0x{i:04d}", f"m-{i}", book, self.now, 1000.0 - i,
                                     question, days=40 if i % 10 == 0 else 200))
        farm_db.store_scan(self.db, rows, [r.condition_id for r in rows]
                           + [A, B, C, "0xddd", "0xeee", "0xfff", "0x777", "0x888"], self.now)

    def ids(self, query=""):
        return re.findall(r'<a href="/review/(0x\w+)(?:\?mode=\w+)?">',
                          self.client.get("/discover" + query).text)

    def test_candidates_by_rank_fifty_per_page(self):
        first, third = self.ids(), self.ids("?page=3")
        self.assertEqual(first, [f"0x{i:04d}" for i in range(50)])
        self.assertEqual(third, [f"0x{i:04d}" for i in range(100, 120)]
                         + ["0xddd", "0xeee", "0xfff"])
        self.assertIn("page 3 of 3", self.client.get("/discover?page=3").text)

    def test_mode_filter_and_paging_links_keep_the_filters(self):
        # The same book 200 days out plans its longshot bid at 1c, which
        # offers under 5% a year: the free zone, labelled floor.
        budget = self.ids("?mode=budget")
        self.assertEqual(budget, [f"0x{i:04d}" for i in range(0, 120, 30)] + ["0xddd"])
        floor = self.ids("?mode=floor&page=2")
        self.assertEqual(floor[0], "0x0052")
        self.assertIn('href="/discover?mode=floor&amp;status=candidate&amp;page=2">Next',
                      self.client.get("/discover?mode=floor").text)
        self.assertIn('<a class="chip" href="/review/0x0000?mode=budget">Review</a>',
                      self.client.get("/discover?mode=budget").text)

    def test_sole_candidates_list_only_under_the_sole_filter(self):
        self.assertEqual(self.ids("?mode=sole"), ["0x777", "0x888"])
        for query in ("?page=3", "?q=golf", "?status=all&q=golf"):
            with self.subTest(query=query):
                self.assertNotIn("0x777", self.ids(query))
        page = self.client.get("/discover").text
        self.assertIn("123 markets", page)
        self.assertIn('status=candidate">Sole&nbsp;<span class="mono">2</span></a>', page)
        listed = self.client.get("/discover?mode=sole").text
        self.assertIn('<span class="badge t-c">sole</span>', listed)
        self.assertNotIn("None", listed)
        # Decided, it lists with its status like any market.
        farm_db.set_approval(self.db, "0x777", 0.99, 10.0, None, now=self.now)
        self.assertIn("0x777", self.ids("?status=approved"))
        self.assertEqual(self.ids("?mode=sole"), ["0x888"])
        self.assertEqual(self.ids("?mode=sole&status=all"), ["0x777", "0x888"])

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
        farm_db.put_setting(self.db, "max_lead", 0.015, self.now)
        page = self.client.get("/settings").text
        self.assertRegex(page, r'name="max_lead" value="0.015"[^>]*>\s*<button[^>]*'
                               r'formaction="/settings/reset/max_lead"')
        self.assertRegex(page, r'(?s)name="min_fav" value="0.9".*?farmer&#39;s value')
        self.assertRegex(page, r'(?s)name="cooldown_s" value="600".*?>default<')
        self.assertNotIn("Global caps", page)
        self.assertNotIn("stuck_share", page)

    def save(self, group, **changes):
        page = self.client.get("/settings").text
        values = dict(re.findall(r'name="(\w+)" value="([^"]*)" inputmode', page))
        keys = [k for k, f in forms.SETTING_FIELDS.items() if f.group == group]
        return self.post(f"/settings/group/{group}", **{**{k: values[k] for k in keys},
                                                         **changes})

    def test_saving_writes_only_the_changed_fields(self):
        resp = self.save("Quoting", max_lead="0.02")
        self.assertEqual((resp.status_code, resp.headers["location"]),
                         (303, "/settings?saved=Quoting"))
        self.assertEqual(farm_db.load_settings(self.db), ({"max_lead": 0.02}, {}))
        self.assertIn("Saved Quoting", self.client.get("/settings?saved=Quoting").text)

    def test_invalid_values_show_inline_and_write_nothing(self):
        resp = self.save("Quoting", max_lead="0.9", keep_ratio="abc", capital="300")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("max_lead: 0.9 is out of range", resp.text)
        self.assertIn("keep_ratio: &#39;abc&#39; is not JSON", resp.text)
        self.assertIn('name="capital" value="300"', resp.text)
        self.assertEqual(farm_db.load_settings(self.db), ({}, {}))

    def test_the_bid_multiple_saves_with_quoting_and_the_tie_band_is_not_shown(self):
        page = self.client.get("/settings").text
        self.assertRegex(page, r'(?s)Bid multiple of capital.*?name="bid_multiple" value="4"')
        self.assertNotIn("tie_band", page)
        resp = self.save("Quoting", bid_multiple="21")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("bid_multiple: 21 is out of range", resp.text)
        self.save("Quoting", bid_multiple="2")
        self.assertEqual(farm_db.load_settings(self.db), ({"bid_multiple": 2.0}, {}))

    def test_the_tail_cap_saves_with_quoting(self):
        page = self.client.get("/settings").text
        self.assertRegex(page, r'(?s)Tail cap \(days of reward\).*?name="tail_days" value="30"')
        resp = self.save("Quoting", tail_days="-1")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("tail_days: -1 is out of range", resp.text)
        self.save("Quoting", tail_days="0")
        self.assertEqual(farm_db.load_settings(self.db), ({"tail_days": 0.0}, {}))

    def test_reset_deletes_the_override(self):
        farm_db.put_setting(self.db, "cooldown_s", 30, self.now)
        resp = self.post("/settings/reset/cooldown_s")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(farm_db.load_settings(self.db), ({}, {}))

    def test_stop_sends_a_command_and_shows_when_it_was_consumed(self):
        page = self.client.get("/settings").text
        self.assertIn('hx-confirm="Stop the farmer?', page)
        self.assertIn("python -m farm.reward_farmer --live --db", page)
        text = self.post("/farmer/stop").text
        command = farm_db.latest_command(self.db, CommandKind.STOP)
        self.assertIsNone(command.consumed_at)
        self.assertIn(f"Stop command {command.id} sent", text)
        self.assertIn("is waiting for the farmer", text)
        farm_db.consume_command(self.db, command.id, "stopping", self.now)
        text = self.client.get("/partials/settings/farmer").text
        self.assertIn("was consumed at", text)
        self.assertIn(": stopping.", text)

    def test_stop_keeping_orders_sends_the_payload_a_plain_stop_leaves_out(self):
        page = self.client.get("/settings").text
        self.assertIn('hx-post="/farmer/stop" hx-vals=\'{"keep_orders": "1"}\'', page)
        self.assertIn('hx-confirm="Stop the farmer and keep its orders?', page)
        self.post("/farmer/stop")
        self.post("/farmer/stop", keep_orders="1")
        self.assertEqual([c.payload for c in farm_db.pending_commands(self.db)],
                         [None, {"keep_orders": True}])

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
        # The count is the queue's: a candidate whose rewards ended has left it.
        farm_db.set_rewards_active(self.db, "0xfff", False)
        self.assertIn('Review queue <span class="badge t-n count">2</span>',
                      self.client.get("/").text)

    def test_alerts_link_to_their_actions(self):
        self.publish(stopped=self.now)
        alerts = self.client.get("/partials/overview/needs").text
        self.assertIn('<a href="/settings">Farmer status</a>', alerts)
        farm_db.set_approval(self.db, A, 0.99, 0.5, None, now=self.now)
        # A longshot bid of 25 shares at 2c uses the whole $0.50.
        above = state(A, slots=quoting_slots(long=(0.02, 25.0)))
        self.publish(states=[above._replace(updated_at=self.now)])
        alerts = self.client.get("/partials/overview/needs").text
        self.assertIn('<a href="/markets?flag=budget-bound">1 budget bound</a>', alerts)


def settle_triage():
    for thread in threading.enumerate():
        if thread.name == "console-triage":
            thread.join(10)


# A judge answer that quotes FakeRemote's rules, so every run counts.
DELTA_PATH = {"path": "delta happens", "rule_quote": "Yes if delta happens", "realistic": False}


class FakeClaude:
    """run_triage: answers each call once gate is set; output, when set,
    replaces every answer (an exception is raised)."""

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.output = None
        self._lock = threading.Lock()

    def __call__(self, step, prompt):
        with self._lock:
            self.calls.append((step.name, prompt))
        self.gate.wait(5)
        if isinstance(self.output, Exception):
            raise self.output
        return envelope(brief_output() if step is triage.BRIEF
                        else judge_output(paths=(DELTA_PATH,)))

    def briefed(self):
        """The questions each brief call listed."""
        return [re.findall(r"^\d+\. (.+\?)$", prompt, re.MULTILINE)
                for step, prompt in self.calls if step == "brief"]

    def questions(self):
        return {re.search(r"Question: (.+)", prompt).group(1)
                for step, prompt in self.calls if step == "judge"}


def yes_path(quote_ok=True, realistic=False):
    return triage.YesPath("The President says so", "a statement by the President counts",
                          realistic, quote_ok)


def judged(answer="near_impossible", settled=True, path=None, reason="Absurd."):
    return triage.JudgeRun(answer, settled, (path or yes_path(),), reason)


NO_NEWS = triage.Brief(("aliens confirmed 2026", "UAP hearing"), "No confirmation is scheduled.",
                       ())


def folded(panel):
    """The triage panel's summary, and what its collapsed details hold."""
    summary, _, rest = panel.partition("<details>")
    return summary, rest.partition("</details>")[0]


class TriageTest(Writes):
    def app_kwargs(self):
        self.claude, self.remote = FakeClaude(), FakeRemote()
        self.addCleanup(settle_triage)
        market = RewardMarket("0xnew", "novel", "Novel market?", self.now + timedelta(days=100),
                              False, 0.001, "tok-new", "No", 50.0, 0.035, 20.0)
        self.scan = FakeScan(self.now, [reward_screener.assess(
            market, FLOOR_BOOK, ScreenConfig(), self.now, fill_cost.DEFAULT_PRIOR)])
        self.scan.release.set()
        return {"run_triage": self.claude, "fetch_rules": self.remote.rules, "scan": self.scan,
                "fetch_page": lambda url: "Delta happened."}

    def panel(self, cid="0xddd"):
        return self.client.get(f"/partials/review/{cid}/triage").text

    def store(self, cid, screen=None):
        screen = screen or triage.decide((judged(),) * 3, NO_NEWS, 100)
        farm_db.store_triage(self.db, cid, self.now, "sonnet", result=triage.to_result(screen),
                             book_mid=0.96, duration_s=54.0, cost_usd=0.16)

    def test_review_page_loads_the_triage_panel(self):
        page = self.client.get("/review/0xddd").text
        self.assertIn('hx-get="/partials/review/0xddd/triage" hx-trigger="load"', page)
        self.assertNotIn("Claude triage (planned)", page)
        text = self.panel()
        for needle in ("Not screened yet. Claude searches the news once for this market's event, "
                       "then judges three times", "ends within 45 days", ">Ask Claude</button>"):
            self.assertIn(needle, text)
        self.assertEqual(self.claude.calls, [])

    def test_a_near_impossible_row_sums_up_and_folds_the_runs_and_searches_away(self):
        self.store("0xddd", triage.decide(
            tuple(judged(reason=f"Reason {i}.") for i in (1, 2, 3)), NO_NEWS, 100))
        text = self.panel()
        summary, details = folded(text)
        for needle in ('<span class="badge t-q">Near impossible</span> 3/3 runs found YES near '
                       "impossible given the news search.", "No confirmation is scheduled.",
                       "The news search reported no facts.",
                       "No run found a realistic way to YES; the ways they listed are in the "
                       "details.",
                       "Run agreement: near impossible 3 of 3"):
            self.assertIn(needle, summary)
        for needle in ("Run 1", "Run 3", "Reason 2.", '"a statement by the President counts"',
                       "Searched: aliens confirmed 2026; UAP hearing"):
            self.assertIn(needle, details)
            self.assertNotIn(needle, summary)
        for needle in ("sonnet ·", "took 54s", "$0.16", ">Rerun</button>"):
            self.assertIn(needle, text)
        for absent in ("Needs human", "not in the rules", "realistic</span>", "p_true",
                       'class="ev', "prompt v", "Stale", "every 3s", "toward YES"):
            self.assertNotIn(absent, text)

    def test_a_quiet_row_says_when_the_market_ends_and_lists_a_fact_pointing_away(self):
        runs = (judged("possible", path=yes_path(realistic=True), reason="Plausible."),
                judged("possible", path=yes_path(False, realistic=True), reason="Plausible."),
                judged("possible", path=triage.YesPath("A one-run path", "", False, True),
                       reason="Plausible."))
        away = triage.Fact("The hearing was cancelled", "q", "https://example.org/a", None,
                           False, True)
        self.store("0xddd", triage.decide(runs, NO_NEWS._replace(facts=(away,)), 30))
        summary, details = folded(self.panel())
        self.assertIn('<li>The President says so <span class="muted">2 of 3 runs</span> '
                      '<span class="c-warn">quote not in the rules</span></li>', summary)
        self.assertNotIn("A one-run path", summary)
        self.assertIn("A one-run path", details)
        for needle in ('<span class="badge t-f">Quiet</span> nothing in the news search points '
                       "to YES, ends in 40 days.", "<li>The hearing was cancelled",
                       "Run agreement: possible 3 of 3"):
            self.assertIn(needle, summary)
        for absent in ("Near impossible", "Needs human", "toward YES"):
            self.assertNotIn(absent, summary + details)

    def test_facts_show_newest_first_with_their_source_quote_check_and_those_toward_yes(self):
        def fact(text, url, verified, toward_yes, date):
            return triage.Fact(text, "q", url, date, toward_yes, verified)
        brief = triage.Brief(("s",), "A hearing is set for June.", (
            fact("Found one", "https://example.org/a?x=1", True, True, "2026-05-01"),
            fact("Missed one", "https://example.org/b", False, False, "2026-06-01"),
            fact("Unread one", "javascript:alert(1)", None, True, None)))
        self.store("0xddd", triage.decide((judged("possible"),) * 3, brief, 100))
        text = self.panel()
        summary, details = folded(text)
        for needle in ('<span class="badge t-n">Needs human</span> 2 news facts point toward '
                       "YES.", "A hearing is set for June.",
                       "<li>0 of 3 runs near impossible</li>",
                       "<li>ends in 100 days, past the 45-day quiet window</li>",
                       '<li><span class="badge t-c">toward YES</span> Unread one <span '
                       "class=\"muted\">couldn't check the quote</span></li>",
                       '<span class="c-warn">quote not on the page</span>'):
            self.assertIn(needle, summary)
        self.assertEqual(
            re.findall(r'<li><span class="muted mono">([-\d]+)</span> (?:<span class="badge '
                       r't-c">(toward YES)</span> )?(\w+ \w+)', summary),
            [("2026-06-01", "", "Missed one"), ("2026-05-01", "toward YES", "Found one")])
        self.assertRegex(summary, r'Found one\s*<a href="https://example.org/a\?x=1" '
                                  r'target="_blank"[^>]*>source ↗</a>\s*<span class="c-good">'
                                  r"quote found on the page</span></li>")
        self.assertEqual(summary.count('<span class="badge t-c">toward YES</span>'), 2)
        self.assertNotIn("recent news:", text)
        self.assertNotIn("javascript:", text)
        self.assertIn("Searched: s", details)

    def test_split_runs_show_why_and_a_quote_not_in_the_rules(self):
        runs = (judged(reason="r"), judged(path=yes_path(False), reason="r"),
                judged("unsure", False, reason="Can't tell."))
        self.store("0xddd", triage.decide(runs, NO_NEWS, 100))
        summary, details = folded(self.panel())
        for needle in ('<span class="badge t-n">Needs human</span> 1 of 3 runs near '
                       "impossible.", "<li>run 2 quoted words not in the rules</li>",
                       f"<li>{triage.NEWS_CANT_SETTLE}</li>".replace("'", "&#39;"),
                       "<li>ends in 100 days, past the 45-day quiet window</li>",
                       "No run found a realistic way to YES; the ways they listed are in the "
                       "details.", "Run agreement: near impossible 2 of 3, unsure 1"):
            self.assertIn(needle, summary)
        self.assertNotIn("<li>1 of 3 runs", summary)
        for needle in ('<span class="c-warn">not in the rules</span>', "news can't settle it",
                       "Can&#39;t tell."):
            self.assertIn(needle, details)

    def test_blank_rules_need_a_human_with_nothing_to_sum_up(self):
        self.store("0xddd", triage.RULES_UNAVAILABLE)
        text = self.panel()
        self.assertIn('<span class="badge t-n">Needs human</span> rules unavailable.', text)
        for absent in ("Ways to YES", "Run agreement", "What's happened", "What&#39;s happened",
                       "news search", "Searched", "<details>"):
            self.assertNotIn(absent, text)

    def test_post_queues_once_polls_until_done_and_stores_the_screen(self):
        self.claude.gate.clear()
        first = self.post("/review/0xddd/triage")
        self.assertEqual(first.status_code, 200)
        self.assertIn('hx-trigger="every 3s"', first.text)
        self.post("/review/0xddd/triage")
        self.assertIn("every 3s", self.panel())
        self.claude.gate.set()
        settle_triage()
        text = self.panel()
        self.assertNotIn("every 3s", text)
        self.assertIn("Near impossible", text)
        self.assertEqual(sorted(step for step, _ in self.claude.calls),
                         ["brief"] + ["judge"] * 3)
        self.assertIn("Resolves to Yes if delta happens.", self.claude.calls[0][1])
        row = farm_db.latest_triage(self.db, "0xddd")
        self.assertEqual((row.status, row.verdict, row.book_mid),
                         ("ok", "near_impossible",
                          json.loads(self.row("0xddd")["metrics"])["book_mid"]))
        self.assertEqual(self.post("/review/0xnone/triage").status_code, 404)

    def test_a_failed_screen_shows_the_error_and_a_retry(self):
        self.claude.output = triage.TriageError("claude (brief) did not finish within 900s")
        self.post("/review/0xddd/triage")
        settle_triage()
        text = self.panel()
        self.assertIn("Claude's screen failed: TriageError: claude (brief) did not finish "
                      "within 900s", text)
        self.assertIn(">Retry</button>", text)
        self.assertEqual(farm_db.latest_triage(self.db, "0xddd").status, "error")

    def test_batch_sends_only_queued_candidates_without_an_ok_row(self):
        self.store("0xddd")
        farm_db.store_triage(self.db, "0xfff", self.now, "sonnet", error="boom")
        self.assertEqual(self.post("/triage/batch", n="0").status_code, 422)
        resp = self.post("/triage/batch", n="5")
        self.assertEqual((resp.status_code, resp.headers["location"]),
                         (303, "/discover?triaged=2"))
        settle_triage()
        self.assertEqual(self.claude.questions(), {"echo?", "foxtrot?"})
        page = self.client.get(resp.headers["location"]).text
        self.assertIn("Sent 2 candidates to Claude.", page)
        self.assertIn("3 of 3 queued candidates screened.", page)
        self.assertNotIn("prompt v", page)
        self.assertEqual(self.post("/triage/batch").headers["location"], "/discover?triaged=0")

    def briefs_asked(self, n):
        for _ in range(200):
            if len(self.claude.briefed()) == n:
                return
            time.sleep(0.02)
        self.fail(f"Claude was asked for {len(self.claude.briefed())} briefs, not {n}")

    def test_a_worker_takes_the_queued_markets_of_one_event_together(self):
        french = {"event_slug": FR, "event_title": "2027 French presidential election"}
        farm_db.store_scan(self.db, [
            assessed_row("0xddd", "delta", BUDGET_BOOK, self.now, 30.0, days=40, **french),
            assessed_row("0xeee", "echo", FLOOR_BOOK, self.now, 20.0, **french),
        ], [A, B, C, "0xddd", "0xeee", "0xfff"], self.now)
        runner = self.app.state.triage
        self.claude.gate.clear()
        self.post("/triage/batch", n="5")
        self.briefs_asked(2)
        self.assertEqual((runner.running(), runner.pending()),
                         ({"0xddd", "0xeee", "0xfff"}, frozenset()))
        self.assertIn("every 3s", self.panel("0xeee"))
        self.assertIn("Claude is searching the news", self.panel("0xeee"))
        self.claude.gate.set()
        settle_triage()
        self.assertEqual(sorted(self.claude.briefed()), [["delta?", "echo?"], ["foxtrot?"]])
        self.assertIn("Event: 2027 French presidential election",
                      "".join(p for step, p in self.claude.calls if step == "brief"))
        self.assertEqual(runner.running(), frozenset())
        self.assertEqual(farm_db.triaged_ids(self.db), {"0xddd", "0xeee", "0xfff"})

    def test_a_batch_that_blows_up_leaves_nothing_running_and_the_runner_working(self):
        runner = self.app.state.triage
        with mock.patch.object(triage, "screen_and_store", side_effect=RuntimeError("boom")):
            self.post("/triage/batch", n="5")
            settle_triage()
        self.assertEqual((runner.running(), runner.pending()), (frozenset(), frozenset()))
        self.assertIsNone(farm_db.latest_triage(self.db, "0xddd"))
        self.post("/review/0xddd/triage")
        settle_triage()
        self.assertEqual(farm_db.latest_triage(self.db, "0xddd").verdict, "near_impossible")

    def scanned(self):
        for _ in range(200):
            if "Done" in self.client.get("/partials/discover/scan").text:
                return
            time.sleep(0.02)
        self.fail("the scan never finished")

    def test_a_scan_screens_its_top_new_candidates_only_when_turned_on(self):
        self.assertIn('name="auto_top_n" value="0"', self.client.get("/settings").text)
        self.assertEqual(self.post("/settings/triage", auto_top_n="-1").status_code, 422)
        self.post("/discover/scan", **scan_form())
        self.scanned()
        settle_triage()
        self.assertEqual(self.claude.calls, [])
        resp = self.post("/settings/triage", auto_top_n="1")
        self.assertIn("top 1 new candidates", self.client.get(resp.headers["location"]).text)
        self.assertEqual(farm_db.load_settings(self.db), ({}, {}))
        self.post("/discover/scan", **scan_form())
        self.scanned()
        settle_triage()
        self.assertEqual(self.claude.questions(), {"Novel market?"})


if __name__ == "__main__":
    unittest.main()
