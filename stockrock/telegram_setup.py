from __future__ import annotations

import argparse
import json
import os

import requests
from dotenv import load_dotenv


def _api(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if payload:
        resp = requests.post(url, json=payload, timeout=15)
    else:
        resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram setup helper for StockRock")
    parser.add_argument("--send-test", action="store_true", help="Send a test alert if chat id is configured")
    args = parser.parse_args()

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")

    me = _api(token, "getMe")
    print("Bot:", json.dumps(me.get("result", {}), indent=2))

    updates = _api(token, "getUpdates")
    result = updates.get("result", [])
    if not result:
        print("No updates yet. Send a message to your bot in Telegram, then run this command again.")
    else:
        candidate_ids = []
        for item in result:
            msg = item.get("message") or item.get("edited_message") or {}
            chat = msg.get("chat", {})
            if "id" in chat:
                candidate_ids.append(chat["id"])
        unique_ids = sorted({str(x) for x in candidate_ids})
        print("Discovered chat_id values:", unique_ids)

    if args.send_test:
        if not chat_id:
            print("Cannot send test: TELEGRAM_CHAT_ID is empty.")
            return
        test = _api(
            token,
            "sendMessage",
            {"chat_id": chat_id, "text": "StockRock test: Telegram alerts are connected."},
        )
        print("sendMessage:", json.dumps(test, indent=2))


if __name__ == "__main__":
    main()
