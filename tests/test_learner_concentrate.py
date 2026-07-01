#!/usr/bin/env python3
"""07-01 YOĞUNLAŞMA — learner kanıtlı-pozitif cell'lere yoğunlaşma (min_samples=6, suppress<-0.08) birim testi.

Doğrular: learner kanıtlı-NEGATİF cell'i (n≥6, exp<-0.08) BASTIRIR; kanıtlı-POZİTİF ve KANITSIZ (n<6) cell'i GEÇİRİR;
enabled=false iken hiç bastırmaz. Canlı-para davranışını (yoğunlaşma) sabitler.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader.learner import Learner


class FakeCfg:
    def __init__(self, d): self._d = d
    def get(self, k, default=None): return self._d.get(k, default)


class FakeStore:
    # [INFO] (regime,side,status) -> (expectancy, n). Learner.suppressed bunu okur; DB gerekmez.
    def __init__(self, data): self.data = data
    def expectancy(self, regime, side, status): return self.data.get((regime, side, status), (None, 0))


class Cand:
    def __init__(self, regime, side, status):
        self.regime, self.side, self.status = regime, side, status


CFG = FakeCfg({"learner": {"enabled": True, "min_samples": 6, "suppress_below_expectancy": -0.08}})
# gerçek canlı verisine benzer cell'ler
STORE = FakeStore({
    ("TREND_DOWN", "SHORT", "FRESH"): (0.109, 25),          # kanıtlı-POZİTİF (edge)
    ("RANGE", "SHORT", "FRESH"): (-0.162, 7),               # kanıtlı-NEGATİF (n≥6)
    ("TREND_DOWN", "LONG", "PULLBACK-MOM"): (-0.108, 11),   # kanıtlı-NEGATİF
    ("RANGE", "LONG", "PULLBACK-MOM"): (0.007, 17),         # ~başabaş POZİTİF (kesilmez)
    ("RANGE", "LONG", "FRESH"): (-0.536, 5),                # NEGATİF ama n<6 → kanıtsız (kesilmez)
    ("TREND_UP", "LONG", "FRESH"): (0.500, 3),              # POZİTİF ama n<6 → kanıtsız (kesilmez, keşif)
})


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += cond; n_fail += (not cond)

    L = Learner(CFG, STORE)
    chk(f"min_samples=6 yüklendi (={L.min_samples})", L.min_samples == 6)
    chk(f"eşik=-0.08 yüklendi (={L.threshold})", abs(L.threshold + 0.08) < 1e-9)

    # kanıtlı-NEGATİF → BASTIR
    s, why = L.suppressed(Cand("RANGE", "SHORT", "FRESH"))
    chk(f"RANGE|SHORT|FRESH (exp-0.162 n7) BASTIRILDI (={why[:40]})", s)
    s, _ = L.suppressed(Cand("TREND_DOWN", "LONG", "PULLBACK-MOM"))
    chk("TREND_DOWN|LONG|PULLBACK-MOM (exp-0.108 n11) BASTIRILDI", s)

    # kanıtlı-POZİTİF → GEÇİR
    s, _ = L.suppressed(Cand("TREND_DOWN", "SHORT", "FRESH"))
    chk("TREND_DOWN|SHORT|FRESH (edge +0.109 n25) GEÇTİ", not s)
    s, _ = L.suppressed(Cand("RANGE", "LONG", "PULLBACK-MOM"))
    chk("RANGE|LONG|PULLBACK-MOM (+0.007 başabaş) GEÇTİ", not s)

    # KANITSIZ (n<6) → GEÇİR (keşif; negatif bile olsa yeterli veri yok)
    s, _ = L.suppressed(Cand("RANGE", "LONG", "FRESH"))
    chk("RANGE|LONG|FRESH (exp-0.536 ama n=5<6) KANITSIZ→GEÇTİ", not s)
    s, _ = L.suppressed(Cand("TREND_UP", "LONG", "FRESH"))
    chk("TREND_UP|LONG|FRESH (poz ama n=3<6) KANITSIZ→GEÇTİ", not s)

    # bilinmeyen cell → GEÇİR
    s, _ = L.suppressed(Cand("TREND_UP", "SHORT", "FRESH"))
    chk("bilinmeyen cell (veri yok) GEÇTİ", not s)

    # enabled=false → asla bastırma
    L2 = Learner(FakeCfg({"learner": {"enabled": False, "min_samples": 6, "suppress_below_expectancy": -0.08}}), STORE)
    s, _ = L2.suppressed(Cand("RANGE", "SHORT", "FRESH"))
    chk("enabled=false → negatif cell bile GEÇTİ", not s)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
