"""
Bracket Board - Phase 3: Return Leaderboards
---------------------------------------------
Computes percentage-return leaderboards per size bracket over multiple
time windows, using:
  - wallet_daily snapshots (from the sweep) for start/end values
  - the events table (from the collector) to adjust for deposits and
    withdrawals (modified Dietz method), so topping up a wallet does
    not masquerade as performance

Windows mature automatically as history accumulates; immature windows
show a countdown instead of misleading numbers.

Skill filters applied to every board:
  - subnet owners excluded (operators, not investors)
  - baseline portfolio >= 1 TAO
  - activity requirement: position in >= 2 subnets OR more than 3 trades
    in the window (single lucky-bet wallets are suppressed)

Every board is recomputed from scratch on each run, so a wallet that
starts losing drops down or off it, and a newly discovered wallet that
performs well appears as soon as it has enough snapshot history to fill
the window. There is no sticky incumbent list.
"""

from datetime import date, timedelta

WINDOWS = [
    ("Since tracking began", None),
    ("7 days", 7),
    ("14 days", 14),
    ("30 days", 30),
    ("90 days", 90),
    ("6 months", 182),
    ("1 year", 365),
]
TOLERANCE_DAYS = 3       # snapshot may be up to this much older than target
MIN_BASELINE_TAO = 1.0
TOP_N = 10
MIN_SUBNETS = 2          # spread across subnets, OR...
MIN_TRADES = 4           # ...more than 3 trades in the window


def _snapshot_at_or_before(conn, day_iso):
    """coldkey -> (day, total_tao): latest snapshot on/before day_iso."""
    rows = conn.execute("""
        SELECT wd.coldkey, wd.day, wd.total_tao FROM wallet_daily wd
        JOIN (SELECT coldkey, MAX(day) AS d FROM wallet_daily
              WHERE day <= ? GROUP BY coldkey) m
          ON m.coldkey = wd.coldkey AND m.d = wd.day""", (day_iso,)).fetchall()
    return {ck: (d, v) for ck, d, v in rows}


def _current_values(conn):
    return {ck: (v or 0.0, subs) for ck, v, subs in conn.execute(
        "SELECT coldkey, SUM(balance_tao), COUNT(DISTINCT netuid) "
        "FROM holders GROUP BY coldkey")}


def _flows_since(conn, day_iso):
    """coldkey -> (net_contribution_tao, trade_count) from events feed.
    BUY adds external TAO into the tracked portfolio; SELL removes it."""
    rows = conn.execute("""
        SELECT coldkey,
               SUM(CASE WHEN action='BUY' THEN tao_amount
                        WHEN action='SELL' THEN -tao_amount ELSE 0 END),
               COUNT(*)
        FROM events WHERE substr(timestamp,1,10) >= ?
        GROUP BY coldkey""", (day_iso,)).fetchall()
    return {ck: (c or 0.0, n) for ck, c, n in rows}


def _events_coverage_start(conn):
    row = conn.execute("SELECT MIN(substr(timestamp,1,10)) FROM events").fetchone()
    return row[0] if row and row[0] else None


def _owners(conn):
    return {r[0] for r in conn.execute(
        "SELECT owner_ss58 FROM subnets WHERE owner_ss58 != ''")}


