"""executor — onaylı adayı borsada açar: kaldıraç + MARKET giriş + STOP_MARKET SL + TAKE_PROFIT TP.

Her giriş ZORUNLU SL ile gelir (çıplak pozisyon yok). dry_run iken binance katmanı emir
göndermez (sentetik) — bu modül mantığı aynı çalışır, sadece gerçek emir kaçmaz.
"""


def _sides(direction):
    # LONG: giriş BUY, çıkış SELL. SHORT: tersi.
    return ("BUY", "SELL") if direction == "LONG" else ("SELL", "BUY")


def enter(cfg, binance, store, cand, sizing, mark, day):
    """Pozisyon aç + SL/TP bracket koy + store'a OPEN trade yaz. trade_id döndürür."""
    sym = cand.symbol
    entry_side, exit_side = _sides(cand.side)
    lev = cfg.risk["leverage"]

    # KESİNLİKLE ISOLATED — cross YASAK. Zaten isolated ise dokunma; cross/bilinmiyorsa isolated'a al.
    # İsolated'a alınamazsa hata yükselir → işlem AÇILMAZ, asla cross'a düşmez (kasa + INJ korunur).
    want = cfg.risk.get("margin_type", "ISOLATED")
    if not cfg.dry_run:
        cur = None
        try:
            cur = binance.margin_type_of(sym)
        except Exception:
            cur = None
        if (cur or "").lower() != want.lower():
            binance.set_margin_type(sym, want)
    binance.set_leverage(sym, lev)

    sl_price = binance.round_price(sym, cand.stop)
    tp_price = binance.round_price(sym, cand.tp) if cand.tp else None

    entry_order = binance.place_market(sym, entry_side, sizing.qty)
    sl_order = binance.place_stop_market(sym, exit_side, sl_price, close_position=True)
    tp_order = (binance.place_take_profit_market(sym, exit_side, tp_price, close_position=True)
                if tp_price else {"orderId": None})

    tid = store.open_trade(
        symbol=sym, side=cand.side, qty=sizing.qty, entry=mark, sl=sl_price, tp=tp_price,
        leverage=lev, risk_usdt=sizing.risk_usdt, regime=cand.regime,
        signal_type=cand.status, tape_verdict=cand.tape_verdict,
        mode=cfg.mode, dry_run=1 if cfg.dry_run else 0,
        entry_order_id=str(entry_order.get("orderId")),
        sl_order_id=str(sl_order.get("algoId") or sl_order.get("orderId")),
        tp_order_id=str(tp_order.get("algoId") or tp_order.get("orderId")),
        sl_init=sl_price, peak_price=mark, trail_state="INIT",   # Aşama 1: trailing başlangıç durumu
    )
    store.log_decision(sym, cand.side, "ENTER",
                       f"qty={sizing.qty} notional={sizing.notional} risk={sizing.risk_usdt} tape={cand.tape_verdict}",
                       {"entry": mark, "sl": sl_price, "tp": tp_price, "rr": cand.rr, "score": cand.score})
    return tid


def flatten(cfg, binance, store, trade, exit_price, reason, notifier=None):
    """Açık pozisyonu kapat (gerçekte market-close + bekleyen emirleri iptal) ve store'da CLOSED yap.

    PnL ve R hesaplar, günlük state'i günceller. exit_price: gerçekleşen/işaretli çıkış fiyatı.
    """
    sym = trade["symbol"]
    _, exit_side = _sides(trade["side"])

    # gerçek modda: bekleyen SL/TP iptal + pozisyonu reduceOnly market ile kapat (eğer hâlâ açıksa)
    if not cfg.dry_run and reason in ("MANUAL", "KILLSWITCH"):
        try:
            binance.place_market(sym, exit_side, trade["qty"], reduce_only=True)
        except Exception:
            pass
        try:
            binance.cancel_all(sym)
        except Exception:
            pass

    entry = trade["entry"]
    qty = trade["qty"]
    direction = 1 if trade["side"] == "LONG" else -1
    pnl = (exit_price - entry) * qty * direction
    risk = trade["risk_usdt"] or 0
    r_mult = (pnl / risk) if risk else 0.0
    # taker fee ~%0.05 round-trip yaklaşık
    fees = (entry + exit_price) * qty * 0.0005

    store.close_trade(trade["id"], exit_price, reason, round(pnl, 4), round(r_mult, 3), round(fees, 4))
    st = store.apply_close_to_day(_day_of(trade), pnl)
    store.update_learning(trade["regime"], trade["side"], trade["signal_type"], r_mult)

    if notifier:
        ic = "🟢" if pnl > 0 else "🔴"
        notifier.send(f"{ic} <b>{sym} {trade['side']}</b> {reason}\n"
                      f"PnL: {pnl:+.3f} USDT ({r_mult:+.2f}R) | fee~{fees:.3f}\n"
                      f"Gün: {st['realized_pnl']:+.2f} | ardışık-zarar: {st['consec_losses']}")
    return pnl, r_mult, st


def _day_of(trade):
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(trade["ts_open"]))
