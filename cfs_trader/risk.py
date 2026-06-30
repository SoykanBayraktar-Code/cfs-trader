"""risk — ön-emir kapısı + pozisyon boyutlandırma. Botun en kritik emniyet katmanı.

Sıra: kill-switch durumları → max eşzamanlı → tape kapısı → learner baskısı → boyutlandır.
Hiçbiri geçmezse ENTER yok. Boyut = risk-tavanı ∧ margin-tavanı ∧ min-notional, step'e yuvarlı.
"""
from dataclasses import dataclass


@dataclass
class Sizing:
    qty: float
    notional: float
    risk_usdt: float
    sl_dist_pct: float


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    sizing: Sizing = None
    halt: bool = False        # True → günü durdur (kill-switch)


def _confidence(tape_score, exp, n, brain_conv):
    """Kazanç-ihtimali VEKİLİ 0-1 (tape gücü + learner beklenti + brain konviksiyon). SAF, test edilebilir.
    NOT: bu sinyaller kanıtlı-prediktif DEĞİL — bounded sizing'de çoğunlukla zayıf-setup'ı KÜÇÜLTÜR (güvenli)."""
    comps = [max(0.0, min(1.0, (float(tape_score) - 1.5) / 2.0))]   # tape: 1.5→0, 3.5→1 (CONFIRM 3.0→0.75)
    if n is not None and n >= 3 and exp is not None:
        comps.append(max(0.0, min(1.0, (float(exp) + 0.3) / 0.8)))  # learner: -0.3R→0, +0.5R→1
    if brain_conv is not None:
        comps.append(max(0.0, min(1.0, float(brain_conv))))
    return round(sum(comps) / len(comps), 3)


def dynamic_conf_mult(cfg, conf):
    """Kazanç-ihtimaliyle ORANTILI boyut ÇARPANI ∈ [min_frac, 1.0] (final notional'a uygulanır → gerçekten bağlar).
    enabled değilse 1.0. min_frac=en zayıf setup'ın tam-boyuta oranı."""
    s = cfg.get("dynamic_sizing", {}) or {}
    if not s.get("enabled", False):
        return 1.0
    mf = float(s.get("min_frac", 0.3))
    return round(mf + (1.0 - mf) * max(0.0, min(1.0, conf)), 3)


def leverage_for(cfg, conf, sl_dist):
    """confidence → kaldıraç (base→max), AMA SL likidasyon-ÖNCESİ kalacak max kaldıraçla sınırlı (güvenlik ÖNCE).
    sl_dist ≤ (1/lev − maint)×safety olmalı → geniş-SL'li işlem yüksek kaldıraç ALMAZ. SAF, test edilebilir."""
    s = cfg.get("dynamic_leverage", {}) or {}
    if not s.get("enabled", False):
        return int(cfg.risk["leverage"])
    base = int(s.get("base_leverage", 5)); mx = int(s.get("max_leverage", 8))
    maint = float(s.get("maint_margin", 0.005)); sf = float(s.get("liq_safety_factor", 0.8))
    desired = base + (mx - base) * max(0.0, min(1.0, conf if conf is not None else 0.5))
    safe_lev = 1.0 / (sl_dist / sf + maint) if sl_dist > 0 else mx   # SL'nin güvenli kalacağı max kaldıraç
    return max(base, min(mx, int(min(desired, safe_lev))))


