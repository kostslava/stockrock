from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, message: str, markdown: bool = False) -> None:
        if not self.enabled:
            logger.info("Telegram disabled: missing bot token or chat id")
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        if markdown:
            payload["parse_mode"] = "Markdown"
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                logger.warning("Telegram API error: status=%s body=%s", resp.status_code, resp.text)
                return
            body = resp.json()
            if not body.get("ok", False):
                logger.warning("Telegram API rejected message: %s", body)
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    def request_approval(self, summary: str, explain: str, timeout_sec: int) -> bool:
        if not self.enabled:
            logger.info("Telegram approval skipped: disabled")
            return True

        buttons = {
            "inline_keyboard": [
                [
                    {"text": "Yes", "callback_data": "approve_yes"},
                    {"text": "No", "callback_data": "approve_no"},
                    {"text": "Explain why", "callback_data": "approve_explain"},
                ]
            ]
        }
        api_base = f"https://api.telegram.org/bot{self.bot_token}"

        # Start from newest update to avoid consuming stale callbacks.
        seed = requests.get(f"{api_base}/getUpdates", params={"timeout": 0}, timeout=10).json()
        max_seen = 0
        for item in seed.get("result", []):
            max_seen = max(max_seen, int(item.get("update_id", 0)))
        if max_seen:
            self._offset = max(self._offset, max_seen + 1)

        payload = {
            "chat_id": self.chat_id,
            "text": summary[:3500],
            "reply_markup": buttons,
            "disable_web_page_preview": True,
            "parse_mode": "Markdown",
        }
        sent = requests.post(f"{api_base}/sendMessage", json=payload, timeout=10)
        sent.raise_for_status()
        sent_data = sent.json()
        message_id = sent_data.get("result", {}).get("message_id")

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                updates_resp = requests.get(
                    f"{api_base}/getUpdates",
                    params={"timeout": 20, "offset": self._offset},
                    timeout=30,
                )
                updates_resp.raise_for_status()
                updates = updates_resp.json()
            except requests.RequestException as exc:
                logger.warning("Telegram polling transient error: %s", exc)
                time.sleep(1)
                continue
            if not updates.get("ok", False):
                logger.warning("Telegram getUpdates error: %s", updates)
                time.sleep(1)
                continue
            for item in updates.get("result", []):
                self._offset = max(self._offset, item["update_id"] + 1)
                cb = item.get("callback_query")
                if not cb:
                    continue
                cb_msg = cb.get("message", {}) or {}
                if str(cb_msg.get("chat", {}).get("id")) != self.chat_id:
                    continue
                if message_id and cb_msg.get("message_id") != message_id:
                    continue
                action = cb.get("data", "")
                cb_id = cb.get("id")
                if cb_id:
                    ack_text = "Got it."
                    if action == "approve_yes":
                        ack_text = "Approved."
                    elif action == "approve_no":
                        ack_text = "Rejected."
                    elif action == "approve_explain":
                        ack_text = "Sending explanation."
                    requests.post(f"{api_base}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": ack_text}, timeout=10)
                if action == "approve_yes":
                    self.send("✅ *Approved.* Executing trade.", markdown=True)
                    return True
                if action == "approve_no":
                    self.send("❌ *Rejected.* Trade cancelled.", markdown=True)
                    return False
                if action == "approve_explain":
                    self.send(explain[:3500], markdown=True)
                    self.send("Use the same *Yes/No* buttons on the approval message.", markdown=True)
        self.send("Approval timed out. Trade cancelled.")
        return False
