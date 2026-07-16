"""Compute and (optionally) write the live target book + risk directive for the EA.

  .venv/bin/python scripts/rebalance.py --dry        # print only, write nothing
  .venv/bin/python scripts/rebalance.py --risk-only  # write only the daily risk directive
  .venv/bin/python scripts/rebalance.py              # write the .reb book + risk directive
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vertex.config import load_config
from vertex.data import panel
from vertex.execution import rebalance


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print only; write nothing")
    ap.add_argument("--risk-only", action="store_true", help="write only the risk directive, not the .reb")
    ap.add_argument("--equity", type=float, default=None, help="override equity (what-if sizing demos)")
    ap.add_argument("--reset-state", action="store_true", help="wipe peak/kill state (e.g. after an account/balance change)")
    args = ap.parse_args()

    cfg = load_config()
    if args.reset_state:
        sp = rebalance._state_path(cfg)
        if os.path.exists(sp):
            os.remove(sp)
        print("exec state reset (peak/kill cleared)")
    close = panel.load_panel(cfg)
    equity, login = rebalance.account_info(cfg)
    if args.equity is not None:
        equity = args.equity
    state = rebalance.load_state(cfg)
    notionals, directive, new_state, diag = rebalance.compute(close, cfg, equity, state, login=login)
    if diag.get("stale_dropped"):
        print(f"  ⚠ dead-feed guard dropped: {diag['stale_dropped']}")

    print(f"\nLIVE REBALANCE  (as of {close.index[-1].date()})  equity ${equity:,.0f}")
    print(f"  book realized vol {diag['realized_vol']*100:.1f}%  ->  vol-target x{diag['vt']:.2f}")
    print(f"  market stress {diag['stress']:.2f} -> regime x{diag['regime_mult']:.2f} | "
          f"drawdown {diag['dd']*100:+.1f}% -> throttle x{diag['dd_mult']:.2f}")
    print(f"  RISK DIRECTIVE (gross multiplier applied by the EA): {directive}")
    if diag["unmapped"]:
        print(f"  ⚠ unmapped (no IC Markets symbol, skipped): {diag['unmapped']}")
    gross_notional = sum(abs(v) for v in notionals.values())
    print(f"\n  TARGET BOOK — {len(notionals)} positions, gross ${gross_notional:,.0f} "
          f"({gross_notional/equity:.1f}x equity at gross=1; EA scales by the directive):")
    for sym, n in sorted(notionals.items(), key=lambda kv: -abs(kv[1])):
        side = "LONG " if n > 0 else "SHORT"
        print(f"    {side} {sym:<9} ${n:>12,.0f}")

    if args.dry:
        print("\n  (--dry: nothing written)")
        return
    path = rebalance.write_files(cfg, notionals, directive, write_reb=not args.risk_only)
    rebalance.save_state(cfg, new_state)
    print(f"\n  wrote risk directive" + (f" + {os.path.basename(path)}" if path else " (risk-only)"))


if __name__ == "__main__":
    main()
