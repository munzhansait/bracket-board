"""
Bracket Board - Step 0: Connection Tester
------------------------------------------
This script checks that your taostats API key works, discovers which
endpoints your key can access, and writes everything into a report
file (test_report.txt) that you can share back in the chat.

It uses ONLY Python's built-in libraries - nothing extra to install.
It paces itself to respect the free tier limit (~5 calls/minute).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

BASE = "https://api.taostats.io"
DOCS_INDEX = "https://docs.taostats.io/llms.txt"
PAUSE_SECONDS = 14  # stay safely under 5 calls/minute

# Candidate endpoints to probe. Some may not exist or may be paywalled -
# that's fine, finding out is the whole point of this test.
CANDIDATES = [
    ("Chain stats (basic health check)", "/api/stats/latest/v1"),
    ("Subnet pool data (subnet 1)", "/api/dtao/pool/latest/v1?netuid=1"),
    ("Subnet pool history (subnet 1)", "/api/dtao/pool/history/v1?netuid=1&limit=2"),
    ("Validator yields (subnet 1)", "/api/dtao/validator/yield/latest/v1?netuid=1"),
    ("Subnet list", "/api/subnet/latest/v1?limit=2"),
    ("Stake balances (subnet 1, top holders?)", "/api/dtao/stake_balance/latest/v1?netuid=1&limit=2"),
    ("Stake balance history sample", "/api/dtao/stake_balance/history/v1?netuid=1&limit=2"),
    ("Stake/unstake events sample", "/api/dtao/delegation/v1?limit=2"),
    ("Account list sample", "/api/account/latest/v1?limit=2"),
]


def read_api_key():
    key = os.environ.get("TAOSTATS_API_KEY", "").strip()
    if not key:
        print("ERROR: The TAOSTATS_API_KEY secret is not set.")
        print("In your GitHub repository go to:")
        print("  Settings -> Secrets and variables -> Actions -> New repository secret")
        print("Name it exactly TAOSTATS_API_KEY and paste your key as the value.")
        sys.exit(1)
    return key


def fetch(url, api_key=None, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("accept", "application/json")
    req.add_header("User-Agent", "BracketBoard-Step0/1.0")
    if api_key:
        req.add_header("Authorization", api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return None, f"CONNECTION ERROR: {e}"


def trim(body, limit=1500):
    """Keep report readable: pretty-print JSON if possible, then trim."""
    try:
        parsed = json.loads(body)
        body = json.dumps(parsed, indent=2)
    except Exception:
        pass
    if len(body) > limit:
        return body[:limit] + "\n... [trimmed] ..."
    return body


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    samples_dir = os.path.join(here, "api_samples")
    os.makedirs(samples_dir, exist_ok=True)
    report_path = os.path.join(here, "test_report.txt")

    api_key = read_api_key()
    lines = []
    lines.append("BRACKET BOARD - STEP 0 TEST REPORT")
    lines.append("Generated: " + datetime.now().isoformat())
    lines.append("=" * 60)

    print("Starting tests. This takes a few minutes because the script")
    print("deliberately waits between calls to respect the free tier.")
    print("Leave this window open until it says DONE.\n")

    # 1) Fetch the official endpoint index (no key needed, docs site)
    print("[1/{}] Downloading official API endpoint index...".format(len(CANDIDATES) + 1))
    status, body = fetch(DOCS_INDEX)
    if status == 200:
        idx_path = os.path.join(samples_dir, "endpoint_index.txt")
        with open(idx_path, "w", encoding="utf-8") as f:
            f.write(body)
        lines.append("\nENDPOINT INDEX: downloaded OK -> api_samples/endpoint_index.txt")
        print("   ...saved.")
    else:
        lines.append("\nENDPOINT INDEX: could not download (status {}).".format(status))
        print("   ...could not download (not a problem, we continue).")

    # 2) Probe each candidate endpoint
    ok_count = 0
    for i, (label, path) in enumerate(CANDIDATES, start=2):
        print("[{}/{}] Testing: {}".format(i, len(CANDIDATES) + 1, label))
        time.sleep(PAUSE_SECONDS)
        url = BASE + path
        status, body = fetch(url, api_key=api_key)

        lines.append("\n" + "-" * 60)
        lines.append("TEST: " + label)
        lines.append("URL: " + url)
        lines.append("HTTP STATUS: " + str(status))

        if status == 200:
            ok_count += 1
            verdict = "WORKS"
            fname = path.split("?")[0].strip("/").replace("/", "_") + ".json"
            with open(os.path.join(samples_dir, fname), "w", encoding="utf-8") as f:
                f.write(body)
            lines.append("VERDICT: WORKS - sample saved to api_samples/" + fname)
        elif status in (401, 403):
            verdict = "KEY PROBLEM OR PAYWALLED"
            lines.append("VERDICT: Unauthorized/forbidden - key invalid, key header format different, or endpoint needs a paid plan.")
        elif status == 404:
            verdict = "ENDPOINT DOES NOT EXIST (that's useful to know)"
            lines.append("VERDICT: Not found - this endpoint path is wrong/retired.")
        elif status == 429:
            verdict = "RATE LIMITED"
            lines.append("VERDICT: Rate limited - we may need to slow down further.")
        elif status is None:
            verdict = "NO CONNECTION"
            lines.append("VERDICT: Could not connect at all (internet/firewall issue?).")
        else:
            verdict = "UNEXPECTED STATUS"
            lines.append("VERDICT: Unexpected response.")

        lines.append("RESPONSE PREVIEW:")
        lines.append(trim(body))
        print("   ...{}".format(verdict))

    lines.append("\n" + "=" * 60)
    lines.append("SUMMARY: {}/{} candidate endpoints returned data.".format(ok_count, len(CANDIDATES)))
    lines.append("Next step: share test_report.txt back in the chat.")
    lines.append("(The report contains NO trace of your API key - safe to share.)")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("\nDONE. Report written to: test_report.txt")
    print("Download it from the 'Artifacts' box on this run's page,")
    print("then share it back in the chat.")


if __name__ == "__main__":
    main()
