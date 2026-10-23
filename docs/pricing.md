# How the bids are priced

The screener and the farmer price the same two bids by the same rules. The
screener prices them once, from the book at scan time. The farmer prices them
again every cycle, with your `p_true`, your budget, and the shares it holds.
A market with no `p_true` follows the book: each cycle the farmer uses the
book midpoint as its `p_true`, with no EV margin under it (see
[Markets without a p_true](farmer.md#markets-without-a-p_true)).
Q_one, Q_two, and the slot names used here are defined in
[The four slots](farmer.md#the-four-slots).

## The strategy

The strategy targets markets whose favorite outcome trades at 90c or more. You
rest a favorite bid just under the midpoint, priced where its rewards minus
the cost of its fills are highest (see [The fill-cost model](#the-fill-cost-model)).
You also rest a longshot bid far below
the longshot midpoint. Above a 0.90 midpoint Polymarket only pays for
two-sided liquidity, so the longshot bid exists to make the favorite bid
score.

## The longshot bid

The longshot bid, `LONG_BID`, exists only to make the favorite bid score. You
do not want it filled, so it rests at the cheapest price that still balances
the favorite bid.

### The three zones

A naked longshot bid at price q is a favorite ask at 1 - q. Whether it fills
depends on the annual return that ask gives a favorite buyer who holds to the
end date, `(1 / (1 - q)) ^ (1 / years left) - 1`, not on its price alone. Of
about 20,000 favorite buys on 250 markets, 0.4% accepted under 2% a year,
2.8% under 5%, 23% under 10%, 74% under 20% and 90% under 30%. That return
puts each longshot price in one of three zones:

| Zone | Return a fill gives a favorite buyer | What limits the bid |
| --- | --- | --- |
| Free | Under 5% a year (`FREE_RETURN` in `farm/reward_screener.py`) | The cost test below, not the naked budget |
| Budgeted | From 5% to 20% a year | The naked budget, which you cap per market when you approve it. The bid's capital is naked risk, and the favorite bid shrinks to what the budget balances |
| None | Over 20% a year (`MAX_RETURN`, a test value) | No longshot bid rests there, and so no favorite bid it would balance |

The free zone reaches 0.3c on a market 25 days from its end date and 1.1c on
one 83 days out. Returns rise as the end date nears, so a price moves from
one zone to the next without the book changing. A scan labels each plan by
its longshot bid: mode `floor` in the free zone and `budget` above it.

### The cost test for a free price

A free price escapes the budget only where a share costs at most $0.50 for
each unit of its score (`FREE_MAX_COST` in `farm/reward_screener.py`). A 0.1c
share must score 0.002. A free price that fails the test is limited by the
budget like any other.

A bid at a price that passes is sized to balance the favorite bid's score,
whatever that costs, so the favorite bid is never trimmed for it. The test
caps the cost of each unit of score, not the dollars the bid ties up. Without
it, the size that covers, Q_one divided by the score, would run to infinity
at the edge of the reward band, where the score runs to zero.

- At 0.1c with a good score, the bid costs cents.
- At the bound, the bid costs $0.50 for each unit of score it covers. That is
  at most half the favorite bid's score times its size, in dollars. It comes
  to about $100 beside a $1,000 favorite bid, and to several hundred where
  the favorite bid sits close to the midpoint.

### Which price the bid takes

The bid takes the cheapest price that passes one of two tests:

- The price is free and passes the cost test. On most markets that price is
  0.1c.
- The covering size fits the budget. The covering size is enough to balance
  the favorite bid's score and at least the reward minimum. Its cost, plus
  any longshot shares held at cost, must stay within the market's approved
  budget.

The cheapest price offers a favorite buyer the least return and sits deepest
in the book, behind more longshot bids, so it fills least often.

Three limits hold at every price:

- The bid never sits above the cheapest price that scores `--min-low-score`
  per share.
- The bid never rests where a fill would give a favorite buyer over 20% a
  year.
- A bid smaller than the market's reward minimum size is dropped, because it
  would carry risk without scoring.

When no price up to the `--min-low-score` cap balances the favorite bid
within the budget, the bid sits at the price where the budget buys the most
score, sized to the budget. The favorite bid shrinks to match, because score
on one side beyond the other earns nothing. A market with no such price gets
no longshot bid. Its favorite bid then shrinks to what the covered `FAV_ASK`
balances, or does not rest. The exits are unaffected.

### What changes while the farmer runs

A scan and the Review preview price the bid once and assume the midpoint has
held. The farmer applies four more rules.

- **A price must stay free for ten minutes.** A price is free of the budget
  only once it has passed the cost test on every cycle for ten minutes
  (`FREE_DWELL_S` in `farm/reward_farmer.py`). The farmer keeps the lowest
  midpoint of those ten minutes, where a longshot share scores least, and
  tests each price there too. One cycle that fails puts the price back under
  the budget at once. A midpoint that flickers across the test therefore
  rests the bid at a dearer price that passed throughout, or under the budget
  where there is none. At start the farmer takes that history from
  `farm_mids`, and a market with none starts under its budget.
- **A longshot fill puts every price under the budget.** Once a longshot bid
  has filled, the budget, less the shares held at cost, limits every price
  until `LONG_ASK` has sold them. The exit adds to Q_one, and a bid sized
  without the budget to balance it would grow with every fill.
- **A resting bid changes zone as the end date nears.** A bid whose price has
  come to offer 5% a year goes back under the budget. A bid whose price has
  come to offer over 20% is replaced by a cheaper one or cancelled. Returns
  only rise, so each happens once.
- **The bid is replaced more often near the edge of the reward band.** A
  small move of the midpoint there changes the bid's score, and so its size,
  a lot. At 0.1c a replacement only moves the bid further back in the queue.

The budget counts a resting longshot bid whose fill would give a favorite
buyer 5% a year or more, plus the longshot shares held, at cost.

## The fill-cost model

A favorite share bought by a fill ties up capital that could earn the hurdle
rate r (`--hurdle-rate`) elsewhere, until it leaves. The farmer offers it back
through `FAV_ASK`, and that ask sells slowly. Measured on 2026-10-10 by
replaying the sweeps of 169 markets against the buys that followed them, the
ask the farmer rests after a fill sold 67.4% of a 100-share fill and 41.9%
of a 1,000-share fill in 30 days. So a share sells after a hold T, taken as
exponential with rate λ a year and cut short at resolution, Y years out,
where a share still unsold is worth p_true. λ is the rate that sells the
measured fraction in 30 days. The fraction runs in a straight line in the
log of the inventory through the two measured sizes, stays at 67.4% under
100 shares and never goes under 5%. The line gives 2,000 shares 34.2%,
where the replay measured 30.9%. The inventory is the favorite shares the
farmer already holds plus the bid's size, so a fill on top of earlier fills
costs more than the first.

In the replay the shares that sold went at about the ask's price. So a share
bought at b is credited a sale at

    a(b) = min(ask(b), p_true)

where ask(b) is the `FAV_ASK` the farmer would rest once the bid fills. That
ask is the midpoint plus `--ask-offset`, or the midpoint an earlier fill
found plus `--ask-offset` while the midpoint is still under it. It is never
under the average cost of the inventory with the fill added plus
`--min-capture`. With δ = ln(1 + r) and k = λ + δ, the fill loses

    L(b) = b - (a(b) * λ/k * (1 - e^(-kY)) + p_true * e^(-kY))

today. For each favorite bid price b on the tick grid inside the reward band,
with the size Q it rests at once the quotes are balanced,

    net(b) = reward/day(b, Q) - Q * rate(m - b) * max(L(b), 0)

where m is the midpoint once your quotes join the book, and rate(d) is how
many times a day a sell sweep reaches more than d below the midpoint. A sweep
that reaches your bid fills all of it. On a short-dated market, or far under
the midpoint, L(b) is negative: the share is worth more than the bid pays.
A bid rests for its reward, so that fill costs nothing and is never counted
as income.

A bid behind a deep book is charged no fills. The model fills the whole bid
whatever rests ahead of it, but a sweep has to take the shares bid above your
price before it reaches your bid. So a bid with more than 20,000 shares bid
above its price (`DEPTH_FREE_SHARES` in `farm/reward_screener.py`) is valued
with a rate of zero, and its net is its reward. Shares at the bid's own price
do not count. Of the 23 fills of the farmer's favorite bids from 2026-10-10
to 2026-10-12 whose book was recorded, 19 had at most 1.1 times the bid's
size ahead, and the deepest of the other four had 12,582 shares ahead. In
the trade histories of the 273 quoting markets read on 2026-10-12, which
cover 13,921 market-days, an hour of selling into bids came to over 20,000
shares 37 times, once in 376 market-days.

The farmer chooses Q as well as b. A market's capital is the most its
favorite bid may be, favorite shares already held counted at cost. At each
price the farmer values all of the capital left, then three quarters, a half,
a quarter and an eighth of it, and leaves out a size under the reward
minimum. The reward is a share of the pool, so each further share earns less.
L grows with Q, because a larger inventory sells more slowly. So the size
that nets the most can be under the capital, and the farmer rests that size
at the price where it nets the most. Where the fills of all the capital cost
nothing, no smaller size nets more. A bid behind a deep book therefore rests
at all of the capital left. At a price with more than 20,000 shares ahead no
size is charged a fill, and the largest size earns the most reward. The price
is chosen by the same net, so the bid can also move to the price behind that
depth where it earns the most. The naked budget is a separate limit where the
longshot bid sits outside the free zone: a bid it cannot balance shrinks to
what it balances, whatever the capital. A scan sizes its bid at all of
`--capital`.

A scan holds no shares and has no p_true, so it values a fill with an
inventory of the bid alone and the book midpoint as p_true. The farmer's ask
rests over the midpoint, so a scan's a(b) is the midpoint. The farmer values
a market that follows the book the same way, with the shares it holds.

The measurement is the table `SOLD` in `farm/fill_cost.py`, next to the command
that produced it. No fill under 53 days from resolution was replayed for the
30 days.

rate(d) comes from the market's fill history: its last 60 days of trades from
the data API, the makers each trade filled included, and the favorite's
minute midpoints from the CLOB. Trades that sold the favorite or bought the
longshot hit favorite bids. Trades less than a minute apart chain into one
sweep, for at most 10 minutes. A sweep counts at distance d when its deepest
fill traded more than d below the midpoint a minute before it began, and that
midpoint was at or above `--min-fav`. Sweeps within an hour of the last one
counted are one event, because a burst fills a resting bid once. The data API
serves only 2,000 rows of a market, so a busy market's history covers less
than 60 days. Events are counted at 12 distances from 0 to 10c, and rate(d)
runs in a straight line between them.

Few markets see enough sweeps to be rated on their own. A scan therefore
pools the histories of a random sample of the markets it values into a
fill prior: per distance, the events a day across those markets, and a
strength in days that says how much the markets differ around that pooled
rate. The sample is random because the prior values mostly quiet markets,
and the busy top markets would inflate it. A market's rate is its own count
shrunk toward the prior,

    rate(d) = (k + s * p) / (t + s)

where k is the market's events at d, t its days of history, p the pooled rate
and s the prior's strength. A nearer bid never gets a lower rate than a
farther one. A scan needs at least 20 sampled histories to fit a prior; with
fewer, the stored prior stays. Until a scan stores one, a built-in prior fit from
256 markets in October 2026 stands in.

The history also carries an exit clock. Each event counted 1c below the
midpoint starts a clock, which stops at the first minute midpoint, a minute
or more after the sweep began, back at or above the midpoint the sweep found.
A clock still running when the history was read counts its time so far, and
a scan pools the clocks into the prior. Fills were valued on that clock, as
if a share sold once the midpoint was back, until the ask's sales were
measured: the midpoint is back within days and the ask sells over weeks. The
clock is still read and stored with each history, and nothing values a fill
with it.

The model leaves out fills of the longshot bid, and the farmer's own record of
its fills.
