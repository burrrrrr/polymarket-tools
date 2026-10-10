import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from polymarket.orders import LimitOrder, Side
from tools import db
from tools.db import Guard, Reason, Sample, Settlement, WatchState

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MARKET = "0x" + "aa" * 32
TOKEN, OTHER = "71" * 38, "72" * 38


def later(seconds):
    return NOW + timedelta(seconds=seconds)


def form(**changes):
    fields = {"token_id": TOKEN, "market": MARKET, "label": "Will it? -> Yes", "side": "BUY",
              "price": "0.999", "size": "100", "target_tick": "0.001", "guard": "1",
              "guard_price": "0.99", "guard_tolerance": "0.005", "guard_max_age_s": "5",
              "expires_after_s": "0", "dry_run": ""}
    fields.update(changes)
    return fields


def new(**changes):
    parsed, errors = db.parse_watch(form(**changes))
    assert not errors, errors
    return parsed


def fired(order_id="0xnew"):
    return Settlement(WatchState.FIRED, None, "", order_id=order_id, attempts=1)


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "tools.db")
        self.db = db.open_db(self.path)
        self.addCleanup(self.db.close)

    def arm(self, **changes):
        return db.arm_watch(self.db, new(**changes), NOW)

    def row(self, watch_id):
        return db.read_watch(self.db, watch_id)

    def fire(self, watch_id, at=1):
        db.mark_firing(self.db, watch_id, later(at), "feed", "0.001", 0.99, 1.0, later(at - 1))

    def raw(self, sql, *params):
        with self.db:
            self.db.execute(sql, params)


class ParseWatchTests(unittest.TestCase):
    def test_the_form_parses_to_a_watch_with_its_order_guard_and_flags(self):
        parsed, errors = db.parse_watch(form(size=" 2,500 ", expires_after_s="3600"))
        self.assertEqual(errors, {})
        self.assertEqual(parsed, db.NewWatch(
            LimitOrder(TOKEN, Side.BUY, 0.999, 2500.0, 3600), MARKET, "Will it? -> Yes", "0.001",
            Guard(0.99, 0.005, 5.0), False))
        self.assertEqual(parsed.order.order_type, "GTD")

    def test_the_defaults_are_the_old_tools(self):
        self.assertEqual(db.GUARD_DEFAULT, Guard(0.99, 0.005, 5.0))
        self.assertEqual(db.TARGET_TICK_DEFAULT, "0.001")
        self.assertEqual(new().order.order_type, "GTC")

    def test_a_watch_can_be_armed_without_a_guard_and_as_a_dry_run(self):
        parsed = new(guard="", guard_price="junk", dry_run="1")
        self.assertEqual((parsed.guard, parsed.dry_run), (None, True))

    def test_each_bad_field_is_named_and_nothing_parses(self):
        cases = {
            "token_id": "", "market": " ", "label": "", "side": "HOLD", "price": "0.9995",
            "size": "0", "target_tick": "0.002", "guard_price": "1.2", "guard_tolerance": "-1",
            "guard_max_age_s": "0", "expires_after_s": "30"}
        for field, value in cases.items():
            with self.subTest(field=field, value=value):
                parsed, errors = db.parse_watch(form(**{field: value}))
                self.assertIsNone(parsed)
                self.assertEqual(list(errors), [field])
        parsed, errors = db.parse_watch({})
        self.assertIsNone(parsed)
        self.assertIn("token_id", errors)

    def test_a_price_is_checked_against_the_target_ticks_grid_and_range(self):
        self.assertEqual(new(price="0.98", target_tick="0.01").order.price, 0.98)
        for price, tick in (("0.985", "0.01"), ("1", "0.001"), ("0", "0.001"), ("0.9999", "0.001")):
            with self.subTest(price=price, tick=tick):
                self.assertEqual(list(db.parse_watch(form(price=price, target_tick=tick))[1]),
                                 ["price"])

    def test_a_sell_is_a_sell_whatever_its_case(self):
        self.assertEqual(new(side="sell").order.side, Side.SELL)


