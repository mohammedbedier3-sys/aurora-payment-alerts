# Ad Account Payment Alerts

Two things, both running every 5 minutes on GitHub Actions' free tier (public
repo = unlimited free minutes) — no server, no Anthropic API usage, no AI
involved in either job:

1. **Payment/status monitor** — checks **every ad account the connected
   Meta token has access to** (discovered fresh via `/me/adaccounts` on
   every run, not scoped to one Business Manager — originally Aurora-only,
   widened 2026-09-16) for account-status problems (disabled, unsettled
   billing, pending review, etc.) and sends a Telegram message **only**
   when an account newly enters or leaves a problem state. Silent
   otherwise. Note: the first run after widening scope sends one baseline
   alert for any account already in a problem state (e.g. long-closed
   accounts) — after that it's purely change-based.
2. **Command handler** — polls for Telegram messages from the account owner
   and acts on a small fixed set of commands (list accounts/campaigns,
   pause/resume a campaign by name or ID). Plain string matching, not AI —
   see `/help` for the full command list.

## How it works

- `.github/workflows/check.yml` — runs `check_accounts.py` on a cron schedule
- `check_accounts.py` — calls the Meta Graph API; compares each account's
  `account_status` against `state.json` for job 1; polls Telegram's
  `getUpdates` for job 2, tracking the last processed update ID so nothing
  is handled twice. Campaign search/pause/resume commands only scan
  currently-ACTIVE accounts (skips closed/disabled ones) to keep each run fast.
- `state.json` — committed back to the repo by the workflow after each run
- Only messages from `TELEGRAM_CHAT_ID` are ever acted on — anyone else
  messaging the bot is silently ignored

## Required repo secrets

Set under Settings → Secrets and variables → Actions:

| Secret | What it is |
|---|---|
| `META_ACCESS_TOKEN` | Meta user access token with `ads_read` scope (same one used elsewhere for Aurora — **expires ~every 60 days**, must be rotated here too when refreshed) |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather for the alert bot |
| `TELEGRAM_CHAT_ID` | Telegram chat ID to send alerts to |

## Manually triggering a check

Actions tab → "Check Aurora ad accounts for payment errors" → Run workflow.
