#!/usr/bin/env python3
"""
bot_core.py — Water Monitor Analytics Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Shared module for both the Vercel webhook and GitHub Actions scraper.
Contains all analytics, NLP intent classification, and Telegram reply builders.
No polling code — this is pure business logic.
"""

import json, re, urllib.request, urllib.parse, base64
from datetime import date, timedelta
from statistics import mean, stdev

# ─── Constants ────────────────────────────────────────────────────────────────
MONTHS = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
    'january':1,'february':2,'march':3,'april':4,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
}

# ─── Data Loading ─────────────────────────────────────────────────────────────

def load_from_github(owner, repo, token, path="water_data.json", branch="main"):
    """
    Load water_data.json from a private (or public) GitHub repo using the Contents API.
    Returns parsed dict, or raises RuntimeError on failure.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "water-monitor-bot/1.0",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        meta = json.loads(r.read())
    content = base64.b64decode(meta["content"]).decode("utf-8")
    return json.loads(content)


def load_from_raw_url(url):
    """Load water_data.json from a public GitHub raw URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "water-monitor-bot/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def load_from_file(path):
    """Load water_data.json from local filesystem."""
    with open(path) as f:
        return json.load(f)


def save_to_file(data, path):
    """Save data dict to a local JSON file."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def push_to_github(data, owner, repo, token, path="water_data.json", branch="main", message="chore: daily water data update"):
    """
    Commit updated water_data.json back to the GitHub repo.
    Overwrites the existing file (gets current SHA first).
    """
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "water-monitor-bot/1.0",
    }

    # Get current file SHA (required for update)
    req = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        meta = json.loads(r.read())
    sha = meta["sha"]

    # Push updated content
    content_b64 = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    payload = json.dumps({
        "message": message,
        "content": content_b64,
        "sha": sha,
        "branch": branch,
    }).encode()

    req2 = urllib.request.Request(api_url, data=payload, headers=headers, method="PUT")
    with urllib.request.urlopen(req2, timeout=10) as r:
        return json.loads(r.read())


# ─── Data helpers ──────────────────────────────────────────────────────────────

def daily_sorted(data):
    return sorted(data.get("readings", []), key=lambda r: r["date"])


def hourly_for(data, date_str):
    """Return list of 24 floats for a given date, or None."""
    return data.get("hourly", {}).get(date_str)


def rolling_avg(readings, before_date, window=30):
    prior = [r for r in readings if r["date"] < before_date]
    if not prior:
        return None
    return mean(r["usage"] for r in prior[-window:])


def add_reading(data, date_str, usage_gallons):
    """
    Append or update a daily reading in data['readings'].
    Returns True if it was a new entry, False if updated.
    """
    readings = data.setdefault("readings", [])
    for r in readings:
        if r["date"] == date_str:
            r["usage"] = round(float(usage_gallons), 1)
            return False
    readings.append({"date": date_str, "usage": round(float(usage_gallons), 1)})
    return True


# ─── Formatting helpers ────────────────────────────────────────────────────────

def fdate(s):
    """2026-03-14  →  Mar 14"""
    try:
        y, m, d = s.split('-')
        mn = ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        return f"{mn[int(m)]} {int(d)}"
    except Exception:
        return s


def fnum(n, decimals=1):
    return f"{n:.{decimals}f}" if n is not None else "—"


def spike_tag(usage, avg, pct):
    if avg is None:
        return ""
    diff = (usage - avg) / avg * 100
    if diff > 50:  return " 🚨🚨"
    if diff > 20:  return " 🚨"
    if diff > 10:  return " ⚠️"
    if diff < -10: return " ✅"
    return ""


# ─── Leak detection ───────────────────────────────────────────────────────────

def leak_score(hours_arr):
    """
    Estimate leak likelihood from hourly pattern.
    Returns (score 0-100, label, explanation).
    A leak shows water running uniformly across all hours, especially midnight–6am.
    """
    if not hours_arr:
        return None, "Unknown", "No hourly data"
    active = sum(1 for h in hours_arr if h > 0)
    night  = sum(1 for h in hours_arr[0:6] if h > 0)   # midnight–6am
    total  = sum(hours_arr)
    # Night usage ratio (weighted 60%)
    night_ratio  = night / 6
    # Active hour ratio (weighted 40%)
    active_ratio = active / 24
    score = int(night_ratio * 60 + active_ratio * 40)
    if score >= 75:
        label, tip = "High leak risk",   "Water running most of the night — strong leak indicator."
    elif score >= 50:
        label, tip = "Possible leak",    "Significant overnight usage — worth checking pipes."
    elif score >= 25:
        label, tip = "Monitor closely",  "Some overnight usage — could be normal or minor drip."
    else:
        label, tip = "Normal pattern",   "Usage concentrated in daytime hours — no leak detected."
    return score, label, tip


# ─── Analytics ────────────────────────────────────────────────────────────────

def insights(data):
    readings = daily_sorted(data)
    if not readings:
        return None
    cfg     = data.get("config", {})
    tpct    = cfg.get("threshold_percent", 20)
    usgs    = [r["usage"] for r in readings]
    last    = readings[-1]
    avg_all = mean(usgs)
    avg_30  = rolling_avg(readings, "9999") or avg_all
    std_all = stdev(usgs) if len(usgs) > 1 else 0
    thr_gal = avg_30 * (1 + tpct / 100)

    spikes = []
    for r in readings:
        avg = rolling_avg(readings, r["date"]) or avg_all
        if r["usage"] > avg * (1 + tpct / 100):
            spikes.append({**r, "avg": avg, "pct": (r["usage"] - avg) / avg * 100})

    trend = None
    if len(readings) >= 14:
        r7  = mean(r["usage"] for r in readings[-7:])
        p7  = mean(r["usage"] for r in readings[-14:-7])
        pct = (r7 - p7) / p7 * 100
        trend = ("rising" if pct > 10 else "falling" if pct < -10 else "stable", pct, r7, p7)

    return dict(
        readings=readings, last=last, avg_all=avg_all, avg_30=avg_30,
        std=std_all, thr_pct=tpct, thr_gal=thr_gal,
        max_day=max(readings, key=lambda r: r["usage"]),
        min_day=min(readings, key=lambda r: r["usage"]),
        spikes=spikes, trend=trend,
        n=len(readings),
        date_range=(readings[0]["date"], readings[-1]["date"]),
    )


# ─── Response builders ────────────────────────────────────────────────────────

def r_help():
    return (
        "💧 <b>Veolia Water Monitor Bot</b>\n\n"
        "<b>Ask me anything in plain English, for example:</b>\n"
        '  "Give me last 7 days usage, day by day"\n'
        '  "Was March 14 a leak?"\n'
        '  "Show hourly breakdown for Mar 16"\n'
        '  "Total usage this month"\n'
        '  "Which hour do I use the most water?"\n'
        '  "Am I using more water lately?"\n\n'
        "<b>Quick commands:</b>\n"
        "  /status   — latest day vs average\n"
        "  /last7    — last 7 days, day by day\n"
        "  /last30   — last 30 days summary\n"
        "  /avg      — rolling averages\n"
        "  /trend    — rising or falling?\n"
        "  /spikes   — all high-usage alerts\n"
        "  /leakcheck — leak pattern analysis\n"
        "  /high     — highest usage day\n"
        "  /low      — lowest usage day\n"
        "  /summary  — full overview\n"
        "  /compare  — this week vs last week\n"
        "  /hourly YYYY-MM-DD — hourly breakdown"
    )


def r_status(ins, data):
    if not ins: return "No data yet."
    last = ins["last"]
    avg  = ins["avg_30"]
    diff = (last["usage"] - avg) / avg * 100
    isspike = last["usage"] > ins["thr_gal"]
    hours = hourly_for(data, last["date"])
    sc, lbl, tip = leak_score(hours)
    return (
        f"📊 <b>Current Status — {fdate(last['date'])}</b>\n\n"
        f"💧 Usage: <b>{fnum(last['usage'])} gal</b>\n"
        f"📈 30-Day Avg: {fnum(avg)} gal\n"
        f"{'⬆️' if diff > 0 else '⬇️'} Difference: <b>{diff:+.1f}%</b>\n"
        f"⚠️  Alert threshold: {fnum(ins['thr_gal'])} gal (+{ins['thr_pct']}%)\n"
        f"Status: {'🚨 <b>SPIKE DETECTED</b>' if isspike else '✅ Normal'}\n"
        + (f"\n🔬 Leak Pattern: <b>{lbl}</b> (score {sc}/100)\n{tip}" if sc is not None else "")
    )


def r_last_n(ins, data, n):
    if not ins: return "No data yet."
    readings = ins["readings"]
    days = readings[-n:]
    if not days:
        return f"Only {len(readings)} day(s) of data available."
    avg      = ins["avg_30"]
    total    = sum(d["usage"] for d in days)
    week_avg = mean(d["usage"] for d in days)
    diff_pct = (week_avg - avg) / avg * 100 if avg else 0

    lines = [f"📅 <b>Last {n} Days — Day by Day</b>\n"]
    for d in days:
        day_avg = rolling_avg(readings, d["date"]) or avg
        diff = (d["usage"] - day_avg) / day_avg * 100
        tag  = spike_tag(d["usage"], day_avg, ins["thr_pct"])
        lines.append(f"  {fdate(d['date'])}: <b>{fnum(d['usage'])} gal</b> ({diff:+.0f}%){tag}")

    lines.append(f"\n📊 <b>{n}-Day Total: {fnum(total, 0)} gal</b>")
    lines.append(f"📊 {n}-Day Average: {fnum(week_avg)} gal/day")
    lines.append(f"📊 Baseline (30-day avg): {fnum(avg)} gal/day")
    lines.append(f"{'⬆️' if diff_pct > 0 else '⬇️'} vs baseline: {diff_pct:+.1f}%")
    return "\n".join(lines)


def r_hourly(data, date_str):
    hours     = hourly_for(data, date_str)
    readings  = daily_sorted(data)

    if not hours:
        return (
            f"No hourly data available for {fdate(date_str)}.\n"
            "Hourly data is only available for dates from your uploaded CSV."
        )

    total  = sum(hours)
    active = sum(1 for h in hours if h > 0)
    peak_h = hours.index(max(hours))
    sc, lbl, tip = leak_score(hours)

    max_h = max(hours) if max(hours) > 0 else 1
    bars  = []
    for i, h in enumerate(hours):
        filled = int(h / max_h * 8)
        bar    = "█" * filled + "░" * (8 - filled)
        label  = f"{i:02d}:00"
        night  = " 🌙" if i < 6 or i >= 22 else ""
        bars.append(f"  {label} {bar} {fnum(h)} gal{night}")

    lines = [
        f"🕐 <b>Hourly Breakdown — {fdate(date_str)}</b>\n",
        f"💧 Daily Total: {fnum(total)} gal",
        f"⏰ Active hours: {active}/24",
        f"📈 Peak hour: {peak_h:02d}:00 — {fnum(hours[peak_h])} gal",
        f"🔬 Leak Pattern: <b>{lbl}</b> (score {sc}/100)",
        f"   {tip}\n",
        "<code>" + "\n".join(bars) + "</code>",
    ]
    if sc and sc >= 50:
        night_active = sum(1 for h in hours[0:6] if h > 0)
        lines.append(
            f"\n⚠️ <b>High overnight usage detected.</b> "
            f"Water was running during {night_active}/6 night hours (midnight–6am)."
        )
    return "\n".join(lines)


def r_leakcheck(ins, data):
    if not ins: return "No data yet."
    readings = ins["readings"]
    hourly   = data.get("hourly", {})

    if not hourly:
        return (
            "No hourly data available for leak analysis.\n"
            "Upload your Veolia hourly CSV to enable this feature."
        )

    results = []
    for r in readings:
        h = hourly.get(r["date"])
        if not h: continue
        sc, lbl, _ = leak_score(h)
        if sc and sc >= 50:
            active = sum(1 for x in h if x > 0)
            night  = sum(1 for x in h[0:6] if x > 0)
            results.append((r["date"], r["usage"], sc, lbl, active, night))

    if not results:
        return (
            "✅ <b>Leak Check — All Clear</b>\n\n"
            f"Analyzed {len(hourly)} days of hourly data.\n"
            "No days showed suspicious overnight usage patterns.\n\n"
            "<i>A leak shows water running consistently across all hours, "
            "especially midnight–6am.</i>"
        )

    results.sort(key=lambda x: x[2], reverse=True)
    lines = [f"🔬 <b>Leak Pattern Analysis</b>\n", f"Found <b>{len(results)} suspicious day(s)</b>:\n"]
    for date_str, usage, sc, lbl, active, night in results:
        lines.append(
            f"📅 <b>{fdate(date_str)}</b> — {fnum(usage)} gal\n"
            f"   Score: {sc}/100 · {lbl}\n"
            f"   Active hours: {active}/24 · Night hours active: {night}/6\n"
        )
    lines.append("<i>Use /hourly YYYY-MM-DD to see the full hour-by-hour breakdown.</i>")
    return "\n".join(lines)


def r_avg(ins):
    if not ins: return "No data yet."
    return (
        f"📊 <b>Water Usage Averages</b>\n\n"
        f"📅 {fdate(ins['date_range'][0])} → {fdate(ins['date_range'][1])}\n"
        f"💧 30-Day rolling avg: <b>{fnum(ins['avg_30'])} gal/day</b>\n"
        f"💧 All-time average: <b>{fnum(ins['avg_all'])} gal/day</b>\n"
        f"📉 Lowest:  {fnum(ins['min_day']['usage'])} gal ({fdate(ins['min_day']['date'])})\n"
        f"📈 Highest: {fnum(ins['max_day']['usage'])} gal ({fdate(ins['max_day']['date'])})\n"
        f"↕️  Std deviation: {fnum(ins['std'])} gal"
    )


def r_trend(ins):
    if not ins: return "No data yet."
    if not ins["trend"]:
        return "Not enough data for trend analysis (need 14+ days)."
    direction, pct, r7, p7 = ins["trend"]
    emoji = {"rising": "📈", "falling": "📉", "stable": "➡️"}[direction]
    return (
        f"{emoji} <b>Usage Trend: {direction.capitalize()}</b>\n\n"
        f"This week avg: <b>{fnum(r7)} gal/day</b>\n"
        f"Last week avg: <b>{fnum(p7)} gal/day</b>\n"
        f"Change: <b>{pct:+.1f}%</b>\n\n"
        + ("⚠️ Usage is rising — possible leak or increased consumption." if direction == "rising"
           else "👍 Usage is decreasing." if direction == "falling"
           else "✅ Usage is stable and consistent.")
    )


def r_spikes(ins):
    if not ins: return "No data yet."
    if not ins["spikes"]:
        return (
            f"✅ <b>No spikes in {ins['n']} days</b>\n\n"
            f"All readings within +{ins['thr_pct']}% of the rolling average."
        )
    lines = [f"🚨 <b>{len(ins['spikes'])} Spike(s) Detected</b>\n"]
    for s in sorted(ins["spikes"], key=lambda x: x["date"], reverse=True):
        lines.append(
            f"  📅 <b>{fdate(s['date'])}</b>: {fnum(s['usage'])} gal "
            f"— ↑{s['pct']:.0f}% above avg ({fnum(s['avg'])} gal)"
        )
    lines.append(f"\n<i>Threshold: +{ins['thr_pct']}% above 30-day rolling average</i>")
    lines.append("<i>Use /hourly YYYY-MM-DD to check if it was a leak or event.</i>")
    return "\n".join(lines)


def r_high(ins):
    if not ins: return "No data yet."
    m, avg = ins["max_day"], ins["avg_all"]
    return (
        f"📈 <b>Highest Usage Day</b>\n\n"
        f"📅 {fdate(m['date'])}: <b>{fnum(m['usage'])} gal</b>\n"
        f"That's {(m['usage']-avg)/avg*100:+.1f}% above your overall avg of {fnum(avg)} gal.\n"
        f"Type: /hourly {m['date']} — to see if it was a leak."
    )


def r_low(ins):
    if not ins: return "No data yet."
    m, avg = ins["min_day"], ins["avg_all"]
    return (
        f"📉 <b>Lowest Usage Day</b>\n\n"
        f"📅 {fdate(m['date'])}: <b>{fnum(m['usage'])} gal</b>\n"
        f"That's {(m['usage']-avg)/avg*100:.1f}% below your overall avg of {fnum(avg)} gal."
    )


def r_summary(ins):
    if not ins: return "No data yet."
    t = ins
    trend_str = ""
    if t["trend"]:
        d, p, r7, p7 = t["trend"]
        trend_str = f"\n📊 Weekly trend: {d.capitalize()} ({p:+.1f}%)"
    return (
        f"📋 <b>Full Summary</b>\n\n"
        f"📅 {fdate(t['date_range'][0])} → {fdate(t['date_range'][1])} ({t['n']} days)\n\n"
        f"💧 <b>Daily Usage:</b>\n"
        f"  30-Day avg:   {fnum(t['avg_30'])} gal\n"
        f"  All-time avg: {fnum(t['avg_all'])} gal\n"
        f"  Highest: {fnum(t['max_day']['usage'])} gal ({fdate(t['max_day']['date'])})\n"
        f"  Lowest:  {fnum(t['min_day']['usage'])} gal ({fdate(t['min_day']['date'])})\n"
        f"  Std dev: {fnum(t['std'])} gal\n\n"
        f"🚨 <b>Alerts:</b> {len(t['spikes'])} spike(s) at +{t['thr_pct']}% threshold"
        f"{trend_str}"
    )


def r_compare(ins):
    if not ins or len(ins["readings"]) < 7:
        return "Need at least 7 days of data."
    readings = ins["readings"]
    this7 = mean(r["usage"] for r in readings[-7:])
    prev7 = mean(r["usage"] for r in readings[-14:-7]) if len(readings) >= 14 else None
    lines = [
        f"📅 <b>This Week vs Last Week</b>\n",
        f"This week avg: <b>{fnum(this7)} gal/day</b>",
    ]
    if prev7:
        lines.append(f"Last week avg: <b>{fnum(prev7)} gal/day</b>")
        d = (this7 - prev7) / prev7 * 100
        lines.append(f"Change: {'⬆️' if d > 0 else '⬇️'} <b>{d:+.1f}%</b>\n")
        lines.append(
            "⚠️ Usage notably higher this week." if d > 15
            else "👍 Lower than last week." if d < -10
            else "✅ About the same as last week."
        )
    return "\n".join(lines)


def r_peak_hour(data):
    hourly = data.get("hourly", {})
    if not hourly:
        return "No hourly data available."
    totals = [0.0] * 24
    for day_hours in hourly.values():
        for i, h in enumerate(day_hours):
            totals[i] += h
    peak   = totals.index(max(totals))
    ranked = sorted(range(24), key=lambda i: totals[i], reverse=True)
    lines  = [
        "⏰ <b>Average Usage by Hour of Day</b>\n",
        f"Peak hour: <b>{peak:02d}:00 – {peak+1:02d}:00</b>\n",
        "<i>Top 5 highest hours:</i>",
    ]
    for i in ranked[:5]:
        avg_h = totals[i] / len(hourly)
        lines.append(f"  {i:02d}:00 — avg {fnum(avg_h)} gal/day")
    lines.append("\n<i>Low overnight usage = normal. High overnight = possible leak.</i>")
    return "\n".join(lines)


def r_total_period(ins, label, n_days):
    if not ins: return "No data yet."
    days  = ins["readings"][-n_days:]
    total = sum(d["usage"] for d in days)
    avg   = mean(d["usage"] for d in days)
    return (
        f"💧 <b>Total Usage — {label}</b>\n\n"
        f"📅 {fdate(days[0]['date'])} → {fdate(days[-1]['date'])}\n"
        f"💧 Total: <b>{total:,.0f} gal</b>\n"
        f"📊 Average: {fnum(avg)} gal/day\n"
        f"📆 Days: {len(days)}"
    )


# ─── Intent Parser ────────────────────────────────────────────────────────────

def parse_date_from_text(text):
    """Try to extract a date from natural language text."""
    t = text.lower()
    if "yesterday" in t:
        return str(date.today() - timedelta(days=1))
    if "today" in t:
        return str(date.today())
    m = re.search(r'(\d{4}-\d{2}-\d{2})', t)
    if m: return m.group(1)
    for name, num in MONTHS.items():
        m = re.search(rf'\b{name}\s+(\d{{1,2}})\b', t)
        if m:
            day = int(m.group(1))
            return f"{date.today().year}-{num:02d}-{day:02d}"
        m = re.search(rf'\b(\d{{1,2}})\s+{name}\b', t)
        if m:
            day = int(m.group(1))
            return f"{date.today().year}-{num:02d}-{day:02d}"
    return None


def parse_n_days(text):
    """Extract number from 'last N days' type phrases."""
    t = text.lower()
    m = re.search(r'last\s+(\d+)\s+days?', t)
    if m: return int(m.group(1))
    m = re.search(r'past\s+(\d+)\s+days?', t)
    if m: return int(m.group(1))
    if re.search(r'last\s+week|past\s+week|this\s+week', t):   return 7
    if re.search(r'last\s+month|past\s+month|this\s+month|monthly', t): return 30
    if re.search(r'last\s+fortnight|last\s+two\s+weeks', t):   return 14
    return None


def classify(text):
    """
    Returns (intent, params_dict).
    Tries hard to understand any reasonable water-usage question.
    """
    t = text.lower().strip().lstrip('/')

    # ── Explicit commands ───────────────────────────────────────────────────
    if t in ("start",):           return "start",    {}
    if t in ("help","?","hi","hello","hey"): return "help", {}
    if t in ("status","check"):   return "status",   {}
    if t in ("today","latest","recent","now"): return "today", {}
    if t in ("avg","average","mean"): return "avg",  {}
    if t in ("trend","direction"):return "trend",    {}
    if t in ("spikes","spike","alerts","alert","anomaly","anomalies"): return "spikes", {}
    if t in ("high","max","highest","most","peak","worst"): return "high", {}
    if t in ("low","min","lowest","least","best"):  return "low",   {}
    if t in ("summary","report","overview","all","full"): return "summary", {}
    if t in ("compare","comparison","week","weekly"): return "compare", {}
    if t in ("last7","7days","7d"):  return "lastn", {"n": 7}
    if t in ("last14","14days","14d","fortnight"): return "lastn", {"n": 14}
    if t in ("last30","30days","30d","monthly","month"): return "lastn", {"n": 30}
    if t in ("leakcheck","leak","leaktest","leakanalysis"): return "leakcheck", {}
    if t.startswith("hourly"):
        d = parse_date_from_text(t)
        return "hourly", {"date": d}
    if t in ("peakhour","peak_hour","which_hour"): return "peakhour", {}

    # ── Natural language ────────────────────────────────────────────────────
    if re.search(r'\bhour(ly|s| by hour)?\b', t) or "each hour" in t or "per hour" in t:
        d = parse_date_from_text(t)
        return "hourly", {"date": d}

    d = parse_date_from_text(t)
    if d and re.search(r'\bhour', t):
        return "hourly", {"date": d}
    if d:
        return "day_detail", {"date": d}

    n = parse_n_days(t)
    if n:
        return "lastn", {"n": n}

    if re.search(r'\b(total|sum|how much|cumulative)\b', t):
        n2 = parse_n_days(t)
        if n2: return "total", {"n": n2, "label": f"last {n2} days"}
        if re.search(r'this month|monthly', t): return "total", {"n": 30, "label": "this month"}
        return "total", {"n": 7, "label": "last 7 days"}

    if re.search(r'day\s+by\s+day|daily\s+break|each\s+day|per\s+day\s+break', t):
        n3 = parse_n_days(t)
        return "lastn", {"n": n3 or 7}

    if re.search(r'\bleak\b|drip|pipe|plumb|running.*water|water.*running|overnight', t):
        return "leakcheck", {}

    if re.search(r'\b(average|avg|mean|typical|normal|usual)\b', t):
        return "avg", {}

    if re.search(r'\b(trend|rising|falling|going up|going down|increasing|decreasing|more.*lately|less.*lately)\b', t):
        return "trend", {}

    if re.search(r'\b(compar|versus|vs|this week.*last week|week.*week)\b', t):
        return "compare", {}

    if re.search(r'\b(which hour|busiest|peak hour|most.*hour|hour.*most)\b', t):
        return "peakhour", {}

    if re.search(r'\b(spike|high usage|abnormal|unusual|alert|exceed|above average)\b', t):
        return "spikes", {}

    if re.search(r'\b(status|how am i|how\'s|am i using|check)\b', t):
        return "status", {}

    if re.search(r'\b(summar|overview|report|tell me|everything|all data)\b', t):
        return "summary", {}

    return "unknown", {}


def generate_reply(text, ins, data):
    """Main dispatcher: classify message → call appropriate response builder."""
    intent, params = classify(text)

    if intent == "start":   return r_help()
    if intent == "help":    return r_help()
    if intent == "status":  return r_status(ins, data)
    if intent == "today":
        if not ins: return "No data yet."
        last = ins["last"]
        avg  = ins["avg_30"]
        diff = (last["usage"] - avg) / avg * 100
        return (
            f"💧 <b>Most Recent Reading</b>\n\n"
            f"📅 {fdate(last['date'])}\n"
            f"💧 <b>{fnum(last['usage'])} gal</b>\n"
            f"{'⬆️' if diff > 0 else '⬇️'} {diff:+.1f}% vs 30-day avg of {fnum(avg)} gal"
        )
    if intent == "avg":      return r_avg(ins)
    if intent == "trend":    return r_trend(ins)
    if intent == "spikes":   return r_spikes(ins)
    if intent == "high":     return r_high(ins)
    if intent == "low":      return r_low(ins)
    if intent == "summary":  return r_summary(ins)
    if intent == "compare":  return r_compare(ins)
    if intent == "leakcheck": return r_leakcheck(ins, data)
    if intent == "peakhour": return r_peak_hour(data)

    if intent == "lastn":
        n = params.get("n", 7)
        return r_last_n(ins, data, n)

    if intent == "total":
        n     = params.get("n", 7)
        label = params.get("label", f"last {n} days")
        return r_total_period(ins, label, n)

    if intent == "hourly":
        d = params.get("date")
        if not d:
            if ins: d = ins["last"]["date"]
            else:   return "Please specify a date, e.g. /hourly 2026-03-14"
        return r_hourly(data, d)

    if intent == "day_detail":
        d = params.get("date")
        if not d: return "Could not parse that date."
        readings = daily_sorted(data)
        entry = next((r for r in readings if r["date"] == d), None)
        if not entry:
            return f"No data for {fdate(d)}."
        avg  = rolling_avg(readings, d) or (ins["avg_all"] if ins else None)
        diff = (entry["usage"] - avg) / avg * 100 if avg else 0
        hours = hourly_for(data, d)
        sc, lbl, tip = leak_score(hours)
        reply = f"📅 <b>{fdate(d)}</b>\n\n💧 Usage: <b>{fnum(entry['usage'])} gal</b>\n"
        if avg:
            reply += f"📊 Avg on that date: {fnum(avg)} gal ({diff:+.1f}%)\n"
        if sc is not None:
            reply += f"🔬 Leak pattern: <b>{lbl}</b> (score {sc}/100)\n{tip}\n"
        reply += f"\nType /hourly {d} for a full hour-by-hour breakdown."
        return reply

    # Unknown — helpful fallback, never says "I can't help"
    return (
        "💧 I'm not sure exactly what you're asking, but here are some options:\n\n"
        "  📊 <b>/last7</b> — last 7 days, day by day\n"
        "  🔬 <b>/leakcheck</b> — analyze for leak patterns\n"
        "  🕐 <b>/hourly 2026-03-14</b> — hour-by-hour breakdown\n"
        "  📋 <b>/summary</b> — full overview\n"
        "  ❓ <b>/help</b> — all commands\n\n"
        "<i>Try: \"Was March 14 a leak?\", \"Give me last 7 days\", "
        "\"Which hour uses most water?\"</i>"
    )


# ─── Telegram sender (used by both webhook and Actions notifier) ───────────────

def send_telegram(token, chat_id, text):
    """Send a Telegram message, splitting at 4000 chars if needed."""
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        try:
            url  = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id":    chat_id,
                "text":       chunk,
                "parse_mode": "HTML",
            }).encode()
            req  = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                result = json.loads(r.read())
                if not result.get("ok"):
                    print(f"Telegram error: {result}")
        except Exception as e:
            print(f"send_telegram failed: {e}")
