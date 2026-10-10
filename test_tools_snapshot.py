import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from polymarket.orders import Side
from test_polymarket_gamma import Gamma, cid, client
from test_polymarket_orders import Clob, entry, without
from tools import db, snapshot

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
PACIFIC = timezone(timedelta(hours=-7))


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.db = db.open_db(os.path.join(tmp.name, "tools.db"))
        self.addCleanup(self.db.close)

    def rows(self):
        return [tuple(r) for table in ("snapshots", "snapshot_orders")
                for r in self.db.execute(f"SELECT * FROM {table} ORDER BY id")]


class TakeTests(Scratch):
    def test_a_snapshot_stores_every_open_order_with_its_market_name(self):
        gamma = Gamma(open_ids=[cid(1)], closed_ids=[cid(2)])
        listing = [entry(id="0xa", market=cid(1)), entry(id="0xb", market=cid(2), side="SELL"),
                   entry(id="0xc", market=cid(1), expiration="0")]
        taken = snapshot.take(Clob(listing), client(gamma), self.db, NOW, "before the upgrade",
                              job_id=3)
        self.assertEqual((taken.orders, taken.names_error), (3, None))
        stored = db.read_snapshot(self.db, taken.snapshot_id)
        self.assertEqual((stored.taken_at, stored.note, stored.source, stored.job_id,
                          stored.orders), (NOW, "before the upgrade", None, 3, 3))
        self.assertEqual(
            [(o.order.order_id, o.market_name, o.order.side, o.order.expiration is None)
             for o in db.read_snapshot_orders(self.db, taken.snapshot_id)],
            [("0xa", f"Q {cid(1)[-4:]}", Side.BUY, False),
             ("0xb", f"Q {cid(2)[-4:]}", Side.SELL, False),
             ("0xc", f"Q {cid(1)[-4:]}", Side.BUY, True)])

    def test_a_failed_listing_raises_and_stores_no_snapshot(self):
        gamma = Gamma()
        for listing in (ConnectionError("timed out"), None, [entry(), without("price")]):
            with self.subTest(listing=listing):
                with self.assertRaises((ConnectionError, ValueError)):
                    snapshot.take(Clob(listing), client(gamma), self.db, NOW, "")
        self.assertEqual(self.rows(), [])
        self.assertEqual(gamma.requests, [])

    def test_the_orders_are_kept_when_the_names_cannot_be_read(self):
        gamma = Gamma(fail_when=lambda ids: True)
        taken = snapshot.take(Clob([entry(market=cid(1))]), client(gamma), self.db, NOW, "")
        self.assertIn("500", taken.names_error)
        self.assertEqual([(o.order.order_id, o.market_name)
                          for o in db.read_snapshot_orders(self.db, taken.snapshot_id)],
                         [("0xorder1", None)])

    def test_an_account_with_no_open_orders_gives_an_empty_snapshot(self):
        gamma = Gamma()
        taken = snapshot.take(Clob([]), client(gamma), self.db, NOW, "all quiet")
        self.assertEqual((taken.orders, db.read_snapshot(self.db, taken.snapshot_id).orders),
                         (0, 0))
        self.assertEqual(gamma.requests, [])


def old_file(directory, name, taken, orders):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        json.dump({"metadata": {"timestamp": taken, "total_orders": len(orders),
                                "snapshot_version": "1.0"}, "orders": orders}, f)


