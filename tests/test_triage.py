import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from farm import farm_db, triage
from farm_console.remote import Rules

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
QUESTION = "Will the US confirm that aliens exist before 2027?"
RULES = ("This market will resolve to \u201cYes\u201d if the US government officially "
         "confirms the existence of extraterrestrial life before December 31, 2026, 11:59 PM "
         "ET \u2014 a statement by the President counts. Otherwise it resolves to \u201cNo\u201d.")
PATH = {"path": "The President confirms aliens exist",
        "rule_quote": "a statement by the President counts", "realistic": False}
NEWS_ITEM = {"text": "The Pentagon released UAP files", "quote": "The files show no evidence.",
             "url": "https://example.org/uap", "date": "2026-05-01"}


def background(answer="near_impossible", settled=True, paths=(PATH,), reason="Absurd."):
    return {"yes_paths": list(paths), "settled_by_news": settled, "answer": answer,
            "reason": reason}


def news(found=False, items=(), summary="Nothing new."):
    return {"searches": ["US government confirms aliens 2026"], "found": found,
            "items": list(items), "summary": summary}


def envelope(structured, **changes):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "num_turns": 1, "total_cost_usd": 0.04, "duration_ms": 26000,
                       "structured_output": structured, **changes})


def run(answer="near_impossible", settled=True, quote_ok=True):
    return triage.BackgroundRun(answer, settled,
                                (triage.YesPath("a path", "a quote", False, quote_ok),), "r")


CLEAN_CHECK = triage.NewsCheck(("a search",), False, (), "Nothing new.")
CLEAN = (CLEAN_CHECK, CLEAN_CHECK)
HEARING = triage.NewsItem("Congress held a UAP hearing", "q", "https://x", None, None)
NEAR = triage.Route(triage.Verdict.NEAR_IMPOSSIBLE)
QUIET = triage.Route(triage.Verdict.QUIET)


def human(*why):
    return triage.Route(triage.Verdict.NEEDS_HUMAN, why)


class BackgroundRouteTest(unittest.TestCase):
    def test_unanimous_near_impossible_runs_route_near_impossible_whatever_the_end_date(self):
        for days in (None, 10, 400):
            with self.subTest(days=days):
                self.assertEqual(triage.background_route((run(),) * 3, days), NEAR)

    def test_possible_or_unsure_runs_news_can_settle_ending_within_45_days_route_quiet(self):
        for days in (0, 30, triage.QUIET_DAYS):
            with self.subTest(days=days):
                self.assertEqual(triage.background_route(
                    (run("possible"), run("unsure"), run()), days), QUIET)

    def test_rule_quotes_do_not_matter_to_the_quiet_route(self):
        self.assertEqual(triage.background_route(
            (run(quote_ok=False), run("possible", quote_ok=False), run()), 20), QUIET)

    def test_past_the_quiet_window_needs_a_human(self):
        self.assertEqual(triage.background_route((run("possible"),) * 3, triage.QUIET_DAYS + 1),
                         human("background 0 of 3 near impossible",
                               "ends in 46 days, past the 45-day quiet window"))

    def test_an_unknown_end_date_needs_a_human(self):
        self.assertEqual(triage.background_route((run(), run("unsure"), run()), None),
                         human("background 2 of 3 near impossible", "end date unknown"))

    def test_any_run_news_cannot_settle_needs_a_human_on_either_route(self):
        self.assertEqual(triage.background_route((run(), run(settled=False), run()), 10),
                         human(triage.NEWS_CANT_SETTLE))
        self.assertEqual(triage.background_route(
            (run("possible"), run("possible", settled=False), run("possible")), 10),
            human("background 0 of 3 near impossible", triage.NEWS_CANT_SETTLE))

    def test_a_voided_near_impossible_run_says_why_when_it_needs_a_human(self):
        pathless = triage.BackgroundRun("near_impossible", True, (), "dup")
        self.assertEqual(triage.background_route((run(), run(quote_ok=False), pathless), 90),
                         human("background 1 of 3 near impossible",
                               "run 2 quoted words not in the rules",
                               "run 3 listed no way to YES",
                               "ends in 90 days, past the 45-day quiet window"))

    def test_no_runs_need_a_human(self):
        self.assertEqual(triage.background_route((), 10).verdict, triage.Verdict.NEEDS_HUMAN)


