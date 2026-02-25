#!/usr/bin/env python3
"""
Streak Bot v4.0 — Telegram Interface (SQLite)
- /start → welcome + register
- /settings → individual settings per coin (assets + min streak per coin)
- /btc /eth /sol /xrp → check current streak
"""

import json
import os
import time
import logging
import requests
from collections import defaultdict
from pathlib import Path

import db

# ─── Rate Limiting ────────────────────────────────────────────────────────────

_rate_limit = {}           # chat_id -> list of timestamps
_rate_limit_cleanup = 0.0  # last cleanup time
MAX_TRACKED_USERS = 5000   # max users in rate limit dict

_global_counter = []       # global requests per second
GLOBAL_MAX_PER_SEC = 50    # max updates processed per second across all users


def _cleanup_rate_limit():
    """Remove stale entries older than 120s. Called periodically."""
    global _rate_limit_cleanup
    now = time.time()
    if now - _rate_limit_cleanup < 60:
        return
    _rate_limit_cleanup = now
    stale = [k for k, v in _rate_limit.items() if not v or now - v[-1] > 120]
    for k in stale:
        del _rate_limit[k]


def is_rate_limited(chat_id: str, max_per_minute: int = 20) -> bool:
    """Returns True if user exceeded max messages per minute or global limit hit."""
    now = time.time()

    # Global rate limit
    _global_counter[:] = [t for t in _global_counter if now - t < 1]
    if len(_global_counter) >= GLOBAL_MAX_PER_SEC:
        return True
    _global_counter.append(now)

    # Periodic cleanup
    _cleanup_rate_limit()

    # Cap tracked users (evict oldest on overflow)
    if chat_id not in _rate_limit and len(_rate_limit) >= MAX_TRACKED_USERS:
        oldest = min(_rate_limit, key=lambda k: _rate_limit[k][-1] if _rate_limit[k] else 0)
        del _rate_limit[oldest]

    # Per-user rate limit
    if chat_id not in _rate_limit:
        _rate_limit[chat_id] = []
    _rate_limit[chat_id] = [t for t in _rate_limit[chat_id] if now - t < 60]
    if len(_rate_limit[chat_id]) >= max_per_minute:
        return True
    _rate_limit[chat_id].append(now)
    return False

# ─── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("STREAK_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    # Load from parent .env file
    _env = Path(__file__).parent.parent / ".env"
    if _env.exists():
        for _line in _env.read_text().splitlines():
            if _line.startswith("STREAK_BOT_TOKEN="):
                TELEGRAM_BOT_TOKEN = _line.split("=", 1)[1].strip()
                break
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("STREAK_BOT_TOKEN not set")

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@yourchannel")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "https://t.me/yourchannel")
X_LINK = os.environ.get("X_LINK", "https://x.com/yourprofile")
TRIAL_DURATION = 86400  # 24 hours

ASSETS = {
    "btc": {"name": "Bitcoin", "short": "BTC"},
    "eth": {"name": "Ethereum", "short": "ETH"},
    "sol": {"name": "Solana", "short": "SOL"},
    "xrp": {"name": "XRP", "short": "XRP"},
}

MIN_STREAK_MIN = 2

COMMANDS = {"/btc": "btc", "/eth": "eth", "/sol": "sol", "/xrp": "xrp"}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# ─── Telegram Helpers ─────────────────────────────────────────────────────────

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

waiting_for_streak = {}      # chat_id -> (asset_key, timestamp)
MAX_WAITING = 1000           # max pending streak inputs
WAITING_TTL = 300            # 5 min timeout for streak input


