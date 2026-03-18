"""
api/data.py — Vercel Serverless Function: Live Data Endpoint
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET /api/data  →  returns water_data.json as application/json

The dashboard (index.html) calls this endpoint on load so it
always shows the latest committed data from the GitHub repo.

Environment variables (same as webhook.py):
  GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH
"""

import json, os, sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot_core


class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(format % args)

    def do_GET(self):
        token  = os.environ.get("GH_TOKEN", "")
        owner  = os.environ.get("GH_OWNER", "")
        repo   = os.environ.get("GH_REPO",  "")
        branch = os.environ.get("GH_BRANCH", "main")

        if not (token and owner and repo):
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "GitHub environment variables not configured."
            }).encode())
            return

        try:
            data   = bot_core.load_from_github(owner, repo, token, "water_data.json", branch)
            body   = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-type",                "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control",               "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-type",                "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
