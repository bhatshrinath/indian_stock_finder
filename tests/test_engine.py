"""Run with:  python -m pytest -q"""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from engine import backtest, config, data, live_analysis, tracker
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
        {"id": "ITC-safe_dip-2026-11-06", "symbol": "ITC", "name": "ITC Ltd.",
         "strategy": "safe_dip", "status": "pending", "signal_date": "2026-11-06",
         "signal_close": 430.0, "max_entry": 438.6, "stop": 410.0, "risk": 20.0},
        {"symbol": "TCS", "name": "TCS", "strategy": "safe_dip", "status": "open", "entry": 3000.0,
         "entry_date": "2026-11-04", "stop": 2900.0, "last_price": 3050.0, "days": 3},
        {"id": "INFY-safe_dip-2026-10-31", "symbol": "INFY", "name": "Infosys",
         "strategy": "safe_dip", "status": "selling", "entry": 1500.0,
         "entry_date": "2026-11-02", "last_price": 1540.0, "sell_reason": "exit_rule",
         "sell_signal_date": "2026-11-06"},
        {"symbol": "OLD", "name": "Old", "strategy": "safe_dip", "status": "exit_rule", "exit_date": "2026-10-01"},
    ]}
    plan = build_plan(tr, "2026-11-06", {"safe_dip": {"win_rate": 66, "trades": 300}})
    assert [b["symbol"] for b in plan["buy"]] == ["ITC"]
    assert [h["symbol"] for h in plan["hold"]] == ["TCS"]
    assert [s["symbol"] for s in plan["sell"]] == ["INFY"]
    assert plan["buy"][0]["alert_qty"] == int(config.TRADE_AMOUNT // 438.6)
    assert plan["buy"][0]["id"] == "ITC-safe_dip-2026-11-06"
    assert plan["sell"][0]["id"] == "INFY-safe_dip-2026-10-31"
    assert plan["sell"][0]["exit_signal_date"] == "2026-11-06"
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


def test_intraday_quote_metrics_and_freshness():
    previous = pd.DatetimeIndex(["2026-11-09 15:30"], tz="Asia/Kolkata")
    current = pd.date_range("2026-11-10 09:15", periods=21, freq="min", tz="Asia/Kolkata")
    ix = previous.append(current)
    close = np.r_[99.0, np.arange(100.0, 121.0)]
    hist = pd.DataFrame({
        "Open": close,
        "High": close + 1,
        "Low": close - 1,
        "Close": close,
        "Volume": np.r_[50, np.repeat(100, 21)],
    }, index=ix)
    q = data.intraday_quote(hist, now=pd.Timestamp("2026-11-10 09:36", tz="Asia/Kolkata").to_pydatetime())
    assert q["price"] == 120
    assert q["open"] == 100
    assert q["prev_close"] == 99
    assert q["day_high"] == 121 and q["day_low"] == 99
    assert q["change_pct"] == pytest.approx(21.21, abs=0.01)
    assert q["vwap"] == pytest.approx(110, abs=0.01)
    assert q["return_5m"] == pytest.approx(4.35, abs=0.01)
    assert q["return_15m"] == pytest.approx(14.29, abs=0.01)
    assert q["range_position"] == pytest.approx(95.5, abs=0.1)
    assert q["delay_seconds"] == 60
    assert q["is_fresh"] and not q["stale"]


def test_daily_fallback_never_claims_a_future_close_time():
    ix = pd.DatetimeIndex(["2026-11-09", "2026-11-10"], tz="Asia/Kolkata")
    hist = pd.DataFrame({"Open": [99, 100], "High": [101, 103], "Low": [98, 99],
                         "Close": [100, 102], "Volume": [1000, 500]}, index=ix)
    now = pd.Timestamp("2026-11-10 10:00", tz="Asia/Kolkata").to_pydatetime()
    q = data.daily_quote(hist, now=now)
    assert pd.Timestamp(q["asof"]).tz_convert("Asia/Kolkata") == pd.Timestamp(now)
    assert q["delay_seconds"] == 0
    assert q["stale"] is True and q["is_fresh"] is False


def test_live_snapshot_preserves_missing_quotes_as_stale():
    old = {"updated": "2026-11-10T04:00:00+00:00", "feed_status": "near_live",
           "quotes": {"TCS": {"price": 3000, "asof": "2026-11-10T03:59:00+00:00",
                               "is_fresh": True, "stale": False}}}
    current = {"ITC": {"price": 430, "asof": "2026-11-10T04:04:00+00:00",
                        "is_fresh": True, "stale": False, "source": "Yahoo Finance intraday"}}
    snap = live_analysis.build_snapshot(
        current, ["ITC", "TCS", "NIFTY"], old, True, updated="2026-11-10T04:05:00+00:00")
    assert snap["requested"] == 3 and snap["received"] == 1 and snap["fresh_quotes"] == 1
    assert snap["feed_status"] == "partial"
    assert snap["quotes"]["TCS"]["stale"] is True
    assert snap["quotes"]["TCS"]["is_fresh"] is False
    assert snap["last_good_update"] == "2026-11-10T04:05:00+00:00"


def test_live_snapshot_keeps_newer_prior_quote_over_daily_fallback():
    old = {"updated": "2026-11-10T04:00:00+00:00", "feed_status": "near_live",
           "quotes": {"TCS": {"price": 3000, "asof": "2026-11-10T03:59:00+00:00",
                               "is_fresh": True, "stale": False, "source": "Yahoo Finance intraday"}}}
    fallback = {"TCS": {"price": 2980, "asof": "2026-11-09T10:00:00+00:00",
                         "is_fresh": False, "stale": True, "source": "Yahoo Finance daily fallback"}}
    snap = live_analysis.build_snapshot(
        fallback, ["TCS"], old, True, updated="2026-11-10T04:05:00+00:00")
    assert snap["quotes"]["TCS"]["price"] == 3000
    assert snap["quotes"]["TCS"]["source"] == "Yahoo Finance intraday"
    assert snap["quotes"]["TCS"]["stale"] is True
    assert snap["feed_status"] == "delayed"


def test_stale_live_quote_cannot_trigger_alert():
    t = {"symbol": "ITC", "strategy": "safe_dip", "status": "pending", "signal_date": "2026-11-06",
         "signal_close": 430.0, "max_entry": 438.6, "stop": 410.0, "initial_stop": 410.0}
    stale = {"price": 405, "open": 432, "day_low": 400, "day_high": 433, "date": "2026-11-09",
             "is_fresh": False, "stale": True}
    tr = {"trades": [dict(t)]}
    assert tracker.update_live(tr, {"ITC": stale}) == []
    assert "live_flags" not in tr["trades"][0]


def test_exchange_session_and_poll_buffer(monkeypatch):
    from engine import run

    monkeypatch.setattr(data, "now_ist", lambda: datetime(2026, 11, 9, 9, 12))
    assert not run.market_is_open()
    assert run.market_poll_window()

    monkeypatch.setattr(data, "now_ist", lambda: datetime(2026, 11, 9, 9, 15))
    assert run.market_is_open()

    monkeypatch.setattr(data, "now_ist", lambda: datetime(2026, 11, 9, 15, 30))
    assert not run.market_is_open()
    assert run.market_poll_window()

    monkeypatch.setattr(data, "now_ist", lambda: datetime(2026, 11, 9, 15, 41))
    assert not run.market_is_open()
    assert not run.market_poll_window()


def test_safe_dip_only_blue_chips():
    f = Features(random_walk_prices(seed=12))
    f.set_large(SYMS[:10])
    st = next(s for s in SWING_STRATEGIES if s.key == "safe_dip")
    sig = st.signal_fn(f)[0]
    assert not sig[SYMS[10:]].to_numpy().any()


def test_portfolio_sim_respects_slots_and_costs():
    f = Features(random_walk_prices(seed=13))
    st = SWING_STRATEGIES[1]                                   # dip_buy: frequent signals
    one = backtest.portfolio_sim(f, st, money=20000, slots=1)
    two = backtest.portfolio_sim(f, st, money=20000, slots=2)
    assert one["trades"] > 0 and two["trades"] >= one["trades"]
    assert one["trades_per_year"] * one["years"] == pytest.approx(one["trades"], rel=0.05)
    assert sum(y["profit"] for y in one["yearly"]) == pytest.approx(one["total_profit"], abs=len(one["yearly"]))
    # one slot can never be invested more than 100% of the time
    assert one["invested_pct_of_time"] <= 100
    # on a random walk the account should not show a big profit (costs + no edge)
    assert one["total_profit"] < 0.5 * 20000


def test_all_checks_must_be_positive():
    from engine.run import all_checks
    good = {"industry": "Fast Moving Consumer Goods", "news_sentiment": 0.2, "red_flags": [],
            "fundamentals": {"roe": 20, "debt_to_equity": 30, "profit_margin": 12, "earnings_growth": 8, "pe": 40},
            "trend": {"above_50": True, "above_200": True, "avg50_above_avg200": True, "ret_6m": 8, "ret_12m": 15,
                      "beats_nifty_6m": True, "rsi14": 58}}
    assert all(all_checks(good, 2).values())
    assert not all_checks({**good, "fundamentals": {}}, 2)["business"]                  # no data -> no pass
    assert not all_checks({**good, "fundamentals": {**good["fundamentals"], "roe": 8}}, 2)["business"]
    assert not all_checks({**good, "red_flags": ["fraud"]}, 2)["news"]
    assert not all_checks({**good, "news_sentiment": -0.3}, 2)["news"]
    assert not all_checks({**good, "trend": {**good["trend"], "rsi14": 82}}, 2)["price"]  # overheated
    assert not all_checks(good, 0)["market"]
    bank = {**good, "industry": "Financial Services", "fundamentals": {**good["fundamentals"], "debt_to_equity": 600}}
    assert all_checks(bank, 2)["business"]                                               # banks: no debt test


# ----------------------------------------------------------------- expected outcomes
def test_hold_path_safety_price_rises_and_locks_gains():
    from engine.backtest import _hold_path
    closes = np.array([100, 100, 110, 120, 130, 126, 100, 100.0])
    opens, lows = closes.copy(), closes.copy()
    lows[5] = 110.0                                    # falls through 12% under the 130 peak (= 114.4)
    r, early = _hold_path(opens, lows, closes, np.full(8, 2), 1, 7, 0.12, 100.0)
    assert early and r == pytest.approx(114.4 / 100 - 1 - config.ROUND_TRIP_COST, abs=1e-6)
    assert r > 0                                       # a gain was locked in, not given back


def test_hold_path_sells_when_market_turns_down():
    from engine.backtest import _hold_path
    closes = np.array([100, 101, 102, 103, 104, 105.0])
    reg = np.array([2, 2, 2, 0, 2, 2])
    r, early = _hold_path(closes.copy(), closes.copy(), closes, reg, 1, 5, 0.12, 100.0)
    assert early and r == pytest.approx(104 / 100 - 1 - config.ROUND_TRIP_COST, abs=1e-6)   # sold at the next open


def test_holding_outcomes_and_trade_percentiles_are_ordered():
    f = Features(random_walk_prices(seed=14, days=1100))
    inds = {s: ("A" if i % 4 == 0 else "B" if i % 4 == 1 else "C" if i % 4 == 2 else "D") for i, s in enumerate(SYMS)}
    out = backtest.holding_outcomes(f, inds, set(SYMS))
    for part in ("stock", "fund"):
        for k, r in out[part].items():
            if r.get("n"):
                assert r["p10"] <= r["p25"] <= r["median"] <= r["p75"] <= r["p90"]
                assert 0 <= r["pct_positive"] <= 100
    res = backtest.backtest_swing(f, SWING_STRATEGIES[1])
    assert res["p10_return"] <= res["median_return"] <= res["p90_return"]
    assert res["p25_return"] <= res["p75_return"] and res["days_median"] <= res["days_p90"]


def test_alert_carries_app_link(monkeypatch):
    from engine import notify
    sent = {}
    monkeypatch.setattr(config, "NTFY_TOPIC", "sf-test-topic")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "APP_URL", "https://example.github.io/indian_stock_finder/")
    monkeypatch.setattr(notify.requests, "post", lambda url, data=None, headers=None, timeout=None, **k: sent.update(url=url, headers=headers))
    notify.send("Title", "Body", "high")
    assert sent["url"].endswith("/sf-test-topic")
    assert sent["headers"]["Click"] == "https://example.github.io/indian_stock_finder/"
    assert "Open app" in sent["headers"]["Actions"] and sent["headers"]["Priority"] == "high"


