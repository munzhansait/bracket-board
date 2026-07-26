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
  - alpha buyers only: a wallet that never bought alpha is a miner,
    validator or emission recipient, not an investor
  - root network (netuid 0) ignored: no alpha exposure, no dTAO risk
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
THINNED_AFTER_DAYS = 100  # beyond here snapshots are thinned to weekly...
THINNED_TOLERANCE_DAYS = 8   # ...so allow a wider baseline window there
MIN_BASELINE_TAO = 1.0
TOP_N = 10
MIN_SUBNETS = 2          # spread across subnets, OR...
MIN_TRADES = 4           # ...more than 3 trades in the window


def _snapshot_at_or_before(conn, day_iso, max_age_days):
    """coldkey -> (day, total_tao): latest snapshot on/before day_iso, but no
    older than max_age_days before it.

    Without the lower bound a wallet with a gap in its history would have a
    months-old balance used as its "30 days ago" value, turning ordinary
    growth since then into a spectacular fake return.
    """
    floor_iso = (date.fromisoformat(day_iso)
                 - timedelta(days=max_age_days)).isoformat()
    rows = conn.execute("""
        SELECT wd.coldkey, wd.day, wd.total_tao FROM wallet_daily wd
        JOIN (SELECT coldkey, MAX(day) AS d FROM wallet_daily
              WHERE day <= ? AND day >= ? GROUP BY coldkey) m
          ON m.coldkey = wd.coldkey AND m.d = wd.day""",
        (day_iso, floor_iso)).fetchall()
    return {ck: (d, v) for ck, d, v in rows}


def _current_values(conn):
    """Alpha positions only. netuid 0 is the root network: staking there
    carries no alpha exchange-rate risk and is not dTAO investing, so it
    must not count towards an investor's portfolio or subnet spread."""
    return {ck: (v or 0.0, subs) for ck, v, subs in conn.execute(
        "SELECT coldkey, SUM(balance_tao), COUNT(DISTINCT netuid) "
        "FROM holders WHERE netuid != 0 GROUP BY coldkey")}


def _alpha_buyers(conn):
    """Coldkeys that have actually bought alpha at least once.

    Miners and validators accumulate alpha from emissions without ever
    buying any. Their balance growth measures operating a subnet, not
    investing in one, so they must not be ranked against investors. A
    wallet that never appears as a buyer is not a dTAO investor, whatever
    its balance says.
    """
    return {ck for (ck,) in conn.execute(
        "SELECT DISTINCT coldkey FROM events "
        "WHERE action='BUY' AND COALESCE(is_transfer,0)=0")}


def _flows_since(conn, day_iso):
    """coldkey -> (net_contribution_tao, trade_count) from events feed.

    BUY adds external TAO into the tracked portfolio; SELL removes it. Both
    count as flows, including stake transfers between wallets - value really
    did move. Transfers are excluded from the trade count though, because
    moving your own stake is not a trading decision and must not buy a wallet
    a place on the board.
    """
    # The sweep renders this board but does not own the events schema, so on a
    # database where the collector has not yet added is_transfer, count every
    # event rather than failing the whole leaderboard.
    have_flag = "is_transfer" in set(
        r[1] for r in conn.execute("PRAGMA table_info(events)"))
    trades_expr = ("SUM(CASE WHEN COALESCE(is_transfer,0)=0 THEN 1 ELSE 0 END)"
                   if have_flag else "COUNT(*)")
    rows = conn.execute("""
        SELECT coldkey,
               SUM(CASE WHEN action='BUY' THEN tao_amount
                        WHEN action='SELL' THEN -tao_amount ELSE 0 END),
               {}
        FROM events WHERE substr(timestamp,1,10) >= ?
        GROUP BY coldkey""".format(trades_expr), (day_iso,)).fetchall()
    return {ck: (c or 0.0, n or 0) for ck, c, n in rows}


def _events_coverage_by_wallet(conn):
    """coldkey -> earliest event date held for that wallet.

    Per wallet, not global: one wallet's deep history said nothing about
    another's, so a wallet whose events only reach back a week was being
    treated as fully flow-adjusted over a year.
    """
    return {ck: d for ck, d in conn.execute(
        "SELECT coldkey, MIN(substr(timestamp,1,10)) FROM events GROUP BY coldkey")
        if d}


def sweep_pass_completed(conn):
    """True once a full sweep pass has finished at least once.

    Until then the holders table only covers the subnets crawled so far, so a
    wallet's 'now' total omits its positions in subnets not yet reached.
    """
    row = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
    try:
        return bool(row) and int(row[0]) > 1
    except (TypeError, ValueError):
        return False


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

    # Snapshots older than ~100 days are thinned to one per week, so an exact
    # 3-day tolerance would wrongly drop every wallet on the long windows.
    age = (today - date.fromisoformat(target)).days
    max_age = TOLERANCE_DAYS if age <= THINNED_AFTER_DAYS else THINNED_TOLERANCE_DAYS

    baselines = _snapshot_at_or_before(conn, target, max_age)
    if len(baselines) < 2:
        return ("no-data", {})
    current = _current_values(conn)
    flows = _flows_since(conn, target)
    coverage = _events_coverage_by_wallet(conn)
    owners = _owners(conn)
    buyers = _alpha_buyers(conn)

    per_bracket = {label: [] for label, _, _ in brackets}
    for ck, (bday, v0) in baselines.items():
        if ck in owners or v0 < MIN_BASELINE_TAO:
            continue
        if ck not in buyers:
            continue  # never bought alpha - operator or emission recipient
        v1, subs = current.get(ck, (0.0, 0))
        contrib, trades = flows.get(ck, (0.0, 0))
        if subs < MIN_SUBNETS and trades < MIN_TRADES:
            continue
        # Only trust the flow adjustment if this wallet's own events reach
        # back past the window start.
        cov = coverage.get(ck)
        adjusted = bool(cov and cov <= target)
        if not adjusted and abs(contrib) > 0:
            # Known flows inside the window but incomplete history before it:
            # the raw number would read a deposit as performance. Drop it
            # rather than publish a figure that flatters the wallet.
            continue
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
             "Only wallets that have actually bought alpha are ranked - miners, "
             "validators and subnet owners accumulate alpha from emissions "
             "rather than investing, and root-network stake is ignored entirely. "
             "Further filters: baseline &ge; 1 TAO, and "
             "&ge;2 subnets or &gt;3 trades required. Rebuilt from scratch every "
             "run, so wallets that start losing drop off and newly discovered "
             "wallets appear as soon as they have enough history. Past performance "
             "is not a prediction. Not financial advice.</div>"]
    if not sweep_pass_completed(conn):
        parts.append(
            "<div class='note' style='border:1px solid #b46;padding:.6em'>"
            "<b>Provisional.</b> No full sweep pass has finished yet, so each "
            "wallet's current value only counts the subnets crawled so far. "
            "Returns below understate any wallet holding positions in subnets "
            "not yet reached. Treat the ranking as indicative until this "
            "notice disappears.</div>")
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
