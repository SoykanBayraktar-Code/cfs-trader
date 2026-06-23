#!/usr/bin/env python3
"""Bağlam (liq_pull) sizing-tilt testi (saf mantık + sizing uygulaması, ağ/engine YOK)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import signals
from cfs_trader.signals import _tilt_from_pull, Candidate


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)

    S = 0.4
    # uyuşma → tam boyut (1.0), cap aşılmaz
    chk("LONG + yukarı-mıknatıs (lp+) uyuşma → tilt 1.0", _tilt_from_pull(0.8, "LONG", S) == 1.0)
    chk("SHORT + aşağı-mıknatıs (lp−) uyuşma → tilt 1.0", _tilt_from_pull(-0.8, "SHORT", S) == 1.0)
    # çelişme → küçültür
    chk("LONG + aşağı-mıknatıs (lp−) çelişme → en fazla 1-strength", abs(_tilt_from_pull(-1.0, "LONG", S) - (1 - S)) < 1e-9)
    chk("SHORT + yukarı-mıknatıs (lp+) çelişme → en fazla 1-strength", abs(_tilt_from_pull(1.0, "SHORT", S) - (1 - S)) < 1e-9)
    # kısmi çelişme orantılı
    chk("kısmi çelişme (-0.5) orantılı küçültür", abs(_tilt_from_pull(-0.5, "LONG", S) - (1 - S * 0.5)) < 1e-9)
    # net görüş yok → dokunma
    chk("|lp|<min_abs → tilt 1.0 (dokunma)", _tilt_from_pull(0.02, "LONG", S, 0.05) == 1.0)
    chk("lp None → tilt 1.0", _tilt_from_pull(None, "LONG", S) == 1.0)
    # sınırlar
    chk("tilt hep [1-strength, 1.0] aralığında", all(
        (1 - S) - 1e-9 <= _tilt_from_pull(v, sd, S) <= 1.0 + 1e-9
        for v in (-1, -0.5, 0.0, 0.5, 1.0) for sd in ("LONG", "SHORT")))

    # context_tilt: disabled config → (1.0, None) (engine çağrısı YOK)
    class Cfg:
        def get(self, k, d=None): return {"enabled": False} if k == "context" else (d or {})
    cand = Candidate(symbol="X", side="LONG", entry=10, stop=9, tp=15, rr=5, score=3,
                     atr_pct=2, status="FRESH", regime="TREND_UP", bias="LONG")
    t, lp = signals.context_tilt(Cfg(), cand)
    chk("context kapalı → (1.0, None), engine çağrısı yok", t == 1.0 and lp is None)

    # sizing tilt'i NOTIONAL'a uygular (çarpan etkisi)
    from cfs_trader import risk
    class Binance:
        def min_notional(self, s): return 5.0
        def round_qty(self, s, q): return round(q, 3)
    class Cfg2:
        risk = {"leverage": 5, "risk_per_trade_pct": 50, "max_position_notional_usdt": 500,
                "min_notional_usdt": 5, "max_sl_pct": 12}
        def get(self, k, d=None): return self.risk if k == "risk" else (d or {})
        def __getitem__(self, k): return self.risk
    cfg2 = Cfg2()
    base = Candidate(symbol="X", side="LONG", entry=100, stop=98, tp=110, rr=5, score=3,
                     atr_pct=2, status="FRESH", regime="TREND_UP", bias="LONG")
    tilted = Candidate(symbol="X", side="LONG", entry=100, stop=98, tp=110, rr=5, score=3,
                       atr_pct=2, status="FRESH", regime="TREND_UP", bias="LONG", context_tilt=0.6)
    sb, _ = risk.size_position(cfg2, Binance(), base, equity=65, mark=100)
    st, _ = risk.size_position(cfg2, Binance(), tilted, equity=65, mark=100)
    chk("tilt=0.6 → notional ~0.6× (küçültür)", sb and st and abs(st.notional - sb.notional * 0.6) < sb.notional * 0.05)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
