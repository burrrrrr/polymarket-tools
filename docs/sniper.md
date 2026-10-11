# Sniper page

The **Sniper** page (`/sniper`) arms tick-change watches. This page places
real orders, unattended, at a moment you do not choose. Read this whole
section before you arm a live watch.

## What a tick-change watch is

A Polymarket market trades on a price grid, and prices run from one tick to
one minus a tick. On a 0.01 tick the highest legal price is $0.99. When
Polymarket shrinks the market's tick to 0.001, $0.999 becomes legal. Orders
at one price fill in the order they arrived, so the first resting order at
the new best price is first in the queue there.

A watch waits for one outcome token's tick to reach a target tick, and then
posts one limit order, once. It exists to be first in that queue. The watch
is a row in `tools.db`, and the worker holds it, so it outlives a console
restart. One token has at most one open watch.

Every sniper order is post-only. It rests or the exchange refuses it, and it
never takes liquidity. The build chose that default, and the owner has not
confirmed it. The form has no option for an order that takes.

## Find a market and arm a watch

1. Open **Sniper**, type a few words under **Find a market**, and click
   **Search**.
2. Read the results. They are Gamma's search, ranked again. Outcomes that
   are ready for the default setup come first, then the best matches, then
   the highest volume. The page shows at most 25 outcomes. **Bid**, **Ask**,
   and **Tick** come from the live book.
3. Click **Arm** on the outcome you want. The status does not block you. It
   only says whether the outcome fits the default setup.
4. Fill in the **Arm a watch** form, which shows the live book above it.
5. For a live watch, type `ARM` and click **Arm watch**. For a rehearsal,
   tick **Dry run** and click **Arm watch** with no word.

The **Status** column judges each outcome for a BUY under the default guard:

| Status | Meaning |
| --- | --- |
| `ready` | Open, accepting orders, on a tick coarser than 0.001, with a best bid within 0.5c of 99.0c |
| `closed` | The market is closed |
| `not accepting orders` | The market takes no orders |
| `tick 0.001 already` | The tick has reached the target, so a watch would be missed |
| `no book` | The book could not be read |
| `no bid` | The book has no bid |
| `bid at 98.0c` | The best bid is outside the guard's range. The status shows the bid |

The form's fields:

