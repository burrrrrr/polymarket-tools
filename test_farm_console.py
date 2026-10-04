import json
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import farm_db
from farm_console import charts, remote, views
from farm_console.app import create_app
from farm_db import Event, EventKind, FarmerStatus, FarmState, MarketRecord, MidSample

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
ORIGIN = "http://127.0.0.1:8787"
A, B, C = "0xaaa", "0xbbb", "0xccc"
CONFIG = {"expiry_buffer_days": 7.0, "min_fav": 0.9}


def metrics(slug, est=10.0, v=0.035, mode="FLOOR"):
    return {"est_daily_usd": est, "plan": {"mode": mode},
            "market": {"slug": slug, "question": f"{slug}?", "fav_outcome": "No", "v": v,
                       "min_size": 50.0, "tick": 0.001, "fav_token_id": "t",
                       "end_date": (NOW + timedelta(days=100)).isoformat(),
                       "rate_per_day": 100.0}}


def market(cid, slug, status="approved", budget=25.0, p_true=0.99, est=10.0, v=0.035,
           paused=False):
    return MarketRecord(
        condition_id=cid, slug=slug, question=f"{slug}?", status=status, p_true=p_true,
        naked_budget_usd=budget, note=None, end_date=NOW + timedelta(days=100),
        rewards_active=True, rank_score=1.0, metrics=metrics(slug, est, v), first_seen=NOW,
        last_scanned=NOW, baseline_fav=None, baseline_long=None, paused=paused,
        naked_paused=False, capital_usd=None,
    )


def slot(decision="hold", order=None, target=None, live_score=None, reason=""):
    return {"order": order and {"id": "o", "price": order[0], "size": order[1]},
            "target": target and {"price": target[0], "size": target[1], "score": target[2]},
            "live_score": live_score, "decision": decision, "reason": reason}


def quoting_slots(fav=(0.98, 100.0), long=(0.001, 500.0), fav_score=0.5):
    return {
        "FAV_BID": slot("hold", fav, (*fav, fav_score), fav_score),
        "LONG_BID": slot("hold", long, (*long, 0.4), 0.4),
        "FAV_ASK": slot("none", reason="no target"),
        "LONG_ASK": slot("none", reason="no target"),
    }


def state(cid, phase="QUOTING", reason="", until=None, mid=0.99, naked_used=0.5,
          slots=None, backoff=None, position=None):
    return FarmState(
        condition_id=cid, updated_at=NOW, phase=phase, phase_reason=reason,
        phase_until=until, book_mid=mid, mid=mid, q1=50.0, q2=50.0, capital_usd=500.0,
        naked_used_usd=naked_used,
        position=position or {"wallet": {"fav": 10.0, "long": 0.0},
                              "baseline": {"fav": 4.0, "long": 0.0},
                              "farm": {"fav": 6.0, "long": 0.0},
                              "avg_cost": {"fav": 0.95, "long": 0.0}},
        slots=quoting_slots() if slots is None else slots,
        book={"bids": [{"price": 0.98, "size": 200.0, "ours": 100.0},
                       {"price": 0.97, "size": 300.0, "ours": 0.0}],
              "asks": [{"price": 0.999, "size": 500.0, "ours": 500.0}]},
        naked_paused_until=None, backoff=backoff or {}, scoring_pct=None,
    )


def status(last_cycle=NOW, interval=5.0, stopped=None, mode="dry"):
    return FarmerStatus(mode, 1, NOW - timedelta(hours=1), last_cycle, interval, False, 1,
                        CONFIG, stopped)


def stored(id, kind, cid=None, slot_name=None, at=NOW, price=None, size=None, message="m"):
    return farm_db.StoredEvent(id, at, kind, message, cid, slot_name, price, size, None, None)


def snap(states=(), markets=(), fills=(), rewards=(), st=None, running=True):
    return views.Snapshot(
        status=st or (status() if running else None),
        states={s.condition_id: s for s in states},
        markets={m.condition_id: m for m in markets},
        fills=list(fills), rewards=list(rewards), counts={"approved": len(markets)},
    )


def kinds(s, now=NOW):
    return [a.kind for a in views.alerts(s, now)]


