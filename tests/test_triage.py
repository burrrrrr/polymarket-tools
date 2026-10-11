import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from farm import farm_db, triage
from console.remote import Rules

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
QUESTION = "Will the US confirm that aliens exist before 2027?"
FILES = "Will the US release its UFO files before 2027?"
RULES = ("This market will resolve to \u201cYes\u201d if the US government officially "
         "confirms the existence of extraterrestrial life before December 31, 2026, 11:59 PM "
         "ET \u2014 a statement by the President counts. Otherwise it resolves to \u201cNo\u201d.")
PATH = {"path": "The President confirms aliens exist",
        "rule_quote": "a statement by the President counts", "realistic": False}
FACT = {"text": "The Pentagon released UAP files", "quote": "The files show no evidence.",
        "url": "https://example.org/uap", "date": "2026-05-01", "toward_yes": []}
STATE = "No confirmation has been made or scheduled."


def judge_output(answer="near_impossible", settled=True, paths=(PATH,), reason="Absurd."):
    return {"yes_paths": list(paths), "settled_by_news": settled, "answer": answer,
            "reason": reason}


def brief_output(facts=(), state=STATE):
    return {"searches": ["US government confirms aliens 2026"], "state": state,
            "facts": list(facts)}


def envelope(structured, **changes):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "num_turns": 1, "total_cost_usd": 0.04, "duration_ms": 26000,
                       "structured_output": structured, **changes})


def run(answer="near_impossible", settled=True, quote_ok=True):
    return triage.JudgeRun(answer, settled,
                           (triage.YesPath("a path", "a quote", False, quote_ok),), "r")


def fact(text, toward_yes=False, url="", date=None, verified=None):
    return triage.Fact(text, "q", url, date, toward_yes, verified)


def briefed(*facts):
    return triage.Brief(("a search",), STATE, facts)


HEARING = fact("Congress held a UAP hearing", True)
DENIAL = fact("The Pentagon denied holding alien remains")
NEAR, QUIET, HUMAN = triage.Verdict


def decided(runs, brief=briefed(), days_left=10):
    screen = triage.decide(runs, brief, days_left)
    return screen.verdict, screen.why


class DecideTest(unittest.TestCase):
    def test_unanimous_near_impossible_runs_are_near_impossible_whatever_the_end_date(self):
        for days in (None, 10, 400):
            with self.subTest(days=days):
                self.assertEqual(decided((run(),) * 3, days_left=days), (NEAR, ()))

    def test_possible_or_unsure_runs_ending_within_45_days_are_quiet(self):
        for days in (0, 30, triage.QUIET_DAYS):
            with self.subTest(days=days):
                self.assertEqual(decided((run("possible"), run("unsure"), run()), days_left=days),
                                 (QUIET, ()))

    def test_rule_quotes_do_not_matter_to_quiet(self):
        self.assertEqual(
            decided((run(quote_ok=False), run("possible", quote_ok=False), run()), days_left=20),
            (QUIET, ()))

    def test_past_the_quiet_window_needs_a_human(self):
        self.assertEqual(decided((run("possible"),) * 3, days_left=triage.QUIET_DAYS + 1),
                         (HUMAN, ("0 of 3 runs near impossible",
                                  "ends in 46 days, past the 45-day quiet window")))

    def test_an_unknown_end_date_needs_a_human(self):
        self.assertEqual(decided((run(), run("unsure"), run()), days_left=None),
                         (HUMAN, ("2 of 3 runs near impossible", "end date unknown")))

    def test_any_run_news_cannot_settle_blocks_both_verdicts(self):
        self.assertEqual(decided((run(), run(settled=False), run())),
                         (HUMAN, (triage.NEWS_CANT_SETTLE,)))
        self.assertEqual(
            decided((run("possible"), run("possible", settled=False), run("possible"))),
            (HUMAN, ("0 of 3 runs near impossible", triage.NEWS_CANT_SETTLE)))

    def test_a_fact_pointing_toward_yes_blocks_both_verdicts(self):
        brief = briefed(DENIAL, HEARING, fact("NASA found a microbe", True))
        self.assertEqual(decided((run(),) * 3, brief),
                         (HUMAN, ("recent news: Congress held a UAP hearing",
                                  "recent news: NASA found a microbe")))
        self.assertEqual(decided((run("possible"),) * 3, brief),
                         (HUMAN, ("0 of 3 runs near impossible",
                                  "recent news: Congress held a UAP hearing",
                                  "recent news: NASA found a microbe")))

    def test_a_fact_pointing_away_from_yes_changes_neither_verdict(self):
        away = briefed(DENIAL, fact("The hearing was cancelled"))
        self.assertEqual(decided((run(),) * 3, away), (NEAR, ()))
        self.assertEqual(decided((run("possible"),) * 3, away), (QUIET, ()))

    def test_a_voided_near_impossible_run_says_why_ahead_of_what_blocked_and_the_end_date(self):
        pathless = triage.JudgeRun("near_impossible", False, (), "dup")
        self.assertEqual(
            decided((run(), run(quote_ok=False), pathless), briefed(HEARING), days_left=90),
            (HUMAN, ("1 of 3 runs near impossible", "run 2 quoted words not in the rules",
                     "run 3 listed no way to YES", triage.NEWS_CANT_SETTLE,
                     "recent news: Congress held a UAP hearing",
                     "ends in 90 days, past the 45-day quiet window")))

    def test_no_runs_need_a_human(self):
        self.assertEqual(triage.decide((), briefed(), 10).verdict, HUMAN)

    def test_the_screen_keeps_the_runs_and_the_brief_it_was_decided_from(self):
        runs, brief = (run(), run("unsure"), run()), briefed(DENIAL)
        screen = triage.decide(runs, brief, 10)
        self.assertEqual((screen.runs, screen.brief), (runs, brief))


