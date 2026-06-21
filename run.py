#!/usr/bin/env python3
"""cfs-trader giriş noktası.

  python3 run.py            # sürekli servis (15dk döngü)
  python3 run.py --once     # tek tarama tiki (test/dry-run)
  python3 run.py --status   # mevcut durum (açık pozisyonlar + günlük state)
"""
import argparse
from cfs_trader.loop import Ctx, scan_tick, poll_tick, run_forever, _utcday


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="tek tik çalıştır ve çık")
    ap.add_argument("--status", action="store_true", help="durumu yazdır ve çık")
    a = ap.parse_args()

    ctx = Ctx()
    cfg = ctx.cfg

    if a.status:
        day = _utcday()
        st = ctx.store.day_state(day)
        print(f"mode={cfg.mode} dry_run={cfg.dry_run} budget={cfg.budget} lev={cfg.risk['leverage']}x")
        print(f"gün {day}: PnL {st['realized_pnl']:+.2f} | işlem {st['trades_count']} | "
              f"ardışık-zarar {st['consec_losses']} | halted={bool(st['halted'])}")
        opens = ctx.store.open_trades()
        print(f"açık pozisyon: {len(opens)}")
        for t in opens:
            print(f"  #{t['id']} {t['symbol']} {t['side']} qty={t['qty']} entry={t['entry']} "
                  f"SL={t['sl']} TP={t['tp']} risk={t['risk_usdt']}")
        return

    if a.once:
        poll_tick(ctx)
        scan_tick(ctx)
        return

    run_forever(ctx)


if __name__ == "__main__":
    main()
