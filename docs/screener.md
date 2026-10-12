# Reward screener

`farm/reward_screener.py` ranks the markets that pay Polymarket liquidity
rewards for one farming strategy, and stores them in `farm.db` (SQLite) for
you to approve or reject. It is read-only. It places, cancels, and signs
nothing, and it needs no credentials.

[How the bids are priced](pricing.md) explains that strategy: the favorite
bid, the three zones of the longshot bid, and the fill-cost model.

## Commands

```bash
python -m farm.reward_screener                # same as 'scan' with defaults
python -m farm.reward_screener scan --mode floor --top 20 --json scan.json
python -m farm.reward_screener scan --exclude sports,politics
python -m farm.reward_screener list --status approved
python -m farm.reward_screener approve <slug-or-condition-id> --p-true 0.97 --budget 25
python -m farm.reward_screener approve <slug-or-condition-id> --p-true 0.97 --capital 200
python -m farm.reward_screener reject <slug-or-condition-id> --note "resolution risk"
```

## How a scan runs

A scan prints one line per stage, with the counts it kept and the reasons it
skipped the rest, then a table sorted by rank. Most of a scan is reading the
fill histories.

1. It reads `/rewards/markets/current`, which takes about ten seconds. It
   reads overlapping pages, so a market that leaves mid-read cannot hide one
   still listed.