def test_live_session_phases():
    from datetime import datetime
    from engine.live_session import phase
    mon = lambda h, m: datetime(2026, 11, 9, h, m)         # a Monday
    assert phase(mon(8, 50)) == "wait" and phase(mon(9, 9)) == "wait"
    assert phase(mon(9, 10)) == "run" and phase(mon(12, 0)) == "run" and phase(mon(15, 40)) == "run"
    assert phase(mon(15, 41)) == "after" and phase(mon(16, 19)) == "after"   # closed, waiting for final data
    assert phase(mon(16, 20)) == "done"                                     # time for the evening scan
    assert phase(datetime(2026, 11, 7, 11, 0)) == "skip"   # Saturday


def test_live_publish_keeps_a_single_commit(tmp_path, monkeypatch):
    import subprocess
    from engine import live_session
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    monkeypatch.setenv("LIVE_PUBLISH_URL", str(remote))
    f = tmp_path / "live.json"
    for n in (1, 2, 3):
        f.write_text('{"updated": %d}' % n, encoding="utf-8")
        assert live_session.publish([f])
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", live_session.BRANCH],
                         capture_output=True, text=True, check=True).stdout.strip().splitlines()
    assert len(log) == 1                                   # force-push: history never grows
    shown = subprocess.run(["git", "--git-dir", str(remote), "show", live_session.BRANCH + ":live.json"],
                           capture_output=True, text=True, check=True).stdout
    assert '"updated": 3' in shown


