"""
Bracket Board - Phase 1: Sweep & Bracket Engine
------------------------------------------------
What this does on every scheduled run:
  1. Refreshes the subnet list and pool prices (cheap: ~3 calls).
  2. Continues crawling holder lists of all subnets, page by page,
     spending at most CALLS_PER_RUN API calls, then stops and saves
     its position so the next run continues where it left off.
  3. When a full pass over all subnets completes, it computes each
     wallet's total portfolio (in TAO), assigns size brackets, and
     records a daily snapshot (the seed of future return math).
  4. Regenerates docs/index.html - the public dashboard.

Design constraints honoured:
  - Pace and ceiling come from the workflow (PACE_SECONDS, CALLS_PER_RUN,
    MONTHLY_CEILING). Defaults suit the free tier (5 calls/min, 10k/month);
    the workflows override them for the paid Standard tier (60/min, 50k).
    The monthly counter is SHARED by sweep/collector/backfill, so every
    workflow must pass the same MONTHLY_CEILING or the lower one starves.
  - Only Python standard library. State lives in bracketboard.db
    (SQLite) which the workflow commits back to the repository.
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, date

BASE = "https://api.taostats.io"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bracketboard.db")
DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")

CALLS_PER_RUN = int(os.environ.get("CALLS_PER_RUN", "40"))
MONTHLY_CEILING = int(os.environ.get("MONTHLY_CEILING", "9000"))
PAUSE_SECONDS = float(os.environ.get("PACE_SECONDS", "13.2"))
PAGE_SIZE = 200
DUST_TAO = 0.05          # ignore positions below this (TAO) to keep DB small
RAO = 1_000_000_000      # 1 TAO = 1e9 rao

BRACKETS = [
    ("1-10", 1, 10),
    ("10-50", 10, 50),
    ("50-100", 50, 100),
    ("100-200", 100, 200),
    ("200-500", 200, 500),
    ("500-1000", 500, 1000),
    ("1000+", 1000, float("inf")),
]

calls_used_this_run = 0


# ----------------------------- storage ---------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS subnets(
        netuid INTEGER PRIMARY KEY, name TEXT, price REAL,
        market_cap REAL, owner_ss58 TEXT, total_holders INTEGER,
        updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS holders(
        netuid INTEGER, coldkey TEXT, hotkey TEXT, hotkey_name TEXT,
        balance_alpha REAL, balance_tao REAL, generation INTEGER,
        updated_at TEXT,
        PRIMARY KEY (netuid, coldkey, hotkey))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wallet_daily(
        day TEXT, coldkey TEXT, total_tao REAL, subnets INTEGER,
        PRIMARY KEY (day, coldkey))""")
    # The holders primary key leads with netuid, so looking a wallet's subnets
    # up by coldkey cannot use it. The leaderboard does exactly that for every
    # event row when restricting flows to tracked subnets.
    conn.execute("CREATE INDEX IF NOT EXISTS h_cold ON holders(coldkey, netuid)")
    return conn


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ----------------------------- API layer -------------------------------

def month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def budget_left(conn):
    used = int(meta_get(conn, "calls_" + month_key(), "0"))
    return MONTHLY_CEILING - used


def record_call(conn):
    key = "calls_" + month_key()
    used = int(meta_get(conn, key, "0")) + 1
    meta_set(conn, key, used)


def api_get(conn, path, retries=2):
    """One paced, budgeted, retried API call. Returns parsed JSON or None."""
    global calls_used_this_run
    if calls_used_this_run >= CALLS_PER_RUN or budget_left(conn) <= 0:
        return None
    key = os.environ.get("TAOSTATS_API_KEY", "").strip()
    if not key:
        print("FATAL: TAOSTATS_API_KEY secret is not set.")
        sys.exit(1)
    url = BASE + path
    for attempt in range(retries + 1):
        time.sleep(PAUSE_SECONDS)
        calls_used_this_run += 1
        record_call(conn)
        conn.commit()
        req = urllib.request.Request(url)
        req.add_header("accept", "application/json")
        req.add_header("Authorization", key)
        req.add_header("User-Agent", "BracketBoard/1.0")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  rate limited; backing off 65s")
                time.sleep(65)
                continue
            if 500 <= e.code < 600 and attempt < retries:
                print("  server error {}; retrying".format(e.code))
                continue
            print("  HTTP {} on {}".format(e.code, path))
            return None
        except Exception as e:
            if attempt < retries:
                continue
            print("  connection error on {}: {}".format(path, e))
            return None
    return None