class AlertRuleTest(unittest.TestCase):
    def test_quiet_when_everything_quotes_inside_budget(self):
        self.assertEqual(kinds(snap([state(A)], [market(A, "a")])), [])

    def test_cooldown_names_reason_and_until(self):
        s = snap([state(A, "COOLDOWN", "ask drop", until=NOW + timedelta(minutes=10))],
                 [market(A, "a")])
        found = views.alerts(s, NOW)
        self.assertEqual([a.kind for a in found], ["Cooldown"])
        self.assertIn("ask drop until", found[0].text)
        self.assertEqual(found[0].href, f"/markets/{A}")

    def test_below_range_fires(self):
        self.assertEqual(kinds(snap([state(A, "BELOW_RANGE")], [market(A, "a")])),
                         ["Below range"])

    def test_paused_fires(self):
        self.assertEqual(kinds(snap([state(A, "PAUSED", "paused by operator", slots={})],
                                    [market(A, "a")])), ["Paused"])

    def test_budget_bound_at_99_percent_not_below(self):
        bound = snap([state(A, naked_used=24.75)], [market(A, "a", budget=25.0)])
        under = snap([state(A, naked_used=24.7)], [market(A, "a", budget=25.0)])
        self.assertEqual(kinds(bound), ["Budget bound"])
        self.assertEqual(kinds(under), [])

    def test_low_score_fav_bid_under_ten_percent(self):
        low = snap([state(A, slots=quoting_slots(fav_score=0.065))], [market(A, "a")])
        ok = snap([state(A, slots=quoting_slots(fav_score=0.10))], [market(A, "a")])
        self.assertEqual(kinds(low), ["Low score"])
        self.assertEqual(kinds(ok), [])

    def test_backoff_slot_fires_per_slot(self):
        s = snap([state(A, backoff={"FAV_BID": {"until": farm_db.stamp(NOW), "attempts": 2}})],
                 [market(A, "a")])
        found = views.alerts(s, NOW)
        self.assertEqual([a.kind for a in found], ["Backoff"])
        self.assertIn("FAV_BID was rejected 2 time(s)", found[0].text)

    def test_naked_fill_within_24h_only(self):
        recent = stored(1, EventKind.FILL, A, "LONG_BID", at=NOW - timedelta(hours=23), size=5)
        old = stored(2, EventKind.FILL, A, "LONG_BID", at=NOW - timedelta(hours=25), size=5)
        fav = stored(3, EventKind.FILL, A, "FAV_BID", at=NOW, size=5)
        self.assertEqual(kinds(snap([state(A)], [market(A, "a")], fills=[recent])),
                         ["Naked fill"])
        self.assertEqual(kinds(snap([state(A)], [market(A, "a")], fills=[old, fav])), [])

    def test_not_running_fires_without_a_market(self):
        found = views.alerts(snap(running=False), NOW)
        self.assertEqual([(a.kind, a.condition_id) for a in found], [("Not running", None)])


class LivenessTest(unittest.TestCase):
    def test_running_within_three_intervals(self):
        live = views.liveness(status(last_cycle=NOW - timedelta(seconds=15)), NOW)
        self.assertEqual((live.running, live.mode, live.message), (True, "DRY RUN", ""))

    def test_stale_after_three_intervals_says_how_long_ago(self):
        live = views.liveness(status(last_cycle=NOW - timedelta(seconds=16)), NOW)
        self.assertFalse(live.running)
        self.assertIn("last cycle was 16s ago", live.message)

    def test_stopped_is_not_running_even_when_recent(self):
        live = views.liveness(status(stopped=NOW - timedelta(seconds=2)), NOW)
        self.assertFalse(live.running)
        self.assertIn("stopped 2s ago", live.message)

    def test_live_mode_label(self):
        self.assertEqual(views.liveness(status(mode="live"), NOW).mode, "LIVE")

    def test_never_ran(self):
        self.assertFalse(views.liveness(None, NOW).running)