class DecideTest(unittest.TestCase):
    def test_clean_news_checks_keep_the_routes_verdict(self):
        for route in (NEAR, QUIET):
            with self.subTest(route.verdict):
                self.assertEqual(triage.decide(route, CLEAN), route)

    def test_a_human_route_stays_with_its_why(self):
        self.assertEqual(triage.decide(human("end date unknown"), CLEAN),
                         human("end date unknown"))

    def test_one_clean_check_and_one_with_an_item_need_a_human(self):
        for route in (NEAR, QUIET):
            with self.subTest(route.verdict):
                self.assertEqual(
                    triage.decide(route, (CLEAN_CHECK, CLEAN_CHECK._replace(items=(HEARING,)))),
                    human("recent news: Congress held a UAP hearing"))

    def test_one_clean_check_and_one_finding_without_items_need_a_human(self):
        found = CLEAN_CHECK._replace(found=True, summary="A hearing.")
        self.assertEqual(triage.decide(QUIET, (CLEAN_CHECK, found)),
                         human("recent news (check 2): A hearing."))

    def test_an_item_both_checks_found_is_listed_once_beside_each_other_item(self):
        other = HEARING._replace(text="NASA found a microbe")
        self.assertEqual(
            triage.decide(NEAR, (CLEAN_CHECK._replace(found=True, items=(HEARING,)),
                                 CLEAN_CHECK._replace(found=True, items=(other, HEARING)))),
            human("recent news: Congress held a UAP hearing",
                  "recent news: NASA found a microbe"))

    def test_fewer_than_two_clean_checks_need_a_human(self):
        for route in (NEAR, QUIET):
            for news in ((), (CLEAN_CHECK,)):
                with self.subTest(route.verdict, checks=len(news)):
                    self.assertEqual(triage.decide(route, news),
                                     human("recent news not checked"))


class ResultTest(unittest.TestCase):
    def test_a_screen_with_two_news_checks_survives_a_json_round_trip(self):
        checks = (triage.NewsCheck(("a", "b"), True, (
                      triage.NewsItem("t", "q", "https://a.org", "2026-05-01", True),
                      triage.NewsItem("u", "r", "https://b.org", None, None)), "One."),
                  triage.NewsCheck(("c",), False, (), "Two."))
        s = triage.Screen(*triage.decide(NEAR, checks), (run(),) * 3, checks)
        self.assertEqual(triage.from_result(json.loads(json.dumps(triage.to_result(s)))), s)
        quiet = triage.Screen(*QUIET, (run("possible"),) * 3, CLEAN)
        self.assertEqual(triage.from_result(json.loads(json.dumps(triage.to_result(quiet)))),
                         quiet)
        skipped = s._replace(news=())
        self.assertEqual(triage.from_result(json.loads(json.dumps(triage.to_result(skipped)))),
                         skipped)


def item(text, url="", date=None, verified=None):
    return triage.NewsItem(text, "q", url, date, verified)


def found(*items):
    return CLEAN_CHECK._replace(found=True, items=items)


def digest_of(*checks, runs=(run(),) * 3, days_left=None):
    return triage.digest(triage.Screen(*triage.decide(NEAR, checks), runs, checks), days_left)


def path(text, realistic=False, quote_ok=True):
    return triage.YesPath(text, "a quote", realistic, quote_ok)


def answering(*answers):
    return tuple(run(a) for a in answers)