2. The first pass assesses every market, only to rank them. It costs fills
   from the fill prior the last scan stored (see
   [The fill-cost model](pricing.md#the-fill-cost-model)).
3. Two sets of markets read their fill history, which is trades from the data
   API and minute midpoints from the CLOB, up to nine requests a market:
   - The top `--tape-markets` of that ranking.
   - 60 markets drawn at random from every market whose fills the first pass
     valued, kept or dropped as `fills cost more than the rewards`. The draw
     costs up to 60 more histories, fewer when it lands in the top, and
     about 50 more seconds.
4. It fits a new fill prior from the random sample's histories alone and
   stores it with the scan.
5. The second pass values every market on that prior, and each market that
   read its history on its own history too. A market whose fills cost more
   than its rewards is dropped as `fills cost more than the rewards`. A
   market that read no history is valued on the prior alone, marked `p` in
   the table.

The prior comes from the random sample because the top markets are the
busiest, while most of the markets the prior values trade little. A prior fit
from the top would cost every quiet market's fills too high. The draw
includes the markets the first pass dropped, so a stored prior that ran high
cannot narrow the sample to the busy markets it spared.

## Scan options

| Option | Default | Meaning |
| --- | --- | --- |
| `--min-fav` | 0.90 | The lowest favorite midpoint to consider |
| `--expiry-buffer-days` | 7 | Stop farming this many days before the market ends |
| `--min-window-days` | 30 | The fewest farmable days a market needs |
| `--horizon-cap-days` | 90 | Caps the days counted toward rank |
| `--capital` | 500 | The USD behind the favorite bid. A scan sizes its bid at all of it. The farmer treats a market's capital as the most it may bid and chooses the size |
| `--hurdle-rate` | 0.40 | Sets what a filled share costs, the same setting the farmer has. The scan has no p_true, so it costs a fill against the book midpoint |
| `--tape-markets` | 100 | How many of the top markets read their fill history. The fill prior is fit from the 60 randomly drawn markets' histories instead, and needs at least 20 of them |
| `--max-lead` | 0.005 | The most the favorite bid may sit above the best bid already in the book. 0 joins the best bid. The bid also stays one tick under the lowest ask |
| `--naked-budget` | 10 | The USD a longshot bid outside the free zone may cost. Set it to the budget you would approve with, since the farmer prices the bid the same way with the approved budget. [The longshot bid](pricing.md#the-longshot-bid) has the rule |
| `--min-low-score` | 0.10 | Caps the longshot bid's price. The bid never sits above the cheapest price that scores at least this per share |
| `--fragile-drop` | 0.03 | Skips a market as a `fragile midpoint` when pulling its best bid of at least the reward minimum size would drop the midpoint by this much. When that maker leaves, our favorite bid follows the next bid down and its reward score collapses. A market is also fragile when pulling that bid leaves no bid or drops the midpoint under `--min-fav` |
| `--exclude` | sports,esports,crypto | Skips, as `excluded category`, every market whose listing tags put it in one of these comma-separated categories: sports, esports, crypto, politics, geopolitics, economy, finance, tech, culture, weather. `--exclude none` keeps them all. Every esports market is also tagged sports, so excluding sports excludes esports too. The console's scan form has a checkbox for each |

`--mode` filters the table only. Every assessment is stored.

## The scan table

| Column | Meaning |
| --- | --- |
| `mode` | `floor` or `budget`, as above |
| `fav` | Favorite outcome and its size-adjusted midpoint in cents |
| `pool$` | The market's whole daily reward pool in USD, sponsored rewards included |
| `v` | Max spread from the midpoint that still scores, in cents |
| `min` | Minimum order size that scores, in shares |
| `est$/d` | Your estimated daily reward: pool times your share of the score |
| `fill/d` | Favorite shares a day expected to fill your bid; `h` from the market's history, `p` from a scan's fill prior alone, `d` from the built-in default prior alone |
| `net$/d` | `est$/d` less `fill/d` times the loss per filled share |
| `cap$` | Capital both bids tie up |
| `APR%` | `est$/d` divided by `cap$`, times 365, before fill costs |
| `net%` | `net$/d` divided by `cap$`, times 365 |
| `days` | Days left to farm, after the expiry buffer |
| `ahead` | Shares of longshot bids priced above yours, divided by your longshot size. A seller has to fill all of them before reaching your bid, so a low value means more fill risk on the leg you do not want filled |
| `vol24h` | 24-hour volume from Gamma |
| `H` | `H` when Gamma reports holding rewards enabled, `-` when not, `?` when unknown |

Rank is the net daily yield (`net$/d` over `cap$`) times the days left,
capped at `--horizon-cap-days`. That is the return you can expect over the
farm horizon, so long-dated markets rank higher than short ones with the same
yield.

## Approve a market

`approve` takes your probability that the favorite wins (`--p-true`) and the
longshot risk budget outside the free zone (`--budget`, default $25). `--capital` sets the most USD
the farmer may bid on this market's favorite, in place of the farmer's
`--capital`.
Without it, a market keeps the capital it already has, or uses the farmer's
default if it has none. `approve` warns, but still approves,
when the planned favorite bid is at or above `--p-true`, because a fill would
lose money. It also warns when a planned longshot leg outside the free zone
costs more than the budget. The zone is the one the leg's price falls in today:
a leg planned in the free zone leaves it as the end date nears.

## Decided markets

Polymarket closes a market only after its on-chain resolution settles, so a
market whose outcome is already known can stay active and accept orders for
days. The scan skips a market when Gamma shows either signal:

- `event closed`: the parent event is closed, for example the award was
  announced or the game is over.
- `resolution proposed`: a UMA resolution has been proposed, and possibly
  disputed.

Only candidates from the latest scan appear in the review queue and on
Discover, so a market that stops qualifying leaves the queue at the next scan.
A candidate also needs `rewards_active = 1`. Rewards can end between scans,
so the console's Review page checks them live with the farmer's own rule and
records what it finds. A candidate whose rewards ended leaves the queue at
once, and it returns if they come back. Approved, rejected and retired markets
always show. Some decided markets show neither signal until Polymarket updates
them. Read the resolution rules before you approve.

## Approximations

- The midpoint drops whole price levels smaller than the reward minimum size.
  Polymarket's adjusted midpoint applies the cutoff per order, so a level of
  many small orders counts here but not there.
- Competing score treats every resting order in the book as one maker. Above a
  0.90 midpoint that overstates the competition, so your estimated share is
  conservative. Your own resting orders also count as competition.
- The two outcome books are exact mirrors, so the screener reads only the
  favorite's book.
- A favorite bid above the best bid becomes the new best bid and pulls the
  midpoint toward itself. Its score is measured against the midpoint it
  creates, not the book's own. Both legs and the competition are scored
  against that shifted midpoint.
- `est$/d` assumes the book stays as it was at scan time, and that no other
  farmer reacts to your orders. Thin books show very high APRs that will not
  survive competition, so read them as a ranking, not a forecast.

## What a scan writes

The `markets` table in `farm.db` is the interface to the farmer (see
[The farm.db contract](farmer.md#the-farmdb-contract)).
`farm/reward_farmer.py` reads the approved rows.

A scan updates the metrics, rank, end date, and `rewards_active` flag. It
never touches your decisions: `status`, `p_true`, `naked_budget_usd`, `note`,
`paused`, `naked_paused`, `capital_usd`, or `sole_bid`. Rescanning is
therefore always safe. A market that leaves the reward set gets
`rewards_active = 0` and keeps its status. Retiring markets is the farmer's
job.
