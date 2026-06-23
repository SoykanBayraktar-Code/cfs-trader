"""signals — crypto-futures-scan motorunu sinyal kütüphanesi olarak çağırır.

Motor cwd-duyarlı (sibling import + `sys.path.insert(0,".")` + kronos subprocess), o yüzden
engine çağrıları engine_path'e chdir edilerek yapılır (context manager), sonra geri dönülür.
Motor DOKUNULMAZ — sadece okunur/çağrılır.
"""
import os
import sys
import contextlib
from dataclasses import dataclass


@dataclass
class Candidate:
    symbol: str
    side: str            # LONG | SHORT
    entry: float
    stop: float
    tp: float
    rr: float
    score: int
    atr_pct: float
    status: str          # FRESH | IZLE-pullback | ...  (signal_type)
    regime: str
    bias: str
    tape_verdict: str = "?"
    tape_score: float = 0.0
    risk_mult: float = 1.0       # kaynak-bazlı risk çarpanı (momentum=0.5 → yarım boyut)
    min_tape_score: float = 0.0  # bu adayın geçmesi için gereken min tape skoru (momentum sıkı eşik)
    context_tilt: float = 1.0    # bağlam (liq_pull) yumuşak sizing-tilt'i (≤1.0; çelişme küçültür)
    liq_pull: float = 0.0        # o anki likidasyon-mıknatıs yönü (-1..+1; ölçüm için kaydedilir)


@contextlib.contextmanager
def _engine_cwd(engine_path):
    old_cwd = os.getcwd()
    added = engine_path not in sys.path
    if added:
        sys.path.insert(0, engine_path)
    os.chdir(engine_path)
    try:
        yield
    finally:
        os.chdir(old_cwd)


def _target_tp(entry, stop, side, target_rr):
    """PTJ-tipi asimetrik hedef: aynı stop, TP = giriş ± target_rr × risk-mesafesi."""
    risk = abs(entry - stop)
    return entry + target_rr * risk if side == "LONG" else entry - target_rr * risk


def scan(cfg, min_vol=None, pool=None, top=None):
    """(regime_dict, [Candidate]) döndürür — config filtrelerini uygular (yön/RR/ATR).
    Evren genişliği config'ten: scan_min_vol/scan_pool/scan_top. target_rr>0 ise TP sabit R-katı (PTJ 5:1)."""
    sig = cfg.signals
    min_vol = min_vol if min_vol is not None else sig.get("scan_min_vol", 30_000_000)
    pool = pool if pool is not None else sig.get("scan_pool", 40)
    top = top if top is not None else sig.get("scan_top", 12)
    target_rr = sig.get("target_rr", 0) or 0
    with _engine_cwd(cfg.engine_path):
        import scan_v3
        r = scan_v3.run(min_vol=min_vol, pool=pool, top=top)
    regime = r["regime"]
    cands = []
    for s in r.get("setups", []):
        if s["direction"] not in sig["directions"]:
            continue
        if s.get("rr", 0) < sig["min_rr"]:          # motorun DOĞAL rr'si = yapısal kalite kapısı
            continue
        if s.get("atr_pct", 99) > sig["max_atr_pct"]:
            continue
        entry = float(s["entry"]); stop = float(s["stop"])
        if target_rr > 0:                            # PTJ 5:1 — TP'yi sabit R-katına çek, trailing koşturur
            tp = _target_tp(entry, stop, s["direction"], target_rr); rr = target_rr
        else:
            tp = float(s.get("tp2") or s.get("tp1")); rr = float(s.get("rr", 0))
        cands.append(Candidate(
            symbol=s["symbol"], side=s["direction"], entry=entry, stop=stop, tp=tp,
            rr=rr, score=int(s.get("score", 0)), atr_pct=float(s.get("atr_pct", 0)),
            status=s.get("status", "?"), regime=regime["regime"], bias=regime["bias"],
        ))
    return regime, cands


# ---- Anomali/momentum sinyal kaynakları (scan_v3'e EK; hepsi tape+risk+trailing'den geçer) ----

