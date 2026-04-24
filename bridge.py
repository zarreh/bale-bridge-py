#!/usr/bin/env python3
"""
Bale ↔ OpenClaw Bridge  (polling mode, CLI backend)
====================================================

How it works:
  1. Polls Bale for new updates using getUpdates (Telegram-compatible API)
  2. Forwards each message to OpenClaw via `openclaw agent --session-id`
  3. Sends OpenClaw's reply back via Bale sendMessage

Setup:
  1. pip install -r requirements.txt
  2. cp .env.example .env  →  fill in BALE_BOT_TOKEN
  3. python bridge.py
"""

import json
import logging
import os
import subprocess
import sys
import time
from threading import Thread, Lock

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────

BALE_TOKEN       = os.environ.get("BALE_BOT_TOKEN", "")
BALE_API_BASE    = "https://tapi.bale.ai/bot"
OPENCLAW_SESSION = os.environ.get("OPENCLAW_SESSION", "bale")
POLL_INTERVAL    = float(os.environ.get("POLL_INTERVAL", "2"))
AGENT_TIMEOUT    = int(os.environ.get("AGENT_TIMEOUT", "180"))
OPENCLAW_BIN     = os.environ.get("OPENCLAW_BIN", "openclaw")

if not BALE_TOKEN:
    sys.exit("ERROR: BALE_BOT_TOKEN is not set in .env")

# ─── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[bale-bridge] %(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bale-bridge")

# ─── In-flight lock ────────────────────────────────────────────────────────

_lock = Lock()
_processing: set = set()

# ─── Bale API ──────────────────────────────────────────────────────────────

def bale_request(method: str, data: dict) -> dict:
    url = f"{BALE_API_BASE}{BALE_TOKEN}/{method}"
    resp = requests.post(url, json=data, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Bale API error: {body}")
    return body.get("result", {})


def bale_send_message(chat_id, text: str):
    bale_request("sendMessage", {"chat_id": chat_id, "text": text})


def bale_get_updates(offset: int) -> list:
    try:
        result = bale_request("getUpdates", {
            "offset": offset,
            "limit": 100,
            "timeout": 5,
        })
        return result if isinstance(result, list) else []
    except Exception as e:
        log.error("getUpdates failed: %s", e)
        return []

# ─── OpenClaw ──────────────────────────────────────────────────────────────

def send_to_openclaw(chat_id: str, text: str, sender: str) -> str | None:
    session_id = f"{OPENCLAW_SESSION}-{chat_id}"
    message = f"[{sender}]: {text}" if sender else text

    log.info("→ OpenClaw  chat=%-12s  sender=%-12s  %s", chat_id, sender, text[:80])

    try:
        result = subprocess.run(
            [OPENCLAW_BIN, "agent", "--session-id", session_id, "--message", message, "--json"],
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT,
        )
        if result.returncode != 0:
            log.error("openclaw agent error (rc=%d): %s", result.returncode, result.stderr[:200])
            return None
        # Parse JSON output and extract the actual reply text
        try:
            data = json.loads(result.stdout)
            payloads = data.get("result", {}).get("payloads", [])
            if payloads:
                return payloads[0].get("text", "").strip() or None
            # Fallback: try top-level summary field
            return data.get("summary", "").strip() or None
        except (json.JSONDecodeError, KeyError):
            # Plain text fallback
            return (result.stdout or "").strip() or None
    except subprocess.TimeoutExpired:
        log.error("openclaw agent timed out after %ds", AGENT_TIMEOUT)
        return None
    except Exception as e:
        log.error("openclaw agent failed: %s", e)
        return None

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

# ─── Update handler ────────────────────────────────────────────────────────

def handle_update(update: dict):
    message = update.get("message") or update.get("channel_post")
    if not message:
        return

    text = (message.get("text") or "").strip()
    if not text:
        return

    chat_id = str(message["chat"]["id"])
    sender_info = message.get("from", {})
    sender = sender_info.get("username") or sender_info.get("first_name") or chat_id

    with _lock:
        if chat_id in _processing:
            log.info("Skipping duplicate for chat %s", chat_id)
            return
        _processing.add(chat_id)

    try:
        reply = send_to_openclaw(chat_id, text, sender)

        if reply:
            log.info("← Bale      chat=%-12s  %s", chat_id, str(reply)[:80])
            for chunk in split_text(reply):
                bale_send_message(chat_id, chunk)
        else:
            bale_send_message(chat_id, "⚠️ دستیار پاسخی نداد. لطفاً دوباره امتحان کنید.")

    except Exception as e:
        log.error("Error handling update: %s", e)
        try:
            bale_send_message(chat_id, "⚠️ خطایی رخ داد. لطفاً دوباره امتحان کنید.")
        except Exception:
            pass
    finally:
        with _lock:
            _processing.discard(chat_id)

# ─── Polling loop ──────────────────────────────────────────────────────────

def poll_loop():
    offset = 0
    log.info("Polling Bale for new messages...")

    while True:
        updates = bale_get_updates(offset)

        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                offset = max(offset, update_id + 1)
            Thread(target=handle_update, args=(update,), daemon=True).start()

        if not updates:
            time.sleep(POLL_INTERVAL)

# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Bale ↔ OpenClaw Bridge starting (CLI mode)")
    log.info("Session prefix: %s", OPENCLAW_SESSION)

    try:
        me = bale_request("getMe", {})
        log.info("Bot identity: @%s (id=%s)", me.get("username"), me.get("id"))
    except Exception as e:
        log.warning("Could not verify bot token: %s", e)

    try:
        poll_loop()
    except KeyboardInterrupt:
        log.info("Shutting down.")
