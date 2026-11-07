"""Run with:  python -m pytest -q"""
import numpy as np
import pandas as pd
import pytest

from engine import backtest, config, data, tracker
from engine.strategies import SWING_STRATEGIES, Features, momentum_scores, pick_portfolio

SYMS = [f"S{i:03d}" for i in range(80)]


def random_walk_prices(seed=1, days=900):
    """Zero-drift market: no strategy should show a real edge here."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=days)
    mkt = np.cumsum(rng.normal(0.0, 0.01, days))
    c = pd.DataFrame({s: 500 * np.exp(mkt + np.cumsum(rng.normal(0, 0.018, days))) for s in SYMS}, index=dates)
    o = c.shift(1).fillna(c) * (1 + rng.normal(0, 0.004, c.shape))
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, c.shape))
    l = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, c.shape))
    v = pd.DataFrame(rng.lognormal(14, 0.4, c.shape), index=dates, columns=SYMS)
    ix = pd.Series(10000 * np.exp(mkt), index=dates)
    return data.Prices(o, h, l, c, v, ix, ix * 1.003, ix * 0.997, source="test")


def test_no_edge_on_random_walk():
    """If a backtest 'finds' profits on pure noise, it is peeking into the future."""
    f = Features(random_walk_prices())
    for st in SWING_STRATEGIES:
        res = backtest.backtest_swing(f, st)
        if res.get("trades", 0) < 50:
            continue
        # average trade should be about minus costs, never a large positive number
        assert res["avg_return"] < 0.6, (st.key, res["avg_return"])


def test_signals_do_not_change_when_future_is_removed():
    """Signal on day t must be identical whether or not later data exists."""
    p = random_walk_prices(seed=3)
    full = Features(p)
    cut = 700
    short = Features(data.Prices(*(x.iloc[:cut] for x in (p.open, p.high, p.low, p.close, p.volume)),
                                 p.index_close.iloc[:cut], p.index_high.iloc[:cut], p.index_low.iloc[:cut]))
    for st in SWING_STRATEGIES:
        a = st.signal_fn(full)[0].iloc[:cut]
        b = st.signal_fn(short)[0]
        assert a.equals(b), st.key


def test_split_adjustment():
    dates = pd.bdate_range("2024-01-01", periods=5)
    close = pd.DataFrame({"X": [100, 102, 51, 52, 53]}, index=dates, dtype=float)    # 1:2 split on day 3
    prev = pd.DataFrame({"X": [99, 100, 51, 51, 52]}, index=dates, dtype=float)      # NSE-adjusted prev close
    adj = close * data.adjustment_factor(prev, close)
    assert adj["X"].tolist() == pytest.approx([50, 51, 51, 52, 53])


def test_trade_simulation_stop_before_target():
    p = random_walk_prices(seed=5, days=300)
    f = Features(p)
    backtest._prep_numpy(f)
    st = next(s for s in SWING_STRATEGIES if s.key == "ema_bounce")
    col, s = 0, 250
    entry_day = s + 1
    # force a day that touches both stop and target: must count as a stop (pessimistic)
    c = f._np["close"][s, col]
    f._np["open"][entry_day, col] = c
    f._np["low"][entry_day, col] = c * 0.5
    f._np["high"][entry_day, col] = c * 2
    t = backtest.simulate_trade(f, st, col, s, stop_px=c * 0.95)
    assert t["reason"] == "stop"
    assert t["ret"] == pytest.approx(-0.05 - config.ROUND_TRIP_COST, abs=1e-9)


def test_gap_up_is_skipped():
    p = random_walk_prices(seed=6, days=300)
    f = Features(p)
    backtest._prep_numpy(f)
    st = SWING_STRATEGIES[0]
    s = 250
    f._np["open"][s + 1, 0] = f._np["close"][s, 0] * 1.05
    assert backtest.simulate_trade(f, st, 0, s, f._np["close"][s, 0] * 0.95) is None


def test_tracker_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "FILE", tmp_path / "tr.json")
    p = random_walk_prices(seed=8, days=400)
    f = Features(p)
    sym = SYMS[0]
    i = 380
    d = f.close.index[i]
    price = float(f.close[sym].iloc[i])
    tr = {"trades": [], "portfolio_snapshots": []}
    pick = {"symbol": sym, "strategy": "ema_bounce", "price": price, "max_entry": price * 1.5,
            "stop": price * 0.5, "target": price * 10, "risk_per_share": price * 0.5}
    tracker.add_signals(tr, [pick], str(d.date()))
    tracker.add_signals(tr, [pick], str(d.date()))          # idempotent
    assert len(tr["trades"]) == 1
    ev = tracker.update_eod(tr, f)
    t = tr["trades"][0]
    assert ev[0]["type"] == "entered"
    assert t["entry"] == pytest.approx(float(f.open[sym].iloc[i + 1]), abs=0.01)
    # with a far stop/target it can only end by time
    assert t["status"] in ("open", "time")
    tracker.update_eod(tr, f)                                # re-running must not duplicate work
    assert sum(e["type"] == "entered" for e in ev) == 1


def test_momentum_portfolio_respects_industry_cap():
    f = Features(random_walk_prices(seed=9))
    sc = momentum_scores(f)
    inds = {s: ("A" if i % 2 else "B") for i, s in enumerate(SYMS)}
    chosen = pick_portfolio(sc, inds, 10, max_per_industry=4)
    assert len(chosen) <= 8
    assert sum(inds[s] == "A" for s in chosen) <= 4


def test_incomplete_last_day_is_dropped():
    """Yahoo sometimes returns the index for the latest day but not the stocks."""
    p = random_walk_prices(seed=10, days=300)
    p.close.iloc[-1, :] = np.nan
    out = data.drop_incomplete_tail(p)
    assert len(out.close) == 299
    assert out.close.iloc[-1].notna().all()
    assert len(out.index_close) == 299


# ----------------------------------------------------------------- safe mode
def _open_trade(f, sym, i, strategy="dip_buy"):
    price = float(f.close[sym].iloc[i])
    return {"id": "t1", "symbol": sym, "name": sym, "strategy": strategy, "signal_date": str(f.close.index[i - 1].date()),
            "signal_close": price, "max_entry": price * 2, "stop": price * 0.01, "initial_stop": price * 0.01,
            "target": None, "risk": price * 0.99, "status": "open", "entry": price,
            "entry_date": str(f.close.index[i].date()), "exit": None, "exit_date": None, "ret_pct": None,
            "days": 1, "hi_close": price, "atr": None}


def test_exit_signal_sells_at_next_open(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "FILE", tmp_path / "tr.json")
    f = Features(random_walk_prices(seed=11, days=400))
    sym = SYMS[1]
    tr = {"trades": [_open_trade(f, sym, 300)]}
    ev = tracker.update_eod(tr, f)
    t = tr["trades"][0]
    assert t["status"] == "selling" and ev[-1]["type"] == "sell_signal"
    sig_day = pd.Timestamp(t["sell_signal_date"])
    nxt = f.close.index[f.close.index > sig_day][0]
    tracker.update_eod(tr, f)                                  # next evening's run
    assert t["status"] in ("exit_rule", "time")
    assert t["exit_date"] == str(nxt.date())
    assert t["exit"] == pytest.approx(float(f.open[sym].loc[nxt]), abs=0.01)


def test_safe_gate_rules():
    from engine.run import safe_gate
    good = {"trades": 300, "win_rate": 66, "profit_factor": 1.4, "avg_return": 0.5,
            "last_12m": {"trades": 40, "avg_return": 0.3}}
    assert safe_gate(good)[0]
    assert not safe_gate({**good, "trades": 10})[0]
    assert not safe_gate({**good, "win_rate": 55})[0]
    assert not safe_gate({**good, "profit_factor": 1.05})[0]
    assert not safe_gate({**good, "last_12m": {"trades": 20, "avg_return": -0.2}})[0]   # recently broken
    assert safe_gate({**good, "last_12m": {"trades": 3, "avg_return": -2}})[0]         # too few to judge


def test_plan_and_message():
    from engine.run import build_plan, plan_message
    tr = {"trades": [
        {"symbol": "ITC", "name": "ITC Ltd.", "strategy": "safe_dip", "status": "pending", "signal_date": "2026-11-06",
         "signal_close": 430.0, "max_entry": 438.6, "stop": 410.0, "risk": 20.0},
        {"symbol": "TCS", "name": "TCS", "strategy": "safe_dip", "status": "open", "entry": 3000.0,
         "entry_date": "2026-11-04", "stop": 2900.0, "last_price": 3050.0, "days": 3},
        {"symbol": "INFY", "name": "Infosys", "strategy": "safe_dip", "status": "selling", "entry": 1500.0,
         "entry_date": "2026-11-02", "last_price": 1540.0, "sell_reason": "exit_rule"},
        {"symbol": "OLD", "name": "Old", "strategy": "safe_dip", "status": "exit_rule", "exit_date": "2026-10-01"},
    ]}
    plan = build_plan(tr, "2026-11-06", {"safe_dip": {"win_rate": 66, "trades": 300}})
    assert [b["symbol"] for b in plan["buy"]] == ["ITC"]
    assert [h["symbol"] for h in plan["hold"]] == ["TCS"]
    assert [s["symbol"] for s in plan["sell"]] == ["INFY"]
    assert plan["buy"][0]["alert_qty"] == int(config.TRADE_AMOUNT // 438.6)
    msg = plan_message(plan, "tomorrow")
    assert "SELL tomorrow" in msg and "INFY" in msg and "BUY tomorrow" in msg and "HOLD" in msg
    assert plan_message({"buy": [], "sell": [], "hold": []}, "today") == ""


def test_live_day_one_protection():
    t = {"symbol": "ITC", "strategy": "safe_dip", "status": "pending", "signal_date": "2026-11-06",
         "signal_close": 430.0, "max_entry": 438.6, "stop": 410.0, "initial_stop": 410.0}
    tr = {"trades": [dict(t)]}
    ev = tracker.update_live(tr, {"ITC": {"price": 415, "open": 432, "day_low": 411, "day_high": 433, "date": "2026-11-09"}})
    assert [e["type"] for e in ev] == ["live_stop"]               # 432 - 20 = 412 safety price was touched
    tr = {"trades": [dict(t)]}
    ev = tracker.update_live(tr, {"ITC": {"price": 445, "open": 445, "day_low": 440, "day_high": 450, "date": "2026-11-09"}})
    assert [e["type"] for e in ev] == ["live_skip"]               # opened above the buy limit
    tr = {"trades": [dict(t)]}
    ev = tracker.update_live(tr, {"ITC": {"price": 431, "open": 431, "day_low": 429, "day_high": 433, "date": "2026-11-06"}})
    assert ev == []                                               # signal day itself: nothing yet


def test_safe_dip_only_blue_chips():
    f = Features(random_walk_prices(seed=12))
    f.set_large(SYMS[:10])
    st = next(s for s in SWING_STRATEGIES if s.key == "safe_dip")
    sig = st.signal_fn(f)[0]
    assert not sig[SYMS[10:]].to_numpy().any()
