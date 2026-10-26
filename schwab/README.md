# Schwab bots: one folder per person

| Folder | Bot | Schwab login | Token file it uses | Account |
|---|---|---|---|---|
| `Micah\0dte` | 0DTE (B + F) | A | its own `token.json` (this IS login A) | chosen with `schwab_select_account.py` |
| `Micah\45DTE` | 45DTE | A | `..\0dte\token.json` (set in its `.env`) | `SCHWAB_ACCOUNT_LAST4` (+ `SCHWAB_ACCOUNT_INDEX`) in `.env` |
| `Dawson\0dte` | 0DTE (B + F) | A | `..\..\Micah\0dte\token.json` (set in its `.env`) | chosen with `schwab_select_account.py` |
| `Ronnie\45DTE` | 45DTE | B | its own `token.json` | `SCHWAB_ACCOUNT_LAST4` (+ `SCHWAB_ACCOUNT_INDEX`) in `.env` |

## The rules

- **One token file per login, one developer app per login.** Login A lives in `Micah\0dte\token.json` and nowhere else: never put a
  `token.json` in `Micah\45DTE` or `Dawson\0dte` (the daily check flags it as a stray login). `Micah\45DTE` and `Dawson\0dte` read that
  file and rewrite it when the token refreshes; ASTRA (`Alpaca\0DTE`) only reads it. All three of those `.env` files must hold the SAME
  `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET`. Renew login A from `Micah\0dte`: `python schwab_login.py --force`, then pick Micah's account.
  `tools\schwab_login_both.py` is retired for this layout (it now only logs in from `Micah\0dte`).
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
- **Code is identical.** `Dawson\0dte` is a byte-for-byte clone of `Micah\0dte`, `Ronnie\45DTE` of `Micah\45DTE`. Edit Micah's copy (or the
  Alpaca copy for shared strategy files, then copy it to Micah's), then run `python tools\check_sync.py --apply-clones` to push the change to
  the clones (git must be on PATH; it refuses to touch anything otherwise). `python tools\check_sync.py` alone only reports drift.
- **Daily check.** `python tools\schwab_token_check.py` reports both logins' ages and whether each folder's `.env` (ASTRA's included) leads
  to the right login.

## First time after `git pull` on the bot machine (`C:\Users\Micah\Trading-Automation`)

`git pull` moves the code but leaves each old folder's runtime files behind. Do NOT start any bot from the new folders before step 1
(a start creates a `logs` folder there, and then the `move` of the old `logs` folder fails with only `Access is denied.`). With every Schwab
bot stopped (Ctrl+C), in order:

1. Move the runtime files into Micah's folders (move, do not copy):
   `move schwab\0dte\.env schwab\Micah\0dte\`, then the same for `token.json`, `selected_account.json` and `logs`;
   `move schwab\45DTE\.env schwab\Micah\45DTE\` and the same for its `logs` folder.
   Leave the old `schwab\45DTE\token.json` where it is (it goes to the archive in step 5): `Micah\45DTE` must have no `token.json`.
   If a `logs` move says `Access is denied.`, the new `logs` folder already exists: rename it away first (`ren schwab\Micah\0dte\logs logs-empty`)
   and move again. Check afterwards that `schwab\0dte` and `schwab\45DTE` hold nothing but the old `token.json` and caches.
2. Open `schwab\Micah\0dte\.env`: remove any `SCHWAB_TOKEN_PATH` line (the file next to it is the login).
   Open `schwab\Micah\45DTE\.env`: set `SCHWAB_TOKEN_PATH=..\0dte\token.json` (add the line, or replace an old absolute path that names the
   old folder), and set `SCHWAB_ACCOUNT_LAST4` (and `SCHWAB_ACCOUNT_INDEX`) for MICAH'S account (see "The 45DTE bots have no selector").
3. Create `schwab\Dawson\0dte\.env` by copying `schwab\Dawson\0dte\.env.example` (never Micah's `.env`); paste login A's app key and secret.
   Create `schwab\Ronnie\45DTE\.env` from `schwab\Ronnie\45DTE\.env.example` with login B's own app key and secret; leave
   `SCHWAB_ACCOUNT_INDEX=0` and `SCHWAB_ACCOUNT_LAST4` blank for now (login B does not exist yet; step 7 fills them in).
4. Open `Alpaca\0DTE\.env` (ASTRA) and set `SCHWAB_TOKEN_PATH=C:\Users\Micah\Trading-Automation\schwab\Micah\0dte\token.json`
   (or remove the line: that path is now the default). Without this ASTRA's live /ES feed silently drops to delayed data.
5. Move the old folders `schwab\0dte` and `schwab\45DTE` out of the way (for example into `C:\Users\Micah\_archive-for-review`) so
   nothing can be started from them by habit. Repoint any Task Scheduler entry or shortcut that launched them.
6. Run `python tools\schwab_token_check.py`. Every routing line (the `schwab\...` and `Alpaca\0DTE` lines) must read `[  ok  ]`. The
   `login B` token line reads `[MISSING]` and the command exits 1 until step 7 is done -- expected at this point.
7. Log in once from `schwab\Micah\0dte` (`python schwab_login.py --force`) and pick Micah's account; then in `schwab\Dawson\0dte` run
   `python schwab_select_account.py` and pick Dawson's (it refuses Micah's). Log in for B from `schwab\Ronnie\45DTE`: the login prints
   `index N: ...last4` for every account -- copy Ronnie's index and last 4 digits into `schwab\Ronnie\45DTE\.env`. Run the token check again:
   everything `[  ok  ]`, exit code 0.
8. Start each bot once and read its start-of-day report. A 0DTE bot prints `Schwab token: OK -- token is N days old ...` and, in dry_run or
   live, `Account: ...1234 (TYPE) [selected] -- ...`; a 45DTE bot prints `account ...1234 | token <file> | equity ...`. The last 4 digits and
   the token file must be the folder's own, as in the table above.
