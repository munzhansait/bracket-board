"""
Bracket Board - data qualification engine
------------------------------------------
Decides which records are fit to publish, before anything is rendered. The
dashboard is a view; this is the control. Nothing here formats anything and
nothing in the renderer decides what is true.

The method is independent corroboration rather than internal consistency.
Every wallet's daily balance is predicted from two things we know separately -
what its subnet's alpha traded at that day, and what the wallet moved in or
out - and compared against the balance actually recorded. Agreement means the
record stands up. A residual nobody can account for means something is wrong
with the data, whatever the arithmetic says, and the wallet is quarantined.

That is the check that catches defects self-consistency cannot. A wallet was
published at +18.56% for a week its subnet moved 3.97%; every internal sum
tied out, because the fault was in the inputs. Reconstructing the balance from
the market price exposes it immediately.

Verdicts are written to the database, never deleted from it. Source events and
balances stay exactly as the API gave them: a record is marked unfit, with the
reason and the size of the discrepancy, so that any exclusion can be audited,
re-run under different thresholds, or reversed when the underlying gap is
filled. Destroying data to make a report look clean is how you lose the
ability to prove the report was ever right.
"""

import os
from datetime import datetime, timezone

import sweep

# A day's price is only usable if enough independent trades set it.
MIN_PRICE_SAMPLES = int(os.environ.get("QUALIFY_MIN_SAMPLES", "5"))
# How far a day's balance may sit from its predicted value, as a share of the
# opening balance, before that day is called unexplained.
RESIDUAL_TOLERANCE = float(os.environ.get("QUALIFY_TOLERANCE", "0.05"))
# Days below this are ignored: percentage residuals on dust are meaningless.
MIN_BALANCE_TAO = float(os.environ.get("QUALIFY_MIN_BALANCE", "1.0"))
# A wallet is quarantined once this share of its checked days fail.
MAX_BAD_SHARE = float(os.environ.get("QUALIFY_MAX_BAD_SHARE", "0.02"))


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS subnet_price(
        netuid INTEGER, day TEXT, price REAL, samples INTEGER,
        PRIMARY KEY (netuid, day))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wallet_quality(
        coldkey TEXT PRIMARY KEY, verdict TEXT, reason TEXT,
        days_checked INTEGER, days_bad INTEGER, worst_residual REAL,
        checked_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wallet_day_exception(
        coldkey TEXT, day TEXT, expected REAL, actual REAL, residual REAL,
        PRIMARY KEY (coldkey, day))""")
    conn.execute("CREATE INDEX IF NOT EXISTS h_pos ON holders(coldkey,netuid,hotkey)")
    conn.execute("CREATE INDEX IF NOT EXISTS ev_day ON events(netuid, timestamp)")


def build_price_index(conn):
    """Median traded price per subnet per day, from every wallet's trades.

    The median, not the mean: a single fat-fingered or zero-priced record
    should not move the benchmark that everything else is judged against.
    """
    conn.execute("DELETE FROM subnet_price")
    buckets = {}
    for netuid, day, price in conn.execute(
            "SELECT netuid, substr(timestamp,1,10), price FROM events "
            "WHERE price > 0"):
        buckets.setdefault((netuid, day), []).append(price)
    rows = []
    for (netuid, day), prices in buckets.items():
        if len(prices) < MIN_PRICE_SAMPLES:
            continue
        prices.sort()
        mid = len(prices) // 2
        median = (prices[mid] if len(prices) % 2
                  else (prices[mid - 1] + prices[mid]) / 2.0)
        rows.append((netuid, day, median, len(prices)))
    conn.executemany(
        "INSERT OR REPLACE INTO subnet_price(netuid,day,price,samples) "
        "VALUES(?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def _positions(conn):
    """coldkey -> set of (netuid, hotkey) we hold a tracked balance for."""
    out = {}
    for ck, n, hk in conn.execute("SELECT coldkey, netuid, hotkey FROM holders"):
        out.setdefault(ck, set()).add((n, hk))
    return out


def _flows(conn):
    """(coldkey, day) -> TAO into (+) or out of (-) tracked positions.

    Restricted to positions we actually hold, because a balance we did not
    record cannot have moved. This is the join that, done at subnet level
    instead of position level, made a validator switch look like a 15% day.
    """
    out = {}
    for ck, day, net in conn.execute("""
            SELECT e.coldkey, substr(e.timestamp,1,10),
                   SUM(CASE WHEN e.action='BUY' THEN e.tao_amount
                            WHEN e.action='SELL' THEN -e.tao_amount ELSE 0 END)
            FROM events e WHERE EXISTS (
                SELECT 1 FROM holders h WHERE h.coldkey=e.coldkey
                AND h.netuid=e.netuid AND h.hotkey=e.hotkey)
            GROUP BY e.coldkey, substr(e.timestamp,1,10)"""):
        out[(ck, day)] = net or 0.0
    return out


def qualify(conn, verbose=True):
    """Score every wallet against the market and record the verdict."""
    ensure_tables(conn)
    priced = build_price_index(conn)
    prices = {(n, d): p for n, d, p in conn.execute(
        "SELECT netuid, day, price FROM subnet_price")}
    positions = _positions(conn)
    flows = _flows(conn)

    series = {}
    for ck, day, tao in conn.execute(
            "SELECT coldkey, day, total_tao FROM wallet_daily ORDER BY coldkey, day"):
        series.setdefault(ck, []).append((day, tao or 0.0))

    conn.execute("DELETE FROM wallet_day_exception")
    verdicts, exceptions = [], []
    stamp = datetime.now(timezone.utc).isoformat()

    for ck, points in series.items():
        pos = positions.get(ck, set())
        subnets = {n for n, _hk in pos}
        if not pos:
            verdicts.append((ck, "unfit", "no tracked position", 0, 0, None, stamp))
            continue
        if len(subnets) != 1:
            # Value cannot be attributed to a price without a per-position
            # balance history, which we do not keep. Not wrong - unprovable.
            verdicts.append((ck, "unverifiable", "spans several subnets",
                             0, 0, None, stamp))
            continue
        netuid = next(iter(subnets))

        checked = bad = 0
        worst = 0.0
        for i in range(1, len(points)):
            d0, v0 = points[i - 1]
            d1, v1 = points[i]
            if v0 < MIN_BALANCE_TAO:
                continue
            p0, p1 = prices.get((netuid, d0)), prices.get((netuid, d1))
            if not p0 or not p1:
                continue                       # no benchmark; not a failure
            expected = v0 * (p1 / p0) + flows.get((ck, d1), 0.0)
            residual = (v1 - expected) / v0
            checked += 1
            if abs(residual) > abs(worst):
                worst = residual
            if abs(residual) > RESIDUAL_TOLERANCE:
                bad += 1
                exceptions.append((ck, d1, expected, v1, residual))

        if checked == 0:
            verdicts.append((ck, "unverifiable", "no day had a usable price",
                             0, 0, None, stamp))
        elif bad > max(1, checked * MAX_BAD_SHARE):
            verdicts.append((ck, "quarantined",
                             "{} of {} days unexplained".format(bad, checked),
                             checked, bad, worst, stamp))
        else:
            verdicts.append((ck, "good", "", checked, bad, worst, stamp))

    conn.execute("DELETE FROM wallet_quality")
    conn.executemany(
        "INSERT INTO wallet_quality(coldkey,verdict,reason,days_checked,"
        "days_bad,worst_residual,checked_at) VALUES(?,?,?,?,?,?,?)", verdicts)
    conn.executemany(
        "INSERT OR REPLACE INTO wallet_day_exception"
        "(coldkey,day,expected,actual,residual) VALUES(?,?,?,?,?)", exceptions)
    conn.commit()

    if verbose:
        from collections import Counter
        tally = Counter(v[1] for v in verdicts)
        print("Qualification: {} subnet-days priced, {} wallets scored".format(
            priced, len(verdicts)))
        for k, n in tally.most_common():
            print("   {:14s} {}".format(k, n))
        print("   {} day-level exceptions recorded".format(len(exceptions)))
    return verdicts


def good_wallets(conn):
    """The only wallets a published board may draw on."""
    try:
        return {ck for (ck,) in conn.execute(
            "SELECT coldkey FROM wallet_quality WHERE verdict='good'")}
    except Exception:
        return None      # engine has never run; caller decides what to do


def main():
    conn = sweep.db()
    qualify(conn)
    conn.close()


if __name__ == "__main__":
    main()
