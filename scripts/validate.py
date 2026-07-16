"""Run a sleeve (default: slow_trend) through the validation engine:
full-sample + in/out-of-sample Sharpe, Deflated Sharpe (multiple-testing honest), and
the crisis-window stress gates. Returns/drawdowns are shown scaled to the 10% portfolio
vol target for interpretability; Sharpe is scale-free.

  .venv/bin/python scripts/validate.py [sleeve_name] [n_trials]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from vertex.config import load_config
from vertex.data import panel
from vertex import sleeves
from vertex.validation import backtester, crisis, metrics

EVAL_START = "2000-01-01"   # the diversified multi-asset era (crypto/indices/FX all live)
VOL_TARGET = 0.10


def _fmt(m, label):
    if not m or not m.get("n"):
        return f"  {label:<10} (insufficient data)"
    return (f"  {label:<10} Sharpe {m['sharpe']:+5.2f} | ann {m['ann_ret']*100:+6.1f}% | "
            f"vol {m['ann_vol']*100:4.1f}% | maxDD {m['maxdd']*100:6.1f}% | hit {m['hit']*100:4.1f}% | n={m['n']}")


def main():
    sleeve_name = sys.argv[1] if len(sys.argv) > 1 else "slow_trend"
    n_trials = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    cfg = load_config()
    close = panel.load_panel(cfg)
    close = close[close.index >= pd.Timestamp(EVAL_START)]
    rets = close.pct_change()

    names = [n.strip() for n in sleeve_name.split(",") if n.strip()]
    budgets = cfg.get("sleeves", {})
    idx = [i["proxy"] for i in cfg.get("universe", {}).get("indices", [])]

    def mk(n):
        # xsect ranks WITHIN the index bloc, so inject the index universe
        return sleeves.build(n, include=idx) if n == "xsect" else sleeves.build(n)

    if names == ["stack"]:
        # the full risk-managed stack: net sleeves + crypto cap + vol target + regime + kill
        from vertex.portfolio import construct
        from vertex.risk import overlay
        combined = construct.combine(close, cfg)
        daily = overlay.run_stack(combined, rets, cfg, rebal_days=cfg.get("rebalance_days", 21))
        label = "FULL STACK — slow+fast+xsect · crypto cap · vol target · regime · kill-switch"
        d10 = daily.dropna()                        # already risk-managed to ~the vol target
    else:
        if len(names) == 1:
            pos = mk(names[0]).raw_positions(close)
            label = names[0]
        else:
            # net per instrument, weighted by (renormalized) risk budgets — never stacked
            w = {n: float(budgets.get(n, 1.0)) for n in names}
            tot = sum(w.values()) or 1.0
            pos = None
            for n in names:
                p = mk(n).raw_positions(close) * (w[n] / tot)
                pos = p if pos is None else pos.add(p, fill_value=0.0)
            label = " + ".join(f"{n}({w[n] / tot:.0%})" for n in names)
        daily = backtester.simulate(pos, rets, rebal_days=cfg.get("rebalance_days", 21), cost_bps=2.0)
        # scale to the 10% vol target for interpretable return/DD (Sharpe unchanged)
        full = metrics.summary(daily)
        k = VOL_TARGET / full["ann_vol"] if full["ann_vol"] > 0 else 1.0
        d10 = (daily * k).dropna()

    print(f"\n{'='*74}\nVALIDATION — '{label}'   ({close.index[0].date()} -> {close.index[-1].date()})")
    if names == ["stack"]:
        print("(stack runs at its own risk-managed vol — returns/DD as produced; Sharpe is scale-free)\n")
    else:
        print(f"(returns/DD scaled to {int(VOL_TARGET*100)}% annual vol; Sharpe is scale-free)\n")

    cut = int(len(d10) * 0.70)
    is_, oos = d10.iloc[:cut], d10.iloc[cut:]
    print(_fmt(metrics.summary(d10), "FULL"))
    print(_fmt(metrics.summary(is_), "IN-SAMPLE"))
    print(_fmt(metrics.summary(oos), "OOS"))

    psr = metrics.probabilistic_sharpe(daily, sr_benchmark_ann=0.0)
    dsr = metrics.deflated_sharpe(daily, n_trials=n_trials)
    print(f"\n  Probabilistic Sharpe (>0):        {psr*100:5.1f}%   (P the true Sharpe beats zero)")
    print(f"  Deflated Sharpe (N={n_trials} trials):     {dsr*100:5.1f}%   (P skill is real after multiple-testing)")

    print("\n  CRISIS-WINDOW STRESS GATES:")
    for c in crisis.report(d10):
        s = c["stats"]
        if not s:
            print(f"    {c['name']:<18} (before data start)")
            continue
        flag = "OK" if (c["kind"] == "sustained" and s["total_ret"] > 0) or \
                        (c["kind"] != "sustained" and s["total_ret"] > -0.10) else "REVIEW"
        print(f"    {c['name']:<18} {c['kind']:<9} ret {s['total_ret']*100:+6.1f}% | "
              f"maxDD {s['maxdd']*100:6.1f}% | Sharpe {s['sharpe']:+5.2f}  [{flag}]  — {c['expect']}")

    print(f"\n  Read: OOS Sharpe + Deflated Sharpe are the honest headline. Crisis gates check\n"
          f"  sustained bears are green and sharp shocks are only a small controlled red.\n")


if __name__ == "__main__":
    main()