def test_unfinished_session_is_ignored():
    from datetime import datetime
    p = random_walk_prices(seed=15, days=300)
    last = p.close.index[-1]
    mid = datetime(last.year, last.month, last.day, 11, 0)            # market still open on the last candle's day
    cut = data.drop_unfinished_session(p, now=mid)
    assert len(cut.close) == 299 and len(cut.index_close) == 299
    late = datetime(last.year, last.month, last.day, 16, 30)          # after the close: candle is final
    assert len(data.drop_unfinished_session(p, now=late).close) == 300
    nxt = datetime(last.year, last.month, last.day, 9, 0) + __import__("datetime").timedelta(days=1)
    assert len(data.drop_unfinished_session(p, now=nxt).close) == 300  # next morning: yesterday is final


def _session_env(monkeypatch, start):
    """Run live_session.main with a fake clock; returns (clock, list of started workflows)."""
    from datetime import datetime
    from engine import live_session
    started = []
    clock = {"t": start}
    monkeypatch.setattr(live_session, "dispatch", lambda wf: started.append(wf) or True)
    monkeypatch.setattr(live_session, "publish", lambda files: True)
    monkeypatch.setattr(live_session.run, "run_live", lambda a: None)
    monkeypatch.setattr(live_session.run, "run_morning", lambda a: None)
    monkeypatch.setattr(live_session.data, "now_ist", lambda: clock["t"])
    return clock, started


