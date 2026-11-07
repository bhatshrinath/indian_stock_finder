"""Alert delivery. All channels are optional and switched on by GitHub secrets:

* ntfy.sh      - free push notifications to a phone app, no account needed (NTFY_TOPIC)
* Telegram     - bot message (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
* Gmail (SMTP) - email using an app password (GMAIL_USER + GMAIL_APP_PASSWORD + ALERT_EMAIL_TO)
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

import requests

from . import config

log = logging.getLogger(__name__)


def channels() -> list[str]:
    ch = []
    if config.NTFY_TOPIC:
        ch.append("ntfy")
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        ch.append("telegram")
    if config.SMTP_USER and config.SMTP_PASSWORD and config.ALERT_EMAIL_TO:
        ch.append("email")
    return ch


def send(title: str, body: str, priority: str = "default") -> None:
    if not body.strip():
        return
    if config.NTFY_TOPIC:
        try:
            headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "chart_with_upwards_trend"}
            if config.APP_URL:
                headers["Click"] = config.APP_URL
            requests.post(f"{config.NTFY_SERVER}/{config.NTFY_TOPIC}", data=body.encode("utf-8"),
                          headers=headers, timeout=15)
        except Exception as exc:
            log.warning("ntfy failed: %s", exc)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            text = f"*{title}*\n{body}" + (f"\n\n{config.APP_URL}" if config.APP_URL else "")
            requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                                "parse_mode": "Markdown", "disable_web_page_preview": True},
                          timeout=15)
        except Exception as exc:
            log.warning("telegram failed: %s", exc)
    if config.SMTP_USER and config.SMTP_PASSWORD and config.ALERT_EMAIL_TO:
        try:
            msg = MIMEText(body + (f"\n\nOpen the app: {config.APP_URL}" if config.APP_URL else ""),
                           "plain", "utf-8")
            msg["Subject"], msg["From"], msg["To"] = title, config.SMTP_USER, config.ALERT_EMAIL_TO
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        except Exception as exc:
            log.warning("email failed: %s", exc)


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
    if kind == "expired":
        return f"⏭ SKIPPED {s}: opened outside the buy range"
    if kind == "stop_raised":
        return f"⬆ {s}: trailing stop raised to {rupees(t['stop'])}"
    if kind == "live_stop":
        return f"⚠ {s} touched stop-loss {rupees(t['stop'])} (now {rupees(e['price'])}). Consider selling."
    if kind == "live_target":
        return f"🎯 {s} reached target {rupees(t['target'])} (now {rupees(e['price'])}). Consider booking profit."
    return f"{kind}: {s}"
