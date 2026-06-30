"""shadow — Faz 0: karar yolunu DEĞİŞTİRMEYEN ek özellikler (yalnız ölçüm için loglanır).

Ücretsiz veri: kline-türevli CVD (15/30/60 dk net-taker), funding, spot-perp basis, flow_regime,
konfluans (yön ile uyuşan bağımsız eksen sayısı). Hepsi trade satırına + Notion'a yazılır; birkaç
hafta sonra hangisinin R ile korelasyonu var ölçülür → ancak edge kanıtlanırsa karara bağlanır.

FAIL-SAFE: her hata yutulur, {} döner — sizing/gate/giriş ASLA etkilenmez. Bağımlılık YOK (urllib).
"""
import json
import urllib.request
import urllib.parse

FAPI = "https://fapi.binance.com"

FLOW_REGIMES = ("AKIS_UP", "AKIS_DOWN", "DIVERJANS_AYI", "DIVERJANS_BOGA", "NOTR")


def _get(path, params, timeout=8):
    url = f"{FAPI}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _cvd_windows(klines):
    """Kapanmış 5m barlardan net-taker fraksiyonu (-1..+1): (2*takerBuyQuote - quoteVol)/quoteVol.
    klines = ham Binance dizisi (idx7=quoteVol, idx10=takerBuyQuote). Son (forming) bar atılır.
    SAF, test edilebilir. Döner: ({cvd15,cvd30,cvd60}, closed_list)."""
    closed = klines[:-1] if klines else []

    def frac(n):
        ks = closed[-n:]
        if len(ks) < n:
            return None
        qv = sum(float(k[7]) for k in ks)
        tb = sum(float(k[10]) for k in ks)
        return round((2 * tb - qv) / qv, 4) if qv > 0 else None

    return {"cvd15": frac(3), "cvd30": frac(6), "cvd60": frac(12)}, closed


def _flow_regime(closed, cvd60):
    """Fiyat yönü (son 12x5m) × CVD yönü → akış rejimi. SAF, test edilebilir."""
    if len(closed) < 13 or cvd60 is None:
        return "NOTR"
    c0 = float(closed[-1][4]); cn = float(closed[-12][4])
    pdir = 1 if c0 > cn else (-1 if c0 < cn else 0)
    cvds = 1 if cvd60 > 0.05 else (-1 if cvd60 < -0.05 else 0)
    if pdir > 0 and cvds > 0:
        return "AKIS_UP"
    if pdir < 0 and cvds < 0:
        return "AKIS_DOWN"
    if pdir > 0 and cvds < 0:
        return "DIVERJANS_AYI"   # fiyat↑ akış↓ = tuzak (long'a dikkat)
    if pdir < 0 and cvds > 0:
        return "DIVERJANS_BOGA"  # fiyat↓ akış↑ = tuzak (short'a dikkat)
    return "NOTR"


def _confluence(cand, side, feats):
    """Yön ile uyuşan BAĞIMSIZ eksen sayısı (0-5). SAF, test edilebilir. Eksenler:
    rejim, tape, cvd60, liq_pull(mıknatıs), ls_bias(kalabalık-kontrarian). SHADOW — karara girmez."""
    n = 0
    reg = cand.regime or ""
    if (reg == "TREND_DOWN" and side == "SHORT") or (reg == "TREND_UP" and side == "LONG"):
        n += 1
    if getattr(cand, "tape_verdict", "") == "CONFIRM":
        n += 1
    cvd = feats.get("cvd60")
    if cvd is not None and ((cvd > 0.05 and side == "LONG") or (cvd < -0.05 and side == "SHORT")):
        n += 1
    lp = getattr(cand, "liq_pull", 0.0) or 0.0
    if (lp > 0.1 and side == "LONG") or (lp < -0.1 and side == "SHORT"):
        n += 1
    lb = getattr(cand, "ls_bias", 0.0) or 0.0
    if (lb > 0.1 and side == "LONG") or (lb < -0.1 and side == "SHORT"):
        n += 1
    return n


def compute(cfg, cand):
    """cand için shadow özellikleri (dict). FAIL-SAFE: hata → kısmi/boş dict. ~2 ucuz REST/işlem."""
    feats = {}
    sym = cand.symbol
    side = (cand.side or "").upper()
    # 1) kline-CVD (5m, son ~60 dk) — ücretsiz, taker-buy hacmi klines'te zaten var
    try:
        kl = _get("/fapi/v1/klines", {"symbol": sym, "interval": "5m", "limit": 14})
        if isinstance(kl, list) and len(kl) >= 13:
            cvd, closed = _cvd_windows(kl)
            feats.update(cvd)
            feats["flow_regime"] = _flow_regime(closed, cvd.get("cvd60"))
    except Exception:
        pass
    # 2) funding + spot-perp basis (premiumIndex) — ücretsiz
    try:
        pi = _get("/fapi/v1/premiumIndex", {"symbol": sym})
        feats["funding"] = round(float(pi.get("lastFundingRate")), 6)
        mk = float(pi.get("markPrice")); ix = float(pi.get("indexPrice"))
        feats["basis_bps"] = round((mk - ix) / ix * 10000, 2) if ix else None
    except Exception:
        pass
    # 3) konfluans (0-5) — SHADOW
    try:
        feats["confluence"] = _confluence(cand, side, feats)
    except Exception:
        pass
    # 4) Faz 1 SHADOW: scalp/coiled-breakout bağlamları (karara DOKUNMAZ; cvd60'ı paylaşır)
    try:
        from . import scalp
        feats.update(scalp.compute(cfg, cand, feats))
    except Exception:
        pass
    return feats
