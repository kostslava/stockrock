from __future__ import annotations

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


class OpenAIAdvisor:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)

    def summarize_decision(self, payload: dict) -> tuple[str, str]:
        if not self.enabled:
            return ("AI summary unavailable (missing OPENAI_API_KEY).", json.dumps(payload, indent=2))
        prompt = (
            "You are a conservative trading assistant. Summarize the proposal in 5 bullets max "
            "for user approval. Then provide an 'Explain Why' section with technical stats and "
            "predicted price impact using the provided data only. Keep it concise."
            f"\n\nDATA:\n{json.dumps(payload, indent=2)}"
        )
        body = {
            "model": self.model,
            "input": prompt,
            "max_output_tokens": 1800,
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = requests.post("https://api.openai.com/v1/responses", headers=headers, json=body, timeout=90)
                resp.raise_for_status()
                data = resp.json()
                text = (data.get("output_text") or "").strip()
                if not text:
                    text_parts: list[str] = []
                    for item in data.get("output", []):
                        for content in item.get("content", []):
                            val = content.get("text")
                            if val:
                                text_parts.append(val)
                    text = "\n".join(text_parts).strip()
                if not text:
                    text = "No summary generated."
                marker = "Explain Why"
                if marker in text:
                    short, full = text.split(marker, 1)
                    return short.strip(), f"{marker}\n{full.strip()}"
                return text, text
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
        logger.warning("OpenAI summary failed after retries: %s", last_error)
        fallback = json.dumps(payload, indent=2)
        return ("AI summary failed; using raw metrics.", fallback)
