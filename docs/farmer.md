# Reward farmer

`farm/reward_farmer.py` farms the liquidity rewards on the markets you approved
with the screener. It runs as a daemon. Every cycle it reads each market's
order book, plans the orders it wants resting, compares them with the orders
it has, and cancels or places the difference.

It is a dry run by default. A dry run reads live public data (books, market
records, and your positions) but sends nothing and needs no private key. It
keeps a simulated order set that never fills, so you can watch orders being
placed once and then held or replaced as the book moves. `--live` signs and
posts real orders.

Both modes publish what the farmer does to `farm_state`, `farmer_status`,
`farm_events`, and `farm_mids` in `farm.db`, so a dry run shows up in the
console the same way a live run does. A dry run still never writes to `markets`.

```bash
python -m farm.reward_farmer                       # dry run on farm.db, forever
python -m farm.reward_farmer --once                # one dry-run cycle, then exit
python -m farm.reward_farmer --db scratch.db       # dry run on another database
python -m farm.reward_farmer --live                # real orders
python -m farm.reward_farmer --live --keep-orders  # Ctrl+C leaves the orders resting
python -m farm.reward_farmer --live --key-stdin    # the wallet key is piped in
```

The farmer also records each quoting market's favorite book to
`farm_books.db`, beside `farm.db`, every five minutes. `farm/farm_books.py` owns
that file, and no other process writes it.

A cycle prints a market's summary line only when it differs from the last
one printed for that market, then one line per action. A market resting
unchanged stays quiet:

```text
[16:36:06] some-market: QUOTING mid 96.9c Q1 308.1 Q2 308.1 | bid 94.9c net $3.12/day | hold 0 cancel 0 place 2
[16:36:06]   PLACE  FAV_BID    94.9c x    526.87  score 0.585  some-market
[16:36:06]   PLACE  LONG_BID    0.1c x    735.88  score 0.419  some-market
[16:36:11] some-market: QUOTING mid 96.9c Q1 308.1 Q2 308.1 | bid 94.9c net $3.12/day | hold 2 cancel 0 place 0
```

`score` is the per-share reward score at the current midpoint. `Q1` and `Q2`
are the planned score on each side. Polymarket pays on the smaller one. The
`bid ... net` part is the favorite bid chosen and its net per day, or why
there is none, such as `no fill history yet` or `no favorite bid pays: best
net -$0.12/day at 93.8c`.

A thread beside the main loop reads each approved market's fill history, so a
cycle never waits on those reads. It reads a market as soon as the farmer
manages it, then every hour, and again at once when `min_fav` changes. Until
that read lands, the farmer keeps valuing the market on the history it has,
read at the old `min_fav`, so the change cancels no favorite bid. It
reads one market at a time: the markets with no history yet first, and the
markets that are quoting before the rest. The hour runs from each market's
own read, not from the start of the pass. A failed read keeps the last
history and is retried after two minutes. A restart keeps the histories the
last run stored that are under 70 minutes old, so a market whose hourly read
was a few minutes late keeps its favorite bid. Until a market's first history
lands, the farmer places no favorite bid there, and only the exits rest. A
read that found no midpoints counts as no history. The farmer values fills on
the fill prior the last scan stored, read again every cycle.

## The four slots

Above a 0.90 midpoint Polymarket pays only for two-sided liquidity:
min(Q_one, Q_two). Q_one sums favorite bids and longshot asks. Q_two sums
favorite asks and longshot bids. The farmer can rest one order in each of
four slots per market:

