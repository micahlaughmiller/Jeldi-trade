# Schwab bots: one folder per person

| Folder | Bot | Schwab login | Token file it uses | Account |
|---|---|---|---|---|
| `Micah\0dte` | 0DTE (B + F) | A | its own `token.json` (this IS login A) | chosen with `schwab_select_account.py` |
| `Micah\45DTE` | 45DTE | A | the newest login-A token (same app key as `Micah\0dte`) | `SCHWAB_ACCOUNT_LAST4` (+ `SCHWAB_ACCOUNT_INDEX`) in `.env` |
| `Dawson\0dte` | 0DTE (B + F) | A | the newest login-A token (same app key) | chosen with `schwab_select_account.py` |
| `Ronnie\45DTE` | 45DTE | B | its own `token.json` (its own app key) | `SCHWAB_ACCOUNT_LAST4` (+ `SCHWAB_ACCOUNT_INDEX`) in `.env` |
| `45DTE` | 45DTE **paper** (sim) | A | the newest login-A token (same app key) | reads only; `SCHWAB_ACCOUNT_INDEX` in `.env` |

`schwab\45DTE` is the original paper-trading 45DTE bot, kept where it always was (`SCHWAB_MODE=sim`); `Micah\45DTE` is the one that
can go live. They are the same code.

## The rules

- **One developer app per login; bots on the same app key share the newest login.** Every bot folder whose `.env` holds login A's
  `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET` (`Micah\0dte`, `Micah\45DTE`, `Dawson\0dte`, the paper `45DTE`) uses whichever of their
  `token.json` files is the newest login -- exactly what `schwab\0dte` and `schwab\45DTE` did before, now across the person folders. No
  `SCHWAB_TOKEN_PATH` line is needed (an explicit one still works and wins). Renew login A from any of those folders
  (`python schwab_login.py --force`; from a 0DTE folder it also asks for the account). ASTRA (`Alpaca\0DTE`) reads `Micah\0dte\token.json`
  read-only, so renew from `Micah\0dte` when you can. `tools\schwab_login_both.py` is retired (it now only logs in from `Micah\0dte`).
- **Login B is separate.** `Ronnie\45DTE` keeps its own `token.json` and has no sibling folder, so it never borrows login A. Log in for B
  only from that folder. Use a SEPARATE developer app (key + secret) for login B: a new login cancels an older login made with the same app
  for the same user, and whether one app can hold two users' logins at once has NOT been tested. If it cannot, every Ronnie login would stop
  Micah's and Dawson's bots, and every login-A renewal would stop Ronnie's.
- **One bot per account.** `Micah\0dte` and `Dawson\0dte` must select DIFFERENT accounts (login A lists both). The selector refuses an
  account another person folder already trades, and a live bot on a shared account keeps managing what it holds but refuses NEW orders
  (`SCHWAB_ALLOW_SHARED_ACCOUNT=true` in `.env` overrides). Never copy `selected_account.json`, `.env` or `logs\` between person folders.
- **The 45DTE bots have no selector.** Put the account's last 4 digits in `SCHWAB_ACCOUNT_LAST4`: the bot trades the account with those
  digits and only warns when `SCHWAB_ACCOUNT_INDEX` (a position in Schwab's list, counted from 0) disagrees. To see login A's list without a
  new login: in `Micah\0dte` run `python schwab_select_account.py`, read the table, press Enter (nothing changes); its `#` column counts
  from 1, so the index is that number MINUS 1.
- **No quotes around paths in `.env`.** `"..\0dte\token.json"` turns `\t` into a hidden TAB and the path silently breaks.
- **Code is identical.** `Dawson\0dte` is a byte-for-byte clone of `Micah\0dte`; `Ronnie\45DTE` and the paper `45DTE` of `Micah\45DTE`. Edit Micah's copy (or the
  Alpaca copy for shared strategy files, then copy it to Micah's), then run `python tools\check_sync.py --apply-clones` to push the change to
  the clones (git must be on PATH; it refuses to touch anything otherwise). `python tools\check_sync.py` alone only reports drift.
