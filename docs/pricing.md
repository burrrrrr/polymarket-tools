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
farmer already holds plus what one fill takes of the bid, so a fill on top
of earlier fills costs more than the first.

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

    net(b) = reward/day(b, Q) - F(Q, A) * rate(m - b) * max(L(b), 0)

where m is the midpoint once your quotes join the book, rate(d) is how
many times a day a sell sweep reaches more than d below the midpoint, and A
is the shares other makers bid above b. Shares at the bid's own price do not
count. F is what one counted sweep fills of the bid, from
`SWEEPS` in `farm/fill_cost.py`:

- A counted sweep brings `sellers` sellers to the bid's price. The rate
  counts sweeps that trade through a price, and most fills are sells at the
  bid's own price, so a counted sweep brings more than one.
- Each seller sells X shares, X lognormal (`mu`, `sigma`).
- A share `hold` of the shares ahead holds in front of the bid, counting no
  more than `DEPTH_CREDIT_MAX` of them. The rest is cancelled or swept with
  it. So a seller fills min(Q, max(0, X - hold * min(A, DEPTH_CREDIT_MAX)))
  of the bid, and F(Q, A) is `sellers` times its mean. The farmer's bids
  behind more than `DEPTH_CREDIT_MAX` were still filled, whole ones
  included, so depth past it buys a bid nothing.

These are fit together to the farmer's own favorite bid fills, because a
seller hits many makers and the public tape does not say which shares
reached you. Each `FILL` event is one positions read with a fill, set beside
the bid it hit, rebuilt from the `PLACE` rows, and beside the shares bid
ahead of it in the `farm_books.db` sample it landed in. A fill that took 95%
or more of that bid, and no more, only says the seller wanted at least that
much, so it counts as censored there. A fill larger than the bid it was
joined to also hit another order, so it counts as an exact size. The fit
maximises the likelihood of where the fills landed and how much each took,
over every stretch the farmer rested a bid, with the rate's scale left free:
the scale is `sellers`. Without the stretches no fill says how often the
book ahead stopped a seller, and the fit finds no hold at all. F is about Q
per seller for a small bid at the front of the book, grows slowly for a
large one, and shrinks the deeper the book ahead. Refit with
`python -m analysis.fill_calibration fit --db data\farm.db --books
data\farm_books.db --cache DIR --until TIME` and paste `SWEEPS` with the
provenance it prints, which names the time and the scan prior the rates
shrank toward. `sellers` scales that prior's rates, so the report warns once
the stored prior is another one, and a new prior needs a refit. The report
prints the fitted values against your fills, by shares ahead among them.
Your own fills set the sweeps only: they are already trades in the public
tape, so they never add to a market's count. A filled share loses L(b) at
the inventory one fill leaves, the shares a seller who gets past the book
ahead takes on average.

On a short-dated market, or far under the midpoint, L(b) is negative: the
share is worth more than the bid pays. A bid rests for its reward, so that
fill costs nothing and is never counted as income.

