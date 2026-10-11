# Orders page

The **Orders** page (`/orders`) stores snapshots of every open order on the
account. From a snapshot's page you place again the orders that are no longer
open, for example after Polymarket cancelled them when your balance dropped.

The console holds no key, so it only queues a job in `tools.db`. The worker
runs the job. While no worker runs, the page shows a banner with the command
that starts one, and a queued job says `It waits for the worker, which is not
running.` Every job on this page needs a keyed worker.

One job of a kind is queued or running at a time. A preview and a restore
count as one kind. A second request gets a notice such as `A snapshot job is
already queued (job 12).`

## Take a snapshot

1. Open **Orders**.
2. Type a note of at most 200 characters, or leave it empty.
3. Click **Take snapshot**.

The worker lists every open order on the account, the farmer's and your own
alike, and stores them with their market names. With each order it stores
whether the order's market had the status `approved` in `farm.db` at your
click, so that a later restore leaves the farmer's orders to the farmer
whatever the farmer manages by then. A listing that fails stores
nothing, and the job ends `failed` with the error. When Gamma cannot name the
markets, the orders are stored without names and the page shows each market's
condition id.

The job panel shows the job's state (`queued`, `running`, `done`, or
`failed`), its log, and a link to the snapshot once it is stored.

## Import the old snapshot files

The snapshot script this page replaces saved each snapshot as an
`orders_snapshot_*.json` file. To store those files as snapshots, run:

```bash
python -m tools import-snapshots                   # the files in snapshots
python -m tools import-snapshots path/to/files     # the files somewhere else
```

With no path, the import reads the `snapshots` folder of the data directory
(see [Where the databases live](../README.md#where-the-databases-live)).

The import needs no key and no running worker. It takes the same `--db` as
the worker, before or after the command. It prints one line, in this form:

```text
Stored <n> snapshot files with <n> orders in <tools.db>; <n> already stored.
```

A file that was stored before, by name, is skipped, so you can run the import
again. A file that is not a snapshot stops the import with `Import failed:
<file> is not an order snapshot`. The files before it stay stored.

The old files hold local times with no time zone. The import reads them in
the time zone of the machine it runs on and stores them as UTC. Run it on the
machine that took the snapshots.

## Preview a restore

1. On **Orders**, click a snapshot's time in the **Snapshots** table.
2. Click **Preview restore**.

The worker lists the orders that are open now and decides what a restore
would do with each order of the snapshot. A preview places nothing. The page
groups the orders under **Would place** and the four skips below, and each
row says why.

## What a restore places and what it skips

The worker goes through the snapshot's orders in their order. For each one,
the first rule that fits decides.

| Rule | Group on the page | Why |
| --- | --- | --- |
| Nothing of the order was left to fill when the snapshot was taken | **Skipped: nothing left to fill** | There is no size to place. |
| The order's market has the status `approved` in `farm.db` at your click | **Skipped: the farmer's market** | The farmer cancels the orders on its markets that are not its own, and places its own. |
| The order's market had the status `approved` when the snapshot was taken | **Skipped: the farmer's market** | The order was the farmer's, or the farmer would have cancelled it. A market the farmer has stopped managing since gets none of its old orders back. |
| The snapshot has no record of that (an imported file, or a snapshot from before the record was kept), and the market has the status `retired` now | **Skipped: the farmer's market** | The farmer managed the market once, and nothing says whose order this was. An order you placed there by hand is skipped too: place it again by hand. |
| The order expires within 2 minutes from now, or has expired | **Skipped: expired** | The CLOB refuses an order that expires within a minute. The second minute is for the job to reach the order. |
| The same order, by its id, is still open | **Skipped: already open** | It never went missing. This holds however much more of it has filled since. |
| An open order that is not in the snapshot has the same token, side, price, and remaining size | **Skipped: already open** | It was placed again already, by you or by an earlier restore. Each such order covers one snapshot order, so two identical snapshot orders with one copy open give one skip and one place. |
| None of the above | **Would place**, then **Placed** | The order is missing. |

What it places:

- **The remaining size.** A restore places the order's size less what had
  filled when the snapshot was taken, cut to whole hundredths of a share. It
  never places the original size of a partly filled order.
- **The same token, side, and price.** The worker reads each market's tick
  once for the job. An order whose price is not a multiple of that tick is
  not sent.
- **The same expiration.** An order with an expiration goes out
  good-til-date with that expiration. An order with none goes out
  good-til-cancelled.
- **Post-only.** Every order of a restore is post-only. An order that would
  cross the book is refused by the exchange and shows under **Rejected by
  the exchange**. It is never filled as a taker.

## Place a restore

Placing follows a finished preview. The **Place** button shows only under a
finished preview that would place at least one order, and the server refuses
a placing request unless the snapshot's newest preview or restore job is a
finished preview. So every restore follows a preview you could read, and
after a restore you preview again before you place again.

1. Read the preview.
2. Type `PLACE` in the field beside the button. The server checks the word
   and answers `Type PLACE to confirm.` without it.
3. Click **Place N orders**.

These are real orders. What happens next:

- The worker lists the open orders again and makes a new plan. It places
  what is missing then, so the count can differ from the preview.
- A finished preview has no time limit of its own. The limit is on the
  restore job, which a worker must start within 5 minutes of your click. A
  restore that waited longer fails with `preview it again`, so a restore
  queued while the worker was down does not run hours later.
- If the worker cannot list the open orders, the job fails and places
  nothing.
- The worker stores one line for each order as it goes. A placed order shows
  under **Placed** with its new order id. An order the exchange refused shows
  under **Rejected by the exchange** with the exchange's reason. An order
  that was not sent, or whose post got no answer, shows under **Error**.
- A restore ends early when the worker stops, and after 3 posts in a row get
  no answer. The lines stored by then stay, and the job's error says how far
  it got.

A restore is safe to run again while the orders it placed still rest
unfilled. Preview again. Each of those orders is open now, so the new plan
skips it.

A restore cannot tell an order that was cancelled from an order that filled
after the snapshot was taken. Both are missing from your open orders, so
both show under **Would place**. The same holds for an order that an earlier
restore placed and that has filled since, in whole or in part. Read the
preview before you place.

## When a line says the order may rest

An **Error** line is one of two kinds.

`not sent: ...` means the order never left the worker, because the market's
tick could not be read or the order could not be signed. Nothing rests.

`no answer (...): the order may rest: check your open orders` means the post
went out and no usable answer came back: a timeout, a dropped connection, a
5xx status, or an answer the worker could not read. The exchange may have
placed the order or not. The worker does not send it again, because a second
post could place a second order.

To settle it, look at your open orders on Polymarket. A new preview also
shows it. Click **Preview restore** again, and the worker lists your open
orders afresh. An order that rests unfilled is skipped as already open. An
order that does not rest is listed under **Would place**.