def rao_to_tao(v):
    try:
        return float(v) / RAO
    except (TypeError, ValueError):
        return 0.0


# --------------------------- sweep logic --------------------------------

def refresh_subnets(conn):
    """Refresh subnet metadata + prices if older than ~20 hours. ~3-4 calls."""
    last = meta_get(conn, "subnets_refreshed_at")
    if last:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(last)).total_seconds()
        if age < 20 * 3600:
            return True
    print("Refreshing subnet list and pool prices...")
    page = 1
    owners = {}
    while True:
        data = api_get(conn, "/api/subnet/latest/v1?limit=100&page={}".format(page))
        if not data:
            return False
        for s in data.get("data", []):
            owners[s["netuid"]] = (s.get("owner") or {}).get("ss58", "")
        nxt = (data.get("pagination") or {}).get("next_page")
        if not nxt:
            break
        page = nxt
    pools = api_get(conn, "/api/dtao/pool/latest/v1?limit=1024")
    if not pools:
        return False
    now = datetime.now(timezone.utc).isoformat()
    for p in pools.get("data", []):
        netuid = p.get("netuid")
        if netuid is None or netuid == 0:
            continue
        conn.execute(
            "INSERT INTO subnets(netuid,name,price,market_cap,owner_ss58,total_holders,updated_at) "
            "VALUES(?,?,?,?,?,COALESCE((SELECT total_holders FROM subnets WHERE netuid=?),0),?) "
            "ON CONFLICT(netuid) DO UPDATE SET name=excluded.name, price=excluded.price, "
            "market_cap=excluded.market_cap, owner_ss58=excluded.owner_ss58, updated_at=excluded.updated_at",
            (netuid, p.get("name") or "", float(p.get("price") or 0),
             rao_to_tao(p.get("market_cap")), owners.get(netuid, ""), netuid, now))
    meta_set(conn, "subnets_refreshed_at", now)
    conn.commit()
    print("  {} subnets on record.".format(
        conn.execute("SELECT COUNT(*) FROM subnets").fetchone()[0]))
    return True


def sweep_order(conn):
    """Biggest subnets first so the dashboard is useful early."""
    return [r[0] for r in conn.execute(
        "SELECT netuid FROM subnets ORDER BY market_cap DESC")]


def continue_sweep(conn):
    """Crawl holder pages until this run's call budget is spent."""
    generation = int(meta_get(conn, "generation", "1"))
    state = json.loads(meta_get(conn, "sweep_state", "{}"))
    order = sweep_order(conn)
    if not order:
        return
    done = set(state.get("done", []))
    current = state.get("current")
    page = state.get("page", 1)
    now = datetime.now(timezone.utc).isoformat()

    while calls_used_this_run < CALLS_PER_RUN and budget_left(conn) > 0:
        if current is None:
            remaining = [n for n in order if n not in done]
            if not remaining:
                finalize_generation(conn, generation)
                generation += 1
                meta_set(conn, "generation", generation)
                done, current, page = set(), None, 1
                meta_set(conn, "sweep_state", json.dumps({"done": [], "current": None, "page": 1}))
                conn.commit()
                continue
            current, page = remaining[0], 1
            print("Sweeping subnet {} (gen {})...".format(current, generation))

        data = api_get(conn, "/api/dtao/stake_balance/latest/v1?netuid={}&limit={}&page={}"
                       .format(current, PAGE_SIZE, page))
        if data is None:
            break  # budget exhausted or persistent error; resume next run
        rows = data.get("data", [])
        for h in rows:
            tao_val = rao_to_tao(h.get("balance_as_tao"))
            if tao_val < DUST_TAO:
                continue
            conn.execute(
                "INSERT INTO holders(netuid,coldkey,hotkey,hotkey_name,balance_alpha,balance_tao,generation,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(netuid,coldkey,hotkey) DO UPDATE SET hotkey_name=excluded.hotkey_name, "
                "balance_alpha=excluded.balance_alpha, balance_tao=excluded.balance_tao, "
                "generation=excluded.generation, updated_at=excluded.updated_at",
                (current, (h.get("coldkey") or {}).get("ss58", ""),
                 (h.get("hotkey") or {}).get("ss58", ""),
                 h.get("hotkey_name") or "", rao_to_tao(h.get("balance")),
                 tao_val, generation, now))
        pag = data.get("pagination") or {}
        total_holders = pag.get("total_items")
        if total_holders is not None:
            conn.execute("UPDATE subnets SET total_holders=? WHERE netuid=?",
                         (total_holders, current))
        nxt = pag.get("next_page")
        if nxt:
            page = nxt
        else:
            done.add(current)
            current, page = None, 1
        meta_set(conn, "sweep_state", json.dumps(
            {"done": sorted(done), "current": current, "page": page}))
        conn.commit()


