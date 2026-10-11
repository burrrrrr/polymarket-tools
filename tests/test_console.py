import contextlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from farm import farm_db, fill_cost
from farm.farm_db import Event, EventKind, FarmerStatus, FarmState, MarketRecord, MidSample
from console import charts, remote, views
from console.app import create_app

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
ORIGIN = "http://127.0.0.1:8787"
A, B, C = "0xaaa", "0xbbb", "0xccc"
CONFIG = {"expiry_buffer_days": 7.0, "min_fav": 0.9}
HISTORY = fill_cost.History((9, 7, 5, 4, 3, 2, 1, 0, 0, 0, 0, 0), 60.0, NOW, 0.90, 2, 10.0)
ECONOMICS = {"net_per_day": 4.2, "reward_per_day": 5.0, "fills_per_day": 8.0,
             "loss_per_share": 0.1}


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


def quoting_slots(fav=(0.98, 100.0), long=(0.001, 500.0), fav_score=0.5, free=False,
                  offers=None):
    """free and offers are the farmer's word on the longshot bid: whether the
    naked budget leaves it alone, and the annual return a fill gives the
    favorite buyer."""
    return {
        "FAV_BID": slot("hold", fav, (*fav, fav_score), fav_score),
        "LONG_BID": {**slot("hold", long, (*long, 0.4), 0.4), "budget_free": free,
                     "fill_return": offers},
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


def holding_long(shares, avg):
    """A farm_state position with these longshot shares above the baseline."""
    return {"wallet": {"fav": 0.0, "long": shares}, "baseline": {"fav": 0.0, "long": 0.0},
            "farm": {"fav": 0.0, "long": shares}, "avg_cost": {"fav": 0.0, "long": avg}}


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
        # A longshot bid at 2c: 1,237.5 shares are $24.75 and 1,235 are $24.70.
        bound = snap([state(A, slots=quoting_slots(long=(0.02, 1237.5)))],
                     [market(A, "a", budget=25.0)])
        under = snap([state(A, slots=quoting_slots(long=(0.02, 1235.0)))],
                     [market(A, "a", budget=25.0)])
        self.assertEqual(kinds(bound), ["Budget bound"])
        self.assertIn("uses $24.75 of its $25.00 budget", views.alerts(bound, NOW)[0].text)
        self.assertEqual(kinds(under), [])

    def test_a_free_bid_over_the_budget_is_not_budget_bound(self):
        # 30,000 shares at 0.1c are $30, and 10,000 at 0.3c too. The $25
        # budget does not limit a bid the farmer says is free of it.
        for long in ((0.001, 30000.0), (0.003, 10000.0)):
            with self.subTest(long=long):
                over = state(A, naked_used=30.0, slots=quoting_slots(long=long, free=True))
                self.assertEqual(kinds(snap([over], [market(A, "a", budget=25.0)])), [])

    def test_the_bid_is_free_when_the_farmer_says_so(self):
        # The farmer publishes whether the budget leaves the longshot bid
        # alone: its price must offer a favorite buyer under 5% a year and
        # have passed its test for ten minutes. A $30 bid on a $25 budget
        # counts against it when the farmer says it is not free.
        slots = quoting_slots(long=(0.001, 30000.0))
        held_back = snap([state(A, slots=slots)], [market(A, "a", budget=25.0)])
        self.assertEqual(kinds(held_back), ["Budget bound"])
        self.assertIn("uses $30.00 of its $25.00 budget", views.alerts(held_back, NOW)[0].text)
        c = views.card(market(A, "a", budget=25.0), state(A, slots=slots), status(), NOW)
        self.assertEqual((c.budget_used_usd, c.free_bid_usd, c.naked_pct), (30.0, 0.0, 100.0))
        # A row from a farmer that published no verdict counts the bid too.
        del slots["LONG_BID"]["budget_free"]
        self.assertEqual(kinds(snap([state(A, slots=slots)], [market(A, "a", budget=25.0)])),
                         ["Budget bound"])
        slots = quoting_slots(long=(0.001, 30000.0), free=True)
        self.assertEqual(kinds(snap([state(A, slots=slots)], [market(A, "a", budget=25.0)])),
                         [])
        # The farmer's word also stands where longshot shares rest an exit.
        with_exit = state(A, slots=slots, position=holding_long(60.0, 0.01))
        self.assertEqual(kinds(snap([with_exit], [market(A, "a", budget=25.0)])), [])

    def test_longshot_shares_held_count_against_the_budget(self):
        # 20 shares held at $1 and a 2c bid for $5 use the whole $25.
        above = state(A, position=holding_long(20.0, 1.0),
                      slots=quoting_slots(long=(0.02, 250.0)))
        self.assertEqual(kinds(snap([above], [market(A, "a", budget=25.0)])), ["Budget bound"])
        # 30 shares at $1 are over the $25 but under the 50-share minimum, so
        # no exit rests for them and the farmer leaves the 0.1c bid free of
        # the budget.
        dust = state(A, position=holding_long(30.0, 1.0), slots=quoting_slots(free=True))
        self.assertEqual(kinds(snap([dust], [market(A, "a", budget=25.0)])), [])
        # 60 shares at 50c rest an exit, so the budget limits the 0.1c bid
        # too: $30 held and its $0.50.
        exit_rests = snap([state(A, position=holding_long(60.0, 0.5))],
                          [market(A, "a", budget=25.0)])
        self.assertEqual(kinds(exit_rests), ["Budget bound"])
        self.assertIn("uses $30.50 of its $25.00 budget", views.alerts(exit_rests, NOW)[0].text)

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
        cancelled = quoting_slots(free=True)
        cancelled["FAV_BID"] = slot("cancel", (0.97, 1000.0), reason="COOLDOWN")
        s = snap(
            [state(A, slots=quoting_slots(fav=(0.98, 100.0), long=(0.001, 500.0), free=True)),
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
        self.assertAlmostEqual(t.naked_counted_usd, 0.02 * 100)
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

    def test_live_rate_counts_quoting_economics_only(self):
        def with_economics(reward):
            slots = quoting_slots()
            slots["FAV_BID"]["economics"] = {**ECONOMICS, "reward_per_day": reward}
            return slots

        quoting = state(A, slots=with_economics(4.8))
        no_fav_bid = state(B)
        paused = state(C, "PAUSED", "paused by operator", slots=with_economics(7.0))
        cooling = state("0xddd", "COOLDOWN", slots=with_economics(9.0))
        self.assertEqual([views.live_daily(s) for s in (quoting, no_fav_bid, paused, cooling)],
                         [4.8, 0.0, None, None])
        t = views.totals(snap([quoting, no_fav_bid, paused, cooling]), NOW)
        self.assertAlmostEqual(t.live_daily_usd, 4.8)
        self.assertAlmostEqual(t.live_hourly_usd, 0.2)


class RewardsTabTest(unittest.TestCase):
    def test_rows_rank_by_live_rate_and_totals_reach_past_thirty_days(self):
        slots = quoting_slots()
        slots["FAV_BID"]["economics"] = {**ECONOMICS, "reward_per_day": 2.4}
        preview = quoting_slots()
        preview["FAV_BID"]["economics"] = {**ECONOMICS, "reward_per_day": 50.0}
        old = "0xold"
        s = snap([state(B), state(C, "PAUSED", slots=preview), state(A, slots=slots)],
                 [market(A, "a"), market(B, "b"), market(C, "c")],
                 rewards=[("2026-10-04", A, 1.0), ("2026-10-01", B, 0.5)])
        tab = views.rewards_tab(s, {A: 10.0, B: 0.5, old: 3.0}, NOW)
        self.assertEqual([(r.condition_id, r.rate_daily_usd, r.total_usd) for r in tab.rows],
                         [(A, 2.4, 10.0), (B, 0.0, 0.5), (old, None, 3.0), (C, None, 0.0)])
        self.assertAlmostEqual(tab.rows[0].rate_hourly_usd, 0.1)
        self.assertIsNone(tab.rows[2].rate_hourly_usd)
        self.assertEqual((tab.total_usd, tab.live_daily_usd, tab.quoting), (13.5, 2.4, 2))
        self.assertIsNone(views.rewards_tab(snap([state(A)]), {}, NOW).total_usd)


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


    def test_card_counts_what_the_naked_budget_limits(self):
        # The farmer publishes everything at risk on the naked leg. A $30
        # bid it says is free takes none of the $25 budget. A $10 bid at 2c
        # takes $10, and 5 longshot shares held at $1 take $5 more. With 60
        # shares held, an exit rests and the farmer counts the 0.1c bid too.
        def figures(**shown):
            c = views.card(market(A, "a", budget=25.0), state(A, **shown), status(), NOW)
            return c.naked_used_usd, c.budget_used_usd, c.free_bid_usd, c.naked_pct

        self.assertEqual(figures(naked_used=30.0,
                                 slots=quoting_slots(long=(0.001, 30000.0), free=True)),
                         (30.0, 0.0, 30.0, 0.0))
        self.assertEqual(figures(naked_used=15.0, slots=quoting_slots(long=(0.02, 500.0)),
                                 position=holding_long(5.0, 1.0)),
                         (15.0, 15.0, 0.0, 60.0))
        self.assertEqual(figures(naked_used=40.0, slots=quoting_slots(long=(0.001, 10000.0)),
                                 position=holding_long(60.0, 0.5)),
                         (40.0, 40.0, 0.0, 100.0))

    def test_card_shows_the_favorite_bid_economics_the_farmer_wrote(self):
        slots = quoting_slots()
        slots["FAV_BID"].update(economics=ECONOMICS, fav_reason="",
                                history=fill_cost.history_to_json(HISTORY))
        c = views.card(market(A, "a"), state(A, slots=slots), status(), NOW)
        self.assertEqual((c.economics["net_per_day"], c.history), (4.2, HISTORY))

    def test_a_state_from_before_fill_costs_has_no_economics(self):
        c = views.card(market(A, "a"), state(A), status(), NOW)
        self.assertEqual((c.economics, c.fav_reason, c.history), (None, "", None))

    def test_a_state_from_before_fill_histories_shows_no_history(self):
        slots = quoting_slots()
        slots["FAV_BID"].update(economics=ECONOMICS, fav_reason="",
                                tape={"sweeps": 3, "span_days": 30.0, "capped": True})
        c = views.card(market(A, "a"), state(A, slots=slots), status(), NOW)
        self.assertEqual((c.economics["net_per_day"], c.history), (4.2, None))


class FillCostFiguresTest(unittest.TestCase):
    def test_discover_ranks_by_net_apr_and_old_rows_show_their_gross_apr(self):
        new = market(A, "a")._replace(metrics={**metrics("a"), "apr": 0.9, "net_apr": 0.4})
        old = market(B, "b")._replace(metrics={**metrics("b"), "apr": 0.9})
        self.assertEqual(views.discover_row(1, new, status(), NOW).net_apr, 0.4)
        self.assertEqual(views.discover_row(2, old, status(), NOW).net_apr, 0.9)


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

    def test_a_failing_listener_never_stalls_the_refreshes(self):
        def listener(rates):
            raise RuntimeError("db locked")
        self.rates = remote.TotalRates(self.fetch, timedelta(minutes=5), lambda: self.now,
                                       on_rates=listener)
        self.answers = [{"0xa": 1.0}, {"0xb": 2.0}]
        self.rates.refresh()
        self.now = NOW + timedelta(minutes=5)
        self.rates.refresh()
        self.assertEqual((self.calls, self.rates.get()), (2, {"0xb": 2.0}))


def settle_total_rates():
    for thread in threading.enumerate():
        if thread.name == "console-total-rates":
            thread.join(10)


@contextlib.contextmanager
def remote_api(respond):
    requests = []

    def handle(request):
        requests.append(request)
        return respond(request)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    with mock.patch.object(remote.httpx, "Client", return_value=client):
        yield requests


class FetchRulesTest(unittest.TestCase):
    def fetch(self, open_entries, closed_entries):
        """(fetch_rules' Rules or LookupError, whether each Gamma query asked for closed)."""
        closed = []

        def handle(request):
            closed.append(request.url.params.get("closed") == "true")
            return httpx.Response(200, json=closed_entries if closed[-1] else open_entries)
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with mock.patch.object(remote.httpx, "Client", return_value=client):
            try:
                return remote.fetch_rules("0xa"), closed
            except LookupError as e:
                return e, closed

    def entry(self, description):
        return {"conditionId": "0xa", "description": description,
                "endDate": "2026-12-01T04:59:00Z"}

    def test_an_open_market_is_read_from_the_plain_query(self):
        rules, closed = self.fetch([self.entry("Open rules.")], [])
        self.assertEqual((rules.description, closed), ("Open rules.", [False]))

    def test_a_closed_market_is_read_from_the_closed_query(self):
        rules, closed = self.fetch([], [self.entry("Closed rules.")])
        self.assertEqual((rules.description, closed), ("Closed rules.", [False, True]))

    def test_a_market_gamma_never_lists_raises(self):
        error, _ = self.fetch([], [])
        self.assertIsInstance(error, LookupError)

    def test_each_query_names_the_market_alone_and_waits_the_console_timeout(self):
        with remote_api(lambda request: httpx.Response(200, json=[])) as requests:
            with self.assertRaises(LookupError):
                remote.fetch_rules("0xa")
        self.assertEqual([(r.method, r.url.path, dict(r.url.params)) for r in requests],
                         [("GET", "/markets", {"condition_ids": "0xa"}),
                          ("GET", "/markets", {"condition_ids": "0xa", "closed": "true"})])
        self.assertEqual({r.extensions["timeout"]["read"] for r in requests}, {remote.TIMEOUT_S})

    def test_a_rate_limited_query_raises_at_once_without_a_retry(self):
        limited = httpx.Response(429, headers={"retry-after": "1"})
        with remote_api(lambda request: limited) as requests, mock.patch("time.sleep") as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                remote.fetch_rules("0xa")
        self.assertEqual(len(requests), 1)
        sleep.assert_not_called()


class FetchBookTest(unittest.TestCase):
    BOOK = {"asset_id": "tok", "tick_size": "0.001",
            "bids": [{"price": "0.95", "size": "100"}, {"price": "0.96", "size": "0"}],
            "asks": [{"price": "0.98", "size": "30"}, {"price": "0.97", "size": "10"}]}

    def test_a_book_is_read_by_token_id_within_the_console_timeout(self):
        with remote_api(lambda request: httpx.Response(200, json=self.BOOK)) as requests:
            levels = remote.fetch_book("tok")
        self.assertEqual(tuple(levels), ([(0.95, 100.0)], [(0.97, 10.0), (0.98, 30.0)], 0.001))
        self.assertEqual([(r.method, r.url.path, dict(r.url.params),
                           r.extensions["timeout"]["read"]) for r in requests],
                         [("GET", "/book", {"token_id": "tok"}, remote.TIMEOUT_S)])

    def test_a_refused_read_raises(self):
        with remote_api(lambda request: httpx.Response(404)):
            with self.assertRaises(httpx.HTTPStatusError):
                remote.fetch_book("tok")


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
            farm_db.approve_market(self.db, cid, 0.99, 25.0, now=self.now)
        self.publish(events=[
            Event(self.now, EventKind.START, "Reward farmer: DRY RUN"),
            Event(self.now, EventKind.PLACE, "PLACE FAV_BID 98.0c alpha", A, "FAV_BID", 0.98, 100.0, 0.5),
            Event(self.now, EventKind.FILL, "FILL alpha FAV_BID 5 shares @ 98.0c", A, "FAV_BID", 0.98, 5.0),
        ], mids=[MidSample(A, self.now - timedelta(minutes=m), 0.99, 0.99, 0.98, 0.001)
                 for m in (30, 20, 10)])
        # Listing the queue starts a total-rates fetch, which must neither reach
        # the network nor outlive the scratch directory.
        self.addCleanup(settle_total_rates)
        self.app = create_app(self.path, **{"fetch_total_rates": dict, **self.app_kwargs()})
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

    def test_the_summary_shows_the_fill_history_and_passes_over_an_old_tape(self):
        for behind, shown in (({"history": fill_cost.history_to_json(HISTORY)},
                               "· history 9 sweeps below mid over 60.0d"),
                              ({"tape": {"sweeps": 3, "shares": 150.0}}, None)):
            with self.subTest(behind=list(behind)):
                slots = quoting_slots()
                slots["FAV_BID"].update(economics=ECONOMICS, fav_reason="", **behind)
                self.publish(states=[state(A, slots=slots)._replace(updated_at=self.now)])
                resp = self.client.get(f"/partials/markets/{A}/summary")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("8.0 fills/day", resp.text)
                if shown:
                    self.assertIn(shown, resp.text)
                else:
                    self.assertNotIn("sweeps", resp.text)

    def test_the_rewards_kpi_shows_the_live_rate_beside_the_screener(self):
        slots = quoting_slots()
        slots["FAV_BID"]["economics"] = ECONOMICS
        self.publish(states=[state(A, slots=slots)._replace(updated_at=self.now)])
        text = self.client.get("/partials/overview/kpis").text
        self.assertIn("$5.00/day", text)
        self.assertIn("$0.21/hr live · screener $10.00", text)

    def test_the_rewards_tab_shows_each_market_rate_and_total_earned(self):
        slots = quoting_slots()
        slots["FAV_BID"]["economics"] = ECONOMICS
        self.publish(states=[state(A, slots=slots)._replace(updated_at=self.now)])
        farm_db.store_rewards(self.db, (self.now - timedelta(days=40)).strftime("%Y-%m-%d"),
                              {A: 7.0}, self.now)
        farm_db.store_rewards(self.db, self.now.strftime("%Y-%m-%d"), {A: 1.25}, self.now)
        text = self.client.get("/activity?tab=rewards").text
        self.assertIn("<th>Market</th><th>Rate now $/hr", text)
        self.assertIn('<td class="mono">$0.21</td><td class="mono">$5.00</td>'
                      '<td class="mono">$10.00</td><td class="mono">n/a</td>'
                      '<td class="mono">$1.25</td><td class="mono">$1.25</td>'
                      '<td class="mono">$8.25</td></tr>', text)
        self.assertIn('<span class="mono value">$8.25</span>', text)

    def test_an_unrewarded_market_lists_under_its_own_phase(self):
        self.publish(states=[state(A, "UNREWARDED", "rewards removed")._replace(
            updated_at=self.now)])
        resp = self.client.get("/markets?phase=UNREWARDED")
        self.assertEqual(resp.status_code, 200)
        for needle in ("alpha?", "rewards removed", "reward listing no longer pays this market"):
            self.assertIn(needle, resp.text)
        self.assertNotIn("bravo?", resp.text)

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

    def test_a_free_bid_over_the_budget_reads_as_outside_it(self):
        # 30,000 shares at 0.1c are $30 against alpha's $25 budget, and the
        # farmer says a fill would give a favorite buyer 0.42% a year.
        over = state(A, naked_used=30.0,
                     slots=quoting_slots(long=(0.001, 30000.0), free=True, offers=0.0042))
        self.publish(states=[over._replace(updated_at=self.now)])
        self.assertNotIn("Budget bound", self.client.get("/partials/overview/alerts").text)
        card = self.client.get("/partials/markets").text
        self.assertIn("$0.00 / $25.00", card)
        self.assertIn("$30.00 outside the budget: a fill gives a buyer 0.4% a year", card)
        self.assertIn("$0.00 of $25 budget", self.client.get("/partials/overview/tracked").text)
        self.assertIn("$0.00 of it counts against $25.00 budget",
                      self.client.get("/partials/overview/kpis").text)
        self.assertIn("$0.00 of $25.00 budget; the longshot bid is outside it. A fill gives a "
                      "buyer 0.4% a year", self.client.get(f"/markets/{A}").text)

    def test_a_bid_outside_the_free_zone_reads_against_the_budget(self):
        above = state(A, naked_used=10.0,
                      slots=quoting_slots(long=(0.02, 500.0), offers=0.0911))
        self.publish(states=[above._replace(updated_at=self.now)])
        card = self.client.get("/partials/markets").text
        self.assertIn("$10.00 / $25.00", card)
        self.assertNotIn("a fill gives a buyer", card)
        self.assertIn("$10.00 of $25 budget", self.client.get("/partials/overview/tracked").text)
        self.assertIn("$10.00 of it counts against $25.00 budget",
                      self.client.get("/partials/overview/kpis").text)
        page = self.client.get(f"/markets/{A}").text
        self.assertIn("$10.00 of $25.00 budget. A fill gives a buyer 9.1% a year</span>", page)
        self.assertNotIn("the longshot bid is outside it", page)

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
