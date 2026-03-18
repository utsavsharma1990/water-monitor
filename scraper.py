#!/usr/bin/env python3
"""
scraper.py — Veolia Water Usage Auto-Scraper for GitHub Actions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reads environment variables, attempts to scrape yesterday's usage
from the Veolia US portal, updates water_data.json, and sends a
Telegram notification.

Environment variables (set as GitHub Secrets):
  VEOLIA_EMAIL          — your Veolia login email
  VEOLIA_PASSWORD       — your Veolia password
  TELEGRAM_BOT_TOKEN    — Telegram bot token
  TELEGRAM_CHAT_ID      — your Telegram chat ID
  MANUAL_DATE           — (optional) override date YYYY-MM-DD
  MANUAL_USAGE          — (optional) override usage gallons, skips scraping
"""

import os, json, sys, time, traceback
from datetime import date, timedelta
from pathlib import Path

# ─── Import analytics module ──────────────────────────────────────────────────
import bot_core

# ─── Config from environment ──────────────────────────────────────────────────
VEOLIA_EMAIL    = os.environ.get("VEOLIA_EMAIL", "")
VEOLIA_PASSWORD = os.environ.get("VEOLIA_PASSWORD", "")
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")
MANUAL_DATE     = os.environ.get("MANUAL_DATE", "").strip()
MANUAL_USAGE    = os.environ.get("MANUAL_USAGE", "").strip()

DATA_FILE = Path("water_data.json")


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    # Bootstrap empty structure
    return {"config": {"threshold_percent": 20}, "readings": [], "hourly": {}}


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2))
    print(f"Saved {DATA_FILE}")


# ─── Veolia scraper ───────────────────────────────────────────────────────────

