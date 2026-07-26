"""
Bracket Board - closed-trade matching
--------------------------------------
Turns the raw event feed into completed round trips: this alpha was bought
here, sold there, for this profit, having been held this long. Written to the
database so every window can query it instead of re-deriving it.

Why lots rather than an average cost. An average tells you what a sale made
but not what it was: a sale of alpha bought this morning and a sale of alpha
bought in March both come out as one blended number. A seven-day board built
on that credits the week with a gain that took five months, which is the
opposite of what a seven-day board is for. Matching first-in-first-out keeps
each parcel's purchase date attached to the sale that closed it, so a window
can ask the question it actually means:

    realised in the window  - sold here, bought whenever
    round trip in the window - bought AND sold here

Both are legitimate and they answer different questions. Neither is inferred;
both come from the trade record alone.

Positions are keyed on (coldkey, netuid, hotkey), because that is what a
balance is keyed on. Matching at subnet level would let a sale on one
validator close a purchase made on another.
"""

import os
from datetime import datetime, timezone

import sweep
import dashboard

MIN_ALPHA = float(os.environ.get("TRADES_MIN_ALPHA", "1e-9"))


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS closed_trade(
        coldkey TEXT, netuid INTEGER, hotkey TEXT,
        bought_at TEXT, sold_at TEXT, alpha REAL,
        buy_price REAL, sell_price REAL,
        cost_tao REAL, proceeds_tao REAL, pnl_tao REAL, pct REAL,
        held_days INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ct_sold ON closed_trade(sold_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ct_ck ON closed_trade(coldkey, sold_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ct_rt ON closed_trade(bought_at, sold_at)")


def _day(ts):
    return str(ts)[:10]


def match_wallet(rows):
    """FIFO-match one wallet's events into closed trades.

    rows: (timestamp, netuid, hotkey, action, tao, alpha, price, is_transfer)
    in ascending time order, wash pairs already removed by the caller.
    """
    lots = {}          # (netuid, hotkey) -> [[alpha_left, price, timestamp], ...]
    out = []
    for ts, netuid, hotkey, action, tao, alpha, price, transfer in rows:
        if not alpha or alpha <= MIN_ALPHA:
            continue
        key = (netuid, hotkey)
        book = lots.setdefault(key, [])
        unit = (tao / alpha) if (tao and alpha) else (price or 0.0)
        if action == "BUY":
            # A transfer in is not a purchase decision, but the alpha is real
            # and has to carry a cost or the sale that closes it books the
            # whole proceeds as profit. Value it at the market at arrival.
            book.append([alpha, unit or (price or 0.0), ts])
        elif action == "SELL":
            left = alpha
            while left > MIN_ALPHA and book:
                lot = book[0]
                take = min(left, lot[0])
                lot[0] -= take
                left -= take
                if not transfer and price and lot[1]:
                    cost = take * lot[1]
                    proceeds = take * price
                    held = (datetime.fromisoformat(_day(ts))
                            - datetime.fromisoformat(_day(lot[2]))).days
                    out.append((netuid, hotkey, lot[2], ts, take, lot[1], price,
                                cost, proceeds, proceeds - cost,
                                100.0 * (proceeds - cost) / cost if cost else 0.0,
                                held))
                if lot[0] <= MIN_ALPHA:
                    book.pop(0)
            # Selling more than we ever saw bought means the buy predates our
            # history. Dropped rather than valued at zero cost, which would
            # invent a profit equal to the entire proceeds.
    return out


def rebuild(conn, verbose=True):
    ensure_tables(conn)
    conn.execute("DELETE FROM closed_trade")
    wallets = [r[0] for r in conn.execute(
        "SELECT DISTINCT coldkey FROM events")]
    total = 0
    batch = []
    for ck in wallets:
        raw = conn.execute(
            "SELECT timestamp, netuid, action, tao_amount, alpha_amount, price, "
            "COALESCE(is_transfer,0), hotkey FROM events WHERE coldkey=? "
            "ORDER BY timestamp", (ck,)).fetchall()
        if not raw:
            continue
        wash = dashboard._wash_pairs([(r[0], r[1], r[2], r[3], r[4], r[5], r[6])
                                      for r in raw])
        rows = [(r[0], r[1], r[7], r[2], r[3], r[4], r[5], r[6])
                for i, r in enumerate(raw) if i not in wash]
        for t in match_wallet(rows):
            batch.append((ck,) + t)
        if len(batch) >= 5000:
            conn.executemany(
                "INSERT INTO closed_trade(coldkey,netuid,hotkey,bought_at,sold_at,"
                "alpha,buy_price,sell_price,cost_tao,proceeds_tao,pnl_tao,pct,"
                "held_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            total += len(batch)
            batch = []
    if batch:
        conn.executemany(
            "INSERT INTO closed_trade(coldkey,netuid,hotkey,bought_at,sold_at,"
            "alpha,buy_price,sell_price,cost_tao,proceeds_tao,pnl_tao,pct,"
            "held_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        total += len(batch)
    conn.commit()
    if verbose:
        print("Closed trades matched: {:,} across {:,} wallets".format(
            total, len(wallets)))
        row = conn.execute(
            "SELECT COUNT(*), ROUND(AVG(held_days),1), ROUND(SUM(pnl_tao),2) "
            "FROM closed_trade").fetchone()
        print("   average holding period {} days, net {} TAO".format(
            row[1], row[2]))
    return total


def window_performance(conn, since_iso, round_trip_only=False):
    """coldkey -> (banked_tao, cost_tao, n_trades) for a window.

    round_trip_only restricts to parcels bought AND sold inside the window -
    what the wallet actually did in this period, rather than what it happened
    to close here after holding since March.
    """
    clause = "sold_at >= ?" + (" AND bought_at >= ?" if round_trip_only else "")
    args = (since_iso, since_iso) if round_trip_only else (since_iso,)
    return {ck: (pnl or 0.0, cost or 0.0, n) for ck, pnl, cost, n in conn.execute(
        "SELECT coldkey, SUM(pnl_tao), SUM(cost_tao), COUNT(*) FROM closed_trade "
        "WHERE " + clause + " GROUP BY coldkey", args)}


def main():
    conn = sweep.db()
    rebuild(conn)
    conn.close()


if __name__ == "__main__":
    main()