def size_position(cfg, binance, cand, equity, mark):
    """Risk-tavanı + margin-tavanı + min-notional ile qty hesapla. Sizing | None döndürür."""
    r = cfg.risk
    sl_dist = abs(cand.entry - cand.stop) / cand.entry if cand.entry else 0
    if sl_dist <= 0:
        return None, "geçersiz SL mesafesi"
    max_sl = r.get("max_sl_pct", 12) / 100.0
    if sl_dist > max_sl:
        return None, f"SL çok geniş (%{sl_dist*100:.1f} > %{r.get('max_sl_pct', 12)}) — kaldıraçta likidasyon riski"
    # SL TABANI (#3, 06-26): çok dar SL gürültüye stop oluyor (backtest 0-2% bandı net kaybeden) → bandın
    # dışını ELE (genişletmek tavana-takılı risk'i artırırdı). Yalnız 2-6% bandında işlem aç. 0=kapalı.
    min_sl = float((cfg.get("exits", {}) or {}).get("sl_min_pct", 0) or 0) / 100.0
    if min_sl > 0 and sl_dist < min_sl:
        return None, f"SL çok dar (%{sl_dist*100:.2f} < %{min_sl*100:.0f}) — gürültü-stop bandı, giriş yok"

    # DİNAMİK KALDIRAÇ: confidence yüksekse büyür (base→max), SL-güvenli sınırda (likidasyon-öncesi-SL garantisi)
    lev = leverage_for(cfg, getattr(cand, "sizing_confidence", None), sl_dist)
    cand.leverage_used = lev
    risk_cap = equity * r["risk_per_trade_pct"] / 100.0        # tam-güven tavanı (conf_mult ile ölçeklenir)
    desired_notional = risk_cap / sl_dist                        # bu kaybı verecek notional
    max_notional = min(lev * equity, r["max_position_notional_usdt"])
    # kaynak-bazlı çarpan NOTIONAL'a uygulanır (risk = notional*sl_dist). %50 risk + dar SL'de
    # pozisyon hep notional-tavanına takılır → çarpanı risk_cap'e koymak ETKİSİZ kalırdı (momentum=0.5 → yarım boyut).
    risk_mult = getattr(cand, "risk_mult", 1.0) or 1.0
    ctx_tilt = getattr(cand, "context_tilt", 1.0) or 1.0   # bağlam (liq_pull) yumuşak tilt ∈ [1-strength,1.0]
    conf_mult = getattr(cand, "conf_mult", 1.0) or 1.0     # kazanç-ihtimaliyle orantılı (final notional'a → bağlar)
    ls_t = getattr(cand, "ls_tilt", 1.0) or 1.0   # top-trader L/S kalabalik-kontrarian tilt
    sq_t = getattr(cand, "sq_tilt", 1.0) or 1.0   # coinalyze squeeze-farkindalik tilt
    notional = min(desired_notional, max_notional) * risk_mult * ctx_tilt * ls_t * sq_t * conf_mult

    min_notional = binance.min_notional(cand.symbol)
    if notional < min_notional:
        if min_notional <= max_notional:
            notional = min_notional                              # min'e yükselt (margin yetiyorsa)
        else:
            return None, f"min-notional {min_notional} > margin-tavanı {max_notional:.1f}"

    qty = binance.round_qty(cand.symbol, notional / mark)
    if qty <= 0:
        return None, "qty step'e yuvarlanınca 0"
    actual_notional = qty * mark
    if actual_notional < min_notional * 0.999:
        return None, f"yuvarlama sonrası notional {actual_notional:.2f} < min {min_notional}"
    actual_risk = actual_notional * sl_dist
    return Sizing(qty=qty, notional=round(actual_notional, 2),
                  risk_usdt=round(actual_risk, 4), sl_dist_pct=round(sl_dist * 100, 2)), ""


