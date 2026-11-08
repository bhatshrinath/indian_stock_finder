"""Live track record: every recommendation the app makes is recorded and its
real outcome tracked. Over time this is the most honest measure of the system
(no backtest assumptions, no hindsight).

Statuses: pending -> open -> (target | stop) or open -> selling -> (exit_rule | time),
or pending -> expired. "selling" means: the exit signal came at the close, so the
user sells at the next market open, and that open price is the recorded exit.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from . import config
from .strategies import Features, SWING_STRATEGIES

log = logging.getLogger(__name__)
FILE = config.OUT_DIR / "track_record.json"
STRATS = {s.key: s for s in SWING_STRATEGIES}


def load() -> dict:
    if FILE.exists():
        try:
            return json.loads(FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("track record unreadable, starting fresh")
    return {"trades": [], "portfolio_snapshots": []}


def save(tr: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(tr, indent=1, default=str), encoding="utf-8")


def add_signals(tr: dict, picks: list[dict], signal_date: str) -> list[dict]:
    """Record today's new swing picks (idempotent per symbol+strategy+date)."""
    existing = {(t["symbol"], t["strategy"], t["signal_date"]) for t in tr["trades"]}
    active = {(t["symbol"], t["strategy"]) for t in tr["trades"] if t["status"] in ("pending", "open")}
    added = []
    for p in picks:
        key = (p["symbol"], p["strategy"], signal_date)
        if key in existing or (p["symbol"], p["strategy"]) in active:
            continue
        t = {
            "id": f"{p['symbol']}-{p['strategy']}-{signal_date}",
            "symbol": p["symbol"], "name": p.get("name", ""), "strategy": p["strategy"],
            "signal_date": signal_date, "signal_close": p["price"],
            "max_entry": p["max_entry"], "stop": p["stop"], "initial_stop": p["stop"],
            "target": p.get("target"), "risk": p["risk_per_share"],
            "status": "pending", "entry": None, "entry_date": None,
            "exit": None, "exit_date": None, "ret_pct": None, "days": 0, "hi_close": None,
            "score": p.get("score"), "atr": p.get("atr"),
        }
        tr["trades"].append(t)
        added.append(t)
    return added


def update_eod(tr: dict, f: Features) -> list[dict]:
    """Advance every pending/open trade using daily bars. Returns events for alerts."""
    events = []
    dates = f.close.index
    for t in tr["trades"]:
        if t["status"] not in ("pending", "open", "selling") or t["symbol"] not in f.close.columns:
            continue
        strat = STRATS.get(t["strategy"])
        if strat is None:
            continue
        sym = t["symbol"]
        o, h, l, c = (getattr(f, k)[sym] for k in ("open", "high", "low", "close"))
        sma5, atr = f.sma5[sym], f.atr14[sym]
        sig_ts = pd.Timestamp(t["signal_date"])
        after = dates[dates > sig_ts]
        for d in after:
            if t["status"] == "selling":
                # sold at the first open after the sell signal
                if d.date().isoformat() <= t["sell_signal_date"]:
                    continue
                op = float(o.get(d, np.nan))
                if not np.isfinite(op):
                    continue
                _close(t, op, str(d.date()), t.get("sell_reason", "exit_rule"))
                events.append({"type": "sold", "trade": t})
                break
            if t["status"] == "pending":
                if t.get("entry_date"):
                    break
                # entry attempt on the first session after the signal
                op = float(o.get(d, np.nan))
                if not np.isfinite(op):
                    continue
                if op > t["max_entry"] or op <= t["stop"]:
                    t["status"] = "expired"
                    t["note"] = "Opened outside the buy range - skipped"
                    events.append({"type": "expired", "trade": t})
                    break
                t.update(status="open", entry=round(op, 2), entry_date=str(d.date()), hi_close=op)
                risk = t["signal_close"] - t["initial_stop"]
                t["stop"] = round(op - risk, 2)
                if strat.target_r:
                    t["target"] = round(op + strat.target_r * risk, 2)
                events.append({"type": "entered", "trade": t})
            if t["status"] != "open":
                break
            if d.date().isoformat() < t["entry_date"]:
                continue
            if t.get("last_processed") and d.date().isoformat() <= t["last_processed"]:
                continue
            hi, lo, cl, op = (float(x.get(d, np.nan)) for x in (h, l, c, o))
            if not np.isfinite(cl):
                continue
            t["days"] = int(np.searchsorted(dates, d) - np.searchsorted(dates, pd.Timestamp(t["entry_date"])) + 1)
            first = d.date().isoformat() == t["entry_date"]
            exit_px = reason = None
            if lo <= t["stop"]:
                exit_px, reason = (t["stop"] if first or op > t["stop"] else op), "stop"
            elif t.get("target") and hi >= t["target"]:
                exit_px, reason = (t["target"] if first or op < t["target"] else op), "target"
            elif strat.exit_above_sma5 and cl > float(sma5.get(d, np.inf)):
                reason = "exit_rule"
            elif t["days"] >= strat.max_hold:
                reason = "time"
            t["last_processed"] = d.date().isoformat()
            t["last_price"] = round(cl, 2)
            if reason in ("exit_rule", "time"):
                # signal at the close -> sell at the next open
                t.update(status="selling", sell_signal_date=d.date().isoformat(), sell_reason=reason)
                events.append({"type": "sell_signal", "trade": t})
                break
            if exit_px is not None:
                _close(t, exit_px, str(d.date()), reason)
                events.append({"type": reason, "trade": t})
                break
            t["hi_close"] = max(t.get("hi_close") or cl, cl)
            atr_val = t.get("atr") or float(atr.get(d, np.nan))
            if strat.trail_atr and np.isfinite(atr_val):
                new_stop = round(t["hi_close"] - strat.trail_atr * atr_val, 2)
                if new_stop > t["stop"]:
                    t["stop"] = new_stop
                    events.append({"type": "stop_raised", "trade": t})
            t["last_price"] = round(cl, 2)
    return events


