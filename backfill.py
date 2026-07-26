"""
Bracket Board - Backfill (run during a paid taostats month)
------------------------------------------------------------
Manually-triggered tool that, for every wallet currently holding >= 1 TAO
in subnets (biggest first):
  1. Pulls its complete stake/unstake transaction history
     (/api/delegation/v1?nominator=...) into the events table.
  2. Pulls its daily balance history
     (/api/dtao/stake_balance/history/v1?coldkey=...) and reconstructs
     past wallet_daily snapshots from it.

Resumable: progress is stored per wallet, so run it as many times as
needed. Designed for the $49 Standard tier (60 calls/min, 50k/month):
the workflow sets PACE_SECONDS=1.1 and a high ceiling. On the free
tier it still works - just very slowly.
"""

import os
from datetime import datetime, timezone, date, timedelta

import sweep
import collector as coll

MIN_TAO = float(os.environ.get("BACKFILL_MIN_TAO", "1.0"))
# Page back until we pass EVENT_DAYS; MAX_EVENT_PAGES is only a runaway guard
# for wallets that trade constantly, not the normal stopping condition.
EVENT_DAYS = int(os.environ.get("BACKFILL_EVENT_DAYS", "400"))
MAX_EVENT_PAGES = int(os.environ.get("BACKFILL_MAX_EVENT_PAGES", "25"))
MAX_HISTORY_PAGES = int(os.environ.get("BACKFILL_MAX_HISTORY_PAGES", "5"))
PAGE_LIMIT = 200


