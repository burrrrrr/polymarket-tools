"""
Claude triage: a screen for markets a human need not look into, because YES
is near impossible under their rules before the end date, or because the
market ends soon and nothing in the news points toward YES. It never
estimates a probability.

Step 1 asks Claude RUNS times, with no tools, for every path to YES under the
rules and whether YES is near impossible assuming nothing unusual happened
recently. background_route() turns the runs into the Route step 2 confirms:
NEAR_IMPOSSIBLE when every run says so with verbatim rule quotes, QUIET when
not but news could show a path opening and the market ends within QUIET_DAYS,
else NEEDS_HUMAN, which skips step 2. Step 2 searches the news since
NEWS_SINCE for anything pointing toward YES, NEWS_RUNS times independently: a
search that finds nothing cannot be checked, so every one must come back
clean for decide() to keep the route's verdict.

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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

import farm_db

# Bump when a prompt or the decision rule changes; every row keeps it.
PROMPT_VERSION = 5
MODEL = "sonnet"
RUNS = 3
NEWS_RUNS = 2
QUIET_DAYS = 45
NEWS_SINCE = date(2025, 10, 1)
TIMEOUT_S = 420
REASON_MAX = 400
SUMMARY_MAX = 600
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


class BackgroundRun(NamedTuple):
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


class NewsItem(NamedTuple):
    text: str
    quote: str  # a sentence the model copied from the page
    url: str
    date: Optional[str]
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


class NewsCheck(NamedTuple):
    searches: tuple
    found: bool  # the model's view: something opened, advanced or made more plausible a YES path
    items: tuple
    summary: str


class Route(NamedTuple):
    """The verdict clean news checks confirm, or NEEDS_HUMAN and why, which
    no news check can change."""
    verdict: Verdict
    why: tuple = ()


class Screen(NamedTuple):
    verdict: Verdict
    why: tuple  # why it is NEEDS_HUMAN; empty otherwise
    runs: tuple
    news: tuple  # NEWS_RUNS NewsChecks; empty when step 2 was skipped


NEWS_CANT_SETTLE = ("news can't settle this market (live event, game in progress, data "
                    "release or private information)")


def _not_near_impossible(runs):
    counted = sum(r.near_impossible for r in runs)
    return (f"background {counted} of {len(runs)} near impossible",
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


def background_route(runs, days_left):
    """The Route the background runs leave for the news checks; days_left
    until the end date, None when unknown. QUIET does not rest on the runs'
    reasoning, so their rule quotes do not matter to it."""
    unsettled = () if all(r.settled_by_news for r in runs) else (NEWS_CANT_SETTLE,)
    if runs and all(r.near_impossible for r in runs):
        return Route(Verdict.NEEDS_HUMAN if unsettled else Verdict.NEAR_IMPOSSIBLE, unsettled)
    why = (*unsettled, *_not_quiet(days_left))
    if runs and not why:
        return Route(Verdict.QUIET)
    return Route(Verdict.NEEDS_HUMAN, (*_not_near_impossible(runs), *why))


def news_why(news):
    if len(news) < NEWS_RUNS:
        return ("recent news not checked",)
    return (*dict.fromkeys(f"recent news: {item.text}" for n in news for item in n.items),
            *(f"recent news (check {k}): {n.summary}"
              for k, n in enumerate(news, 1) if n.found and not n.items))


def decide(route, news):
    """The final Route: route's verdict when every news check came back clean,
    else NEEDS_HUMAN and why. news is empty when skipped."""
    if route.verdict is Verdict.NEEDS_HUMAN:
        return route
    why = news_why(news)
    return Route(Verdict.NEEDS_HUMAN, why) if why else route


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


def _on_page(item, fetch):
    try:
        text = fetch(item.url)
    except Exception:
        return None
    return quote_in(item.quote, text)


def verify_items(items, fetch):
    """items with verified set by reading each page through fetch (fetch_page
    or a stand-in)."""
    return tuple(item._replace(verified=_on_page(item, fetch)) for item in items)


BACKGROUND_SCHEMA = {
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

NEWS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["searches", "found", "items", "summary"],
    "properties": {
        "searches": {"type": "array", "items": {"type": "string"}},
        "found": {"type": "boolean"},
        "items": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "quote", "url"],
            "properties": {"text": {"type": "string"}, "quote": {"type": "string"},
                           "url": {"type": "string"}, "date": {"type": "string"}},
        }},
        "summary": {"type": "string", "maxLength": SUMMARY_MAX},
    },
}


class Step(NamedTuple):
    name: str
    tools: str
    schema: dict
    system: str


BACKGROUND = Step("background", "", BACKGROUND_SCHEMA, (
    "You screen prediction markets for one property: whether YES is near impossible under "
    "the market's rules before its end date. You have no web access. You do not estimate "
    "probabilities."))
NEWS = Step("news", "WebSearch,WebFetch", NEWS_SCHEMA, (
    "You check recent news for one prediction market: whether anything has opened, "
    "advanced or made more plausible a way for it to resolve YES. Search the web and read "
    "the pages you cite. You do not estimate probabilities. Prediction markets, betting "
    "odds and forecasting sites are not news."))


def _market(question, end, rules):
    return f"Question: {question}\nEnd date: {end}\nRules, verbatim:\n{rules}"


def build_background_prompt(question, end, rules, today):
    """Step 1's prompt: the question, its end date (text) and its rules verbatim."""
    return f"""Today is {today.isoformat()}. Your training data may be older than today.

{_market(question, end, rules)}

1. List every way this market could resolve YES under the rules as written, including loose
   readings of the wording (a statement, a post, an official claim, a technicality, a 50-50 or
   early resolution). For each, copy the exact words of the rules that would make it count into
   rule_quote (verbatim, or empty if no clause covers it), and say whether it is realistic before
   the end date.
2. settled_by_news: true if public news reporting would show whether one of these ways has
   opened. False when it turns on something news does not report as it happens: a livestream,
   a game in progress, a pending data release, private information, or a price, index, rate or
   other figure that can cross its threshold without any news first.
3. answer, judged assuming nothing unusual has happened recently (a separate news check
   covers that):
   - near_impossible: every path to YES needs something extraordinary (physically, legally or
     politically absurd) before the end date.
   - possible: at least one realistic path to YES exists.
   - unsure: you cannot tell.
   When in doubt, answer unsure. A wrong near_impossible loses money; unsure costs nothing.
4. reason: at most {REASON_MAX} characters on why."""