class TotalsTest(unittest.TestCase):
    def test_sums_over_resting_orders_quoting_budgets_and_farm_inventory(self):
        cancelled = quoting_slots()
        cancelled["FAV_BID"] = slot("cancel", (0.97, 1000.0), reason="COOLDOWN")
        s = snap(
            [state(A, slots=quoting_slots(fav=(0.98, 100.0), long=(0.001, 500.0))),
             state(B, slots=quoting_slots(fav=(0.90, 10.0), long=(0.02, 100.0))),
             state(C, "COOLDOWN", slots=cancelled)],
            [market(A, "a", budget=25, est=10), market(B, "b", budget=30, est=5),
             market(C, "c", budget=50, est=100)],
            rewards=[("2026-10-04", A, 1.5), ("2026-10-04", B, 0.5), ("2026-10-03", A, 9.0)],
        )
        t = views.totals(s, NOW)
        self.assertEqual((t.quoting, t.approved), (2, 3))
        self.assertAlmostEqual(t.fav_bid_usd, 0.98 * 100 + 0.90 * 10)
        self.assertAlmostEqual(t.naked_usd, 0.001 * 500 * 2 + 0.02 * 100)
        self.assertEqual(t.naked_budget_usd, 55)
        self.assertAlmostEqual(t.inventory_usd, 3 * 6.0 * 0.95)
        self.assertEqual(t.est_daily_usd, 15)
        self.assertEqual(t.earned_today_usd, 2.0)

    def test_place_counts_its_target_and_no_rewards_is_none(self):
        placing = quoting_slots()
        placing["FAV_BID"] = slot("place", None, (0.96, 50.0, 0.5))
        t = views.totals(snap([state(A, slots=placing)], [market(A, "a")]), NOW)
        self.assertAlmostEqual(t.fav_bid_usd, 48.0)
        self.assertIsNone(t.earned_today_usd)


class BandStripTest(unittest.TestCase):
    def test_marks_on_the_favorite_axis(self):
        band = views.band_strip(0.95, 0.04, 0.94, 0.03)
        self.assertEqual((band.lo, band.hi), (0.91, 0.99))
        self.assertEqual([(m.kind, m.price, m.left) for m in band.marks],
                         [("fav_bid", 0.94, 37.5), ("mid", 0.95, 50.0),
                          ("long_bid", 0.97, 75.0)])

    def test_axis_clamps_at_one(self):
        band = views.band_strip(0.98, 0.04, 0.97, 0.001)
        self.assertEqual((band.lo, band.hi), (0.94, 1.0))
        self.assertEqual([m.left for m in band.marks], [50.0, 66.67, 98.33])

    def test_off_band_order_pins_to_the_edge_and_missing_bids_are_unmarked(self):
        band = views.band_strip(0.95, 0.02, 0.90, None)
        self.assertEqual([(m.kind, m.left) for m in band.marks], [("fav_bid", 0.0), ("mid", 50.0)])
        self.assertIsNone(views.band_strip(None, 0.02, None, None))


class CardsTest(unittest.TestCase):
    def test_approved_market_without_farm_state_is_waiting(self):
        s = snap([state(A)], [market(A, "a"), market(B, "b"), market(C, "c", status="rejected")])
        cards = views.tracked(s, NOW)
        self.assertEqual([(c.condition_id, c.phase) for c in cards],
                         [(A, "QUOTING"), (B, "WAITING")])
        self.assertEqual(cards[1].phase_reason, "waiting for farmer")

    def test_phase_filter_and_cutoff(self):
        s = snap([state(A), state(B, "PAUSED", slots={})], [market(A, "a"), market(B, "b")])
        page = views.markets_page(s, "PAUSED", NOW)
        self.assertEqual([c.condition_id for c in page.cards], [B])
        self.assertEqual(page.cards[0].cutoff_in_s, timedelta(days=93).total_seconds())

    def test_card_band_uses_resting_bids(self):
        c = views.card(market(A, "a", v=0.04), state(A, mid=0.95), status(), NOW)
        self.assertEqual([m.kind for m in c.band.marks], ["fav_bid", "mid", "long_bid"])
        self.assertEqual((c.fav_bid.price, c.long_bid.price), (0.98, 0.001))


class DetailTest(unittest.TestCase):
    def test_book_rows_order_and_scores(self):
        rows = views.book_rows(state(A).book, 0.99, 0.035)
        self.assertEqual([(r.side, r.price) for r in rows],
                         [("ask", 0.999), ("mid", 0.99), ("bid", 0.98), ("bid", 0.97)])
        self.assertAlmostEqual(rows[2].score, ((0.035 - 0.01) / 0.035) ** 2)

    def test_fav_bid_fill_edge_is_p_true_minus_price(self):
        row = views.fill_row(stored(1, EventKind.FILL, A, "FAV_BID", price=0.96, size=10),
                             {A: market(A, "a", p_true=0.99)})
        self.assertAlmostEqual(row.edge, 0.03)
        long = views.fill_row(stored(2, EventKind.FILL, A, "LONG_BID", price=0.002, size=10),
                              {A: market(A, "a")})
        self.assertIsNone(long.edge)


