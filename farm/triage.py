"""
Claude triage: a screen for markets a human need not look into, because YES
is near impossible under their rules before the end date, or because the
market ends soon and nothing in the news points toward YES. It never
estimates a probability.

Markets are screened an event at a time, at most EVENT_MARKETS together,
because nearly every market turns on current events and one search serves
all of an event's markets. One call with web search writes the event's
brief: where it stands today and the facts found since NEWS_SINCE, each with
the markets it moves toward YES. Its quotes are checked against the pages
they cite. Each market is then judged RUNS times with no tools, from its
rules and the brief: every path to YES under the rules, and whether YES is
near impossible. One judge alone is unstable, so decide() wants them
unanimous: NEAR_IMPOSSIBLE when every run says so with verbatim rule quotes,
QUIET when not but the market ends within QUIET_DAYS, and NEEDS_HUMAN
otherwise, or whenever a fact points toward YES or news could not show a
path opening.

The calls run on the user's Claude Code login, never --bare, which refuses
subscription auth. Prompts go on stdin: Windows caps a command line at
32,767 characters.
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from enum import Enum
from html.parser import HTMLParser
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

import httpx

from farm import farm_db

MODEL = "sonnet"
RUNS = 3
QUIET_DAYS = 45
NEWS_SINCE = date(2025, 10, 1)
# The most markets one brief covers, and the most claude processes judging
# them at a time.
EVENT_MARKETS = 8
JUDGES_AT_ONCE = 6
JUDGE_TIMEOUT_S = 420
BRIEF_TIMEOUT_S = 900
REASON_MAX = 400
STATE_MAX = 1500
PAGE_TIMEOUT_S = 20
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# Sites whose numbers are other people's bets on the outcome, not news of it.
MARKET_DOMAINS = ("polymarket.com", "kalshi.com", "manifold.markets", "predictit.org",
                  "metaculus.com", "betfair.com", "oddschecker.com")


class TriageError(Exception):
    pass


class Verdict(Enum):
    NEAR_IMPOSSIBLE = "near_impossible"
    QUIET = "quiet"
    NEEDS_HUMAN = "needs_human"


ANSWERS = ("near_impossible", "possible", "unsure")


class YesPath(NamedTuple):
    path: str
    rule_quote: str  # verbatim words of the rules that would make it count, "" if none
    realistic: bool
    quote_ok: bool  # rule_quote is "" or found in the rules


class JudgeRun(NamedTuple):
    answer: str  # one of ANSWERS
    settled_by_news: bool  # whether public news could show a YES path opening
    yes_paths: tuple
    reason: str

    @property
    def near_impossible(self):
        """Whether the run counts toward NEAR_IMPOSSIBLE: it says so, lists a
        way to YES, and every rule quote is in the rules."""
        return (self.answer == "near_impossible" and bool(self.yes_paths)
                and all(p.quote_ok for p in self.yes_paths))


def _web(url):
    return url.lower().startswith(("https://", "http://"))


class Fact(NamedTuple):
    text: str
    quote: str  # a sentence the model copied from the page
    url: str
    date: Optional[str]
    toward_yes: bool  # opens, advances or makes more plausible a way to YES for this market
    verified: Optional[bool]  # quote found on the page; None when the page could not be read

    @property
    def sourced(self):
        """Whether it names a web page; only those are shown as links."""
        return _web(self.url)

    @property
    def circular(self):
        """Whether it cites a market or forecast on the outcome itself."""
        host = urlsplit(self.url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in MARKET_DOMAINS)


class Brief(NamedTuple):
    """What the news search found on a market's event. The markets of one
    event hold the same brief but for each fact's toward_yes."""
    searches: tuple
    state: str  # where the event stands today
    facts: tuple


NO_BRIEF = Brief((), "", ())


class Screen(NamedTuple):
    verdict: Verdict
    why: tuple  # why it is NEEDS_HUMAN; empty otherwise
    runs: tuple
    brief: Brief


NEWS_CANT_SETTLE = ("news can't settle this market (live event, game in progress, data "
                    "release or private information)")


def _not_near_impossible(runs):
    counted = sum(r.near_impossible for r in runs)
    return (f"{counted} of {len(runs)} runs near impossible",
            *(f"run {i} " + ("quoted words not in the rules" if r.yes_paths
                             else "listed no way to YES")
              for i, r in enumerate(runs, 1)
              if r.answer == "near_impossible" and not r.near_impossible))