class ImportTests(Scratch):
    def setUp(self):
        super().setUp()
        self.files = os.path.join(self.dir, "snapshots")
        os.mkdir(self.files)
        old_file(self.files, "orders_snapshot_20260718_221655.json", "2026-07-18T22:16:55.620915",
                 [entry(id="0xa", market_name="Will A happen?", outcome="Yes"),
                  entry(id="0xb", market=cid(2), market_name="Will B happen?", side="SELL",
                        expiration="0")])
        old_file(self.files, "orders_snapshot_20260725_004812.json", "2026-07-25T00:48:12.150937",
                 [entry(id="0xc")])

    def test_each_file_becomes_one_snapshot_named_after_it(self):
        done = snapshot.import_files(self.db, self.files, local_zone=PACIFIC)
        self.assertEqual((done.files, done.orders, done.skipped), (2, 3, 0))
        newest, oldest = db.read_snapshots(self.db)
        self.assertEqual([(s.source, s.orders, s.job_id) for s in (oldest, newest)],
                         [("orders_snapshot_20260718_221655.json", 2, None),
                          ("orders_snapshot_20260725_004812.json", 1, None)])
        self.assertEqual(
            [(o.order.order_id, o.market_name, o.order.outcome, o.order.side, o.order.price,
              o.order.size, o.order.size_matched, o.order.expiration is None)
             for o in db.read_snapshot_orders(self.db, oldest.id)],
            [("0xa", "Will A happen?", "Yes", Side.BUY, 0.986, 2000.0, 150.5, False),
             ("0xb", "Will B happen?", "No", Side.SELL, 0.986, 2000.0, 150.5, True)])
        self.assertIsNone(db.read_snapshot_orders(self.db, newest.id)[0].market_name)

    def test_a_files_local_time_is_stored_as_utc(self):
        snapshot.import_files(self.db, self.files, local_zone=PACIFIC)
        self.assertEqual([s.taken_at for s in db.read_snapshots(self.db)],
                         [datetime(2026, 7, 25, 7, 48, 12, tzinfo=timezone.utc),
                          datetime(2026, 7, 19, 5, 16, 55, tzinfo=timezone.utc)])

    def test_without_a_zone_the_machines_own_zone_is_used(self):
        snapshot.import_files(self.db, self.files)
        self.assertEqual(db.read_snapshots(self.db)[0].taken_at,
                         datetime(2026, 7, 25, 0, 48, 12).astimezone(timezone.utc))

    def test_importing_twice_changes_nothing(self):
        snapshot.import_files(self.db, self.files, local_zone=PACIFIC)
        before = self.rows()
        again = snapshot.import_files(self.db, self.files, local_zone=PACIFIC)
        self.assertEqual((again.files, again.orders, again.skipped), (0, 0, 2))
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(before), 2 + 3)

    def test_a_file_added_later_is_the_only_one_imported(self):
        snapshot.import_files(self.db, self.files, local_zone=PACIFIC)
        old_file(self.files, "orders_snapshot_20260801_015315.json", "2026-08-01T01:53:15",
                 [entry(id="0xd"), entry(id="0xe")])
        done = snapshot.import_files(self.db, self.files, local_zone=PACIFIC)
        self.assertEqual((done.files, done.orders, done.skipped), (1, 2, 2))
        self.assertEqual(len(db.read_snapshots(self.db)), 3)

    def test_only_snapshot_files_are_read(self):
        with open(os.path.join(self.files, "notes.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertEqual(snapshot.import_files(self.db, self.files).files, 2)

    def test_a_file_that_is_not_a_snapshot_is_refused_by_name_and_stores_nothing(self):
        broken = {"no_orders.json": {"metadata": {"timestamp": "2026-08-01T01:53:15"}},
                  "no_time.json": {"metadata": {}, "orders": []},
                  "bad_order.json": {"metadata": {"timestamp": "2026-08-01T01:53:15"},
                                     "orders": [entry(), without("price")]}}
        for name, content in broken.items():
            path = os.path.join(self.files, f"orders_snapshot_{name}")
            with self.subTest(name=name):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(content, f)
                with self.assertRaisesRegex(ValueError, f"orders_snapshot_{name}"):
                    snapshot.import_files(self.db, self.files, local_zone=PACIFIC)
                os.remove(path)
        self.assertEqual(len(db.read_snapshots(self.db)), 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM snapshot_orders").fetchone()[0], 3)

    def test_a_missing_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            snapshot.import_files(self.db, os.path.join(self.dir, "nowhere"))


if __name__ == "__main__":
    unittest.main()
