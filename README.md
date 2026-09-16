# Aurora Payment Alerts

Checks every ad account under the **Aurora for Advertising** Business Manager
(`321924497251498`) every 15 minutes for account-status problems — disabled,
unsettled billing, pending review, etc. Sends a Telegram message **only**
when an account newly enters a problem state, or is newly resolved. Silent
otherwise.

Runs entirely on GitHub Actions' free tier — no server, no Anthropic API
usage for the recurring check itself.

## How it works

- `.github/workflows/check.yml` — runs `check_accounts.py` on a cron schedule
- `check_accounts.py` — calls the Meta Graph API, compares each account's
  `account_status` against `state.json` (last known state), and only alerts
  on a change
- `state.json` — committed back to the repo by the workflow after each run

## Required repo secrets

Set under Settings → Secrets and variables → Actions:

| Secret | What it is |
|---|---|
| `META_ACCESS_TOKEN` | Meta user access token with `ads_read` scope (same one used elsewhere for Aurora — **expires ~every 60 days**, must be rotated here too when refreshed) |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather for the alert bot |
| `TELEGRAM_CHAT_ID` | Telegram chat ID to send alerts to |

## Manually triggering a check

Actions tab → "Check Aurora ad accounts for payment errors" → Run workflow.