class ResultTest(unittest.TestCase):
    def test_a_screen_survives_a_json_round_trip(self):
        brief = triage.Brief(("a", "b"), "Where it stands.", (
            triage.Fact("t", "q", "https://a.org", "2026-05-01", True, True),
            triage.Fact("u", "r", "https://b.org", None, False, None)))
        for screen in (triage.decide((run(),) * 3, brief, 100),
                       triage.decide((run("possible"),) * 3, briefed(), 30),
                       triage.RULES_UNAVAILABLE):
            with self.subTest(screen.verdict):
                stored = json.loads(json.dumps(triage.to_result(screen)))
                self.assertEqual(triage.from_result(stored), screen)


def digest_of(brief=briefed(), runs=(run(),) * 3, days_left=None):
    return triage.digest(triage.decide(runs, brief, days_left), days_left)


def path(text, realistic=False, quote_ok=True):
    return triage.YesPath(text, "a quote", realistic, quote_ok)


def answering(*answers):
    return tuple(run(a) for a in answers)


class DigestTest(unittest.TestCase):
    def test_facts_are_newest_first_and_undated_last_whichever_way_they_point(self):
        d = digest_of(briefed(fact("undated", True), fact("may", date="2026-05-01"),
                              fact("june", True, date="2026-06-01")))
        self.assertEqual([(f.text, f.toward_yes) for f in d.facts],
                         [("june", True), ("may", False), ("undated", True)])

    def test_paths_merge_across_runs_counting_runs_and_realistic_ones(self):
        runs = (triage.JudgeRun("possible", True, (
                    path("A rare path"), path("The President says so", True),
                    path("the president  SAYS so", True)), "r"),
                triage.JudgeRun("possible", True, (
                    path("The President says so", quote_ok=False), path("Congress acts")), "r"),
                triage.JudgeRun("possible", True, (
                    path("Congress acts", True), path("The president says so")), "r"))
        self.assertEqual(digest_of(runs=runs).paths, (
            triage.PathSummary("The President says so", 3, 1, True),
            triage.PathSummary("Congress acts", 2, 1, False),
            triage.PathSummary("A rare path", 1, 0, False)))

    def test_paths_listed_by_as_many_runs_rank_the_more_realistic_first(self):
        runs = (triage.JudgeRun("possible", True, (path("X"), path("Y", True)), "r"),)
        self.assertEqual([p.path for p in digest_of(runs=runs).paths], ["Y", "X"])

    def test_a_near_impossible_headline_counts_the_runs_and_names_the_news_search(self):
        self.assertEqual(digest_of(briefed(DENIAL)).headline,
                         "Near impossible: 3/3 runs found YES near impossible given the news "
                         "search.")

    def test_a_quiet_headline_says_when_the_market_ends_if_known(self):
        screen = triage.decide(answering("possible", "unsure", "possible"), briefed(DENIAL), 12)
        for days, ends in ((12, ", ends in 12 days"), (None, "")):
            with self.subTest(days=days):
                self.assertEqual(triage.digest(screen, days).headline,
                                 f"Quiet: nothing in the news search points to YES{ends}.")

    def test_a_human_headline_counts_the_facts_toward_yes_else_gives_the_first_reason(self):
        other = fact("NASA found a microbe", True)
        for brief, headline in ((briefed(HEARING, DENIAL), "1 news fact points toward YES."),
                                (briefed(HEARING, DENIAL, other),
                                 "2 news facts point toward YES.")):
            with self.subTest(headline):
                self.assertEqual(digest_of(brief).headline, "Needs human: " + headline)
        split = answering("near_impossible", "possible", "unsure")
        for screen, headline in (
                (triage.decide(split, briefed(DENIAL), 80), "1 of 3 runs near impossible."),
                (triage.decide((run(settled=False),) * 3, briefed(), 80),
                 triage.NEWS_CANT_SETTLE + "."),
                (triage.RULES_UNAVAILABLE, "rules unavailable.")):
            with self.subTest(headline):
                self.assertEqual(triage.digest(screen, 80).headline, "Needs human: " + headline)

    def test_also_lists_the_reasons_the_headline_and_the_facts_do_not_already_give(self):
        split = (run(), run("possible"), run(settled=False))
        self.assertEqual(digest_of(briefed(DENIAL), split, 80).also,
                         (triage.NEWS_CANT_SETTLE, "ends in 80 days, past the 45-day quiet window"))
        self.assertEqual(digest_of(briefed(HEARING, DENIAL), split, 80).also,
                         ("2 of 3 runs near impossible", triage.NEWS_CANT_SETTLE,
                          "ends in 80 days, past the 45-day quiet window"))
        self.assertEqual(digest_of(briefed(HEARING)).also, ())
        self.assertEqual(digest_of().also, ())

    def test_agreement_puts_the_most_common_answer_first_out_of_all_runs(self):
        for answers, agreement in (
                (("possible", "near_impossible", "near_impossible"),
                 "near impossible 2 of 3, possible 1"),
                (("unsure", "possible", "near_impossible"),
                 "near impossible 1 of 3, possible 1, unsure 1"),
                (("possible",) * 3, "possible 3 of 3"), ((), "")):
            with self.subTest(answers):
                self.assertEqual(digest_of(runs=answering(*answers)).agreement, agreement)