class DigestTest(unittest.TestCase):
    def test_a_development_both_checks_report_is_merged_and_verified_if_either_copy_is(self):
        for first, second, verified in ((False, True, True), (None, False, False),
                                        (None, None, None)):
            with self.subTest(first=first, second=second):
                d = digest_of(found(item("Congress held a hearing", "https://a.org/x", None,
                                         first)),
                              found(item("Other", "https://b.org"),
                                    item("A hearing was held", "https://a.org/x", "2026-05-01",
                                         second)))
                self.assertEqual(d.developments[0], triage.Development(
                    "Congress held a hearing", "https://a.org/x", "2026-05-01", verified, (1, 2)))
                self.assertEqual(len(d.developments), 2)

    def test_urls_differing_in_fragment_utm_trailing_slash_or_host_case_merge(self):
        d = digest_of(found(item("a", "https://Example.ORG/news/1/#top"),
                            item("b", "HTTPS://example.org/news/1?utm_source=x&utm_medium=y")),
                      found(item("c", "https://example.org/news/1/?utm_campaign=z#c")))
        self.assertEqual([(v.text, v.checks) for v in d.developments], [("a", (1, 2))])

    def test_distinct_urls_stay_distinct_even_with_the_same_text(self):
        d = digest_of(found(item("Same", "https://example.org/news/1"),
                            item("Same", "https://example.org/news/2"),
                            item("Same", "https://example.org/news/1?id=2")),
                      CLEAN_CHECK)
        self.assertEqual([v.url for v in d.developments],
                         ["https://example.org/news/1", "https://example.org/news/2",
                          "https://example.org/news/1?id=2"])

    def test_items_without_a_web_url_merge_on_their_normalized_text(self):
        d = digest_of(found(item("Congress held a “UAP” hearing", "javascript:x")),
                      found(item('congress  held a "UAP" HEARING'), item("NASA found a microbe")))
        self.assertEqual([(v.text, v.checks) for v in d.developments],
                         [('Congress held a “UAP” hearing', (1, 2)),
                          ("NASA found a microbe", (2,))])

    def test_developments_are_newest_first_and_undated_last(self):
        d = digest_of(found(item("undated", "https://a.org"),
                            item("may", "https://b.org", "2026-05-01"),
                            item("june", "https://c.org", "2026-06-01")), CLEAN_CHECK)
        self.assertEqual([v.text for v in d.developments], ["june", "may", "undated"])

    def test_paths_merge_across_runs_counting_runs_and_realistic_ones(self):
        runs = (triage.BackgroundRun("possible", True, (
                    path("A rare path"), path("The President says so", True),
                    path("the president  SAYS so", True)), "r"),
                triage.BackgroundRun("possible", True, (
                    path("The President says so", quote_ok=False), path("Congress acts")), "r"),
                triage.BackgroundRun("possible", True, (
                    path("Congress acts", True), path("The president says so")), "r"))
        self.assertEqual(digest_of(CLEAN_CHECK, CLEAN_CHECK, runs=runs).paths, (
            triage.PathSummary("The President says so", 3, 1, True),
            triage.PathSummary("Congress acts", 2, 1, False),
            triage.PathSummary("A rare path", 1, 0, False)))

    def test_paths_listed_by_as_many_runs_rank_the_more_realistic_first(self):
        runs = (triage.BackgroundRun("possible", True, (path("X"), path("Y", True)), "r"),)
        self.assertEqual([p.path for p in digest_of(CLEAN_CHECK, CLEAN_CHECK, runs=runs).paths],
                         ["Y", "X"])

    def test_a_near_impossible_headline_counts_runs_and_clean_checks(self):
        self.assertEqual(digest_of(CLEAN_CHECK, CLEAN_CHECK).headline,
                         "Near impossible: 3/3 runs found no realistic way to YES and 2/2 news "
                         "checks found nothing.")

    def test_a_quiet_headline_says_when_the_market_ends_if_known(self):
        runs = answering("possible", "unsure", "possible")
        for days, ends in ((12, ", ends in 12 days"), (None, "")):
            with self.subTest(days=days):
                d = triage.digest(triage.Screen(*QUIET, runs, CLEAN), days)
                self.assertEqual(d.headline, "Quiet: a realistic way to YES exists, 2/2 news "
                                             f"checks found nothing pointing to it{ends}.")

    def test_a_human_headline_counts_developments_else_gives_the_first_reason(self):
        one = found(item("A", "https://a.org"))
        two = found(item("A", "https://a.org"), item("B", "https://b.org"))
        for checks, headline in (((one, one), "1 news development points toward YES."),
                                 ((one, two), "2 news developments point toward YES."),
                                 ((CLEAN_CHECK, found()._replace(summary="A hearing.")),
                                  "recent news (check 2): A hearing.")):
            with self.subTest(headline):
                self.assertEqual(digest_of(*checks).headline, "Needs human: " + headline)
        split = answering("near_impossible", "possible", "unsure")
        for screen, headline in (
                (triage.Screen(*triage.background_route(split, 80), split, ()),
                 "background 1 of 3 near impossible."),
                (triage.Screen(*human(triage.NEWS_CANT_SETTLE), split, ()),
                 triage.NEWS_CANT_SETTLE + "."),
                (triage.RULES_UNAVAILABLE, "rules unavailable.")):
            with self.subTest(headline):
                self.assertEqual(triage.digest(screen, 80).headline, "Needs human: " + headline)

    def test_agreement_puts_the_most_common_answer_first_out_of_all_runs(self):
        for answers, agreement in (
                (("possible", "near_impossible", "near_impossible"),
                 "near impossible 2 of 3, possible 1"),
                (("unsure", "possible", "near_impossible"),
                 "near impossible 1 of 3, possible 1, unsure 1"),
                (("possible",) * 3, "possible 3 of 3"), ((), "")):
            with self.subTest(answers):
                screen = triage.Screen(triage.Verdict.NEEDS_HUMAN, ("x",), answering(*answers), ())
                self.assertEqual(triage.digest(screen, None).agreement, agreement)


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