def _not_quiet(days_left):
    if days_left is None:
        return ("end date unknown",)
    if days_left > QUIET_DAYS:
        return (f"ends in {days_left} days, past the {QUIET_DAYS}-day quiet window",)
    return ()


def _news_line(fact):
    return f"recent news: {fact.text}"


def decide(runs, brief, days_left):
    """The Screen of a market from its judge runs and its brief; days_left
    until the end date, None when unknown. A fact that does not point toward
    YES changes nothing. QUIET does not rest on the runs' reasoning, so their
    rule quotes do not matter to it."""
    blocked = (*(() if all(r.settled_by_news for r in runs) else (NEWS_CANT_SETTLE,)),
               *dict.fromkeys(_news_line(f) for f in brief.facts if f.toward_yes))
    not_quiet = _not_quiet(days_left)
    if runs and all(r.near_impossible for r in runs):
        verdict, why = (Verdict.NEEDS_HUMAN if blocked else Verdict.NEAR_IMPOSSIBLE), blocked
    elif runs and not blocked and not not_quiet:
        verdict, why = Verdict.QUIET, ()
    else:
        verdict, why = Verdict.NEEDS_HUMAN, (*_not_near_impossible(runs), *blocked, *not_quiet)
    return Screen(verdict, why, runs, brief)


def days_until(end, today):
    """Whole days from today to end (a date, None when unknown)."""
    return None if end is None else (end - today).days


_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "…": "...",
})
_EDGE = " .,;:!?\"'()[]"


def _normalize(text):
    text = " ".join(text.casefold().translate(_FOLD).split())
    return re.sub(r" ([,.;:!?])", r"\1", text)


QUOTE_MIN_WORDS = 4
# Words a quote may get wrong per this many it copies: "officially" for
# "explicitly" still shows the model read the rules; an invented clause does not.
WORDS_PER_SLIP = 10


def _find(part, words, start):
    """The index in words just past the first run matching part from start,
    allowing one substituted word per WORDS_PER_SLIP; -1 when none does."""
    allowed = len(part) // WORDS_PER_SLIP
    for i in range(start, len(words) - len(part) + 1):
        slips = 0
        for want, got in zip(part, words[i:i + len(part)]):
            slips += want != got
            if slips > allowed:
                break
        else:
            return i + len(part)
    return -1


def quote_in(quote, text):
    """Whether quote appears in text, ignoring case, quote and dash styles,
    spacing and the quote's surrounding punctuation. An ellipsis elides words:
    the parts around it must appear in that order."""
    def tokens(s):
        return [w for w in (w.strip(_EDGE) for w in s.split()) if w]

    parts = [p for p in (tokens(p) for p in _normalize(quote).split("...")) if p]
    if sum(len(p) for p in parts) < QUOTE_MIN_WORDS:
        return False
    words, at = tokens(_normalize(text)), 0
    for part in parts:
        at = _find(part, words, at)
        if at < 0:
            return False
    return True


class _PageText(HTMLParser):
    HIDDEN = {"script", "style", "noscript", "template"}
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
              "td", "th", "table", "section", "article", "header", "footer", "blockquote",
              "figcaption", "title"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        self.hidden += tag in self.HIDDEN
        if tag in self.BLOCKS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.HIDDEN and self.hidden:
            self.hidden -= 1
        if tag in self.BLOCKS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def page_text(html):
    """The text a reader sees in html: tags, scripts and styles removed."""
    parser = _PageText()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def fetch_page(url):
    """The text of the web page at url; raises when it cannot be read."""
    resp = httpx.get(url, timeout=PAGE_TIMEOUT_S, follow_redirects=True,
                     headers={"User-Agent": BROWSER_UA})
    resp.raise_for_status()
    kind = resp.headers.get("content-type", "")
    if not kind.startswith(("text/", "application/xhtml")):
        raise TriageError(f"{url} is {kind or 'untyped'}, not a web page")
    return page_text(resp.text)


def verify_briefs(briefs, fetch):
    """The briefs of one event's markets with each fact's verified set by
    reading its page through fetch (fetch_page or a stand-in), each page once."""
    pages = {}

    def read(url):
        if url not in pages:
            try:
                pages[url] = fetch(url)
            except Exception:
                pages[url] = None
        return pages[url]

    def on_page(fact):
        text = read(fact.url)
        return None if text is None else quote_in(fact.quote, text)

    # The briefs differ only in toward_yes, so one check of each fact serves them all.
    checked = [on_page(f) for f in briefs[0].facts] if briefs else []
    return tuple(b._replace(facts=tuple(f._replace(verified=v)
                                        for f, v in zip(b.facts, checked))) for b in briefs)


JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["yes_paths", "settled_by_news", "answer", "reason"],
    "properties": {
        "yes_paths": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "rule_quote", "realistic"],
            "properties": {"path": {"type": "string"}, "rule_quote": {"type": "string"},
                           "realistic": {"type": "boolean"}},
        }},
        "settled_by_news": {"type": "boolean"},
        "answer": {"enum": list(ANSWERS)},
        "reason": {"type": "string", "maxLength": REASON_MAX},
    },
}

BRIEF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["searches", "state", "facts"],
    "properties": {
        "searches": {"type": "array", "items": {"type": "string"}},
        "state": {"type": "string", "maxLength": STATE_MAX},
        "facts": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "quote", "url", "toward_yes"],
            "properties": {"text": {"type": "string"}, "quote": {"type": "string"},
                           "url": {"type": "string"}, "date": {"type": "string"},
                           "toward_yes": {"type": "array", "items": {"type": "integer"}}},
        }},
    },
}


class Step(NamedTuple):
    name: str
    tools: str
    schema: dict
    timeout_s: int
    system: str


BRIEF = Step("brief", "WebSearch,WebFetch", BRIEF_SCHEMA, BRIEF_TIMEOUT_S, (
    "You research where one prediction-market event stands today, so that another model "
    "with no web access can judge each of its markets. Search the web and open the pages "
    "you cite. You do not estimate probabilities. Prediction markets, betting odds and "
    "forecasting sites are not news."))
JUDGE = Step("judge", "", JUDGE_SCHEMA, JUDGE_TIMEOUT_S, (
    "You screen prediction markets for one property: whether YES is near impossible under "
    "the market's rules before its end date. You have no web access; a news search run "
    "today gives you the current facts. You do not estimate probabilities."))


def build_brief_prompt(title, end, rules, questions, today):
    """The brief's prompt: the event, its end date (text), the rules of its
    first market verbatim and its markets' questions, numbered from 1."""
    listed = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    return f"""Today is {today.isoformat()}.

Event: {title}
End date: {end}
Rules of market 1, verbatim (any other market's rules differ in the subject they name):
{rules}

Markets of this event to be judged:
{listed}

1. Search for where the event stands from {NEWS_SINCE.isoformat()} through \
{today.isoformat()}: whether it has happened or been scheduled, its key dates and deadlines, \
who or what is actually in contention, and anything that rules a listed market's subject in \
or out. List every search you run in searches.
2. Open every page you cite with WebFetch and read it. A search result's summary is not the \
page.
3. state: at most {STATE_MAX} characters on where the event stands today. If you find \
nothing about a listed market's subject, say so here.
4. facts: each fact a judge of these markets needs, whether it points toward YES or away \
from it. text: what happened, one sentence. quote: a sentence copied verbatim from the page \
as WebFetch returned it, never from a search result's summary. url: the page you opened. \
date: YYYY-MM-DD when the page gives one. toward_yes: the numbers of the listed markets for \
which this fact opens, advances or makes more plausible a way to YES, even if other reports \
contradict it; empty when it is neutral or points away from YES for all of them. Toward YES \
includes official steps; pressure, orders or demands from people who can make it happen; \
precedent, or peers doing the same thing; credible reports of talks, plans or intent. A \
denial does not cancel such a fact: list the denial as a fact of its own.

Do not estimate a probability. Prediction markets, betting odds and forecasting sites are \
not news."""


def _market(question, end, rules):
    return f"Question: {question}\nEnd date: {end}\nRules, verbatim:\n{rules}"


def _listed(fact):
    points = "toward YES" if fact.toward_yes else "not toward YES"
    return f"- {fact.date or 'undated'}, {points}: {fact.text} <{fact.url}>"


