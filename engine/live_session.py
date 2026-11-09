"""Keeps live prices fresh for a whole market session.

GitHub's free scheduler can run late or skip runs, so instead of relying on a
15-minute schedule, ONE job starts (from several schedule slots, or by hand) and
then keeps itself running: every ~2 minutes it refreshes prices, checks stop-loss
levels, sends alerts, and publishes the snapshot to a one-commit branch
(`live-data`). Publishing there means no history growth and no site rebuilds.

    python -m engine.live_session            # until 3:40 PM IST (max ~5h40m)
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

from . import config, data, run

log = logging.getLogger("live_session")

BRANCH = "live-data"
OPEN_FROM = (9, 10)        # IST: start refreshing a little before the 9:15 opening
CLOSE_AT = (15, 40)        # IST: stop a little after the 3:30 close


def phase(now: datetime) -> str:
    """'skip' (weekend), 'wait' (before the market window), 'run', or 'done' (after it)."""
    if now.weekday() >= 5:
        return "skip"
    mins = now.hour * 60 + now.minute
    if mins < OPEN_FROM[0] * 60 + OPEN_FROM[1]:
        return "wait"
    if mins > CLOSE_AT[0] * 60 + CLOSE_AT[1]:
        return "done"
    return "run"


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=120, help="seconds between refreshes")
    ap.add_argument("--max-minutes", type=int, default=340, help="stop after this long (job limit is 6 hours)")
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    started = time.time()
    rounds = failures = 0
    while True:
        now = data.now_ist()
        ph = phase(now)
        if ph in ("skip", "done"):
            log.info("Session over (%s)", ph)
            break
        if time.time() - started > args.max_minutes * 60:
            log.info("Time limit reached: a later slot takes over")
            break
        if ph == "run":
            try:
                run.run_live(argparse.Namespace(force=False, demo=False))
                if not args.no_publish:
                    publish([config.OUT_DIR / "live.json", config.OUT_DIR / "track_record.json"])
                rounds += 1
            except Exception as exc:                       # keep the session alive through a bad minute
                failures += 1
                log.warning("refresh failed (%s): %s", failures, exc)
        time.sleep(args.every if ph == "run" else 60)
    log.info("Done: %d refreshes, %d failures", rounds, failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