class VerifyItemsTest(unittest.TestCase):
    def test_page_text_keeps_what_a_reader_sees(self):
        text = triage.page_text(PAGE)
        self.assertTrue(triage.quote_in("The files show no evidence.", text))
        self.assertNotIn("var s", text)
        self.assertNotIn("color", text)
        blocks = triage.page_text("<h1>Files</h1><p>The files<br>today</p>")
        self.assertNotIn("FilesThe", blocks)
        self.assertNotIn("filestoday", blocks)

    def test_marks_each_item_found_not_found_or_unreadable(self):
        pages = {"https://a": triage.page_text(PAGE), "https://b": "Something else entirely."}

        def fetch(url):
            if url not in pages:
                raise ConnectionError("unreachable")
            return pages[url]
        items = tuple(triage.NewsItem("t", "The files show no evidence.", url, None, None)
                      for url in ("https://a", "https://b", "https://c"))
        self.assertEqual([i.verified for i in triage.verify_items(items, fetch)],
                         [True, False, None])

    def test_circular_items_cite_a_market(self):
        def item(url):
            return triage.NewsItem("t", "q", url, None, None)
        self.assertTrue(item("https://polymarket.com/event/x").circular)
        self.assertTrue(item("https://gamma-api.polymarket.com/m").circular)
        self.assertFalse(item("https://www.reuters.com/a").circular)
        self.assertFalse(item("https://notpolymarket.com/a").circular)


class BuildPromptTest(unittest.TestCase):
    def test_background_prompt_dates_today_and_carries_the_rules_verbatim(self):
        prompt = triage.build_background_prompt(QUESTION, "2026-12-31", RULES, date(2026, 10, 9))
        for needle in ("Today is 2026-10-09. Your training data may be older than today.",
                       f"Question: {QUESTION}", "End date: 2026-12-31",
                       f"Rules, verbatim:\n{RULES}", "assuming nothing unusual has happened",
                       "settled_by_news", "A wrong near_impossible loses money",
                       "a price, index, rate or\n   other figure that can cross its threshold"):
            self.assertIn(needle, prompt)

    def test_news_prompt_lists_each_path_once_and_the_window(self):
        other = triage.YesPath("A whistleblower is believed", "", True, True)
        runs = (triage.BackgroundRun("near_impossible", True, (
                    triage.YesPath(PATH["path"], PATH["rule_quote"], False, True), other), "r"),
                triage.BackgroundRun("near_impossible", True, (other,), "r"))
        prompt = triage.build_news_prompt(QUESTION, "2026-12-31", RULES, runs, date(2026, 10, 9))
        for needle in ("Today is 2026-10-09.", RULES, f"- {PATH['path']}\n- {other.path}\n",
                       "from 2025-10-01 through 2026-10-09", "copied verbatim from the page",
                       "Do not estimate a probability", "sites are not news"):
            self.assertIn(needle, prompt)
        self.assertEqual(prompt.count(other.path), 1)

    def test_news_prompt_counts_anything_making_yes_more_plausible_even_when_denied(self):
        prompt = triage.build_news_prompt(QUESTION, "2026-12-31", RULES, (run(),),
                                          date(2026, 10, 9))
        for needle in ("items: each development that opened, advanced or made more plausible a "
                       "way to YES", "even if other reports contradict it",
                       "pressure, orders or demands from people who can make it happen",
                       "precedent, or peers doing the same thing",
                       "credible reports of talks, plans or intent",
                       "A denial does not cancel an item: list the item and put the denial in "
                       "summary", "Only news that purely restates the status quo or points away "
                       "from YES goes in summary alone",
                       "found: true if anything opened, advanced or made more plausible"):
            self.assertIn(needle, prompt)


