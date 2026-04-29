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
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from threading import Thread, Lock

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Global config ─────────────────────────────────────────────────────────

BALE_API_BASE    = "https://tapi.bale.ai/bot"
POLL_INTERVAL    = float(os.environ.get("POLL_INTERVAL", "2"))
AGENT_TIMEOUT    = int(os.environ.get("AGENT_TIMEOUT", "300"))
OPENCLAW_BIN     = os.environ.get("OPENCLAW_BIN", "openclaw")
BOTS_CONFIG_PATH = Path(os.environ.get("BOTS_CONFIG", "bots.json"))
FAL_KEY          = os.environ.get("FAL_KEY", "")

# ─── Image generation keywords (Farsi + English) ──────────────────────────
IMAGE_GEN_PATTERNS = re.compile(
    r'(عکس\s*(بساز|بگیر|بکش|ایجاد|خلق|تولید|درست)|'
    r'(بساز|بکش|درست\s*کن|خلق\s*کن|ایجاد\s*کن|تولید\s*کن).{0,40}(عکس|تصویر|نقاشی)|'
    r'(عکس|تصویر|نقاشی).{0,40}(بساز|بکش|درست\s*کن|خلق\s*کن|ایجاد\s*کن|تولید\s*کن|بگیر)|'
    r'یه\s*(عکس|تصویر|نقاشی)\s*(از|بساز|بکش|درست|باحال|خنده|جالب)|'
    r'برام\s*(یه\s*)?(عکس|تصویر|نقاشی)|'
    r'(میتونی|میتونه|میشه|ممکنه).{0,30}(عکس|تصویر|نقاشی).{0,40}(بساز|بکش|درست|بگیر|خلق|ایجاد|تولید)|'
    r'generate\s*(an?\s*)?(image|photo|picture|illustration)|'
    r'create\s*(an?\s*)?(image|photo|picture|illustration)|'
    r'(draw|paint|imagine|visualize)\s+.{3,}|'
    r'make\s*(an?\s*)?(image|photo|picture|illustration)|'
    r'can you (make|create|generate|draw).{0,20}(image|photo|picture))',
    re.IGNORECASE
)

def is_image_request(text: str) -> bool:
    """Return True if the message is asking for image generation."""
    return bool(IMAGE_GEN_PATTERNS.search(text))

def translate_prompt_to_english(prompt: str) -> str:
    """Translate a prompt to English using OpenAI for better image generation results."""
    try:
        import requests as _req, os as _os
        api_key = None
        try:
            import subprocess as _sp
            r = _sp.run(['bash', '-c', r"grep -oP '(?<=OPENAI_API_KEY=)\S+' ~/.bashrc | head -1"],
                        capture_output=True, text=True)
            api_key = r.stdout.strip()
        except Exception:
            pass
        if not api_key:
            return prompt
        resp = _req.post(
            'https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': 'gpt-4o-mini',
                'messages': [
                    {'role': 'system', 'content': 'You are a prompt translator. Translate the user message to English as a descriptive image generation prompt. Output only the English prompt, nothing else.'},
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': 200,
            },
            timeout=15
        )
        resp.raise_for_status()
        translated = resp.json()['choices'][0]['message']['content'].strip()
        logging.getLogger('bale-bridge').info('Translated prompt: %s → %s', prompt[:60], translated[:60])
        return translated
    except Exception as e:
        logging.getLogger('bale-bridge').warning('Prompt translation failed, using original: %s', e)
        return prompt