def build_judge_prompt(question, end, rules, brief, today):
    """A judge run's prompt: the question, its end date (text), its rules
    verbatim and what the brief found, each fact with where it points for
    this market."""
    facts = "\n".join(_listed(f) for f in brief.facts) or "- none"
    return f"""Today is {today.isoformat()}. Your training data may be older than today.

{_market(question, end, rules)}

A news search run today on this market's event. A fact marked toward YES opens, advances or
makes more plausible a way to YES for this market; the others are neutral or point away.
Where the event stands: {brief.state}
Facts it found:
{facts}

1. List every way this market could resolve YES under the rules as written, including loose
   readings of the wording (a statement, a post, an official claim, a technicality, a 50-50 or
   early resolution). For each, copy the exact words of the rules that would make it count into
   rule_quote (verbatim, or empty if no clause covers it), and say whether it is realistic before
   the end date.
2. settled_by_news: true if public news reporting would show whether one of these ways has
   opened. False when it turns on something news does not report as it happens: a livestream,
   a game in progress, a pending data release, private information, or a price, index, rate or
   other figure that can cross its threshold without any news first.
3. answer, judged from the rules and the news search above:
   - near_impossible: every path to YES needs something extraordinary (physically, legally or
     politically absurd) before the end date.
   - possible: at least one realistic path to YES exists.
   - unsure: you cannot tell.
   When in doubt, answer unsure. A wrong near_impossible loses money; unsure costs nothing.
4. reason: at most {REASON_MAX} characters on why."""


def _envelope(cli_output):
    try:
        envelope = json.loads(cli_output)
    except ValueError:
        raise TriageError(f"claude printed no JSON: {cli_output[:200]!r}") from None
    if not isinstance(envelope, dict):
        raise TriageError("claude's output is not a JSON object")
    if envelope.get("is_error"):
        raise TriageError(f"claude failed ({envelope.get('subtype')}): "
                          f"{str(envelope.get('result'))[:300]}")
    return envelope


def _structured(cli_output):
    obj = _envelope(cli_output).get("structured_output")
    if not isinstance(obj, dict):
        raise TriageError("claude returned no structured_output object")
    return obj


def _cost(cli_output):
    return json.loads(cli_output).get("total_cost_usd") or 0.0


def _typed(obj, key, kind):
    value = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(value, kind):
        raise TriageError(f"{key} {value!r} is not a {kind.__name__}")
    return value


def _strings(obj, key):
    value = _typed(obj, key, list)
    if not all(isinstance(s, str) for s in value):
        raise TriageError(f"{key} is not a list of strings")
    return tuple(value)


def _yes_path(obj, rules):
    quote = _typed(obj, "rule_quote", str)
    return YesPath(_typed(obj, "path", str), quote, _typed(obj, "realistic", bool),
                   quote == "" or quote_in(quote, rules))


def parse_judge(cli_output, rules):
    """The JudgeRun in a judge call's claude output, its rule quotes checked
    against rules, or TriageError."""
    obj = _structured(cli_output)
    if obj.get("answer") not in ANSWERS:
        raise TriageError(f"answer {obj.get('answer')!r} is not one of {ANSWERS}")
    return JudgeRun(obj["answer"], _typed(obj, "settled_by_news", bool),
                    tuple(_yes_path(p, rules) for p in _typed(obj, "yes_paths", list)),
                    _typed(obj, "reason", str))


def _toward(obj, n):
    numbers = _typed(obj, "toward_yes", list)
    if not all(type(k) is int and 1 <= k <= n for k in numbers):
        raise TriageError(f"toward_yes {numbers!r} is not a list of market numbers 1 to {n}")
    return frozenset(numbers)


def _fact(obj, toward_yes):
    when = obj.get("date")
    if when is not None and not isinstance(when, str):
        raise TriageError(f"date {when!r} is not a str")
    return Fact(_typed(obj, "text", str), _typed(obj, "quote", str), _typed(obj, "url", str),
                when or None, toward_yes, None)


def parse_brief(cli_output, n):
    """The Brief of each of the n markets the brief call numbered, in their
    order and with its facts not yet verified, or TriageError."""
    obj = _structured(cli_output)
    searches, state = _strings(obj, "searches"), _typed(obj, "state", str)
    facts = [(f, _toward(f, n)) for f in _typed(obj, "facts", list)]
    return tuple(Brief(searches, state, tuple(_fact(f, k in toward) for f, toward in facts))
                 for k in range(1, n + 1))


def to_result(screen):
    """A Screen as the JSON object the triage table stores."""
    return {"verdict": screen.verdict.value, "why": list(screen.why),
            "runs": [{**r._asdict(), "yes_paths": [p._asdict() for p in r.yes_paths]}
                     for r in screen.runs],
            "brief": {"searches": list(screen.brief.searches), "state": screen.brief.state,
                      "facts": [f._asdict() for f in screen.brief.facts]}}


def from_result(obj):
    """The Screen a stored to_result object holds."""
    brief = obj["brief"]
    return Screen(
        Verdict(obj["verdict"]), tuple(obj["why"]),
        tuple(JudgeRun(**{**r, "yes_paths": tuple(YesPath(**p) for p in r["yes_paths"])})
              for r in obj["runs"]),
        Brief(tuple(brief["searches"]), brief["state"],
              tuple(Fact(**f) for f in brief["facts"])))