class ParseTest(unittest.TestCase):
    def test_background_checks_each_rule_quote_against_the_rules(self):
        made_up = {"path": "Aliens land", "rule_quote": "if aliens land on the lawn",
                   "realistic": True}
        unquoted = {"path": "A loose reading", "rule_quote": "", "realistic": False}
        r = triage.parse_background(envelope(background(paths=(PATH, made_up, unquoted))),
                                    RULES)
        self.assertEqual((r.answer, r.settled_by_news, r.reason),
                         ("near_impossible", True, "Absurd."))
        self.assertEqual([(p.path, p.realistic, p.quote_ok) for p in r.yes_paths],
                         [(PATH["path"], False, True), ("Aliens land", True, False),
                          ("A loose reading", False, True)])

    def test_malformed_background_output_raises_triage_error(self):
        cases = {
            "unknown answer": envelope(background(answer="likely")),
            "no answer": envelope({k: v for k, v in background().items() if k != "answer"}),
            "settled_by_news not a bool": envelope({**background(), "settled_by_news": "yes"}),
            "yes_paths not a list": envelope({**background(), "yes_paths": PATH}),
            "path without a quote": envelope(background(paths=({"path": "x",
                                                                 "realistic": True},))),
            "realistic not a bool": envelope(background(paths=({**PATH, "realistic": 0},))),
            "no reason": envelope({k: v for k, v in background().items() if k != "reason"}),
            "no structured_output": envelope(None),
            "is_error": envelope(background(), is_error=True, subtype="error_max_turns"),
            "not JSON": "Error: not logged in",
        }
        for name, output in cases.items():
            with self.subTest(name), self.assertRaises(triage.TriageError):
                triage.parse_background(output, RULES)

    def test_news_parses_items_unverified(self):
        n = triage.parse_news(envelope(news(True, (NEWS_ITEM, {**NEWS_ITEM, "date": ""}))))
        self.assertEqual((n.searches, n.found, n.summary),
                         (("US government confirms aliens 2026",), True, "Nothing new."))
        self.assertEqual(n.items[0], triage.NewsItem(NEWS_ITEM["text"], NEWS_ITEM["quote"],
                                                     NEWS_ITEM["url"], "2026-05-01", None))
        self.assertIsNone(n.items[1].date)

    def test_malformed_news_output_raises_triage_error(self):
        cases = {
            "found not a bool": envelope({**news(), "found": "no"}),
            "searches not strings": envelope({**news(), "searches": [1]}),
            "item without a url": envelope(news(items=({"text": "t", "quote": "q"},))),
            "date not a string": envelope(news(items=({**NEWS_ITEM, "date": 2026},))),
            "no summary": envelope({k: v for k, v in news().items() if k != "summary"}),
        }
        for name, output in cases.items():
            with self.subTest(name), self.assertRaises(triage.TriageError):
                triage.parse_news(output)

    def test_is_error_says_why(self):
        with self.assertRaisesRegex(triage.TriageError, "error_max_turns.*ran out"):
            triage.parse_news(envelope(news(), is_error=True, subtype="error_max_turns",
                                       result="ran out"))


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
            return triage.run_claude(step, "the prompt", timeout_s=30), calls

    def test_background_runs_without_tools_and_the_prompt_goes_on_stdin(self):
        done = subprocess.CompletedProcess([], 0, envelope(background()), "")
        out, ((argv, kw),) = self.run_with(triage.BACKGROUND, done)
        self.assertEqual(out, envelope(background()))
        self.assertEqual(argv[:2], [r"C:\bin\claude.EXE", "-p"])
        flags = dict(zip(argv, argv[1:]))
        self.assertEqual((flags["--model"], flags["--output-format"], flags["--tools"]),
                         ("sonnet", "json", ""))
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(json.loads(flags["--json-schema"]), triage.BACKGROUND_SCHEMA)
        self.assertEqual(flags["--system-prompt"], triage.BACKGROUND.system)
        for flag in ("--no-session-persistence", "--strict-mcp-config",
                     "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertNotIn("--bare", argv)
        self.assertNotIn("the prompt", argv)
        self.assertEqual((kw["input"], kw["timeout"]), ("the prompt", 30))
        self.assertNotEqual(os.path.abspath(kw["cwd"]), os.getcwd())

    def test_news_runs_with_web_search_and_fetch(self):
        _, ((argv, _),) = self.run_with(triage.NEWS,
                                        subprocess.CompletedProcess([], 0, envelope(news()), ""))
        flags = dict(zip(argv, argv[1:]))
        self.assertEqual((flags["--tools"], flags["--allowedTools"]),
                         ("WebSearch,WebFetch", "WebSearch,WebFetch"))
        self.assertEqual(json.loads(flags["--json-schema"]), triage.NEWS_SCHEMA)

    def test_timeout_and_silent_failure_raise_triage_error(self):
        with self.assertRaisesRegex(triage.TriageError, r"\(news\) did not finish within 30s"):
            self.run_with(triage.NEWS, subprocess.TimeoutExpired(["claude"], 30))
        with self.assertRaisesRegex(triage.TriageError, "exited 1: not logged in"):
            self.run_with(triage.BACKGROUND, subprocess.CompletedProcess([], 1, "",
                                                                         "not logged in\n"))

    def test_no_cli_raises_triage_error(self):
        with mock.patch.object(triage.shutil, "which", return_value=None), \
                self.assertRaisesRegex(triage.TriageError, "not on PATH"):
            triage.run_claude(triage.BACKGROUND, "p")


def metrics():
    return {"book_mid": 0.94, "volume24hr": 130.0, "est_daily_usd": 0.22, "net_apr": 0.165,
            "plan": {"fav_bid_price": 0.9},
            "market": {"question": QUESTION, "fav_outcome": "No", "rate_per_day": 1.0}}


class StubClaude:
    """run: background answers in turn (cycling), then the news output; any
    answer that is an exception is raised."""

    def __init__(self, answers, news_output=None):
        self.answers = list(answers)
        self.news_output = news_output if news_output is not None else news()
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, step, prompt):
        with self._lock:
            self.calls.append((step.name, prompt))
            answer = (self.answers[(len(self.calls) - 1) % len(self.answers)]
                      if step is triage.BACKGROUND else self.news_output)
        if isinstance(answer, Exception):
            raise answer
        return envelope(answer)

    def steps(self):
        return [name for name, _ in self.calls]


