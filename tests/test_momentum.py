#!/usr/bin/env python3
"""Momentum/oi_surge saf-mantık testi (ağsız): seviye hesabı, yön çıkarımı, sınıf filtresi."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import signals


def main():
    n_ok = n_fail = 0

    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += bool(cond); n_fail += (not cond)

    # ---- _mom_levels ----
    # LONG: px=100, atr=4% → atr=4. stop=100-1.5*4=94. risk=6. tp=100+2.5*6=115. rr=2.5
    lv = signals._mom_levels(100, 4.0, "LONG", 1.5, 2.5)
    chk(f"LONG seviye (entry,stop,tp,rr)={lv}", lv and abs(lv[0]-100)<1e-9 and abs(lv[1]-94)<1e-9 and abs(lv[2]-115)<1e-9 and lv[3]==2.5)
    # SHORT: px=100, atr=4% → stop=106, risk=6, tp=100-2.5*6=85
    lv = signals._mom_levels(100, 4.0, "SHORT", 1.5, 2.5)
    chk(f"SHORT seviye={lv}", lv and abs(lv[1]-106)<1e-9 and abs(lv[2]-85)<1e-9)
    # atr=0 → None
    chk("atr=0 → None", signals._mom_levels(100, 0, "LONG", 1.5, 2.5) is None)
    # stop pozitif kalmalı: aşırı geniş sl_mult negatif stop → None
    chk("negatif stop → None", signals._mom_levels(10, 80.0, "LONG", 2.0, 2.5) is None)

    # ---- _oi_direction ----
    chk("taker 1.3 fund 0.01 → LONG", signals._oi_direction(1.3, 0.01) == "LONG")
    chk("taker 1.3 fund 0.08 → None (funding yüksek)", signals._oi_direction(1.3, 0.08) is None)
    chk("taker 0.8 → SHORT", signals._oi_direction(0.8, 0.0) == "SHORT")
    chk("taker 1.0 → None (belirsiz)", signals._oi_direction(1.0, 0.0) is None)
    chk("taker None → None", signals._oi_direction(None, None) is None)

    # ---- _class_ok ----
    chk("🟢SESSIZ-BIRIKIM ∈ [SESSIZ,ERKEN]", signals._class_ok("🟢SESSIZ-BIRIKIM", ["SESSIZ", "ERKEN"]))
    chk("🔴GEC/ISINMIS ∉ [SESSIZ,ERKEN]", not signals._class_ok("🔴GEC/ISINMIS", ["SESSIZ", "ERKEN"]))
    chk("🟢 ATESLENIYOR ∈ [ATESLENIYOR,KOSUYOR]", signals._class_ok("🟢 ATESLENIYOR", ["ATESLENIYOR", "KOSUYOR"]))
    chk("🔴 TUKENMIS/tuzak ∉ izinli", not signals._class_ok("🔴 TUKENMIS/tuzak", ["ATESLENIYOR", "KOSUYOR"]))
    chk("None etiket → False", not signals._class_ok(None, ["SESSIZ"]))

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
