#!/usr/bin/env python3
"""#3 SL-bandı testi — size_position yalnız [sl_min_pct, max_sl_pct] bandında işlem açar.
Dar (<2%) ve geniş (>6%) SL'ler REDDEDİLİR; 2-6% bandı geçer (risk/qty hesabı bozulmadan)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader.cfg import get as get_cfg
from cfs_trader.signals import Candidate
from cfs_trader import risk


class FakeB:
    def min_notional(self, s): return 5.0
    def round_qty(self, s, q): return round(q, 4)
    def round_price(self, s, p): return round(p, 6)


def cand(side, entry, stop):
    return Candidate(symbol="TUSDT", side=side, entry=entry, stop=stop, tp=None,
                     rr=5.0, score=8, atr_pct=2.0, status="FRESH", regime="TREND_DOWN",
                     bias="BOTH", tape_verdict="CONFIRM", tape_score=3.5)


def main():
    cfg = get_cfg()
    cfg.risk.update({"max_sl_pct": 6, "risk_per_trade_pct": 25.0, "leverage": 5,
                     "max_position_notional_usdt": 500, "min_notional_usdt": 5.0})
    cfg._d["exits"] = dict(cfg._d.get("exits", {})); cfg._d["exits"]["sl_min_pct"] = 2.0
    cfg._d["dynamic_sizing"] = {"enabled": False}
    cfg._d["dynamic_leverage"] = {"enabled": False}
    b = FakeB(); eq = 190.0; n_ok = n_fail = 0

    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)

    # 1) dar SL %1 (SHORT) → RED
    sz, why = risk.size_position(cfg, b, cand("SHORT", 100.0, 101.0), eq, 100.0)
    chk(f"dar SL %1 reddedildi ({why})", sz is None and "dar" in why)
    # 2) dar SL %1 (LONG) → RED
    sz, why = risk.size_position(cfg, b, cand("LONG", 100.0, 99.0), eq, 100.0)
    chk("dar SL %1 LONG reddedildi", sz is None and "dar" in why)
    # 3) geniş SL %8 → RED (tavan 6)
    sz, why = risk.size_position(cfg, b, cand("SHORT", 100.0, 108.0), eq, 100.0)
    chk(f"geniş SL %8 reddedildi ({why})", sz is None and "geniş" in why)
    # 4) tam sınır %2 → GEÇER
    sz, why = risk.size_position(cfg, b, cand("SHORT", 100.0, 102.0), eq, 100.0)
    chk(f"sınır SL %2 geçti (sl_dist={sz.sl_dist_pct if sz else '-'})", sz is not None and abs(sz.sl_dist_pct - 2.0) < 0.01)
    # 5) band içi %3 → GEÇER, risk hesabı tutuyor (notional×sl_dist)
    sz, why = risk.size_position(cfg, b, cand("SHORT", 100.0, 103.0), eq, 100.0)
    ok = sz is not None and abs(sz.sl_dist_pct - 3.0) < 0.01 and abs(sz.risk_usdt - sz.notional * 0.03) < 0.01
    chk(f"band içi %3 geçti, risk={sz.risk_usdt if sz else '-'} = notional×3%", ok)
    # 6) tam tavan %6 → GEÇER
    sz, why = risk.size_position(cfg, b, cand("SHORT", 100.0, 106.0), eq, 100.0)
    chk("tavan SL %6 geçti", sz is not None and abs(sz.sl_dist_pct - 6.0) < 0.01)
    # 7) sl_min_pct=0 → taban KAPALI, dar SL geçer (geri-uyum)
    cfg._d["exits"]["sl_min_pct"] = 0
    sz, why = risk.size_position(cfg, b, cand("SHORT", 100.0, 101.0), eq, 100.0)
    chk("sl_min_pct=0 → taban kapalı, %1 geçer", sz is not None)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