def mid(minutes_ago, value=0.95, fav=0.94, long=0.02):
    return MidSample(A, NOW - timedelta(minutes=minutes_ago), value, value, fav, long)


class ChartsTest(unittest.TestCase):
    def test_empty_mid_chart_says_so(self):
        svg = charts.mid_chart([], 0.03, [], NOW)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("No midpoint history yet", svg)
        self.assertNotIn("polyline", svg)

    def test_samples_older_than_the_window_are_dropped(self):
        svg = charts.mid_chart([mid(60 * 25)], 0.03, [], NOW)
        self.assertIn("No midpoint history yet", svg)

    def test_mid_chart_draws_band_mid_bids_and_only_markers_in_range(self):
        samples = [mid(m) for m in (600, 300, 60, 1)]
        markers = [views.Marker(NOW - timedelta(minutes=120), "FILL", "FILL a FAV_BID"),
                   views.Marker(NOW - timedelta(minutes=30), "PHASE", "a: QUOTING -> COOLDOWN"),
                   views.Marker(NOW - timedelta(hours=30), "FILL", "too old")]
        svg = charts.mid_chart(samples, 0.03, markers, NOW)
        for part in ('class="band"', 'class="mid"', 'class="fav-bid"', 'class="long-bid"'):
            self.assertIn(part, svg)
        self.assertEqual(svg.count('class="marker marker-fill"'), 1)
        self.assertEqual(svg.count('class="marker marker-phase"'), 1)
        self.assertNotIn("too old", svg)
        width = 880
        xs = [float(x) for x in re.findall(r'<line x1="([\d.]+)" x2="[\d.]+" y1="12"', svg)]
        self.assertTrue(all(52 <= x <= width - 12 for x in xs), xs)
        self.assertIn(">98.0c<", svg)

    def test_a_none_bid_breaks_the_step_line(self):
        samples = [mid(30), mid(20, fav=None), mid(10)]
        svg = charts.mid_chart(samples, 0.03, [], NOW)
        fav = re.search(r'class="fav-bid" d="([^"]+)"', svg).group(1)
        self.assertEqual(fav.count("M"), 2)

    def test_rewards_bars_empty_and_stacked(self):
        today = NOW
        self.assertIn("No rewards recorded yet", charts.rewards_bars([], {}, today))
        rows = [("2026-10-04", A, 2.0), ("2026-10-04", B, 1.0), ("2026-10-01", A, 1.5),
                ("2026-08-01", A, 99.0)]
        svg = charts.rewards_bars(rows, {A: "a", B: "b"}, today)
        self.assertEqual(svg.count('class="reward-bar"'), 3)
        self.assertIn("$2.00", svg)
        self.assertNotIn("$99.00", svg)


class TotalRatesTest(unittest.TestCase):
    """The holder runs each refresh through spawn; the test runs it when it chooses."""

    def setUp(self):
        self.now = NOW
        self.answers = []
        self.calls = 0
        self.spawned = []
        self.rates = remote.TotalRates(self.fetch, timedelta(minutes=5), lambda: self.now,
                                       self.spawned.append)

    def fetch(self):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def land(self):
        while self.spawned:
            self.spawned.pop(0)()

    def test_none_until_the_first_fetch_lands_then_the_map(self):
        self.answers = [{"0xa": 1.0}]
        self.assertIsNone(self.rates.get())
        self.now = NOW + timedelta(minutes=6)
        self.assertIsNone(self.rates.get())
        self.assertEqual(len(self.spawned), 1)
        self.land()
        self.assertEqual(self.rates.get(), {"0xa": 1.0})

    def test_no_refetch_within_ttl(self):
        self.answers = [{"0xa": 1.0}, {"0xb": 2.0}]
        self.rates.get()
        self.land()
        self.now = NOW + timedelta(minutes=4)
        self.rates.get()
        self.land()
        self.assertEqual((self.calls, self.rates.get()), (1, {"0xa": 1.0}))
        self.now = NOW + timedelta(minutes=5)
        self.rates.get()
        self.land()
        self.assertEqual((self.calls, self.rates.get()), (2, {"0xb": 2.0}))

    def test_a_failed_or_empty_refresh_keeps_the_old_map(self):
        self.answers = [{"0xa": 1.0}, ConnectionError("clob down"), {}]
        self.rates.refresh()
        for minutes in (5, 10):
            self.now = NOW + timedelta(minutes=minutes)
            self.rates.get()
            self.land()
            self.assertEqual(self.rates.get(), {"0xa": 1.0})
        self.assertEqual(self.calls, 3)


