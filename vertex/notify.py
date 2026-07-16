"""Telegram reporting for Eshu Forex Trader — pure transport plumbing (HTTP to the Bot API) plus
a report formatter. Fail-soft: a missing token or a network hiccup never breaks a run.
Token + chat id come ONLY from the environment (.env via config.secrets), never hard-coded.
"""

import requests

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(token, chat_id, text, parse_mode="HTML"):
    """POST a message. Returns True on success, False (never raises) on any failure."""
    if not token or not chat_id:
        return False
    try:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = requests.post(_API.format(token=token), data=payload, timeout=10)
        return bool(r.ok)
    except Exception:
        return False


def daily_report_text(as_of, equity, directive, diag, notionals, is_rebal):
    """Format the daily/rebalance status report (HTML). Honest framing — a risk/status
    report, never a 'trade signal'."""
    if directive == "FLATTEN":
        return (f"🚨 <b>Eshu Forex Trader — KILL-SWITCH</b>  ({as_of})\n"
                f"Drawdown breached the −20% floor → <b>flattening the book to cash</b>.\n"
                f"Equity: <b>${equity:,.0f}</b>\n"
                f"<i>Systematic CTA · robustness-first · will re-enter after a cooldown.</i>")

    header = "🔄 <b>Eshu Forex Trader — REBALANCE</b>" if is_rebal else "🤖 <b>Eshu Forex Trader — daily</b>"
    gross = sum(abs(v) for v in notionals.values())
    lines = [f"{header}  ({as_of})",
             f"Equity: <b>${equity:,.0f}</b>",
             f"Risk gross: <b>×{directive}</b>  "
             f"(vol-tgt ×{diag['vt']:.2f} · regime ×{diag['regime_mult']:.2f} · "
             f"dd {diag['dd']*100:+.0f}% ×{diag['dd_mult']:.2f})",
             f"Book: {len(notionals)} positions · gross ${gross:,.0f} ({gross/equity:.1f}× at gross=1)"]
    if is_rebal:
        for sym, n in sorted(notionals.items(), key=lambda kv: -abs(kv[1]))[:6]:
            lines.append(f"  {'L' if n > 0 else 'S'} <code>{sym}</code> ${n:,.0f}")
    lines.append("<i>Systematic CTA · robustness-first · status/risk report, not a trade signal.</i>")
    return "\n".join(lines)
