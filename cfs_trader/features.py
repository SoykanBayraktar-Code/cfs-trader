"""features — datapool ham verisini GÜRÜLTÜSÜ AZALTILMIŞ + KALİBRE EDİLMİŞ özelliklere dönüştürür.

market_data.db (ham snapshots) → temiz, karşılaştırılabilir, sınırlı (-1..+1) özellik vektörü → `features` tablosu.
Trading DB'sine DOKUNMAZ, karar yoluna BAĞLI DEĞİL (bu aşama: optimize/kalibre; entegrasyon AYRI onayla).

GÜRÜLTÜ-AZALTMA: düşük-yoğunluk likidasyon kümelerini ele, mesafe-sönümlü ağırlık, top-N,
  birden çok snapshot varsa EMA-yumuşatma, kapsam/staleness bayrakları.
KALİBRASYON: her ham metrik farklı ölçekte → domain-çapalı bounded -1..+1 (history biriktikçe
  bu çapalar ampirik percentile ile DEĞİŞTİRİLEBİLİR — şimdilik sabit prior, raporda işaretli).
"""
import os
import math
import json
import time
import sqlite3

from .cfg import _ROOT

_DB = os.path.join(_ROOT, "data", "market_data.db")

_FEAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    symbol TEXT,                -- NULL = global makro
    data   TEXT NOT NULL        -- JSON kalibre özellik dict
);
CREATE INDEX IF NOT EXISTS ix_feat ON features(symbol, ts);
"""


def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# ═══════════ KALİBRASYON (domain-çapalı, bounded -1..+1) ═══════════
# NOT: çapalar (0.5, 500, 1e6, decay) sabit PRIOR'dur; yeterli history biriktiğinde
#      ampirik percentile/z-score ile değiştirilmeli (calibration_readiness() bunu izler).
def cal_fng(v):
    """Fear&Greed 0-100 → -1..+1. 50=nötr, + = greed/risk-on."""
    return _clip((float(v) - 50.0) / 50.0)


def cal_stablecoin(change_pct):
    """7g stablecoin arz %değişimi → -1..+1. + = sermaye giriyor (risk-on). ±%0.5 ≈ güçlü."""
    return _clip(math.tanh(float(change_pct) / 0.5))


def cal_etf(sum_m):
    """10g net ETF akışı (milyon$) → -1..+1. + = kurumsal giriş (risk-on). ±$500M ≈ güçlü."""
    return _clip(math.tanh(float(sum_m) / 500.0))


def cal_cexflow(net_usd):
    """on-chain net (=outflow−inflow) → -1..+1. + = borsadan çıkış = BİRİKİM (bullish). ±$1M ≈ güçlü."""
    return _clip(math.tanh(float(net_usd) / 1_000_000.0))


def liq_pull(clusters, min_intensity=30.0, decay_pct=2.0, top=12):
    """Likidasyon mıknatıslarından YÖNLÜ çekim (-1..+1). + = yukarı çekim, − = aşağı.
    GÜRÜLTÜ-AZALTMA: intensity<eşik ele · mesafe-sönümlü (yakın=ağır) · top-N · tanh sınırla.
    Döner: (pull, nearest_cluster)."""
    cl = [c for c in (clusters or []) if c.get("intensity", 0) >= min_intensity]
    cl.sort(key=lambda c: -c.get("intensity", 0.0))
    cl = cl[:top]
    if not cl:
        return 0.0, None
    raw = 0.0
    for c in cl:
        dist = float(c.get("dist_pct", 0.0))
        w = (c["intensity"] / 100.0) * math.exp(-abs(dist) / decay_pct)
        raw += w * (1.0 if dist > 0 else (-1.0 if dist < 0 else 0.0))
    pull = _clip(math.tanh(raw))
    nearest = min(cl, key=lambda c: abs(float(c.get("dist_pct", 99))))
    return pull, nearest


def _ema(vals, alpha=0.5):
    """Snapshot zaman-serisini yumuşat (gürültü-azaltma). 1 değer → passthrough."""
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    e = vals[0]
    for v in vals[1:]:
        e = alpha * v + (1 - alpha) * e
    return round(e, 4)


# ═══════════ DB yardımcıları ═══════════
def _conn():
    c = sqlite3.connect(_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(_FEAT_SCHEMA)
    return c


def _last_snaps(c, source, symbol=None, k=5):
    """Bir kaynağın son k ham snapshot'ını (yeni→eski) JSON-parse edip döner."""
    if symbol is None:
        rows = c.execute("SELECT data,ts FROM snapshots WHERE source=? AND symbol IS NULL ORDER BY ts DESC LIMIT ?",
                         (source, k)).fetchall()
    else:
        rows = c.execute("SELECT data,ts FROM snapshots WHERE source=? AND symbol=? ORDER BY ts DESC LIMIT ?",
                         (source, symbol, k)).fetchall()
    out = []
    for r in rows:
        try:
            out.append((json.loads(r["data"]), r["ts"]))
        except Exception:
            pass
    return out