- **Daily check.** `python tools\schwab_token_check.py` reports both logins' ages and whether each folder's `.env` (ASTRA's included) leads
  to the right login.

## First time after `git pull` on the bot machine (`C:\Users\Micah\Trading-Automation`)

`git pull` moves the code but leaves each old folder's runtime files behind. Do NOT start any bot from the new folders before step 1
(a start creates a `logs` folder there, and then the `move` of the old `logs` folder fails with only `Access is denied.`). With every Schwab
bot stopped (Ctrl+C), in order:

1. Move the 0DTE runtime files into Micah's folder (move, do not copy):
   `move schwab\0dte\.env schwab\Micah\0dte\`, then the same for `token.json`, `selected_account.json` and `logs`.
   If the `logs` move says `Access is denied.`, the new `logs` folder already exists: rename it away first (`ren schwab\Micah\0dte\logs logs-empty`)
   and move again. Check afterwards that `schwab\0dte` holds nothing but caches.
   The paper bot's folder `schwab\45DTE` STAYS exactly as it is (`.env`, `token.json`, `logs`): it shares login A by app key, as before.
2. Open `schwab\Micah\0dte\.env` and `schwab\45DTE\.env`: remove any `SCHWAB_TOKEN_PATH` line that names the old `schwab\0dte` folder
   (a blank or missing line is right: the bots share login A by app key).
   Create `schwab\Micah\45DTE\.env` from `schwab\Micah\45DTE\.env.example` (login A's app key and secret), and set `SCHWAB_ACCOUNT_LAST4`
   (and `SCHWAB_ACCOUNT_INDEX`) for MICAH'S account (see "The 45DTE bots have no selector"). It starts in `SCHWAB_MODE=sim`; switch it to live
   only when you mean it.
3. Create `schwab\Dawson\0dte\.env` by copying `schwab\Dawson\0dte\.env.example` (never Micah's `.env`); paste login A's app key and secret
   (that key is what makes it share login A).
   Create `schwab\Ronnie\45DTE\.env` from `schwab\Ronnie\45DTE\.env.example` with login B's own app key and secret; leave
   `SCHWAB_ACCOUNT_INDEX=0` and `SCHWAB_ACCOUNT_LAST4` blank for now (login B does not exist yet; step 7 fills them in).
4. Open `Alpaca\0DTE\.env` (ASTRA) and set `SCHWAB_TOKEN_PATH=C:\Users\Micah\Trading-Automation\schwab\Micah\0dte\token.json`
   (or remove the line: that path is now the default). Without this ASTRA's live /ES feed silently drops to delayed data.
5. Move the old folder `schwab\0dte` out of the way (for example into `C:\Users\Micah\_archive-for-review`) so nothing can be started
   from it by habit; `schwab\45DTE` stays. Repoint any Task Scheduler entry or shortcut that launched `schwab\0dte\scheduler.py`.
6. Run `python tools\schwab_token_check.py`. Every routing line (the `schwab\...` and `Alpaca\0DTE` lines) must read `[  ok  ]`. The
   `login B` token line reads `[MISSING]` and the command exits 1 until step 7 is done -- expected at this point.
7. Log in once from `schwab\Micah\0dte` (`python schwab_login.py --force`) and pick Micah's account; then in `schwab\Dawson\0dte` run
   `python schwab_select_account.py` and pick Dawson's (it refuses Micah's). Log in for B from `schwab\Ronnie\45DTE`: the login prints
   `index N: ...last4` for every account -- copy Ronnie's index and last 4 digits into `schwab\Ronnie\45DTE\.env`. Run the token check again:
   everything `[  ok  ]`, exit code 0.
8. Start each bot once and read its start-of-day report. A 0DTE bot prints `Schwab token: OK -- token is N days old ...` and, in dry_run or
   live, `Account: ...1234 (TYPE) [selected] -- ...`; a 45DTE bot prints `account ...1234 | token <file> | equity ...`. The last 4 digits and
   the token file must be the folder's own, as in the table above.

## Resting exits at Schwab (6 Nov 2026)

Right after an entry fills, the 0DTE bot places the close at Schwab so it sits there ready to execute even if the script is
slow or down:

- `RESTING_EXIT_MODE = "oco"` (the default): the bot first asks Schwab's order preview whether it accepts an OCO made of a
  profit-target limit (buy to close at the target) and a stop-limit on the spread (stop at the stop price, limit a buffer above
  it, `RESTING_STOP_LIMIT_PCT_OF_WIDTH`). If Schwab takes it, that OCO rests at Schwab. If Schwab refuses the form (likely: its
  API has not accepted stop orders on two-leg spreads), the bot remembers that for the session and rests the profit-target limit
  alone; the stop stays with the script, as before. `"target"` skips the OCO attempt and always rests the target alone.
- The script's own exit rules keep running. Before any close of its own (stop, momentum reversal, trail, floor, end of day) it
  cancels the resting order first and waits for Schwab to confirm; a resting order that filled in the meantime is booked at its
  fill and no second close is sent.
- A resting order that fills is booked as `OCO_TARGET` or `OCO_STOP` at Schwab's fill price. A resting order that dies without a
  fill is placed again once, then the exits are script-managed for that spread. Every step is in the journal:
  `RESTING_EXIT_PLACED` (with `mode`), `RESTING_EXIT_LOST`, `RESTING_EXIT_FAILED`, `RESTING_EXIT_CANCELLED`,
  `RESTING_EXIT_CANCEL_FAILED`.
- Strategy F (stock spreads) keeps script-managed exits unless `RESTING_EXITS_FOR_STOCKS = True`: its targets sit inside the
  quote noise and some names refuse penny prices.
- At the hard stop the bot books a resting exit that filled at the bell before closing, leaves a tracked spread's resting
  order to that spread's own close, and never cancels orders on legs it does not manage.
- The simulator and the Alpaca paper bots never rest anything (`RESTING_EXITS = False` on Alpaca; the simulator has no OCO).
- `EXIT_PNL_BASIS = "fill"`: on Schwab the official P/L is the fill price, not the tick before the trigger.
