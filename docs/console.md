# Console

`console` is a local web console for the reward farmer and the order
tools. It shows what the farmer is doing, live, and it is where you run scans,
approve markets, and control the farmer. Every change it makes to the farm
goes through `farm_db` into `farm.db`, and the farmer picks it up on its next
cycle. Its **Orders** and **Sniper** pages queue work for the worker through
`tools.db` (see [Orders page](orders.md#orders-page) and [Sniper page](sniper.md#sniper-page)).

```bash
python -m console                        # port 8787
python -m console --db scratch.db        # another farm database
python -m console --tools-db scratch-tools.db   # another worker database
python -m console --port 8788            # another port
```

Then open http://127.0.0.1:8787. The console prints `Console on
http://127.0.0.1:8787 reading <farm.db> and <tools.db>` and serves until
Ctrl+C. Run it from the repository root, or with the repository on
`PYTHONPATH`, so Python can find the `console` package. `--db` defaults to
`farm.db`, the production database, and `--tools-db` to `tools.db`, both in
the data directory (see
[Where the databases live](../README.md#where-the-databases-live)). A
relative `--db` or `--tools-db` path is read from the current directory.

Run it next to the farmer on the same `farm.db` and next to the worker on the
same `tools.db`. The sidebar lists both processes. A farm page shows a banner
when the farmer is not running, and a tool page shows one when the worker is
not. The console reads `POLYMARKET_FUNDER_ADDRESS` from `.env` to show your
holdings on the Review page. It never reads or handles a private key.

Pages:

| Page | Shows | Actions |
| --- | --- | --- |
| Overview (`/`) | Totals (markets quoting, capital in favorite bids, naked longshot bids and the part of them the budgets limit, farm inventory, the screener's $/day estimate, today's earnings), the alerts that need you, the tracked markets, and the latest events | Each alert links to its action: Resume, Edit budget, Edit p_true, or the farmer status |
| Discover (`/discover`) | The scan parameters, the last scan's funnel (listed, passed the prefilter, assessed on the book, and the skip reasons), and the stored markets by rank, 50 per page. Filter by mode, text in the question or slug, status, and minimum days to the farmer's cutoff | Run a scan |
| Review (`/review/<condition_id>`) | The plan from the last scan: the reward band with the scanned best bid and ask and your planned bids, each planned order with why it is priced that way, the yield figures (gross, fills/day, fill cost and net APR), the resolution rules from Gamma, and what your wallet already holds, which becomes the farmer's baseline. `/review` opens the best-ranked undecided candidate | Approve with p_true, naked budget, and capital; reject with a note; retire an approved market; re-approve a retired one |
| Live markets (`/markets?phase=`) | One card per managed market: phase, midpoint, the reward band with your bids marked, the four slots, Q1/Q2 and naked budget bars, inventory, and the expiry cutoff. Approved markets the farmer has not picked up yet show as waiting | Pause or resume quoting, pause or resume the naked leg, edit p_true and budget, retire, end a cooldown |
| Market detail (`/markets/<condition_id>`) | Stat tiles, a 24-hour midpoint chart with the band, your bids and fill and phase markers, the slots, the book inside the band, the position, the lifecycle, fills, and the market's events | The same controls as the card |
| Activity (`/activity?tab=`) | `rewards`: earnings per day and market against the screener estimate. `fills`: each fill with its approximate edge against `p_true`. `inventory`: wallet, baseline, farm shares, and exit orders. `log`: every farm event, filterable by kind and market | None |
| Settings (`/settings`) | The farmer process (mode, pid, start, last cycle, interval, heartbeat) and every setting with its effective value and where it comes from: your override, the farmer's own value, or the built-in default | Stop the farmer, save or reset settings, cancel all managed orders |
| Orders (`/orders`) | The snapshot job in progress and every stored snapshot. A snapshot's page (`/orders/snapshots/<id>`) shows its orders and its newest preview or restore | Take a snapshot, preview a restore, place it |
| Sniper (`/sniper`) | A search over the open markets and every watch with the quote the worker last saw, its outcome, and its log. `/sniper/arm` is the form that arms one outcome | Arm a watch, disarm it |

## Running a scan from the console

The Discover page runs the same scan as `python -m farm.reward_screener scan`,
in a background thread of the console. It takes two to three minutes. The
page shows the current stage while it runs, then the counts and the duration,
or the error. Only one scan runs at a time: a second request while one runs gets
"A scan is already in progress". The form starts from the parameters of the
last scan run from the console, or the screener's defaults. A scan stores its
rows and its funnel in one transaction and never changes your decisions.

## Decisions and controls

- **Approve** sets `status` to `approved` with the p_true, naked budget, and
  capital you entered. You enter p_true as a whole percent from 1 to 99, with
  the - and + buttons or the arrow keys stepping it by 1; the database stores
  it as a probability. The naked budget starts at $10 for a new market. A
  blank capital means the farmer's `capital` setting.
  On a market that is already approved, the same form edits those terms.
  After a decision on a candidate, the console opens the next undecided
  candidate. After an edit, it opens the market's page.
- **EV check.** While you type p_true, the Review page shows what the farmer
  will do with it.
  - The farmer never bids above p_true minus `ev_margin`, rounded down to the
    tick. The check uses the effective `ev_margin`: your override, else the
    farmer's last value, else the default.
  - When the planned bid is above that cap, the check shows the cap the
    farmer bids no higher than and its lower score, or that the cap is
    outside the band. That score is measured against the midpoint the capped
    bid creates.
  - The live preview below the check shows the price the farmer would pick
    under that cap and its net per day. Until you type a p_true, it values
    the market at the book midpoint, as the scan does.
  - The preview's fills come from the history the scan stored and the stored
    fill prior. A market the scan did not read is valued on the prior alone,
    and the preview says so. With no prior stored yet, it says to run a scan.
  - The check also warns when your naked budget is below a planned longshot
    leg outside the free zone, which the scan priced within its own naked
    budget.
- **Pause and naked leg** set `paused` or `naked_paused` to the level the
  button names, so pressing twice changes nothing. Each button names the
  action it takes, such as Resume while the market is paused.
- **Retire** asks first, naming the market, then sets `status` to `retired`.
  The farmer cancels the market's orders on its next cycle.
- **End cooldown** shows only while the market is in `COOLDOWN`. It inserts
  an `end_cooldown` command, and the farmer quotes the market again on its
  next cycle (see
  [Pausing, settings, and stopping](farmer.md#pausing-settings-and-stopping)).
- **Cancel all** pauses every approved market. You must type `CANCEL`, and
  the server checks it.
- **Stop farmer** asks first, then inserts a `stop` command. The Settings
  page shows when the farmer consumed it. **Stop, keep orders** inserts a
  `stop` command that asks the farmer to leave its orders resting, as
  `--keep-orders` does.
- **Settings** forms check each value with the same rules the farmer uses
  and write only the fields you changed, all in one transaction. A value
  that fails shows its error next to the field and nothing is saved.
  **Reset** deletes the override, so the farmer's own value applies again.

There is no live switch. Live mode needs the private key, which the farmer
reads in its own terminal, so you start it there:
`python -m farm.reward_farmer --live`.

## Security

The console has no login and controls real orders, so it must only be
reachable from your own machine, and only by its own pages:

- It binds `127.0.0.1` only. To reach it from another machine, use an SSH
  tunnel, as [Safety](../README.md#safety) shows. Never change the bind address.
- Every request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`.
  Anything else gets 400. This blocks DNS rebinding, where a hostile site
  points its own domain at 127.0.0.1.
- Every request other than GET or HEAD needs the console's token, a random
  value made fresh each time the console starts. The pages carry it in a
  `<meta>` tag, htmx sends it as the `X-Console-Token` header, and plain forms
  send it as the `console_token` field. A missing or wrong token gets 403.
  Another web site open in your browser cannot read the token, so it cannot
  forge a request.
- A request other than GET or HEAD whose `Origin` header names another origin
  gets 403, even with the token.
- A request that makes real orders move carries a typed word, which the
  server checks: `CANCEL` to cancel all managed orders, `PLACE` to place a
  restore, and `ARM` to arm a live watch.
- Every decision and control is a POST. Two GET requests write, the Review
  page's live check and its skip to the next quotable candidate, and they
  only record a candidate's `rewards_active` as Polymarket reports it. No
  request can choose that value.

`console/security.py` holds all of these checks, as a middleware in
front of every route.

## Live updates

The pages update without a reload. A live region reloads when the farmer
finishes a cycle, when a farm event appears, when the farmer or the worker
starts or stops, and when a job or a watch changes. After a market control,
the regions that show that market reload at once. The charts are SVG drawn
on the server.

The alerts come from a rule table in `console/views.py`: cooldown, below
range, paused, naked budget 99% or more used with no longshot bid resting free
of it, a favorite bid scoring under 0.10,
a slot in backoff, a longshot-bid fill in the last 24 hours, and the farmer not
running. The farmer counts as not running when its last cycle is older than
three intervals, or when it has stopped.

`/api/overview`, `/api/markets`, `/api/markets/<condition_id>`,
`/api/events?after=&before=&limit=&condition_id=`, and `/api/activity?tab=`
return the same data the pages show, as JSON.

The midpoint chart reads `farm_mids`, which the farmer fills with one sample
per market per minute. A market's chart is empty until the farmer has run for
a minute. The resolution rules come from Gamma when you open a Review page
and are kept for 10 minutes. The live check needs the total reward listing,
which includes sponsored pools, leaves out markets paid only by sponsors, and
takes about ten seconds to read. The console
reads it in a background thread when a Review page first asks, then at most
every 5 minutes. Until it lands, the console records no rewards change and
the queue keeps every candidate where it is. If Gamma or the data API is
unreachable, that panel says so and offers a retry; the rest of the page still
works.

htmx 2.0.11 and htmx-ext-sse 2.2.4 are vendored in `console/static/`, so
the console works offline except for the IBM Plex fonts, which fall back to
system fonts, and the Review page's rules and holdings. To upgrade, download
`dist/htmx.min.js` from the `htmx.org` package and `dist/sse.min.js` from the
`htmx-ext-sse` package, and update the versions here.

## What the terms mean

Each term on the console that is not self-explanatory, such as naked budget,
Q1 / Q2, a phase or a slot name, has an info button after it. Hover over it or
Tab to it to see what the term means. Escape closes the explanation.