LABELS = {Verdict.NEAR_IMPOSSIBLE: "Near impossible", Verdict.QUIET: "Quiet",
          Verdict.NEEDS_HUMAN: "Needs human"}


class PathSummary(NamedTuple):
    """One way to YES, merged across the judge runs that listed it."""
    path: str
    runs: int
    realistic: int  # how many of those runs called it realistic
    quote_missing: bool  # some run's rule quote for it is not in the rules


class Digest(NamedTuple):
    verdict: Verdict
    reason: str  # the sentence deciding the verdict, without the verdict's label
    also: tuple  # the lines of why that neither reason nor facts already give
    facts: tuple  # newest first, undated last
    paths: tuple  # listed by the most runs first, then called realistic by the most
    agreement: str  # how the runs answered, as "near impossible 2 of 3, possible 1"

    @property
    def headline(self):
        return f"{LABELS[self.verdict]}: {self.reason}"


def _paths(runs):
    merged = {}
    for run in runs:
        listed = {}
        for p in run.yes_paths:
            key = _normalize(p.path)
            prev = listed.setdefault(key, p)
            listed[key] = prev._replace(realistic=prev.realistic or p.realistic,
                                        quote_ok=prev.quote_ok and p.quote_ok)
        for key, p in listed.items():
            s = merged.get(key, PathSummary(p.path, 0, 0, False))
            merged[key] = PathSummary(s.path, s.runs + 1, s.realistic + p.realistic,
                                      s.quote_missing or not p.quote_ok)
    return tuple(sorted(merged.values(), key=lambda p: (-p.runs, -p.realistic)))


def _agreement(runs):
    counts = Counter(r.answer for r in runs)
    order = sorted(counts, key=lambda a: (-counts[a], ANSWERS.index(a)))
    return ", ".join(f"{a.replace('_', ' ')} {counts[a]}" + (f" of {len(runs)}" if i == 0 else "")
                     for i, a in enumerate(order))


def _reason(screen, toward, days_left):
    if screen.verdict is Verdict.NEAR_IMPOSSIBLE:
        runs = len(screen.runs)
        return f"{runs}/{runs} runs found YES near impossible given the news search."
    if screen.verdict is Verdict.QUIET:
        ends = "" if days_left is None else f", ends in {days_left} days"
        return f"nothing in the news search points to YES{ends}."
    if toward == 1:
        return "1 news fact points toward YES."
    if toward:
        return f"{toward} news facts point toward YES."
    return (screen.why[0].rstrip(".") if screen.why else "no reason recorded") + "."


def digest(screen, days_left):
    """screen summed up for a reader: its verdict and the reason that decided
    it, the news facts, each way to YES once, and how the runs answered.
    days_left until the end date, None when unknown."""
    facts = tuple(sorted(screen.brief.facts, key=lambda f: f.date or "", reverse=True))
    toward = [_news_line(f) for f in facts if f.toward_yes]
    also = tuple(w for w in screen.why if w not in toward) if toward else screen.why[1:]
    return Digest(screen.verdict, _reason(screen, len(toward), days_left), also, facts,
                  _paths(screen.runs), _agreement(screen.runs))


def run_claude(step, prompt):
    """claude -p's JSON output for this Step and prompt, or TriageError.

    It runs in an empty directory, so no project CLAUDE.md or settings reach it.
    """
    exe = shutil.which("claude")
    if exe is None:
        raise TriageError("the claude CLI is not on PATH")
    argv = [exe, "-p", "--model", MODEL, "--output-format", "json", "--no-session-persistence",
            "--strict-mcp-config", "--disable-slash-commands", "--tools", step.tools,
            *(("--allowedTools", step.tools) if step.tools else ()),
            "--json-schema", json.dumps(step.schema), "--system-prompt", step.system]
    with tempfile.TemporaryDirectory(prefix="triage-") as cwd:
        try:
            done = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=step.timeout_s,
                                  cwd=cwd)
        except subprocess.TimeoutExpired:
            raise TriageError(
                f"claude ({step.name}) did not finish within {step.timeout_s}s") from None
    if not done.stdout.strip():
        raise TriageError(f"claude ({step.name}) exited {done.returncode}: "
                          f"{done.stderr.strip()[-300:]}")
    return done.stdout


class Market(NamedTuple):
    """What a screen reads of one market."""
    question: str
    end: Optional[date]  # None when unknown
    rules: str