def rules(text=RULES):
    return lambda cid: Rules(text, "https://www.whitehouse.gov", None)


class ScreenAndStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = farm_db.open_db(os.path.join(tmp.name, "farm.db"))
        self.addCleanup(self.db.close)
        self.ends(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc))
        self.pages = {}

    def ends(self, end_date):
        farm_db.store_scan(self.db, [farm_db.ScanRow(
            "0xali", "aliens", QUESTION, end_date and end_date.isoformat(), 2.0,
            json.dumps(metrics()))], ["0xali"], NOW - timedelta(days=1))

    def fetch(self, url):
        return self.pages[url]

    def screen(self, claude, fetch_rules=rules()):
        return triage.screen_and_store(self.db, "0xali", NOW, fetch_rules, claude, self.fetch)

    def test_a_unanimous_background_then_two_quiet_news_checks_store_near_impossible(self):
        claude = StubClaude([background()])
        row = self.screen(claude)
        self.assertEqual(sorted(claude.steps()), ["background"] * 3 + ["news"] * 2)
        self.assertEqual((row.status, row.verdict, row.book_mid, row.prompt_version, row.model),
                         ("ok", "near_impossible", 0.94, triage.PROMPT_VERSION, "sonnet"))
        self.assertAlmostEqual(row.cost_usd, 0.20)
        s = triage.from_result(row.result)
        self.assertEqual((s.verdict, s.why, len(s.runs)), (triage.Verdict.NEAR_IMPOSSIBLE, (), 3))
        self.assertEqual([n.searches for n in s.news],
                         [("US government confirms aliens 2026",)] * 2)
        self.assertEqual(triage.to_result(s), row.result)
        self.assertEqual(farm_db.triaged_ids(self.db, triage.PROMPT_VERSION), {"0xali"})

    def test_the_news_checks_are_asked_at_once(self):
        both = threading.Barrier(2, timeout=5)
        claude = StubClaude([background()])

        def run(step, prompt):
            if step is triage.NEWS:
                both.wait()
            return claude(step, prompt)
        self.assertEqual(self.screen(run).verdict, "near_impossible")

    def test_prompts_carry_today_and_the_rules_and_never_the_price(self):
        claude = StubClaude([background()])
        self.screen(claude)
        for step, prompt in claude.calls:
            with self.subTest(step):
                self.assertIn("Today is 2026-10-09.", prompt)
                self.assertIn(RULES, prompt)
                self.assertIn("End date: 2026-12-31", prompt)
                for leak in ("94", "16.5", "0.22", "130", "0.9", "APR", "midpoint", "priced at"):
                    self.assertNotIn(leak, prompt)

    def test_a_split_background_never_asks_the_news(self):
        claude = StubClaude([background(), background("unsure"), background()])
        row = self.screen(claude)
        self.assertEqual(claude.steps(), ["background"] * 3)
        s = triage.from_result(row.result)
        self.assertEqual((row.verdict, s.why, s.news),
                         ("needs_human", ("background 2 of 3 near impossible",
                                          "ends in 83 days, past the 45-day quiet window"), ()))

    def test_a_possible_market_ending_within_45_days_asks_the_news_and_stores_quiet(self):
        self.ends(NOW + timedelta(days=triage.QUIET_DAYS))
        claude = StubClaude([background("possible"), background("unsure")])
        row = self.screen(claude)
        self.assertEqual(sorted(claude.steps()), ["background"] * 3 + ["news"] * 2)
        s = triage.from_result(row.result)
        self.assertEqual((row.verdict, s.verdict, s.why, len(s.news)),
                         ("quiet", triage.Verdict.QUIET, (), 2))
        self.assertIn("End date: 2026-11-23", claude.calls[0][1])

    def test_a_quiet_market_with_a_news_item_needs_a_human(self):
        self.ends(NOW + timedelta(days=10))
        self.pages[NEWS_ITEM["url"]] = "elsewhere"
        row = self.screen(StubClaude([background("possible")], news(items=(NEWS_ITEM,))))
        self.assertEqual((row.verdict, triage.from_result(row.result).why),
                         ("needs_human", ("recent news: The Pentagon released UAP files",)))

    def test_an_unknown_end_date_never_asks_the_news_for_a_possible_market(self):
        self.ends(None)
        claude = StubClaude([background("possible")])
        row = self.screen(claude)
        self.assertEqual(claude.steps(), ["background"] * 3)
        self.assertIn("End date: unknown", claude.calls[0][1])
        self.assertEqual(triage.from_result(row.result).why,
                         ("background 0 of 3 near impossible", "end date unknown"))

    def test_news_that_cannot_settle_it_skips_the_news_check(self):
        claude = StubClaude([background(), background(settled=False)])
        row = self.screen(claude)
        self.assertEqual(claude.steps(), ["background"] * 3)
        self.assertEqual(row.verdict, "needs_human")
        self.assertEqual(triage.from_result(row.result).why, (triage.NEWS_CANT_SETTLE,))

    def test_a_news_item_needs_a_human_and_its_quote_is_checked_on_its_page(self):
        self.pages[NEWS_ITEM["url"]] = triage.page_text(PAGE)
        row = self.screen(StubClaude([background()], news(items=(NEWS_ITEM,))))
        s = triage.from_result(row.result)
        self.assertEqual((row.verdict, s.why),
                         ("needs_human", ("recent news: The Pentagon released UAP files",)))
        self.assertEqual([n.items[0].verified for n in s.news], [True, True])

    def test_blank_rules_need_a_human_without_asking_claude(self):
        claude = StubClaude([background()])
        row = self.screen(claude, rules("  "))
        self.assertEqual(claude.calls, [])
        self.assertEqual((row.status, row.verdict, triage.from_result(row.result).why),
                         ("ok", "needs_human", ("rules unavailable",)))

    def test_a_failed_call_or_rules_fetch_stores_an_error_row(self):
        row = self.screen(StubClaude([background(), triage.TriageError("claude (background) "
                                                                       "did not finish")]))
        self.assertEqual((row.status, row.verdict, row.result, row.error),
                         ("error", None, None, "TriageError: claude (background) did not finish"))

        def no_rules(cid):
            raise LookupError("Gamma has no record of this market")
        claude = StubClaude([background()])
        row = self.screen(claude, no_rules)
        self.assertEqual((row.status, row.error),
                         ("error", "LookupError: Gamma has no record of this market"))
        self.assertEqual(claude.calls, [])
        self.assertEqual(farm_db.triaged_ids(self.db, triage.PROMPT_VERSION), frozenset())


if __name__ == "__main__":
    unittest.main()