def finalize_generation(conn, generation):
    """A full pass finished: purge stale rows, snapshot wallet totals.

    v1.1: delta snapshots + retention thinning to keep the DB small.
      - A wallet gets a new daily row only if its total changed
        meaningfully (>0.5% or >0.1 TAO) or it has no row yet.
      - Daily detail is kept ~100 days; older rows thin to Mondays.
    """
    print("Full sweep pass {} complete - snapshotting wallets.".format(generation))
    conn.execute("DELETE FROM holders WHERE generation < ?", (generation,))
    today = date.today().isoformat()

    previous = dict(conn.execute("""
        SELECT wd.coldkey, wd.total_tao FROM wallet_daily wd
        JOIN (SELECT coldkey, MAX(day) AS d FROM wallet_daily GROUP BY coldkey) m
          ON m.coldkey = wd.coldkey AND m.d = wd.day""").fetchall())

    current = conn.execute("""
        SELECT coldkey, SUM(balance_tao), COUNT(DISTINCT netuid)
        FROM holders GROUP BY coldkey""").fetchall()

    written = 0
    for coldkey, total, subs in current:
        total = total or 0.0
        prev = previous.get(coldkey)
        changed = (prev is None
                   or abs(total - prev) > max(0.1, abs(prev) * 0.005))
        if changed:
            conn.execute(
                "INSERT INTO wallet_daily(day,coldkey,total_tao,subnets) VALUES(?,?,?,?) "
                "ON CONFLICT(day,coldkey) DO UPDATE SET "
                "total_tao=excluded.total_tao, subnets=excluded.subnets",
                (today, coldkey, total, subs))
            written += 1
    print("  snapshot rows written: {} of {} wallets (delta mode)".format(
        written, len(current)))

    conn.execute("""
        DELETE FROM wallet_daily
        WHERE day < date('now','-100 days') AND strftime('%w', day) != '1'""")

    meta_set(conn, "last_full_sweep", datetime.now(timezone.utc).isoformat())
    conn.commit()
    conn.execute("VACUUM")


# --------------------------- dashboard ----------------------------------

def bracket_of(total):
    for label, lo, hi in BRACKETS:
        if lo <= total < hi:
            return label
    return None


