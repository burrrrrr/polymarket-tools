import html
import re
import unittest
from pathlib import Path
from unittest import mock

import jinja2

from console import forms, glossary, views
from console.glossary import GLOSSARY, Term
from test_console import A
from test_console_writes import FakeRemote, Writes

TEMPLATES = Path(__file__).parent.parent / "console" / "templates"
TIP = re.compile(r'<button type="button" class="tip" aria-label="([^"]*)" data-tip="([^"]*)">'
                 r'</button>')

# Each tip(...) call with a non-literal argument, and the keys it can take.
DYNAMIC = {
    "name": set(forms.SCAN_FIELDS),
    "r.key": set(forms.SETTING_FIELDS),
    "term": {"favorite", "longshot"},
    "c.phase": {*views.PHASES, views.WAITING},
    "m.phase": {*views.PHASES, views.WAITING},
    "s.slot": set(views.SLOTS),
    "o.slot": set(views.SLOTS),
    "f.slot": set(views.SLOTS),
    "plan.mode | lower": set(views.MODES),
    "c.mode | lower": set(views.MODES),
    "s.decision": {"hold", "backoff", "preview"},
}


def template_keys():
    """Every glossary key the templates can ask for, and the unknown call sites."""
    used, unknown = set(), []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for arg in re.findall(r"\btip\(([^)]*)\)", text):
            literal = re.fullmatch(r"'(\w+)'", arg.strip())
            if literal:
                used.add(literal.group(1))
            elif arg.strip() in DYNAMIC:
                used |= DYNAMIC[arg.strip()]
            else:
                unknown.append(f"{path.name}: tip({arg})")
        used |= set(re.findall(r"\bglossary\.(\w+)\.text", text))
    return used, unknown


def tips(page):
    """(aria-label, data-tip) of each tip button, unescaped."""
    found = [(html.unescape(a), html.unescape(t)) for a, t in TIP.findall(page)]
    assert len(found) == page.count('class="tip"'), "a tip button with unexpected markup"
    return found


class GlossaryTest(unittest.TestCase):
    def test_every_key_the_templates_use_exists_and_every_entry_is_used(self):
        used, unknown = template_keys()
        self.assertEqual(unknown, [], "register the dynamic tip() call in DYNAMIC")
        self.assertEqual(used - set(GLOSSARY), set(), "keys missing from the glossary")
        self.assertEqual(set(GLOSSARY) - used, set(), "glossary entries no page uses")

    def test_unknown_key_raises_instead_of_rendering_blank(self):
        with self.assertRaises(KeyError):
            glossary.tip("no_such_term")
        env = jinja2.Environment()
        env.globals["tip"] = glossary.tip
        with self.assertRaises(KeyError):
            env.from_string("<th>Pool{{ tip('no_such_term') }}</th>").render()

    def test_tip_escapes_its_text(self):
        with mock.patch.dict(GLOSSARY, {"pool": Term('A "pool"', "<b>x</b> & y")}):
            self.assertEqual(TIP.findall(glossary.tip("pool")),
                             [("What does A &#34;pool&#34; mean?", "&lt;b&gt;x&lt;/b&gt; &amp; y")])


class RenderedTipsTest(Writes):
    def app_kwargs(self):
        remote = FakeRemote()
        return {"fetch_info": remote.info, "fetch_book": remote.levels,
                "fetch_total_rates": remote.totals}

    # Per page, keys whose tips must be there.
    PAGES = {
        "/": {"capital_in_bids", "naked_at_risk", "farm_inventory", "rate_now", "est_daily",
              "earned_today", "q1_q2", "QUOTING"},
        "/discover": {"min_fav", "min_rate", "fragile_drop", "mode", "favorite", "pool", "band",
                      "est_daily", "planned_capital", "net_apr", "days", "ahead", "vol24h"},
        "/review/0xddd": {"budget", "band", "min_size", "band_strip", "ahead",
                          "holding_rewards", "p_true", "naked_budget", "capital_cap",
                          "ev_check", "start_paused", "sole_bid"},
        "/partials/review/0xddd/live?p_true=97&naked_budget=25": {
            "score_per_share", "ties_up", "FAV_BID", "LONG_BID", "est_share", "est_daily",
            "fill_cost", "fills_per_day", "net_daily", "planned_capital", "apr", "net_apr"},
        "/markets": {"QUOTING", "WAITING", "mid_effective", "book_mid", "q1_q2",
                     "naked_budget", "farm_inventory", "baseline", "FAV_BID", "LONG_ASK",
                     "hold", "expiry_cutoff", "naked_leg", "sole_bid", "floor"},
        f"/markets/{A}": {"QUOTING", "mid_effective", "book_mid", "capital_in_bids",
                          "expiry_cutoff", "net_daily", "backoff", "book_in_band",
                          "score_per_share",
                          "baseline", "longshot", "edge_vs_p_true"},
        "/activity": {"earned_today", "scoring_share", "est_daily", "rate_now", "earned_total"},
        "/activity?tab=fills": {"edge_vs_p_true", "FAV_BID"},
        "/activity?tab=inventory": {"baseline", "farm_inventory"},
        "/settings": {*forms.SETTING_FIELDS, "heartbeat", "keep_orders", "cancel_all"},
    }

    def test_each_page_renders_labelled_tips_for_its_terms(self):
        by_text = {term.text: key for key, term in GLOSSARY.items()}
        for url, keys in self.PAGES.items():
            with self.subTest(url=url):
                found = tips(self.client.get(url).text)
                for label, text in found:
                    self.assertRegex(label, r"^What does \S.* mean\?$")
                    self.assertIn(text, by_text, "data-tip is not a glossary text")
                self.assertEqual(keys - {by_text[text] for _, text in found}, set())

    def test_base_page_loads_tips_js_as_javascript(self):
        mtime = int((TEMPLATES.parent / "static" / "tips.js").stat().st_mtime)
        url = f"/static/tips.js?v={mtime}"
        self.assertIn(f'<script src="{url}" defer></script>', self.client.get("/").text)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/javascript"))

    def test_settings_and_scan_hints_come_from_the_glossary(self):
        old = GLOSSARY["keep_ratio"].text
        with mock.patch.dict(GLOSSARY, {
                "keep_ratio": Term("Keep ratio", "Changed keep-ratio text."),
                "min_rate": Term("Min pool", "Changed min-pool text.")}):
            settings = self.client.get("/settings").text
            discover = self.client.get("/discover").text
        self.assertIn(("What does Keep ratio mean?", "Changed keep-ratio text."), tips(settings))
        self.assertNotIn(html.escape(old), settings)
        self.assertIn(("What does Min pool mean?", "Changed min-pool text."), tips(discover))


if __name__ == "__main__":
    unittest.main()
