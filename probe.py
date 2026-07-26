"""
Bracket Board - one-wallet probe
---------------------------------
Cheap sanity check before committing to a long backfill. For a single
wallet it makes at most two API calls - one events page, one history
page - and prints exactly what came back, including the real field
names. Writes nothing back to the repository.

Set PROBE_COLDKEY to pick a wallet; otherwise the largest holder is used.
"""

import os
import sys

import sweep
import collector as coll
import backfill

PAGE = 5  # tiny page size - we only want to see the shape


def show(label, data):
    if data is None:
        print("  {}: FAILED (see HTTP line above)".format(label))
        return False
    items = data.get("data", [])
    print("  {}: OK - {} record(s)".format(label, len(items)))
    if items:
        print("    field names: {}".format(sorted(items[0].keys())))
        print("    first record: {}".format(items[0]))
    else:
        print("    (empty result set - endpoint accepted the query)")
    return True


def main():
    conn = sweep.db()
    backfill.ensure_tables(conn)

    ck = os.environ.get("PROBE_COLDKEY", "").strip()
    if not ck:
        row = conn.execute(
            "SELECT coldkey FROM holders GROUP BY coldkey "
            "ORDER BY SUM(balance_tao) DESC LIMIT 1").fetchone()
        if not row:
            print("FAIL: holders table is empty - run the sweep first.")
            sys.exit(1)
        ck = row[0]

    positions = conn.execute(
        "SELECT DISTINCT hotkey, netuid FROM holders WHERE coldkey=?",
        (ck,)).fetchall()
    print("Probing coldkey : {}".format(ck))
    print("Known positions : {}".format(len(positions)))
    if not positions:
        print("FAIL: no (hotkey, netuid) positions known for this wallet.")
        sys.exit(1)

    ok = True

    print("\n[1/2] events  /api/delegation/v1")
    ok &= show("events", sweep.api_get(
        conn, "/api/delegation/v1?nominator={}&limit={}&page=1".format(ck, PAGE)))

    hotkey, netuid = positions[0]
    print("\n[2/2] history /api/dtao/stake_balance/history/v1"
          "  (hotkey={}, netuid={})".format(hotkey, netuid))
    hist = sweep.api_get(
        conn, "/api/dtao/stake_balance/history/v1?coldkey={}&hotkey={}"
              "&netuid={}&limit=100&page=1".format(ck, hotkey, netuid))
    ok &= show("history", hist)

    # The backfill reads these two fields; confirm they really exist.
    if hist and hist.get("data"):
        first = hist["data"][0]
        for field in ("timestamp", "balance_as_tao"):
            if field in first:
                print("    OK  backfill reads '{}' -> {!r}".format(field, first[field]))
            else:
                print("    MISSING  backfill reads '{}' but it is not in the "
                      "response".format(field))
                ok = False

        # The endpoint returns a snapshot per block, so a day can carry several
        # records. Run the real reducer the backfill uses and prove it collapses
        # them instead of summing them into a fake spike.
        raw = {}
        for item in hist["data"]:
            day = str(item.get("timestamp", ""))[:10]
            raw.setdefault(day, []).append(sweep.rao_to_tao(item.get("balance_as_tao")))
        reduced = backfill.latest_per_day(hist["data"])
        busiest = max(len(v) for v in raw.values())
        print("\n    {} record(s) over {} day(s); busiest day holds {} record(s)"
              .format(len(hist["data"]), len(raw), busiest))
        print("    day           n   naive-sum        backfill")
        for day in sorted(raw)[-5:]:
            print("      {}  {:3d}   {:10.2f}      {:10.2f} TAO".format(
                day, len(raw[day]), sum(raw[day]), reduced[day]))

        if len(reduced) != len(raw):
            print("\n    FAIL reducer dropped days ({} -> {})".format(
                len(raw), len(reduced)))
            ok = False
        elif any(reduced[d] > max(raw[d]) + 1e-6 for d in raw):
            print("\n    FAIL reducer still inflates at least one day")
            ok = False
        elif busiest > 1:
            print("\n    OK  {} records shared a day and the reducer collapsed "
                  "them to one snapshot (no inflation)".format(busiest))
        else:
            print("\n    OK  one record per day already; reducer is a no-op here")

    print("\nCalls used: {}".format(sweep.calls_used_this_run))
    print("RESULT: {}".format("PASS" if ok else "FAIL"))
    conn.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
