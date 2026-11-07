"""Optional: plain-English research notes written by Claude.

Enabled only when the ANTHROPIC_API_KEY secret is set. One request per daily
run covers all top picks. The model only summarises the facts we give it
(technicals, backtest stats, headlines); it is told not to invent numbers.
"""
from __future__ import annotations

import json
import logging
import re

from . import config

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a careful equity research assistant for a retired Indian investor with "
    "limited trading experience. Write in simple, calm English. Use ONLY the facts provided; "
    "never invent numbers, events or price targets. Mention the main risk for every stock. "
    "Do not use hype words. Each note is 2-3 short sentences."
)


def write_notes(market: dict, picks: list[dict]) -> dict[str, str]:
    if not config.ANTHROPIC_API_KEY or not picks:
        return {}
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; skipping AI notes")
        return {}

    facts = []
    for p in picks:
        facts.append({
            "symbol": p["symbol"], "company": p.get("name"), "industry": p.get("industry"),
            "type": p.get("kind"), "setup": p.get("strategy_name"),
            "price": p.get("price"), "stop": p.get("stop"), "target": p.get("target"),
            "historical_win_rate_pct": p.get("hist_win_rate"),
            "relative_strength_percentile": p.get("rs_percentile"),
            "return_6m_pct": p.get("ret_6m"), "delivery_pct": p.get("delivery_pct"),
            "fundamentals": p.get("fundamentals"), "warnings": p.get("warnings"),
            "headlines": [n["title"] for n in (p.get("news") or [])[:4]],
        })
    prompt = (
        "Market context:\n" + json.dumps(market, default=str) +
        "\n\nStocks:\n" + json.dumps(facts, default=str) +
        "\n\nReturn ONLY a JSON object mapping each symbol to its note, e.g. "
        '{"TCS": "note...", "INFY": "note..."}. Also include a key "_market" with a '
        "2-sentence summary of today's market mood for the investor."
    )
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.beta.messages.create(
            model=config.AI_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            log.warning("AI notes: request declined")
            return {}
        text = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        notes = json.loads(m.group(0)) if m else {}
        return {k: str(v) for k, v in notes.items()}
    except anthropic.RateLimitError as exc:
        log.warning("AI notes rate limited: %s", exc)
    except anthropic.APIStatusError as exc:
        log.warning("AI notes API error %s: %s", exc.status_code, exc)
    except anthropic.APIConnectionError as exc:
        log.warning("AI notes connection error: %s", exc)
    except (json.JSONDecodeError, AttributeError) as exc:
        log.warning("AI notes unparseable: %s", exc)
    return {}