class QuoteInTest(unittest.TestCase):
    def test_ignores_quote_and_dash_styles_spacing_case_and_edge_punctuation(self):
        for quote in ('resolve to "Yes" if', "THE US   government\nofficially confirms",
                      "ET - a statement by the President counts.",
                      '"the existence of extraterrestrial life before December 31, 2026,"'):
            with self.subTest(quote):
                self.assertTrue(triage.quote_in(quote, RULES))

    def test_an_ellipsis_elides_words_between_parts_found_in_order(self):
        for quote in ("resolve to \"Yes\" if the US government ... before December 31, 2026",
                      "officially confirms… a statement by the President counts"):
            with self.subTest(quote):
                self.assertTrue(triage.quote_in(quote, RULES))
        for quote in ("a statement by the President counts ... the US government officially",
                      "the US government ... the Pope officially confirms"):
            with self.subTest(quote):
                self.assertFalse(triage.quote_in(quote, RULES))

    def test_one_slipped_word_per_ten_is_tolerated(self):
        self.assertTrue(triage.quote_in(
            "if the US government formally confirms the existence of extraterrestrial life", RULES))
        self.assertFalse(triage.quote_in(
            "if the US military formally confirms the existence of extraterrestrial life", RULES))
        self.assertFalse(triage.quote_in("the US government formally confirms", RULES))

    def test_a_paraphrase_a_scrap_or_nothing_is_not_found(self):
        for quote in ("the government confirms that aliens exist", "", ' ". ',
                      "Yes ... No", "the US"):
            with self.subTest(quote):
                self.assertFalse(triage.quote_in(quote, RULES))


PAGE = ("<html><head><title>UAP</title><style>p { color: red }</style>"
        "<script>var s = 'The files show no evidence.'</script></head>"
        "<body><h1>Files</h1><p>The files <b>show</b> no&nbsp;evidence.</p></body></html>")


class VerifyBriefsTest(unittest.TestCase):
    def test_page_text_keeps_what_a_reader_sees(self):
        text = triage.page_text(PAGE)
        self.assertTrue(triage.quote_in("The files show no evidence.", text))
        self.assertNotIn("var s", text)
        self.assertNotIn("color", text)
        blocks = triage.page_text("<h1>Files</h1><p>The files<br>today</p>")
        self.assertNotIn("FilesThe", blocks)
        self.assertNotIn("filestoday", blocks)

    def test_marks_each_fact_found_not_found_or_unreadable_in_every_markets_brief(self):
        pages = {"https://a": triage.page_text(PAGE), "https://b": "Something else entirely."}

        def fetch(url):
            if url not in pages:
                raise ConnectionError("unreachable")
            return pages[url]
        facts = tuple(triage.Fact("t", "The files show no evidence.", url, None, False, None)
                      for url in ("https://a", "https://b", "https://c"))
        toward = tuple(f._replace(toward_yes=True) for f in facts)
        first, second = triage.verify_briefs((briefed(*facts), briefed(*toward)), fetch)
        for brief in (first, second):
            self.assertEqual([f.verified for f in brief.facts], [True, False, None])
        self.assertEqual([f.toward_yes for f in second.facts], [True] * 3)
        self.assertEqual(first._replace(facts=()), briefed())

    def test_reads_a_page_once_however_many_facts_and_markets_cite_it(self):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return "The files show no evidence. The hearing is set for June."
        facts = (triage.Fact("t", "The files show no evidence.", "https://a", None, True, None),
                 triage.Fact("u", "The hearing is set for July.", "https://a", None, False, None),
                 triage.Fact("v", "The files show no evidence.", "https://b", None, False, None))
        briefs = triage.verify_briefs((briefed(*facts),) * 4, fetch)
        self.assertEqual(sorted(fetched), ["https://a", "https://b"])
        self.assertEqual([[f.verified for f in b.facts] for b in briefs],
                         [[True, False, True]] * 4)

    def test_circular_facts_cite_a_market(self):
        self.assertTrue(fact("t", url="https://polymarket.com/event/x").circular)
        self.assertTrue(fact("t", url="https://gamma-api.polymarket.com/m").circular)
        self.assertFalse(fact("t", url="https://www.reuters.com/a").circular)
        self.assertFalse(fact("t", url="https://notpolymarket.com/a").circular)


