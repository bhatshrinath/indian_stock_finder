"""Runs the market day: ONE start, then everything chains.

GitHub's free scheduler does not fire for this repository, so nothing here depends on it.
One start (a tap on "Run workflow", or an outside timer) is enough:

    morning reminder -> live prices every ~2 minutes (9:10 AM-3:40 PM IST) -> evening scan

* A job can only run 6 hours, so near its limit it starts the next shift itself.
* After the close it waits until the day's data is final (4:20 PM IST), then starts the daily scan.
* Prices are published to a one-commit branch (`live-data`): no history growth, no site rebuilds.

    python -m engine.live_session
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests

from . import config, data, run

log = logging.getLogger("live_session")

BRANCH = "live-data"
OPEN_FROM = (9, 10)        # IST: start refreshing a little before the 9:15 opening
CLOSE_AT = (15, 40)        # IST: stop refreshing a little after the 3:30 close
SCAN_AT = (16, 20)         # IST: the day's data is final; start the evening scan


def phase(now: datetime) -> str:
    """'skip' (weekend), 'wait' (before the market window), 'run', 'after' (closed, waiting
    for final data) or 'done' (time for the evening scan)."""
    if now.weekday() >= 5:
        return "skip"
    mins = now.hour * 60 + now.minute
    if mins < OPEN_FROM[0] * 60 + OPEN_FROM[1]:
        return "wait"
    if mins <= CLOSE_AT[0] * 60 + CLOSE_AT[1]:
        return "run"
    if mins < SCAN_AT[0] * 60 + SCAN_AT[1]:
        return "after"
    return "done"


def publish(files: list[Path]) -> bool:
    """Force-push the files to the one-commit `live-data` branch."""
    token, repo = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    override = os.getenv("LIVE_PUBLISH_URL", "")          # tests publish to a local repository
    if not override and (not token or not repo):
        log.warning("GITHUB_TOKEN / GITHUB_REPOSITORY not set: not publishing")
        return False
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            if f.exists():
                shutil.copy(f, Path(tmp) / f.name)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

        def git(*a: str) -> None:
            subprocess.run(["git", *a], cwd=tmp, env=env, check=True, capture_output=True, text=True)

        git("init", "-q")
        git("checkout", "-q", "-b", BRANCH)
        git("add", "-A")
        git("-c", "user.name=stock-finder-bot", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-q", "-m", "live prices")
        url = override or "https://x-access-token:%s@github.com/%s.git" % (token, repo)
        git("push", "-q", "-f", url, "%s:%s" % (BRANCH, BRANCH))
    return True


def dispatch(workflow: str) -> bool:
    """Start another workflow (allowed with the job's own token, unlike other triggers)."""
    token, repo = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    if not token or not repo:
        log.warning("cannot start %s: no token", workflow)
        return False
    ref = os.getenv("GITHUB_REF_NAME") or "main"
    try:
        r = requests.post("https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (repo, workflow),
                          headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
                          json={"ref": ref}, timeout=20)
    except requests.RequestException as exc:
        log.warning("could not start %s: %s", workflow, exc)
        return False
    if r.status_code != 204:
        log.warning("could not start %s: HTTP %s", workflow, r.status_code)
        return False
    log.info("Started %s", workflow)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=60, help="seconds between refreshes")
    ap.add_argument("--max-minutes", type=int, default=335, help="hand over to a new shift after this long (job limit is 6 hours)")
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    started = time.time()
    started_phase = phase(data.now_ist())
    rounds = failures = 0
    reminded = False
    next_step = None                                       # what to start when this shift ends
    while True:
        ph = phase(data.now_ist())
        if ph == "skip":
            log.info("Weekend: nothing to do")
            break
        if ph == "done":
            if started_phase != "done":
                next_step = "daily.yml"                    # evening scan, with the day's final data
            break
        if time.time() - started > args.max_minutes * 60:
            next_step = "live-session.yml"                 # relay: the next shift takes over
            break
        if ph == "wait" and not reminded:
            reminded = True
            try:
                run.run_morning(argparse.Namespace(demo=False))
            except Exception as exc:
                log.warning("morning reminder failed: %s", exc)
        if ph == "run":
            try:
                run.run_live(argparse.Namespace(force=False, demo=False))
                if not args.no_publish:
                    publish([config.OUT_DIR / "live.json", config.OUT_DIR / "track_record.json"])
                rounds += 1
            except Exception as exc:                       # keep the day going through a bad minute
                failures += 1
                log.warning("refresh failed (%s): %s", failures, exc)
        time.sleep(args.every if ph == "run" else 60)
    log.info("Shift over: %d refreshes, %d failures", rounds, failures)
    if next_step and not args.no_publish:
        dispatch(next_step)
    return 0


if __name__ == "__main__":
    sys.exit(main())