def build_news_prompt(question, end, rules, runs, today):
    """Step 2's prompt: the market and the YES paths the background runs listed."""
    paths = dict.fromkeys(p.path for r in runs for p in r.yes_paths)
    listed = "\n".join(f"- {p}" for p in paths) or "- none listed"
    return f"""Today is {today.isoformat()}.

{_market(question, end, rules)}

Ways it could resolve YES, listed by a model whose knowledge may end months before today
{listed}

1. First search the market's subject broadly for what has happened from \
{NEWS_SINCE.isoformat()} through {today.isoformat()}, without assuming the list above is \
complete. Then search each listed way. List every search you run in searches.
2. items: each development that opened, advanced or made more plausible a way to YES, listed \
above or not, even if other reports contradict it. That includes official steps; pressure, \
orders or demands from people who can make it happen; precedent, or peers doing the same \
thing; credible reports of talks, plans or intent. A denial does not cancel an item: list the \
item and put the denial in summary. text: what happened, one sentence. quote: a sentence \
copied verbatim from the page. url: the page. date: YYYY-MM-DD when the page gives one. Only \
news that purely restates the status quo or points away from YES goes in summary alone.
3. found: true if anything opened, advanced or made more plausible a way to YES.
4. summary: at most {SUMMARY_MAX} characters on what the news shows.

Do not estimate a probability. Prediction markets, betting odds and forecasting sites are \
not news."""


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


