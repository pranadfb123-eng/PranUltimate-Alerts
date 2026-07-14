"""
update_dhan_secret.py
======================
Run this each morning right after you regenerate your Dhan access token and
paste it into intraday_config.json. It reads the token from that file and
pushes it to the DHAN_ACCESS_TOKEN GitHub Actions secret automatically,
using the GitHub CLI (gh) — so you never have to manually paste it into
the GitHub website.

One-time setup required before this works:
  1. Install GitHub CLI: https://cli.github.com/
  2. Authenticate once:  gh auth login   (opens a browser, log in normally)

Usage:
  py -3.13 update_dhan_secret.py

Edit REPO below if your repo name/owner ever changes.
"""

import json
import os
import subprocess
import sys

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "intraday_config.json")
REPO        = "pranadfb123-eng/PranUltimate-Alerts"


def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: {CONFIG_PATH} not found.")
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    token = cfg.get("access_token")
    if not token:
        print("ERROR: 'access_token' not found in intraday_config.json.")
        sys.exit(1)

    print(f"Pushing today's DHAN_ACCESS_TOKEN to {REPO} ...")

    try:
        result = subprocess.run(
            ["gh", "secret", "set", "DHAN_ACCESS_TOKEN", "--repo", REPO, "--body", token],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        print("ERROR: GitHub CLI ('gh') not found. Install it from https://cli.github.com/ "
              "and run 'gh auth login' once before using this script.")
        sys.exit(1)

    if result.returncode != 0:
        print(f"FAILED: {result.stderr.strip()}")
        print("If this says you're not authenticated, run: gh auth login")
        sys.exit(1)

    print("Done — DHAN_ACCESS_TOKEN secret updated for today.")


if __name__ == "__main__":
    main()