def compute_board(conn, window_days, brackets, bracket_of):
    """Returns (status, per_bracket) where per_bracket maps label -> rows.
    Row: (coldkey, ret_pct, pnl_tao, value_now, subnets, trades, adjusted)"""
    today = date.today()
    first_row = conn.execute("SELECT MIN(day) FROM wallet_daily").fetchone()
    first_day = first_row[0] if first_row else None
    if not first_day:
        return ("no-data", {})

    if window_days is None:
        target = first_day
    else:
        target_date = today - timedelta(days=window_days)
        if date.fromisoformat(first_day) > target_date + timedelta(days=TOLERANCE_DAYS):
            days_more = (date.fromisoformat(first_day)
                         + timedelta(days=window_days) - today).days
            return ("maturing:{}".format(max(days_more, 1)), {})
        target = target_date.isoformat()

    baselines = _snapshot_at_or_before(conn, target)
    if len(baselines) < 2:
        return ("no-data", {})
    current = _current_values(conn)
    flows = _flows_since(conn, target)
    cov_start = _events_coverage_start(conn)
    flows_cover_window = bool(cov_start and cov_start <= target)
    owners = _owners(conn)

    per_bracket = {label: [] for label, _, _ in brackets}
    for ck, (bday, v0) in baselines.items():
        if ck in owners or v0 < MIN_BASELINE_TAO:
            continue
        v1, subs = current.get(ck, (0.0, 0))
        contrib, trades = flows.get(ck, (0.0, 0))
        if subs < MIN_SUBNETS and trades < MIN_TRADES:
            continue
        adjusted = flows_cover_window
        c = contrib if adjusted else 0.0
        denom = v0 + max(c, 0.0) / 2.0
        if denom <= 0:
            continue
        pnl = v1 - v0 - c
        ret = 100.0 * pnl / denom
        if ret > 10000:      # data glitch guard
            continue
        label = bracket_of(v1 if v1 > 0 else v0)
        if label:
            per_bracket[label].append((ck, ret, pnl, v1, subs, trades, adjusted))

    for label in per_bracket:
        per_bracket[label] = sorted(per_bracket[label], key=lambda r: -r[1])[:TOP_N]
    return ("ok", per_bracket)


def render_html(conn, brackets, bracket_of, esc):
    parts = ["<h1 style='font-size:1.2em;margin-top:2em'>Performance Leaderboards</h1>",
             "<div class='note'>Percentage return per wallet, ranked within size "
             "brackets. Adjusted for deposits/withdrawals where the event feed covers "
             "the window (&#10003; in Adj column); otherwise raw balance change. "
             "Skill filters: subnet owners excluded, baseline &ge; 1 TAO, and "
             "&ge;2 subnets or &gt;3 trades required. Rebuilt from scratch every "
             "run, so wallets that start losing drop off and newly discovered "
             "wallets appear as soon as they have enough history. Past performance "
             "is not a prediction. Not financial advice.</div>"]
    for wname, wdays in WINDOWS:
        status, boards = compute_board(conn, wdays, brackets, bracket_of)
        parts.append("<h2>{}</h2>".format(esc(wname)))
        if status == "no-data":
            parts.append("<div class='meta'>Waiting for first snapshots - "
                         "completes with the first full sweep pass.</div>")
            continue
        if status.startswith("maturing:"):
            parts.append("<div class='meta'>Maturing - approximately {} more day(s) "
                         "of history needed.</div>".format(status.split(":")[1]))
            continue
        any_rows = False
        for label, _, _ in brackets:
            rows = boards.get(label, [])
            if not rows:
                continue
            any_rows = True
            parts.append("<h3 style='font-size:.95em;color:#c9a9e8;margin:1em 0 0'>"
                         "Bracket {} TAO</h3>".format(label))
            parts.append("<table><tr><th>#</th><th>Wallet</th><th class='num'>Return</th>"
                         "<th class='num'>P&amp;L (TAO)</th><th class='num'>Now (TAO)</th>"
                         "<th class='num'>Subnets</th><th class='num'>Trades</th>"
                         "<th>Adj</th></tr>")
            for i, (ck, ret, pnl, v1, subs, trades, adj) in enumerate(rows, 1):
                parts.append(
                    "<tr><td>{}</td><td><a href='https://taostats.io/account/{}'>{}</a></td>"
                    "<td class='num'>{:+.2f}%</td><td class='num'>{:+.3f}</td>"
                    "<td class='num'>{:.2f}</td><td class='num'>{}</td>"
                    "<td class='num'>{}</td><td>{}</td></tr>".format(
                        i, esc(ck), esc(ck[:10] + "..." + ck[-6:]),
                        ret, pnl, v1, subs, trades, "&#10003;" if adj else "raw"))
            parts.append("</table>")
        if not any_rows:
            parts.append("<div class='meta'>No wallets pass the skill filters in "
                         "this window yet.</div>")
    return "".join(parts)
