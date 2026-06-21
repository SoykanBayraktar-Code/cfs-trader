"""position_manager — açık pozisyonları borsayla mutabık kılar, SL/TP çıkışlarını tespit eder.

dry_run/paper: çıkışı mark fiyatı SL/TP'yi geçti mi diye SİMÜLE eder.
live: pozisyon borsada kapandıysa, SL mi TP mi dolduğunu emir durumundan bulur, kalanı iptal eder.
"""
from . import executor


def reconcile(cfg, binance, store, notifier=None):
    """Tüm OPEN trade'leri kontrol et; kapananı CLOSED yap. Kapatılan işlem listesini döndürür."""
    closed = []
    open_trades = store.open_trades()
    if not open_trades:
        return closed

    if cfg.dry_run:
        for t in open_trades:
            mark = binance.mark_price(t["symbol"])
            hit = _simulate_exit(t, mark)
            if hit:
                reason, price = hit
                executor.flatten(cfg, binance, store, t, price, reason, notifier)
                closed.append((t["symbol"], reason))
        return closed

    # --- live ---
    ex_pos = {p["symbol"]: p for p in binance.positions()}
    for t in open_trades:
        sym = t["symbol"]
        if sym in ex_pos and abs(float(ex_pos[sym]["positionAmt"])) > 0:
            continue  # hâlâ açık
        # pozisyon kapanmış → SL/TP hangisi doldu?
        reason, price = _live_exit_price(binance, t)
        executor.flatten(cfg, binance, store, t, price, reason, notifier)
        try:
            binance.cancel_all(sym)  # kalan bracket emrini temizle
        except Exception:
            pass
        closed.append((sym, reason))
    return closed


def _simulate_exit(t, mark):
    """LONG: mark≤SL→SL, mark≥TP→TP. SHORT: tersi. (reason, price) | None."""
    if t["side"] == "LONG":
        if mark <= t["sl"]:
            return "SL", t["sl"]
        if t["tp"] and mark >= t["tp"]:
            return "TP", t["tp"]
    else:
        if mark >= t["sl"]:
            return "SL", t["sl"]
        if t["tp"] and mark <= t["tp"]:
            return "TP", t["tp"]
    return None


def _live_exit_price(binance, t):
    """Dolan algo emrini (SL/TP) bul → reason. Gerçek çıkış fiyatı son userTrade'den.

    Algo durumu tetiği belirler; fiyatı son kapanış trade'inden alırız (en doğru PnL).
    """
    reason = None
    for aid, why in ((t["sl_order_id"], "SL"), (t["tp_order_id"], "TP")):
        if not aid or str(aid) in ("None", "null") or str(aid).startswith("DRY"):
            continue
        try:
            o = binance.get_algo_order(t["symbol"], aid)
            if o.get("algoStatus") in ("FILLED", "TRIGGERED", "EXECUTED"):
                reason = why
                break
        except Exception:
            continue

    price = None
    try:
        trs = binance.user_trades(t["symbol"], limit=10)
        if trs:
            price = float(trs[-1]["price"])
    except Exception:
        pass
    if price is None:
        price = binance.mark_price(t["symbol"])

    if reason is None:  # algo durumu okunamadıysa fiyatı SL/TP'ye yakınlığına göre yorumla
        tp = t["tp"] or (price * 2)
        reason = "SL" if abs(price - t["sl"]) <= abs(price - tp) else "TP"
    return reason, price


def flatten_all(cfg, binance, store, reason, notifier=None):
    """Kill-switch: tüm açık pozisyonları kapat."""
    for t in store.open_trades():
        price = binance.mark_price(t["symbol"])
        executor.flatten(cfg, binance, store, t, price, reason, notifier)
