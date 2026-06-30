"""derivs_ctx — türev-istihbarat CONFLUENCE sentez katmanı (SHADOW-first).

Mevcut kaynakları (cz.py funding/OI/liq, ls.py bias, scalp oi_trend, F&G) YENİDEN
ÇEKMEZ; cand'da zaten dolu alanları + F&G'yi (engine macro_ctx reuse) okuyup sinyalin
YÖNÜYLE kıyaslar → tek bir derivs_bias {CONFIRM,CAUTION,CONFLICT,NEUTRAL} + derivs_score.

FAIL-OPEN: girdi yoksa NEUTRAL; hiçbir çağrı raise etmez (döngü ASLA bloklanmaz).
Eşikler/ağırlıklar config'de (derivs:). KALİBRASYON (06-29, 67-işlem canlı veri):
  - ls_bias (+0.59 ayrım) ve oi_trend (+0.53) en güçlü ayrıştırıcı → ağırlık 1.0
  - funding (yalnız all_pos/all_neg ikili) ve liq_imb zayıf/gürültülü → ağırlık 0.4
  - F&G yön-gürültü (ADIM3 backtest %36; dönem Extreme Fear + kazanan=SHORT) → ağırlık 0.0
  - cvd_divergence ÖLÜ (veride tümü 0) → KULLANILMAZ
İlk teslim SHADOW: hesaplar + loglar, canlı kararı (yön/boyut/kaldıraç) DEĞİŞTİRMEZ.
"""
import json
import time

_fng_cache = {"ts": 0.0, "val": None, "label": None}
_FNG_TTL = 3600.0


