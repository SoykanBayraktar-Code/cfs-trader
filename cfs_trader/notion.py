"""notion — cfs-trader işlemlerini Notion 'İşlem Kayıt Defteri' DB'sine otomatik loglar.

FAIL-SAFE: HER hata yutulur, ASLA trade yoluna sızmaz (loglama trading'i etkilemez).
  Giriş  → Sonuç=AÇIK satır oluşturur, page_id döner (store'a yazılır).
  Kapanış → aynı satırı KAZANÇ/KAYIP + çıkış alanlarıyla günceller (page_id yoksa tam satır oluşturur).
Bağımlılık YOK (urllib + stdlib). Token: secrets.env NOTION_TOKEN (cfg ortam değişkenine yükler).
Yapılandırma: config notion: {enabled, database_id, api_version, timeout}. enabled=false → tamamen pasif.
"""
import os
import json
import urllib.request
import datetime

API = "https://api.notion.com/v1"

# select kolonlarında izinli değerler (DB şemasıyla birebir; bilinmeyen → o alan yazılmaz, hata değil)
SIG = {"FRESH", "IZLE-pullback", "IZLE-uzamis", "PULLBACK-MOM", "MOMENTUM", "OI-SURGE", "COIL-BREAKOUT"}
TAPE = {"CONFIRM", "CAUTION", "VETO", "NODATA"}
TRAIL = {"INIT", "BE", "TRAIL"}
EXR = {"TP", "SL", "TRAILING", "TIME", "MANUAL", "KILLSWITCH"}
REG = {"TREND_UP", "TREND_DOWN", "RANGE"}
FLOW = {"AKIS_UP", "AKIS_DOWN", "DIVERJANS_AYI", "DIVERJANS_BOGA", "NOTR"}


def _cfg(cfg):
    return cfg.get("notion", {}) or {}


def _token():
    return os.environ.get("NOTION_TOKEN")


def enabled(cfg):
    """Loglama aktif mi: config.notion.enabled + token + database_id hepsi var mı."""
    c = _cfg(cfg)
    return bool(c.get("enabled", False) and _token() and c.get("database_id"))


def _req(method, url, cfg, body=None):
    """Notion REST çağrısı → JSON | None. Çağıran fail-safe sarmalar."""
    tok = _token()
    if not tok:
        return None
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}",
        "Notion-Version": str(_cfg(cfg).get("api_version", "2022-06-28")),
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=float(_cfg(cfg).get("timeout", 8))) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- property builder (Notion REST formatı) ----------
def _f(x):
    try:
        return round(float(x), 8)
    except Exception:
        return None


def _num(p, k, v):
    if v is not None:
        p[k] = {"number": v}


def _sel(p, k, v, allowed=None):
    if v and (allowed is None or v in allowed):
        p[k] = {"select": {"name": str(v)}}


def _txt(p, k, v, n=1900):
    if v:
        p[k] = {"rich_text": [{"text": {"content": str(v)[:n]}}]}


