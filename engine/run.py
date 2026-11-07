"""Engine entry point.

    python -m engine.run --mode full     # after market close: scan, backtest, recommend, alert
    python -m engine.run --mode live     # during market hours: live prices + stop/target alerts
    python -m engine.run --mode morning  # 8:45 AM IST: pre-market reminder of today's buys
    python -m engine.run --mode full --demo   # offline test with synthetic data
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import ai_notes, backtest, config, data, fundamentals, news, notify, tracker
from .strategies import SWING_STRATEGIES, Features, momentum_scores, pick_portfolio
from .universe import load_nifty50, load_universe

log = logging.getLogger("engine")

DISCLAIMER = ("Educational research tool, not SEBI-registered investment advice. "
              "Past performance (including backtests) does not guarantee future results. "
              "Never invest money you cannot afford to lose; always use the stop-loss.")


# ======================================================================= helpers
def clean(o):
    """Make data JSON-safe (NaN/inf -> None, numpy -> python)."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def write_json(name: str, obj) -> None:
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.OUT_DIR / name
    path.write_text(json.dumps(clean(obj), separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    log.info("wrote %s (%.0f KB)", path.name, path.stat().st_size / 1024)


def r2(x):
    try:
        x = float(x)
        return None if not np.isfinite(x) else round(x, 2)
    except (TypeError, ValueError):
        return None


def spark(series: pd.Series, n: int = 90) -> list:
    return [r2(v) for v in series.dropna().iloc[-n:].tolist()]


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


REGIME_TEXT = {
    2: ("Bullish", "Nifty is above its 50 and 200-day averages. Good time for new swing trades at normal size."),
    1: ("Cautious", "Nifty is above its 200-day average but has lost short-term strength. Take fewer trades at half size."),
    0: ("Bearish", "Nifty is below its 200-day average. No new swing trades; long-term buying only in small parts."),
}


# ======================================================================= market
def market_summary(f: Features, p: data.Prices) -> dict:
    ix = f.ix
    i = -1
    regime = int(f.regime.iloc[i])
    label, advice = REGIME_TEXT[regime]
    last = f.close.iloc[-1]
    prev = f.close.iloc[-2]
    above200 = (f.close.iloc[-1] > f.sma200.iloc[-1])
    valid = f.sma200.iloc[-1].notna()
    breadth = float(above200[valid].mean() * 100) if valid.any() else None
    adv = int((last > prev).sum())
    dec = int((last < prev).sum())
    return {
        "date": str(f.close.index[-1].date()),
        "nifty": r2(ix.iloc[i]), "nifty_change_pct": r2((ix.iloc[i] / ix.iloc[i - 1] - 1) * 100),
        "nifty_sma50": r2(f.ix_sma50.iloc[i]), "nifty_sma200": r2(f.ix_sma200.iloc[i]),
        "nifty_ret_1m": r2((ix.iloc[i] / ix.iloc[-22] - 1) * 100),
        "nifty_ret_1y": r2((ix.iloc[i] / ix.iloc[-253] - 1) * 100) if len(ix) > 253 else None,
        "regime": regime, "regime_label": label, "regime_advice": advice,
        "breadth_above_200dma": r2(breadth), "advances": adv, "declines": dec,
        "nifty_pe": p.index_pe, "nifty_pb": p.index_pb,
        "nifty_spark": spark(ix, 120),
    }


# ======================================================================= swing picks
def swing_picks(f: Features, uni: pd.DataFrame, bt: dict, p: data.Prices) -> list[dict]:
    names = dict(zip(uni.symbol, uni.name))
    inds = dict(zip(uni.symbol, uni.industry))
    out = []
    i = -1
    for st in SWING_STRATEGIES:
        stats = bt.get(st.key, {})
        # a setup is only recommended if its own backtest is profitable after costs
        enabled = (stats.get("trades", 0) >= 30 and (stats.get("avg_return") or 0) > 0
                   and (stats.get("profit_factor") or 0) >= 1.1)
        sig, stop = st.signal_fn(f)
        today = sig.iloc[i]
        for sym in today[today].index:
            price = float(f.close[sym].iloc[i])
            stop_px = float(stop[sym].iloc[i])
            risk = price - stop_px
            if not np.isfinite(risk) or risk <= 0:
                continue
            target = price + st.target_r * risk if st.target_r else None
            exit_hint = float(f.sma5[sym].iloc[i]) if st.exit_above_sma5 else None
            out.append({
                "symbol": sym, "name": names.get(sym, sym), "industry": inds.get(sym, ""),
                "kind": "swing", "strategy": st.key, "strategy_name": st.name,
                "why": st.plain_english, "how_to_exit": st.how_to_exit,
                "strategy_enabled": enabled,
                "price": r2(price), "stop": r2(stop_px), "target": r2(target),
                "exit_hint": r2(exit_hint),
                "max_entry": r2(price * (1 + st.max_gap_up)),
                "risk_per_share": r2(risk), "risk_pct": r2(risk / price * 100),
                "reward_pct": r2((target / price - 1) * 100) if target else r2(stats.get("avg_win")),
                "max_hold_days": st.max_hold, "atr": r2(f.atr14[sym].iloc[i]),
                "rsi14": r2(f.rsi14[sym].iloc[i]), "rsi2": r2(f.rsi2[sym].iloc[i]),
                "rs_percentile": r2(f.rs_rank[sym].iloc[i] * 100),
                "ret_1m": r2(f.ret21[sym].iloc[i] * 100), "ret_6m": r2(f.ret126[sym].iloc[i] * 100),
                "vol_ratio": r2(f.vol_ratio[sym].iloc[i]),
                "delivery_pct": r2(p.delivery_pct.get(sym)) if len(p.delivery_pct) else None,
                "sma50": r2(f.sma50[sym].iloc[i]), "sma200": r2(f.sma200[sym].iloc[i]),
                "hist_win_rate": stats.get("win_rate"), "hist_avg_return": stats.get("avg_return"),
                "hist_avg_days": stats.get("avg_days"), "hist_trades": stats.get("trades"),
                "spark": spark(f.close[sym]),
            })
    return out


def score_swing(pk: dict, regime: int) -> None:
    s = 0.0
    wr = pk.get("hist_win_rate") or 45
    ar = pk.get("hist_avg_return") or 0
    s += 30 * clamp((wr - 40) / 30)                       # historical hit rate
    s += 15 * clamp(ar / 2.0)                             # historical average gain
    s += 20 * clamp((pk.get("rs_percentile") or 50) / 100)  # leadership
    vr = pk.get("vol_ratio") or 1
    s += 10 * clamp(vr / 2) if pk["strategy"] == "breakout" else 5
    dp = pk.get("delivery_pct")
    s += 5 if dp is None else (10 if dp >= 50 else 5 if dp >= 35 else 0)
    s += 10 * clamp(pk.get("news_sentiment", 0), -1, 1)
    s += 10 if regime == 2 else 0
    s += 3 * pk.get("quality_pts", 0)
    if pk.get("red_flags"):
        s -= 40
    if not pk.get("strategy_enabled"):
        s -= 25
    pk["score"] = int(round(clamp(s, 0, 100)))
    pk["confidence"] = "High" if s >= 70 else "Medium" if s >= 55 else "Low"


# ======================================================================= long-term
def long_term_picks(f: Features, uni: pd.DataFrame, prev_holdings: list[str],
                    use_fundamentals: bool) -> tuple[list[dict], dict]:
    inds = dict(zip(uni.symbol, uni.industry))
    names = dict(zip(uni.symbol, uni.name))
    sc = momentum_scores(f)
    if sc.empty:
        return [], {}
    top = list(sc.index[:35])
    funda = fundamentals.fetch(top) if use_fundamentals else {}
    bad = []
    for s in top:
        pts, warn = fundamentals.quality_check(funda.get(s, {}))
        if pts <= -2 or any("loss-making" in w for w in warn):
            bad.append(s)
    chosen = pick_portfolio(sc.drop(index=bad, errors="ignore"), inds, config.LT_PORTFOLIO_SIZE, prev_holdings)
    picks = []
    for rank, s in enumerate(chosen, 1):
        row = sc.loc[s]
        pts, warn = fundamentals.quality_check(funda.get(s, {}))
        price = float(f.close[s].iloc[-1])
        picks.append({
            "symbol": s, "name": names.get(s, s), "industry": inds.get(s, ""), "kind": "long_term",
            "large": bool(f.large.get(s, False)),            # Nifty 50 blue chip
            "rank": rank, "score": r2(row.score), "price": r2(price),
            "weight_pct": round(100 / len(chosen), 2),
            "ret_6m": r2(f.ret126[s].iloc[-1] * 100), "ret_12m": r2(f.ret252[s].iloc[-1] * 100),
            "volatility": r2(row.vol * 100),
            "pct_from_high": r2((price / float(f.high252[s].iloc[-1]) - 1) * 100),
            "sma200": r2(f.sma200[s].iloc[-1]),
            # long-term exit: review if it closes 15% below its 200-day average or drops out at rebalance
            "review_below": r2(float(f.sma200[s].iloc[-1]) * 0.85),
            "fundamentals": funda.get(s, {}), "quality_pts": pts, "warnings": warn,
            "is_new": s not in prev_holdings, "spark": spark(f.close[s], 250),
        })
    changes = {"added": [s for s in chosen if s not in prev_holdings],
               "removed": [s for s in prev_holdings if s not in chosen]}
    return picks, changes


def deployment_plan(regime: int) -> str:
    return {
        2: "Market trend is up: invest the long-term amount in 2 equal parts, 2 weeks apart.",
        1: "Market is mixed: invest in 3 equal monthly parts.",
        0: "Market is in a downtrend: invest only half now, spread over 6 monthly parts, keep the rest in a liquid fund/FD.",
    }[regime]


def next_rebalance(today: pd.Timestamp) -> str:
    nxt = (today + pd.offsets.BMonthEnd(1))
    if nxt.date() == today.date():
        nxt = today + pd.offsets.BMonthEnd(2)
    return str(nxt.date())


# ======================================================================= safe mode
def safe_gate(stats: dict) -> tuple[bool, str]:
    """Is a method reliable enough to recommend to a very cautious investor?"""
    if not stats or not stats.get("trades"):
        return False, "Not tested yet"
    if stats["trades"] < config.SAFE_MIN_TRADES:
        return False, f"Only {stats['trades']} past trades: not enough evidence yet"
    if (stats.get("win_rate") or 0) < config.SAFE_MIN_WIN_RATE:
        return False, (f"Made money in {stats.get('win_rate')}% of past trades, below the "
                       f"{config.SAFE_MIN_WIN_RATE:.0f}% safety bar")
    if (stats.get("profit_factor") or 0) < config.SAFE_MIN_PROFIT_FACTOR or (stats.get("avg_return") or 0) <= 0:
        return False, "Past gains did not outweigh past losses by a safe margin"
    l12 = stats.get("last_12m") or {}
    if l12.get("trades", 0) >= 10 and (l12.get("avg_return") or 0) < 0:
        return False, "It lost money over the last 12 months, so it is paused until it recovers"
    return True, "Reliable over the past 5 years and still working recently"


def shares_for(amount: float, price: float | None) -> int:
    return int(amount // price) if price and price > 0 else 0


def build_plan(tr: dict, last_date: str, bt: dict) -> dict:
    """What the investor should do at the next market open: SELL, BUY, HOLD."""
    plan = {"buy": [], "sell": [], "hold": []}
    for t in tr["trades"]:
        st = t["status"]
        strat = tracker.STRATS.get(t["strategy"])
        stats = bt.get(t["strategy"], {})
        base = {"symbol": t["symbol"], "name": t.get("name") or t["symbol"], "strategy": t["strategy"],
                "max_hold_days": strat.max_hold if strat else None}
        if st == "pending":
            plan["buy"].append({
                **base, "signal_date": t["signal_date"], "price": t["signal_close"], "max_entry": t["max_entry"],
                "stop": t["stop"], "risk_per_share": t["risk"],
                "risk_pct": r2(t["risk"] / t["signal_close"] * 100) if t["signal_close"] else None,
                "hist_win_rate": stats.get("win_rate"), "hist_avg_return": stats.get("avg_return"),
                "hist_avg_win": stats.get("avg_win"), "hist_avg_loss": stats.get("avg_loss"),
                "hist_avg_days": stats.get("avg_days"), "hist_trades": stats.get("trades"),
                "alert_qty": shares_for(config.TRADE_AMOUNT, t["max_entry"])})
        elif st == "selling":
            last = t.get("last_price")
            plan["sell"].append({
                **base, "reason": t.get("sell_reason", "exit_rule"), "entry": t.get("entry"),
                "entry_date": t.get("entry_date"), "last_price": last,
                "ret_pct": r2((last / t["entry"] - 1 - config.ROUND_TRIP_COST) * 100) if last and t.get("entry") else None})
        elif st == "open":
            plan["hold"].append({
                **base, "entry": t.get("entry"), "entry_date": t.get("entry_date"), "stop": t["stop"],
                "last_price": t.get("last_price"), "days": t.get("days")})
        elif st in ("stop", "target") and t.get("exit_date") == last_date:
            # exit level was reached during the day: anyone still holding should sell at the open
            plan["sell"].append({**base, "reason": st, "entry": t.get("entry"), "last_price": t.get("exit"),
                                 "ret_pct": t.get("ret_pct")})
    return plan


def plan_message(plan: dict, when: str) -> str:
    """Plain-language plan used for phone alerts."""
    L = []
    if plan["sell"]:
        L.append(f"💰 SELL {when} at 9:15 AM:")
        for s in plan["sell"]:
            ret = f" (about {s['ret_pct']:+.1f}%)" if s.get("ret_pct") is not None else ""
            why = {"stop": "It fell to the safety price. Small losses are part of the plan.",
                   "target": "It reached the goal price.",
                   "time": "The time limit is over."}.get(s["reason"], "The trade is complete.")
            L.append(f"• All your {s['symbol']} shares{ret}. {why}")
        L.append("")
    if plan["buy"]:
        L.append(f"🛒 BUY {when} at 9:15 AM:")
        for b in plan["buy"]:
            q = b["alert_qty"]
            L.append(f"• {q} shares of {b['name']} ({b['symbol']}). Pay at most ₹{b['max_entry']:,.2f} a share "
                     f"(about ₹{q * b['max_entry']:,.0f} in total). If the price is higher, don't buy.")
            L.append(f"  Safety sell price about ₹{b['stop']:,.0f}: most you could lose is about "
                     f"₹{q * b['risk_per_share']:,.0f}. We'll alert you if it gets there.")
        L.append("")
    if plan["hold"]:
        L.append("✋ HOLD (nothing to do): " + ", ".join(h["symbol"] for h in plan["hold"]))
    return "\n".join(L).strip()


# ======================================================================= modes
def run_full(args) -> None:
    t0 = time.time()
    uni = load_universe()
    p = data.load_prices(list(uni.symbol), demo=args.demo)
    f = Features(p)
    f.set_large(load_nifty50())                       # blue chips for the safe method
    last_date = str(f.close.index[-1].date())
    mkt = market_summary(f, p)
    log.info("Data: %s, last date %s, regime %s", p.source, last_date, mkt["regime_label"])

    # ---- backtests
    bt = {}
    if not args.no_backtest:
        for st in SWING_STRATEGIES:
            t1 = time.time()
            bt[st.key] = backtest.backtest_swing(f, st)
            bt[st.key].update(name=st.name, description=st.plain_english, exit=st.how_to_exit)
            log.info("backtest %-10s %s (%.1fs)", st.key,
                     {k: bt[st.key].get(k) for k in ("trades", "win_rate", "avg_return", "profit_factor")},
                     time.time() - t1)
        # the investor's real account, for each safe method (shown on the Results screen)
        for st in SWING_STRATEGIES:
            if st.key in config.SAFE_STRATEGIES:
                bt[st.key]["portfolio"] = backtest.portfolio_sim(
                    f, st, money=config.TRADE_AMOUNT * config.MAX_OPEN_TRADES, slots=config.MAX_OPEN_TRADES,
                    per_day=config.MAX_NEW_PER_DAY)
                log.info("portfolio sim %s: %s", st.key,
                         {k: bt[st.key]["portfolio"].get(k) for k in ("trades", "win_rate", "total_profit",
                                                                    "per_year_profit", "worst_drop_pct")})
        inds = dict(zip(uni.symbol, uni.industry))
        lt = backtest.backtest_momentum(f, inds)
        lt_guard = backtest.backtest_momentum(f, inds, trend_guard=True)
        log.info("backtest momentum cagr %s vs nifty %s, guarded %s", lt.get("cagr"),
                 lt.get("bench_cagr"), lt_guard.get("cagr"))
        write_json("backtest.json", {
            "generated": datetime.now(timezone.utc).isoformat(), "data_from": str(f.close.index[0].date()),
            "data_to": last_date, "cost_round_trip_pct": config.ROUND_TRIP_COST * 100,
            "swing": bt, "long_term": lt, "long_term_guarded": lt_guard,
            "notes": [
                "Universe is today's Nifty 200, so the past excludes stocks that later dropped out "
                "(survivorship bias). Real results will likely be somewhat lower.",
                "Swing trades: entry at next day's open, skipped if it gaps up >2%; stop checked "
                f"before target on the same day; {config.ROUND_TRIP_COST * 100:.2f}% round-trip costs deducted "
                f"(0.30% + a Rs {config.DP_CHARGE_RS:.0f} depository charge on a Rs {config.TRADE_AMOUNT:,.0f} trade).",
                "Long-term: monthly rebalance of the top-15 momentum stocks, max 4 per industry.",
            ]})
    else:
        try:
            old = json.loads((config.OUT_DIR / "backtest.json").read_text(encoding="utf-8"))
            bt = old.get("swing", {})
        except (OSError, json.JSONDecodeError):
            bt = {}

    # ---- swing candidates
    picks = swing_picks(f, uni, bt, p)
    log.info("swing signals today: %d", len(picks))

    # ---- long-term portfolio
    tr = tracker.load()
    snaps = tr.get("portfolio_snapshots", [])
    prev = list(snaps[-1]["holdings"].keys()) if snaps else []
    lt_picks, changes = long_term_picks(f, uni, prev, use_fundamentals=not args.demo)

    # ---- fundamentals for swing picks (light), news for everything shown
    if not args.demo and picks:
        top_syms = [pk["symbol"] for pk in picks][:25]
        fd = fundamentals.fetch(top_syms)
        for pk in picks:
            pts, warn = fundamentals.quality_check(fd.get(pk["symbol"], {}))
            pk.update(fundamentals=fd.get(pk["symbol"], {}), quality_pts=pts, warnings=warn)
    names = {x["symbol"]: x["name"] for x in picks + lt_picks}
    nw = news.news_for(names) if (names and not args.no_news) else {}
    for x in picks + lt_picks:
        n = nw.get(x["symbol"], {})
        x["news"] = n.get("items", [])
        x["news_sentiment"] = n.get("sentiment", 0.0)
        x["red_flags"] = n.get("red_flags", [])
    for pk in picks:
        score_swing(pk, mkt["regime"])
    picks.sort(key=lambda x: x["score"], reverse=True)

    # ---- track record: advance existing trades first (frees slots, creates sell signals)
    events = tracker.update_eod(tr, f)
    active = [t for t in tr["trades"] if t["status"] in ("pending", "open", "selling")]

    safe = {k: safe_gate(bt.get(k, {})) for k in config.SAFE_STRATEGIES}
    if config.SAFE_MODE:
        # very low risk: only reliable blue-chip setups, affordable with the trade amount,
        # no bad news, never more than MAX_OPEN_TRADES at once, one new trade per day
        held = {t["symbol"] for t in active}
        cands = [pk for pk in picks if pk["strategy"] in safe and safe[pk["strategy"]][0]
                 and mkt["regime"] >= 1 and not pk["red_flags"] and pk.get("news_sentiment", 0) > -0.3
                 and not any("loss-making" in w for w in pk.get("warnings", []))
                 and pk["max_entry"] <= config.TRADE_AMOUNT and pk["symbol"] not in held]
        # strongest stock first: the exact rule validated in the portfolio simulation
        cands.sort(key=lambda pk: -(pk.get("rs_percentile") or 0))
        slots = max(0, config.MAX_OPEN_TRADES - len(active))
        actionable = cands[: min(slots, config.MAX_NEW_PER_DAY)]
    else:
        actionable = [pk for pk in picks if pk["confidence"] in ("High", "Medium")
                      and not pk["red_flags"] and pk["strategy_enabled"] and mkt["regime"] >= 1]
        actionable = actionable[: (6 if mkt["regime"] == 2 else 3)]
    for pk in picks:
        pk["action"] = "BUY" if pk in actionable else "WATCH"
    mkt_news = news.market_news() if not args.no_news else []

    new_trades = tracker.add_signals(tr, actionable, last_date)
    if lt_picks:
        tracker.snapshot_portfolio(tr, last_date, lt_picks, float(f.ix.iloc[-1]))
    tracker.save(tr)
    perf = tracker.portfolio_performance(tr, f)

    # ---- AI notes (optional)
    notes = ai_notes.write_notes(
        {k: mkt[k] for k in ("nifty", "nifty_change_pct", "regime_label", "breadth_above_200dma")},
        actionable + lt_picks[:8])
    for x in picks + lt_picks:
        if x["symbol"] in notes:
            x["ai_note"] = notes[x["symbol"]]
    if notes.get("_market"):
        mkt["ai_summary"] = notes["_market"]

    # ---- open positions view (from track record) and tomorrow's plan
    open_trades = [t for t in tr["trades"] if t["status"] in ("open", "pending", "selling")]
    plan = build_plan(tr, last_date, bt)

    latest = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "data_source": p.source, "data_date": last_date, "demo": bool(args.demo),
        "disclaimer": DISCLAIMER, "market": mkt,
        "swing": picks[:40], "actionable": [x["symbol"] + ":" + x["strategy"] for x in actionable],
        "long_term": {"picks": lt_picks, "changes": changes, "deployment": deployment_plan(mkt["regime"]),
                      "next_rebalance": next_rebalance(f.close.index[-1]),
                      "live_performance": perf, "size": config.LT_PORTFOLIO_SIZE},
        "open_trades": open_trades,
        "plan": plan,
        "safe": {
            "enabled": config.SAFE_MODE, "trade_amount": config.TRADE_AMOUNT,
            "max_open_trades": config.MAX_OPEN_TRADES, "universe": "Nifty 50",
            "bar": {"min_win_rate": config.SAFE_MIN_WIN_RATE, "min_profit_factor": config.SAFE_MIN_PROFIT_FACTOR,
                    "min_trades": config.SAFE_MIN_TRADES},
            "methods": {k: {"ok": ok, "why": why, "name": bt.get(k, {}).get("name"),
                            "stats": {s: bt.get(k, {}).get(s) for s in
                                      ("trades", "win_rate", "avg_return", "avg_win", "avg_loss",
                                       "profit_factor", "avg_days", "max_losing_streak")},
                            "last_12m": bt.get(k, {}).get("last_12m"),
                            "portfolio": bt.get(k, {}).get("portfolio")}
                        for k, (ok, why) in safe.items()},
        },
        # stocks to sell at the next open (exit signal at the close, or exit level hit today)
        "sell_today": [{"symbol": s["symbol"], "reason": s["reason"], "price": s.get("last_price"),
                        "ret_pct": s.get("ret_pct")} for s in plan["sell"]],
        "track_summary": tracker.summary(tr),
        "strategies": [{"key": s.key, "name": s.name, "why": s.plain_english, "exit": s.how_to_exit,
                        "stats": {k: bt.get(s.key, {}).get(k) for k in
                                  ("trades", "win_rate", "avg_return", "avg_win", "avg_loss",
                                   "profit_factor", "avg_days", "max_losing_streak")},
                        "last_12m": bt.get(s.key, {}).get("last_12m")} for s in SWING_STRATEGIES],
        "alert_channels": notify.channels(),
        "defaults": {"capital": config.DEFAULT_CAPITAL, "risk_per_trade": config.DEFAULT_RISK_PER_TRADE,
                     "max_position_pct": config.MAX_POSITION_PCT},
    }
    write_json("latest.json", latest)
    write_json("market_news.json", {"generated": latest["generated"], "items": mkt_news})
    write_json("live.json", {"updated": latest["generated"], "market_open": False, "quotes": {}})

    # ---- alerts
    if config.SAFE_MODE:
        # results of finished trades + tomorrow's plan, in plain words; silent if nothing to do
        results = [notify.format_event(e) for e in events if e["type"] in ("sold", "stop", "target", "expired")]
        body = plan_message(plan, "tomorrow")
        msg = "\n".join(results + ([""] if results and body else []) + ([body] if body else []))
        if msg.strip() and not args.demo:
            notify.send("Plan for tomorrow" if body else "Trade update", msg)
    else:
        lines = [notify.format_event(e) for e in events]
        if actionable:
            lines.append(f"\n📈 {len(actionable)} new swing idea(s) for tomorrow ({mkt['regime_label']} market):")
            for pk in actionable:
                tgt = f"target ₹{pk['target']:,}" if pk.get("target") else f"exit above ₹{pk['exit_hint']:,}"
                lines.append(f"• {pk['symbol']} ({pk['strategy_name']}) buy ≤ ₹{pk['max_entry']:,}, "
                             f"stop ₹{pk['stop']:,}, {tgt} [{pk['confidence']}]")
        if changes.get("added") and snaps:
            lines.append(f"\n🧺 Long-term portfolio changes: add {', '.join(changes['added'])}; "
                         f"remove {', '.join(changes['removed']) or 'none'}")
        if lines and not args.demo:
            notify.send(f"Stock Finder - {last_date}", "\n".join(lines))
    log.info("done in %.0fs: %d actionable, %d LT picks, %d events, %d new tracked",
             time.time() - t0, len(actionable), len(lt_picks), len(events), len(new_trades))


def market_is_open() -> bool:
    n = data.now_ist()
    if n.weekday() >= 5:
        return False
    mins = n.hour * 60 + n.minute
    return 9 * 60 + 10 <= mins <= 15 * 60 + 40


def run_live(args) -> None:
    if not market_is_open() and not args.force:
        log.info("Market closed - nothing to do")
        return
    tr = tracker.load()
    syms = sorted({t["symbol"] for t in tr["trades"] if t["status"] in ("open", "pending", "selling")})
    try:
        latest = json.loads((config.OUT_DIR / "latest.json").read_text(encoding="utf-8"))
        syms = sorted(set(syms) | {x["symbol"] for x in latest.get("swing", [])[:20]}
                      | {x["symbol"] for x in latest.get("long_term", {}).get("picks", [])})
    except (OSError, json.JSONDecodeError):
        pass
    quotes = data.live_quotes(syms)
    events = tracker.update_live(tr, quotes)
    tracker.save(tr)
    write_json("live.json", {"updated": datetime.now(timezone.utc).isoformat(),
                             "market_open": market_is_open(), "quotes": quotes})
    if events:
        notify.send("Stock Finder - live alert", "\n".join(notify.format_event(e) for e in events), "high")
    log.info("live: %d quotes, %d events", len(quotes), len(events))


def run_morning(args) -> None:
    """Pre-market reminder (8:45 AM IST): today's buy ideas with limits, stops and targets."""
    try:
        latest = json.loads((config.OUT_DIR / "latest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.info("No data yet - skipping morning reminder")
        return
    data_day = datetime.fromisoformat(latest["data_date"]).date()
    if (data.now_ist().date() - data_day).days > 4:
        log.info("Data is stale (%s) - skipping morning reminder", data_day)
        return
    if config.SAFE_MODE:
        plan = latest.get("plan") or {"buy": [], "sell": [], "hold": []}
        body = plan_message(plan, "today")
        if not (plan["buy"] or plan["sell"]):
            log.info("Nothing to buy or sell today - no reminder sent")
            return
        log.info("morning reminder:\n%s", body)
        if not args.demo:
            notify.send("Today's plan (market opens 9:15 AM)", body)
        return
    tr = tracker.load()
    pending = [t for t in tr["trades"] if t["status"] == "pending"]
    open_ = [t for t in tr["trades"] if t["status"] == "open"]
    mkt = latest.get("market", {})
    lines = [f"Market mood: {mkt.get('regime_label', '-')}. Market opens at 9:15 AM."]
    lines.append("")
    if pending:
        lines.append(f"🛒 Buy today at the open ({len(pending)}):")
        for t in pending:
            exit_ = f"target ₹{t['target']:,}" if t.get("target") else "exit when it closes above its 5-day average"
            lines.append(f"• {t['symbol']}: buy only if price ≤ ₹{t['max_entry']:,}. "
                         f"Stop-loss about ₹{t['stop']:,}, {exit_}")
        lines.append("Place the stop-loss right after buying. Skip a stock if it opens above the limit.")
    else:
        lines.append("No new buys today. Patience is part of the plan.")
    if open_:
        lines.append("")
        lines.append(f"📂 Open trades: {', '.join(t['symbol'] for t in open_)}. Check stops are in place.")
    lines.append("")
    lines.append("Quantities for your capital are in the app.")
    msg = chr(10).join(lines)
    log.info("morning reminder:\n%s", msg)
    if not args.demo:
        notify.send("Good morning - today's plan", msg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "live", "morning"], default="full")
    ap.add_argument("--demo", action="store_true", help="synthetic data (offline testing)")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-backtest", action="store_true")
    ap.add_argument("--force", action="store_true", help="live mode even if market is closed")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    if args.demo:  # never mix synthetic results into the real published data
        config.OUT_DIR = config.CACHE_DIR / "demo"
        tracker.FILE = config.OUT_DIR / "track_record.json"
    if args.mode == "full":
        run_full(args)
    elif args.mode == "morning":
        run_morning(args)
    else:
        run_live(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
