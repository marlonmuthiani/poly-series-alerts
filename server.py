#!/usr/bin/env python3
"""
Streak Server v4.0 — Polymarket Data Engine (SQLite)
Monitors 5-min binary markets, detects streaks, writes state.
Pushes alerts to Telegram based on individual user preferences.
On restart: batch-loads recent outcomes via Gamma API (single request).
Deduplicates alerts so no repeated notifications after restart.
"""

import json
import os
import time
import logging
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import db

# ─── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("STREAK_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    _env = Path(__file__).parent.parent / ".env"
    if _env.exists():
        for _line in _env.read_text().splitlines():
            if _line.startswith("STREAK_BOT_TOKEN="):
                TELEGRAM_BOT_TOKEN = _line.split("=", 1)[1].strip()
                break
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("STREAK_BOT_TOKEN not set")
GAMMA_API = "https://gamma-api.polymarket.com"
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@yourchannel")

ASSETS = {
    "btc": {"name": "Bitcoin", "short": "BTC"},
    "eth": {"name": "Ethereum", "short": "ETH"},
    "sol": {"name": "Solana", "short": "SOL"},
    "xrp": {"name": "XRP", "short": "XRP"},
}

GLOBAL_MIN_STREAK = 5
POLL_INTERVAL = 10
LOOKBACK_PER_ASSET = 30
ENV_FILE = Path(__file__).parent.parent / ".env"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")

# ─── Proxy ────────────────────────────────────────────────────────────────────

def load_proxy():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            for key in ("PROXY_TURKEY", "PROXY_RUSSIA"):
                if line.strip().startswith(f"{key}="):
                    val = line.strip().split("=", 1)[1].strip()
                    if val:
                        log.info(f"Proxy loaded ({key})")
                        return {"https": val, "http": val}
    env_proxy = os.environ.get("HTTPS_PROXY")
    if env_proxy:
        return {"https": env_proxy, "http": env_proxy}
    return None

PROXIES = load_proxy()

http = requests.Session()
if PROXIES:
    http.proxies.update(PROXIES)
http.headers["User-Agent"] = "StreakTracker/4.0"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_slug(asset: str, epoch: int) -> str:
    return f"{asset}-updown-5m-{epoch}"

def current_epoch() -> int:
    return (int(time.time()) // 300) * 300

def market_link(slug: str) -> str:
    return f"https://polymarket.com/event/{slug}"

# ─── Gamma API ────────────────────────────────────────────────────────────────

def fetch_market(slug: str) -> dict | None:
    for attempt in range(3):
        try:
            resp = http.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=15)
            if resp.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            event = data[0]
            markets = event.get("markets", [])
            if not markets:
                return None
            m = markets[0]
            raw_prices = m.get("outcomePrices", "[]")
            raw_outcomes = m.get("outcomes", "[]")
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
            return {
                "slug": slug,
                "title": event.get("title", ""),
                "status": m.get("umaResolutionStatus"),
                "outcome_prices": prices,
                "outcomes": outcomes,
            }
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                log.error(f"Gamma error for {slug}: {e}")
    return None

def fetch_batch_resolved(limit: int = 100) -> list:
    for attempt in range(3):
        try:
            resp = http.get(f"{GAMMA_API}/events", params={
                "tag_slug": "5M",
                "closed": "true",
                "limit": limit,
                "order": "endDate",
                "ascending": "false",
            }, timeout=30)
            if resp.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            resp.raise_for_status()
            events = resp.json()
            results = []
            for event in events:
                slug = event.get("slug", "")
                markets = event.get("markets", [])
                if not markets:
                    continue
                m = markets[0]
                raw_prices = m.get("outcomePrices", "[]")
                raw_outcomes = m.get("outcomes", "[]")
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                status = m.get("umaResolutionStatus")
                results.append({
                    "slug": slug,
                    "status": status,
                    "outcome_prices": prices,
                    "outcomes": outcomes,
                })
            return results
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                log.error(f"Batch fetch error: {e}")
    return []

def get_winner(market: dict) -> str | None:
    if market.get("status") != "resolved":
        return None
    prices = market["outcome_prices"]
    outcomes = market["outcomes"]
    for i, price in enumerate(prices):
        if str(price) == "1" and i < len(outcomes):
            return outcomes[i]
    return None

def parse_slug(slug: str) -> tuple:
    parts = slug.split("-updown-5m-")
    if len(parts) == 2:
        try:
            return parts[0], int(parts[1])
        except ValueError:
            pass
    return None, None

