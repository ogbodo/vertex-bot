"""Risk / regime overlay — Python owns the risk authority (the EA is a dumb reconciler).

Turns a raw combined book into a DE-RISKED one, layering four controls that the research
identifies as the real robustness levers (not the signal):

  1. PORTFOLIO VOL TARGETING (Barroso-Santa-Clara) — scale gross so the book's trailing
     realized vol tracks a constant target. The single most evidence-backed crash reducer.
  2. MARKET-STRESS REGIME CUT — a stress score from realized-vol level + average pairwise
     correlation; convex extra de-risk when the regime turns dangerous (correlations →1).
  3. DRAWDOWN THROTTLE — gross scales down as the strategy's own trailing drawdown deepens.
  4. HARD KILL-SWITCH — at −kill_dd from peak, flatten to cash for a cooldown, then restart
     from a fresh peak. Does NOT depend on diversification holding (it can't, in a crisis).

Controls 3–4 are path-dependent (they read realized equity), so this is a proper
SEQUENTIAL day-by-day simulator, not a vectorized approximation. Undeployed gross = cash.
No lookahead: every multiplier at day t uses information through t and is applied to t+1.
"""

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _logistic(x):
    return 1.0 / (1.0 + np.exp(-x))


def market_stress(rets, vol_win=20, ref_win=504):
    """Daily stress score in [0,1] from (a) how extreme trailing market vol is vs its own
    2-year history, and (b) the average pairwise correlation across the book (crisis => →1).
    Both use only trailing data.

    `rets` should be a BROAD market panel (the context radar) — it need not equal the
    traded universe. A single-instrument book MUST still pass a multi-asset panel here,
    otherwise the correlation read is undefined (audit finding: n=1 used to divide by zero
    and silently disable the regime layer)."""
    port = rets.mean(axis=1)                                   # equal-weight market proxy
    vol = port.rolling(vol_win).std() * np.sqrt(TRADING_DAYS)
    volz = (vol - vol.rolling(ref_win).mean()) / vol.rolling(ref_win).std()
    stress_vol = _logistic(volz.clip(-4, 4))

    # average pairwise correlation proxy: for an equal-weight book,
    # Var(port) = (avg_var / N) * (1 + (N-1) * avg_corr)  =>  solve for avg_corr.
    # N is the PER-DAY count of live instruments (audit fix: was the static column count,
    # which mis-scaled early history and broke entirely for tiny universes).
    n = rets.notna().sum(axis=1).astype(float)
    n = n.where(n >= 2)                                        # need >=2 names for a correlation
    port_var = port.rolling(vol_win).var()
    avg_var = rets.rolling(vol_win).var().mean(axis=1).replace(0.0, np.nan)
    avg_corr = ((n * port_var / avg_var - 1.0) / (n - 1.0)).clip(0.0, 1.0)

    return (0.6 * stress_vol + 0.4 * avg_corr).clip(0.0, 1.0).fillna(0.0)


def run_stack(combined_pos, rets, cfg, rebal_days=21, cost_bps=2.0,
              vol_win=60, max_gross=2.0, stress_cut=0.7, dd_floor_mult=0.2, return_detail=False):
    """Sequential portfolio simulator with the full risk overlay. Returns the daily
    strategy-return Series (already risk-managed to ~the vol target). If return_detail,
    also returns a DataFrame of the actual (risk-scaled) positions held each day."""
    r = cfg.get("risk", {}) or {}
    target_vol = float(r.get("vol_target", 0.10))
    dd_floor = float(r.get("dd_throttle_floor", 0.15))
    kill_dd = float(r.get("kill_switch_dd", 0.20))

    # monthly-held target positions (hold between rebalances)
    held = combined_pos.copy()
    keep = (np.arange(len(held)) % rebal_days) == 0
    held.loc[~keep] = np.nan
    held = held.ffill().clip(-4, 4)

    # vol-target multiplier from the UN-scaled book's trailing realized vol (no circularity)
    book_ret = (held.shift(1) * rets).sum(axis=1, min_count=1)
    realized = book_ret.rolling(vol_win).std() * np.sqrt(TRADING_DAYS)
    vt = (target_vol / realized).replace([np.inf, -np.inf], np.nan).clip(upper=max_gross).fillna(0.0)

    stress = market_stress(rets)

    dates = held.index
    heldv = np.nan_to_num(held.values)
    retv = rets.reindex(columns=held.columns).values
    vtv = vt.values
    stv = stress.values

    equity, peak, killed, cooldown = 1.0, 1.0, False, 0
    prev = np.zeros(heldv.shape[1])
    out = np.full(len(dates), np.nan)
    pos_arr = np.zeros((len(dates), heldv.shape[1]))

    for t in range(len(dates) - 1):
        dd = equity / peak - 1.0
        if not killed and dd <= -kill_dd:            # hard stop -> flatten to cash
            killed, cooldown = True, rebal_days
        if killed:
            cooldown -= 1
            if cooldown <= 0:                        # re-enter after a cooldown, fresh peak
                killed, peak = False, equity

        dd_mult = np.clip(1.0 + dd / dd_floor, dd_floor_mult, 1.0)   # throttle as DD deepens
        regime_mult = 1.0 - stress_cut * stv[t]                     # cut hard when stressed
        gross = 0.0 if killed else float(np.clip(vtv[t] * regime_mult * dd_mult, 0.0, max_gross))

        pos = heldv[t] * gross
        pos_arr[t] = pos
        rn = np.nansum(pos * retv[t + 1]) - np.nansum(np.abs(pos - prev)) * (cost_bps / 1e4)
        prev = pos
        equity *= (1.0 + rn)
        peak = max(peak, equity)
        out[t + 1] = rn

    daily = pd.Series(out, index=dates)
    if return_detail:
        return daily, pd.DataFrame(pos_arr, index=dates, columns=held.columns)
    return daily
