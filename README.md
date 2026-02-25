# 🔥 Poly Series Alerts

**Real-time streak detection for Polymarket 5-minute binary markets.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue?logo=telegram)](https://core.telegram.org/bots)

---

## What is this?

Polymarket runs 5-minute binary markets for crypto assets (BTC, ETH, SOL, XRP). Every 5 minutes, each coin resolves as **Up** or **Down**.

When the same outcome repeats consecutively — that's a **streak**. Long streaks are statistically rare and can signal mean-reversion opportunities.

This bot monitors all 4 assets in real time and **sends you a Telegram alert** the moment a streak hits your threshold.

### Example Alert

```
🔥 Streak 9 — 🔴 BTC
[⏳ Current] [➡️ Next]
```

## Features

- 📊 **4 assets** — BTC, ETH, SOL, XRP tracked simultaneously
- ⚡ **Real-time** — polls Polymarket Gamma API every 10 seconds
- 🎯 **Per-coin settings** — set individual streak thresholds for each asset
- ⏸ **Pause/resume** — pause alerts without losing your settings
- 🔔 **Smart deduplication** — no duplicate alerts on server restart
- 📢 **Channel subscription gating** — optional free-tier with channel sub requirement
- 🗄 **SQLite storage** — lightweight, no external DB needed
- 🔄 **Auto-recovery** — batch-loads recent history on restart, no missed streaks
- 🧵 **Thread-safe** — parallel alert delivery via thread pool

## Architecture

The project runs as **two separate processes**:

| Process | File | Role |
|---------|------|------|
| **Server** | `server.py` | Monitors Polymarket, detects streaks, sends alerts |
| **Bot** | `bot.py` | Handles Telegram commands (`/start`, `/settings`, `/status`) |
| **DB** | `db.py` | Shared SQLite interface (thread-safe, WAL mode) |

```
┌─────────────┐     ┌──────────┐     ┌─────────────┐
│  Polymarket  │────▶│  server  │────▶│  Telegram    │
│  Gamma API   │     │  .py     │     │  (alerts)    │
└─────────────┘     └────┬─────┘     └─────────────┘
                         │
                    ┌────┴─────┐
                    │ streak.db │
                    └────┬─────┘
                         │
                    ┌────┴─────┐     ┌─────────────┐
                    │  bot.py  │◀───▶│  Telegram    │
                    └──────────┘     │  (commands)  │
                                     └─────────────┘
```

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/yourusername/poly-series-alerts.git
cd poly-series-alerts
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your Telegram bot token
```

**Required:**
- `STREAK_BOT_TOKEN` — get from [@BotFather](https://t.me/BotFather)

**Optional:**
- `CHANNEL_USERNAME` — Telegram channel for subscription gating
- `PROXY_*` — SOCKS5 proxy if Polymarket is geo-blocked in your region

### 3. Run

Start both processes (e.g., in separate terminals or via `tmux`):

```bash
# Terminal 1: Data engine + alerts
python server.py

# Terminal 2: Telegram bot (commands)
python bot.py
```

Or with a process manager:

```bash
# tmux example
tmux new-session -d -s streak
tmux send-keys -t streak 'python server.py' Enter
tmux split-window -t streak
tmux send-keys -t streak 'python bot.py' Enter
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + registration |
| `/settings` | Configure coins and streak thresholds |
| `/status` | Current streak for all 4 coins |
| `/btc` `/eth` `/sol` `/xrp` | Detailed view: last 30 outcomes + streak |

## Configuration

### Streak Thresholds

Each user can set individual thresholds per coin via `/settings`:

- **Min value:** 2
- **Default:** 8
- Toggle coins on/off independently
- "Set All" button for bulk changes

### Subscription Gating (Optional)

The bot supports a freemium model:
- **24-hour free trial** on first `/start`
- After trial: requires subscription to your Telegram channel
- Subscribers get unlimited access
- Periodic re-verification of subscriptions

To disable gating, remove `CHANNEL_USERNAME` from your `.env`.

## How Streak Detection Works

1. Server polls Gamma API for resolved 5-min markets
2. For each asset, outcomes are appended chronologically
3. Consecutive same-direction outcomes are counted as a streak
4. When streak ≥ user's threshold → alert is sent
5. Deduplication ensures no repeated alerts for the same streak

### On Restart

The server performs a batch recovery:
- Fetches recent resolved markets from Gamma API
- Rebuilds streak state from historical outcomes
- Sends alerts for any active streaks that weren't previously notified

## Tech Stack

- **Python 3.10+** — no heavy frameworks
- **SQLite** (WAL mode) — fast, concurrent, zero-config
- **Telegram Bot API** — long polling, inline keyboards
- **Polymarket Gamma API** — market data and resolution status
- **requests** — HTTP client with proxy support

## License

[MIT](LICENSE) — use it, fork it, build on it.

---

*Not financial advice. Trade at your own risk.*