class ArmTests(Scratch):
    def test_an_armed_watch_reads_back_with_the_console_columns_and_nothing_else(self):
        watch_id = self.arm()
        row = self.row(watch_id)
        self.assertEqual((row.id, row.created_at, row.order, row.market, row.label, row.target_tick,
                          row.guard, row.dry_run, row.disarm_requested_at, row.state),
                         (watch_id, NOW, LimitOrder(TOKEN, Side.BUY, 0.999, 100.0, 0), MARKET,
                          "Will it? -> Yes", "0.001", Guard(0.99, 0.005, 5.0), False,
                          None, WatchState.ARMED))
        self.assertEqual((row.reason, row.detail, row.tick, row.bid, row.ask, row.seen_at,
                          row.triggered_at, row.source, row.send_ms, row.post_ms, row.attempts,
                          row.order_id, row.resolved_at),
                         (None,) * 13)
        self.assertTrue(row.open)

    def test_one_open_watch_for_a_token_and_a_new_one_once_it_closed(self):
        first = self.arm()
        self.assertIsNone(self.arm(price="0.998"))
        self.assertIsNotNone(self.arm(token_id=OTHER))
        self.fire(first)
        self.assertIsNone(self.arm(), "a firing watch left its token open")
        db.close_watch(self.db, first, fired(), later(2))
        self.assertIsNotNone(self.arm())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM watches").fetchone()[0], 3)

    def test_watches_list_open_ones_first_and_then_newest_first(self):
        closed = self.arm()
        db.close_watch(self.db, closed, Settlement(WatchState.DISARMED, None, ""), later(1))
        armed = self.arm(token_id=OTHER)
        again = self.arm()
        self.assertEqual([w.id for w in db.watches(self.db)], [again, armed, closed])
        self.assertEqual([w.id for w in db.open_watches(self.db)], [armed, again])
        self.assertEqual([w.id for w in db.watches(self.db, limit=2)], [again, armed])

    def test_an_unknown_watch_reads_as_none(self):
        self.assertIsNone(self.row(99))


class DisarmTests(Scratch):
    def test_a_disarm_is_a_request_the_worker_answers_not_a_state_change(self):
        watch_id = self.arm()
        self.assertTrue(db.request_disarm(self.db, watch_id, later(1)))
        row = self.row(watch_id)
        self.assertEqual((row.state, row.disarm_requested_at), (WatchState.ARMED, later(1)))
        self.assertFalse(db.request_disarm(self.db, watch_id, later(2)), "asked twice")
        self.assertEqual(self.row(watch_id).disarm_requested_at, later(1))

    def test_a_firing_watch_takes_the_request_and_a_closed_one_refuses_it(self):
        firing, closed = self.arm(), self.arm(token_id=OTHER)
        self.fire(firing)
        db.close_watch(self.db, closed, Settlement(WatchState.MISSED, Reason.AT_TARGET, ""), NOW)
        self.assertTrue(db.request_disarm(self.db, firing, later(1)))
        self.assertFalse(db.request_disarm(self.db, closed, later(1)))
        self.assertFalse(db.request_disarm(self.db, 99, later(1)))


class WorkerWriteTests(Scratch):
    def test_firing_freezes_the_trigger_and_the_quote_the_guard_judged(self):
        watch_id = self.arm()
        self.fire(watch_id, at=5)
        row = self.row(watch_id)
        self.assertEqual((row.state, row.triggered_at, row.source, row.tick, row.bid, row.ask,
                          row.seen_at),
                         (WatchState.FIRING, later(5), "feed", "0.001", 0.99, 1.0, later(4)))
        self.assertTrue(row.open)

    def test_only_an_armed_watch_starts_firing(self):
        watch_id = self.arm()
        self.fire(watch_id, at=1)
        self.fire(watch_id, at=9)
        self.assertEqual(self.row(watch_id).triggered_at, later(1))

    def test_a_settlement_closes_an_open_watch_once_and_a_second_one_changes_nothing(self):
        watch_id = self.arm()
        self.fire(watch_id)
        done = Settlement(WatchState.FIRED, None, "", order_id="0xnew", attempts=2)
        self.assertTrue(db.close_watch(self.db, watch_id, done, later(3), send_ms=0.4,
                                       post_ms=180.0))
        row = self.row(watch_id)
        self.assertEqual((row.state, row.reason, row.detail, row.order_id, row.attempts,
                          row.send_ms, row.post_ms, row.resolved_at, row.open),
                         (WatchState.FIRED, None, "", "0xnew", 2, 0.4, 180.0, later(3), False))
        self.assertFalse(db.close_watch(
            self.db, watch_id, Settlement(WatchState.FAILED, Reason.VENUE, "late"), later(9)))
        self.assertEqual(self.row(watch_id), row)

    def test_an_armed_watch_closes_straight_from_armed(self):
        watch_id = self.arm()
        refused = Settlement(WatchState.REJECTED, Reason.GUARD_PRICE, "bid was 97.0c")
        self.assertTrue(db.close_watch(self.db, watch_id, refused, later(1)))
        row = self.row(watch_id)
        self.assertEqual((row.state, row.reason, row.detail), (WatchState.REJECTED,
                                                               Reason.GUARD_PRICE, "bid was 97.0c"))

    def test_a_watch_found_firing_when_a_worker_starts_is_closed_with_the_settlement_given(self):
        left, armed, done = self.arm(), self.arm(token_id=OTHER), self.arm(token_id="73" * 38)
        self.fire(left)
        self.fire(done)
        db.close_watch(self.db, done, fired(), later(1))
        interrupted = Settlement(WatchState.FAILED, Reason.INTERRUPTED, "")
        self.assertEqual(db.close_firing(self.db, interrupted, db.INTERRUPTED_LINE, later(60)),
                         [left])
        row = self.row(left)
        self.assertEqual((row.state, row.reason, row.resolved_at),
                         (WatchState.FAILED, Reason.INTERRUPTED, later(60)))
        self.assertTrue(row.reason.may_rest)
        self.assertEqual(self.row(armed).state, WatchState.ARMED)
        self.assertEqual(self.row(done).state, WatchState.FIRED)
        self.assertEqual([line.message for line in db.read_watch_log(self.db, left)],
                         [db.INTERRUPTED_LINE])
        self.assertEqual(db.close_firing(self.db, interrupted, db.INTERRUPTED_LINE, later(61)),
                         [])

    def test_samples_land_on_armed_watches_only(self):
        armed, firing = self.arm(), self.arm(token_id=OTHER)
        self.fire(firing, at=1)
        db.publish_samples(self.db, [Sample(armed, "0.01", 0.99, 0.995, later(2)),
                                     Sample(firing, "0.001", 0.5, 0.6, later(2))])
        row = self.row(armed)
        self.assertEqual((row.tick, row.bid, row.ask, row.seen_at), ("0.01", 0.99, 0.995, later(2)))
        self.assertEqual((self.row(firing).bid, self.row(firing).seen_at), (0.99, later(0)))
        db.publish_samples(self.db, [Sample(armed, "0.01", None, None, later(3))])
        self.assertEqual((self.row(armed).bid, self.row(armed).seen_at), (None, later(3)))