| Slot | Order | Side | Purpose |
| --- | --- | --- | --- |
| `FAV_BID` | Buy the favorite | Q_one | The main quote. It rests at the price and size with the best net of its fill cost (see [The fill-cost model](pricing.md#the-fill-cost-model)), never above your `p_true` minus `--ev-margin` (or the book midpoint on a market [that follows the book](#markets-without-a-p_true)) or the ceiling its own fills set (see [After a fill](pricing.md#after-a-fill)), never over the market's bid room (its bid multiple times the capital left), and only when that net beats what the orders earn without it. Uses cash. |
| `LONG_ASK` | Sell the longshot | Q_one | Sells longshot shares from an unwanted fill. Priced at the mirror of the favorite bid, so it scores the same, and never under their average cost plus `--min-capture`. |
| `FAV_ASK` | Sell the favorite | Q_two | Exits favorite shares above their average cost plus `--min-capture`, which frees the capital again. |
| `LONG_BID` | Buy the longshot | Q_two | Exists only to make Q_one count. It rests at the cheapest price that balances Q_one, 0.1c on most markets, and never where a fill would give a favorite buyer over 20% a year. [The longshot bid](pricing.md#the-longshot-bid) has the rule. |

Q_two comes from the covered `FAV_ASK` first, and only the remainder from the
naked `LONG_BID`.

The two asks rest for as few as 5 shares, the smallest order the CLOB takes,
so the shares a partial fill leaves always have an exit. An ask under the
market's reward minimum scores nothing, and the plan counts none of it toward
Q_one or Q_two.

The farmer never buys at market. All inventory comes from passive fills of
these orders.

## Market lifecycle

Each market is in one of eight states:

- `QUOTING`: orders rest as planned.
- `COOLDOWN`: the favorite's ask fell by `--drop-threshold` within
  `--drop-window` seconds. The ask is the price at which the favorite's asks
  add up to three reward minimums, so one minimum-size lot that comes and
  goes on a thin book does not move it. The bids come down for `--cooldown`
  seconds, or until an `end_cooldown` command ends the cooldown (see
  [Pausing, settings, and stopping](#pausing-settings-and-stopping)). A
  falling offer is sellers repricing the favorite down. Only the ask counts,
  because a bid that leaves looks the same whether it was cancelled or sold
  into. When a bid leaves, the farmer keeps quoting and re-plans against the
  bids that remain.
- `FILLED`: a favorite bid of the farmer's left the book without its cancel,
  so it filled. The open-order listing shows that at once, every cycle, while
  the positions read lags by up to a minute. Planning in that gap would see
  no bid and the old inventory, and bid all the capital again. The
  farmer expects the orders it rested last cycle (the listing less what its
  cancels removed, plus the places the CLOB accepted), so a bid placed and
  filled before the next listing counts too, and so does one that shrank. A
  cancel that fails because the order was already matched or cancelled keeps
  that market from placing a favorite bid in the same cycle. The bids come
  down and the exits rest until positions show all the shares, at most two
  minutes; the farmer reads positions every cycle meanwhile. Each read
  records the shares it shows as a fill, priced at the orders that left the
  book, oldest first, and keeps waiting for the rest. Only a fill positions
  show moves the post-fill ceilings. A bid that left and never shows in the
  positions, such as one cancelled by hand, raises an `ALERT` after two
  minutes, leaves the ceilings as they were, and the market quotes again. A cooldown in force wins
  over it, and it wins over a sole bid.
- `BELOW_RANGE`: the midpoint is below `--min-fav`. The bids come down.
  Quoting resumes once the midpoint is 0.5c above `--min-fav`.
- `SOLE_BID`: you set `sole_bid = 1` on a market with a `p_true`, and no other bid
  supports a favorite bid. That means one of three things. The midpoint
  without the farmer's own orders is below `--min-fav`. Or no price within
  `--max-lead` of the best other bid scores. Or the midpoint is too low for
  a longshot bid to balance a favorite bid: under a 90c midpoint with a 6.5c
  band a longshot bid scores only above 3.5c, which near the end date offers
  a favorite buyer more than the 20% a year limit. On a thin book the first
  two happen when one maker's bid leaves, and the third on a wide spread.
  Without the flag the market goes `BELOW_RANGE` or rests no favorite bid.
  With it, the favorite bid stays as the only bid in the reward band:

  - Polymarket's midpoint is then halfway between the farmer's bid and the
    best ask. The bid rests two ticks above the lowest price whose midpoint
    keeps the longshot bid inside the band. In a book with a 97c ask and a
    5.5c band, a 0.1c longshot bid scores above a 94.4c midpoint, so the
    lowest price is 91.9c and the bid rests at 92.1c. The farmer's own
    favorite ask is left out of that lowest price, so an exit that falls to
    sell inventory never lifts the bid that bought it. It still sets the
    midpoint the plan scores, where it can stand in for the longshot bid on
    Q_two, and the bid stays under it.
  - With no other maker scoring, any score on both sides earns the whole
    pool, so the bid sits as low as still pays. It uses all of the market's
    capital left, not its bid room. The farmer does not value its fills:
    every sale into the book fills this bid.
  - A longshot bid already resting stays as it rests, whatever it costs or
    covers, if it is at the lowest tick or offers a favorite buyer under 7% a
    year. One resting anywhere else is replaced: normal quoting can leave it
    near the 20% a year limit, and around that the favorite bid would sit
    cents lower than a sole bid should. With none to keep, one goes to the
    lowest tick with the whole naked budget.
  - The longshot bid moves up only when the favorite bid cannot rest its two
    ticks above the lowest paying price. That happens when the ask is too
    low, and when the EV cap or the ask would hold the bid at the highest
    price it may have. Each tick the longshot bid rises lets the favorite bid
    rest two ticks lower. It goes to the highest price that still offers a
    favorite buyer under 7% a year, sized with the naked budget, and no
    higher than keeps the midpoint above 90c. Under a 94c ask in the book
    above, 100 days from the end date, the longshot bid goes to 1.8c and the
    favorite bid to 91.7c. A longshot fill costs at most the naked budget,
    while the favorite bid carries the capital. This 7% is the sole bid's
    own limit. The 5% free zone of normal quoting is unchanged.
  - When no longshot price leaves the favorite bid its two ticks, the bid
    rests at its cap around the kept or cheapest longshot bid that works.
  - A favorite bid already resting stays from the lowest paying price up to
    1c above the target. The target rises a tick for each tick the ask falls.
  - The bid never goes above `p_true` minus `--ev-margin` or the ceiling its
    own fills set, and never crosses an ask. If no price fits, the market leaves `SOLE_BID` and the other
    checks apply.

  It lasts as long as no other bid returns. An ask drop still starts a
  `COOLDOWN`, and a pause, a naked-leg pause or a longshot fill's cooldown
  ends it. When the other bids support a favorite bid again, the market
  waits until they have done so for five minutes without a break, then
  quotes as usual. Support that returns on a thin book mostly leaves again
  within minutes, and each switch replaces both bids, which score nothing
  for about 30 seconds each time. The sole orders keep resting and scoring
  through the wait. If the returned bids leave the sole bid outside the
  band, there is nothing to hold and the market quotes as usual at once. A
  restart does not carry the wait over.
- `PAUSED`: you set `paused = 1` on the market. All orders come down. When
  you clear it, the market goes through the checks above again, so it can go
  straight to `COOLDOWN` or `BELOW_RANGE` instead of `QUOTING`.
- `UNREWARDED`: the total reward listing pays the market under $1 a day, or
  the market left the listing. Its own CLOB record keeps showing the old
  rate after rewards end, so only the listing counts. The bids come down,
  but the farmer keeps managing the market. When the listing pays it $1 a
  day again, the market goes through the checks above,
  the pause included, and quotes again unless one of them holds it. This
  check wins over a pause.
- `RETIRED`: the market closed or stopped accepting orders, you no longer
  approve it, it reached its end date minus `--expiry-buffer-days`, or Gamma
  shows it is already decided (its event closed or a resolution was
  proposed; checked once a minute). All orders are cancelled. In live mode the
  database row becomes `retired` with a note. A row you rejected keeps its
  `rejected` status. Retirement wins over a pause. If you approve a retired
  market again after the farmer has seen it unapproved, the farmer manages it
  afresh, from a new lifecycle state through the same checks, so an expired
  market retires again at once. A dry run that retires a market leaves its
  row `approved`, so that market stays retired until you retire it and
  approve it again.

`COOLDOWN`, `FILLED`, `BELOW_RANGE` and `UNREWARDED` take down the two bids only. The
exits of shares the farmer holds, `FAV_ASK` and `LONG_ASK`, stay resting,
priced as in any plan: they only sell inventory, at or above its cost plus
`--min-capture`, and a sale frees its capital whatever stopped the quoting.
`PAUSED` and `RETIRED` take down every order.

Every cycle the farmer re-reads the approved rows, their controls, the
settings, and the commands from `farm.db`. A new approval, a rejection, a
pause, or a setting therefore takes effect within one `--interval`. The farmer
reads positions every 60 seconds and every market's Gamma record (reward terms,
tick, trading status) every 5 minutes, in batched requests on a thread of its
own, and a newly approved market's at once.

## Markets without a p_true

`p_true` is your view of the favorite. Leave it NULL and the market follows
the book. Each cycle its `p_true` is the adjusted midpoint of the favorite
book without the farmer's own orders, and the favorite bid goes no higher.
No `--ev-margin` applies: the margin is a buffer on a view, and the
[fill-cost model](pricing.md#the-fill-cost-model) already charges what a
fill at that price costs, valued at the midpoint.

- A one-sided book has no midpoint, so the market rests no favorite bid that
  cycle. Its exits rest as they do in any one-sided book.
- The market never enters `SOLE_BID`, whatever `sole_bid` says.
- The farmer prints its `p_true` as `market`.

## Pausing, settings, and stopping

The console (or any process with write access to `farm.db`) controls a
running farmer through the database. Per-market controls are columns on
`markets` that hold a level, not one-off commands. Setting one twice does
the same as setting it once, and a farmer that restarts reads the same level.

- `paused = 1` puts the market in `PAUSED`, which cancels its orders. Set it
  back to 0 to resume.
- `naked_paused = 1` takes down the `LONG_BID` for as long as it stays set,
  the same way a longshot fill does for `--naked-cooldown` seconds. The
  favorite bid shrinks to whatever the covered `FAV_ASK` still balances.
- `capital_usd` replaces `--capital` for that market only. NULL uses
  `--capital`.
- `bid_multiple` replaces `--bid-multiple` for that market only, from 1 to
  20. NULL uses `--bid-multiple`.
- `sole_bid = 1` lets the market enter `SOLE_BID` (see
  [Market lifecycle](#market-lifecycle)). Set it only on a market you are
  near certain of. A market with no `p_true` never enters `SOLE_BID`.
- To retire a market, set its `status` to `retired`. To cancel everything,
  set `paused = 1` on every approved market.

The `settings` table overrides the farmer's command-line options. The
command-line flags give the defaults, and each row replaces one of them
while it exists.

- **Keys.** The keys are the option names with underscores, and the seconds
  options end in `_s`: `capital`, `max_lead`, `ask_offset`, `min_capture`,
  `ev_margin`, `min_fav`, `min_low_score`, `keep_ratio`, `hurdle_rate`,
  `bid_multiple`, `tail_days`, `tie_band`, `drop_threshold`, `drop_window_s`,
  `cooldown_s`, `naked_cooldown_s`, and `expiry_buffer_days`. Each value is
  a JSON number. The console shows every one but `tie_band`.
- **Bad values.** `farm_db.put_setting` refuses an unknown key or an
  out-of-range value. A bad row written another way is ignored, with an
  `ALERT` event.
- **Changes.** When the effective config changes, the farmer logs a
  `SETTINGS` event with the old and new values. A changed setting moves the
  targets at once, but a resting order that still passes the keep checks
  below stays where it is.
- **Retired settings.** Opening the database deletes their rows:
  `fav_target_score`, from before fill-cost pricing, `depth_haircut`,
  `fill_lookback_days` and `sweep_prior_weight`, from before fill histories,
  and `stuck_share`, from before the ask's sales were measured.

To stop the farmer, insert a `stop` row into `commands`. The farmer reads it
on its next cycle, stamps `consumed_at` and `result`, and shuts down the same
way as on Ctrl+C. A stop sent while no farmer runs is marked expired when the
next farmer starts, so it does not stop that farmer.

To end a market's cooldown early, insert an `end_cooldown` row into
`commands` with the payload `{"condition_id": "<id>"}`. This is a command and
not a column because a cooldown lives only in the running farmer, and a
restart clears it. On its next cycle the farmer sets a market in `COOLDOWN`
back to `QUOTING`, logs a `PHASE` event that says the operator ended it, and
forgets the asks it saw before, so the drop that started the cooldown cannot
start it again. A further drop still can. The market then goes through the
other lifecycle checks in the same cycle, so it can land in `BELOW_RANGE`,
`PAUSED`, `UNREWARDED`, or `RETIRED` instead. For a market that is not in
`COOLDOWN`, or that the farmer does not manage, the farmer changes nothing
and writes the reason in `result`. Like a stop, the command is marked expired
if no farmer was running when it was sent.

## Safety mechanisms

- Every order is post-only, so a fill is always a maker fill.
- Every order is good-till-date. It expires at the market's end date minus
  `--expiry-buffer-days`, even if the farmer dies.
- The favorite bid never prices above `p_true - --ev-margin`, or above the
  book midpoint on a market with no `p_true`, or above the ceiling the
  market's own fills set.
- `--capital` caps each market's favorite inventory. Favorite shares already
  held count against it at cost. The favorite bid may rest up to
  `--bid-multiple` times what is left, at the size under that which nets the
  most, and rests nothing once the capital is used. One sweep can fill all of
  a resting bid, so a market can hold up to its multiple times its capital
  for a moment; the backtest reports how often.
- `--tail-days` caps each quoting favorite bid by what one sweep filling all
  of it would lose: at most that many days of its plan's reward, the filled
  shares valued as the ask sells that inventory (see
  [the tail cap](pricing.md#the-tail-cap)). 0 is no cap. A sole bid is not
  capped.
- The approved budget caps the naked longshot leg outside the free zone, and
  no longshot bid rests where a fill would give a favorite buyer over 20% a
  year. [The longshot bid](pricing.md#the-longshot-bid) has both rules.
- A longshot fill (your longshot shares went up) prints an `ALERT` and pauses
  the longshot bid for `--naked-cooldown` seconds. Without it, the favorite
  bid shrinks to whatever the covered favorite ask still balances.
- An order that is still close to its target stays in place. It must sit on
  the current tick, score at least `--keep-ratio` of the target, keep a size
  within -20% and +25% of the target, and stay within the price cap or
  budget, which a longshot bid resting free of it is not held to. Otherwise
  it is replaced. A favorite bid is judged by its net as
  it rests, at its own price and size, instead of its score and the target's
  size. It stays while its price is still a candidate, its net trails the
  best bid's by no more than `1 - --keep-ratio` of it (or $0.02/day, if
  more), it beats the orders without it, the naked budget still balances at
  least 80% of it, it is no more than 25% over the bid room, and its tail is
  no more than 25% over the tail cap. One priced
  over the bid the tie band chose stays only while its net is inside the
  band. This
  keeps queue position and avoids churning orders on every small move of the
  midpoint. A longshot ask is judged by its price instead of its score. One
  priced over its target is replaced, because the bids ahead of it in the
  favorite book fill first. One priced under its target stays.
- An order the CLOB rejects, such as a post-only cross or an unfunded bid,
  backs off that market's slot for 30 seconds. Each further rejection doubles
  the wait, up to 15 minutes, and an accepted order resets it. The backoff is
  logged once per rejection.
- In live mode, Ctrl+C, SIGTERM, a `stop` command, or a crash cancels every
  order on the managed markets before the farmer exits. A normal `--once`
  exit leaves the orders resting. So do Ctrl+C and SIGTERM under
  `--keep-orders`, and a `stop` command that asks to keep the orders. Orders
  left that way rest unmanaged until the farmer starts again: no cooldowns,
  retirement, or fill handling, and they expire only at their good-till-date.
  The next start adopts the ones still close to their targets and replaces
  the rest.

## Positions and existing orders

The farmer reads your positions from the public data API for
`POLYMARKET_FUNDER_ADDRESS` in `.env`. It never sells shares that were in your
wallet before it started managing a market:

- The first time it sees an approved market, it records your holdings of both
  tokens as a baseline (0 when you hold none). In live mode the baseline goes
  into the `baseline_fav` and `baseline_long` columns of `markets`, so it
  survives restarts. A dry run keeps it in memory only.
- On every positions refresh the baseline drops to your current holdings if
  those are lower. A manual sale therefore does not leave shares reserved that
  are gone.
- The farm's inventory is your holdings above the baseline. Only those shares
  are offered through `FAV_ASK` or `LONG_ASK`, or count against the capital
  and budget.

At startup it prints a `holding` line with your holdings, the baseline, and
the farm's share for every managed market where you have a position.

Approximation: the data API reports one average price per token, blended
across baseline and farm shares. The farmer uses that blended average as the
farm inventory's cost, for the `FAV_ASK` exit floor and the capital and budget
caps. When your earlier shares cost more or less than the farm's fills, the
exit floor is off by that difference.

In live mode the farmer adopts the open orders on the markets it manages. It
keeps the ones that match a slot target and cancels the rest, including
orders you placed there by hand. It never reads or touches orders on markets
it does not manage.

## Key options

| Option | Default | Meaning |
| --- | --- | --- |
| `--db` | `farm.db` in the data directory | SQLite file written by the screener. See [Where the databases live](../README.md#where-the-databases-live) |
| `--live` | off | Sign and post real orders |
| `--key-stdin` | off | Live only. Read the wallet key from one line of standard input, not from the environment or Bitwarden |
| `--once` | off | Run one cycle and exit |
| `--keep-orders` | off | Live only. Ctrl+C and SIGTERM leave the managed orders resting instead of cancelling them. A crash still cancels them. Not with `--heartbeat` |
| `--interval` | 5 | Seconds between cycles |
| `--capital` | 500 | Most USD of favorite inventory a market may hold, at cost |
| `--bid-multiple` | 4 | Most a quoting favorite bid may rest, as a multiple of the capital left. A sole bid rests the capital left |
| `--tie-band` | 0.05 | Favorite bids netting within this fraction of the best net are tied, and the lowest price rests |
| `--tail-days` | 30 | Most days of its plan's reward one sweep filling the whole quoting favorite bid may lose, its shares sold as that inventory sells. 0 is no cap |
| `--max-lead` | 0.005 | Most the favorite bid may sit above the best bid already in the book. 0 joins it |
| `--ask-offset` | 0.01 | Favorite ask distance above the midpoint |
| `--min-capture` | 0.005 | Favorite ask and longshot ask minimum above the shares' average cost |
| `--ev-margin` | 0.02 | The favorite bid stays at or below `p_true` minus this. A market with no `p_true` has no margin |
| `--min-fav` | 0.90 | Stop quoting below this favorite midpoint |
| `--min-low-score` | 0.10 | The longshot bid never sits above the cheapest price scoring this per share |
| `--keep-ratio` | 0.8 | Keep an order that scores at least this share of its target, a favorite bid that nets, at its own price and size, at least this share of the best bid's |
| `--hurdle-rate` | 0.40 | Yearly return capital must beat; a filled share costs its price minus what it sells for, discounted at this rate over the weeks the sale takes |
| `--drop-threshold` | 0.03 | Fall in the favorite's ask, where its asks add up to three reward minimums, that starts a cooldown |
| `--drop-window` | 60 | Seconds over which that fall is measured |
| `--cooldown` | 600 | Seconds without orders after an ask drop |
| `--naked-cooldown` | 3600 | Seconds without a longshot bid after a longshot fill |
| `--expiry-buffer-days` | 7 | Retire this many days before the end date |
| `--heartbeat` | off | Live only. See the warning below |

## The heartbeat warning

`--heartbeat` posts a heartbeat to Polymarket every 5 seconds. If the
heartbeats then stop for 10 seconds, because the farmer crashed, the network
dropped, or the machine slept, Polymarket cancels **all** open orders owned by
these API credentials. That may include orders placed by your other tools.
Polymarket does not document whether the scope is the API key or the whole
account. Without `--heartbeat`, a dead farmer leaves its orders resting until
their good-till-date expiry.

This is an open risk, not a settled fact. Leave `--heartbeat` off until the
scope is confirmed:

- Polymarket's own pages describe the scope two ways. The "Manage Orders"
  page says a lapse cancels all open orders "owned by those CLOB API
  credentials". The "Send heartbeat" reference page says "for the user".
- Every process here signs in with the same API key. `connect` in
  `polymarket/client.py` derives it from the wallet key, for the farmer, the
  worker, and the scoring probe alike. So a lapse could cancel the orders a
  restore or a watch placed, and orders you placed by hand, along with the
  farmer's.
- Before anyone turns `--heartbeat` on, the farmer needs an API key of its
  own, and the scope needs a test on a second, throwaway account. The code
  has no option for a separate key yet.
- Do not test the scope on the main account. If the scope is the whole
  account, the test itself cancels every open order.

`--heartbeat` and `--keep-orders` cannot be combined, because Polymarket
cancels the orders 10 seconds after the heartbeats stop.

## Going live

1. Run a scan and approve the markets you want, with your `--p-true` and
   `--budget` for each. Pass `--follow-market` in place of `--p-true` to let a market follow
   the book.
2. Run a dry run on the same database and read the output for a few cycles.
   Check the prices, sizes, and the `holding` lines.
3. Make sure the wallet holds enough USDC. At startup the live farmer compares
   your collateral balance with the favorite bids it plans and warns if the
   balance is short.
4. Start `python -m farm.reward_farmer --live`. It checks the region it
   connects from and does not start from a place where Polymarket rejects new
   orders. It then reads the wallet key from the environment or from
   Bitwarden, and locks the vault again as soon as the key is read (see
   [Where the wallet key comes from](../README.md#where-the-wallet-key-comes-from)).
   Leave `--heartbeat` off.
5. Stop it with Ctrl+C or a `stop` command, which cancel the orders on the
   managed markets.

A live start first checks the region this machine connects from and prints
the result, as the worker does. From a place where Polymarket rejects new
orders it prints `Not started: a live farmer would place orders from here.
Start it without --live for a dry run.` and exits with status 1, before it
reads any key. A dry run checks no region.

## The farm.db contract

`farm/farm_db.py` owns the schema and every SQL statement the tools run against
`farm.db`. Opening the file migrates an older one in place: missing columns
are added with `ALTER TABLE` and missing tables are created, so opening it
twice changes nothing. The file runs in WAL mode with a 5-second busy
timeout, because the farmer and the console write at the same time.

Each column has exactly one writer process. The module docstring of
`farm/farm_db.py` lists every table with its writer and its purpose.

`farm_events` holds changes only: `PLACE`, `CANCEL`, `FILL`, `PHASE`,
`ALERT`, `REJECT`, `BACKOFF`, `RETIRE`, `START`, `STOP`, and `SETTINGS`. A
`PLACE` row's `data` holds the `order_id` the CLOB gave the order, or is
empty when the CLOB refused it, and a `CANCEL` row's the order it cancelled.
At
startup the farmer deletes the events older than 30 days and the `farm_mids`
samples older than 7 days. A live farmer fetches `rewards_daily`, your reward
earnings per market per day, every 10 minutes.

The one exception to one writer per column: the live farmer retires a market
by changing `status` from `approved` to `retired` and writing a `note`, in a
single conditional `UPDATE`. A status you already changed is never
overwritten.

A `FILL` event comes from a change in your positions, because the data API
reports no fills. Its price is the price of the order the listing showed
leaving the book, else of the order that rested in that slot at the previous
cycle, and its `data` says which (`price_source`), that order's
`order_id`, and `filled_at`, when it left the book.
`analysis/fill_calibration.py` joins the `order_id` to the `PLACE` row to
find the bid a fill hit. A market that joins the managed set, at start or approved
later, rebuilds its post-fill ceilings from these rows. An older row without
`filled_at` counts from when positions showed it.

`scoring_pct` and `rewards_daily` come from authenticated reward endpoints
whose response fields Polymarket does not document. The farmer reads them
defensively, logs a warning when the shape does not match, and keeps
farming.
