"""Alert delivery. All channels are optional and switched on by GitHub secrets:

* ntfy.sh      - free push notifications to a phone app, no account needed (NTFY_TOPIC)
* Telegram     - bot message (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
* Gmail (SMTP) - email using an app password (GMAIL_USER + GMAIL_APP_PASSWORD + ALERT_EMAIL_TO)

Every message is checked (did the service accept it?) and written to docs/data/alerts.json together
with the alert schedule's memory (which evening plan was already sent, and so on). The app shows
"last phone alert: sent 4:24 PM" from it, so a failure is visible instead of silent.
"""
from __future__ import annotations

import json
import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests

from . import config

log = logging.getLogger(__name__)

FILE = "alerts.json"
LOG_KEEP = 40
PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
RETRY_STATUS = {429, 500, 502, 503, 504}


def channels() -> list[str]:
    ch = []
    if config.NTFY_TOPIC:
        ch.append("ntfy")
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        ch.append("telegram")
    if config.SMTP_USER and config.SMTP_PASSWORD and config.ALERT_EMAIL_TO:
        ch.append("email")
    return ch


def _ntfy(title: str, body: str, priority: str, tags: list[str]) -> dict:
    """Publish as JSON (safe for ₹, emoji and long text), retrying when the service is busy."""
    payload = {"topic": config.NTFY_TOPIC, "title": title, "message": body,
               "priority": PRIORITY.get(priority, 3), "tags": tags}
    if config.APP_URL:
        payload["click"] = config.APP_URL                                  # tap the notification
        payload["actions"] = [{"action": "view", "label": "Open app", "url": config.APP_URL}]
    res: dict = {"channel": "ntfy", "ok": False}
    for attempt in range(3):
        try:
            r = requests.post(config.NTFY_SERVER, json=payload, timeout=20)
            res = {"channel": "ntfy", "ok": r.status_code == 200, "status": r.status_code}
            if res["ok"]:
                return res
            res["error"] = (r.text or "")[:160].strip()
            if r.status_code not in RETRY_STATUS:
                break
        except requests.RequestException as exc:
            res = {"channel": "ntfy", "ok": False, "status": None, "error": str(exc)[:160]}
        time.sleep(4 * (attempt + 1))
    log.warning("ntfy failed: %s", res)
    return res


def _telegram(title: str, body: str) -> dict:
    text = f"{title}\n{body}" + (f"\n\n{config.APP_URL}" if config.APP_URL else "")
    try:
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
                          timeout=20)
        ok = r.status_code == 200
        res = {"channel": "telegram", "ok": ok, "status": r.status_code}
        if not ok:
            res["error"] = (r.text or "")[:160]
    except requests.RequestException as exc:
        res = {"channel": "telegram", "ok": False, "status": None, "error": str(exc)[:160]}
    if not res["ok"]:
        log.warning("telegram failed: %s", res)
    return res


def _email(title: str, body: str) -> dict:
    try:
        msg = MIMEText(body + (f"\n\nOpen the app: {config.APP_URL}" if config.APP_URL else ""), "plain", "utf-8")
        msg["Subject"], msg["From"], msg["To"] = title, config.SMTP_USER, config.ALERT_EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
        return {"channel": "email", "ok": True}
    except Exception as exc:
        log.warning("email failed: %s", exc)
        return {"channel": "email", "ok": False, "error": str(exc)[:160]}


def send(title: str, body: str, priority: str = "default", kind: str = "alert",
         tags: list[str] | None = None) -> list[dict]:
    """Send to every configured channel. Returns one result per channel (also recorded in alerts.json)."""
    if not body.strip():
        return []
    results = []
    if config.NTFY_TOPIC:
        results.append(_ntfy(title, body, priority, tags or ["chart_with_upwards_trend"]))
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        results.append(_telegram(title, body))
    if config.SMTP_USER and config.SMTP_PASSWORD and config.ALERT_EMAIL_TO:
        results.append(_email(title, body))
    if results:
        record(title, body, kind, results)
    return results


# ======================================================================= alerts.json: log + memory
def load() -> dict:
    try:
        d = json.loads((config.OUT_DIR / FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d: dict) -> None:
    d["updated"] = datetime.now(timezone.utc).isoformat()
    d["channels"] = channels()
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    (config.OUT_DIR / FILE).write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def record(title: str, body: str, kind: str, results: list[dict]) -> None:
    d = load()
    entry = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "title": title, "text": body[:600],
             "ok": any(r.get("ok") for r in results), "results": results}
    d["log"] = ([entry] + d.get("log", []))[:LOG_KEEP]
    d["last"] = entry
    if entry["ok"]:
        d["last_ok"] = entry["at"]
    else:
        d["last_error"] = {"at": entry["at"], "title": title,
                           "error": "; ".join(f"{r['channel']}: {r.get('status') or ''} {r.get('error', '')}".strip()
                                              for r in results if not r.get("ok"))}
    save(d)


def state() -> dict:
    return load().get("state", {})


def set_state(**kw) -> None:
    d = load()
    d["state"] = {**d.get("state", {}), **kw}
    save(d)


# ======================================================================= trade event lines
def rupees(x) -> str:
    return f"₹{x:,.2f}" if x is not None else "-"


def format_event(e: dict) -> str:
    t = e["trade"]
    s = t["symbol"]
    kind = e["type"]
    if kind == "entered":
        return f"✅ BOUGHT {s} at {rupees(t['entry'])}. Stop {rupees(t['stop'])}, target {rupees(t.get('target'))}"
    if kind == "target":
        return f"🎯 TARGET HIT {s}: sold {rupees(t['exit'])} ({t['ret_pct']:+.1f}%)"
    if kind == "stop":
        return f"🛑 STOP-LOSS {s}: sold {rupees(t['exit'])} ({t['ret_pct']:+.1f}%)"
    if kind == "exit_rule":
        return f"💰 EXIT {s}: sell signal at {rupees(t['exit'])} ({t['ret_pct']:+.1f}%)"
    if kind == "time":
        return f"⏱ TIME EXIT {s} at {rupees(t['exit'])} ({t['ret_pct']:+.1f}%)"
    if kind == "sell_signal":
        return f"💰 SELL {s} at tomorrow's market open (9:15 AM). The trade is complete."
    if kind == "sold":
        return f"✔ {s} sold at {rupees(t['exit'])} ({t['ret_pct']:+.1f}%)"
    if kind == "expired":
        return f"⏭ SKIPPED {s}: opened outside the buy range"
    if kind == "stop_raised":
        return f"⬆ {s}: trailing stop raised to {rupees(t['stop'])}"
    if kind == "live_stop":
        return (f"🔴 SELL NOW: all your {s} shares. It fell to the safety price {rupees(t['stop'])} "
                f"(now {rupees(e['price'])}). Selling now keeps the loss small.")
    if kind == "live_skip":
        return (f"⏭ Don't buy {s} today: it opened at {rupees(e['price'])}, outside the buy range "
                f"(up to {rupees(t['max_entry'])}). No action needed.")
    if kind == "live_target":
        return f"🎯 {s} reached target {rupees(t['target'])} (now {rupees(e['price'])}). Consider booking profit."
    return f"{kind}: {s}"