def ensure_tables(conn):
    coll.ensure_tables(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS backfill_done(
        coldkey TEXT PRIMARY KEY, events_done INTEGER DEFAULT 0,
        history_done INTEGER DEFAULT 0, finished_at TEXT)""")


def candidates(conn):
    return [r[0] for r in conn.execute("""
        SELECT h.coldkey FROM holders h
        LEFT JOIN backfill_done b ON b.coldkey = h.coldkey
        WHERE b.coldkey IS NULL OR b.events_done = 0 OR b.history_done = 0
        GROUP BY h.coldkey HAVING SUM(h.balance_tao) >= ?
        ORDER BY SUM(h.balance_tao) DESC""", (MIN_TAO,))]


def backfill_events(conn, ck):
    """Pull this wallet's trade history back to EVENT_DAYS ago.

    Paging stops on date, not on a blind page count: a fixed cap silently
    truncated hyperactive wallets, and the missing deposits then read as
    performance in the long-window return maths.
    """
    cutoff = (date.today() - timedelta(days=EVENT_DAYS)).isoformat()
    page = 1
    while page <= MAX_EVENT_PAGES:
        data = sweep.api_get(conn, "/api/delegation/v1?nominator={}&limit={}&page={}"
                             .format(ck, PAGE_LIMIT, page))
        if data is None:
            return False  # budget/errors - retry next run
        items = data.get("data", [])
        oldest = None
        for item in items:
            ev = coll.parse_event(item)
            if not ev[2]:
                ev = ev[:2] + (ck,) + ev[3:]  # nominator implied
            conn.execute(
                "INSERT OR IGNORE INTO events(block_number,timestamp,coldkey,hotkey,"
                "netuid,action,tao_amount,alpha_amount,price,is_transfer,watched) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,0)", ev)
            day = str(ev[1])[:10]
            if day and (oldest is None or day < oldest):
                oldest = day
        if oldest and oldest <= cutoff:
            break  # reached far enough back; older trades cannot affect a window
        nxt = (data.get("pagination") or {}).get("next_page")
        if not nxt:
            break
        page = nxt
    return True


def latest_per_day(items):
    """Collapse balance records to ONE value per calendar day: the latest
    reading of that day.

    The endpoint returns a snapshot per block, so a single day can carry
    several records of the same balance. Adding them up would multiply that
    day's holdings, which then reads as a fake spike-and-crash in the return
    maths. Records are not guaranteed to arrive newest-first, so compare
    timestamps rather than trusting order.
    """
    best = {}
    for item in items:
        ts = str(item.get("timestamp", ""))
        day = ts[:10]
        if not day:
            continue
        tao = sweep.rao_to_tao(item.get("balance_as_tao"))
        prev = best.get(day)
        if prev is None or ts > prev[0]:
            best[day] = (ts, tao)
    return dict((day, tao) for day, (_ts, tao) in best.items())


def backfill_history(conn, ck):
    """Daily per-subnet balances -> aggregate to wallet_daily (only for
    days older than existing snapshots, so live delta data wins).

    The history endpoint requires coldkey AND hotkey AND netuid together -
    passing coldkey alone returns HTTP 400 - so walk every position this
    wallet is known to hold, taken from the holders table.
    """
    positions = conn.execute(
        "SELECT DISTINCT hotkey, netuid FROM holders WHERE coldkey=?",
        (ck,)).fetchall()
    if not positions:
        return True  # nothing known to fetch; don't retry this wallet forever
    daily = {}
    sampled = False
    for hotkey, netuid in positions:
        items = []
        page = 1
        while page <= MAX_HISTORY_PAGES:
            data = sweep.api_get(
                conn,
                "/api/dtao/stake_balance/history/v1?coldkey={}&hotkey={}"
                "&netuid={}&limit={}&page={}".format(
                    ck, hotkey, netuid, PAGE_LIMIT, page))
            if data is None:
                return False
            batch = data.get("data", [])
            if not sampled and batch:
                print("  history sample keys: {}".format(sorted(batch[0].keys())))
                sampled = True
            items.extend(batch)
            nxt = (data.get("pagination") or {}).get("next_page")
            if not nxt:
                break
            page = nxt
        # One snapshot per day for THIS position, then add positions together:
        # two hotkeys on the same subnet should sum, two readings of the same
        # hotkey on the same day should not.
        for day, tao in latest_per_day(items).items():
            daily.setdefault(day, {})
            daily[day][netuid] = daily[day].get(netuid, 0.0) + tao
    first_live = conn.execute(
        "SELECT MIN(day) FROM wallet_daily WHERE coldkey=?", (ck,)).fetchone()[0]
    for day, per_subnet in daily.items():
        if first_live and day >= first_live:
            continue  # live snapshots take precedence
        total = sum(per_subnet.values())
        if total <= 0:
            continue
        conn.execute(
            "INSERT INTO wallet_daily(day,coldkey,total_tao,subnets) VALUES(?,?,?,?) "
            "ON CONFLICT(day,coldkey) DO NOTHING",
            (day, ck, total, len(per_subnet)))
    return True


def main():
    conn = sweep.db()
    ensure_tables(conn)
    todo = candidates(conn)
    print("Backfill candidates remaining: {}".format(len(todo)))
    processed = 0
    for ck in todo:
        if sweep.calls_used_this_run >= sweep.CALLS_PER_RUN or sweep.budget_left(conn) <= 0:
            break
        row = conn.execute(
            "SELECT events_done, history_done FROM backfill_done WHERE coldkey=?",
            (ck,)).fetchone() or (0, 0)
        ev_done, hist_done = row
        if not ev_done:
            ev_done = 1 if backfill_events(conn, ck) else 0
        if ev_done and not hist_done:
            hist_done = 1 if backfill_history(conn, ck) else 0
        conn.execute(
            "INSERT INTO backfill_done(coldkey,events_done,history_done,finished_at) "
            "VALUES(?,?,?,?) ON CONFLICT(coldkey) DO UPDATE SET "
            "events_done=excluded.events_done, history_done=excluded.history_done, "
            "finished_at=excluded.finished_at",
            (ck, ev_done, hist_done,
             datetime.now(timezone.utc).isoformat() if ev_done and hist_done else None))
        conn.commit()
        if ev_done and hist_done:
            processed += 1
    print("Wallets fully backfilled this run: {}. Calls used: {}."
          .format(processed, sweep.calls_used_this_run))
    sweep.build_dashboard(conn)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