def _end_text(end):
    return end.isoformat() if end else "unknown"


def _judged(market, brief, calls, brief_share, today):
    try:
        outputs = [call.result() for call in calls]
        runs = tuple(parse_judge(o, market.rules) for o in outputs)
    except Exception as e:
        return e
    return (decide(runs, brief, days_until(market.end, today)),
            brief_share + sum(_cost(o) for o in outputs))


def screen_event(title, markets, today, run=run_claude, fetch=fetch_page):
    """For each Market of one event, in order: its (Screen, cost_usd of its
    judge runs and its even share of the brief), or the exception that stopped
    its judging. Raises when the brief fails. run is run_claude or a stand-in;
    fetch reads the pages the facts cite."""
    first = markets[0]
    asked = run(BRIEF, build_brief_prompt(title, _end_text(first.end), first.rules,
                                          [m.question for m in markets], today))
    briefs = parse_brief(asked, len(markets))
    share = _cost(asked) / len(markets)
    with ThreadPoolExecutor(JUDGES_AT_ONCE) as pool:
        calls = [[pool.submit(run, JUDGE, build_judge_prompt(m.question, _end_text(m.end),
                                                             m.rules, b, today))
                  for _ in range(RUNS)] for m, b in zip(markets, briefs)]
        # The judges are not told what verified, so the pages are read while they run.
        briefs = verify_briefs(briefs, fetch)
        return [_judged(m, b, c, share, today) for m, b, c in zip(markets, briefs, calls)]


def event_batches(records):
    """records (farm_db.MarketRecords) split into lists sharing an event, each
    at most EVENT_MARKETS long, in the order given. A market with no event is
    its own."""
    batches, filling = [], {}
    for record in records:
        event = record.metrics.get("market", {}).get("event_slug") or record.condition_id
        batch = filling.get(event)
        if batch is None or len(batch) == EVENT_MARKETS:
            batch = filling[event] = []
            batches.append(batch)
        batch.append(record)
    return batches


def read_batches(conn, condition_ids):
    """The event_batches of the markets with these ids, in the ids' order; an
    id the markets table lacks is left out."""
    by_id = {m.condition_id: m for m in farm_db.read_markets(conn, condition_ids)}
    return event_batches([by_id[c] for c in condition_ids if c in by_id])


RULES_UNAVAILABLE = Screen(Verdict.NEEDS_HUMAN, ("rules unavailable",), (), NO_BRIEF)


def screen_and_store(conn, condition_ids, now, fetch_rules, run=run_claude, fetch=fetch_page):
    """Screen one event batch and store each market's result, or the error
    that stopped it; the stored farm_db.TriageRecords in condition_ids' order.
    fetch_rules(condition_id) returns a market's remote.Rules. A market whose
    rules are blank needs a human, unasked, and stays out of the brief."""
    by_id = {m.condition_id: m for m in farm_db.read_markets(conn, condition_ids)}
    records = [by_id[c] for c in condition_ids]
    started = time.monotonic()
    outcomes, asked = {}, {}
    for r in records:
        try:
            rules = fetch_rules(r.condition_id).description.strip()
        except Exception as e:
            outcomes[r.condition_id] = e
            continue
        if rules:
            asked[r.condition_id] = Market(r.question or r.slug,
                                           r.end_date.date() if r.end_date else None, rules)
        else:
            outcomes[r.condition_id] = (RULES_UNAVAILABLE, None)
    if asked:
        markets = list(asked.values())
        title = (by_id[next(iter(asked))].metrics.get("market", {}).get("event_title")
                 or markets[0].question)
        try:
            screened = screen_event(title, markets, now.date(), run, fetch)
        except Exception as e:
            screened = [e] * len(markets)
        outcomes.update(zip(asked, screened))
    duration_s = time.monotonic() - started
    # Rows are stored here, not by the judge threads: a connection stays in its thread.
    for r in records:
        outcome, book_mid = outcomes[r.condition_id], r.metrics.get("book_mid")
        if isinstance(outcome, Exception):
            farm_db.store_triage(conn, r.condition_id, now, MODEL,
                                 error=f"{type(outcome).__name__}: {outcome}",
                                 book_mid=book_mid, duration_s=duration_s)
        else:
            result, cost = outcome
            farm_db.store_triage(conn, r.condition_id, now, MODEL, result=to_result(result),
                                 book_mid=book_mid, duration_s=duration_s, cost_usd=cost)
    return [farm_db.latest_triage(conn, c) for c in condition_ids]