The farmer chooses Q as well as b. A market's capital is the most favorite
inventory it may hold, shares already held counted at cost. The resting bid
may be larger: up to the bid multiple (`--bid-multiple`, default 4, or the
market's own, set on its approval) times the capital left, the bid room. A
sweep fills only part of a large bid, so a bid over the capital earns more
reward for about the same fills. One sweep can still fill all of it, which
is why the multiple is yours to set. Once the inventory at cost reaches the
capital, the bid room is nothing and no favorite bid rests. At each price the
farmer values all of the bid room, then three quarters, a half, a quarter
and so on while a size buys the reward minimum. The reward is a share of the
pool, so each further share earns less, while a larger bid fills more and L
grows with the inventory a fill leaves. So the size that nets the most can be
under the bid room. Where the fills cost nothing, no smaller size nets more,
and a bid behind a deep book costs little. The naked budget is a separate limit where the
longshot bid sits outside the free zone: a bid it cannot balance shrinks to
what it balances, whatever the bid room. So on most markets the longshot
cover, not the multiple, sets how large the bid is, and a larger room lets
the bid rest deeper with the same score. A sole bid rests at the capital
left, not the bid room: it values no fills. A scan sizes its bid at all of
`--capital`.

### The tail cap

Sellers that take a large bid are common, and a few of them buy much of
what the farmer is filled (the report's tail share). So a quoting favorite
bid also has a tail: what one seller dumping X's 99th percentile
(`TAIL_QUANTILE`, `Sweeps.dump`) would lose, past the share of the bids
ahead that holds, valued the way a fill is, with the inventory those shares
leave to sell,

    T(A, Q) = min(Q, max(0, dump - hold * min(A, DEPTH_CREDIT_MAX)))
    tail(b, Q) = T * max(L(b) at an inventory of the shares held plus T, 0)

The bid may rest only while its tail is at most `--tail-days` (default 30,
the `tail_days` setting, 0 for no cap) days of the plan's reward. A larger
bid, a higher price and a later end date each lose more, and depth ahead
past `DEPTH_CREDIT_MAX` lowers the tail no further. The tail grows faster
with the size than the reward does, so at each price the farmer passes the sizes over the cap,
largest first, and values each size on its own: its reward, and the slower
sale of a larger inventory. The cap shrinks the bid under the bid room,
whichever is tighter, and the price cap still applies. When the cap stops
every bid that would pay, the reason says how many days of reward the
cheapest tail costs. A resting bid stays while its tail is within a quarter
over the cap, so a bid the cap sized is not replaced each time its reward
dips. A sole bid is not capped.
`python -m analysis.fill_calibration report` prints what the cap does to the
markets quoting now, from no position, for the caps, quantiles and hurdles
you ask for.

### Choosing among bids that earn about the same

The farmer does not take the single best net. Several prices and sizes net
within a few percent of each other, and of bids that earn about the same,
the lowest costs least when it fills. Among the bids netting at least the
best net less the tie band (`--tie-band`, 5% of it), the farmer rests the
lowest price, at that price its best net, then its smaller size. A bid
already resting above that price stays while its own net is inside the band,
and is replaced once it falls out of it.

A scan holds no shares and has no p_true, so it values a fill with an
inventory of what one fill takes of the bid and the book midpoint as
p_true. The farmer's ask rests over the midpoint, so a scan's a(b) is the
midpoint. The farmer values a market that follows the book the same way,
with the shares it holds.

## After a fill

What the market's own fills say puts a ceiling on its next favorite bid,
the lower of these (`farm/fill_watch.py`):

- While the farm holds favorite shares, the bid stays a tick under the last
  fill and under their average cost. Shares held from before the farm
  managed the market blend the wallet's average cost, so there the last fill
  alone holds the bid.
- For 6 hours after a favorite ask sold, the bid stays a tick under its
  price. 37% of favorite fills were followed by another in the same market
  within 6 hours, against about 3% at random.

The ceiling lowers the bid's cap below p_true less the EV margin, for the
quoting bid and the sole bid alike, and a resting bid over it is replaced.
It does not change whether the other makers support a bid, so it never
starts a sole bid. A restart, or a market approved later, rebuilds the
ceilings from the stored fills.
The market page shows the ceiling and why, on the `FAV_BID` row.

Whether these rules, the fill model and the bid multiple pay is measured by
`python -m analysis.pricing_backtest run`, which walks the farmer through the
recorded books with its position carried, under today's rule, all the new
rules, and each new rule switched off.

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

The scan fits its prior at its own `--min-fav`, and the farmer reads
histories at its own. A history read at another floor counted other sweeps
and is dropped. A prior fit within 0.05 of the reader's floor is used, and
one fit farther off gives way to the built-in prior. That is a known
mismatch: the prior also counts sweeps from midpoints between the two
floors. Scanning at the farmer's `--min-fav` would make the floors agree by
construction.

The history also carries an exit clock. Each event counted 1c below the
midpoint starts a clock, which stops at the first minute midpoint, a minute
or more after the sweep began, back at or above the midpoint the sweep found.
A clock still running when the history was read counts its time so far, and
a scan pools the clocks into the prior. Fills were valued on that clock, as
if a share sold once the midpoint was back, until the ask's sales were
measured: the midpoint is back within days and the ask sells over weeks. The
clock is still read and stored with each history, and nothing values a fill
with it.

The model leaves out fills of the longshot bid.

## Checking the fill model

`python -m analysis.fill_calibration report --db ... --books ... --cache ...
--split DATE --until TIME` is the gate for any change to the fill model: the
sweeps, the prior rule or `FillCurve`. It scores predicted against realized
fills over every stretch the farmer rested a favorite bid before `--until`
(`farm_books.db`), with each market's history rebuilt as it was then from
the backtest cache. It prints events, shares and whole-bid fills (95% of the
bid or more) realized over predicted per model, over all the data, before
the split and from it, with 90% intervals from resampling markets. It breaks
them down by predicted events per market, by where the rate came from and by
the shares ahead of the bid, prints the fit with intervals from refitting
resampled markets, and prints the tail share, the top markets and the tail
cap on current markets. A change passes when three things hold. The
production model's interval over all the data includes 1, for events and
for shares. Sweeps fit before the split, scored from it, have a ratio inside
[0.67, 1.5] and an interval that reaches it. And production's whole-bid
fills over all the data, every depth ahead summed, pass that same band.

Only the sweeps are refit before the split. The rates shrink toward the
stored prior, which the scan fit over the last 60 days of each market's
trades, a window that overlaps the test window, so the events score from the
split is not fully held out.

The walk backtest (`python -m analysis.pricing_backtest run`) scores fills
from the public sells, the same tape the burst table came from, so it cannot
check a fill-model change.