class BuildPromptTest(unittest.TestCase):
    def test_brief_prompt_numbers_the_markets_and_carries_the_event_the_rules_and_the_window(self):
        prompt = triage.build_brief_prompt("Alien disclosure in 2026", "2026-12-31", RULES,
                                           [QUESTION, FILES], date(2026, 10, 9))
        for needle in ("Today is 2026-10-09.", "Event: Alien disclosure in 2026",
                       "End date: 2026-12-31", RULES, f"1. {QUESTION}\n2. {FILES}\n",
                       "from 2025-10-01 through 2026-10-09", "Do not estimate a probability",
                       "sites are not news"):
            self.assertIn(needle, prompt)

    def test_brief_prompt_asks_for_quotes_from_the_fetched_page_and_for_who_each_fact_helps(self):
        prompt = triage.build_brief_prompt("Alien disclosure in 2026", "2026-12-31", RULES,
                                           [QUESTION, FILES], date(2026, 10, 9))
        for needle in ("Open every page you cite with WebFetch",
                       "never from a search result's summary",
                       "toward_yes: the numbers of the listed markets for which this fact opens, "
                       "advances or makes more plausible a way to YES"):
            self.assertIn(needle, prompt)

    def test_judge_prompt_carries_the_market_the_state_and_each_fact_with_where_it_points(self):
        brief = triage.Brief(("s",), "The hearing is set for June.", (
            triage.Fact("Congress set a hearing", "q", "https://a.org/x", "2026-05-01", True, None),
            triage.Fact("The Pentagon denied it", "q", "https://b.org/y", None, False, False)))
        prompt = triage.build_judge_prompt(QUESTION, "2026-12-31", RULES, brief,
                                           date(2026, 10, 9))
        for needle in ("Today is 2026-10-09. Your training data may be older than today.",
                       f"Question: {QUESTION}", "End date: 2026-12-31",
                       f"Rules, verbatim:\n{RULES}", "The hearing is set for June.",
                       "- 2026-05-01, toward YES: Congress set a hearing <https://a.org/x>",
                       "- undated, not toward YES: The Pentagon denied it <https://b.org/y>",
                       "settled_by_news", "When in doubt, answer unsure. A wrong near_impossible "
                       "loses money; unsure costs nothing.",
                       "a price, index, rate or\n   other figure that can cross its threshold"):
            self.assertIn(needle, prompt)
        self.assertNotIn("assuming nothing unusual has happened", prompt)

    def test_judge_prompt_says_when_the_search_found_no_facts(self):
        prompt = triage.build_judge_prompt(QUESTION, "2026-12-31", RULES, briefed(),
                                           date(2026, 10, 9))
        self.assertIn(f"{STATE}\nFacts it found:\n- none\n", prompt)


class ParseTest(unittest.TestCase):
    def test_judge_checks_each_rule_quote_against_the_rules(self):
        made_up = {"path": "Aliens land", "rule_quote": "if aliens land on the lawn",
                   "realistic": True}
        unquoted = {"path": "A loose reading", "rule_quote": "", "realistic": False}
        r = triage.parse_judge(envelope(judge_output(paths=(PATH, made_up, unquoted))), RULES)
        self.assertEqual((r.answer, r.settled_by_news, r.reason),
                         ("near_impossible", True, "Absurd."))
        self.assertEqual([(p.path, p.realistic, p.quote_ok) for p in r.yes_paths],
                         [(PATH["path"], False, True), ("Aliens land", True, False),
                          ("A loose reading", False, True)])

    def test_malformed_judge_output_raises_triage_error(self):
        cases = {
            "unknown answer": envelope(judge_output(answer="likely")),
            "no answer": envelope({k: v for k, v in judge_output().items() if k != "answer"}),
            "settled_by_news not a bool": envelope({**judge_output(), "settled_by_news": "yes"}),
            "yes_paths not a list": envelope({**judge_output(), "yes_paths": PATH}),
            "path without a quote": envelope(judge_output(paths=({"path": "x",
                                                                   "realistic": True},))),
            "realistic not a bool": envelope(judge_output(paths=({**PATH, "realistic": 0},))),
            "no reason": envelope({k: v for k, v in judge_output().items() if k != "reason"}),
            "no structured_output": envelope(None),
            "is_error": envelope(judge_output(), is_error=True, subtype="error_max_turns"),
            "not JSON": "Error: not logged in",
        }
        for name, output in cases.items():
            with self.subTest(name), self.assertRaises(triage.TriageError):
                triage.parse_judge(output, RULES)

    def test_brief_gives_each_market_the_same_facts_pointing_its_own_way(self):
        facts = ({**FACT, "toward_yes": [1, 3]}, {**FACT, "text": "A denial", "date": ""},
                 {**FACT, "text": "A leak", "toward_yes": [2]})
        briefs = triage.parse_brief(envelope(brief_output(facts)), 3)
        self.assertEqual([[f.toward_yes for f in b.facts] for b in briefs],
                         [[True, False, False], [False, False, True], [True, False, False]])
        for b in briefs:
            self.assertEqual((b.searches, b.state),
                             (("US government confirms aliens 2026",), STATE))
            self.assertEqual([(f.text, f.quote, f.url, f.date, f.verified) for f in b.facts], [
                (FACT["text"], FACT["quote"], FACT["url"], "2026-05-01", None),
                ("A denial", FACT["quote"], FACT["url"], None, None),
                ("A leak", FACT["quote"], FACT["url"], "2026-05-01", None)])

    def test_malformed_brief_output_raises_triage_error(self):
        cases = {
            "state not a string": envelope({**brief_output(), "state": 1}),
            "no state": envelope({k: v for k, v in brief_output().items() if k != "state"}),
            "searches not strings": envelope({**brief_output(), "searches": [1]}),
            "facts not a list": envelope({**brief_output(), "facts": FACT}),
            "fact without a url": envelope(brief_output(({"text": "t", "quote": "q",
                                                          "toward_yes": []},))),
            "date not a string": envelope(brief_output(({**FACT, "date": 2026},))),
            "no toward_yes": envelope(brief_output(
                ({k: v for k, v in FACT.items() if k != "toward_yes"},))),
            "toward_yes not a list": envelope(brief_output(({**FACT, "toward_yes": 1},))),
            "toward_yes not numbers": envelope(brief_output(({**FACT, "toward_yes": ["1"]},))),
            "toward_yes a bool": envelope(brief_output(({**FACT, "toward_yes": [True]},))),
            "toward_yes before the first market": envelope(
                brief_output(({**FACT, "toward_yes": [0]},))),
            "toward_yes past the last market": envelope(
                brief_output(({**FACT, "toward_yes": [3]},))),
        }
        for name, output in cases.items():
            with self.subTest(name), self.assertRaises(triage.TriageError):
                triage.parse_brief(output, 2)

    def test_is_error_says_why(self):
        with self.assertRaisesRegex(triage.TriageError, "error_max_turns.*ran out"):
            triage.parse_brief(envelope(brief_output(), is_error=True,
                                        subtype="error_max_turns", result="ran out"), 1)