def _f(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _cz_fields(cand):
    """cand.cz_snapshot (JSON) -> {funding_div, all_pos, all_neg, oi_total, liq_imb}. Eksikse {}."""
    out = {}
    snap = getattr(cand, "cz_snapshot", None)
    if not snap:
        return out
    try:
        d = json.loads(snap) if isinstance(snap, str) else snap
        coin = (d or {}).get("coin", {}) or {}
        out["funding_div"] = _f(coin.get("funding_div"))
        out["all_pos"] = bool(coin.get("all_pos"))
        out["all_neg"] = bool(coin.get("all_neg"))
        out["oi_total"] = _f(coin.get("oi_total"))
        out["liq_imb"] = _f((coin.get("liq", {}) or {}).get("imb"))
    except Exception:
        pass
    return out


def _fng(cfg):
    """F&G ham 0-100 + sınıf (engine macro_ctx reuse, 1sa cache, fail-safe). YALNIZ loglanır (ağırlık 0)."""
    now = time.time()
    if _fng_cache["val"] is not None and now - _fng_cache["ts"] < _FNG_TTL:
        return _fng_cache["val"], _fng_cache["label"]
    val, label = None, None
    try:
        from .signals import _engine_cwd
        with _engine_cwd(cfg.engine_path):
            import macro_ctx
            fg = macro_ctx.fear_greed()
        if fg:
            val = _f(fg.get("value"))
            label = fg.get("label")
    except Exception:
        val, label = None, None
    _fng_cache.update({"ts": now, "val": val, "label": label})
    return val, label


def components(side, inp, cfg):
    """SAF, test edilebilir. side LONG/SHORT, inp=girdi dict. Her bileşen ∈ [-1,1] (+ = işlem
    yönünü DESTEKLER). Döner: (comps {ad:(deger,agirlik)}, ağırlıklı_skor, aktif_bileşen_sayısı)."""
    dc = cfg.get("derivs", {}) or {}
    w = dc.get("weights", {}) or {}
    is_long = (side == "LONG")
    oi_norm = float(dc.get("oi_collapse_norm", 1.0)) or 1.0
    comps = {}

    # 1) ls_extreme (kalabalık-kontraryan): ls>0=kalabalık short→LONG favori; <0=kalabalık long→SHORT favori
    ls = _f(inp.get("ls_bias"))
    if ls is not None:
        comps["ls"] = (_clamp(ls if is_long else -ls), float(w.get("ls", 1.0)))

    # 2) oi_trend: OI yükseliyor(+)=trend sağlam; çöküyor(-)=zayıf katılım/fakeout (bot trend-uyumlu girer)
    oi = _f(inp.get("oi_trend"))
    if oi is not None:
        comps["oi"] = (_clamp(oi / oi_norm), float(w.get("oi", 1.0)))

    # 3) funding (yalnız temiz ikili): all_pos=tüm borsa funding+ =kalabalık uniform LONG→squeeze-down→SHORT favori
    fund_align = None
    if inp.get("all_pos"):
        fund_align = (-1.0 if is_long else 1.0)
    elif inp.get("all_neg"):
        fund_align = (1.0 if is_long else -1.0)
    if fund_align is not None:
        comps["funding"] = (fund_align, float(w.get("funding", 0.4)))

    # 4) liq_imbalance: imb>0=short-squeeze(yukarı baskı)→LONG destek; <0=long-flush(aşağı)→SHORT destek
    imb = _f(inp.get("liq_imb"))
    if imb is not None:
        comps["liq"] = (_clamp(imb if is_long else -imb), float(w.get("liq", 0.4)))

    # 5) fng: AĞIRLIK 0 (yön-gürültü, kalibrasyon) → skora KATILMAZ; yalnız detayda loglanır
    fngv = _f(inp.get("fng"))
    if fngv is not None:
        lo = float(dc.get("fng_extreme_lo", 25)); hi = float(dc.get("fng_extreme_hi", 75))
        fa = 0.0
        if fngv >= hi:
            fa = (-1.0 if is_long else 0.0)
        elif fngv <= lo:
            fa = (0.0 if is_long else -1.0)
        comps["fng"] = (fa, float(w.get("fng", 0.0)))

    wsum = sum(wt for _, wt in comps.values())
    score = (sum(v * wt for v, wt in comps.values()) / wsum) if wsum > 0 else 0.0
    n_active = sum(1 for _, wt in comps.values() if wt > 0)
    return comps, round(score, 4), n_active


def evaluate(cfg, cand):
    """cand'dan girdileri topla → (derivs_bias, derivs_score, snapshot_json). FAIL-OPEN (hata→NEUTRAL)."""
    dc = cfg.get("derivs", {}) or {}
    try:
        cz = _cz_fields(cand)
        fngv, fnglabel = _fng(cfg)
        inp = {
            "ls_bias": getattr(cand, "ls_bias", None),
            "oi_trend": getattr(cand, "oi_trend", None),
            "funding_div": cz.get("funding_div"),
            "all_pos": cz.get("all_pos"), "all_neg": cz.get("all_neg"),
            "liq_imb": cz.get("liq_imb"),
            "fng": fngv,
        }
        comps, score, n_active = components(cand.side, inp, cfg)
        min_comp = int(dc.get("min_components", 2))
        ct = float(dc.get("confirm_thresh", 0.35))
        cf = float(dc.get("conflict_thresh", 0.35))
        if n_active < min_comp:
            bias = "NEUTRAL"
        elif score >= ct:
            bias = "CONFIRM"
        elif score <= -cf:
            bias = "CONFLICT"
        else:
            bias = "CAUTION"
        snap = json.dumps({
            "bias": bias, "score": score, "n_active": n_active,
            "comps": {k: round(v, 3) for k, (v, wt) in comps.items()},
            "fng": fngv, "fng_label": fnglabel,
        }, separators=(",", ":"))
        return bias, score, snap
    except Exception:
        return "NEUTRAL", 0.0, None


def evaluate_symbol(cfg, symbol, side, oi_trend=None):
    """Kuru-test/standalone: cz + ls + F&G'yi TAZE çeker, evaluate eder (GERÇEK EMİR YOK).
    oi_trend kline-türevi (scalp) — standalone'da verilmezse None (o bileşen düşer)."""
    class _C:
        pass
    c = _C(); c.symbol = symbol; c.side = side; c.oi_trend = oi_trend
    try:
        from . import cz as czmod
        c.cz_snapshot = czmod.snapshot(symbol)
    except Exception:
        c.cz_snapshot = None
    try:
        from . import ls as lsmod
        c.ls_bias = lsmod.bias(lsmod.live_snapshot(symbol))
    except Exception:
        c.ls_bias = None
    return evaluate(cfg, c)


if __name__ == "__main__":
    import sys
    from .cfg import get
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    side = sys.argv[2] if len(sys.argv) > 2 else "SHORT"
    b, s, snap = evaluate_symbol(get(), sym, side)
    print(f"{sym} {side}: bias={b} score={s}\n{snap}")
