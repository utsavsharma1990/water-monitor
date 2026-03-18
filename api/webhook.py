"""
api/webhook.py — Vercel Serverless Function: Telegram Bot Webhook
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Receives POST requests from Telegram, generates replies using bot_core,
and sends them back. Deployed automatically by Vercel.

Environment variables required in Vercel project settings:
  TELEGRAM_BOT_TOKEN   — your bot token from @BotFather
  TELEGRAM_CHAT_ID     — your personal Telegram chat ID
  GH_TOKEN             — GitHub personal access token (read repo)
  GH_OWNER             — GitHub username  (e.g. utsavsharma)
  GH_REPO              — GitHub repo name (e.g. water-monitor)
  GH_BRANCH            — branch name, default "main"
"""

import json, os, sys
from http.server import BaseHTTPRequestHandler

# Ensure the repo root is on the Python path so we can import bot_core
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot_core


def _load_data():
    """Fetch water_data.json from GitHub using the repo API."""
    token  = os.environ.get("GH_TOKEN", "")
    owner  = os.environ.get("GH_OWNER", "")
    repo   = os.environ.get("GH_REPO",  "")
    branch = os.environ.get("GH_BRANCH", "main")

    if not (token and owner and repo):
        raise RuntimeError("GH_TOKEN / GH_OWNER / GH_REPO env vars not set.")

    return bot_core.load_from_github(owner, repo, token, "water_data.json", branch)


def _handle_update(update):
    """Process one Telegram update object and send a reply."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = str(os.environ.get("TELEGRAM_CHAT_ID", ""))

    if not bot_token:
        print("TELEGRAM_BOT_TOKEN not set")
        return

    msg     = update.get("message", {})
    from_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip()

    if not text:
        return  # Ignore non-text messages (stickers, photos, etc.)

    print(f"[webhook] [{from_id}] {text[:80]}")

    # Restrict to your chat ID
    if chat_id and from_id != chat_id:
        bot_core.send_telegram(bot_token, from_id, "⛔ Unauthorized.")
        return

    # Load data and compute insights
    try:
        data = _load_data()
    except Exception as e:
        bot_core.send_telegram(bot_token, from_id,
            f"⚠️ Could not load water data: {e}\n\n"
            "Make sure the GitHub environment variables are configured in Vercel.")
        return

    ins   = bot_core.insights(data)
    reply = bot_core.generate_reply(text, ins, data)
    bot_core.send_telegram(bot_token, from_id, reply)


# ─── Vercel handler ───────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Suppress default HTTP log noise in Vercel logs."""
        print(format % args)

    def do_POST(self):
        try:
            length   = int(self.headers.get("Content-Length", 0))
            body     = self.rfile.read(length)
            update   = json.loads(body)
            _handle_update(update)
        except Exception as e:
            print(f"[webhook] ERROR: {e}")

        # Always return 200 OK — Telegram will retry if we return non-200
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        """Health check endpoint — visit /api/webhook to confirm it's live."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Water Monitor Bot webhook is running.")
