"""Daily v2 run (for the launchd agent): refresh the data panel, then write the risk
directive (every day) and — on the monthly rebalance window (days 1-3, redundant so it
never misses on a weekend/holiday) — the full target book.

Safe to run before the IC Markets demo exists: equity falls back to the config value and
the files simply sit unread. Fail-soft throughout so one bad day never kills the agent.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vertex.config import load_config
from vertex.data import panel
from vertex.execution import rebalance
from vertex import notify


def _clear_old_rebs(qdir):
    """Keep only the newest .reb — the EA uses the latest, so old ones just accumulate."""
    try:
        for f in os.listdir(qdir):
            if f.startswith("vxq_v2_rebalance_") and f.endswith(".reb"):
                os.remove(os.path.join(qdir, f))
    except Exception:
        pass


def main():
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cfg = load_config()

    # 1) refresh the frozen panel (fall back to the last one if the feed is down)
    try:
        close = panel.build_panel(cfg)
        panel.save_panel(cfg, close)
        src = "refreshed"
    except Exception as e:
        close = panel.load_panel(cfg)
        src = f"stale (refresh failed: {e})"

    # 2) compute + write the directive daily; the full book WEEKLY (Monday — matches the
    #    validated 5-trading-day cadence; the directive still de-risks daily in between)
    is_rebal_day = datetime.now().weekday() == 0
    equity, login = rebalance.account_info(cfg)
    state = rebalance.load_state(cfg)
    notionals, directive, new_state, diag = rebalance.compute(close, cfg, equity, state, login=login)

    qdir = (cfg.get("execution", {}) or {}).get("queue_dir")
    if is_rebal_day and qdir:
        _clear_old_rebs(qdir)
    rebalance.write_files(cfg, notionals, directive, write_reb=is_rebal_day)

    # Report to Telegram: always on a rebalance or a kill; on a quiet day only if the risk
    # gross moved materially (>=0.05) — avoids a same-every-day spam message.
    gross_val = 0.0 if directive == "FLATTEN" else float(directive)
    last_gross = float(state.get("last_gross", -1))
    notable = is_rebal_day or directive == "FLATTEN" or last_gross < 0 or abs(gross_val - last_gross) >= 0.05
    new_state["last_gross"] = gross_val
    rebalance.save_state(cfg, new_state)

    sec = cfg.get("secrets", {})
    if not sec.get("telegram_token"):
        tg = "no token"
    elif not notable:
        tg = "quiet (no material change)"
    else:
        tg = "sent" if notify.send_message(
            sec.get("telegram_token"), sec.get("telegram_chat_id"),
            notify.daily_report_text(close.index[-1].date(), equity, directive, diag, notionals, is_rebal_day)
        ) else "send FAILED"

    kind = "FULL REBALANCE (.reb + directive)" if is_rebal_day else "risk directive only"
    print(f"[{stamp}] panel {src} | {kind} | equity ${equity:,.0f} | "
          f"gross={gross_val:.3f} directive={directive} | {len(notionals)} targets | telegram={tg}"
          + (f" | UNMAPPED {diag['unmapped']}" if diag["unmapped"] else ""))


if __name__ == "__main__":
    main()