def parse_background(cli_output, rules):
    """The BackgroundRun in step 1's claude output, its rule quotes checked
    against rules, or TriageError."""
    obj = _structured(cli_output)
    if obj.get("answer") not in ANSWERS:
        raise TriageError(f"answer {obj.get('answer')!r} is not one of {ANSWERS}")
    return BackgroundRun(obj["answer"], _typed(obj, "settled_by_news", bool),
                         tuple(_yes_path(p, rules) for p in _typed(obj, "yes_paths", list)),
                         _typed(obj, "reason", str))


def _news_item(obj):
    when = obj.get("date") if isinstance(obj, dict) else None
    if when is not None and not isinstance(when, str):
        raise TriageError(f"date {when!r} is not a str")
    return NewsItem(_typed(obj, "text", str), _typed(obj, "quote", str), _typed(obj, "url", str),
                    when or None, None)


def parse_news(cli_output):
    """The NewsCheck in step 2's claude output, its items not yet verified, or
    TriageError."""
    obj = _structured(cli_output)
    return NewsCheck(_strings(obj, "searches"), _typed(obj, "found", bool),
                     tuple(_news_item(i) for i in _typed(obj, "items", list)),
                     _typed(obj, "summary", str))


def to_result(screen):
    """A Screen as the JSON object the triage table stores."""
    return {"verdict": screen.verdict.value, "why": list(screen.why),
            "runs": [{**r._asdict(), "yes_paths": [p._asdict() for p in r.yes_paths]}
                     for r in screen.runs],
            "news": [{**n._asdict(), "searches": list(n.searches),
                      "items": [i._asdict() for i in n.items]} for n in screen.news]}


def from_result(obj):
    """The Screen a stored to_result object of this PROMPT_VERSION holds."""
    return Screen(
        Verdict(obj["verdict"]), tuple(obj["why"]),
        tuple(BackgroundRun(**{**r, "yes_paths": tuple(YesPath(**p) for p in r["yes_paths"])})
              for r in obj["runs"]),
        tuple(NewsCheck(**{**n, "searches": tuple(n["searches"]),
                           "items": tuple(NewsItem(**i) for i in n["items"])})
              for n in obj["news"]))


LABELS = {Verdict.NEAR_IMPOSSIBLE: "Near impossible", Verdict.QUIET: "Quiet",
          Verdict.NEEDS_HUMAN: "Needs human"}


class Development(NamedTuple):
    """One news item, merged across the news checks that reported it."""
    text: str
    url: str
    date: Optional[str]
    verified: Optional[bool]  # True if any copy was found on its page, else False if any was read
    checks: tuple  # 1-based indexes of the news checks that reported it

    @property
    def sourced(self):
        return _web(self.url)


class PathSummary(NamedTuple):
    """One way to YES, merged across the background runs that listed it."""
    path: str
    runs: int
    realistic: int  # how many of those runs called it realistic
    quote_missing: bool  # some run's rule quote for it is not in the rules


class Digest(NamedTuple):
    verdict: Verdict
    reason: str  # the sentence deciding the verdict, without the verdict's label
    developments: tuple  # newest first, undated last
    paths: tuple  # listed by the most runs first, then called realistic by the most
    agreement: str  # how the runs answered, as "near impossible 2 of 3, possible 1"

    @property
    def headline(self):
        return f"{LABELS[self.verdict]}: {self.reason}"