def generate_image_fal(prompt: str) -> tuple[str | None, str]:
    """Call fal.ai FLUX Dev to generate an image. Returns (local file path or None, english_prompt)."""
    if not FAL_KEY:
        logging.getLogger('bale-bridge').error('FAL_KEY not set')
        return None, prompt
    # Translate prompt to English first for best results
    english_prompt = translate_prompt_to_english(prompt)
    try:
        import fal_client
        os.environ['FAL_KEY'] = FAL_KEY
        result = fal_client.run(
            'fal-ai/flux/dev',
            arguments={
                'prompt': english_prompt,
                'image_size': 'landscape_4_3',
                'num_inference_steps': 28,
                'num_images': 1,
                'enable_safety_checker': True,
            }
        )
        images = result.get('images', [])
        if not images:
            return None, english_prompt
        img_url = images[0].get('url')
        if not img_url:
            return None, english_prompt
        # Download the image
        import requests as _req
        resp = _req.get(img_url, timeout=60)
        resp.raise_for_status()
        dest = f'/home/cyrus/.openclaw/media/inbound/fal_image_{uuid.uuid4().hex[:8]}.jpg'
        with open(dest, 'wb') as f:
            f.write(resp.content)
        return dest, english_prompt
    except Exception as e:
        logging.getLogger('bale-bridge').error('fal.ai image gen failed: %s', e)
        return None, english_prompt

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
                 model: str | None = None, system_prompt: str | None = None,
                 agent: str | None = None):
        if not name:
            raise ValueError("bot name is required")
        if not token:
            raise ValueError(f"bot '{name}' is missing a token")
        self.name = name
        self.token = token
        self.session_prefix = session_prefix or name
        self.model = model
        self.system_prompt = system_prompt
        self.agent = agent  # OpenClaw agent id (e.g. "bale-ali")
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

    def download_file(self, file_id: str) -> str | None:
        """Download a Bale file by file_id, save to /tmp, return local path."""
        try:
            result = self._api("getFile", {"file_id": file_id})
            file_path = result.get("file_path")
            if not file_path:
                return None
            # Correct Bale download URL: https://tapi.bale.ai/file/bot{token}/{file_path}
            url = f"https://tapi.bale.ai/file/bot{self.token}/{file_path}"
            # Preserve original extension from file_path
            ext = os.path.splitext(file_path)[1] if file_path else ".ogg"
            if not ext:
                ext = ".ogg"
            local_path = f"/tmp/bale_file_{file_id[:12]}{ext}"
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)
            return local_path
        except Exception as e:
            self.log.error("download_file failed: %s", e)
            return None

    def send_voice(self, chat_id: str, audio_path: str):
        """Send a voice/audio file back to Bale."""
        try:
            url = f"{BALE_API_BASE}{self.token}/sendVoice"
            with open(audio_path, "rb") as f:
                requests.post(url, data={"chat_id": chat_id}, files={"voice": f}, timeout=30)
        except Exception as e:
            self.log.error("send_voice failed: %s", e)

    def send_photo(self, chat_id: str, image_path: str, caption: str = ""):
        """Send an image file back to Bale."""
        try:
            url = f"{BALE_API_BASE}{self.token}/sendPhoto"
            with open(image_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                requests.post(url, data=data, files={"photo": f}, timeout=60)
        except Exception as e:
            self.log.error("send_photo failed: %s", e)

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

    def _run_openclaw(self, session_id: str, message: str) -> tuple[str | None, str | None]:
        """Run openclaw agent with a message. Returns (text, audio_path)."""
        try:
            cmd = [OPENCLAW_BIN, "agent", "--session-id", session_id,
                   "--message", message, "--json"]
            if self.agent:
                cmd += ["--agent", self.agent]
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=AGENT_TIMEOUT,
            )
            if result.returncode != 0:
                self.log.error("openclaw error (rc=%d): %s", result.returncode, result.stderr[:200])
                return None, None
            try:
                data = json.loads(result.stdout)
                payloads = data.get("result", {}).get("payloads", [])
                if payloads:
                    p = payloads[0]
                    text = p.get("text", "").strip() or None
                    audio = p.get("mediaUrl") or (p.get("mediaUrls") or [None])[0]
                    return text, audio
                summary = data.get("summary", "").strip() or None
                return summary, None
            except (json.JSONDecodeError, KeyError):
                return (result.stdout or "").strip() or None, None
        except subprocess.TimeoutExpired:
            self.log.error("openclaw timed out after %ds", AGENT_TIMEOUT)
            return None, None
        except Exception as e:
            self.log.error("openclaw failed: %s", e)
            return None, None

    def send_to_openclaw(self, chat_id: str, text: str, sender: str) -> tuple[str | None, str | None]:
        # Session ID scopes conversation per (bot, chat) so different bots
        # never share memory even if chat IDs happen to collide.
        session_id = f"{self.session_prefix}-{chat_id}"
        message = text

        # If it's a voice message, include media attachment hint for OpenClaw
        if text.startswith("[voice message:") and ".ogg" in text:
            import re
            m = re.search(r'\[voice message: (.+?)\]', text)
            if m:
                ogg_path = m.group(1)
                message = f"[media attached: {ogg_path} (audio/ogg; codecs=opus) | {ogg_path}]\n<media:audio>"

        self.log.info("→ OpenClaw  chat=%-12s  sender=%-12s  %s",
                      chat_id, sender, text[:80])

        return self._run_openclaw(session_id, message)

    # ── Update handling ───────────────────────────────────────────────────

    def handle_update(self, update: dict):
        message = update.get("message") or update.get("channel_post")
        if not message:
            return

        chat_id = str(message["chat"]["id"])
        sender_info = message.get("from", {})
        sender = (sender_info.get("username")
                  or sender_info.get("first_name")
                  or chat_id)

        text = (message.get("text") or "").strip()

        # Handle voice/audio messages
        voice = message.get("voice") or message.get("audio")
        if voice and not text:
            file_id = voice.get("file_id")
            if file_id:
                local_path = self.download_file(file_id)
                if local_path:
                    # Copy to OpenClaw inbound media folder
                    import shutil
                    import uuid
                    dest = f"/home/cyrus/.openclaw/media/inbound/bale_voice_{uuid.uuid4().hex[:8]}.ogg"
                    shutil.copy2(local_path, dest)
                    text = f"[voice message: {dest}]"
                    self.log.info("Voice saved → %s", dest)
                else:
                    text = "[voice message: download failed]"
            else:
                return

        # Handle photo messages
        photos = message.get("photo")
        if photos and not text:
            import shutil, uuid
            # Bale sends multiple sizes; pick the largest (last)
            photo = photos[-1]
            file_id = photo.get("file_id")
            caption = message.get("caption", "").strip()
            if file_id:
                local_path = self.download_file(file_id)
                if local_path:
                    ext = os.path.splitext(local_path)[1] or ".jpg"
                    dest = f"/home/cyrus/.openclaw/media/inbound/bale_photo_{uuid.uuid4().hex[:8]}{ext}"
                    shutil.copy2(local_path, dest)
                    self.log.info("Photo saved → %s", dest)
                    # Build media attachment message for OpenClaw
                    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
                    text = f"[media attached: {dest} ({mime}) | {dest}]\n<media:image>"
                    if caption:
                        text += f"\n{caption}"
                else:
                    text = "[photo: download failed]"
                    if caption:
                        text += f" — caption: {caption}"
            else:
                return

        # Handle document messages (could be images sent as files)
        document = message.get("document")
        if document and not text:
            import shutil, uuid
            mime_type = document.get("mime_type", "")
            file_id = document.get("file_id")
            caption = message.get("caption", "").strip()
            if file_id and mime_type.startswith("image/"):
                local_path = self.download_file(file_id)
                if local_path:
                    ext = os.path.splitext(local_path)[1] or ".jpg"
                    dest = f"/home/cyrus/.openclaw/media/inbound/bale_doc_{uuid.uuid4().hex[:8]}{ext}"
                    shutil.copy2(local_path, dest)
                    self.log.info("Document/image saved → %s", dest)
                    text = f"[media attached: {dest} ({mime_type}) | {dest}]\n<media:image>"
                    if caption:
                        text += f"\n{caption}"
                else:
                    text = f"[document: download failed]"
            else:
                return

        if not text:
            return

        with self._lock:
            if chat_id in self._processing:
                self.log.info("Skipping duplicate for chat %s", chat_id)
                return
            self._processing.add(chat_id)

        try:
            # ── Image generation request? ───────────────────────────────────────
            if is_image_request(text) and not text.startswith("[media attached:"):
                self.log.info("🎨 Image request detected for chat %s", chat_id)
                self.send_message(chat_id, "🎨 در حال ساخت تصویر... لطفاً چند ثانیه صبر کنید.")
                image_path, english_prompt = generate_image_fal(text)
                if image_path:
                    self.log.info("← Bale(image) chat=%-12s  %s", chat_id, image_path)
                    self.send_photo(chat_id, image_path)
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass
                    # Give OpenClaw context so follow-up questions work
                    session_id = f"{self.session_prefix}-{chat_id}"
                    self._run_openclaw(session_id,
                        f"[system context: An image was just generated and sent to the user. "
                        f"Prompt used: '{english_prompt}'. "
                        f"If the user asks about the image or requests changes, you know what was created.]")
                else:
                    self.send_message(chat_id, "⚠️ متأسفم، ساخت تصویر موفق نشد. دوباره امتحان کنید.")
                return

            reply_text, reply_audio = self.send_to_openclaw(chat_id, text, sender)

            if reply_audio:
                # Resolve path: check as-is, then in /tmp/, then in agent workspace
                audio_path = None
                for candidate in [
                    reply_audio,
                    os.path.join('/tmp', os.path.basename(reply_audio)),
                ]:
                    if os.path.isfile(candidate):
                        audio_path = candidate
                        break

                if audio_path:
                    self.log.info("← Bale(voice) chat=%-12s  %s", chat_id, audio_path)
                    self.send_voice(chat_id, audio_path)
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass
                    return  # don't fall through to text handling
            elif reply_text:
                self.log.info("← Bale      chat=%-12s  %s", chat_id, str(reply_text)[:80])
                for chunk in split_text(reply_text):
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
                agent=entry.get("agent"),
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