# ═══════════ ÖZELLİK ÜRETİMİ ═══════════
def macro_features(c, k=5):
    """Global makro → risk_on_off kompoziti (-1..+1) + bileşenler + kapsam/güven. EMA-yumuşatılmış."""
    comps, cov = {}, {}
    # stablecoin
    s = _last_snaps(c, "macro_stablecoin", None, k)
    comps["stablecoin"] = _ema([cal_stablecoin(d.get("change_pct", 0)) for d, _ in s if "change_pct" in d]) if s else None
    cov["stablecoin"] = bool(s)
    # fng
    s = _last_snaps(c, "macro_fng", None, k)
    comps["fng"] = _ema([cal_fng(d.get("value", 50)) for d, _ in s if d and d.get("value") is not None]) if s else None
    cov["fng"] = comps["fng"] is not None
    # etf (null olabilir)
    s = _last_snaps(c, "macro_etf", None, k)
    etf_vals = [cal_etf(d.get("sum_m", 0)) for d, _ in s if d and d.get("sum_m") is not None]
    comps["etf"] = _ema(etf_vals) if etf_vals else None
    cov["etf"] = comps["etf"] is not None
    avail = [v for v in comps.values() if v is not None]
    risk = round(sum(avail) / len(avail), 4) if avail else None
    return {"risk_on_off": risk, "components": comps, "coverage": cov,
            "confidence": round(len(avail) / 3.0, 2)}


def symbol_features(c, sym, k=5):
    """Parite-başı → liq_pull (likidasyon mıknatıs yönü) + cexflow_signal (kapsam-duyarlı)."""
    out = {"symbol": sym}
    # liqmap
    snaps = _last_snaps(c, "liqmap", sym, k)
    pulls, nearest = [], None
    for d, _ in snaps:
        clusters = d.get("clusters") or (d.get("all_up", []) + d.get("all_down", []))
        p, n = liq_pull(clusters)
        pulls.append(p)
        if nearest is None and n:
            nearest = {"dist_pct": n.get("dist_pct"), "side": n.get("side"), "intensity": n.get("intensity")}
    out["liq_pull"] = _ema(pulls) if pulls else None
    out["liq_nearest"] = nearest
    out["liq_coverage"] = bool(snaps)
    # cexflow (kapsam-duyarlı: covered=false → null, gürültü değil)
    snaps = _last_snaps(c, "cexflow", sym, k)
    cf = next((d for d, _ in snaps if d.get("covered")), None)
    if cf:
        out["cexflow_signal"] = cal_cexflow(cf.get("net_usd", 0))
        out["cexflow_verdict"] = cf.get("verdict")
        out["cexflow_coverage"] = True
    else:
        out["cexflow_signal"] = None
        out["cexflow_coverage"] = False
    return out


def calibration_readiness(c):
    """Her kaynakta kaç snapshot var → kalibrasyon olgunluğu (çok history = sabit-çapa yerine ampirik)."""
    rows = c.execute("SELECT source, COUNT(*) n FROM snapshots GROUP BY source").fetchall()
    return {r["source"]: r["n"] for r in rows}


def build(symbols=None, log=print):
    c = _conn()
    macro = macro_features(c)
    _store_feat(c, None, macro)
    log("== MAKRO özellik ==")
    log("  risk_on_off=%s (güven %s) bileşen=%s" % (macro["risk_on_off"], macro["confidence"], macro["components"]))
    per = {}
    for s in (symbols or []):
        f = symbol_features(c, s)
        _store_feat(c, s, f)
        per[s] = f
        log("  %s: liq_pull=%s nearest=%s | cexflow=%s(%s)" % (
            s, f["liq_pull"], f["liq_nearest"], f["cexflow_signal"],
            "kapsam" if f["cexflow_coverage"] else "kapsam-dışı"))
    readiness = calibration_readiness(c)
    c.close()
    return {"macro": macro, "symbols": per, "calibration_readiness": readiness}


def _store_feat(c, symbol, data):
    c.execute("INSERT INTO features (ts,symbol,data) VALUES (?,?,?)",
              (int(time.time()), symbol, json.dumps(data, ensure_ascii=False, default=str)))
    c.commit()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    r = build(syms)
    print("\n=== KALİBRASYON OLGUNLUĞU (kaynak → snapshot sayısı; çok = ampirik kalibrasyona hazır) ===")
    print(json.dumps(r["calibration_readiness"], ensure_ascii=False))


if __name__ == "__main__":
    main()