def _mom_levels(px, atr_pct, side, sl_atr_mult, tp_rr):
    """Momentum/anomali adayı için ATR-tabanlı seviye. (entry, stop, tp, rr) | None.
    Momentum'da yapısal pullback yok → market giriş + ATR-stop; trailing üstünü toplar."""
    atr = (atr_pct / 100.0) * px
    if atr <= 0:
        return None
    if side == "LONG":
        stop = px - sl_atr_mult * atr
        tp = px + tp_rr * (px - stop)
    else:
        stop = px + sl_atr_mult * atr
        tp = px - tp_rr * (stop - px)
    if stop <= 0 or tp <= 0:
        return None
    return px, stop, tp, tp_rr


def _oi_direction(taker, fund):
    """oi_surge yön ipucu (engine mantığı): taker buy/sell + funding. Belirsizse None."""
    if taker is None:
        return None
    if taker >= 1.15 and (fund or 0) < 0.05:
        return "LONG"
    if taker <= 0.87:
        return "SHORT"
    return None


def _class_ok(text, keywords):
    """Sınıf etiketi (emoji'li) istenen anahtar kelimelerden birini içeriyor mu."""
    t = (text or "").upper()
    return any(k.upper() in t for k in keywords)


def _build_mom_candidate(symbol, side, score, m, regime, signal_type, risk_mult=1.0, min_tape_score=0.0):
    """setup_levels'tan px+atr al → ATR-tabanlı Candidate kur. _engine_cwd İÇİNDE çağrılmalı. None=ele."""
    import setup_levels
    try:
        b = setup_levels.build(symbol, side)
    except Exception:
        return None
    px = b.get("price")
    atr_pct = b.get("atr_pct", 0) or 0
    if not px or atr_pct <= 0 or atr_pct > m.get("max_atr_pct", 8.0):
        return None
    lv = _mom_levels(px, atr_pct, side, m.get("sl_atr_mult", 1.5), m.get("tp_rr", 2.5))
    if not lv:
        return None
    entry, stop, tp, rr = lv
    return Candidate(symbol=symbol, side=side, entry=entry, stop=stop, tp=tp, rr=rr,
                     score=int(round(score)), atr_pct=float(atr_pct), status=signal_type,
                     regime=regime["regime"], bias=regime["bias"],
                     risk_mult=float(risk_mult), min_tape_score=float(min_tape_score))


def scan_momentum(cfg, regime):
    """momentum_scan → boğa-breakout adayları (ATESLENIYOR/KOSUYOR; TUKENMIS=tuzak elenir). [Candidate]."""
    m = cfg.get("momentum", {}) or {}
    mc = m.get("momentum_scan", {}) or {}
    classes = mc.get("classes", ["ATESLENIYOR", "KOSUYOR"])
    cap = m.get("max_per_tick", 4)
    cands = []
    with _engine_cwd(cfg.engine_path):
        import momentum_scan
        r = momentum_scan.run(min_vol=mc.get("min_vol", 5_000_000), top=mc.get("top", 16))
        for row in r.get("rows", []):
            if not _class_ok(row.get("tag"), classes):
                continue
            side = "LONG"   # momentum_scan yalnız boğa-breakout üretir
            if side not in cfg.signals["directions"]:
                continue
            c = _build_mom_candidate(row["sym"], side, row.get("score", 0), m, regime, "MOMENTUM",
                                     risk_mult=mc.get("risk_mult", 0.5), min_tape_score=mc.get("min_tape_score", 4.5))
            if c:
                cands.append(c)
            if len(cands) >= cap:
                break
    return cands


