"""scalp — Faz 1 SHADOW: coiled-breakout / scalp setup bağlamları (yalnız ÖLÇÜM, karara DOKUNMAZ).

6 bileşen + bileşik scalp_score (0-6):
  SIKIŞMA  : squeeze_pct (BBW yüzdelik, düşük=sıkışık), atr_contraction (ATR şimdi/ort, <1=daralıyor)
  YÜKLENME : oi_trend (OI % değişim), cvd_divergence (birikim+1/dağıtım-1), book_asym (defter asimetrisi)
  ATEŞLEME : vol_surge (son bar hacmi/ort, >1.5=patlama)
Yüksek skor = yay gerilmiş + yükleniyor + ateşliyor → scalp adayı. Birkaç hafta sonra R/hız ile
korelasyonu ölçülür → ancak kanıtlanırsa karara bağlanır (Faz 2). FAIL-SAFE, bağımlılık YOK (urllib).
"""
import json
import urllib.parse
import urllib.request

FAPI = "https://fapi.binance.com"


def _get(path, params, timeout=8):
    url = f"{FAPI}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---- SAF yardımcılar (ağsız, test edilebilir) ----
def _sma(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _sma(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def bbw_series(closes, n=20):
    """Bollinger genişliği serisi: her n-pencere için 4*std/sma. SAF."""
    out = []
    for i in range(n, len(closes) + 1):
        w = closes[i - n:i]
        m = _sma(w)
        if m > 0:
            out.append(4 * _std(w) / m)
    return out


def squeeze_pct(closes, n=20):
    """Şu anki BBW'nin seri içindeki yüzdelik sırası (0-100). Düşük=sıkışık. SAF."""
    s = bbw_series(closes, n)
    if len(s) < 5:
        return None
    cur = s[-1]
    return round(sum(1 for x in s if x <= cur) / len(s) * 100, 1)


def atr_series(highs, lows, closes, n=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return [_sma(trs[i - n:i]) for i in range(n, len(trs) + 1)] if len(trs) >= n else []


def atr_contraction(highs, lows, closes, n=14):
    """Şimdiki ATR / ATR ortalaması (<1 = daralma). SAF."""
    a = atr_series(highs, lows, closes, n)
    if len(a) < 3 or _sma(a) <= 0:
        return None
    return round(a[-1] / _sma(a), 3)


def vol_surge(vols, n=20):
    """Son KAPANMIŞ bar hacmi / önceki n-ortalama (>1.5 = patlama). SAF."""
    if len(vols) < n + 1:
        return None
    avg = _sma(vols[-n - 1:-1])
    return round(vols[-1] / avg, 2) if avg > 0 else None


def range_position(highs, lows, closes, n=20):
    """Fiyatın son n bar aralığındaki yeri (0=dip, 1=tepe). Kırılma kenarına dayanma sinyali. SAF.
    LONG ön-kırılma: tepeye yakın (→1); SHORT: dibe yakın (→0)."""
    if len(closes) < n:
        return None
    hi = max(highs[-n:])
    lo = min(lows[-n:])
    if hi <= lo:
        return None
    return round((closes[-1] - lo) / (hi - lo), 3)


def oi_trend(ois):
    """OI % değişimi (ilk→son). Pozitif = yüklenme. SAF."""
    if len(ois) < 2 or ois[0] <= 0:
        return None
    return round((ois[-1] / ois[0] - 1) * 100, 2)


def cvd_divergence(closes, cvd60, flat_thr=0.005):
    """Fiyat ~yatay + CVD güçlü → birikim(+1)/dağıtım(-1)/yok(0). SAF."""
    if cvd60 is None or len(closes) < 13:
        return 0
    if abs(closes[-1] / closes[-13] - 1) > flat_thr:   # fiyat trendde → divergence değil
        return 0
    return 1 if cvd60 > 0.05 else (-1 if cvd60 < -0.05 else 0)


def book_asym(bids, asks, mid, pct=1.0):
    """±pct% içinde (bid_not - ask_not)/toplam ∈ [-1,1]. >0 bid-ağır (yukarı eğilim). SAF."""
    lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
    bb = sum(p * q for p, q in bids if p >= lo)
    ba = sum(p * q for p, q in asks if p <= hi)
    tot = bb + ba
    return round((bb - ba) / tot, 3) if tot > 0 else None


def score(side, sq, atrc, oit, cvddiv, basym, vsurge):
    """Yön-uyumlu scalp setup skoru 0-6. SAF."""
    s = 0
    if sq is not None and sq < 25:
        s += 1
    if atrc is not None and atrc < 0.9:
        s += 1
    if oit is not None and oit > 1.0:
        s += 1
    if cvddiv and ((cvddiv > 0 and side == "LONG") or (cvddiv < 0 and side == "SHORT")):
        s += 1
    if basym is not None and ((basym > 0.1 and side == "LONG") or (basym < -0.1 and side == "SHORT")):
        s += 1
    if vsurge is not None and vsurge > 1.5:
        s += 1
    return s


def compute(cfg, cand, base=None):
    """cand için scalp shadow özellikleri (dict). FAIL-SAFE. ~3 REST/işlem (yalnız girişlerde)."""
    base = base or {}
    sym = cand.symbol
    side = (cand.side or "").upper()
    feats = {}
    # 15m klines → squeeze, atr-daralma, hacim-patlama, cvd-diverjans
    try:
        kl = _get("/fapi/v1/klines", {"symbol": sym, "interval": "15m", "limit": 60})
        if isinstance(kl, list) and len(kl) >= 25:
            closed = kl[:-1]
            closes = [float(k[4]) for k in closed]
            highs = [float(k[2]) for k in closed]
            lows = [float(k[3]) for k in closed]
            vols = [float(k[5]) for k in closed]
            feats["squeeze_pct"] = squeeze_pct(closes)
            feats["atr_contraction"] = atr_contraction(highs, lows, closes)
            feats["vol_surge"] = vol_surge(vols)
            feats["range_pos"] = range_position(highs, lows, closes)
            feats["cvd_divergence"] = cvd_divergence(closes, base.get("cvd60"))
    except Exception:
        pass
    # OI geçmişi (15m) → yüklenme
    try:
        oih = _get("/futures/data/openInterestHist", {"symbol": sym, "period": "15m", "limit": 8})
        if isinstance(oih, list) and len(oih) >= 2:
            feats["oi_trend"] = oi_trend([float(x["sumOpenInterest"]) for x in oih])
    except Exception:
        pass
    # defter asimetrisi
    try:
        d = _get("/fapi/v1/depth", {"symbol": sym, "limit": 500})
        bids = [(float(p), float(q)) for p, q in d["bids"]]
        asks = [(float(p), float(q)) for p, q in d["asks"]]
        if bids and asks:
            feats["book_asym"] = book_asym(bids, asks, (bids[0][0] + asks[0][0]) / 2)
    except Exception:
        pass
    # bileşik skor
    try:
        feats["scalp_score"] = score(side, feats.get("squeeze_pct"), feats.get("atr_contraction"),
                                     feats.get("oi_trend"), feats.get("cvd_divergence"),
                                     feats.get("book_asym"), feats.get("vol_surge"))
    except Exception:
        pass
    return feats