def _url_key(url):
    """url with its scheme and host lowercased and its fragment, utm_*
    parameters and trailing slash dropped."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                       urlencode(query), ""))


def _either(a, b):
    return True if True in (a, b) else False if False in (a, b) else None


def _developments(news):
    merged = {}
    for k, check in enumerate(news, 1):
        for item in check.items:
            key = (("url", _url_key(item.url)) if item.sourced
                   else ("text", _normalize(item.text)))
            seen = merged.get(key)
            merged[key] = (
                Development(item.text, item.url, item.date, item.verified, (k,)) if seen is None
                else seen._replace(date=seen.date or item.date,
                                   verified=_either(seen.verified, item.verified),
                                   checks=tuple(dict.fromkeys((*seen.checks, k)))))
    return tuple(sorted(merged.values(), key=lambda d: d.date or "", reverse=True))


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


def _reason(screen, developments, days_left):
    runs, checks = len(screen.runs), len(screen.news)
    if screen.verdict is Verdict.NEAR_IMPOSSIBLE:
        return (f"{runs}/{runs} runs found no realistic way to YES and {checks}/{checks} news "
                "checks found nothing.")
    if screen.verdict is Verdict.QUIET:
        ends = "" if days_left is None else f", ends in {days_left} days"
        return (f"a realistic way to YES exists, {checks}/{checks} news checks found nothing "
                f"pointing to it{ends}.")
    if len(developments) == 1:
        return "1 news development points toward YES."
    if developments:
        return f"{len(developments)} news developments point toward YES."
    return (screen.why[0].rstrip(".") if screen.why else "no reason recorded") + "."


def digest(screen, days_left):
    """screen summed up for a reader: its verdict and the reason that decided
    it, each news development and way to YES once, and how the runs answered.
    days_left until the end date, None when unknown."""
    developments = _developments(screen.news)
    return Digest(screen.verdict, _reason(screen, developments, days_left), developments,
                  _paths(screen.runs), _agreement(screen.runs))


def run_claude(step, prompt, timeout_s=TIMEOUT_S):
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
                                  encoding="utf-8", errors="replace", timeout=timeout_s,
                                  cwd=cwd)
        except subprocess.TimeoutExpired:
            raise TriageError(f"claude ({step.name}) did not finish within {timeout_s}s") from None
    if not done.stdout.strip():
        raise TriageError(f"claude ({step.name}) exited {done.returncode}: "
                          f"{done.stderr.strip()[-300:]}")
    return done.stdout


def _ask(run, step, prompt, n):
    """n independent outputs of run(step, prompt), asked all at once."""
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda _: run(step, prompt), range(n)))


def screen(question, end, rules, today, run=run_claude, fetch=fetch_page):
    """(Screen, cost_usd of every call) of one market from its rules text and
    end date (None when unknown). run is run_claude or a stand-in; fetch reads
    the pages news items cite."""
    end_text = end.isoformat() if end else "unknown"
    outputs = _ask(run, BACKGROUND, build_background_prompt(question, end_text, rules, today),
                   RUNS)
    runs = tuple(parse_background(o, rules) for o in outputs)
    route = background_route(runs, days_until(end, today))
    news = ()
    if route.verdict is not Verdict.NEEDS_HUMAN:
        checked = _ask(run, NEWS, build_news_prompt(question, end_text, rules, runs, today),
                       NEWS_RUNS)
        news = tuple(n._replace(items=verify_items(n.items, fetch))
                     for n in map(parse_news, checked))
        outputs += checked
    return Screen(*decide(route, news), runs, news), sum(_cost(o) for o in outputs)


RULES_UNAVAILABLE = Screen(Verdict.NEEDS_HUMAN, ("rules unavailable",), (), ())


def screen_and_store(conn, condition_id, now, fetch_rules, run=run_claude, fetch=fetch_page):
    """Screen one market and store the result, or the error that stopped it;
    the stored farm_db.TriageRecord. fetch_rules(condition_id) returns its
    remote.Rules; a market whose rules are blank needs a human, unasked."""
    record = farm_db.read_markets(conn, (condition_id,))[0]
    end = record.end_date.date() if record.end_date else None
    book_mid = record.metrics.get("book_mid")
    started = time.monotonic()
    try:
        rules = fetch_rules(condition_id).description.strip()
        result, cost = ((RULES_UNAVAILABLE, None) if not rules else
                        screen(record.question or record.slug, end, rules, now.date(), run, fetch))
    except Exception as e:
        farm_db.store_triage(conn, condition_id, now, PROMPT_VERSION, MODEL,
                             error=f"{type(e).__name__}: {e}", book_mid=book_mid,
                             duration_s=time.monotonic() - started)
    else:
        farm_db.store_triage(conn, condition_id, now, PROMPT_VERSION, MODEL,
                             result=to_result(result), book_mid=book_mid,
                             duration_s=time.monotonic() - started, cost_usd=cost)
    return farm_db.latest_triage(conn, condition_id, PROMPT_VERSION)
