"""booktp — Faz: emir-defteri tabanlı TP (v1, tek hedef).

Girişte derin order-book'u çekip kâr yönünde [min_r, max_r] penceresindeki EN BÜYÜK likidite
kümesini bulur, TP'yi onun hemen ÖNÜNE koyar (duvar emilmeden çık). Sabit 5R yerine gerçekçi,
genelde daha yakın TP → hızlı kâr + devir. FAIL-SAFE: hata/küme yok/disabled → None (çağıran 5R'ye düşer).
Yalnız TP'yi etkiler; SL/risk/sizing DEĞİŞMEZ. Bağımlılık YOK (urllib).
"""
import json
import urllib.parse
import urllib.request

FAPI = "https://fapi.binance.com"


def _depth(sym, limit=1000, timeout=8):
    url = f"{FAPI}/fapi/v1/depth?symbol={urllib.parse.quote(sym)}&limit={limit}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _clusters(levels, ref, up, bin_pct):
    """Kâr yönündeki seviyeleri bin_pct'lik kovalara topla → {dist_pct: notional}. SAF, test edilebilir.
    up=True → ref ÜSTÜ (ask/LONG direnci); up=False → ref ALTI (bid/SHORT desteği)."""
    buckets = {}
    for p, q in levels:
        if up and p <= ref:
            continue
        if not up and p >= ref:
            continue
        dist = abs(p / ref - 1) * 100.0
        b = round(round(dist / bin_pct) * bin_pct, 4)
        if b <= 0:
            continue
        buckets[b] = buckets.get(b, 0.0) + p * q
    return buckets


def pick_tp(buckets, ref, entry, risk, up, min_r, max_r, before):
    """Kümelerden TP fiyatı seç (SAF, test edilebilir). [min_r,max_r] penceresinde EN BÜYÜK küme → önü. None=uygun yok."""
    if not buckets or risk <= 0:
        return None
    def price_at(dp):
        return ref * (1 + dp / 100.0) if up else ref * (1 - dp / 100.0)
    def r_of(dp):
        return abs(price_at(dp) - entry) / risk
    cands = [(dp, usd) for dp, usd in buckets.items() if min_r <= r_of(dp) <= max_r]
    if not cands:
        return None
    best_dp = max(cands, key=lambda x: x[1])[0]   # en büyük likidite
    wall = price_at(best_dp)
    tp = wall * (1 - before) if up else wall * (1 + before)   # duvarın hemen önü
    return round(tp, 10)


def compute(cfg, symbol, side, entry, stop, mark=None):
    """Defter-TP fiyatı | None (fail-safe). entry/ref = gerçek giriş (mark). SL/risk için stop."""
    ex = (cfg.get("exits", {}) or {}).get("book_tp", {}) or {}
    if not ex.get("enabled", False):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    ref = float(mark) if mark else entry
    up = (side == "LONG")
    try:
        d = _depth(symbol, int(ex.get("depth", 1000)))
        levels = [(float(p), float(q)) for p, q in (d["asks"] if up else d["bids"])]
    except Exception:
        return None
    buckets = _clusters(levels, ref, up, float(ex.get("bin_pct", 0.5)))
    return pick_tp(buckets, ref, entry, risk, up,
                   float(ex.get("min_r", 1.5)), float(ex.get("max_r", 6.0)),
                   float(ex.get("before_pct", 0.1)) / 100.0)