| Field | Default | Rule |
| --- | --- | --- |
| **Side** | `BUY` | `BUY` or `SELL` |
| **Price (dollars)** | `0.999` | A multiple of the target tick, from one tick to one minus a tick |
| **Size (shares)** | empty | A positive number |
| **Target tick** | `0.001` | One of `0.1`, `0.01`, `0.001`, `0.0001` |
| **Expires after (s)** | `0` | `0` for good-til-cancelled, or at least 60 seconds, counted from the signing |
| **Guard the entry price** | ticked | See [The guard](#the-guard) |
| **Required price** | `0.99` | A price between 0 and 1 |
| **Tolerance** | `0.005` | From 0 to 1 |
| **Max quote age (s)** | `5` | A positive number of seconds |
| **Dry run** | not ticked | See [Dry runs and the keyless worker](#dry-runs-and-the-keyless-worker) |

A token that already has an open watch is refused with `This token already
has an open watch.`

## What the worker does with an armed watch

The worker reads the open watches four times a second. It adopts a new one
in these steps:

1. It reads the token's book. If the tick is already at the target or finer,
   the watch closes as `missed` and never fires.
2. It keeps that book's best bid and ask as the first quote.
3. It reads what signing needs from the exchange, and signs the order for
   the target tick ahead of time. It signs again every half second, because
   a signed order carries its signing time, and it never sends a signature
   older than 2 seconds.

If the book or the exchange cannot be read, the watch stays `armed`, its log
says why, and the worker tries again 2 seconds later.

Two threads then look for the tick change: the market websocket, and a read
of the books every half second. The thread that first sees the tick at the
target judges the guard and posts the order itself. A watch fires once.

While the watch is armed, its card shows the tick, the bid, and the ask the
worker last saw, refreshed every second.

## The guard

The guard stops the order when the market is not where you expected it at
the moment the tick changes. With **Guard the entry price** ticked, all of
these must hold or the watch closes as `rejected` and nothing is sent:

- The worker saw a quote before the tick change, with a price on your side
  of the book.
- That quote is no older than **Max quote age (s)**. The default is 5
  seconds.
- The side's own best price in that quote is within **Tolerance** of
  **Required price**. The side's own best price is the best bid for a BUY and
  the best ask for a SELL. The defaults ask for 99.0c within 0.5c.

The guard reads the last quote from before the change on purpose. Once the
finer tick opens, other orders reprice the book, and a quote from after the
change would refuse a setup that was valid a moment earlier. The worker never
takes a quote from an observation that already shows the target tick.

With the box not ticked, the watch has no guard, and the order goes out at
the tick change whatever the price is.

## Dry runs and the keyless worker

A dry run rehearses everything up to the post and sends nothing. Tick **Dry
run: rehearse everything up to the post, send nothing** on the form. A dry
run needs no typed word.

- On a keyed worker a dry run signs the order, as a live watch does, and
  then stops. It ends `rehearsed` with `signed, not sent`.
- On a worker started with `--no-key` it signs nothing. It ends `rehearsed`
  with `not signed (no key), not sent`.
- A dry run can also end `missed`, `rejected`, `disarmed`, or `failed`
  because the order could not be signed. It can never end `fired`. SQLite
  refuses that row.

The closed card of a dry run shows the quote the guard judged, which thread
saw the tick, and the time from the tick to the post. So a dry run on a real
tick change tests your guard settings with no money at risk.

A worker with no key adopts dry-run watches only. A live watch then stays
`armed`, and its card says `Armed. Waiting for a keyed worker: the running
worker holds no key.`

## How a watch ends

A watch is open while it is `armed` or `firing`. Every other state is final.
SQLite refuses any change to the state of a closed watch, so to try again
you arm a new one.

| State | What happened | Is an order on the book? |
| --- | --- | --- |
| `armed` | The watch waits for the tick change, for a worker, or for a keyed worker | No |
| `firing` | The guard passed and the first post has left. The worker waits for the answer or retries | Possibly |
| `fired` | The exchange accepted the order. The card shows its order id | Yes. A real post-only order rests, and the worker does nothing more with it. Cancel it on Polymarket if you do not want it |
| `rehearsed` | A dry run reached the post | No |
| `rejected` | Your guard refused. The card says which check failed | No. Nothing was sent |
| `missed` | The tick was already at the target when the worker adopted the watch | No, with one exception below |
| `disarmed` | You disarmed it before it fired, or while it was retrying after refusals | No |
| `failed` | See the next table | It depends on the reason |

A `failed` watch gives its reason in the sentence on its card:

| The card says | What happened | Is an order on the book? |
| --- | --- | --- |
| `The exchange refused the order: ...` | Every post was refused, and the retry window closed or the refusal was one no retry can change | No |
| `The order could not be signed: ...` | The worker could not sign at the tick | No |
| `The worker stopped inside the retry window after N refusals: ... No order rests.` | You stopped the worker while it was retrying | No |
| `The post got no answer (...). An order may rest: check your open orders.` | A post went out and no usable answer came back | Unknown. Check your open orders on Polymarket |
| `The worker stopped while the order was leaving. An order may rest: check your open orders.` | The worker died between the post and its answer, and the next worker found the watch `firing` | Unknown. Check your open orders on Polymarket |

The card prints the two unknown cases in the warning color. In both, look at
your open orders on Polymarket and cancel the order there if you do not want
it. The worker never sends that order again, and it never resumes a fire
after a restart.

How the worker retries:

- The exchange's order endpoint can lag the tick event, so a refusal of the
  first post is expected. The worker posts again every 0.25 seconds until 10
  seconds have passed since the tick change.
- It stops at once on a refusal that no retry can change: the address is
  blocked, the account cannot fund the order, or the post-only order would
  cross the book.
- It never retries a post that got no answer, because the order may rest and
  a second post could place a second order.
- Every refusal goes into the watch's log with the exchange's message.

The exception for `missed` is a crash at the moment of the post. The worker
records `firing` in `tools.db` up to about 50 milliseconds after the post
starts, and never before it, so that no database write delays the post. If
the worker process dies in that gap, the row is still `armed` although an
order may rest. The next worker then finds the tick at the target and closes
the watch as `missed`. After a worker crash near a tick change, check your
open orders even when the watch says `missed`.

Stopping the worker with Ctrl+C leaves each armed watch `armed`, and the next
worker adopts it again. No watch fires while no worker runs. A tick that
changes in that time is found at the next start, and the watch closes as
`missed`.

## Disarm a watch

Click **Disarm** on an open watch. The console writes the request to
`tools.db`, and the worker answers within a second.

A disarm cannot stop a post that has already left:

- The worker reads the request up to a quarter of a second after your click.
  A tick change inside that time still fires the watch.
- Once the first post has left, a disarm only stops the retries. If the
  exchange accepted the first post, the watch ends `fired` and the order
  rests. If the exchange refused it and the worker was retrying, the watch
  ends `disarmed` and no order rests.

So read the watch's final state after a disarm. `disarmed` means no order.
`fired` means the order went out first.

With no worker running, the card says `Disarm requested. Waiting for the
worker to answer.` The next worker to start, keyed or not, disarms the watch.

## What is not confirmed against the live exchange

As of 2026-10-11 the restore and the sniper have posted only to fake
clients, in the tests and in the build's own checks. The feed's subscribe
message was checked on Polymarket's live market channel, which is read-only.
These points wait for the first live use:

- **The order of the feed's messages.** The guard assumes that the tick
  change arrives before any price at the new tick. If a price at the new
  tick arrives first, the guard judges that price, and it can refuse a valid
  setup or pass one it should refuse. A dry-run watch on a real tick change
  answers this with no money at risk, because the closed card records the
  quote the guard judged.
- **The shape of the exchange's answer to a post.** The worker counts an
  order as placed only when the answer has `success` true, no `errorMsg`,
  and an `orderID`. If the real answer differs, an accepted order shows as a
  refusal or as an order that may rest. Check your open orders after the
  first live restore and the first live fire.
- **The text of the refusals.** The worker recognizes a refusal that no retry
  can change by words in the exchange's message, listed in
  `TERMINAL_REFUSALS` in `polymarket/orders.py`. If the exchange words one
  differently, the worker retries it for the whole 10 seconds instead of
  stopping at once. The exact text of the refusal during the tick race is
  also unknown.
- **The delay under load.** The worker shortens Python's thread switch
  interval to 0.5 ms so that a busy thread cannot hold up the thread that
  sees the tick. A comment in `tools/sniper.py` records the measurement
  behind that, taken on 2026-10-11 with a CPU-bound thread standing in for a
  restore. A parked thread woke after a median 15.5 ms and a worst 110 ms at
  Python's default 5 ms, against 0.08 ms and 0.35 ms at 0.5 ms. Nobody has
  measured it with a real restore running, and a restore's posts share the
  sniper's HTTP connection. Until someone measures it, avoid running a
  restore while a live watch is armed.

[Safety](../README.md#safety) has two more open points: the SSH tunnel and the scope of
the farmer's heartbeat.
