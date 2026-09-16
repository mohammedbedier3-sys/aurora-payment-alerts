#!/usr/bin/env python3
"""
Two jobs, run together every 5 minutes:

1. PAYMENT/STATUS MONITOR - checks every ad account the connected Meta token
   has access to (not scoped to one Business Manager - discovered fresh via
   /me/adaccounts on every run) for account-status problems (payment
   failures, disabled accounts, pending review, etc.) and sends a Telegram
   message only when an account newly enters or leaves a problem state.
   Silent on every run where nothing changed. Note: on the very first run
   after widening scope, any account that is *already* in a problem state
   (e.g. long-closed accounts) will generate one baseline alert, since there
   is no prior state to compare against - after that, it's purely change-based.

2. COMMAND HANDLER - polls for new Telegram messages from the account owner
   and acts on a small fixed set of commands (/accounts, /campaigns,
   /pause, /resume, /help). No AI involved - plain string matching - so
   this stays free to run. Only messages from TELEGRAM_CHAT_ID are ever
   acted on.

Reads secrets from environment variables (set as GitHub Actions secrets):
  META_ACCESS_TOKEN    - Meta Graph API user access token (ads_management scope
                          needed for pause/resume; ads_read alone is enough
                          for the monitor and read-only commands)
  TELEGRAM_BOT_TOKEN   - Telegram bot token from @BotFather
  TELEGRAM_CHAT_ID     - Telegram chat ID to send alerts to / accept commands from

Persists state in state.json (account statuses + last-processed Telegram
update ID), committed back to the repo by the GitHub Actions workflow after
each run.
"""

import json
import os
import sys
from pathlib import Path

import requests

GRAPH_API = "https://graph.facebook.com/v21.0"
STATE_FILE = Path(__file__).parent / "state.json"

# account_status codes that mean "not delivering normally" and are worth a
# human's attention. 1 = ACTIVE is the only "everything is fine" state.
# See: https://developers.facebook.com/docs/marketing-api/reference/ad-account/#fields
PROBLEM_STATUSES = {
    2: "DISABLED",
    3: "UNSETTLED",
    7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT",
    9: "IN_GRACE_PERIOD",
    100: "PENDING_CLOSURE",
    101: "CLOSED",
}

HELP_TEXT = (
    "Commands:\n"
    "/accounts - list all your ad accounts and their status\n"
    "/campaigns <account name or part of it> - list that account's active campaigns\n"
    "/pause <campaign name or part of it> - pause a campaign (asks to confirm by ID if more than one matches)\n"
    "/resume <campaign name or part of it> - resume a paused campaign\n"
    "/pauseid <campaign_id> - pause by exact ID\n"
    "/resumeid <campaign_id> - resume by exact ID\n"
    "/help - show this message"
)


# ---------- Meta Graph API ----------