class ConstraintTests(Scratch):
    def refused(self, sql, *params):
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw(sql, *params)

    def test_a_closed_watch_is_final_whatever_state_is_written(self):
        for settlement in (fired(), Settlement(WatchState.DISARMED, None, ""),
                           Settlement(WatchState.FAILED, Reason.UNANSWERED, "timed out")):
            with self.subTest(state=settlement.state):
                watch_id = self.arm(token_id=f"{settlement.state.value}-token")
                db.close_watch(self.db, watch_id, settlement, NOW)
                for state in ("armed", "firing", "fired", "disarmed"):
                    self.refused("UPDATE watches SET state = ?, order_id = ? WHERE id = ?", state,
                                 "0xnew" if state == "fired" else None, watch_id)
                self.assertEqual(self.row(watch_id).state, settlement.state)

    def test_fired_means_an_order_id_and_an_order_id_means_fired(self):
        watch_id = self.arm()
        self.refused("UPDATE watches SET state = 'fired' WHERE id = ?", watch_id)
        self.refused("UPDATE watches SET order_id = '0xnew' WHERE id = ?", watch_id)
        self.refused("UPDATE watches SET state = 'failed', order_id = '0xnew' WHERE id = ?",
                     watch_id)
        with self.assertRaises(sqlite3.IntegrityError):
            db.close_watch(self.db, watch_id, Settlement(WatchState.FIRED, None, ""), NOW)
        self.assertEqual(self.row(watch_id).state, WatchState.ARMED)

    def test_a_dry_run_is_never_fired_and_a_live_watch_is_never_rehearsed(self):
        dry, live = self.arm(dry_run="1"), self.arm(token_id=OTHER)
        with self.assertRaises(sqlite3.IntegrityError):
            db.close_watch(self.db, dry, fired(), NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            db.close_watch(self.db, live, Settlement(WatchState.REHEARSED, None, ""), NOW)
        self.assertEqual([self.row(w).state for w in (dry, live)], [WatchState.ARMED] * 2)

    def test_the_guard_is_whole_or_absent_and_the_enums_are_closed(self):
        self.refused("UPDATE watches SET guard_price = NULL WHERE id = ?", self.arm())
        base = ("INSERT INTO watches (created_at, token_id, market, label, side, price, size, "
                "expires_after_s, target_tick, dry_run, state) VALUES "
                "('2026-10-11T12:00:00+00:00', 'tok', 'mkt', 'label', ?, 0.999, 100, 0, "
                "'0.001', 0, ?)")
        self.refused(base, "HOLD", "armed")
        self.refused(base, "BUY", "waiting")
        self.refused("UPDATE watches SET reason = 'because' WHERE id = ?", self.arm(token_id=OTHER))


class ChangeMarkTests(Scratch):
    def test_every_change_to_a_watch_moves_the_version(self):
        seen = [db.watch_version(self.db)]

        def moved():
            seen.append(db.watch_version(self.db))
            return seen[-1] != seen[-2]

        watch_id = self.arm()
        self.assertTrue(moved(), "armed")
        db.publish_samples(self.db, [Sample(watch_id, "0.01", 0.99, 1.0, later(1))])
        self.assertTrue(moved(), "sampled")
        db.request_disarm(self.db, watch_id, later(2))
        self.assertTrue(moved(), "disarm requested")
        self.fire(watch_id)
        self.assertTrue(moved(), "firing")
        db.close_watch(self.db, watch_id, fired(), later(3))
        self.assertTrue(moved(), "closed")
        db.append_log(self.db, later(4), "fired", watch_id=watch_id)
        self.assertTrue(moved(), "logged")
        db.append_log(self.db, later(5), "started", job_id=1)
        self.assertFalse(moved(), "a job line is not a watch change")

    def test_the_version_counts_each_write_once(self):
        watch_id = self.arm()
        db.request_disarm(self.db, watch_id, later(1))
        self.assertEqual(self.db.execute("SELECT version FROM watches WHERE id = ?",
                                         (watch_id,)).fetchone()[0], 2)


class LogTests(Scratch):
    def test_a_watchs_lines_read_back_oldest_first_and_apart_from_other_watches(self):
        db.append_log(self.db, NOW, "adopted", watch_id=1)
        db.append_log(self.db, later(1), "other", watch_id=2)
        db.append_log(self.db, later(2), "fired", watch_id=1)
        db.append_log(self.db, later(3), "a job line", job_id=1)
        self.assertEqual([(line.at, line.watch_id, line.message)
                          for line in db.read_watch_log(self.db, 1)],
                         [(NOW, 1, "adopted"), (later(2), 1, "fired")])


class DescribeTests(Scratch):
    def said(self, watch_id, keyed=True, now=later(10)):
        return db.describe(self.row(watch_id), keyed, now)

    def test_an_armed_watch_says_what_it_waits_for(self):
        live, dry = self.arm(), self.arm(token_id=OTHER, dry_run="1")
        self.assertEqual(self.said(live, keyed=None), "Armed. Waiting for a worker.")
        self.assertEqual(self.said(live, keyed=False),
                         "Armed. Waiting for a keyed worker: the running worker holds no key.")
        self.assertEqual(self.said(dry, keyed=False),
                         "Armed. The worker has not read the book yet.")
        db.publish_samples(self.db, [Sample(live, "0.01", 0.99, 0.995, later(7))])
        self.assertEqual(self.said(live), "Armed. Tick 0.01, bid 99.0c, ask 99.5c, seen 3s ago.")
        db.request_disarm(self.db, live, later(8))
        self.assertEqual(self.said(live), "Disarm requested. Waiting for the worker to answer.")

    def test_a_firing_watch_says_the_order_left(self):
        watch_id = self.arm()
        self.fire(watch_id, at=5)
        self.assertEqual(self.said(watch_id),
                         "Firing. The order left 5s ago; waiting for the exchange's answer.")

    def test_each_closed_state_reads_as_its_outcome(self):
        cases = [
            (fired()._replace(attempts=2), "Fired. Order 0xnew rests after 2 attempts."),
            (Settlement(WatchState.REHEARSED, None, "signed, not sent"),
             "Rehearsed. A dry run: signed, not sent."),
            (Settlement(WatchState.REJECTED, Reason.GUARD_PRICE, "bid was 97.0c"),
             "Rejected by your guard, nothing was sent: bid was 97.0c."),
            (Settlement(WatchState.FAILED, Reason.VENUE, "not enough balance"),
             "The exchange refused the order: not enough balance."),
            (Settlement(WatchState.FAILED, Reason.UNANSWERED, "TimeoutError: timed out"),
             "The post got no answer (TimeoutError: timed out). An order may rest: check your "
             "open orders."),
            (Settlement(WatchState.FAILED, Reason.INTERRUPTED, ""),
             "The worker stopped while the order was leaving. An order may rest: check your "
             "open orders."),
            (Settlement(WatchState.FAILED, Reason.STOPPED, "venue said no", attempts=3),
             "The worker stopped inside the retry window after 3 refusals: venue said no. No "
             "order rests."),
            (Settlement(WatchState.FAILED, Reason.SIGN, "KeyError: tok"),
             "The order could not be signed: KeyError: tok."),
            (Settlement(WatchState.MISSED, Reason.AT_TARGET, "0.001"),
             "Missed: the tick was already 0.001 when the worker adopted the watch."),
            (Settlement(WatchState.DISARMED, None, ""), "Disarmed."),
        ]
        for n, (settlement, sentence) in enumerate(cases):
            with self.subTest(sentence=sentence):
                watch_id = self.arm(token_id=f"{n}" * 38,
                                    dry_run="1" if settlement.state is WatchState.REHEARSED else "")
                db.close_watch(self.db, watch_id, settlement, later(9))
                self.assertEqual(self.said(watch_id), sentence)
                self.assertEqual(self.said(watch_id, keyed=None), sentence)


if __name__ == "__main__":
    unittest.main()
