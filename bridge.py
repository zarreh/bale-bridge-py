#!/usr/bin/env python3
"""
Bale ↔ OpenClaw Bridge  (polling mode, CLI backend, multi-token)
================================================================

How it works:
  1. For each configured bot token, a worker polls Bale for new updates
  2. Forwards each message to OpenClaw via `openclaw agent --session-id`
  3. Sends OpenClaw's reply back via Bale sendMessage

Configuration (in priority order):
  1. bots.json   — list of {name, token, session_prefix?} objects
  2. .env        — single-bot legacy mode (BALE_BOT_TOKEN)

Setup:
  1. pip install -r requirements.txt
  2. cp bots.example.json bots.json  →  fill in your bots
     (or keep .env with BALE_BOT_TOKEN for single-bot mode)
  3. python bridge.py
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread, Lock

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Global config ─────────────────────────────────────────────────────────

BALE_API_BASE    = "https://tapi.bale.ai/bot"
POLL_INTERVAL    = float(os.environ.get("POLL_INTERVAL", "2"))
AGENT_TIMEOUT    = int(os.environ.get("AGENT_TIMEOUT", "180"))
OPENCLAW_BIN     = os.environ.get("OPENCLAW_BIN", "openclaw")
BOTS_CONFIG_PATH = Path(os.environ.get("BOTS_CONFIG", "bots.json"))

# ─── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(name)s] %(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bale-bridge")


# ─── Bot worker ────────────────────────────────────────────────────────────

class BotWorker:
    """One polling worker per Bale token. Fully isolated state."""

    def __init__(self, name: str, token: str, session_prefix: str | None = None,
                 model: str | None = None, system_prompt: str | None = None):
        if not name:
            raise ValueError("bot name is required")
        if not token:
            raise ValueError(f"bot '{name}' is missing a token")
        self.name = name
        self.token = token
        self.session_prefix = session_prefix or name
        self.model = model
        self.system_prompt = system_prompt
        self.offset = 0
        self._processing: set = set()
        self._lock = Lock()
        self.log = logging.getLogger(f"bale-bridge.{name}")

    # ── Bale API ──────────────────────────────────────────────────────────

    def _api(self, method: str, data: dict) -> dict:
        url = f"{BALE_API_BASE}{self.token}/{method}"
        resp = requests.post(url, json=data, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Bale API error: {body}")
        return body.get("result", {})

    def send_message(self, chat_id, text: str):
        self._api("sendMessage", {"chat_id": chat_id, "text": text})

    def get_updates(self, offset: int) -> list:
        try:
            result = self._api("getUpdates", {
                "offset": offset,
                "limit": 100,
                "timeout": 5,
            })
            return result if isinstance(result, list) else []
        except Exception as e:
            self.log.error("getUpdates failed: %s", e)
            return []

    # ── OpenClaw ──────────────────────────────────────────────────────────

    def _run_openclaw(self, session_id: str, message: str) -> str | None:
        """Run openclaw agent with a message and return the text reply."""
        try:
            result = subprocess.run(
                [OPENCLAW_BIN, "agent", "--session-id", session_id,
                 "--message", message, "--json"],
                capture_output=True, text=True, timeout=AGENT_TIMEOUT,
            )
            if result.returncode != 0:
                self.log.error("openclaw error (rc=%d): %s", result.returncode, result.stderr[:200])
                return None
            try:
                data = json.loads(result.stdout)
                payloads = data.get("result", {}).get("payloads", [])
                if payloads:
                    return payloads[0].get("text", "").strip() or None
                return data.get("summary", "").strip() or None
            except (json.JSONDecodeError, KeyError):
                return (result.stdout or "").strip() or None
        except subprocess.TimeoutExpired:
            self.log.error("openclaw timed out after %ds", AGENT_TIMEOUT)
            return None
        except Exception as e:
            self.log.error("openclaw failed: %s", e)
            return None

    def send_to_openclaw(self, chat_id: str, text: str, sender: str) -> str | None:
        # Session ID scopes conversation per (bot, chat) so different bots
        # never share memory even if chat IDs happen to collide.
        session_id = f"{self.session_prefix}-{chat_id}"
        message = f"[{sender}]: {text}" if sender else text

        self.log.info("→ OpenClaw  chat=%-12s  sender=%-12s  %s",
                      chat_id, sender, text[:80])

        return self._run_openclaw(session_id, message)

    # ── Update handling ───────────────────────────────────────────────────

    def handle_update(self, update: dict):
        message = update.get("message") or update.get("channel_post")
        if not message:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        chat_id = str(message["chat"]["id"])
        sender_info = message.get("from", {})
        sender = (sender_info.get("username")
                  or sender_info.get("first_name")
                  or chat_id)

        with self._lock:
            if chat_id in self._processing:
                self.log.info("Skipping duplicate for chat %s", chat_id)
                return
            self._processing.add(chat_id)

        try:
            reply = self.send_to_openclaw(chat_id, text, sender)

            if reply:
                self.log.info("← Bale      chat=%-12s  %s", chat_id, str(reply)[:80])
                for chunk in split_text(reply):
                    self.send_message(chat_id, chunk)
            else:
                self.send_message(chat_id, "⚠️ دستیار پاسخی نداد. لطفاً دوباره امتحان کنید.")

        except Exception as e:
            self.log.error("Error handling update: %s", e)
            try:
                self.send_message(chat_id, "⚠️ خطایی رخ داد. لطفاً دوباره امتحان کنید.")
            except Exception:
                pass
        finally:
            with self._lock:
                self._processing.discard(chat_id)

    # ── Polling loop ──────────────────────────────────────────────────────

    def poll_loop(self):
        self.log.info("Polling Bale for new messages (session prefix: %s)",
                      self.session_prefix)
        while True:
            updates = self.get_updates(self.offset)

            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    self.offset = max(self.offset, update_id + 1)
                Thread(target=self.handle_update, args=(update,), daemon=True).start()

            if not updates:
                time.sleep(POLL_INTERVAL)

    def run_forever(self):
        """Wrap poll_loop with auto-restart so one bot's crash can't kill others."""
        backoff = 5
        while True:
            try:
                self.poll_loop()
            except Exception as e:
                self.log.error("poll_loop crashed: %s — restarting in %ds", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            else:
                return

    def verify(self) -> bool:
        try:
            me = self._api("getMe", {})
            self.log.info("Bot identity: @%s (id=%s)", me.get("username"), me.get("id"))
            return True
        except Exception as e:
            self.log.error("getMe failed — token may be invalid: %s", e)
            return False


# ─── Text chunking ─────────────────────────────────────────────────────────

def split_text(text: str, max_len: int = 4000) -> list:
    chunks = []
    while len(text) > max_len:
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


# ─── Config loading ────────────────────────────────────────────────────────

def load_bots() -> list[BotWorker]:
    """Load bot definitions from bots.json, or fall back to single-token .env."""
    if BOTS_CONFIG_PATH.is_file():
        log.info("Loading bots from %s", BOTS_CONFIG_PATH)
        try:
            raw = json.loads(BOTS_CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"ERROR: {BOTS_CONFIG_PATH} is not valid JSON: {e}")

        if not isinstance(raw, list) or not raw:
            sys.exit(f"ERROR: {BOTS_CONFIG_PATH} must be a non-empty JSON array")

        workers = []
        seen_names: set = set()
        seen_tokens: set = set()
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                sys.exit(f"ERROR: bots.json entry #{i} must be an object")
            name = entry.get("name")
            token = entry.get("token")
            if name in seen_names:
                sys.exit(f"ERROR: duplicate bot name '{name}' in {BOTS_CONFIG_PATH}")
            if token in seen_tokens:
                sys.exit(f"ERROR: duplicate token for bot '{name}' in {BOTS_CONFIG_PATH}")
            seen_names.add(name)
            seen_tokens.add(token)
            workers.append(BotWorker(
                name=name,
                token=token,
                session_prefix=entry.get("session_prefix"),
                model=entry.get("model"),
                system_prompt=entry.get("system_prompt"),
            ))
        return workers

    # Legacy single-bot mode
    token = os.environ.get("BALE_BOT_TOKEN", "").strip()
    if token:
        name = os.environ.get("BALE_BOT_NAME", "ALI_BALE")
        session_prefix = os.environ.get("OPENCLAW_SESSION") or name
        log.info("Loading single bot '%s' from .env (legacy mode)", name)
        return [BotWorker(name=name, token=token, session_prefix=session_prefix)]

    sys.exit(
        f"ERROR: no bots configured.\n"
        f"Create {BOTS_CONFIG_PATH} (see bots.example.json) "
        f"or set BALE_BOT_TOKEN in .env"
    )


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    log.info("Bale ↔ OpenClaw Bridge starting (CLI mode, multi-token)")

    workers = load_bots()
    log.info("Configured %d bot(s): %s", len(workers),
             ", ".join(w.name for w in workers))

    for w in workers:
        w.verify()  # non-fatal — log and continue

    threads = []
    for w in workers:
        t = Thread(target=w.run_forever, name=f"worker-{w.name}", daemon=True)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
