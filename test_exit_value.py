import math
import unittest
from datetime import datetime, timezone

from exit_value import (
    EXIT_JSON_VERSION,
    BuyTape,
    Piece,
    Sweep,
    tape_from_json,
    tape_to_json,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
DAY = 86400.0
START = T - 60 * DAY


def sweep(days_ago, *pieces):
    return Sweep(T - days_ago * DAY, tuple(Piece(p, s) for p, s in pieces))


# Three buys, the newest swept two levels.
SWEEPS = (sweep(30, (0.95, 100.0)), sweep(10, (0.96, 40.0)),
          sweep(2, (0.97, 25.0), (0.965, 300.0)))


def fields(tape):
    return tape.sweeps, tape.read_at, tape.first_at


class BuyTapeTest(unittest.TestCase):
    def test_sweeps_are_held_oldest_first_and_pieces_highest_first(self):
        shuffled = [SWEEPS[2]._replace(pieces=SWEEPS[2].pieces[::-1]), SWEEPS[0], SWEEPS[1]]
        tape = BuyTape(shuffled, NOW, START)
        self.assertEqual(tape.sweeps, SWEEPS)

    def test_a_sweep_that_is_not_a_buy_of_the_favorite_is_refused(self):
        for bad in (sweep(1), sweep(1, (1.2, 10.0)), sweep(1, (0.0, 10.0)),
                    sweep(1, (0.95, 0.0)), sweep(1, (0.95, math.nan)), sweep(61, (0.95, 10.0))):
            with self.subTest(sweep=bad), self.assertRaises(ValueError):
                BuyTape([bad], NOW, START)

    def test_as_of_is_the_tape_as_it_was_read_then(self):
        tape = BuyTape(SWEEPS, NOW, START).as_of(T - 5 * DAY)
        self.assertEqual(fields(tape), (SWEEPS[:2], datetime.fromtimestamp(T - 5 * DAY,
                                                                           timezone.utc), START))

    def test_as_of_before_the_tape_begins_has_no_sweeps_and_no_exposure(self):
        tape = BuyTape(SWEEPS, NOW, START).as_of(START - DAY)
        self.assertEqual((tape.sweeps, tape.first_at, tape.read_at.timestamp()),
                         ((), START - DAY, START - DAY))


class TapeJsonTest(unittest.TestCase):
    def test_a_tape_reads_back_as_it_was_written(self):
        tape = tape_from_json(tape_to_json(BuyTape(SWEEPS, NOW, START), 500))
        self.assertEqual(fields(tape), (SWEEPS, NOW, START))

    def test_an_empty_tape_keeps_where_it_is_complete_from(self):
        tape = tape_from_json(tape_to_json(BuyTape((), NOW, START), 500))
        self.assertEqual(fields(tape), ((), NOW, START))

    def test_a_long_tape_keeps_its_newest_sweeps_complete_from_the_oldest_kept(self):
        tape = tape_from_json(tape_to_json(BuyTape(SWEEPS, NOW, START), 2))
        self.assertEqual(fields(tape), (SWEEPS[1:], NOW, SWEEPS[1].at))

    def test_junk_reads_as_no_tape(self):
        good = tape_to_json(BuyTape(SWEEPS, NOW, START), 500)
        newest, oldest = good["sweeps"][-1], good["sweeps"][0]
        for data in (
            None, "junk", [], {}, {**good, "v": EXIT_JSON_VERSION + 1}, {**good, "v": None},
            {**good, "read_at": "soon"}, {**good, "first_at": "then"},
            {**good, "first_at": newest[0] + 1}, {**good, "sweeps": "junk"},
            {**good, "sweeps": good["sweeps"][::-1]},
            {**good, "sweeps": [oldest, [newest[0], []]]},
            {**good, "sweeps": [oldest, [newest[0], [[1.2, 10.0]]]]},
            {**good, "sweeps": [oldest, [newest[0], [[0.97, -1.0]]]]},
            {**good, "sweeps": [oldest, [newest[0], [[0.97]]]]},
            {**good, "sweeps": [oldest, [None, [[0.97, 1.0]]]]},
        ):
            with self.subTest(data=data):
                self.assertIsNone(tape_from_json(data))


if __name__ == "__main__":
    unittest.main()
