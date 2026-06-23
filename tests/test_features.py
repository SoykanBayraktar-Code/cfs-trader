#!/usr/bin/env python3
"""features kalibrasyon + gürültü-azaltma testi (saf fonksiyonlar, DB/ağ YOK)."""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import features as F


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)

    # ---- kalibrasyon sınırları ve yönü ----
    chk("cal_fng 50→0 nötr", abs(F.cal_fng(50)) < 1e-9)
    chk("cal_fng 0→-1, 100→+1 (sınır)", F.cal_fng(0) == -1.0 and F.cal_fng(100) == 1.0)
    chk("cal_fng 23→~-0.54 (Extreme Fear)", abs(F.cal_fng(23) - (-0.54)) < 0.01)
    chk("cal_stablecoin 0→0", abs(F.cal_stablecoin(0)) < 1e-9)
    chk("cal_stablecoin + risk-on / − risk-off", F.cal_stablecoin(1.0) > 0 and F.cal_stablecoin(-1.0) < 0)
    chk("cal_etf işaret", F.cal_etf(500) > 0.7 and F.cal_etf(-500) < -0.7)
    chk("cal_cexflow + birikim(bullish) / − dağıtım", F.cal_cexflow(2e6) > 0 and F.cal_cexflow(-2e6) < 0)
    chk("tüm kalibrasyon -1..+1 sınırında", all(-1.0 <= f <= 1.0 for f in
        [F.cal_fng(999), F.cal_stablecoin(99), F.cal_etf(-99999), F.cal_cexflow(9e9)]))

    # ---- liq_pull yön + gürültü-azaltma ----
    above = [{"price": 1, "side": "SHORT_LIQ", "intensity": 90, "dist_pct": 0.5},
             {"price": 2, "side": "SHORT_LIQ", "intensity": 80, "dist_pct": 1.0}]
    below = [{"price": 1, "side": "LONG_LIQ", "intensity": 90, "dist_pct": -0.5},
             {"price": 2, "side": "LONG_LIQ", "intensity": 80, "dist_pct": -1.0}]
    pu, _ = F.liq_pull(above); pd, _ = F.liq_pull(below)
    chk("liq_pull üstte güçlü küme → + (yukarı çekim)", pu > 0.2)
    chk("liq_pull altta güçlü küme → − (aşağı çekim)", pd < -0.2)
    # gürültü: düşük intensity elenir
    noise = [{"price": 1, "side": "SHORT_LIQ", "intensity": 10, "dist_pct": 0.5}]
    pn, nn = F.liq_pull(noise, min_intensity=30)
    chk("liq_pull düşük-intensity elendi → 0/None", pn == 0.0 and nn is None)
    chk("liq_pull boş → 0/None", F.liq_pull([]) == (0.0, None))
    # nearest = en yakın küme
    mixed = [{"intensity": 90, "dist_pct": 3.0, "side": "SHORT_LIQ"},
             {"intensity": 85, "dist_pct": -0.3, "side": "LONG_LIQ"}]
    _, near = F.liq_pull(mixed)
    chk("liq_pull nearest = en yakın (dist -0.3)", near and abs(near["dist_pct"] - (-0.3)) < 1e-9)

    # ---- EMA gürültü-azaltma ----
    chk("ema tek değer → passthrough", F._ema([0.5]) == 0.5)
    chk("ema seri yumuşatır (0,1,0 arası)", 0 < F._ema([0.0, 1.0, 0.0]) < 1.0)
    chk("ema None'ları atlar", F._ema([None, 0.4, None]) == 0.4)
    chk("ema hepsi None → None", F._ema([None, None]) is None)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