# ─── Telegram (alerts only) ──────────────────────────────────────────────────

def tg_send_to(chat_id: str, text: str, buttons=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.ok:
                return True
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                log.warning(f"TG 429 ({chat_id}): retry after {retry_after}s")
                time.sleep(min(retry_after, 30))
                continue
            if resp.status_code == 403:
                log.info(f"TG 403 ({chat_id}): user blocked bot, skipping")
                return False  # don't retry blocked users
            log.warning(f"TG send fail ({chat_id}): {resp.status_code}")
        except Exception as e:
            log.warning(f"TG error ({chat_id}): {e}")
        time.sleep(2)
    return False

_tg_pool = ThreadPoolExecutor(max_workers=10)


def send_streak_alert(asset_key: str, streak: int, direction: str,
                       current_slug: str, next_slug: str) -> int:
    """Send alert to matching users. Returns count of successful sends."""
    a = ASSETS[asset_key]
    dot = "🟢" if direction == "Up" else "🔴"

    fire_count = min(streak - 7, 3)
    fires = "🔥" * max(fire_count, 1)
    if streak >= 11:
        fires += "🚨"

    pad = "\u2007" * 20
    text = f"<b>Streak {streak} — {dot} {a['short']}\n{fires}</b>{pad}"

    row = [{"text": "⏳ Current", "url": market_link(current_slug)}]
    if next_slug:
        row.append({"text": "➡️ Next", "url": market_link(next_slug)})
    buttons = [row]

    # Efficient: single SQL query for matching users
    chat_ids = db.get_all_users_for_alert(asset_key, streak)
    if not chat_ids:
        log.info(f"🔔 ALERT: {a['short']} streak={streak} {direction} → no matching users")
        return 0

    # Parallel sends via thread pool (non-blocking for 1K+ users)
    futures = {_tg_pool.submit(tg_send_to, cid, text, buttons): cid for cid in chat_ids}
    sent_count = 0
    for future in futures:
        try:
            if future.result(timeout=30):
                sent_count += 1
        except Exception:
            pass

    log.info(f"🔔 ALERT: {a['short']} streak={streak} {direction} → sent to {sent_count}/{len(chat_ids)} users")
    return sent_count

# ─── Alert Deduplication ─────────────────────────────────────────────────────

def should_alert(asset_key: str, streak: int, direction: str, last_epoch: int, alerts: dict) -> bool:
    prev = alerts.get(asset_key)
    if not prev:
        return True
    if prev.get("direction") != direction:
        return True
    if streak > prev.get("streak", 0):
        return True
    prev_epoch = prev.get("last_epoch", 0)
    streak_start_epoch = last_epoch - (streak - 1) * 300
    prev_streak_start = prev_epoch - (prev.get("streak", 1) - 1) * 300
    if streak_start_epoch != prev_streak_start:
        return True
    return False

def record_alert(asset_key: str, streak: int, direction: str, last_epoch: int, alerts: dict):
    data = {
        "streak": streak,
        "direction": direction,
        "last_epoch": last_epoch,
        "timestamp": int(time.time()),
    }
    alerts[asset_key] = data
    db.save_alert(asset_key, data)

# ─── Engine ───────────────────────────────────────────────────────────────────

class StreakServer:
    def __init__(self):
        db.startup()
        self.state = db.load_state()
        self.alerts = db.load_alerts()
        self.processed = {a: set() for a in ASSETS}
        for a in ASSETS:
            s = self.state[a]
            if s.get("processed_epochs"):
                self.processed[a] = set(s["processed_epochs"])

    def _compute_streak(self, outcomes: list) -> tuple:
        if not outcomes:
            return 0, None
        direction = outcomes[-1]
        streak = 0
        for o in reversed(outcomes):
            if o == direction:
                streak += 1
            else:
                break
        return streak, direction

    def _save_state(self):
        db.save_full_state(self.state)

    def initialize(self):
        now_epoch = current_epoch()

        has_fresh_state = False
        for asset_key in ASSETS:
            s = self.state[asset_key]
            if s.get("outcomes") and s.get("last_epoch", 0) > now_epoch - 3600:
                has_fresh_state = True
                break

        if has_fresh_state:
            log.info("State found in DB, fetching gap from Gamma API (batch)...")
            self._init_from_state_with_gap()
        else:
            log.info("No fresh state, batch-loading from Gamma API...")
            self._init_from_batch()

        self._save_state()
        log.info("Initialization complete.")

    def _init_from_batch(self):
        batch = fetch_batch_resolved(limit=LOOKBACK_PER_ASSET * len(ASSETS) + 20)
        if not batch:
            log.warning("Batch fetch returned nothing!")
            return

        by_asset = {a: [] for a in ASSETS}
        for item in batch:
            asset, epoch = parse_slug(item["slug"])
            if asset not in ASSETS:
                continue
            winner = get_winner(item)
            if winner:
                by_asset[asset].append((epoch, winner, item["outcome_prices"]))

        for asset_key in ASSETS:
            short = ASSETS[asset_key]["short"]
            results = by_asset[asset_key]
            if not results:
                log.info(f"  {short}: no resolved markets found")
                continue
            results.sort(key=lambda x: x[0])
            results = results[-LOOKBACK_PER_ASSET:]
            outcomes = [w for _, w, _ in results]
            processed = [e for e, _, _ in results]
            streak, direction = self._compute_streak(outcomes)

            self.state[asset_key] = {
                "streak": streak,
                "direction": direction,
                "last_epoch": results[-1][0],
                "last_prices": results[-1][2],
                "outcomes": outcomes,
                "processed_epochs": processed,
            }
            self.processed[asset_key] = set(processed)
            log.info(f"  {short}: {len(results)} outcomes, streak={streak} {direction}")
            self._check_post_restore_alert(asset_key)

    def _init_from_state_with_gap(self):
        max_last = max(self.state[a].get("last_epoch", 0) for a in ASSETS)
        now_epoch = current_epoch()

        if max_last >= now_epoch - 300:
            for asset_key in ASSETS:
                s = self.state[asset_key]
                outcomes = s.get("outcomes", [])
                streak, direction = self._compute_streak(outcomes)
                s["streak"] = streak
                s["direction"] = direction
                log.info(f"  {ASSETS[asset_key]['short']}: up-to-date, streak={streak} {direction}")
                self._check_post_restore_alert(asset_key)
            return

        gap_size = (now_epoch - max_last) // 300
        fetch_limit = min(gap_size * len(ASSETS) + 10, 100)
        log.info(f"  Gap: ~{gap_size} epochs, fetching {fetch_limit} events...")
        batch = fetch_batch_resolved(limit=fetch_limit)

        # Collect gap events per asset, then sort chronologically before appending
        gap_events = {a: [] for a in ASSETS}
        for item in batch:
            asset, epoch = parse_slug(item["slug"])
            if asset not in ASSETS:
                continue
            if epoch in self.processed.get(asset, set()):
                continue
            winner = get_winner(item)
            if not winner:
                continue
            gap_events[asset].append((epoch, winner, item["outcome_prices"]))

        for asset in ASSETS:
            gap_events[asset].sort(key=lambda x: x[0])  # chronological order!
            s = self.state[asset]
            for epoch, winner, prices in gap_events[asset]:
                s["outcomes"].append(winner)
                s["outcomes"] = s["outcomes"][-LOOKBACK_PER_ASSET:]
                self.processed[asset].add(epoch)
                s["processed_epochs"] = sorted(self.processed[asset])[-LOOKBACK_PER_ASSET:]
                if epoch > s.get("last_epoch", 0):
                    s["last_epoch"] = epoch
                    s["last_prices"] = prices

        for asset_key in ASSETS:
            s = self.state[asset_key]
            outcomes = s.get("outcomes", [])
            streak, direction = self._compute_streak(outcomes)
            s["streak"] = streak
            s["direction"] = direction
            short = ASSETS[asset_key]["short"]
            log.info(f"  {short}: streak={streak} {direction}, {len(outcomes)} outcomes")
            self._check_post_restore_alert(asset_key)

    def _check_post_restore_alert(self, asset_key: str):
        s = self.state[asset_key]
        streak = s["streak"]
        direction = s["direction"]
        short = ASSETS[asset_key]["short"]
        if streak >= GLOBAL_MIN_STREAK:
            if should_alert(asset_key, streak, direction, s["last_epoch"], self.alerts):
                log.info(f"  {short}: 🔔 Post-restore alert! streak={streak} {direction}")
                now_ep = current_epoch()
                current_slug = make_slug(asset_key, now_ep)
                next_slug = make_slug(asset_key, now_ep + 300)
                sent = send_streak_alert(asset_key, streak, direction, current_slug, next_slug)
                if sent > 0:
                    record_alert(asset_key, streak, direction, s["last_epoch"], self.alerts)
                else:
                    log.warning(f"  {short}: alert send failed — will retry next cycle")
            else:
                log.info(f"  {short}: streak={streak} >= threshold but alert already sent")

    def _update(self, asset_key: str, epoch: int, winner: str, prices: list) -> bool:
        s = self.state[asset_key]
        self.processed[asset_key].add(epoch)
        sorted_epochs = sorted(self.processed[asset_key])[-LOOKBACK_PER_ASSET:]
        s["processed_epochs"] = sorted_epochs
        if winner == s["direction"]:
            s["streak"] += 1
        else:
            s["streak"] = 1
            s["direction"] = winner
        s["last_epoch"] = epoch
        s["last_prices"] = prices if isinstance(prices, list) else []
        s["outcomes"].append(winner)
        s["outcomes"] = s["outcomes"][-LOOKBACK_PER_ASSET:]
        return s["streak"] >= GLOBAL_MIN_STREAK

    def recheck_subscriptions(self):
        """Periodic recheck: verify subscribers are still subscribed."""
        stale = db.get_stale_subscribers(max_age_seconds=86400)
        if not stale:
            return
        log.info(f"♻️ Rechecking {len(stale)} subscriptions...")
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChatMember"
        checked = 0
        for chat_id in stale:
            try:
                resp = requests.get(url, params={
                    "chat_id": CHANNEL_USERNAME, "user_id": chat_id
                }, timeout=5)
                if resp.ok:
                    status = resp.json().get("result", {}).get("status", "left")
                    is_sub = status in ("member", "administrator", "creator")
                    db.set_subscriber(chat_id, is_sub)
                    checked += 1
                    if not is_sub:
                        log.info(f"  ❌ {chat_id} unsubscribed")
            except Exception:
                pass
            time.sleep(0.2)  # rate limit
        log.info(f"♻️ Rechecked {checked}/{len(stale)} subscriptions")

    def check_asset(self, asset_key: str):
        now_epoch = current_epoch()
        for offset in range(4):
            epoch = now_epoch - (offset + 1) * 300
            if epoch in self.processed[asset_key]:
                continue
            slug = make_slug(asset_key, epoch)
            market = fetch_market(slug)
            if not market:
                continue
            winner = get_winner(market)
            if not winner:
                continue
            alert = self._update(asset_key, epoch, winner, market["outcome_prices"])
            self._save_state()
            s = self.state[asset_key]
            short = ASSETS[asset_key]["short"]
            log.info(f"✅ {short}: {winner} | streak={s['streak']} | epoch={epoch}")
            if alert:
                if should_alert(asset_key, s["streak"], s["direction"], s["last_epoch"], self.alerts):
                    now_ep = current_epoch()
                    current_slug = make_slug(asset_key, now_ep)
                    next_slug = make_slug(asset_key, now_ep + 300)
                    sent = send_streak_alert(asset_key, s["streak"], s["direction"],
                                             current_slug, next_slug)
                    if sent > 0:
                        record_alert(asset_key, s["streak"], s["direction"], s["last_epoch"], self.alerts)

    def run(self):
        log.info("=" * 60)
        log.info("🟢 Streak Server v4.0 (SQLite + batch init + individual alerts)")
        log.info(f"   Assets: {', '.join(ASSETS[a]['short'] for a in ASSETS)}")
        log.info(f"   Global min streak: {GLOBAL_MIN_STREAK}")
        log.info(f"   Lookback: {LOOKBACK_PER_ASSET} per asset")
        log.info(f"   Poll interval: {POLL_INTERVAL}s")
        log.info("=" * 60)
        self.initialize()
        log.info("=" * 60)
        log.info("Monitoring markets...")
        cycle = 0
        while True:
            try:
                cycle += 1
                for asset_key in ASSETS:
                    self.check_asset(asset_key)
                    time.sleep(1)
                # Periodic subscription recheck (~every 6h at 10s interval)
                if cycle % 2160 == 0:
                    self.recheck_subscriptions()
                if cycle % 10 == 0:
                    for ak in ASSETS:
                        s = self.state[ak]
                        log.info(f"📊 {ASSETS[ak]['short']}: streak={s['streak']} {s['direction'] or '—'}")
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                log.info("Shutting down server...")
                self._save_state()
                break
            except Exception as e:
                log.error(f"Error: {e}")
                time.sleep(5)


if __name__ == "__main__":
    StreakServer().run()
