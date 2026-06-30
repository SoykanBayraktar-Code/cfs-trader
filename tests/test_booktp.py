#!/usr/bin/env python3
"""booktp (defter-TP v1) testi — küme toplama + seçim (min/max-R, en-büyük, önü) + compute (mock depth)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import booktp


class Cfg:
    def __init__(self, bt):
        self.bt = bt
    def get(self, k, d=None):
        return {"book_tp": self.bt} if k == "exits" else d


def asks(*pairs):
    return [[str(p), str(n / p)] for p, n in pairs]   # (fiyat, notional) → [price, qty]


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)

    # entry=100, stop=98 → risk=2 (1R = +2%). ref=100, LONG (ask üstü).
    lv = [(float(p), float(q)) for p, q in asks((102, 500000), (105, 300000), (110, 200000), (130, 999000))]
    bk = booktp._clusters(lv, 100.0, True, 0.5)
    chk("kümeler: +2/+5/+10/+30% kovaları", set(round(k) for k in bk) == {2, 5, 10, 30})

    # pick: [1.5R,6R] → +2%(1R,500k) ELENİR (min_r), +30%(15R) ELENİR (max_r); kalan en büyük +5%(2.5R,300k)
    tp = booktp.pick_tp(bk, 100.0, 100.0, 2.0, True, 1.5, 6.0, 0.001)
    chk(f"en büyük in-window +5% seçildi, önü ≈104.895 (={tp})", abs(tp - 104.895) < 0.01)
    chk("RR ≈2.45", abs((tp - 100) / 2.0 - 2.447) < 0.01)

    # min_r floor: büyük ama yakın (+2%,1R) küme alınmaz
    bk2 = booktp._clusters([(float(p), float(q)) for p, q in asks((102, 900000))], 100.0, True, 0.5)
    chk("yalnız +2%(1R) → pencerede yok → None", booktp.pick_tp(bk2, 100.0, 100.0, 2.0, True, 1.5, 6.0, 0.001) is None)

    # compute: mock depth, LONG
    orig = booktp._depth
    booktp._depth = lambda s, limit=1000, timeout=8: {"asks": asks((105, 300000), (110, 200000)), "bids": []}
    try:
        cfg = Cfg({"enabled": True, "min_r": 1.5, "max_r": 6.0, "bin_pct": 0.5, "before_pct": 0.1, "depth": 1000})
        tp = booktp.compute(cfg, "X", "LONG", 100.0, 98.0, 100.0)
        chk(f"compute LONG defter-TP ≈104.895 (={tp})", tp and abs(tp - 104.895) < 0.02)
        # disabled → None
        chk("disabled → None", booktp.compute(Cfg({"enabled": False}), "X", "LONG", 100.0, 98.0, 100.0) is None)
        # SHORT: bid desteği altta
        booktp._depth = lambda s, limit=1000, timeout=8: {"asks": [], "bids": asks((95, 300000), (90, 200000))}
        tps = booktp.compute(cfg, "X", "SHORT", 100.0, 102.0, 100.0)
        chk(f"compute SHORT defter-TP ≈95.095 (={tps})", tps and abs(tps - 95.095) < 0.02)
    finally:
        booktp._depth = orig

    # FAIL-SAFE: depth patlasa None
    booktp._depth = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net"))
    try:
        chk("depth hata → None (exception YOK)",
            booktp.compute(Cfg({"enabled": True}), "X", "LONG", 100.0, 98.0, 100.0) is None)
    finally:
        booktp._depth = orig

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