def gate(cfg, store, binance, cand, equity, mark, day, learner=None, scalp_ok=False):
    """Tüm emniyetleri sırayla uygula. GateResult döndürür.
    scalp_ok=True: korumalı scalp yolu — tape CONFIRM/skor şartını GEVŞETİR (VETO + diğer TÜM emniyetler aynen)."""
    r = cfg.risk
    st = store.day_state(day)

    if st["halted"]:
        return GateResult(False, f"gün durduruldu ({st['halt_reason']})")

    # günlük zarar kill-switch (taban = bütçe). daily_max_loss_pct=0 → KAPALI (kullanıcı kaldırdı).
    if r.get("daily_max_loss_pct", 0):
        daily_limit = cfg.budget * r["daily_max_loss_pct"] / 100.0
        if st["realized_pnl"] <= -daily_limit:
            return GateResult(False, f"günlük zarar {st['realized_pnl']:.2f} ≤ -{daily_limit:.2f}", halt=True)

    # ardışık zarar kesici
    if st["consec_losses"] >= r["max_consecutive_losses"]:
        return GateResult(False, f"{st['consec_losses']} ardışık zarar ≥ {r['max_consecutive_losses']}", halt=True)

    # max eşzamanlı pozisyon
    if store.open_count() >= r["max_concurrent"]:
        return GateResult(False, f"max eşzamanlı pozisyon ({r['max_concurrent']}) dolu")

    # ÇİFT-GİRİŞ KORUMASI: aynı sembolde zaten pozisyon varsa girme (DB veya borsa).
    # Borsa kontrolü restart-kesintili yetim pozisyonları + kullanıcının manuel pozisyonlarını da korur.
    if any(t["symbol"] == cand.symbol for t in store.open_trades()):
        return GateResult(False, f"{cand.symbol} zaten açık (DB) — çift-giriş engellendi")
    if not cfg.dry_run:
        try:
            if any(abs(float(p["positionAmt"])) > 0 for p in binance.positions(cand.symbol)):
                return GateResult(False, f"{cand.symbol} borsada zaten açık — çift-giriş engellendi")
        except Exception:
            pass

    # tape kapısı (KATKI-TEMELLİ GEVŞETME): CONFIRM her zaman geçer; ek olarak CONFIRM'e yakın
    # CAUTION'lar (score_avg >= tape_min_score) da geçer. Motor CONFIRM_SCORE=3; tape_min_score=2.7
    # → %10 gevşek. VETO (güçlü ters akış) HER ZAMAN reddedilir (motora dokunulmaz, koruma durur).
    if cfg.signals.get("require_tape_confirm", True):
        tape_min = cfg.signals.get("tape_min_score", 3.0)
        if cand.tape_verdict == "VETO":
            return GateResult(False, "tape VETO (güçlü ters akış)")   # scalp yolu DAHİL her zaman bloklar
        if not scalp_ok and cand.tape_verdict != "CONFIRM" and cand.tape_score < tape_min:
            return GateResult(False, f"tape {cand.tape_verdict} skor {cand.tape_score:.1f} < {tape_min}")

    # kaynak-bazlı SIKI tape eşiği (momentum: gevşemeden bağımsız, skor da yüksek olmalı)
    min_ts = getattr(cand, "min_tape_score", 0.0) or 0.0
    if not scalp_ok and min_ts and cand.tape_score < min_ts:
        return GateResult(False, f"tape skoru zayıf {cand.tape_score:.1f} < {min_ts} (sıkı eşik)")


    # MIKNATIS-ÇELİŞKİ FİLTRESİ (06-25): güçlü likidasyon mıknatısına KARŞI işlem açma.
    # liq_pull>0=yukarı mıknatıs (SHORT'a karşı), <0=aşağı (LONG'a karşı).
    # Veri: mıknatısa-karşı +0.10R vs uyuşan +0.33R. |liq_pull|>=eşik + çelişki → REJECT.
    _cx = cfg.get("context", {}) or {}
    _skip = _cx.get("skip_conflict_above", 0)
    _lp = getattr(cand, "liq_pull", 0.0) or 0.0
    if _cx.get("enabled") and _skip and abs(_lp) >= _skip:
        if (_lp > 0 and cand.side == "SHORT") or (_lp < 0 and cand.side == "LONG"):
            return GateResult(False, f"mıknatısa karşı güçlü çelişki (liq_pull={_lp:+.2f} {cand.side}) — giriş yok")
    # learner baskısı (Faz 3 — enabled değilse atlanır)
    if learner is not None:
        sup, why = learner.suppressed(cand)
        if sup:
            return GateResult(False, f"learner baskısı: {why}")

    # KAZANÇ-İHTİMALİYLE ORANTILI DİNAMİK BOYUT: confidence (tape+learner+brain) → risk% ∈ [min,max]
    exp, n = (None, 0)
    try:
        exp, n = store.expectancy(cand.regime, cand.side, cand.status)
    except Exception:
        pass
    conf = _confidence(cand.tape_score, exp, n, getattr(cand, "brain_conviction", None))
    cm = dynamic_conf_mult(cfg, conf)
    cand.sizing_confidence = conf      # ölçüm/şeffaflık için kaydedilir
    cand.conf_mult = cm
    cand.risk_pct_used = round(r["risk_per_trade_pct"] * cm, 2)   # efektif risk% (kasanın %'si, gösterim)
    sizing, why = size_position(cfg, binance, cand, equity, mark)
    if sizing is None:
        return GateResult(False, f"boyutlandırma: {why}")

    # "Kasa müsaitse işlem açabilsin" — gerçek serbest margin yeni pozisyonun margin'ini karşılıyor mu?
    # (dry_run/paper'da atlanır; canlıda gerçek available_usdt'ye bakar; %2 tampon fee/slippage için.)
    if not cfg.dry_run:
        try:
            need_margin = sizing.notional / r["leverage"]
            avail = binance.available_usdt()
            if avail < need_margin * 1.02:
                return GateResult(False, f"kasa müsait değil: serbest {avail:.2f} USDT < gerekli margin {need_margin:.2f}")
        except Exception as e:
            return GateResult(False, f"margin kontrolü hatası: {e}")

    return GateResult(True, "", sizing=sizing)