class RunClaudeTest(unittest.TestCase):
    def run_with(self, step, result):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(triage.shutil, "which", return_value=r"C:\bin\claude.EXE"), \
                mock.patch.object(triage.subprocess, "run", fake_run):
            return triage.run_claude(step, "the prompt"), calls

    def test_the_judge_runs_without_tools_and_the_prompt_goes_on_stdin(self):
        done = subprocess.CompletedProcess([], 0, envelope(judge_output()), "")
        out, ((argv, kw),) = self.run_with(triage.JUDGE, done)
        self.assertEqual(out, envelope(judge_output()))
        self.assertEqual(argv[:2], [r"C:\bin\claude.EXE", "-p"])
        flags = dict(zip(argv, argv[1:]))
        self.assertEqual((flags["--model"], flags["--output-format"], flags["--tools"]),
                         ("sonnet", "json", ""))
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(json.loads(flags["--json-schema"]), triage.JUDGE_SCHEMA)
        self.assertEqual(flags["--system-prompt"], triage.JUDGE.system)
        for flag in ("--no-session-persistence", "--strict-mcp-config",
                     "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertNotIn("--bare", argv)
        self.assertNotIn("the prompt", argv)
        self.assertEqual((kw["input"], kw["timeout"]), ("the prompt", triage.JUDGE.timeout_s))
        self.assertNotEqual(os.path.abspath(kw["cwd"]), os.getcwd())

    def test_the_brief_runs_with_web_search_and_fetch_and_longer_to_finish(self):
        done = subprocess.CompletedProcess([], 0, envelope(brief_output()), "")
        _, ((argv, kw),) = self.run_with(triage.BRIEF, done)
        flags = dict(zip(argv, argv[1:]))
        self.assertEqual((flags["--tools"], flags["--allowedTools"]),
                         ("WebSearch,WebFetch", "WebSearch,WebFetch"))
        self.assertEqual(json.loads(flags["--json-schema"]), triage.BRIEF_SCHEMA)
        self.assertEqual(kw["timeout"], triage.BRIEF.timeout_s)
        self.assertGreater(triage.BRIEF.timeout_s, triage.JUDGE.timeout_s)

    def test_timeout_and_silent_failure_raise_triage_error(self):
        quick = triage.BRIEF._replace(timeout_s=30)
        with self.assertRaisesRegex(triage.TriageError, r"\(brief\) did not finish within 30s"):
            self.run_with(quick, subprocess.TimeoutExpired(["claude"], 30))
        with self.assertRaisesRegex(triage.TriageError, "exited 1: not logged in"):
            self.run_with(triage.JUDGE, subprocess.CompletedProcess([], 1, "", "not logged in\n"))

    def test_no_cli_raises_triage_error(self):
        with mock.patch.object(triage.shutil, "which", return_value=None), \
                self.assertRaisesRegex(triage.TriageError, "not on PATH"):
            triage.run_claude(triage.JUDGE, "p")


EVENT = {"event_slug": "alien-disclosure-2026", "event_title": "Alien disclosure in 2026"}
BRIEF_COST, JUDGE_COST = 0.36, 0.02


def metrics(**event):
    return {"book_mid": 0.94, "volume24hr": 130.0, "est_daily_usd": 0.22, "net_apr": 0.165,
            "plan": {"fav_bid_price": 0.9},
            "market": {"fav_outcome": "No", "rate_per_day": 1.0, **event}}


class StubClaude:
    """run: brief for the brief call and, for a judge call, the answer its
    question has in answers, else a near-impossible one. An exception is
    raised instead of answered."""

    def __init__(self, brief=None, answers=None):
        self.brief = brief if brief is not None else brief_output()
        self.answers = answers or {}
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, step, prompt):
        with self._lock:
            self.calls.append((step.name, prompt))
        if step is triage.BRIEF:
            answer, cost = self.brief, BRIEF_COST
        else:
            question = re.search(r"Question: (.+)", prompt).group(1)
            answer, cost = self.answers.get(question, judge_output()), JUDGE_COST
        if isinstance(answer, Exception):
            raise answer
        return envelope(answer, total_cost_usd=cost)

    def prompts(self, step):
        return [prompt for name, prompt in self.calls if name == step]