def scan_oi_surge(cfg, regime):
    """oi_surge → pre-pump birikim adayları (SESSIZ/ERKEN; GEC=kovalama elenir). Yön taker/fund. [Candidate]."""
    m = cfg.get("momentum", {}) or {}
    oc = m.get("oi_surge", {}) or {}
    classes = oc.get("classes", ["SESSIZ", "ERKEN"])
    cap = m.get("max_per_tick", 4)
    cands = []
    with _engine_cwd(cfg.engine_path):
        import oi_surge
        r = oi_surge.run(min_vol=oc.get("min_vol", 20_000_000), oi_min=oc.get("oi_min", 4.0), top=oc.get("top", 12))
        for row in r.get("surge", []):
            if not _class_ok(row.get("cls"), classes):
                continue
            side = _oi_direction(row.get("taker"), row.get("fund"))
            if side is None or side not in cfg.signals["directions"]:
                continue
            if oc.get("long_only", False) and side != "LONG":
                continue   # oi_surge tezi = birikim→yükseliş; SHORT'lar 0/2 kaybetti
            c = _build_mom_candidate(row["sym"], side, row.get("score", 0), m, regime, "OI-SURGE",
                                     risk_mult=oc.get("risk_mult", 1.0), min_tape_score=oc.get("min_tape_score", 0.0))
            if c:
                cands.append(c)
            if len(cands) >= cap:
                break
    return cands


def _is_pullback_long(b, pm):
    """AKILLI PULLBACK: güçlü uptrend + ema21'e geri çekilmiş + sağlıklı RSI = 'trendde dip al' (tepe değil).
    b = setup_levels.build çıktısı. True = giriş-adayı pullback."""
    tr = b.get("trends", {}) or {}
    if tr.get("1h") != "UP" or tr.get("4h") != "UP":     # üst zaman dilimi trendi yukarı olmalı
        return False
    px = b.get("price"); e21 = b.get("ema21"); e50 = b.get("ema50"); rsi = b.get("rsi", 50)
    if not (px and e21 and e50) or not (e21 > e50):       # ema dizilimi yukarı (kısa>uzun)
        return False
    band = pm.get("pullback_band_pct", 2.0) / 100.0
    if px > e21 * (1 + band):                              # ema21'in ÇOK üstünde = uzamış/tepe, pullback değil
        return False
    if px < e21 * (1 - 2 * band):                          # ema21'in çok altında = trend kırılıyor
        return False
    if not (pm.get("rsi_min", 40) <= rsi <= pm.get("rsi_max", 68)):  # aşırı-alım(tepe) ve çöküş dışı
        return False
    return True


def scan_pullback_momentum(cfg, regime):
    """Akıllı pullback-momentum: güçlü mover'lar arasından TRENDDE geri çekilmiş olanları LONG yakalar.
    Tepe kovalamaz (ema21'e pullback + sağlıklı RSI). scan_all'a EK kaynak — tam CONFIRM + yarım risk."""
    pm = cfg.get("pullback_momentum", {}) or {}
    if not pm.get("enabled", False) or "LONG" not in cfg.signals["directions"]:
        return []
    target_rr = cfg.signals.get("target_rr", 5.0) or 5.0
    cands = []
    with _engine_cwd(cfg.engine_path):
        import datahub, setup_levels
        try:
            from coil_scanner import NON_CRYPTO
        except Exception:
            NON_CRYPTO = set()
        uni = datahub.get_ticker(min_vol=pm.get("min_vol", 10_000_000), exclude=NON_CRYPTO)
        movers = sorted([r for r in uni if r.get("chg24", 0) >= pm.get("min_chg24", 8.0)],
                        key=lambda r: -r["chg24"])[:pm.get("top_gainers", 12)]
        for r in movers:
            sym = r["symbol"]
            try:
                b = setup_levels.build(sym, "LONG")
            except Exception:
                continue
            if not _is_pullback_long(b, pm):
                continue
            px = b.get("price"); atr_pct = b.get("atr_pct", 0) or 0
            if not px or atr_pct <= 0 or atr_pct > pm.get("max_atr_pct", 8.0):
                continue
            lv = _mom_levels(px, atr_pct, "LONG", pm.get("sl_atr_mult", 1.5), target_rr)
            if not lv:
                continue
            entry, stop, tp, rr = lv
            cands.append(Candidate(
                symbol=sym, side="LONG", entry=entry, stop=stop, tp=tp, rr=rr,
                score=int(r.get("chg24", 0)), atr_pct=float(atr_pct), status="PULLBACK-MOM",
                regime=regime["regime"], bias=regime["bias"],
                risk_mult=float(pm.get("risk_mult", 0.5)), min_tape_score=float(pm.get("min_tape_score", 3.0)),
            ))
            if len(cands) >= pm.get("max_per_tick", 3):
                break
    return cands


