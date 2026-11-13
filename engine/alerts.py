"""What the phone is told, and when. One place for the whole alert schedule:

* Every weekday evening (about 4:30 PM, after the scan): the plan for the next trading day - what to
  sell, what to buy - also on quiet days ("nothing to sell"), so silence always means something broke.
* 8:45 AM: a reminder, only when something must be sold or bought at the 9:15 opening.
* During market hours: SELL NOW when a short-term trade falls to its safety price (run.run_live).
* Once after an update of the alerts: a short note on what will come.

The phone cannot know which shares the user owns (that list stays on the phone), so long-term sell
alerts name the shares the app suggested: "if you own X, sell". The app itself shows the exact status.
Repository variable DAILY_DIGEST=off keeps quiet evenings quiet (only days with something to do).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from . import data, notify

log = logging.getLogger(__name__)

VERSION = 2                     # bump to send the one-time "what you will get" note again
EVENING_FROM = 15 * 60 + 30     # IST minutes: the day's final plan exists only after the close
QUIET_FROM, QUIET_TO = 21 * 60 + 30, 7 * 60     # no messages at night


def daily_digest_on() -> bool:
    return os.getenv("DAILY_DIGEST", "on").strip().lower() != "off"


def _mins(t: datetime) -> int:
    return t.hour * 60 + t.minute


def quiet_hours(now: datetime) -> bool:
    m = _mins(now)
    return m >= QUIET_FROM or m < QUIET_TO


def next_session(day: str) -> date:
    d = date.fromisoformat(day) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def day_label(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def inr(x) -> str:
    if x is None:
        return "-"
    return f"₹{x:,.0f}" if abs(x) >= 1000 else f"₹{x:,.2f}".rstrip("0").rstrip(".")


def short_name(x: dict) -> str:
    n = (x.get("name") or x["symbol"]).replace(" Limited", "").replace(" Ltd.", "").replace(" Ltd", "")
    return f"{n} ({x['symbol']})" if n.upper() != x["symbol"] else x["symbol"]


# ======================================================================= the evening plan
def mood(latest: dict) -> str:
    """One line on the market. The sell-everything rule is the guard (Nifty 3% below its 200-day average)."""
    m = latest.get("market") or {}
    g = (latest.get("lt_signals") or {}).get("guard") or m.get("guard") or {}
    reg = m.get("regime", 1)
    if g.get("out"):
        return "🔴 Market falling"
    return {2: "🟢 Market rising", 1: "🟡 Market unsure"}.get(reg, "🟠 Market weak")


def evening(latest: dict, prev: dict | None, results: list[str], short_plan: str) -> tuple[str, str, str]:
    """(title, body, priority) of the evening message, from the freshly built latest.json."""
    m = latest.get("market") or {}
    when = day_label(next_session(latest["data_date"]))
    lt = latest.get("lt_signals") or {}
    g = lt.get("guard") or m.get("guard") or {}
    sells, lines = [], []
    chg = m.get("nifty_change_pct")
    lines.append(f"{mood(latest)}. Nifty {m.get('nifty') or 0:,.0f}" + (f" ({chg:+.1f}% today)." if chg is not None else "."))
    if lt.get("guard_turned_down"):
        sells.append("🔴 The whole market has turned down: the Nifty closed more than 3% below its 200-day average. "
                     "Sell your long-term shares and the index fund at 9:15 AM and keep the money in a liquid fund "
                     "or FD. Gold can stay. The app will say when to buy again.")
    elif g.get("out"):
        lines.append(f"Buy nothing new while the market is down. The app says buy again once the Nifty closes "
                     f"above {g.get('buy_above') or 0:,.0f} (its 200-day average).")
    elif lt.get("guard_turned_up"):
        lines.append("🟢 The Nifty is back above its 200-day average: buying is allowed again.")
    elif g.get("below_avg") and g.get("sell_below"):
        lines.append(f"🟠 The Nifty is a little below its 200-day average. Keep holding: the app says sell only if it "
                     f"closes below {g['sell_below']:,.0f}.")
    for s in lt.get("sell", []):
        sells.append(f"• {short_name(s)}: {s['why']}")
    if short_plan:
        lines.append(short_plan)
    if results:
        lines.append("\n".join(results))
    buys = [b for b in lt.get("buy", []) if (b.get("max_entry") or 0) > 0] if not g.get("out") else []
    if buys:
        lines.append("🛒 To buy with new money (hold for a year or more):")
        lines += [f"• {short_name(b)} up to {inr(b.get('max_entry'))}" for b in buys[:4]]
    lines.append("Open the app for how many shares." if buys or sells else "✅ Nothing to do. Your long-term shares: keep holding.")
    body = ""
    if sells:
        body = f"💰 If you own these, sell at 9:15 AM {when}:\n" + "\n".join(sells) + "\n\n"
    body += "\n".join(x for x in lines if x)
    has_sell = bool(sells) or "SELL" in short_plan
    if has_sell:
        title = f"⚠️ Sell at 9:15 AM {when}"
    elif "BUY" in short_plan:
        title = f"🛒 Short trade at 9:15 AM {when}"
    else:
        title = f"Plan for {when}: nothing to sell ✅"
    return title, body.strip(), "high" if has_sell else "default"


def maybe_send_evening(latest: dict, prev: dict | None, results: list[str], short_plan: str,
                       now: datetime | None = None) -> bool:
    """Send the evening plan once per market day, only after the close and never at night."""
    now = now or data.now_ist()
    if not notify.channels():
        return False
    day = latest.get("data_date")
    st = notify.state()
    if not day or st.get("evening_for") == day:
        return False
    if day != now.date().isoformat() or _mins(now) < EVENING_FROM or quiet_hours(now):
        return False                       # a run during the day or at night: not the evening plan
    title, body, prio = evening(latest, prev, results, short_plan)
    quiet = prio == "default" and not short_plan and not results and not (latest.get("lt_signals") or {}).get("buy")
    if not daily_digest_on() and prio == "default":
        log.info("Quiet evening and DAILY_DIGEST=off: no message")
        notify.set_state(evening_for=day)
        return False
    res = notify.send(title, body, prio, kind="evening", tags=["calendar"] if not quiet else ["white_check_mark"])
    if any(r.get("ok") for r in res):
        notify.set_state(evening_for=day, regime=(latest.get("market") or {}).get("regime"))
        return True
    return False


# ======================================================================= the morning reminder
def morning(latest: dict, short_plan: str) -> tuple[str, str] | None:
    """Only when something must be done at the opening today."""
    lt = latest.get("lt_signals") or {}
    reg = (latest.get("market") or {}).get("regime", 1)
    parts = []
    if lt.get("guard_turned_down"):
        parts.append("🔴 Sell your long-term shares and the index fund at 9:15 AM: the market turned down "
                     "yesterday (Nifty below its 200-day average). Gold can stay.")
    if lt.get("sell"):
        parts.append("💰 If you own these, sell at 9:15 AM:\n" + "\n".join(f"• {short_name(s)}: {s['why']}" for s in lt["sell"]))
    if short_plan:
        parts.append(short_plan)
    if not parts:
        return None
    if reg >= 1 and not short_plan and not lt.get("sell") and not lt.get("guard_turned_down"):
        return None
    return "Today at 9:15 AM", "\n\n".join(parts)


# ======================================================================= one-time note
WELCOME = ("You will now get:\n"
           "• Every weekday at about 4:30 PM: the plan for the next day - what to sell, what to buy - also on "
           "quiet days, so you always know the alerts work.\n"
           "• 8:45 AM: a reminder, only when something must be bought or sold at the opening.\n"
           "Why it was quiet: alerts came only for short trades, and none was allowed since 8 Nov.\n"
           "Also new, after a test on 18 years of prices: the app sells everything only when the Nifty closes 3% "
           "below its 200-day average (fewer false alarms, smaller falls), and short trades are switched off "
           "(over 18 years they lost money after charges). Tap to open the app.")


def maybe_send_welcome(now: datetime | None = None) -> bool:
    now = now or data.now_ist()
    if not notify.channels() or quiet_hours(now) or notify.state().get("welcome", 0) >= VERSION:
        return False
    res = notify.send("🔔 Stock Finder alerts are on", WELCOME, "default", kind="welcome", tags=["bell"])
    if any(r.get("ok") for r in res):
        notify.set_state(welcome=VERSION)
        return True
    return False