def meta_get(path: str, params: dict) -> dict:
    token = os.environ["META_ACCESS_TOKEN"]
    resp = requests.get(
        f"{GRAPH_API}/{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def meta_post(path: str, data: dict) -> dict:
    token = os.environ["META_ACCESS_TOKEN"]
    resp = requests.post(
        f"{GRAPH_API}/{path}",
        headers={"Authorization": f"Bearer {token}"},
        data=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_all_accounts() -> list[dict]:
    """Every ad account the connected token has access to, across every
    Business Manager and personal account - not scoped to one business."""
    accounts = []
    params = {"fields": "id,name,account_status,disable_reason", "limit": 200}
    path = "me/adaccounts"
    while True:
        data = meta_get(path, params)
        accounts.extend(data.get("data", []))
        cursors = data.get("paging", {}).get("cursors", {})
        after = cursors.get("after")
        next_page = data.get("paging", {}).get("next")
        if not next_page or not after:
            break
        params["after"] = after
    return accounts


def get_active_campaigns(account_id: str) -> list[dict]:
    data = meta_get(
        f"{account_id}/campaigns",
        {
            "fields": "id,name,status,effective_status",
            "filtering": json.dumps(
                [{"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]}]
            ),
            "limit": 200,
        },
    )
    return data.get("data", [])


def set_campaign_status(campaign_id: str, status: str) -> None:
    meta_post(campaign_id, {"status": status})


# ---------- Telegram ----------


def send_telegram(text: str) -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


def get_telegram_updates(offset: int) -> list[dict]:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    resp = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


# ---------- state ----------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------- job 1: payment/status monitor ----------


def run_status_monitor(state: dict) -> bool:
    """Returns True if state changed."""
    try:
        accounts = get_all_accounts()
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else str(e)
        send_telegram(
            f"⚠️ Payment-alert check failed to run: could not reach "
            f"Meta Graph API ({e}). This likely means the Meta access token "
            f"has expired and needs refreshing.\n\nDetails: {body[:300]}"
        )
        print(f"ERROR calling Meta Graph API: {e}\n{body}", file=sys.stderr)
        raise

    changed = False
    alerts = []
    accounts_state = state.setdefault("accounts", {})

    seen_ids = set()
    for acct in accounts:
        acct_id = acct["id"]
        seen_ids.add(acct_id)
        name = acct.get("name", acct_id)
        status_code = acct.get("account_status")
        disable_reason = acct.get("disable_reason")
        previous_status = accounts_state.get(acct_id, {}).get("status")

        is_problem_now = status_code in PROBLEM_STATUSES
        was_problem_before = previous_status in PROBLEM_STATUSES

        if is_problem_now and not was_problem_before:
            status_label = PROBLEM_STATUSES.get(status_code, str(status_code))
            reason_line = (
                f"\nMeta's stated reason code: {disable_reason}"
                if disable_reason not in (None, 0)
                else ""
            )
            alerts.append(
                f"🚨 Payment/account issue: {name}\n"
                f"Status: {status_label}{reason_line}\n"
                f"Account ID: {acct_id}\n"
                f"Check it: https://adsmanager.facebook.com/adsmanager/manage/accounts?act={acct_id.replace('act_', '')}"
            )
        elif not is_problem_now and was_problem_before:
            alerts.append(f"✅ Resolved: {name} ({acct_id}) is back to ACTIVE.")

        if accounts_state.get(acct_id, {}).get("status") != status_code:
            accounts_state[acct_id] = {"status": status_code, "name": name}
            changed = True

    for stale_id in list(accounts_state.keys()):
        if stale_id not in seen_ids:
            del accounts_state[stale_id]
            changed = True

    for alert in alerts:
        send_telegram(alert)

    print(f"Checked {len(accounts)} accounts. {len(alerts)} status alert(s) sent.")
    return changed


# ---------- job 2: command handler ----------


def find_campaigns(accounts: list[dict], query: str) -> list[dict]:
    """Search active/paused campaigns across all ACTIVE accounts by name
    substring. Skips non-active accounts (closed/disabled/etc.) - they won't
    have manageable campaigns and searching them would just waste API calls
    across 50+ accounts."""
    query_lower = query.strip().lower()
    matches = []
    for acct in accounts:
        if acct.get("account_status") != 1:
            continue
        try:
            campaigns = get_active_campaigns(acct["id"])
        except requests.HTTPError:
            continue
        for c in campaigns:
            if query_lower in c["name"].lower():
                matches.append({**c, "account_name": acct.get("name", acct["id"])})
    return matches


def find_accounts(accounts: list[dict], query: str) -> list[dict]:
    query_lower = query.strip().lower()
    if not query_lower:
        return accounts
    return [a for a in accounts if query_lower in a.get("name", "").lower()]


def handle_command(text: str, accounts: list[dict]) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/help", "/start"):
        return HELP_TEXT

    if cmd == "/accounts":
        lines = []
        for a in accounts:
            status = PROBLEM_STATUSES.get(a.get("account_status"), "ACTIVE")
            lines.append(f"- {a.get('name')} ({a['id']}): {status}")
        text = "Your ad accounts:\n" + "\n".join(lines)
        return text[:4000]  # stay under Telegram's 4096-char message limit

    if cmd == "/campaigns":
        if not arg:
            return "Usage: /campaigns <account name or part of it>"
        matched_accounts = find_accounts(accounts, arg)
        if not matched_accounts:
            return f"No account matching '{arg}'."
        if len(matched_accounts) > 1:
            names = ", ".join(a["name"] for a in matched_accounts)
            return f"Multiple accounts match '{arg}': {names}. Be more specific."
        acct = matched_accounts[0]
        campaigns = get_active_campaigns(acct["id"])
        if not campaigns:
            return f"{acct['name']}: no active/paused campaigns."
        lines = [f"- {c['name']} [{c['effective_status']}] (id: {c['id']})" for c in campaigns]
        return f"{acct['name']} campaigns:\n" + "\n".join(lines)

    if cmd in ("/pause", "/resume"):
        if not arg:
            return f"Usage: {cmd} <campaign name or part of it>"
        matches = find_campaigns(accounts, arg)
        if not matches:
            return f"No campaign matching '{arg}'."
        if len(matches) > 1:
            lines = [f"- {m['name']} ({m['account_name']}) id: {m['id']}" for m in matches[:10]]
            id_cmd = "/pauseid" if cmd == "/pause" else "/resumeid"
            return (
                f"{len(matches)} campaigns match '{arg}':\n"
                + "\n".join(lines)
                + f"\n\nUse {id_cmd} <exact id> to target one."
            )
        target = matches[0]
        new_status = "PAUSED" if cmd == "/pause" else "ACTIVE"
        set_campaign_status(target["id"], new_status)
        verb = "Paused" if cmd == "/pause" else "Resumed"
        return f"✅ {verb}: {target['name']} ({target['account_name']})"

    if cmd in ("/pauseid", "/resumeid"):
        if not arg:
            return f"Usage: {cmd} <exact campaign id>"
        new_status = "PAUSED" if cmd == "/pauseid" else "ACTIVE"
        try:
            set_campaign_status(arg.strip(), new_status)
        except requests.HTTPError as e:
            body = e.response.text if e.response is not None else str(e)
            return f"❌ Failed: {body[:300]}"
        verb = "Paused" if cmd == "/pauseid" else "Resumed"
        return f"✅ {verb} campaign {arg.strip()}"

    return f"Unknown command '{cmd}'. Send /help for the list of commands."


def run_command_handler(state: dict) -> bool:
    """Returns True if state changed."""
    offset = state.get("telegram_offset", 0)
    updates = get_telegram_updates(offset)
    if not updates:
        return False

    allowed_chat_id = str(os.environ["TELEGRAM_CHAT_ID"])
    accounts = None  # lazy-fetch only if a command actually needs it

    for update in updates:
        state["telegram_offset"] = update["update_id"] + 1
        msg = update.get("message")
        if not msg or "text" not in msg:
            continue
        sender_chat_id = str(msg["chat"]["id"])
        if sender_chat_id != allowed_chat_id:
            continue  # ignore anyone who isn't the account owner
        if not msg["text"].startswith("/"):
            continue

        if accounts is None:
            accounts = get_all_accounts()

        try:
            reply = handle_command(msg["text"], accounts)
        except requests.HTTPError as e:
            body = e.response.text if e.response is not None else str(e)
            reply = f"❌ Command failed: {body[:300]}"
        send_telegram(reply)

    return True


# ---------- entry point ----------


def main() -> int:
    state = load_state()
    changed = False

    try:
        changed = run_status_monitor(state) or changed
    except requests.HTTPError:
        save_state(state)
        return 1

    changed = run_command_handler(state) or changed

    if changed:
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