def test_session_starts_the_evening_scan_after_the_close(monkeypatch):
    from datetime import datetime
    from engine import live_session
    clock, started = _session_env(monkeypatch, datetime(2026, 11, 9, 15, 30))        # Monday, still open
    steps = iter([datetime(2026, 11, 9, 15, 45), datetime(2026, 11, 9, 16, 21)])
    monkeypatch.setattr(live_session.time, "sleep", lambda s: clock.update(t=next(steps, clock["t"])))
    assert live_session.main([]) == 0
    assert started == ["daily.yml"]


def test_session_started_after_the_scan_time_does_nothing(monkeypatch):
    from datetime import datetime
    from engine import live_session
    clock, started = _session_env(monkeypatch, datetime(2026, 11, 9, 17, 0))
    assert live_session.main([]) == 0 and started == []                              # no duplicate evening scans


def test_session_hands_over_to_the_next_shift(monkeypatch):
    from datetime import datetime
    from engine import live_session
    clock, started = _session_env(monkeypatch, datetime(2026, 11, 9, 11, 0))
    monkeypatch.setattr(live_session.time, "sleep", lambda s: None)
    assert live_session.main(["--max-minutes", "-1"]) == 0                           # time limit already reached
    assert started == ["live-session.yml"]


def test_dispatch_calls_the_github_api(monkeypatch):
    from engine import live_session
    sent = {}
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(live_session.requests, "post",
                        lambda url, **k: sent.update(url=url, **k) or type("R", (), {"status_code": 204})())
    assert live_session.dispatch("daily.yml")
    assert sent["url"].endswith("/repos/o/r/actions/workflows/daily.yml/dispatches")
    assert sent["headers"]["Authorization"] == "Bearer x" and sent["json"]["ref"]