def scan_all(cfg):
    """scan_v3 + (oi_surge/momentum/pullback-momentum) birleşik aday listesi. Sembol bazında dedup, anomali ÖNCELİKLİ
    (paylaşılan sembolde momentum framing kazanır + tape bütçesinde öne geçer). (regime, [Candidate])."""
    regime, v3 = scan(cfg)
    m = cfg.get("momentum", {}) or {}
    extra = []
    if m.get("enabled", False):
        srcs = m.get("sources", ["oi_surge", "momentum_scan"])
        if "oi_surge" in srcs:
            try:
                extra += scan_oi_surge(cfg, regime)
            except Exception:
                pass
        if "momentum_scan" in srcs:
            try:
                extra += scan_momentum(cfg, regime)
            except Exception:
                pass
    # akıllı pullback-momentum (momentum bloğundan bağımsız, kendi config'iyle)
    try:
        extra += scan_pullback_momentum(cfg, regime)
    except Exception:
        pass
    seen, merged = set(), []
    for c in extra + v3:
        if c.symbol in seen:
            continue
        seen.add(c.symbol)
        merged.append(c)
    return regime, merged


def confirm_tape(cfg, cand, dur=22):
    """Adayı 22s derin tape'den geçir; cand.tape_verdict/tape_score günceller ve verdict döndürür."""
    with _engine_cwd(cfg.engine_path):
        import tape
        res = tape.tape_check(cand.symbol, cand.side, dur=dur)
    cand.tape_verdict = res.get("verdict", "?")
    cand.tape_score = res.get("score_avg", 0.0)
    return res


# ───────────────────────── bağlam: liq_pull yumuşak sizing-tilt ─────────────────────────
def live_liq_pull(cfg, symbol):
    """Canlı likidasyon haritasından liq_pull (-1..+1). Hata/None → None (fail-safe)."""
    from . import features
    try:
        with _engine_cwd(cfg.engine_path):
            import liqmap
            hm = liqmap.liq_heatmap(symbol)
        if not hm:
            return None
        pull, _ = features.liq_pull(hm.get("clusters", []))
        return pull
    except Exception:
        return None


def _tilt_from_pull(lp, side, strength=0.4, min_abs=0.05):
    """SAF tilt mantığı (test edilebilir). tilt ∈ [1-strength, 1.0] — yalnız çelişme küçültür."""
    if lp is None or abs(lp) < min_abs:
        return 1.0
    side_sign = 1.0 if side == "LONG" else -1.0
    agreement = max(-1.0, min(1.0, lp * side_sign))      # +1 uyuşma, -1 çelişme
    tilt = 1.0 - strength * max(0.0, -agreement)         # yalnız çelişme küçültür
    return round(max(1.0 - strength, min(1.0, tilt)), 4)


def context_tilt(cfg, cand):
    """liq_pull'a göre YUMUŞAK sizing-tilt → (tilt, liq_pull). FAIL-SAFE.
    GÜVENLİK: tilt ∈ [1-strength, 1.0] — SADECE çelişme küçültür; uyuşma=tam boyut → notional-cap
    asla aşılmaz (likidasyon riski ARTMAZ). |liq_pull|<min_abs = net görüş yok → 1.0 (dokunmaz)."""
    c = cfg.get("context", {}) or {}
    if not c.get("enabled", False) or c.get("source", "liq_pull") != "liq_pull":
        return 1.0, None
    lp = live_liq_pull(cfg, cand.symbol)
    if lp is None:
        return 1.0, None
    tilt = _tilt_from_pull(lp, cand.side, float(c.get("tilt_strength", 0.4)), float(c.get("min_abs", 0.05)))
    return tilt, round(lp, 4)
