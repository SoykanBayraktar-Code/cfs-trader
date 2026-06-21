"""trailing — Aşama 1 çıkış yönetimi: breakeven + trailing stop (kazananı koştur).

poll_tick'te reconcile'dan ÖNCE çağrılır. Her açık işlemde:
  peak (high-water) izlenir → +breakeven_at_r'de SL girişe çekilir,
  +trail_after_r sonrası SL = tepe − trail_distance_r·R olarak peak'i takip eder.
SL YALNIZ lehte taşınır (asla gevşemez). Hard SL hep borsada: yeni SL ÖNCE konur, sonra
eski iptal edilir → pozisyon asla çıplak kalmaz. dry_run'da binance sentetik; SL fiyatı
DB'de güncellenir, reconcile simülasyonu güncel SL'yi kullanır (paper'da da gerçekçi).
"""


def _r_now(side, entry, sl_init, price):
    """(şu anki R, R-birimi-fiyat). r_unit = |giriş − ilkSL|. r_unit≤0 ise (None,0)."""
    r_unit = abs(entry - sl_init)
    if r_unit <= 0:
        return None, 0.0
    r = (price - entry) / r_unit if side == "LONG" else (entry - price) / r_unit
    return r, r_unit


def _desired_sl(ex, side, entry, peak_price, r_unit, peak_r):
    """peak_r'ye göre hedef SL fiyatı + durum. (None, None) = taşıma yok.
    LONG'da yüksek SL = daha iyi; SHORT'ta düşük = daha iyi."""
    be_at = ex.get("breakeven_at_r", 0.8)
    trail_after = ex.get("trail_after_r", 1.0)
    trail_dist = ex.get("trail_distance_r", 0.7)
    be_buf = ex.get("breakeven_buffer_pct", 0.05) / 100.0

    eps = 1e-9  # float kılpayı: 0.79999..R, +0.8R eşiğini kaçırmasın
    target, state = None, None
    # breakeven — SL girişe (+ küçük lehte tampon, round-trip fee'yi karşılar)
    if peak_r >= be_at - eps:
        target = entry * (1 + be_buf) if side == "LONG" else entry * (1 - be_buf)
        state = "BE"
    # trailing — tepe ∓ trail_dist·R; breakeven'dan daha iyiyse devralır
    if peak_r >= trail_after - eps:
        tp_sl = (peak_price - trail_dist * r_unit) if side == "LONG" else (peak_price + trail_dist * r_unit)
        if target is None or (side == "LONG" and tp_sl > target) or (side == "SHORT" and tp_sl < target):
            target, state = tp_sl, "TRAIL"
    return target, state


def _better(side, new_sl, cur_sl, min_move_frac):
    """new_sl mevcut SL'den anlamlı (lehte + min eşik) daha iyi mi?"""
    if side == "LONG":
        return new_sl > cur_sl * (1 + min_move_frac)
    return new_sl < cur_sl * (1 - min_move_frac)


def _replace_sl(binance, trade, new_sl):
    """Yeni SL (closePosition) koy → başarılıysa eskiyi iptal et. (ok, yeni_algo_id) döndürür.
    Sıra önemli: önce yeni konur (çıplak pencere yok), sonra eski iptal (TP'ye dokunulmaz)."""
    sym = trade["symbol"]
    exit_side = "SELL" if trade["side"] == "LONG" else "BUY"
    try:
        res = binance.place_stop_market(sym, exit_side, new_sl, close_position=True)
    except Exception:
        return False, None
    new_id = str(res.get("algoId") or res.get("orderId"))
    old_id = trade["sl_order_id"]
    if (old_id and str(old_id) not in ("None", "null")
            and not str(old_id).startswith("DRY") and str(old_id) != new_id):
        try:
            binance.cancel_algo_order(sym, old_id)
        except Exception:
            pass  # iki SL kalsa bile: biri tetiklenince pozisyon kapanır, diğeri no-op; reconcile temizler
    return True, new_id


def manage(cfg, binance, store, notifier=None, log=None):
    """Tüm açık işlemleri tara, gereken SL taşımalarını yap. Taşınanların listesini döndürür."""
    ex = cfg.get("exits", {}) or {}
    if not ex.get("trailing_enabled", False):
        return []
    min_move = ex.get("min_sl_move_pct", 0.05) / 100.0
    notify_moves = ex.get("notify_moves", True)
    moved = []

    for t in store.open_trades():
        side, entry = t["side"], t["entry"]
        sl_init = t["sl_init"] if t["sl_init"] is not None else t["sl"]
        try:
            price = binance.mark_price(t["symbol"])
        except Exception:
            continue
        r_now, r_unit = _r_now(side, entry, sl_init, price)
        if r_now is None:
            continue

        # high-water mark güncelle (SL taşınmasa bile peak ilerler)
        peak = t["peak_price"] if t["peak_price"] is not None else entry
        new_peak = max(peak, price) if side == "LONG" else min(peak, price)
        if new_peak != peak:
            store.update_trade_peak(t["id"], new_peak)
            peak = new_peak
        peak_r = (peak - entry) / r_unit if side == "LONG" else (entry - peak) / r_unit

        target, state = _desired_sl(ex, side, entry, peak, r_unit, peak_r)
        if target is None:
            continue
        target = binance.round_price(t["symbol"], target)
        cur_sl = t["sl"]
        if not _better(side, target, cur_sl, min_move):
            continue

        ok, new_id = _replace_sl(binance, t, target)
        if not ok:
            if log:
                log(f"   trailing {t['symbol']}: SL taşınamadı (yeni emir reddi) — eski SL korunuyor")
            continue
        prev_state = t["trail_state"] or "INIT"
        store.update_trade_sl(t["id"], target, sl_order_id=new_id, peak_price=peak, trail_state=state)
        moved.append((t["symbol"], state, cur_sl, target, peak_r))
        if log:
            tag = "🔒 breakeven" if state == "BE" else "⤴️ trailing"
            log(f"   {tag} {t['symbol']} {side}: SL {cur_sl} → {target} (tepe {peak_r:+.2f}R)")
        # Telegram: yalnız durum GEÇİŞLERİNDE (INIT→BE, →TRAIL) — her trail adımında değil (spam yok)
        if notifier and notify_moves and state != prev_state:
            icon = "🔒" if state == "BE" else "⤴️"
            notifier.send(f"{icon} <b>{t['symbol']} {side}</b> SL → {target}\n"
                          f"durum {state} | tepe {peak_r:+.2f}R | giriş {entry}")
    return moved