# ---------------------------------------------------------------- long-term champions
def _crash_market(years=12, seed=5):
    """Daily prices with two market crashes: a 30% fall in year 3 and a 25% fall in year 8."""
    from engine import champions  # noqa: F401
    dates = pd.bdate_range("2008-01-01", periods=252 * years)
    t = np.arange(len(dates))
    trend = np.exp(0.10 * t / 252)
    dip = np.ones(len(dates))
    for start, depth in ((252 * 3, 0.30), (252 * 8, 0.25)):
        n = 120                                          # 60 days down, 60 days back up
        shape = np.concatenate([np.linspace(0, 1, n // 2), np.linspace(1, 0, n // 2)])
        dip[start:start + n] *= 1 - depth * shape
    ix = pd.Series(10000 * trend * dip, index=dates)
    return dates, trend, dip, ix


def test_find_crashes_and_recovery_months():
    from engine import champions
    _, _, _, ix = _crash_market()
    cr = champions.find_crashes(ix)
    assert len(cr) == 2
    assert all(c["fall"] <= -15 for c in cr)
    assert all(c["recovered"] is not None and c["months"] > c["months_down"] for c in cr)


def test_fast_bouncer_beats_slow_one():
    from engine import champions
    dates, trend, dip, ix = _crash_market()
    rng = np.random.default_rng(3)
    noise = lambda: np.exp(np.cumsum(rng.normal(0, 0.004, len(dates))))
    # FAST: shallower dips, grows faster. SLOW: falls the same, then stays down for 3 years.
    slow_dip = dip.copy()
    for start in (252 * 3, 252 * 8):
        slow_dip[start + 60:start + 60 + 756] = slow_dip[start + 60]
    D = pd.DataFrame({"FAST": 100 * np.exp(0.18 * np.arange(len(dates)) / 252) * (1 - (1 - dip) / 2) * noise(),
                      "SLOW": 100 * trend * slow_dip * noise()}, index=dates)
    st = champions.study(D, ix, min_years=10, min_crashes=2)
    rows = {r["symbol"]: r for r in st["rows"]}
    fast, slow = rows["FAST"], rows["SLOW"]
    assert fast["signs"]["bounce"] and fast["signs"]["beats"] and fast["price_champion"]
    assert not slow["signs"]["bounce"] and not slow["price_champion"]
    assert fast["score"] > slow["score"]
    # FAST falls about half as far as the market and gets back sooner
    assert fast["crashes"][0][0] > st["crashes"][0]["fall"]
    assert fast["crashes"][0][1] <= st["crashes"][0]["months"]


def test_champion_rule_test_never_peeks_at_the_future():
    """Picks for a start year must not change when prices after that year change."""
    from engine import champions
    dates, trend, dip, ix = _crash_market(years=14)
    rng = np.random.default_rng(7)
    D = pd.DataFrame({f"S{i}": 100 * trend ** rng.uniform(0.5, 1.8) * dip ** rng.uniform(0.3, 1.5)
                      * np.exp(np.cumsum(rng.normal(0, 0.01, len(dates)))) for i in range(25)}, index=dates)
    inds = {c: f"ind{i % 6}" for i, c in enumerate(D.columns)}
    crashes = champions.find_crashes(ix)
    effects = [champions.crash_effects(D, c) for c in crashes]
    a = champions.rule_test(D, ix, inds, crashes, effects, starts=(2016,), top=5)
    D2 = D.copy()
    D2.loc["2016-01-01":] = D2.loc["2016-01-01":].to_numpy()[:, ::-1]   # scramble the future
    b = champions.rule_test(D2, ix, inds, crashes, effects, starts=(2016,), top=5)
    assert a and b and a[0]["picks"] == b[0]["picks"]


def test_business_signs_unknown_never_counts_against():
    from engine import champions
    assert champions.business_signs({}) == {"debt": None, "profit": None, "price": None, "dividend": None}
    b = champions.business_signs({"debt_to_equity": 250, "roe": 25, "profit_margin": 12, "pe": 95})
    assert b["debt"] is False and b["profit"] is True and b["price"] is False
    assert champions.business_signs({"roe": 30, "profit_margin": -3})["profit"] is False