def rules(text=RULES, **by_id):
    """fetch_rules: text for every market but those in by_id, which get their
    own text or raise their exception."""
    def fetch(cid):
        found = by_id.get(cid, text)
        if isinstance(found, Exception):
            raise found
        return Rules(found, "https://www.whitehouse.gov", None)
    return fetch


class Markets(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = farm_db.open_db(os.path.join(tmp.name, "farm.db"))
        self.addCleanup(self.db.close)

    def add(self, cid, question, end=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
            rank=2.0, **event):
        farm_db.store_scan(self.db, [farm_db.ScanRow(
            cid, cid[2:], question, end and end.isoformat(), rank,
            json.dumps(metrics(**event)))], [cid], NOW - timedelta(days=1))


class ReadBatchesTest(Markets):
    def batches(self, *ids):
        return [[m.condition_id for m in batch] for batch in triage.read_batches(self.db, ids)]

    def test_markets_of_an_event_batch_together_in_the_order_given(self):
        self.add("0xa", "a?", rank=1.0, **EVENT)
        self.add("0xb", "b?", rank=9.0, event_slug="other", event_title="Other")
        self.add("0xc", "c?", rank=5.0, **EVENT)
        self.add("0xd", "d?", rank=7.0, event_slug="other", event_title="Other")
        self.assertEqual(self.batches("0xc", "0xb", "0xa", "0xd"),
                         [["0xc", "0xa"], ["0xb", "0xd"]])

    def test_a_market_without_an_event_is_a_batch_of_its_own(self):
        self.add("0xa", "a?")
        self.add("0xb", "b?", **EVENT)
        self.add("0xc", "c?")
        self.assertEqual(self.batches("0xa", "0xb", "0xc"), [["0xa"], ["0xb"], ["0xc"]])

    def test_an_event_with_more_markets_than_a_batch_holds_splits(self):
        ids = [f"0x{i:02}" for i in range(2 * triage.EVENT_MARKETS + 1)]
        for cid in ids:
            self.add(cid, f"{cid}?", **EVENT)
        self.assertEqual(triage.EVENT_MARKETS, 8)
        self.assertEqual(self.batches(*ids), [ids[:8], ids[8:16], ids[16:]])

    def test_an_unknown_id_is_left_out(self):
        self.add("0xa", "a?")
        self.assertEqual(self.batches("0xnope", "0xa"), [["0xa"]])
        self.assertEqual(self.batches(), [])


class ScreenAndStoreTest(Markets):
    """0xali and 0xufo share an event; 0xsolo has none."""

    def setUp(self):
        super().setUp()
        self.add("0xali", QUESTION, **EVENT)
        self.add("0xufo", FILES, **EVENT)
        self.add("0xsolo", "Will it rain on Mars?")
        self.pages = {}
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        return self.pages[url]

    def screen(self, claude, ids=("0xali", "0xufo"), fetch_rules=rules()):
        return triage.screen_and_store(self.db, ids, NOW, fetch_rules, claude, self.fetch)

    def test_one_brief_then_three_judge_runs_a_market_store_each_markets_screen(self):
        claude = StubClaude()
        ufo, ali = self.screen(claude, ("0xufo", "0xali"))
        self.assertEqual(sorted(name for name, _ in claude.calls), ["brief"] + ["judge"] * 6)
        self.assertEqual(claude.calls[0][0], "brief")
        self.assertEqual((ufo.condition_id, ali.condition_id), ("0xufo", "0xali"))
        for row in (ufo, ali):
            self.assertEqual((row.status, row.verdict, row.book_mid, row.model, row.at),
                             ("ok", "near_impossible", 0.94, "sonnet", NOW))
            s = triage.from_result(row.result)
            self.assertEqual((s.verdict, s.why, len(s.runs)), (NEAR, (), 3))
            self.assertEqual((s.brief.searches, s.brief.state),
                             (("US government confirms aliens 2026",), STATE))
            self.assertEqual(triage.to_result(s), row.result)
        self.assertEqual(farm_db.triaged_ids(self.db), {"0xali", "0xufo"})

    def test_a_market_pays_its_judge_runs_and_an_even_share_of_the_brief(self):
        for row in self.screen(StubClaude()):
            self.assertAlmostEqual(row.cost_usd, BRIEF_COST / 2 + 3 * JUDGE_COST)
        solo, = self.screen(StubClaude(), ("0xsolo",))
        self.assertAlmostEqual(solo.cost_usd, BRIEF_COST + 3 * JUDGE_COST)

    def test_the_rows_of_a_batch_took_as_long_as_the_batch(self):
        ali, ufo = self.screen(StubClaude())
        self.assertEqual(ali.duration_s, ufo.duration_s)
        self.assertGreaterEqual(ali.duration_s, 0)

    def test_the_brief_is_asked_about_the_event_and_each_judge_about_its_own_market(self):
        claude = StubClaude(brief_output(({**FACT, "toward_yes": [2]},), "Files are due in June."))
        self.pages[FACT["url"]] = "elsewhere"
        self.screen(claude)
        asked, = claude.prompts("brief")
        for needle in ("Event: Alien disclosure in 2026", "End date: 2026-12-31", RULES,
                       f"1. {QUESTION}\n2. {FILES}\n"):
            self.assertIn(needle, asked)
        judged = claude.prompts("judge")
        for question, points in ((QUESTION, "not toward YES"), (FILES, "toward YES")):
            mine = [p for p in judged if f"Question: {question}" in p]
            self.assertEqual(len(mine), 3)
            for prompt in mine:
                self.assertIn("Files are due in June.", prompt)
                self.assertIn(f"- 2026-05-01, {points}: The Pentagon released UAP files "
                              "<https://example.org/uap>", prompt)

    def test_a_market_with_no_event_is_briefed_under_its_own_question(self):
        claude = StubClaude()
        self.screen(claude, ("0xsolo",))
        self.assertIn("Event: Will it rain on Mars?", claude.prompts("brief")[0])

    def test_prompts_carry_today_and_the_rules_and_never_the_price(self):
        claude = StubClaude()
        self.screen(claude)
        for step, prompt in claude.calls:
            with self.subTest(step):
                self.assertIn("Today is 2026-10-09.", prompt)
                self.assertIn(RULES, prompt)
                self.assertIn("End date: 2026-12-31", prompt)
                for leak in ("94", "16.5", "0.22", "130", "0.9", "APR", "midpoint", "priced at"):
                    self.assertNotIn(leak, prompt)

    def test_a_fact_toward_yes_for_one_market_needs_a_human_there_and_nowhere_else(self):
        self.pages[FACT["url"]] = "elsewhere"
        ali, ufo = self.screen(StubClaude(brief_output(({**FACT, "toward_yes": [2]},))))
        self.assertEqual((ali.verdict, triage.from_result(ali.result).why),
                         ("near_impossible", ()))
        self.assertEqual((ufo.verdict, triage.from_result(ufo.result).why),
                         ("needs_human", ("recent news: The Pentagon released UAP files",)))
        self.assertEqual([[f.toward_yes for f in triage.from_result(r.result).brief.facts]
                          for r in (ali, ufo)], [[False], [True]])

    def test_a_fact_pointing_away_from_yes_leaves_a_quiet_market_quiet(self):
        self.add("0xali", QUESTION, NOW + timedelta(days=triage.QUIET_DAYS), **EVENT)
        self.pages[FACT["url"]] = "elsewhere"
        claude = StubClaude(brief_output((FACT,)), {QUESTION: judge_output("possible")})
        ali, ufo = self.screen(claude)
        self.assertEqual((ali.verdict, triage.from_result(ali.result).why), ("quiet", ()))
        self.assertEqual(ufo.verdict, "near_impossible")
        self.assertEqual(sum("End date: 2026-11-23" in p for p in claude.prompts("judge")), 3)

    def test_a_possible_market_past_the_quiet_window_needs_a_human(self):
        ali, _ = self.screen(StubClaude(answers={QUESTION: judge_output("possible")}))
        self.assertEqual((ali.verdict, triage.from_result(ali.result).why),
                         ("needs_human", ("0 of 3 runs near impossible",
                                          "ends in 83 days, past the 45-day quiet window")))

    def test_an_unknown_end_date_is_told_to_claude_and_needs_a_human_unless_near_impossible(self):
        self.add("0xali", QUESTION, None, **EVENT)
        claude = StubClaude(answers={QUESTION: judge_output("unsure")})
        ali, = self.screen(claude, ("0xali",))
        self.assertIn("End date: unknown", claude.prompts("judge")[0])
        self.assertEqual(triage.from_result(ali.result).why,
                         ("0 of 3 runs near impossible", "end date unknown"))

    def test_each_cited_page_is_read_once_and_the_check_reaches_every_markets_screen(self):
        self.pages = {FACT["url"]: triage.page_text(PAGE), "https://example.org/b": "elsewhere"}
        facts = (FACT, {**FACT, "text": "A second reading", "toward_yes": [1]},
                 {**FACT, "url": "https://example.org/b"},
                 {**FACT, "url": "https://example.org/gone"})
        rows = self.screen(StubClaude(brief_output(facts)))
        self.assertEqual(sorted(self.fetched), ["https://example.org/b",
                                                "https://example.org/gone", FACT["url"]])
        for row in rows:
            self.assertEqual([f.verified for f in triage.from_result(row.result).brief.facts],
                             [True, True, False, None])

    def test_blank_rules_need_a_human_and_stay_out_of_the_brief(self):
        claude = StubClaude()
        ali, ufo = self.screen(claude, fetch_rules=rules(**{"0xali": "  "}))
        blank = triage.from_result(ali.result)
        self.assertEqual((ali.status, ali.verdict, blank.why, blank.brief, ali.cost_usd),
                         ("ok", "needs_human", ("rules unavailable",), triage.Brief((), "", ()),
                          None))
        asked, = claude.prompts("brief")
        self.assertIn(f"1. {FILES}\n", asked)
        self.assertNotIn(QUESTION, asked)
        self.assertEqual(len(claude.prompts("judge")), 3)
        self.assertEqual(ufo.verdict, "near_impossible")
        self.assertAlmostEqual(ufo.cost_usd, BRIEF_COST + 3 * JUDGE_COST)

    def test_a_batch_of_blank_rules_never_asks_claude(self):
        claude = StubClaude()
        rows = self.screen(claude, fetch_rules=rules(""))
        self.assertEqual(claude.calls, [])
        self.assertEqual([r.verdict for r in rows], ["needs_human"] * 2)

    def test_a_failed_rules_fetch_is_that_markets_error_alone(self):
        claude = StubClaude()
        ali, ufo = self.screen(claude, fetch_rules=rules(
            **{"0xali": LookupError("Gamma has no record of this market")}))
        self.assertEqual((ali.status, ali.verdict, ali.result, ali.error),
                         ("error", None, None, "LookupError: Gamma has no record of this market"))
        self.assertEqual((ufo.status, ufo.verdict), ("ok", "near_impossible"))
        self.assertNotIn(QUESTION, claude.prompts("brief")[0])
        self.assertEqual(farm_db.triaged_ids(self.db), {"0xufo"})

    def test_a_failed_brief_is_the_error_of_every_market_it_covered_and_of_no_other(self):
        failures = {
            "the call": (triage.TriageError("claude (brief) did not finish within 900s"),
                         "TriageError: claude (brief) did not finish within 900s"),
            "its output": ({**brief_output(), "state": 1}, "TriageError: state 1 is not a str"),
        }
        self.add("0xblank", "Will the files name a planet?", **EVENT)
        for name, (brief, error) in failures.items():
            with self.subTest(name):
                claude = StubClaude(brief)
                ali, ufo, blank = self.screen(claude, ("0xali", "0xufo", "0xblank"),
                                              rules(**{"0xblank": ""}))
                for row in (ali, ufo):
                    self.assertEqual((row.status, row.verdict, row.result, row.error),
                                     ("error", None, None, error))
                self.assertEqual((blank.status, blank.verdict), ("ok", "needs_human"))
                self.assertEqual(claude.prompts("judge"), [])

    def test_a_failed_judge_is_that_markets_error_alone(self):
        failures = {
            "a call": (triage.TriageError("claude (judge) did not finish within 420s"),
                       "TriageError: claude (judge) did not finish within 420s"),
            "its output": (judge_output(answer="likely"),
                           "TriageError: answer 'likely' is not one of "
                           "('near_impossible', 'possible', 'unsure')"),
        }
        for name, (answer, error) in failures.items():
            with self.subTest(name):
                ali, ufo = self.screen(StubClaude(answers={QUESTION: answer}))
                self.assertEqual((ali.status, ali.verdict, ali.result, ali.error),
                                 ("error", None, None, error))
                self.assertEqual((ufo.status, ufo.verdict), ("ok", "near_impossible"))
                self.assertAlmostEqual(ufo.cost_usd, BRIEF_COST / 2 + 3 * JUDGE_COST)

    def test_a_batchs_judge_calls_run_six_at_a_time(self):
        ids = [f"0xm{i}" for i in range(triage.EVENT_MARKETS)]
        for cid in ids:
            self.add(cid, f"Market {cid}?", **EVENT)
        claude, six, lock = StubClaude(), threading.Barrier(6, timeout=5), threading.Lock()
        busy = []
        peak = []

        def run(step, prompt):
            if step is triage.JUDGE:
                with lock:
                    busy.append(1)
                    peak.append(len(busy))
                try:
                    six.wait()
                finally:
                    with lock:
                        busy.pop()
            return claude(step, prompt)
        rows = self.screen(run, ids)
        self.assertEqual([r.verdict for r in rows], ["near_impossible"] * len(ids))
        self.assertEqual(max(peak), 6)


if __name__ == "__main__":
    unittest.main()
