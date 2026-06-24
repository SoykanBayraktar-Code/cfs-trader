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

    # ---- KES+YOĞUNLAŞ: _regime_filter (RANGE'de FRESH ele) ----
    def mk(sym, status): return Candidate(symbol=sym, side="LONG", entry=1, stop=0.98, tp=1.1, rr=5,
                                          score=3, atr_pct=2, status=status, regime="RANGE", bias="LONG")
    cands = [mk("A", "FRESH"), mk("B", "PULLBACK-MOM"), mk("C", "FRESH")]
    class CfgRF:
        def __init__(self, skip): self.signals = {"skip_range_fresh": skip}
    out = signals._regime_filter("RANGE", cands, CfgRF(True))
    chk("RANGE + skip → FRESH elendi, diğeri kaldı", [c.symbol for c in out] == ["B"])
    out = signals._regime_filter("TREND_DOWN", cands, CfgRF(True))
    chk("TREND_DOWN + skip → hepsi kaldı (sadece RANGE'de eler)", len(out) == 3)
    out = signals._regime_filter("RANGE", cands, CfgRF(False))
    chk("RANGE + skip kapalı → hepsi kaldı", len(out) == 3)

    # ---- SLX post-mortem: ters-rejim filtresi ----
    def mks(sym, side, regime): return Candidate(symbol=sym, side=side, entry=1, stop=0.98, tp=1.1, rr=5,
                                                 score=3, atr_pct=2, status="PULLBACK-MOM", regime=regime, bias=side)
    class CfgCR:
        signals = {"skip_counter_regime": True, "skip_range_fresh": False}
        def get(self, k, d=None): return self.signals if k == "signals" else (d or {})
    longs_shorts = [mks("A", "LONG", "TREND_DOWN"), mks("B", "SHORT", "TREND_DOWN")]
    out = signals._regime_filter("TREND_DOWN", longs_shorts, CfgCR())
    chk("TREND_DOWN → LONG elendi, SHORT kaldı (ters-rejim)", [c.symbol for c in out] == ["B"])
    ls2 = [mks("A", "LONG", "TREND_UP"), mks("B", "SHORT", "TREND_UP")]
    out = signals._regime_filter("TREND_UP", ls2, CfgCR())
    chk("TREND_UP → SHORT elendi, LONG kaldı (ters-rejim)", [c.symbol for c in out] == ["A"])
    class CfgCRoff:
        signals = {"skip_counter_regime": False, "skip_range_fresh": False}
        def get(self, k, d=None): return self.signals if k == "signals" else (d or {})
    out = signals._regime_filter("TREND_DOWN", longs_shorts, CfgCRoff())
    chk("skip_counter_regime kapalı → hepsi kaldı", len(out) == 2)

    # ---- DİNAMİK BOYUT: _confidence + dynamic_risk_pct ----
    from cfs_trader import risk as R
    chk("confidence tape CONFIRM(3.0) tek → 0.75", R._confidence(3.0, None, 0, None) == 0.75)
    chk("confidence tape min-pass 2.2 → 0.35", R._confidence(2.2, None, 0, None) == 0.35)
    chk("confidence tape 1.0 → 0 (clip)", R._confidence(1.0, None, 0, None) == 0.0)
    chk("confidence learner n<3 atlanır", R._confidence(3.0, 0.5, 2, None) == 0.75)
    chk("confidence tape+learner+brain ortalama", abs(R._confidence(3.0, 0.5, 5, 0.8) - round((0.75+1.0+0.8)/3,3)) < 1e-9)
    chk("confidence brain düşük → ortalama düşer", R._confidence(3.0, None, 0, 0.2) == round((0.75+0.2)/2,3))

    class CfgDyn:
        def __init__(self, en): self._en=en
        def get(self, k, d=None): return {"enabled": self._en, "min_frac": 0.3} if k=="dynamic_sizing" else (d or {})
    chk("conf_mult conf=0 → min_frac 0.3", R.dynamic_conf_mult(CfgDyn(True), 0.0) == 0.3)
    chk("conf_mult conf=1 → tam 1.0", R.dynamic_conf_mult(CfgDyn(True), 1.0) == 1.0)
    chk("conf_mult conf=0.5 → 0.65", R.dynamic_conf_mult(CfgDyn(True), 0.5) == 0.65)
    chk("conf_mult ≤1.0 (tam-boyutu aşmaz)", R.dynamic_conf_mult(CfgDyn(True), 5.0) == 1.0)
    chk("conf_mult kapalı → 1.0", R.dynamic_conf_mult(CfgDyn(False), 0.9) == 1.0)

    # ---- DİNAMİK KALDIRAÇ: leverage_for (confidence + SL-güvenlik) ----
    class CfgLev:
        risk = {"leverage": 5}
        def __init__(self, en): self._en=en
        def get(self, k, d=None):
            return {"enabled": self._en, "base_leverage": 5, "max_leverage": 8,
                    "liq_safety_factor": 0.8, "maint_margin": 0.005} if k=="dynamic_leverage" else (d or {})
    C = CfgLev(True)
    chk("yüksek-conf + dar SL(%2) → 8x", R.leverage_for(C, 1.0, 0.02) == 8)
    chk("düşük-conf(%0) → 5x (base)", R.leverage_for(C, 0.0, 0.02) == 5)
    chk("yüksek-conf + GENİŞ SL(%12) → SL-güvenlik 6x'e kısar", R.leverage_for(C, 1.0, 0.12) == 6)
    chk("yüksek-conf + SL(%9) → 8x", R.leverage_for(C, 1.0, 0.09) == 8)
    chk("kapalı → sabit 5x", R.leverage_for(CfgLev(False), 1.0, 0.02) == 5)
    # GÜVENLİK invariantı: dönen kaldıraçta SL likidasyon-öncesi (sl_dist ≤ (1/lev-maint)*0.8)
    ok_inv = True
    for cf in (0.0, 0.5, 1.0):
        for sld in (0.02, 0.05, 0.09, 0.12):
            lev = R.leverage_for(C, cf, sld)
            if sld > (1.0/lev - 0.005) * 0.8 + 1e-9: ok_inv = False
    chk("GÜVENLİK: dönen kaldıraçta SL hep likidasyon-öncesi", ok_inv)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