class Console(unittest.TestCase):
    """The app against a scratch farm.db seeded through farm_db."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "farm.db")
        self.db = farm_db.open_db(self.path)
        self.addCleanup(self.db.close)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        farm_db.store_scan(self.db, [
            farm_db.ScanRow(cid, slug, f"{slug}?", (self.now + timedelta(days=100)).isoformat(),
                            1.0, json.dumps(metrics(slug)))
            for cid, slug in ((A, "alpha"), (B, "bravo"), (C, "charlie"))
        ], [A, B, C], self.now)
        for cid in (A, B):
            farm_db.approve_market(self.db, cid, 0.99, 25.0)
        self.publish(events=[
            Event(self.now, EventKind.START, "Reward farmer: DRY RUN"),
            Event(self.now, EventKind.PLACE, "PLACE FAV_BID 98.0c alpha", A, "FAV_BID", 0.98, 100.0, 0.5),
            Event(self.now, EventKind.FILL, "FILL alpha FAV_BID 5 shares @ 98.0c", A, "FAV_BID", 0.98, 5.0),
        ], mids=[MidSample(A, self.now - timedelta(minutes=m), 0.99, 0.99, 0.98, 0.001)
                 for m in (30, 20, 10)])
        self.app = create_app(self.path, **self.app_kwargs())
        self.client = TestClient(self.app, base_url=ORIGIN)

    def app_kwargs(self):
        return {}

    def publish(self, events=(), mids=(), stopped=None, states=None, last_cycle=None,
                conn=None):
        st = FarmerStatus("dry", 1, self.now, last_cycle or self.now, 5.0, False, 1, CONFIG,
                          stopped)
        farm_db.publish(conn or self.db, st, [state(A)._replace(updated_at=self.now)]
                        if states is None else states, list(events), list(mids))


class RoutesTest(Console):
    PAGES = {
        "/": ["Needs attention", "Tracked markets", "alpha?", "PLACE FAV_BID 98.0c alpha"],
        "/markets": ["alpha?", "bravo?", "WAITING", f'hx-post="/markets/{A}/paused"',
                     'class="mk fav_bid"'],
        "/markets?phase=WAITING": ["bravo?"],
        f"/markets/{A}": ["alpha?", "Order slots", "Book inside the band", "Lifecycle",
                          'class="fav-bid"', "FILL alpha FAV_BID"],
        f"/markets/{B}": ["bravo?", "waiting for the farmer"],
        "/activity": ["Daily rewards by market", "No rewards recorded yet"],
        "/activity?tab=fills": ["FAV_BID", "approximate", "1.0c"],
        "/activity?tab=inventory": ["alpha?", "6 / 0"],
        "/activity?tab=log": ['<span class="kind">PLACE</span><span class="c-fav">FAV_BID 98.0c alpha', "Reward farmer: DRY RUN"],
        "/activity?tab=log&kind=FILL": ["alpha FAV_BID 5 shares"],
        "/partials/overview/kpis": ["Capital in bids", "$98.00"],
        "/partials/overview/alerts": ["Needs attention"],
        "/partials/overview/tracked": ["alpha?"],
        "/partials/overview/events": ["PLACE FAV_BID 98.0c alpha"],
        "/partials/chrome/status": ["DRY RUN", "cycle 5s"],
        "/partials/chrome/banner": [],
        "/partials/markets": ["alpha?", "bravo?"],
        f"/partials/markets/{A}/summary": ["Mid (effective)", "Expiry cutoff"],
        f"/partials/markets/{A}/live": ["Order slots", "Book inside the band"],
        f"/partials/markets/{A}/chart": ["<svg"],
        f"/partials/markets/{A}/history": ["Lifecycle", "Fills"],
        "/partials/activity?tab=rewards": ["Screener estimate"],
        "/partials/activity?tab=fills": ["FAV_BID"],
        "/partials/activity?tab=inventory": ["alpha?"],
        "/partials/activity?tab=log": ["PLACE"],
    }

    def test_every_page_and_partial_renders_its_content(self):
        for url, needles in self.PAGES.items():
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                for needle in needles:
                    self.assertIn(needle, resp.text)

    def test_log_kind_filter_excludes_other_kinds(self):
        text = self.client.get("/activity?tab=log&kind=FILL").text
        self.assertNotIn("FAV_BID 98.0c alpha", text)

    def test_unknown_market_region_and_phase_are_refused(self):
        self.assertEqual(self.client.get("/markets/0xnope").status_code, 404)
        self.assertEqual(self.client.get("/partials/overview/nope").status_code, 404)
        self.assertEqual(self.client.get("/markets?phase=NOPE").status_code, 400)

    def test_pages_carry_live_regions_but_no_banner_while_running(self):
        text = self.client.get("/").text
        self.assertIn('sse-connect="/stream"', text)
        self.assertIn('hx-get="/partials/overview/kpis" hx-trigger="sse:cycle"', text)
        self.assertIn('hx-get="/partials/overview/events" hx-trigger="sse:farm-event"', text)
        self.assertNotIn("Not running", text)

    def test_stopped_farmer_shows_the_banner_on_every_page(self):
        self.publish(stopped=self.now)
        for url in ("/", "/markets", f"/markets/{A}", "/activity", "/partials/chrome/banner"):
            with self.subTest(url=url):
                self.assertIn("The farmer is not running. It stopped", self.client.get(url).text)
        self.assertIn("figures are from the farmer's last cycle",
                      self.client.get("/partials/overview/kpis").text)

    def test_paused_market_shows_alert_and_badge(self):
        self.publish(states=[state(A, "PAUSED", "paused by operator", slots={})
                             ._replace(updated_at=self.now)])
        self.assertIn("paused by operator", self.client.get("/partials/overview/alerts").text)
        self.assertIn('<span class="badge t-n">PAUSED<button type="button" class="tip"',
                      self.client.get("/partials/markets").text)


class ApiTest(Console):
    def test_overview_json(self):
        body = self.client.get("/api/overview").json()
        self.assertEqual(body["chrome"]["liveness"]["mode"], "DRY RUN")
        self.assertEqual(body["overview"]["totals"]["quoting"], 1)
        self.assertEqual(body["overview"]["totals"]["fav_bid_usd"], 98.0)
        self.assertEqual([m["phase"] for m in body["overview"]["markets"]], ["QUOTING", "WAITING"])

    def test_markets_json_and_detail(self):
        cards = self.client.get("/api/markets").json()["cards"]
        self.assertEqual(cards[0]["band"]["marks"][1]["kind"], "mid")
        detail = self.client.get(f"/api/markets/{A}").json()
        self.assertEqual(len(detail["mids"]), 3)
        self.assertEqual(detail["mids"][0]["fav_bid"], 0.98)
        self.assertEqual([f["slot"] for f in detail["fills"]], ["FAV_BID"])

    def test_events_paging(self):
        newest = self.client.get("/api/events?limit=2").json()
        self.assertEqual([e["kind"] for e in newest], ["FILL", "PLACE"])
        older = self.client.get(f"/api/events?before={newest[-1]['id']}").json()
        self.assertEqual([e["kind"] for e in older], ["START"])
        after = self.client.get(f"/api/events?after={older[0]['id']}&condition_id={A}").json()
        self.assertEqual([e["kind"] for e in after], ["FILL", "PLACE"])

    def test_activity_json(self):
        body = self.client.get("/api/activity?tab=fills").json()
        self.assertEqual(body["activity"][0]["edge"], 0.99 - 0.98)


class StreamTest(Console):
    def read_stream(self, poll_s, polls, during=lambda: None):
        client = TestClient(create_app(self.path, poll_s=poll_s, max_polls=polls),
                            base_url=ORIGIN)
        worker = threading.Thread(target=during)
        worker.start()
        text = client.get("/stream").text
        worker.join()
        return text

    def test_cycle_and_farm_event_after_the_farmer_publishes(self):
        def farmer_cycles():
            time.sleep(0.15)
            conn = farm_db.open_db(self.path)
            self.publish(last_cycle=self.now + timedelta(seconds=5),
                         events=[Event(self.now, EventKind.CANCEL, "CANCEL", A)], conn=conn)
            conn.close()

        text = self.read_stream(0.05, 10, farmer_cycles)
        self.assertIn(f"event: cycle\ndata: {farm_db.stamp(self.now + timedelta(seconds=5))}",
                      text)
        self.assertIn("event: farm-event\n", text)

    def test_quiet_farmer_sends_no_signals(self):
        text = self.read_stream(0.02, 5)
        self.assertNotIn("event:", text)


if __name__ == "__main__":
    unittest.main()