def tg_get_updates(offset=None):
    params = {"timeout": 30, "limit": 10, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(f"{API}/getUpdates", params=params, timeout=35)
        if resp.ok:
            return resp.json().get("result", [])
    except:
        pass
    return []


def tg_send(chat_id, text, buttons=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        resp = requests.post(f"{API}/sendMessage", json=payload, timeout=5)
        if resp.ok:
            return resp.json().get("result", {}).get("message_id")
    except:
        pass
    return None


def tg_edit(chat_id, message_id, text, buttons=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        resp = requests.post(f"{API}/editMessageText", json=payload, timeout=5)
        if not resp.ok:
            log.warning(f"tg_edit fail: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log.warning(f"tg_edit error: {e}")


def tg_answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{API}/answerCallbackQuery", json=payload, timeout=5)
    except:
        pass


def check_channel_subscription(user_id: str) -> bool:
    """Check if user is member/admin/creator of CHANNEL_USERNAME.
    Fails closed — returns False on API error (require explicit confirmation)."""
    try:
        resp = requests.get(f"{API}/getChatMember", params={
            "chat_id": CHANNEL_USERNAME,
            "user_id": user_id
        }, timeout=5)
        if resp.ok:
            status = resp.json().get("result", {}).get("status", "left")
            return status in ("member", "administrator", "creator")
    except:
        pass
    return False  # fail closed


def send_blocked_message(chat_id: str):
    tg_send(chat_id,
        "🔒 <b>Your access has expired</b>\n\n"
        "Your 24-hour free trial is over.\n\n"
        "To continue receiving streak alerts and using all bot features, "
        "subscribe to our Telegram channel — it's <b>completely free</b>!\n\n"
        "👇 Subscribe now and tap \"I've Subscribed\" to unlock the bot.",
        buttons=[[
            {"text": "📢 Subscribe", "url": CHANNEL_LINK},
            {"text": "✅ I've Subscribed", "callback_data": "check_sub"},
        ]])


def check_and_gate(chat_id: str, user_id: str) -> bool:
    """Returns True if user can proceed, False if blocked."""
    if not db.is_user_active(chat_id):
        if check_channel_subscription(user_id):
            db.set_subscriber(chat_id, True)
            return True
        else:
            send_blocked_message(chat_id)
            return False
    return True


# ─── Settings UI ──────────────────────────────────────────────────────────────

def build_settings_text(prefs: dict, chat_id: str = None) -> str:
    coins = prefs["coins"]
    lines = ""
    for key in ASSETS:
        c = coins[key]
        short = ASSETS[key]["short"]
        status = "✅" if c["enabled"] else "❌"
        streak_info = f"streak ≥ {c['min_streak']}" if c["enabled"] else "off"
        lines += f"  {short} {status}  ({streak_info})\n"
    paused = db.is_alerts_paused(chat_id) if chat_id else False
    pause_line = "\n⏸ <b>Alerts are PAUSED</b> — you won't receive notifications.\n" if paused else ""
    text = (
        "⚙️ <b>Your Settings</b>\n\n"
        f"{lines}"
        f"{pause_line}\n"
        "<i>Tap a coin to toggle on/off.\n"
        "Tap the streak button to set threshold per coin.</i>"
    )

    if chat_id:
        text += "\n\n━━━━━━━━━━━━━━━\n"
        if db.is_subscriber_cached(chat_id):
            text += "\n💚 <b>Subscribed</b> — unlimited access. Thank you for your support!"
        else:
            remaining = db.get_trial_remaining(chat_id)
            if remaining > 0:
                hours = int(remaining // 3600)
                minutes = int((remaining % 3600) // 60)
                text += (
                    f"\n⚠️ <b>Free trial: {hours}h {minutes}m remaining</b>\n\n"
                    "After trial expires, the bot will stop sending alerts.\n"
                    "Subscribe to our channel for <b>unlimited free access forever</b>!"
                )
            else:
                text += (
                    "\n⚠️ <b>Trial expired!</b>\n\n"
                    "Subscribe to continue using the bot."
                )

    return text


def build_settings_buttons(prefs: dict, chat_id: str = None) -> list:
    coins = prefs["coins"]
    rows = []
    asset_row = []
    for key in ASSETS:
        short = ASSETS[key]["short"]
        on = "✅" if coins[key]["enabled"] else "❌"
        asset_row.append({"text": f"{short} {on}", "callback_data": f"toggle_{key}"})
    rows.append(asset_row)

    streak_row = []
    for key in ASSETS:
        if coins[key]["enabled"]:
            short = ASSETS[key]["short"]
            streak_row.append({
                "text": f"{short}: ≥{coins[key]['min_streak']}",
                "callback_data": f"streak_{key}",
            })
    if streak_row:
        rows.append(streak_row)

    rows.append([{"text": "📋 Set All", "callback_data": "streak_all"}])

    # Pause/resume alerts toggle
    paused = db.is_alerts_paused(chat_id) if chat_id else False
    if paused:
        rows.append([{"text": "▶️ Resume Alerts", "callback_data": "toggle_pause"}])
    else:
        rows.append([{"text": "⏸ Pause Alerts", "callback_data": "toggle_pause"}])

    # Subscription buttons
    if chat_id:
        if db.is_subscriber_cached(chat_id):
            rows.append([{"text": "🐦 Follow on X", "url": X_LINK}])
        else:
            remaining = db.get_trial_remaining(chat_id)
            if remaining > 0:
                rows.append([
                    {"text": "📢 Subscribe to Channel", "url": CHANNEL_LINK},
                    {"text": "✅ I've Subscribed", "callback_data": "check_sub"},
                ])
                rows.append([{"text": "🐦 Follow on X", "url": X_LINK}])
            else:
                rows.append([
                    {"text": "📢 Subscribe to Channel", "url": CHANNEL_LINK},
                    {"text": "✅ I've Subscribed", "callback_data": "check_sub"},
                ])

    return rows


def format_outcomes_visual(state: dict, asset_key: str) -> str:
    s = state[asset_key]
    a = ASSETS[asset_key]
    outcomes = s.get("outcomes", [])[-30:]
    dots = "".join("🟢" if o == "Up" else "🔴" for o in outcomes)
    streak = s["streak"]
    direction = s["direction"] or "—"
    arrow = "⬆️" if direction == "Up" else ("⬇️" if direction == "Down" else "")
    return (
        f"<b>{a['short']} — Last {len(outcomes)} outcomes</b>\n\n"
        f"{dots}\n\n"
        f"Current streak: <b>{streak}</b> {direction} {arrow}"
    )


# ─── Main Loop ────────────────────────────────────────────────────────────────

def run():
    db.startup()
    log.info("🤖 Streak Bot v4.0 (SQLite)")
    log.info("   Waiting for users...")

    offset = None

    while True:
        try:
            updates = tg_get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1

                # ── Rate limit check ──
                _uid = str(
                    update.get("callback_query", {}).get("from", {}).get("id")
                    or update.get("message", {}).get("from", {}).get("id", "0")
                )
                if is_rate_limited(_uid):
                    continue

                # ── Callback query ──
                callback = update.get("callback_query")
                if callback:
                    cb_data = callback.get("data", "")
                    cb_user_id = str(callback["from"]["id"])
                    cb_chat_id = str(callback["message"]["chat"]["id"])
                    cb_msg_id = callback["message"]["message_id"]

                    prefs = db.get_user_prefs(cb_chat_id)

                    # Toggle alerts pause (MUST be before toggle_ asset check!)
                    if cb_data == "toggle_pause":
                        paused = db.toggle_alerts_paused(cb_chat_id)
                        if paused:
                            tg_answer_callback(callback["id"], "⏸ Alerts paused")
                            log.info(f"⏸ {cb_user_id}: paused alerts")
                        else:
                            tg_answer_callback(callback["id"], "▶️ Alerts resumed!")
                            log.info(f"▶️ {cb_user_id}: resumed alerts")
                        prefs = db.get_user_prefs(cb_chat_id)
                        tg_edit(cb_chat_id, cb_msg_id,
                                build_settings_text(prefs, cb_chat_id),
                                build_settings_buttons(prefs, cb_chat_id))
                        continue

                    # Toggle asset
                    if cb_data.startswith("toggle_"):
                        asset = cb_data[7:]
                        if asset in ASSETS:
                            coins = prefs["coins"]
                            enabled_count = sum(1 for c in coins.values() if c["enabled"])
                            if coins[asset]["enabled"]:
                                if enabled_count > 1:
                                    db.set_coin_pref(cb_chat_id, asset, enabled=False)
                                    tg_answer_callback(callback["id"])
                                    log.info(f"⚙️ {cb_user_id}: disabled {asset.upper()}")
                                else:
                                    tg_answer_callback(callback["id"], "Need at least 1 coin!")
                                    continue
                            else:
                                db.set_coin_pref(cb_chat_id, asset, enabled=True)
                                tg_answer_callback(callback["id"])
                                log.info(f"⚙️ {cb_user_id}: enabled {asset.upper()}")
                            prefs = db.get_user_prefs(cb_chat_id)
                            tg_edit(cb_chat_id, cb_msg_id,
                                    build_settings_text(prefs, cb_chat_id),
                                    build_settings_buttons(prefs, cb_chat_id))
                        continue

                    # Set streak for all coins
                    if cb_data == "streak_all":
                        # Evict oldest if at capacity
                        if len(waiting_for_streak) >= MAX_WAITING and cb_chat_id not in waiting_for_streak:
                            oldest_k = min(waiting_for_streak, key=lambda k: waiting_for_streak[k][1])
                            del waiting_for_streak[oldest_k]
                        waiting_for_streak[cb_chat_id] = ("_all", time.time())
                        tg_answer_callback(callback["id"])
                        tg_send(cb_chat_id,
                            f"🔢 Enter min streak for <b>ALL</b> coins (≥ {MIN_STREAK_MIN}):")
                        log.info(f"⚙️ {cb_user_id}: setting streak for ALL")
                        continue

                    # Set streak for specific coin
                    if cb_data.startswith("streak_"):
                        asset = cb_data[7:]
                        if asset in ASSETS:
                            if len(waiting_for_streak) >= MAX_WAITING and cb_chat_id not in waiting_for_streak:
                                oldest_k = min(waiting_for_streak, key=lambda k: waiting_for_streak[k][1])
                                del waiting_for_streak[oldest_k]
                            waiting_for_streak[cb_chat_id] = (asset, time.time())
                            short = ASSETS[asset]["short"]
                            tg_answer_callback(callback["id"])
                            tg_send(cb_chat_id,
                                f"🔢 Enter min streak for <b>{short}</b> (any number ≥ {MIN_STREAK_MIN}):")
                            log.info(f"⚙️ {cb_user_id}: setting streak for {short}")
                        continue

                    # Check subscription callback
                    if cb_data == "check_sub":
                        is_sub = check_channel_subscription(cb_user_id)
                        db.set_subscriber(cb_chat_id, is_sub)
                        if is_sub:
                            tg_answer_callback(callback["id"], "✅ Subscription confirmed! Unlimited access activated.")
                        else:
                            tg_answer_callback(callback["id"], "❌ Subscription not found. Please subscribe first.")
                        prefs = db.get_user_prefs(cb_chat_id)
                        tg_edit(cb_chat_id, cb_msg_id,
                                build_settings_text(prefs, cb_chat_id),
                                build_settings_buttons(prefs, cb_chat_id))
                        continue

                    tg_answer_callback(callback["id"])
                    continue

                # ── Message ──
                msg = update.get("message")
                if not msg:
                    continue

                text = msg.get("text", "").strip()
                chat_id = str(msg["chat"]["id"])
                user_id = str(msg.get("from", {}).get("id", ""))
                username = msg.get("from", {}).get("username")

                # Waiting for streak number (with TTL check)
                if chat_id in waiting_for_streak:
                    asset_val, wait_ts = waiting_for_streak.pop(chat_id)
                    if time.time() - wait_ts <= WAITING_TTL:
                        try:
                            num = int(text.strip())
                            if num >= MIN_STREAK_MIN:
                                if asset_val == "_all":
                                    db.set_all_streaks(chat_id, num)
                                    prefs = db.get_user_prefs(chat_id)
                                    tg_send(chat_id,
                                        f"✅ All coins min streak set to <b>{num}</b>",
                                        buttons=build_settings_buttons(prefs))
                                    log.info(f"⚙️ {user_id}: ALL min_streak={num}")
                                else:
                                    short = ASSETS.get(asset_val, {}).get("short", asset_val.upper())
                                    db.set_coin_pref(chat_id, asset_val, min_streak=num)
                                    prefs = db.get_user_prefs(chat_id)
                                    tg_send(chat_id,
                                        f"✅ {short} min streak set to <b>{num}</b>",
                                        buttons=build_settings_buttons(prefs))
                                    log.info(f"⚙️ {user_id}: {short} min_streak={num}")
                            else:
                                tg_send(chat_id,
                                    f"❌ Min value is {MIN_STREAK_MIN}.")
                        except ValueError:
                            tg_send(chat_id, "❌ That's not a number. Try /settings again.")
                        continue
                    # expired — fall through to normal command processing

                text_lower = text.lower()

                if text_lower == "/start":
                    is_new = db.register_user(chat_id, username)
                    # Check subscription immediately
                    is_sub = check_channel_subscription(user_id)
                    db.set_subscriber(chat_id, is_sub)

                    # 1. Welcome + explanation
                    tg_send(chat_id,
                        "<b>Polymarket Streak Tracker</b>\n\n"

                        "This bot monitors Polymarket 5-minute binary markets "
                        "for BTC, ETH, SOL and XRP in real time.\n\n"

                        "Every 5 minutes, each coin resolves as Up or Down. "
                        "When the same outcome repeats multiple times in a row — "
                        "that's a streak. Long streaks are statistically rare "
                        "and can signal opportunities for mean-reversion "
                        "or martingale-style strategies.\n\n"

                        "The bot counts consecutive outcomes per coin and "
                        "sends you a notification the moment a streak reaches "
                        "the threshold you set. You decide which coins matter "
                        "and how long a streak should be before you hear about it.",
                    )

                    # 2. What to do next
                    prefs = db.get_user_prefs(chat_id)
                    tg_send(chat_id,
                        "<b>Get started</b>\n\n"

                        "Right now you're set up to receive alerts for all 4 coins "
                        "when a streak hits 8 or more. You can change this anytime.\n\n"

                        "<b>/settings</b> — open your preferences. "
                        "Toggle coins on or off, and set a custom streak "
                        "threshold for each one individually. Tap a coin "
                        "button to enable or disable it. Tap the streak "
                        "number to type in your own value — any number works.\n\n"

                        "<b>/status</b> — see all coins at once: current streak "
                        "count and direction (Up or Down) in one compact view.\n\n"

                        "<b>/btc /eth /sol /xrp</b> — check a specific coin "
                        "in detail: last 30 outcomes and current streak.\n\n"

                        "That's it. Configure your settings below and the bot "
                        "will do the rest.",
                        buttons=build_settings_buttons(prefs, chat_id),
                    )

                    # 3. Links
                    tg_send(chat_id,
                        f"<a href=\"{X_LINK}\">X (Twitter)</a> · "
                        "<a href=\"https://github.com/poly-series-alerts\">GitHub</a>\n\n"
                        "<i>Not financial advice. Trade at your own risk.</i>",
                    )
                    if is_new:
                        log.info(f"✅ New user: {user_id}")
                    else:
                        log.info(f"✅ /start: {user_id}")
                    continue

                if text_lower == "/settings":
                    db.register_user(chat_id, username)
                    prefs = db.get_user_prefs(chat_id)
                    tg_send(chat_id,
                        build_settings_text(prefs, chat_id),
                        buttons=build_settings_buttons(prefs, chat_id))
                    log.info(f"⚙️ Settings: {user_id}")
                    continue

                if text_lower == "/status":
                    db.register_user(chat_id, username)
                    if not check_and_gate(chat_id, user_id):
                        continue
                    state = db.load_state()
                    lines = []
                    for key in ASSETS:
                        s = state[key]
                        short = ASSETS[key]["short"]
                        streak = s.get("streak", 0)
                        direction = s.get("direction")
                        dot = "🟢" if direction == "Up" else ("🔴" if direction == "Down" else "⚪")
                        lines.append(f"{dot} {short}  <b>{streak}</b> {direction or '—'}")
                    tg_send(chat_id, "\n".join(lines))
                    log.info(f"Command /status from {user_id}")
                    continue

                if text_lower in COMMANDS:
                    db.register_user(chat_id, username)
                    if not check_and_gate(chat_id, user_id):
                        continue
                    state = db.load_state()
                    tg_send(chat_id, format_outcomes_visual(state, COMMANDS[text_lower]))
                    log.info(f"Command {text_lower} from {user_id}")
                    continue

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error(f"Error: {e}")
            import time
            time.sleep(1)


if __name__ == "__main__":
    run()
