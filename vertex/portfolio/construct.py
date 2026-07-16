"""Portfolio construction — net the sleeves into ONE book per instrument, then cap the
crypto bloc.

Two deliberate design choices, both from the research and the robustness objective:
  • FIXED, pre-registered risk budgets (config `sleeves:`), not optimized weights.
    Optimizing sleeve weights on history is the overfit trap, and risk-parity leverage
    is exactly what suffered a −26.7% structural failure in 2022. Fixed budgets are the
    honest, robust choice.
  • ONE net position per instrument (sleeves are summed, never stacked) — the anti-
    duplication rule: if two sleeves both want gold, they net into a single position;
    if they disagree, they partially cancel (correctly = lower conviction).
  • The crypto bloc (BTC+ETH, treated as one correlated unit) is capped at a fraction of
    total gross risk, because in a crash BTC/ETH move as one and must not dominate.
"""

import numpy as np

from vertex import sleeves
from vertex.data.panel import trade_proxies


def combine(close, cfg):
    """Net all configured sleeves at their risk budgets, then cap the crypto bloc.
    Returns a DataFrame of net target positions (index=dates, cols=TRADED instruments).

    `close` may carry radar-only context columns (used by the regime layer elsewhere);
    sleeves see ONLY the trade universe, so context instruments can never be positioned."""
    budgets = cfg.get("sleeves", {}) or {}
    idx = [i["proxy"] for i in cfg.get("universe", {}).get("indices", [])]
    tot = sum(budgets.values()) or 1.0

    trade_cols = [p["proxy"] for p in trade_proxies(cfg) if p["proxy"] in close.columns]
    trade_close = close[trade_cols]

    sleeve_params = cfg.get("sleeve_params", {}) or {}   # optional per-sleeve overrides (for WF selection)
    combined = None
    for name, b in budgets.items():
        params = dict(sleeve_params.get(name, {}))
        if name == "xsect":
            params["include"] = idx
        sl = sleeves.build(name, **params)
        p = sl.raw_positions(trade_close) * (float(b) / tot)
        combined = p if combined is None else combined.add(p, fill_value=0.0)

    return cap_crypto_bloc(combined, cfg)


def cap_crypto_bloc(pos, cfg):
    """Scale the crypto bloc DOWN (never up) so its gross never exceeds `crypto_bloc_cap`
    of the book's total gross on any day. BTC+ETH are treated as one correlated unit."""
    cap = float(cfg.get("risk", {}).get("crypto_bloc_cap", 0.15))
    crypto = [c["proxy"] for c in cfg.get("universe", {}).get("crypto", []) if c["proxy"] in pos.columns]
    if not crypto:
        return pos
    gross = pos.abs().sum(axis=1).replace(0.0, np.nan)
    crypto_frac = pos[crypto].abs().sum(axis=1) / gross
    scale = (cap / crypto_frac).clip(upper=1.0).fillna(1.0)   # only cap; leave sub-cap days alone
    pos = pos.copy()
    pos[crypto] = pos[crypto].mul(scale, axis=0)
    return pos
