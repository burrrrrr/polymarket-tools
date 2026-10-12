"""
What each term on the console means, in one place. Templates call
{{ tip('key') }} right after a label, header or badge; an unknown key raises,
so a typo fails the page instead of rendering an empty tip.
"""

from typing import NamedTuple

from markupsafe import Markup


class Term(NamedTuple):
    """label names the term in the tip's "What does <label> mean?"."""

    label: str
    text: str


GLOSSARY = {
    # modes and outcomes
    "mode": Term("Mode", "Floor or budget: where the scan planned the longshot bid. Floor is the "
                         "free zone, at a price where a fill would give a favorite buyer under "
                         "5% a year, which the naked budget does not limit. Budget is above it, "
                         "within the budget."),
    "floor": Term("Floor", "The scan planned the longshot bid in the free zone: at a price where "
                           "a fill would give a favorite buyer under 5% a year to the end date, "
                           "0.1c on most markets. Almost no buyer takes that, so where a share "
                           "there costs at most $0.50 for each unit of its score, the bid is "
                           "sized to balance the favorite bid whatever that costs: the naked "
                           "budget does not limit it. The price must have passed that test for "
                           "ten minutes: after one dip the bid moves to a dearer price that "
                           "passed throughout, or back under the budget where there is none."),
    "budget": Term("Budget", "The scan planned the longshot bid where a fill would give a "
                             "favorite buyer 5% a year or more, because no price in the free "
                             "zone qualifies. That bid is real risk, capped by the market's "
                             "naked budget, and the favorite bid shrinks to what it balances. No "
                             "longshot bid rests where a fill would give over 20% a year."),
    "favorite": Term("Favorite", "The outcome priced at 90c or more. The farmer bids on it and "
                                 "is happy to be filled below your p_true."),
    "longshot": Term("Longshot", "The other outcome. The farmer bids on it only so the "
                                 "favorite bid earns rewards, and doesn't want those bids "
                                 "filled."),

    # the market's reward terms and the screener's figures
    "pool": Term("Pool", "The market's daily reward pool in USD, split among market makers "
                         "by reward score."),
    "band": Term("Band", "Max spread: how far from the midpoint an order can sit and still "
                         "earn rewards. An order scores ((v − distance) / v)², so 1 at the "
                         "midpoint and 0 at the edge."),
    "band_strip": Term("Reward band strip", "The reward band on the favorite's price axis. "
                                            "Blue is our favorite bid, orange our longshot bid "
                                            "at its mirror price (1 minus its price), grey the "
                                            "book's best bid and ask."),
    "min_size": Term("Min size", "Orders smaller than this many shares earn no rewards."),
    "est_daily": Term("Est $/day", "Our estimated share of the daily pool if the book stays "
                                   "as it was when read and nobody reacts. Use it to rank "
                                   "markets; it is optimistic."),
    "planned_capital": Term("Capital", "USD the planned quotes would tie up: the favorite bid "
                                       "plus the longshot bid."),
    "apr": Term("APR", "Estimated $/day ÷ capital × 365, before fill costs. A ranking figure, "
                       "not a forecast."),
    "fills_per_day": Term("Fills/day", "Favorite shares a day expected to fill our bid: its "
                                       "size × how often a sell sweep reached that far below "
                                       "the midpoint in the market's last 60 days, at most "
                                       "once an hour, shrunk toward the rate the last scan "
                                       "pooled across markets. Without the market's history "
                                       "it is that pooled rate alone, and the farmer does not "
                                       "bid."),
    "fill_cost": Term("Fill cost/day", "Fills/day × what each filled share loses: its price "
                                       "minus what it is worth today at the hurdle rate. The "
                                       "farmer's ask sells it slowly, measured at 67% of a "
                                       "100-share inventory and 42% of a 1,000-share one in "
                                       "30 days, at the ask's price or p_true if that is "
                                       "lower. A share unsold at the end date is worth "
                                       "p_true. Without a p_true it uses the midpoint. A "
                                       "sweep that reaches the bid fills all of it. Never "
                                       "under zero: a fill worth more than its price is not "
                                       "counted as income."),
    "net_apr": Term("Net APR", "Est $/day less the fill cost, ÷ capital × 365. Markets rank by "
                               "this."),
    "net_daily": Term("Net/day", "The favorite bid's share of the pool less what its fills "
                                 "cost a day, at the price the farmer chose for the best net. "
                                 "With no favorite bid, why none pays."),
    "days": Term("Days", "Days until the farmer stops: the market's end date minus the "
                         "expiry buffer."),
    "ahead": Term("Depth ahead", "Longshot shares bid above our longshot bid, as a multiple "
                                 "of our size. They fill before ours, so a higher number means "
                                 "our longshot bid is safer."),
    "vol24h": Term("Vol 24h", "USD traded in the last 24 hours."),
    "holding_rewards": Term("Holding rewards", "Polymarket pays a yield on shares held in "
                                               "this market, so inventory from fills earns "
                                               "something while you hold it."),
    "est_share": Term("Est share", "Our share of the reward score in this market, against "
                                   "the liquidity already resting in the band."),
    "score_per_share": Term("Score per share", "Reward score per share at this price: 1 at "
                                               "the midpoint, falling to 0 at the band edge."),
    "ties_up": Term("Ties up", "USD this order locks while it rests."),

    # your decision
    "p_true": Term("p_true", "Your probability that the favorite wins, as a whole percent "
                             "from 1 to 99. The favorite bid "
                             "never goes above p_true minus the EV margin, so any fill is +EV "
                             "by your own estimate."),
    "naked_budget": Term("Naked budget", "The most USD the longshot side may risk outside the "
                                         "free zone: a resting longshot bid whose fill would "
                                         "give a favorite buyer 5% a year or more, plus longshot "
                                         "shares held, at cost. A bid offering under 5% a year "
                                         "is outside the budget where a share costs at most "
                                         "$0.50 for each unit of its score: it is sized to "
                                         "balance the favorite bid. Once a longshot bid has "
                                         "filled, the budget limits every price until the shares "
                                         "are sold. A scan prices every market's longshot bid "
                                         "with the scan's naked budget, so set it to what you'd "
                                         "approve with."),
    "capital_cap": Term("Capital cap", "The most USD this market may tie up on the favorite "
                                       "side: resting favorite bids plus favorite shares held, "
                                       "at cost. The farmer rests the bid size at or under it "
                                       "that nets the most. Leave blank to use the farmer's "
                                       "default."),
    "ev_check": Term("EV check", "Compares the planned favorite bid with your p_true minus "
                                 "the EV margin. The farmer never bids above that cap, and "
                                 "below it picks the price with the best net of fill costs."),

    # live state
    "mid_effective": Term("Mid (effective)", "The reward midpoint including our own favorite "
                                             "bid. In a wide book our bid becomes the best bid "
                                             "and pulls the midpoint toward itself."),
    "book_mid": Term("Book mid", "The midpoint of other people's orders only, ignoring "
                                 "orders below the minimum size."),
    "q1_q2": Term("Q1 / Q2", "Our reward score on each side. Q1 is favorite bids plus "
                             "longshot asks; Q2 is favorite asks plus longshot bids. Above 90c "
                             "only the smaller side pays, so the farmer keeps them equal."),
    "naked_at_risk": Term("Naked at risk", "USD in resting longshot bids, in the free zone and "
                                           "above it. If all of them filled and the longshot "
                                           "lost, this is what you'd lose. A bid sized without "
                                           "the naked budget does not count against it."),
    "capital_in_bids": Term("Capital in bids", "USD locked in resting favorite bids."),
    "farm_inventory": Term("Farm inventory", "Shares the farmer bought through fills, above "
                                             "the baseline. Only these are offered back for "
                                             "sale."),
    "baseline": Term("Baseline", "Shares you already held when the farmer started managing "
                                 "this market. The farmer never sells them."),
    "rate_now": Term("Rate now", "Polymarket's live scoring share of each quoting market's "
                                 "pool. In dry run, or before the first share arrives, the "
                                 "farmer's modeled share stands in; it runs low. Only "
                                 "quoting markets count, one without a favorite bid at 0. "
                                 "Per hour is per day ÷ 24."),
    "earned_today": Term("Earned today", "Rewards Polymarket has credited today in every "
                                         "market the wallet earns in, split into farmer-"
                                         "managed and other. Paid daily at midnight UTC; "
                                         "live mode only."),
    "earned_total": Term("Total earned", "Every reward Polymarket has credited, summed over "
                                         "each day the farmer fetched from the earnings "
                                         "API. Live mode only."),
    "scoring_share": Term("Scoring share", "Polymarket's live figure for the share of this "
                                           "market's rewards we are currently earning."),
    "edge_vs_p_true": Term("Edge vs p_true", "p_true minus the fill price. Positive means the "
                                             "fill was +EV by your estimate. Approximate, "
                                             "because fill prices are inferred."),
    "book_in_band": Term("Book inside the band", "Resting orders close enough to the midpoint "
                                                 "to earn rewards. Our longshot orders appear "
                                                 "at their mirror price, 1 minus the price."),
    "expiry_cutoff": Term("Expiry cutoff", "End date minus the expiry buffer. Every order is "
                                           "GTD and expires then, and the market retires."),
    "naked_leg": Term("Naked leg", "The longshot bid. Pausing it removes the longshot bid, "
                                   "and the favorite bid with it, because one-sided quotes "
                                   "earn nothing above 90c."),
    "sole_bid": Term("Sole bid", "Normally the farmer takes its orders down when no other "
                                 "bid supports the midpoint. With this allowed it keeps its "
                                 "favorite bid there instead, at full size, as the only bid "
                                 "in the reward band, until another bid returns. Every sale "
                                 "into the book fills that bid, so allow it only on a market "
                                 "you are near certain of."),

    # order slots and their decisions
    "FAV_BID": Term("FAV_BID", "Buys the favorite below the midpoint, at the price and the "
                               "size within the capital cap that earn the most rewards net "
                               "of what its fills cost: a filled share "
                               "ties up capital until FAV_ASK sells it, or until the market "
                               "resolves if it never does."),
    "LONG_BID": Term("LONG_BID", "Buys the longshot far below its midpoint. It exists only to "
                                 "make the favorite bid earn rewards; this is the naked leg."),
    "FAV_ASK": Term("FAV_ASK", "Sells favorite shares the farmer bought, above the midpoint "
                               "and above their cost. It frees capital and earns the spread."),
    "LONG_ASK": Term("LONG_ASK", "Sells longshot shares picked up from a longshot fill."),
    "hold": Term("Hold", "The resting order is still close enough to its target, so it stays "
                         "put and keeps its place in the queue."),
    "backoff": Term("Backoff", "The exchange rejected this order recently. The farmer waits "
                               "(30s, doubling up to 15 min) before trying again."),
    "preview": Term("Preview", "The market is paused, so nothing is sent. This is the order "
                               "the farmer would place on its first cycle after Resume, "
                               "planned against the live book with the approved terms."),
    "start_paused": Term("Start paused", "Approve with the market paused. The farmer plans its "
                                         "orders against the live book and shows them on the "
                                         "market page as a preview without placing them. "
                                         "Click Resume there to place them."),

    # phases
    "QUOTING": Term("Quoting", "Orders are resting and earning rewards."),
    "SOLE_BID": Term("Sole bid", "No other bid supports the midpoint, and you allowed a sole "
                                 "bid on this market. The favorite bid rests at full size as "
                                 "the only bid in the reward band, two ticks above the lowest "
                                 "price that keeps the longshot bid scoring, and the longshot "
                                 "bid stays as it rests. If no favorite bid fits around it, "
                                 "the longshot bid moves up, to at most the price that offers "
                                 "a favorite buyer under 7% a year. Every sale into the book "
                                 "fills the favorite bid. Quoting resumes once other bids "
                                 "have supported the market for five minutes."),
    "COOLDOWN": Term("Cooldown", "The favorite's ask fell sharply (by at least the drop "
                                 "threshold within the drop window). The ask is the price "
                                 "at which the favorite's asks add up to three reward "
                                 "minimums. All orders are pulled until the cooldown ends "
                                 "or you end it with End cooldown."),
    "BELOW_RANGE": Term("Below range", "The favorite's midpoint is below the minimum. All "
                                       "orders are pulled until it recovers 0.5c above it."),
    "PAUSED": Term("Paused", "You paused this market. All orders are pulled until you resume "
                             "it."),
    "UNREWARDED": Term("Unrewarded", "Polymarket's reward listing no longer pays this "
                                     "market. All orders are pulled, and quoting resumes "
                                     "on its own if rewards return."),
    "RETIRED": Term("Retired", "The farmer stopped managing this market: market closed or "
                               "decided, expiry cutoff reached, or you retired it."),
    "WAITING": Term("Waiting", "Approved, but the farmer hasn't picked it up yet. It starts "
                               "on its next cycle."),

    # settings and scan parameters
    "capital": Term("Capital per market", "The most USD one market may tie up on the "
                                          "favorite side: its resting bid plus farm inventory "
                                          "at cost. The farmer rests the bid size at or under "
                                          "it that nets the most. A scan sizes its planned "
                                          "favorite bid at all of it."),
    "max_lead": Term("Max lead", "The most the favorite bid may sit above the best bid "
                                 "already in the book, in price: 0.005 is 0.5c. 0 joins "
                                 "the best bid."),
    "ask_offset": Term("Ask offset", "How far above the midpoint the favorite exit (FAV_ASK) "
                                     "sits, in price."),
    "min_capture": Term("Min capture", "No exit, favorite or longshot, sells below average cost "
                                       "plus this, in price."),
    "ev_margin": Term("EV margin", "The favorite bid never goes above p_true minus this, so "
                                   "every fill keeps at least this much edge."),
    "min_fav": Term("Min favorite mid", "Markets whose favorite trades below this are skipped "
                                        "by the screener and pulled to BELOW_RANGE by the "
                                        "farmer."),
    "min_low_score": Term("Min longshot score", "The longshot bid never sits above the cheapest "
                                                "price scoring at least this per share. It takes "
                                                "the cheapest price in the free zone, where a "
                                                "fill gives a favorite buyer under 5% a year, "
                                                "whose share costs at most $0.50 for each unit "
                                                "of its score, whatever its size costs. "
                                                "Otherwise it takes the cheapest price whose "
                                                "size covering the favorite bid fits the naked "
                                                "budget, or else the price where the budget buys "
                                                "the most score, and the favorite bid shrinks to "
                                                "match."),
    "keep_ratio": Term("Keep ratio", "Keep a resting order while it scores at least this "
                                     "fraction of its target. A favorite bid stays while it "
                                     "earns, at its own price and size, at least this "
                                     "fraction of the best bid's net $/day."),
    "drop_threshold": Term("Ask drop threshold", "A fall this large in the favorite's "
                                                 "ask within the drop window starts a "
                                                 "cooldown. Only the ask counts, because a "
                                                 "bid leaving looks the same cancelled or "
                                                 "sold into, and the farmer re-plans against "
                                                 "the bids that remain."),
    "drop_window_s": Term("Drop window", "How many seconds back the ask's fall is "
                                         "measured."),
    "cooldown_s": Term("Cooldown", "Seconds all orders stay pulled after an ask drop."),
    "naked_cooldown_s": Term("Naked cooldown", "Pause the naked leg this long, in seconds, "
                                               "after a longshot fill."),
    "expiry_buffer_days": Term("Expiry buffer", "Days before the end date when every order "
                                                "expires (GTD) and the market retires."),
    "min_window_days": Term("Min window", "Skip markets with fewer farmable days left than "
                                          "this."),
    "horizon_cap_days": Term("Horizon cap", "The most days counted toward a market's rank, "
                                            "so a long-dated market doesn't outrank on time "
                                            "alone."),
    "min_rate": Term("Min pool", "Skip markets whose daily reward pool is smaller than this, "
                                 "in USD."),
    "excluded_categories": Term("Exclude categories", "Skip markets Polymarket tags with a "
                                                      "checked category. Every esports "
                                                      "market is also tagged sports, so "
                                                      "excluding sports excludes esports "
                                                      "too."),
    "fragile_drop": Term("Fragile mid drop", "Skip markets whose midpoint would fall this "
                                             "much if their best bid left. Our favorite bid "
                                             "would follow the next bid down and its score "
                                             "would collapse."),
    "hurdle_rate": Term("Hurdle rate", "The yearly return capital must beat, 0.40 is 40%. A "
                                       "filled favorite share costs its price minus what it "
                                       "sells for, discounted at this rate over its hold: "
                                       "until the farmer's ask sells it, weeks on average "
                                       "and longer the more shares are held, or until the "
                                       "end date. On a short-dated market a share can be "
                                       "worth more than its price. That fill costs nothing "
                                       "and is not counted as income."),
    "tape_markets": Term("Markets read for fills", "How many of the top markets read their "
                                                   "fill history: 60 days of trades and "
                                                   "midpoints. Up to 60 more drawn at random "
                                                   "read theirs too, and the scan pools "
                                                   "the random ones' sweeps into the fill "
                                                   "prior every market is valued on, since "
                                                   "the top ones are the busiest; a market "
                                                   "not read has that prior alone."),
    "heartbeat": Term("Heartbeat", "If the farmer stops sending heartbeats for 10s, "
                                   "Polymarket cancels every order owned by these API "
                                   "credentials, possibly including your other tools' "
                                   "orders."),
    "keep_orders": Term("Stop, keep orders", "Stops the farmer but leaves its orders "
                                             "resting, unmanaged until it starts again: no "
                                             "cooldowns, retirement, or fill handling, and "
                                             "they expire only at their GTD date. On start "
                                             "it keeps the ones still close to target; a "
                                             "market you unapprove meanwhile keeps its "
                                             "orders until they expire. "
                                             "The heartbeat, if on, cancels them within "
                                             "about 10s."),
    "cancel_all": Term("Cancel all", "Pauses every approved market, so the farmer cancels all "
                                     "their orders. Markets you don't farm are untouched."),
}

def tip(key):
    """The tip button for a glossary term; KeyError for an unknown key."""
    term = GLOSSARY[key]
    return Markup('<button type="button" class="tip" aria-label="What does {} mean?" '
                  'data-tip="{}"></button>').format(term.label, term.text)
