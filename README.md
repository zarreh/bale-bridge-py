# Bale ↔ OpenClaw Bridge (Python, polling mode)

Connects a Bale bot to your OpenClaw agent using polling — no webhook or public URL needed.

## Setup

### 1. Create a Bale bot
- Open Bale → search **BotFather**
- Create a bot, copy the token

### 2. Install dependencies
```bash
cd ~/bale-bridge-py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

**Multi-bot mode (recommended):** create `bots.json` from the example and fill in one entry per Bale bot.

```bash
cp bots.example.json bots.json
nano bots.json
```

```json
[
  { "name": "ALI_BALE",     "token": "123:ABC...", "session_prefix": "ali" },
  { "name": "SUPPORT_BALE", "token": "789:XYZ...", "session_prefix": "support" }
]
```

Each bot runs in its own polling thread with an isolated OpenClaw session namespace (`{session_prefix}-{chat_id}`), so channels never cross-contaminate. `bots.json` is gitignored.

**Legacy single-bot mode:** if `bots.json` is absent, `BALE_BOT_TOKEN` in `.env` is used (named `ALI_BALE` by default; override with `BALE_BOT_NAME`).

| Variable | What it is |
|---|---|
| `BALE_BOT_TOKEN` | Single-bot token (legacy; ignored when `bots.json` exists) |
| `BALE_BOT_NAME` | Label for the single-bot token (default: `ALI_BALE`) |
| `OPENCLAW_TOKEN` | From `~/.openclaw/openclaw.json` → `gateway.auth.token` |
| `OPENCLAW_SESSION` | Session prefix override for single-bot mode |
| `POLL_INTERVAL` | Seconds between polls when idle (default: `2`) |
| `AGENT_TIMEOUT` | OpenClaw CLI timeout seconds (default: `180`) |
| `BOTS_CONFIG` | Path to bots config file (default: `bots.json`) |

### 4. Start
```bash
source venv/bin/activate
python bridge.py
```

### 5. Test
Send a message to your Bale bot — it should reply via OpenClaw.

---

## Running as a service (systemd)

```ini
# /etc/systemd/system/bale-bridge.service
[Unit]
Description=Bale-OpenClaw Bridge
After=network.target

[Service]
Type=simple
User=cyrus
WorkingDirectory=/home/cyrus/bale-bridge-py
ExecStart=/home/cyrus/bale-bridge-py/venv/bin/python bridge.py
Restart=on-failure
EnvironmentFile=/home/cyrus/bale-bridge-py/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now bale-bridge
```