def _date(p, k, ts):
    try:
        iso = datetime.datetime.fromtimestamp(int(ts), datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        p[k] = {"date": {"start": iso}}
    except Exception:
        pass


def _props(trade):
    """trade satırı (Row/dict) → Notion REST properties. Türemiş: Sonuç, Notional, SL Mesafe %, RR, MFE, Süre."""
    t = dict(trade)
    g = t.get
    e = _f(g("entry")); si = _f(g("sl_init")) or _f(g("sl")); tp = _f(g("tp")); pk = _f(g("peak_price"))
    side = (g("side") or "").upper(); pnl = _f(g("pnl_usdt")); status = (g("status") or "").upper()
    sonuc = "AÇIK" if status == "OPEN" else ("KAZANÇ" if (pnl or 0) > 0 else ("KAYIP" if (pnl or 0) < 0 else "BREAKEVEN"))
    qty = _f(g("qty")); notional = round(qty * e, 2) if (qty and e) else None
    slpct = round(abs(si - e) / e * 100, 3) if (e and si) else None
    rr = round(abs(tp - e) / abs(si - e), 2) if (e and si and tp and abs(si - e) > 0) else None

    p = {"İşlem": {"title": [{"text": {"content": f"{g('symbol')} {side} #{g('id')}"}}]}}
    _num(p, "Bot ID", g("id")); _txt(p, "Sembol", g("symbol"), 60)
    _sel(p, "Yön", side, {"LONG", "SHORT"}); _sel(p, "Sonuç", sonuc)
    _sel(p, "Rejim", g("regime"), REG); _sel(p, "Sinyal Tipi", g("signal_type"), SIG)
    _sel(p, "Tape Kararı", g("tape_verdict"), TAPE); _sel(p, "Mod", g("mode"), {"live", "testnet"})
    _num(p, "Giriş", e); _num(p, "SL", _f(g("sl"))); _num(p, "SL İlk", si); _num(p, "TP", tp)
    _num(p, "SL Mesafe %", slpct); _num(p, "Planlanan RR", rr); _num(p, "Kaldıraç", _f(g("leverage")))
    _num(p, "Miktar", qty); _num(p, "Notional", notional)
    _num(p, "Risk USDT", _f(g("risk_usdt"))); _num(p, "Risk %", _f(g("risk_pct_used")))
    _num(p, "liq_pull", _f(g("liq_pull"))); _num(p, "Bağlam Tilt", _f(g("context_tilt")))
    _num(p, "L/S Bias", _f(g("ls_bias"))); _num(p, "L/S Tilt", _f(g("ls_tilt"))); _num(p, "Squeeze Tilt", _f(g("sq_tilt")))
    _num(p, "Brain Konviksiyon", _f(g("brain_conviction"))); _num(p, "Brain Size Hint", _f(g("brain_size_hint")))
    _num(p, "Sizing Confidence", _f(g("sizing_confidence")))
    # Faz 0 SHADOW özellikleri
    _num(p, "CVD 15dk", _f(g("cvd15"))); _num(p, "CVD 30dk", _f(g("cvd30"))); _num(p, "CVD 60dk", _f(g("cvd60")))
    _num(p, "Funding", _f(g("funding"))); _num(p, "Basis (bps)", _f(g("basis_bps")))
    _sel(p, "Flow Rejimi", g("flow_regime"), FLOW); _num(p, "Konfluans", _f(g("confluence")))
    # Faz 1 SHADOW: scalp/coiled-breakout bağlamları
    _num(p, "Squeeze %", _f(g("squeeze_pct"))); _num(p, "ATR Daralma", _f(g("atr_contraction")))
    _num(p, "OI Trend %", _f(g("oi_trend"))); _num(p, "CVD Diverjans", _f(g("cvd_divergence")))
    _num(p, "Defter Asim", _f(g("book_asym"))); _num(p, "Hacim Patlama", _f(g("vol_surge")))
    _num(p, "Range Pozisyon", _f(g("range_pos"))); _num(p, "Scalp Skor", _f(g("scalp_score")))
    _txt(p, "Brain Notu", g("llm_note"), 500); _txt(p, "cz_snapshot", g("cz_snapshot"), 180); _txt(p, "ls_snapshot", g("ls_snapshot"), 180)
    if g("ts_open"):
        _date(p, "Açılış", g("ts_open"))
    if status != "OPEN":
        _sel(p, "Çıkış Sebebi", g("exit_reason"), EXR); _num(p, "Çıkış Fiyatı", _f(g("exit_price")))
        _num(p, "PnL USDT", pnl); _num(p, "R Multiple", _f(g("r_multiple"))); _num(p, "Fee USDT", _f(g("fees_usdt")))
        _num(p, "Tepe Fiyat", pk)
        mfe = round((((e - pk) if side == "SHORT" else (pk - e)) / abs(e - si)), 2) if (pk and e and si and abs(e - si) > 0) else None
        _num(p, "MFE (R)", mfe); _sel(p, "Trail Durumu", g("trail_state"), TRAIL)
        dur = round((int(g("ts_close")) - int(g("ts_open"))) / 3600, 2) if (g("ts_close") and g("ts_open")) else None
        _num(p, "Süre (saat)", dur)
        if g("ts_close"):
            _date(p, "Kapanış", g("ts_close"))
    return p


def log_entry(cfg, trade):
    """Girişte Notion satırı oluştur (Sonuç=AÇIK). page_id | None. FAIL-SAFE."""
    try:
        if not enabled(cfg):
            return None
        body = {"parent": {"database_id": _cfg(cfg)["database_id"]}, "properties": _props(trade)}
        res = _req("POST", f"{API}/pages", cfg, body)
        return res.get("id") if res else None
    except Exception:
        return None


def log_exit(cfg, trade, page_id=None):
    """Kapanışta satırı güncelle (Sonuç + çıkış alanları). page_id yoksa tam satır oluştur. FAIL-SAFE → bool."""
    try:
        if not enabled(cfg):
            return False
        props = _props(trade)
        if page_id and str(page_id) not in ("None", "null", ""):
            res = _req("PATCH", f"{API}/pages/{page_id}", cfg, {"properties": props})
        else:
            res = _req("POST", f"{API}/pages", cfg, {"parent": {"database_id": _cfg(cfg)["database_id"]}, "properties": props})
        return bool(res)
    except Exception:
        return False
