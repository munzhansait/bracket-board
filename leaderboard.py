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
MAX_STEP_RATIO = 2.0     # a step that doubles in a day is a cash flow, not a gain
MIN_STEPS = 3            # need a few linked snapshots to chain a return
MAX_DROPPED_SHARE = 0.25  # too many unexplained jumps -> do not rank at all
MIN_STEP_TAO = 1.0       # ignore growth measured off a near-empty wallet
MAX_PNL_VS_PEAK = 3.0    # profit far beyond the largest balance ever held is
                         # arithmetically impossible without unseen deposits
MIN_WINDOW_TAO = 1.0     # a wallet that emptied out mid-window is not ranked
MAX_PLAUSIBLE_RETURN = 2000.0   # above this it is an artefact, not a trader
MIN_RISKED_TAO = 1.0     # closed trades must have tied up at least this much
MAX_GAP_DAYS = 2         # a hole in the daily history disqualifies the window
MAX_FLOW_VS_START = 2.0  # more money moving than invested -> no honest return
METHOD_TOLERANCE_PP = 2.0    # the two methods may differ by this many points...
METHOD_TOLERANCE_REL = 0.10  # ...or this share of the figure, whichever is larger


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


# wallet_daily is reconstructed from the positions in holders, so it only
# covers subnets the sweep has actually crawled. events cover every subnet the
# wallet traded. Mixing the two subtracts whole-portfolio flows from a partial
# portfolio: one wallet showed 25.9 TAO of net sales against a 39 TAO balance
# that never moved, and the chain read +200% for a week that gained 3.7%.
# Every flow query is therefore restricted to the subnets we actually track.
# Matched on hotkey as well as subnet. A wallet moving stake from one validator
# to another writes a sell on the old hotkey and a buy on the new one; if only
# the new hotkey is tracked, the sell never touches our balance while the buy
# adds to it. Keyed on subnet alone the pair netted to zero, so a 1.005 TAO
# validator switch read as a 15% day and published an 8 TAO wallet at +18.56%
# when its subnet had moved 3.97%. Balances are per position, so flows must be.
TRACKED_SUBNET = ("EXISTS (SELECT 1 FROM holders h "
                  "WHERE h.coldkey = e.coldkey AND h.netuid = e.netuid "
                  "AND h.hotkey = e.hotkey)")


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
        SELECT e.coldkey,
               SUM(CASE WHEN e.action='BUY' THEN e.tao_amount
                        WHEN e.action='SELL' THEN -e.tao_amount ELSE 0 END),
               {}
        FROM events e WHERE substr(e.timestamp,1,10) >= ? AND {}
        GROUP BY e.coldkey""".format(
            trades_expr.replace("is_transfer", "e.is_transfer"), TRACKED_SUBNET),
        (day_iso,)).fetchall()
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


def _daily_series(conn, since_iso):
    """coldkey -> [(day, total_tao), ...] ascending, from since_iso onwards."""
    series = {}
    for ck, day, tao in conn.execute(
            "SELECT coldkey, day, total_tao FROM wallet_daily WHERE day >= ? "
            "ORDER BY coldkey, day", (since_iso,)):
        series.setdefault(ck, []).append((day, tao or 0.0))
    return series


def _flows_by_day(conn, since_iso):
    """(coldkey, day) -> net TAO in (+) or out (-) recorded by the feed."""
    out = {}
    for ck, day, net in conn.execute(
            "SELECT e.coldkey, substr(e.timestamp,1,10), "
            "SUM(CASE WHEN e.action='BUY' THEN e.tao_amount "
            "         WHEN e.action='SELL' THEN -e.tao_amount ELSE 0 END) "
            "FROM events e WHERE substr(e.timestamp,1,10) >= ? AND " + TRACKED_SUBNET +
            " GROUP BY e.coldkey, substr(e.timestamp,1,10)", (since_iso,)):
        out[(ck, day)] = net or 0.0
    return out


def time_weighted_return(series, flows, ck):
    """Chain day-over-day growth, discarding steps that are really cash flows.

    Modified Dietz needs every deposit and withdrawal, and this feed does not
    provide them: a stake transfer is recorded against the sender's coldkey,
    so money arriving in a wallet is invisible on that wallet. One observed
    wallet jumped 4.92 -> 2400.38 TAO overnight with 53 TAO of recorded flow,
    and Dietz scored it +8574%.

    So subtract the flows we do know about, and treat any step that still
    moves more than MAX_STEP_RATIO as an unrecorded flow rather than as
    performance - dropping that step from the chain instead of believing it.
    Returns (return_fraction, pnl_tao, steps_used, steps_dropped).
    """
    growth, pnl, used, dropped = 1.0, 0.0, 0, 0
    for i in range(1, len(series)):
        _d0, v0 = series[i - 1]
        d1, v1 = series[i]
        # A percentage measured off a near-empty wallet is noise: one wallet
        # emptied to 0.12 TAO and rebuilt, and chaining those steps scored it
        # +2289% off moves worth a fraction of a TAO.
        if v0 < MIN_STEP_TAO:
            dropped += 1
            continue
        ratio = (v1 - flows.get((ck, d1), 0.0)) / v0
        if ratio <= 0 or ratio > MAX_STEP_RATIO or ratio < 1.0 / MAX_STEP_RATIO:
            dropped += 1
            continue
        growth *= ratio
        pnl += v0 * (ratio - 1.0)
        used += 1
    return growth - 1.0, pnl, used, dropped


def _traded_vs_tracked(conn, day_iso):
    """coldkey -> subnets it traded in the window but that we do not track.

    The daily value series only covers subnets present in holders. If a wallet
    traded somewhere we do not track, its published return describes a fraction
    of its portfolio while reading as the whole thing. That is the failure that
    published a 3.71% week as +200.56%, so those wallets are not ranked at all.
    """
    tracked = {}
    for ck, n, hk in conn.execute("SELECT coldkey, netuid, hotkey FROM holders"):
        tracked.setdefault(ck, set()).add((n, hk))
    missing = {}
    for ck, n, hk in conn.execute(
            "SELECT DISTINCT coldkey, netuid, hotkey FROM events "
            "WHERE substr(timestamp,1,10) >= ?", (day_iso,)):
        if (n, hk) not in tracked.get(ck, ()):
            missing.setdefault(ck, set()).add((n, hk))
    return missing


def _realised(conn, ck, since_iso):
    """TAO actually banked by selling inside the window.

    Uses the same cost-basis walk the wallet page prints, so the column and
    the trade list can never tell different stories.
    """
    try:
        import dashboard
        return sum(t["pnl"] or 0.0
                   for t in dashboard.realized_trades(conn, ck, since_iso))
    except Exception:
        return 0.0


def _window_flows(conn, day_iso):
    """coldkey -> (bought_tao, sold_tao, transferred_net_tao) inside the window.

    Kept separate so the page can show a value bridge a reader can add up,
    rather than a single net number they have to trust.
    """
    out = {}
    for ck, action, transfer, total in conn.execute(
            "SELECT e.coldkey, e.action, COALESCE(e.is_transfer,0), "
            "SUM(e.tao_amount) FROM events e "
            "WHERE substr(e.timestamp,1,10) >= ? AND " + TRACKED_SUBNET +
            " GROUP BY e.coldkey, e.action, COALESCE(e.is_transfer,0)", (day_iso,)):
        b, s, t = out.get(ck, (0.0, 0.0, 0.0))
        amt = total or 0.0
        if transfer:
            t += amt if action == "BUY" else -amt
        elif action == "BUY":
            b += amt
        elif action == "SELL":
            s += amt
        out[ck] = (b, s, t)
    return out


def audit(ck, series, per_day, flows_win, untracked, coverage, target,
          owners, buyers, subs, trades, qualified=None):
    """Every reason this wallet must not be published for this window.

    Returns (reasons, record). An empty reason list is the only thing that
    earns a place on the board. Publishing a wrong number is worse than
    publishing nothing, so each check below suppresses rather than adjusts.
    """
    reasons = []
    # The qualification engine's verdict comes first: it corroborates each
    # wallet's balance against what its subnet actually traded at, which is
    # the only check here that uses evidence from outside the wallet itself.
    if qualified is not None and ck not in qualified:
        reasons.append("balance does not match the market price of what it holds")
    if ck in owners:
        reasons.append("subnet owner")
    if ck not in buyers:
        reasons.append("never bought alpha")
    if untracked:
        reasons.append("traded in a position we do not track")
    cov = coverage.get(ck)
    if not cov or cov > target:
        reasons.append("trade history starts after the window")
    if len(series) < MIN_STEPS + 1:
        return reasons + ["not enough daily snapshots"], None

    values = [v for _d, v in series]
    days = [date.fromisoformat(d) for d, _v in series]
    if max((days[i] - days[i - 1]).days for i in range(1, len(days))) > MAX_GAP_DAYS:
        reasons.append("gap in the daily history")
    if min(values) < MIN_WINDOW_TAO:
        reasons.append("wallet emptied out mid-window")

    start, end = values[0], values[-1]
    bought, sold, moved = flows_win.get(ck, (0.0, 0.0, 0.0))
    net_in = bought - sold + moved
    gain = end - start - net_in            # exact arithmetic, no modelling

    # Two independent measures. The bridge above is plain subtraction; the
    # chain below compounds daily moves. They answer the same question by
    # different routes, so a disagreement means the inputs are inconsistent
    # and neither number can be trusted.
    twr_frac, _pnl, used, dropped = time_weighted_return(series, per_day, ck)
    if dropped:
        reasons.append("balance moved without a matching trade")
    if used < MIN_STEPS:
        reasons.append("too few usable days")

    base = start + max(net_in, 0.0) / 2.0
    if base <= 0:
        return reasons + ["no capital at risk"], None
    dietz_pct = 100.0 * gain / base
    twr_pct = 100.0 * twr_frac

    if abs(net_in) > MAX_FLOW_VS_START * start:
        reasons.append("more money moved than was invested")
    if abs(dietz_pct - twr_pct) > max(METHOD_TOLERANCE_PP,
                                      abs(dietz_pct) * METHOD_TOLERANCE_REL):
        reasons.append("two return methods disagree")
    if abs(dietz_pct) > MAX_PLAUSIBLE_RETURN:
        reasons.append("return outside any plausible range")
    # Activity is deliberately NOT a gate. A quiet wallet's return is no less
    # correct than a busy one's, and suppressing it would be withholding a true
    # figure rather than a doubtful one. The trade count is published beside
    # every row so a reader can weigh it themselves.

    return reasons, {
        "start": start, "end": end, "start_day": series[0][0],
        "end_day": series[-1][0], "bought": bought, "sold": sold,
        "moved": moved, "gain": gain, "ret": dietz_pct, "twr": twr_pct,
    }


def realised_board(conn, window_days, brackets, bracket_of):
    """Wallets ranked on money actually banked by selling, inside the window.

    A deliberately different standard from the total-return board, because the
    two measures depend on different evidence. Total return needs a trustworthy
    balance series, which means every position must be tracked - and that rule
    excludes precisely the wallets that trade most, since an active trader is
    the one most likely to touch a subnet the crawl has not reached. Ranking
    banked money by that standard left 739 wallets with priced sales unable to
    appear on any board.

    Realised profit needs none of it. What a sale banked is the price it went
    out at against the average price paid, both of which come from the trade
    record alone; no balance, no coverage of other subnets, nothing inferred.
    So this board asks only for a complete trade history and priced sales, and
    measures return against the capital those closed trades actually tied up.
    """
    from collections import Counter
    if not window_days:
        return ("no-data", {}, {})
    target = (date.today() - timedelta(days=window_days)).isoformat()

    coverage = _events_coverage_by_wallet(conn)
    owners = _owners(conn)
    buyers = _alpha_buyers(conn)
    current = _current_values(conn)
    try:
        import dashboard
    except Exception:
        return ("no-data", {}, {})

    # Round trips only: parcels bought AND sold inside the window. A sale here
    # of alpha bought in March is a real gain, but it is not something the
    # wallet did in these days, and crediting the window with it is what makes
    # a seven-day board meaningless - across the data only 32% of trades closed
    # in a 7-day window were also opened in it.
    try:
        import trades as _trades
        perf = _trades.window_performance(conn, target, round_trip_only=True)
        held = {ck: (d or 0) for ck, d in conn.execute(
            "SELECT coldkey, AVG(held_days) FROM closed_trade "
            "WHERE sold_at >= ? AND bought_at >= ? GROUP BY coldkey",
            (target, target))}
    except Exception:
        return ("no-data", {}, {})
    active = list(perf)

    per_bracket = {label: [] for label, _, _ in brackets}
    exceptions = Counter()
    for ck in active:
        if ck in owners:
            exceptions["subnet owner"] += 1
            continue
        if ck not in buyers:
            exceptions["never bought alpha"] += 1
            continue
        cov = coverage.get(ck)
        if not cov or cov > target:
            exceptions["trade history starts after the window"] += 1
            continue
        banked, risked, nclosed = perf[ck]
        if not nclosed:
            exceptions["no round trip completed in the window"] += 1
            continue
        if risked < MIN_RISKED_TAO:
            # A percentage off a fraction of a TAO is arithmetic, not skill:
            # +163% on 0.02 TAO risked is two pence and would top the board.
            exceptions["too little capital at risk to rank"] += 1
            continue
        v1, subs = current.get(ck, (0.0, 0))
        label = bracket_of(v1)
        if not label:
            exceptions["outside every size bracket"] += 1
            continue
        per_bracket[label].append({
            "ck": ck, "realised": banked, "risked": risked,
            "realised_pct": 100.0 * banked / risked, "sells": nclosed,
            "held": round(held.get(ck, 0), 1),
            "v": v1, "subs": subs, "trades": nclosed, "target": target,
        })

    for label in per_bracket:
        per_bracket[label] = sorted(
            per_bracket[label], key=lambda r: -r["realised_pct"])[:TOP_N]
    return ("ok", per_bracket, dict(exceptions))


def compute_board(conn, window_days, brackets, bracket_of):
    status, per_bracket, _ex = audited_board(conn, window_days, brackets, bracket_of)
    return status, per_bracket


def audited_board(conn, window_days, brackets, bracket_of):
    """Returns (status, per_bracket, exceptions).

    per_bracket maps label -> list of records that passed every check.
    exceptions counts why the rest were withheld, so the page can say what it
    is not showing instead of quietly dropping wallets.
    """
    from collections import Counter
    today = date.today()
    first_row = conn.execute("SELECT MIN(day) FROM wallet_daily").fetchone()
    first_day = first_row[0] if first_row else None
    if not first_day:
        return ("no-data", {}, {})

    if window_days is None:
        target = first_day
    else:
        target_date = today - timedelta(days=window_days)
        if date.fromisoformat(first_day) > target_date + timedelta(days=TOLERANCE_DAYS):
            days_more = (date.fromisoformat(first_day)
                         + timedelta(days=window_days) - today).days
            return ("maturing:{}".format(max(days_more, 1)), {}, {})
        target = target_date.isoformat()

    # Snapshots older than ~100 days are thinned to one per week, so an exact
    # 3-day tolerance would wrongly drop every wallet on the long windows.
    age = (today - date.fromisoformat(target)).days
    max_age = TOLERANCE_DAYS if age <= THINNED_AFTER_DAYS else THINNED_TOLERANCE_DAYS

    baselines = _snapshot_at_or_before(conn, target, max_age)
    if len(baselines) < 2:
        return ("no-data", {}, {})
    current = _current_values(conn)
    flows = _flows_since(conn, target)
    series_all = _daily_series(conn, target)
    per_day = _flows_by_day(conn, target)
    flows_win = _window_flows(conn, target)
    untracked = _traded_vs_tracked(conn, target)
    coverage = _events_coverage_by_wallet(conn)
    owners = _owners(conn)
    buyers = _alpha_buyers(conn)
    try:
        import qualify
        qualified = qualify.good_wallets(conn)
    except Exception:
        qualified = None      # engine unavailable: fall back to local checks

    per_bracket = {label: [] for label, _, _ in brackets}
    exceptions = Counter()
    for ck, (_bday, v0) in baselines.items():
        if v0 < MIN_BASELINE_TAO:
            exceptions["too small to rank"] += 1
            continue
        v1, subs = current.get(ck, (0.0, 0))
        _contrib, trades = flows.get(ck, (0.0, 0))
        reasons, rec = audit(ck, series_all.get(ck) or [], per_day, flows_win,
                             untracked.get(ck), coverage, target, owners,
                             buyers, subs, trades, qualified)
        if reasons or not rec:
            for r in reasons:
                exceptions[r] += 1
            continue
        label = bracket_of(v1 if v1 > 0 else v0)
        if not label:
            exceptions["outside every size bracket"] += 1
            continue
        # What the wallet actually banked by selling inside this window, and
        # that as a share of the capital it started with. Ranking on total
        # return alone put holders at the top of every board and buried anyone
        # who traded and took money off the table - 739 wallets had priced
        # sales in a 30-day window and none of them led a single bracket.
        sales = _realised(conn, ck, target)
        rec["realised"] = sales
        rec["realised_pct"] = (100.0 * sales / rec["start"]
                               if rec["start"] > 0 else 0.0)
        rec.update({"ck": ck, "v": v1, "subs": subs, "trades": trades,
                    "target": target})
        per_bracket[label].append(rec)

    # Keep the best of both rankings. A reader after the strongest holder and a
    # reader after the strongest trader are asking different questions, and
    # neither should be answered with the other's list.
    for label in per_bracket:
        rows = per_bracket[label]
        keep = {id(r): r for r in sorted(rows, key=lambda r: -r["ret"])[:TOP_N]}
        keep.update({id(r): r for r in
                     sorted(rows, key=lambda r: -r["realised_pct"])[:TOP_N]})
        per_bracket[label] = sorted(keep.values(), key=lambda r: -r["ret"])
    return ("ok", per_bracket, dict(exceptions))


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
            rows = [(r["ck"], r["ret"], r["gain"], r["v"], r["subs"],
                     r["trades"], True) for r in rows]
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
