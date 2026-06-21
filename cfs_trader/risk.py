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


def size_position(cfg, binance, cand, equity, mark):
    """Risk-tavanı + margin-tavanı + min-notional ile qty hesapla. Sizing | None döndürür."""
    r = cfg.risk
    sl_dist = abs(cand.entry - cand.stop) / cand.entry if cand.entry else 0
    if sl_dist <= 0:
        return None, "geçersiz SL mesafesi"
    max_sl = r.get("max_sl_pct", 12) / 100.0
    if sl_dist > max_sl:
        return None, f"SL çok geniş (%{sl_dist*100:.1f} > %{r.get('max_sl_pct', 12)}) — kaldıraçta likidasyon riski"

    risk_cap = equity * r["risk_per_trade_pct"] / 100.0          # SL'de kaybedilecek tavan
    desired_notional = risk_cap / sl_dist                        # bu kaybı verecek notional
    max_notional = min(r["leverage"] * equity, r["max_position_notional_usdt"])
    notional = min(desired_notional, max_notional)

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


def gate(cfg, store, binance, cand, equity, mark, day, learner=None):
    """Tüm emniyetleri sırayla uygula. GateResult döndürür."""
    r = cfg.risk
    st = store.day_state(day)

    if st["halted"]:
        return GateResult(False, f"gün durduruldu ({st['halt_reason']})")

    # günlük zarar kill-switch (taban = bütçe)
    daily_limit = cfg.budget * r["daily_max_loss_pct"] / 100.0
    if st["realized_pnl"] <= -daily_limit:
        return GateResult(False, f"günlük zarar {st['realized_pnl']:.2f} ≤ -{daily_limit:.2f}", halt=True)

    # ardışık zarar kesici
    if st["consec_losses"] >= r["max_consecutive_losses"]:
        return GateResult(False, f"{st['consec_losses']} ardışık zarar ≥ {r['max_consecutive_losses']}", halt=True)

    # max eşzamanlı pozisyon
    if store.open_count() >= r["max_concurrent"]:
        return GateResult(False, f"max eşzamanlı pozisyon ({r['max_concurrent']}) dolu")

    # tape CONFIRM kapısı
    if cfg.signals.get("require_tape_confirm", True) and cand.tape_verdict != "CONFIRM":
        return GateResult(False, f"tape {cand.tape_verdict} (CONFIRM değil)")

    # learner baskısı (Faz 3 — enabled değilse atlanır)
    if learner is not None:
        sup, why = learner.suppressed(cand)
        if sup:
            return GateResult(False, f"learner baskısı: {why}")

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
