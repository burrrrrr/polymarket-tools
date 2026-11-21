# polymarket-tools

A reward farmer, screener, console and order tools for one Polymarket account.
`README.md` covers setup, processes and safety. `docs/` has one page per tool.

## The live account

- The farmer, worker and console usually run live on the real account. Don't
  start, stop or restart them. Tell the user what to restart.
- Never place, cancel or sign an order. Keyed commands prompt for the Bitwarden
  master password, so give the user the command to run.
- Open the databases in `data/` read-only. Tests and browser checks use a
  scratch database and a port other than 8787.
- An order is the farmer's when its id is in farm.db `farmer_orders` or it
  expires at its market's `expires_at`. Every other order is the user's manual
  order, including one on a market the farmer manages. Leave manual orders out
  of farmer findings.
- The farmer's bids may total more than the wallet's cash. When cash runs out,
  Polymarket cancels open orders, and that can include the user's manual
  orders. The shortfall itself is expected. The worker's hourly snapshots of
  non-farmer orders exist so the user can put the manual ones back with the
  restore tool.

## Pricing changes

- Before proposing a fix for a reported problem, check the claim against
  farm.db or the live book and say whether it holds.
- Show the effect on named live markets: each slot's price before and after,
  and reward in dollars a day, not only share of the pool.
- A fill is never free. It ties up cash the user would otherwise trade with.
- When a book is thin or cash is short, rest the bid lower or behind depth.
  Pulling orders or stopping quoting is not the fix.
- Before testing a pricing change, write down what it is tested against: the
  hurdle rate, the backtest horizon, and the live markets to report on. Use the
  values the user gives. If they gave none, put the values you picked at the
  top of the result so the user can correct them. The live `hurdle_rate`
  setting is not necessarily the user's opportunity cost.
- Leave a pricing change uncommitted until that test shows it improves reward
  net of fill cost.

## Code and data

- No version numbers or compatibility code for stored data. When a stored
  shape changes, drop the rows the new code can't read and tell the user
  what gets deleted.
- The console only needs to work in a desktop browser.
- Tests never touch the network. `python run_tests.py` takes about 40 s.
  If a new test opens a socket or takes seconds, fix it before committing.
- Docker Desktop shares the WSL kernel with the user's distros. Mount
  `/proc/sys` read-only in any privileged container; `infra/verify.sh` shows how.

## Public repo

The repo is public. Tracked files, tests and commit messages use placeholders
for the wallet address, real name, home-directory paths and server IPs (use
RFC 5737 addresses such as 203.0.113.10). Never force-add `work/`, `data/`,
`.env` or `scoring.csv`.

## Commits

- By default, commit on `master`, one commit per task, with test, code and
  docs together.
- When the user wants a change kept out of the running farmer, or asks for a
  PR, build it in a worktree at `../polymarket-tools-<topic>` on a branch named
  `<area>/<topic>` (as in `farmer/fill-pricing`), push it, and open a PR. Leave
  it unmerged until the user says to merge.
- Other sessions share the main checkout. Stage files by name, never
  `git add -A`.
- Delegates don't commit. The lead commits after verifying.
- `.githooks/commit-msg` enforces the message format. Never pass `--no-verify`.

## Plans and handoffs

Write plans and handoffs to `work/<topic>/`. Read its `HANDOFF.md` before
continuing work a previous session started.