def update_live(tr: dict, quotes: dict[str, dict]) -> list[dict]:
    """Intraday: warn when an open trade touches its stop or target (final booking happens EOD)."""
    events = []
    for t in tr["trades"]:
        if t["symbol"] not in quotes:
            continue
        q = quotes[t["symbol"]]
        # Daily fallbacks and retained last-good quotes are useful for display,
        # but are never safe evidence that a stop or target traded today.
        if q.get("stale") or q.get("is_fresh") is False:
            continue
        if t["status"] == "pending" and q.get("open") and q["date"] > t["signal_date"]:
            # buy day: the trade starts at today's open, so protect it from the first minute
            flags = t.setdefault("live_flags", {})
            if q["open"] > t["max_entry"] or q["open"] <= t["stop"]:
                if flags.get("skip") != q["date"]:
                    flags["skip"] = q["date"]
                    events.append({"type": "live_skip", "trade": t, "price": q["open"]})
                continue
            day_stop = round(q["open"] - (t["signal_close"] - t["initial_stop"]), 2)
            if q["day_low"] <= day_stop and flags.get("stop") != q["date"]:
                flags["stop"] = q["date"]
                events.append({"type": "live_stop", "trade": {**t, "stop": day_stop}, "price": q["price"]})
            continue
        if t["status"] != "open":
            continue
        t["last_price"] = q["price"]
        flags = t.setdefault("live_flags", {})
        today = q["date"]
        if q["day_low"] <= t["stop"] and flags.get("stop") != today:
            flags["stop"] = today
            events.append({"type": "live_stop", "trade": t, "price": q["price"]})
        if t.get("target") and q["day_high"] >= t["target"] and flags.get("target") != today:
            flags["target"] = today
            events.append({"type": "live_target", "trade": t, "price": q["price"]})
    return events


def _close(t: dict, px: float, d: str, reason: str) -> None:
    t.update(status=reason, exit=round(px, 2), exit_date=d,
             ret_pct=round((px / t["entry"] - 1 - config.ROUND_TRIP_COST) * 100, 2))


def summary(tr: dict) -> dict:
    closed = [t for t in tr["trades"] if t["status"] in ("target", "stop", "exit_rule", "time")]
    open_ = [t for t in tr["trades"] if t["status"] in ("open", "selling")]
    out = {"closed": len(closed), "open": len(open_),
           "pending": sum(t["status"] == "pending" for t in tr["trades"]),
           "since": min((t["signal_date"] for t in tr["trades"]), default=None)}
    if closed:
        rets = np.array([t["ret_pct"] for t in closed])
        out.update(win_rate=round(100 * (rets > 0).mean(), 1), avg_return=round(rets.mean(), 2),
                   total_return_sum=round(rets.sum(), 1),
                   best=round(rets.max(), 2), worst=round(rets.min(), 2))
        by = {}
        for t in closed:
            by.setdefault(t["strategy"], []).append(t["ret_pct"])
        out["by_strategy"] = {k: {"trades": len(v), "win_rate": round(100 * np.mean(np.array(v) > 0), 1),
                                  "avg_return": round(float(np.mean(v)), 2)} for k, v in by.items()}
    return out


def snapshot_portfolio(tr: dict, date: str, holdings: list[dict], index_level: float) -> None:
    """Record the long-term model portfolio once per month to track it live."""
    snaps = tr.setdefault("portfolio_snapshots", [])
    month = date[:7]
    if snaps and snaps[-1]["date"][:7] == month:
        return
    snaps.append({"date": date, "index": round(index_level, 2),
                  "holdings": {h["symbol"]: h["price"] for h in holdings}})


def portfolio_performance(tr: dict, f: Features) -> list[dict]:
    """Return per-snapshot performance of the model portfolio vs Nifty so far."""
    out = []
    last = f.close.iloc[-1]
    for s in tr.get("portfolio_snapshots", []):
        rets = [last[sym] / px - 1 for sym, px in s["holdings"].items()
                if sym in last.index and np.isfinite(last[sym]) and px]
        if not rets:
            continue
        out.append({"date": s["date"], "return_pct": round(100 * float(np.mean(rets)), 2),
                    "nifty_pct": round(100 * (float(f.ix.iloc[-1]) / s["index"] - 1), 2)})
    return out
