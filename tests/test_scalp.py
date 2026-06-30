#!/usr/bin/env python3
"""scalp (Faz 1 SHADOW) testi — saf bileşenler + skor + compute (mock _get). Ağsız."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import scalp


class C:
    def __init__(self, symbol, side):
        self.symbol = symbol
        self.side = side


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += bool(cond); n_fail += (not cond)

    # daralan oynaklık: erken büyük salınım, geç küçük → BBW/ATR sonda en düşük
    tight = [100 + ((60 - i) / 60.0) * 10 * (1 if i % 2 else -1) for i in range(60)]
    # genişleyen oynaklık (ters)
    wide = [100 + ((i + 1) / 60.0) * 10 * (1 if i % 2 else -1) for i in range(60)]

    sp = scalp.squeeze_pct(tight)
    chk(f"squeeze_pct daralan→düşük ({sp})", sp is not None and sp < 40)
    chk(f"squeeze_pct genişleyen→yüksek ({scalp.squeeze_pct(wide)})", scalp.squeeze_pct(wide) > 60)

    hi = [c + 1 for c in tight]; lo = [c - 1 for c in tight]
    atrc = scalp.atr_contraction(hi, lo, tight)
    chk(f"atr_contraction daralan <1 ({atrc})", atrc is not None and atrc < 1.0)

    chk(f"vol_surge son bar patlama ≈3 ({scalp.vol_surge([100]*20 + [300])})",
        abs(scalp.vol_surge([100] * 20 + [300]) - 3.0) < 0.01)
    chk("vol_surge yetersiz veri → None", scalp.vol_surge([100, 200]) is None)

    chk(f"oi_trend yükseliş +8% ({scalp.oi_trend([100,102,104,108])})",
        abs(scalp.oi_trend([100, 102, 104, 108]) - 8.0) < 0.01)

    asc = list(range(80, 100))   # 20 bar, fiyat tepeye dayanmış
    chk("range_position tepe → ~1", scalp.range_position([c + 0.5 for c in asc], [c - 0.5 for c in asc], asc) > 0.9)
    desc = list(range(100, 80, -1))   # fiyat dibe
    chk("range_position dip → ~0", scalp.range_position([c + 0.5 for c in desc], [c - 0.5 for c in desc], desc) < 0.1)

    flat = [100.0 + 0.01 * (i % 2) for i in range(14)]
    trend = [100.0 + i for i in range(14)]
    chk("cvd_divergence yatay+cvd>0 → +1 (birikim)", scalp.cvd_divergence(flat, 0.1) == 1)
    chk("cvd_divergence yatay+cvd<0 → -1 (dağıtım)", scalp.cvd_divergence(flat, -0.1) == -1)
    chk("cvd_divergence trend → 0 (diverjans değil)", scalp.cvd_divergence(trend, 0.1) == 0)

    ba = scalp.book_asym([(99, 1000)], [(101, 100)], 100.0)
    chk(f"book_asym bid-ağır → + ({ba})", ba is not None and ba > 0.5)

    chk("score LONG hepsi uyumlu → 6", scalp.score("LONG", 10, 0.8, 5, 1, 0.5, 2.0) == 6)
    chk("score SHORT cvd/book ters → 4", scalp.score("SHORT", 10, 0.8, 5, 1, 0.5, 2.0) == 4)
    chk("score hiçbiri → 0", scalp.score("LONG", 80, 1.2, 0.0, 0, 0.0, 1.0) == 0)

    # compute: mock _get (path'e göre)
    def fake_get(path, params, timeout=8):
        if "klines" in path:
            return [[0, 0, c + 1, c - 1, c, (300 if i == 58 else 100)] for i, c in enumerate(tight)]
        if "openInterestHist" in path:
            return [{"sumOpenInterest": str(100 + i * 2)} for i in range(8)]
        if "depth" in path:
            return {"bids": [["99", "1000"]], "asks": [["101", "100"]]}
        return {}
    orig = scalp._get
    scalp._get = fake_get
    try:
        f = scalp.compute({}, C("X", "LONG"), {"cvd60": 0.1})
        chk(f"compute alanları dolu ({sorted(f)})",
            all(k in f for k in ("squeeze_pct", "atr_contraction", "vol_surge", "range_pos", "cvd_divergence", "oi_trend", "book_asym", "scalp_score")))
        chk(f"compute scalp_score yüksek LONG ({f.get('scalp_score')})", f.get("scalp_score", 0) >= 4)
    finally:
        scalp._get = orig

    # FAIL-SAFE: _get patlasa → boş/kısmi, exception YOK
    scalp._get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net"))
    try:
        chk("compute fail-safe (hata→exception yok)", isinstance(scalp.compute({}, C("X", "LONG"), {}), dict))
    finally:
        scalp._get = orig

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
