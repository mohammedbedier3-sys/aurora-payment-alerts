#!/usr/bin/env python3
"""
Checks every active ad account under the Aurora for Advertising Business
Manager for account-status problems (payment failures, disabled accounts,
pending review, etc.) and sends a Telegram message only when an account
newly enters a problem state. Silent on every run where nothing changed.

Reads secrets from environment variables (set as GitHub Actions secrets):
  META_ACCESS_TOKEN    - Meta Graph API user access token (ads_read scope)
  TELEGRAM_BOT_TOKEN   - Telegram bot token from @BotFather
  TELEGRAM_CHAT_ID     - Telegram chat ID to send alerts to

Persists last-seen status per account in state.json, committed back to the
repo by the GitHub Actions workflow after each run, so re-notification only
happens on a genuine state *change*, not every 15-minute check.
"""

import json
import os
import sys
from pathlib import Path

import requests

AURORA_BUSINESS_ID = "321924497251498"
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


def get_aurora_accounts() -> list[dict]:
    accounts = []
    params = {"fields": "id,name,account_status,disable_reason", "limit": 200}
    path = f"{AURORA_BUSINESS_ID}/owned_ad_accounts"
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


def send_telegram(text: str) -> None:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def main() -> int:
    try:
        accounts = get_aurora_accounts()
    except requests.HTTPError as e:
        # Meta token itself broken/expired - this is worth knowing about too,
        # since silence would otherwise look identical to "everything is fine".
        body = e.response.text if e.response is not None else str(e)
        send_telegram(
            f"⚠️ Aurora payment-alert check failed to run: could not reach "
            f"Meta Graph API ({e}). This likely means the Meta access token "
            f"has expired and needs refreshing.\n\nDetails: {body[:300]}"
        )
        print(f"ERROR calling Meta Graph API: {e}\n{body}", file=sys.stderr)
        return 1

    state = load_state()
    changed = False
    alerts = []

    seen_ids = set()
    for acct in accounts:
        acct_id = acct["id"]
        seen_ids.add(acct_id)
        name = acct.get("name", acct_id)
        status_code = acct.get("account_status")
        disable_reason = acct.get("disable_reason")
        previous_status = state.get(acct_id, {}).get("status")

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

        if state.get(acct_id, {}).get("status") != status_code:
            state[acct_id] = {"status": status_code, "name": name}
            changed = True

    # Drop accounts no longer under the Business Manager at all
    for stale_id in list(state.keys()):
        if stale_id not in seen_ids:
            del state[stale_id]
            changed = True

    for alert in alerts:
        send_telegram(alert)

    if changed:
        save_state(state)

    print(f"Checked {len(accounts)} accounts. {len(alerts)} alert(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
