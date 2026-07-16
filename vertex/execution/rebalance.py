"""Live execution layer — compute the current target book + risk directive and write the
files the EA reconciles against. This mirrors the validated backtest (construct + overlay)
for the LATEST bar, with persisted equity-peak state for the path-dependent drawdown/kill
logic (so the live risk dial matches what the backtest simulated day-by-day).

Files written into execution.queue_dir:
  • vxq_v2_rebalance_<ts>.reb  — one line `SYMBOL|target_notional` (IC Markets symbols),
    sized at gross-multiplier = 1 (the full target book). Written monthly.
  • vxq_v2_risk_state.txt      — line 1: valid-until epoch (freshness); line 2: the gross
    MULTIPLIER (0..max) the EA applies to every target, or the word FLATTEN. Written daily,
    so the risk dial can de-risk intra-month without rewriting the book.
Read back:
  • vxq_v2_account.json        — {equity, ...} the EA publishes (fallback: config demo balance).

SIZING: notional_i = equity * raw_i, where raw_i is the netted combined position. Holding
that USD notional makes instrument i contribute raw_i * ret_i to the fractional book return
— identical to the backtest — so the EA's fills reproduce the validated risk. The EA then
multiplies by the directive's gross multiplier and converts to lots via live contract size.
"""

import json
import os
import time
from datetime import datetime, timezone

import numpy as np

from vertex.data import panel
from vertex.portfolio import construct
from vertex.risk import overlay

TRADING_DAYS = 252
DIRECTIVE_TTL = 90000       # ~25h: a daily run keeps it fresh; staler => EA holds (dead-man)
MAX_STALE_DAYS = 7          # refuse to hold an instrument whose feed died (no real print in this long)


def _qdir(cfg):
    return (cfg.get("execution", {}) or {}).get("queue_dir")


def _state_path(cfg):
    d = os.path.join(cfg["_root"], "data", "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "exec_state.json")


def load_state(cfg):
    try:
        return json.load(open(_state_path(cfg)))
    except Exception:
        return {"peak_equity": 0.0, "killed": False, "cooldown": 0}


def save_state(cfg, s):
    try:
        json.dump(s, open(_state_path(cfg), "w"))
    except Exception:
        pass


def account_info(cfg):
    """(equity, login) from the EA-published account file; login is None if unknown
    (pre-attach, or an older EA build that doesn't export it)."""
    qd = _qdir(cfg)
    if qd:
        try:
            d = json.load(open(os.path.join(qd, "vxq_v2_account.json")))
            return float(d["equity"]), (int(d["login"]) if d.get("login") else None)
        except Exception:
            pass
    return float((cfg.get("execution", {}) or {}).get("demo_balance_fallback", 10000)), None


def account_equity(cfg):
    return account_info(cfg)[0]


def compute(close, cfg, equity, state, login=None):
    """Return (notionals {sym: usd}, directive (float gross | 'FLATTEN'), new_state, diag).

    Safety semantics (audit fixes):
      • state is BOUND to the account login — a different login (or first sight of one)
        resets peak/kill state instead of carrying another account's drawdown across.
      • instruments whose feed is dead (no REAL print within MAX_STALE_DAYS, per the
        panel's pre-ffill meta) are dropped — never hold a phantom position.
      • the kill-switch cooldown ticks once per calendar day, not once per call."""
    # -- account binding: never apply one account's peak/kill state to another --
    if login is not None:
        prev = state.get("login")
        if prev is not None and int(prev) != int(login):
            state = {"peak_equity": 0.0, "killed": False, "cooldown": 0}
        state["login"] = int(login)

    rets = close.pct_change()
    combined = construct.combine(close, cfg)
    raw = combined.iloc[-1].dropna()

    # -- dead-feed guard: drop instruments with no real (pre-ffill) print recently --
    stale = []
    meta = panel.load_meta(cfg)
    if meta:
        import pandas as pd
        today = close.index[-1]
        for proxy in list(raw.index):
            lr = meta.get(proxy)
            if lr and (today - pd.Timestamp(lr)).days > MAX_STALE_DAYS:
                stale.append(proxy)
                raw = raw.drop(proxy)

    smap = cfg.get("broker_symbols", {}) or {}
    notionals, unmapped = {}, []
    for proxy, val in raw.items():
        if abs(float(val)) < 1e-9:
            continue
        sym = smap.get(proxy)
        if not sym:
            unmapped.append(proxy)
            continue
        notionals[sym] = round(equity * float(val), 2)

    r = cfg.get("risk", {}) or {}
    target_vol = float(r.get("vol_target", 0.10))
    dd_floor = float(r.get("dd_throttle_floor", 0.15))
    kill_dd = float(r.get("kill_switch_dd", 0.20))

    book_ret = (combined.shift(1) * rets).sum(axis=1, min_count=1)
    realized = float((book_ret.rolling(60).std() * np.sqrt(TRADING_DAYS)).iloc[-1])
    vt = min(2.0, target_vol / realized) if realized and realized > 0 else 0.0
    stress = float(overlay.market_stress(rets).iloc[-1])
    regime_mult = 1.0 - 0.7 * stress

    peak = max(float(state.get("peak_equity", 0.0)), equity)
    dd = (equity / peak - 1.0) if peak > 0 else 0.0
    killed = bool(state.get("killed", False))
    cooldown = int(state.get("cooldown", 0))
    if not killed and dd <= -kill_dd:
        killed, cooldown = True, 21
    if killed:
        # cooldown ticks once per CALENDAR DAY (audit fix: was per call, so manual test
        # runs could burn the whole cooldown off in minutes)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state.get("cooldown_date") != today_str:
            cooldown -= 1
            state["cooldown_date"] = today_str
        if cooldown <= 0:
            killed, peak = False, equity
    dd_mult = max(0.2, min(1.0, 1.0 + dd / dd_floor))

    gross = 0.0 if killed else max(0.0, min(2.0, vt * regime_mult * dd_mult))
    directive = "FLATTEN" if killed else round(gross, 3)
    new_state = {"peak_equity": peak, "killed": killed, "cooldown": cooldown,
                 "cooldown_date": state.get("cooldown_date"), "login": state.get("login")}
    diag = {"equity": equity, "realized_vol": realized, "vt": vt, "stress": stress,
            "regime_mult": regime_mult, "dd": dd, "dd_mult": dd_mult, "gross": gross,
            "unmapped": unmapped, "stale_dropped": stale}
    return notionals, directive, new_state, diag


def write_files(cfg, notionals, directive, write_reb=True):
    """Write the risk directive (always) and, on a rebalance day, the .reb book."""
    qd = _qdir(cfg)
    if not qd:
        raise RuntimeError("no execution.queue_dir in config")
    os.makedirs(qd, exist_ok=True)
    with open(os.path.join(qd, "vxq_v2_risk_state.txt"), "w") as f:
        f.write(f"{int(time.time()) + DIRECTIVE_TTL}\n{directive}\n")
    path = None
    if write_reb:
        path = os.path.join(qd, f"vxq_v2_rebalance_{int(time.time())}.reb")
        with open(path, "w") as f:
            for sym, n in notionals.items():
                f.write(f"{sym}|{n:.2f}\n")
    return path