def scrape_veolia(target_date_str):
    """
    Attempt to log in to the Veolia US portal and retrieve yesterday's usage.
    Returns float (gallons) or None on failure.

    The Veolia portal (myveolia.us) is a React single-page app.
    We use requests + BeautifulSoup for a lightweight attempt first,
    then fall back to a Telegram prompt asking for manual entry.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        print("requests/bs4 not available")
        return None

    if not VEOLIA_EMAIL or not VEOLIA_PASSWORD:
        print("Veolia credentials not set — skipping scrape")
        return None

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    try:
        # Step 1: Get login page to collect CSRF / session cookies
        base = "https://www.myveolia.us"
        r = session.get(f"{base}/en/home", timeout=15)
        r.raise_for_status()

        # Step 2: Attempt API login (common Veolia endpoint pattern)
        login_payload = {
            "username": VEOLIA_EMAIL,
            "password": VEOLIA_PASSWORD,
            "rememberMe": False,
        }
        r2 = session.post(
            f"{base}/api/v1/auth/login",
            json=login_payload,
            timeout=15
        )
        if r2.status_code not in (200, 201):
            print(f"Login returned {r2.status_code} — portal may have changed")
            return None

        # Step 3: Request usage data for target date
        r3 = session.get(
            f"{base}/api/v1/consumption/daily",
            params={"date": target_date_str},
            timeout=15
        )
        if r3.status_code != 200:
            print(f"Usage API returned {r3.status_code}")
            return None

        payload = r3.json()
        # Try common response shapes
        usage = (
            payload.get("gallons") or
            payload.get("usage") or
            payload.get("consumption") or
            (payload.get("data") or {}).get("gallons")
        )
        if usage is not None:
            return float(usage)

        print(f"Unexpected response shape: {list(payload.keys())}")
        return None

    except Exception as e:
        print(f"Scrape error: {e}")
        return None


def ask_via_telegram(target_date_str):
    """
    Send a Telegram message asking Utsav to reply with today's usage.
    Returns None (we can't wait for a reply in a GitHub Actions job).
    The next bot interaction will handle the manual entry.
    """
    if BOT_TOKEN and CHAT_ID:
        msg = (
            f"💧 <b>Water Monitor — Manual Entry Needed</b>\n\n"
            f"Could not auto-scrape usage for <b>{target_date_str}</b>.\n\n"
            f"Please open the Veolia app, find yesterday's usage, then reply:\n"
            f"<code>/add {target_date_str} &lt;gallons&gt;</code>\n\n"
            f"Example: <code>/add {target_date_str} 215.5</code>"
        )
        bot_core.send_telegram(BOT_TOKEN, CHAT_ID, msg)
        print("Sent Telegram prompt for manual entry")
    return None


# ─── Notification ─────────────────────────────────────────────────────────────

def send_daily_alert(data, target_date_str, usage):
    """Send a Telegram alert about yesterday's usage."""
    if not BOT_TOKEN or not CHAT_ID:
        return

    ins = bot_core.insights(data)
    if not ins:
        return

    readings   = bot_core.daily_sorted(data)
    avg        = bot_core.rolling_avg(readings, target_date_str) or ins["avg_all"]
    thr_pct    = ins["thr_pct"]
    threshold  = avg * (1 + thr_pct / 100)
    is_spike   = usage > threshold
    diff_pct   = (usage - avg) / avg * 100

    if is_spike:
        msg = (
            f"🚨 <b>Water Usage Alert — {bot_core.fdate(target_date_str)}</b>\n\n"
            f"💧 Usage: <b>{bot_core.fnum(usage)} gal</b>\n"
            f"📊 30-day avg: {bot_core.fnum(avg)} gal\n"
            f"⬆️ That's <b>{diff_pct:+.1f}%</b> above average!\n\n"
            f"⚠️ This exceeds your +{thr_pct}% alert threshold.\n"
            f"Check for leaks: /leakcheck"
        )
    else:
        msg = (
            f"✅ <b>Daily Water Update — {bot_core.fdate(target_date_str)}</b>\n\n"
            f"💧 Usage: <b>{bot_core.fnum(usage)} gal</b>\n"
            f"📊 30-day avg: {bot_core.fnum(avg)} gal\n"
            f"{'⬆️' if diff_pct > 0 else '⬇️'} {diff_pct:+.1f}% vs average — normal range."
        )

    bot_core.send_telegram(BOT_TOKEN, CHAT_ID, msg)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Determine target date
    if MANUAL_DATE:
        target_date = MANUAL_DATE
        print(f"Manual date override: {target_date}")
    else:
        target_date = str(date.today() - timedelta(days=1))
        print(f"Auto target date (yesterday): {target_date}")

    # Load existing data
    data = load_data()
    print(f"Loaded {len(data.get('readings', []))} existing readings")

    # Check if we already have this date
    existing = next((r for r in data.get("readings", []) if r["date"] == target_date), None)
    if existing and not MANUAL_USAGE:
        print(f"Already have data for {target_date} ({existing['usage']} gal) — nothing to do.")
        sys.exit(0)

    # Determine usage
    usage = None

    if MANUAL_USAGE:
        try:
            usage = float(MANUAL_USAGE)
            print(f"Using manual usage: {usage} gal")
        except ValueError:
            print(f"Invalid MANUAL_USAGE value: {MANUAL_USAGE}")
            sys.exit(1)
    else:
        print("Attempting Veolia scrape…")
        usage = scrape_veolia(target_date)

    if usage is None:
        print("Scrape failed — requesting manual entry via Telegram")
        ask_via_telegram(target_date)
        # Exit 0 so the workflow doesn't fail (just no data today)
        sys.exit(0)

    # Update data
    is_new = bot_core.add_reading(data, target_date, usage)
    print(f"{'Added' if is_new else 'Updated'} reading: {target_date} = {usage} gal")

    # Update last_updated in config
    data.setdefault("config", {})["last_updated"] = target_date

    # Save to file
    save_data(data)

    # Send Telegram notification
    send_daily_alert(data, target_date, usage)

    print("Done!")


if __name__ == "__main__":
    main()
