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
```bash
cp .env.example .env
nano .env
```

| Variable | What it is |
|---|---|
| `BALE_BOT_TOKEN` | Token from Bale's BotFather |
| `OPENCLAW_TOKEN` | From `~/.openclaw/openclaw.json` → `gateway.auth.token` |
| `OPENCLAW_SESSION` | Session key prefix (default: `bale`) |
| `POLL_INTERVAL` | Seconds between polls when idle (default: `2`) |

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
