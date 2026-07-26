"""
Bracket Board - Phase 2: Live Collector & Alerts
-------------------------------------------------
Runs every ~20 minutes. Each run:
  1. Fetches the newest stake/unstake events from the global feed
     (/api/delegation/v1), typically 1-3 API calls.
  2. Stores new events in the database (shared with the sweep).
  3. If any event involves a wallet in watchlist.txt, sends one
     combined alert via Telegram and/or email (whichever secrets
     are configured; missing channels are skipped silently).

Budget: shares the same monthly ceiling as the sweep via the common
meta counter in bracketboard.db. This run is capped by CALLS_PER_RUN
(set to a small number in the workflow, e.g. 6).
"""

import json
import os
import smtplib
import sqlite3
import ssl
import urllib.request
from email.mime.text import MIMEText
from datetime import datetime, timezone

import sweep  # reuse db(), api_get(), budget, rao_to_tao

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(HERE, "watchlist.txt")
MAX_PAGES = 3
PAGE_LIMIT = 200
EVENT_RETENTION_DAYS = 45  # non-watchlist events pruned after this


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
        block_number INTEGER, timestamp TEXT, coldkey TEXT, hotkey TEXT,
        netuid INTEGER, action TEXT, tao_amount REAL, alpha_amount REAL,
        price REAL, watched INTEGER DEFAULT 0)""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ev_dedup ON events
        (block_number, coldkey, hotkey, netuid, action, tao_amount, timestamp)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ev_cold ON events(coldkey)")


def load_watchlist():
    if not os.path.exists(WATCHLIST_PATH):
        return set()
    keys = set()
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                keys.add(line)
    return keys


def parse_event(item):
    """Defensive extraction - field names per taostats delegation feed."""
    def ss58(field):
        v = item.get(field)
        if isinstance(v, dict):
            return v.get("ss58", "") or ""
        return v or ""
    coldkey = ss58("coldkey") or ss58("nominator")
    hotkey = ss58("hotkey") or ss58("delegate")
    action = str(item.get("action", "")).lower()
    if action in ("add", "stake", "delegate"):
        action = "BUY"
    elif action in ("remove", "unstake", "undelegate"):
        action = "SELL"
    else:
        action = action.upper() or "?"
    netuid = item.get("netuid")
    try:
        netuid = int(netuid) if netuid is not None else -1
    except (TypeError, ValueError):
        netuid = -1
    tao = sweep.rao_to_tao(item.get("tao_amount") or item.get("amount"))
    # The feed calls these "alpha" and "alpha_price_in_tao"; the older
    # "alpha_amount"/"rate" names never matched, so both silently stored 0.
    alpha = sweep.rao_to_tao(item.get("alpha_amount") or item.get("alpha"))
    try:
        price = float(item.get("rate") or item.get("alpha_price_in_tao") or 0)
    except (TypeError, ValueError):
        price = 0.0
    block = item.get("block_number") or 0
    ts = item.get("timestamp") or ""
    return (block, ts, coldkey, hotkey, netuid, action, tao, alpha, price)


def collect(conn, watchlist):
    last_seen = int(sweep.meta_get(conn, "last_event_block", "0"))
    max_block = last_seen
    new_events, watched_events = [], []
    first_run = last_seen == 0

    for page in range(1, MAX_PAGES + 1):
        data = sweep.api_get(conn, "/api/delegation/v1?limit={}&page={}"
                             .format(PAGE_LIMIT, page))
        if not data:
            break
        items = data.get("data", [])
        if page == 1 and items:
            print("Feed sample keys: {}".format(sorted(items[0].keys())))
        page_has_old = False
        for item in items:
            ev = parse_event(item)
            block = ev[0]
            if block <= last_seen:
                page_has_old = True
                continue
            max_block = max(max_block, block)
            new_events.append(ev)
            if ev[2] in watchlist:
                watched_events.append(ev)
        if page_has_old or not items or not (data.get("pagination") or {}).get("next_page"):
            break

    if first_run and new_events:
        # baseline run: record position, store events, but don't alert
        # (avoids blasting old history as "news")
        watched_events = []

    for ev in new_events:
        watched = 1 if ev[2] in watchlist else 0
        conn.execute(
            "INSERT OR IGNORE INTO events(block_number,timestamp,coldkey,hotkey,"
            "netuid,action,tao_amount,alpha_amount,price,watched) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", ev + (watched,))
    if max_block > last_seen:
        sweep.meta_set(conn, "last_event_block", max_block)
    conn.execute(
        "DELETE FROM events WHERE watched=0 AND timestamp < datetime('now', ?)",
        ("-{} days".format(EVENT_RETENTION_DAYS),))
    conn.commit()
    print("New events stored: {} (watched: {})".format(
        len(new_events), len(watched_events)))
    return watched_events


# ------------------------------ alerts ----------------------------------

def subnet_names(conn):
    return dict(conn.execute("SELECT netuid, name FROM subnets").fetchall())


def format_alert(events, names):
    lines = []
    for block, ts, coldkey, hotkey, netuid, action, tao, alpha, price in events:
        sn = names.get(netuid) or "SN{}".format(netuid)
        short = coldkey[:8] + "..." + coldkey[-4:] if len(coldkey) > 14 else coldkey
        lines.append(
            "{} {} on {} (SN{}): {:.3f} TAO"
            "{} | {}\nhttps://taostats.io/account/{}".format(
                short, action, sn, netuid, tao,
                " @ {:.6f} TAO/alpha".format(price) if price else "",
                ts, coldkey))
    header = "Bracket Board: {} watched wallet event(s)\n\n".format(len(events))
    return header + "\n\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram not configured - skipping.")
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text,
                              "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot{}/sendMessage".format(token),
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("Telegram alert sent.")
    except Exception as e:
        print("Telegram send failed: {}".format(e))


def send_email(text, count):
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = os.environ.get("ALERT_EMAIL_TO", "").strip() or address
    if not address or not app_pw:
        print("Email not configured - skipping.")
        return
    try:
        msg = MIMEText(text)
        msg["Subject"] = "Bracket Board: {} watched wallet event(s)".format(count)
        msg["From"] = address
        msg["To"] = to_addr
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(address, app_pw)
            server.sendmail(address, [to_addr], msg.as_string())
        print("Email alert sent.")
    except Exception as e:
        print("Email send failed: {}".format(e))


def main():
    conn = sweep.db()
    ensure_tables(conn)
    if sweep.budget_left(conn) <= 0:
        print("Monthly API budget ceiling reached - collector skipping.")
        return
    watchlist = load_watchlist()
    print("Watchlist wallets: {}".format(len(watchlist)))
    watched = collect(conn, watchlist)
    if watched:
        text = format_alert(watched, subnet_names(conn))
        send_telegram(text)
        send_email(text, len(watched))
    conn.commit()
    conn.close()
    print("Collector run complete.")


if __name__ == "__main__":
    main()
