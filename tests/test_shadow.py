#!/usr/bin/env python3
"""shadow (Faz 0) testi — CVD pencereleri + flow_regime + konfluans + compute (ağsız, _get mock)."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import shadow


def kline(close, quote_vol, taker_buy_quote):
    # ham Binance dizisi: idx4=close, idx7=quoteVol, idx10=takerBuyQuote
    return [0, 0, 0, 0, close, 0, 0, quote_vol, 0, 0, taker_buy_quote, 0]


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)

    # ---- _cvd_windows: alıcı baskın (taker buy %70) → net +0.4 ----
    kl = [kline(100 + i, 100, 70) for i in range(14)]  # 14 bar, sonuncu forming
    cvd, closed = shadow._cvd_windows(kl)
    chk(f"cvd60 = +0.4 (alıcı baskın) (={cvd['cvd60']})", abs(cvd["cvd60"] - 0.4) < 1e-6)
    chk("forming bar atıldı (13 closed)", len(closed) == 13)
    # satıcı baskın
    kl2 = [kline(100, 100, 30) for _ in range(14)]
    cvd2, _ = shadow._cvd_windows(kl2)
    chk(f"cvd satıcı baskın = -0.4 (={cvd2['cvd60']})", abs(cvd2["cvd60"] + 0.4) < 1e-6)

    # ---- _flow_regime ----
    up = [kline(100 + i, 100, 70) for i in range(14)][:-1]   # fiyat↑ + cvd↑
    chk("fiyat↑ akış↑ → AKIS_UP", shadow._flow_regime(up, 0.4) == "AKIS_UP")
    dn = [kline(100 - i, 100, 30) for i in range(14)][:-1]   # fiyat↓ + cvd↓
    chk("fiyat↓ akış↓ → AKIS_DOWN", shadow._flow_regime(dn, -0.4) == "AKIS_DOWN")
    chk("fiyat↑ akış↓ → DIVERJANS_AYI", shadow._flow_regime(up, -0.4) == "DIVERJANS_AYI")
    chk("cvd None → NOTR", shadow._flow_regime(up, None) == "NOTR")

    # ---- _confluence ----
    cand = types.SimpleNamespace(regime="TREND_DOWN", tape_verdict="CONFIRM", liq_pull=-0.5, ls_bias=-0.5)
    # SHORT: rejim(TREND_DOWN+SHORT)✓ tape✓ cvd60(-0.4 short)✓ liq_pull(-0.5 short)✓ ls_bias(-0.5 short)✓ = 5
    chk("konfluans tam uyum = 5", shadow._confluence(cand, "SHORT", {"cvd60": -0.4}) == 5)
    # tam ters yön (tape da CONFIRM değil) → hiçbiri uymaz = 0
    candL = types.SimpleNamespace(regime="TREND_DOWN", tape_verdict="VETO", liq_pull=-0.5, ls_bias=-0.5)
    chk("konfluans tam ters yön = 0", shadow._confluence(candL, "LONG", {"cvd60": -0.4}) == 0)
    cand2 = types.SimpleNamespace(regime="RANGE", tape_verdict="CONFIRM", liq_pull=0.0, ls_bias=0.0)
    chk("konfluans kısmi (sadece tape) = 1", shadow._confluence(cand2, "SHORT", {"cvd60": 0.0}) == 1)

    # ---- compute (mock _get) ----
    def fake_get(path, params, timeout=8):
        if "klines" in path:
            return [kline(100 + i, 100, 70) for i in range(14)]
        if "premiumIndex" in path:
            return {"lastFundingRate": "0.00012", "markPrice": "100.5", "indexPrice": "100.0"}
        return {}
    orig = shadow._get
    shadow._get = fake_get
    try:
        c = types.SimpleNamespace(symbol="TESTUSDT", side="LONG", regime="TREND_UP",
                                  tape_verdict="CONFIRM", liq_pull=0.5, ls_bias=0.5)
        f = shadow.compute(None, c)
        chk(f"compute cvd60 (={f.get('cvd60')})", abs(f.get("cvd60", 0) - 0.4) < 1e-6)
        chk(f"compute flow_regime AKIS_UP (={f.get('flow_regime')})", f.get("flow_regime") == "AKIS_UP")
        chk(f"compute funding (={f.get('funding')})", f.get("funding") == 0.00012)
        chk(f"compute basis_bps +50 (={f.get('basis_bps')})", abs(f.get("basis_bps", 0) - 50.0) < 0.1)
        chk(f"compute konfluans=5 (={f.get('confluence')})", f.get("confluence") == 5)
    finally:
        shadow._get = orig

    # ---- FAIL-SAFE: _get patlasa {} / kısmi, exception YOK ----
    def boom(*a, **k):
        raise RuntimeError("ağ")
    shadow._get = boom
    try:
        f = shadow.compute(None, types.SimpleNamespace(symbol="X", side="LONG", regime="RANGE",
                                                       tape_verdict="?", liq_pull=0, ls_bias=0))
        chk("hata → exception YOK (confluence yine hesaplanır)", isinstance(f, dict) and "cvd60" not in f)
    finally:
        shadow._get = orig

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