def build_dashboard(conn):
    """Write docs/index.html.

    Delegates to dashboard.py. If anything in there fails, fall back to the
    plain holdings table below rather than leaving the site without a page.
    """
    os.makedirs(DOCS_DIR, exist_ok=True)
    try:
        import dashboard
        import leaderboard
        state = json.loads(meta_get(conn, "sweep_state", "{}"))
        ranked = conn.execute(
            "SELECT COUNT(*) FROM backfill_done WHERE history_done=1").fetchone()[0]
        meta = dashboard.meta_line(
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            len(state.get("done", [])),
            conn.execute("SELECT COUNT(*) FROM subnets").fetchone()[0],
            str(meta_get(conn, "last_full_sweep", "never"))[:16],
            meta_get(conn, "calls_" + month_key(), "0"), MONTHLY_CEILING,
            conn.execute("SELECT COUNT(DISTINCT day) FROM wallet_daily").fetchone()[0],
            ranked)
        html = dashboard.render(conn, BRACKETS, bracket_of,
                                leaderboard.audited_board, leaderboard.WINDOWS, meta)
        with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print("Dashboard regenerated ({} KB).".format(len(html) // 1024))
        return
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("Rich dashboard failed ({}); writing the fallback page.".format(e))

    _build_fallback_dashboard(conn)


def _build_fallback_dashboard(conn):
    owners = {r[0] for r in conn.execute(
        "SELECT owner_ss58 FROM subnets WHERE owner_ss58 != ''")}
    wallets = conn.execute("""
        SELECT coldkey, SUM(balance_tao) AS total, COUNT(DISTINCT netuid) AS subs
        FROM holders GROUP BY coldkey""").fetchall()

    per_bracket = {label: [] for label, _, _ in BRACKETS}
    for coldkey, total, subs in wallets:
        label = bracket_of(total or 0)
        if label:
            per_bracket[label].append(
                (coldkey, total, subs, coldkey in owners))

    subnets_total = conn.execute("SELECT COUNT(*) FROM subnets").fetchone()[0]
    state = json.loads(meta_get(conn, "sweep_state", "{}"))
    covered = len(state.get("done", []))
    last_full = meta_get(conn, "last_full_sweep", "never")
    calls_used = meta_get(conn, "calls_" + month_key(), "0")
    snap_days = conn.execute(
        "SELECT COUNT(DISTINCT day) FROM wallet_daily").fetchone()[0]

    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    parts = []
    parts.append("""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bracket Board</title><style>
 body{font-family:system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 .wrap{max-width:900px;margin:0 auto;padding:20px}
 h1{font-size:1.5em} h2{font-size:1.1em;margin-top:1.8em;color:#9fd3a8}
 .meta{color:#8a8f98;font-size:.85em;line-height:1.5}
 table{width:100%;border-collapse:collapse;font-size:.85em;margin-top:.5em}
 th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #23262d}
 th{color:#8a8f98;font-weight:600}
 td.num{text-align:right;font-variant-numeric:tabular-nums}
 a{color:#7ab8f5;text-decoration:none;word-break:break-all}
 .tag{background:#3a2d12;color:#e2b04a;border-radius:4px;padding:1px 6px;font-size:.75em}
 .note{background:#161a21;border:1px solid #23262d;border-radius:8px;padding:10px 14px;
       font-size:.85em;color:#aab;line-height:1.5;margin-top:1em}
</style></head><body><div class="wrap">
<h1>Bracket Board</h1>""")
    parts.append('<div class="meta">Updated: {} UTC &middot; Subnet coverage this pass: {}/{} '
                 '&middot; Last full sweep: {} &middot; API calls this month: {}/{} '
                 '&middot; Snapshot days collected: {}</div>'.format(
                     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                     covered, subnets_total, esc(str(last_full)[:16]),
                     calls_used, MONTHLY_CEILING, snap_days))
    parts.append('<div class="note">Phase 1 shows <b>holdings</b>, not performance. '
                 'Return leaderboards (7/14/30/90-day) switch on automatically as daily '
                 'snapshots accumulate. Wallets marked <span class="tag">owner</span> are '
                 'subnet owners - likely operators, not investors. This is public '
                 'blockchain data, not financial advice.</div>')

    for label, lo, hi in BRACKETS:
        rows = sorted(per_bracket[label], key=lambda r: -(r[1] or 0))[:20]
        parts.append("<h2>Bracket {} TAO <span class='meta'>({} wallets)</span></h2>"
                     .format(label, len(per_bracket[label])))
        if not rows:
            parts.append('<div class="meta">No wallets found yet - sweep in progress.</div>')
            continue
        parts.append("<table><tr><th>#</th><th>Wallet</th>"
                     "<th class='num'>Portfolio (TAO)</th><th class='num'>Subnets</th></tr>")
        for i, (ck, total, subs, is_owner) in enumerate(rows, 1):
            tag = ' <span class="tag">owner</span>' if is_owner else ""
            parts.append(
                "<tr><td>{}</td><td><a href='https://taostats.io/account/{}'>{}</a>{}</td>"
                "<td class='num'>{:.2f}</td><td class='num'>{}</td></tr>".format(
                    i, esc(ck), esc(ck[:10] + "..." + ck[-6:]), tag, total or 0, subs))
        parts.append("</table>")

    try:
        import leaderboard
        parts.append(leaderboard.render_html(
            conn, BRACKETS, bracket_of, esc))
    except Exception as e:
        parts.append("<div class='meta'>Leaderboards unavailable this run: {}</div>"
                     .format(esc(str(e))))

    parts.append("</div></body></html>")
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write("".join(parts))
    print("Dashboard regenerated.")


# ------------------------------ main ------------------------------------

def main():
    conn = db()
    if budget_left(conn) <= 0:
        print("Monthly API budget ceiling reached - skipping this run.")
        build_dashboard(conn)
        return
    if refresh_subnets(conn):
        continue_sweep(conn)
    build_dashboard(conn)
    conn.commit()
    conn.close()
    print("Run complete. Calls used this run: {}".format(calls_used_this_run))


if __name__ == "__main__":
    main()
