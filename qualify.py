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


def build_price_index(conn, only_days=None):
    """Median traded price per subnet per day, from every wallet's trades.

    The median, not the mean: a single fat-fingered or zero-priced record
    should not move the benchmark that everything else is judged against.

    Incremental. A past day's price is settled once trading has moved on, so
    only days carrying new events are recomputed - scanning the whole events
    table four times a day would cost more every day, forever, to re-derive
    numbers that cannot change.
    """
    if only_days is None:
        conn.execute("DELETE FROM subnet_price")
        cur = conn.execute(
            "SELECT netuid, substr(timestamp,1,10), price FROM events "
            "WHERE price > 0")
    else:
        if not only_days:
            return 0
        keys = list(only_days)
        conn.executemany("DELETE FROM subnet_price WHERE netuid=? AND day=?", keys)
        marks = ",".join("(?,?)" for _ in keys)
        flat = [x for pair in keys for x in pair]
        cur = conn.execute(
            "SELECT netuid, substr(timestamp,1,10), price FROM events "
            "WHERE price > 0 AND (netuid, substr(timestamp,1,10)) IN "
            "(VALUES {})".format(marks), flat)

    buckets = {}
    for netuid, day, price in cur:
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


def _dirty(conn, watermark):
    """Wallets whose verdict could have changed since the last run.

    Three ways a verdict goes stale, and the third is the one that is easy to
    miss: new trades of its own, new daily balances of its own, or a new price
    on a subnet it holds - because the benchmark it is judged against moved
    even though nothing about the wallet did.
    """
    dirty = set()
    days = set()
    for ck, netuid, day in conn.execute(
            "SELECT coldkey, netuid, substr(timestamp,1,10) FROM events "
            "WHERE rowid > ?", (watermark,)):
        dirty.add(ck)
        days.add((netuid, day))
    if days:
        subs = {n for n, _d in days}
        marks = ",".join("?" for _ in subs)
        for (ck,) in conn.execute(
                "SELECT DISTINCT coldkey FROM holders WHERE netuid IN ({})"
                .format(marks), list(subs)):
            dirty.add(ck)
    for (ck,) in conn.execute(
            "SELECT DISTINCT coldkey FROM wallet_daily WHERE day > "
            "COALESCE((SELECT value FROM meta WHERE key='qualify_last_day'),'')"):
        dirty.add(ck)
    return dirty, days


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


def qualify(conn, verbose=True, full=None):
    """Score wallets against the market and record the verdict.

    Incremental by default: only wallets whose inputs moved are rescored, and
    a verdict already on file stands until something it depends on changes.
    Pass full=True (or set QUALIFY_FULL=1) after changing a threshold, when
    every stored verdict was reached under rules that no longer apply.
    """
    ensure_tables(conn)
    if full is None:
        full = os.environ.get("QUALIFY_FULL", "") == "1"
    have = conn.execute("SELECT COUNT(*) FROM wallet_quality").fetchone()[0]
    full = full or not have

    watermark = int(sweep.meta_get(conn, "qualify_event_rowid", "0") or 0)
    if full:
        targets, changed_days = None, None
    else:
        targets, changed_days = _dirty(conn, watermark)
        if not targets:
            if verbose:
                print("Qualification: nothing changed since the last run.")
            return []

    priced = build_price_index(conn, changed_days)
    prices = {(n, d): p for n, d, p in conn.execute(
        "SELECT netuid, day, price FROM subnet_price")}
    positions = _positions(conn)
    flows = _flows(conn)

    series = {}
    for ck, day, tao in conn.execute(
            "SELECT coldkey, day, total_tao FROM wallet_daily ORDER BY coldkey, day"):
        if targets is None or ck in targets:
            series.setdefault(ck, []).append((day, tao or 0.0))

    if full:
        conn.execute("DELETE FROM wallet_day_exception")
        conn.execute("DELETE FROM wallet_quality")
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

    # Rescored wallets have their old verdict and exceptions replaced; every
    # other wallet keeps the verdict already on file.
    if not full and verdicts:
        rescored = [(v[0],) for v in verdicts]
        conn.executemany("DELETE FROM wallet_quality WHERE coldkey=?", rescored)
        conn.executemany("DELETE FROM wallet_day_exception WHERE coldkey=?", rescored)
    conn.executemany(
        "INSERT INTO wallet_quality(coldkey,verdict,reason,days_checked,"
        "days_bad,worst_residual,checked_at) VALUES(?,?,?,?,?,?,?)", verdicts)
    conn.executemany(
        "INSERT OR REPLACE INTO wallet_day_exception"
        "(coldkey,day,expected,actual,residual) VALUES(?,?,?,?,?)", exceptions)

    # Two statements, not a cross join. SQLite will not resolve a bare rowid
    # once the FROM clause names more than one source, and the engine failed
    # outright on "no such column: rowid" - which the sweep caught, so the
    # board silently fell back to its own checks with no market corroboration.
    row = conn.execute("SELECT MAX(rowid) FROM events").fetchone()
    day = conn.execute("SELECT MAX(day) FROM wallet_daily").fetchone()
    sweep.meta_set(conn, "qualify_event_rowid", (row and row[0]) or 0)
    sweep.meta_set(conn, "qualify_last_day", (day and day[0]) or "")
    conn.commit()

    if verbose:
        from collections import Counter
        tally = Counter(v[1] for v in verdicts)
        print("Qualification ({}): {} subnet-days repriced, {} wallets scored"
              .format("full" if full else "incremental", priced, len(verdicts)))
        for k, n in tally.most_common():
            print("   {:14s} {}".format(k, n))
        print("   {} day-level exceptions recorded".format(len(exceptions)))
        standing = conn.execute(
            "SELECT verdict, COUNT(*) FROM wallet_quality GROUP BY verdict")
        print("   register now: " + ", ".join(
            "{} {}".format(n, v) for v, n in standing))
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
