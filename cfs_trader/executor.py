"""executor — onaylı adayı borsada açar: kaldıraç + MARKET giriş + STOP_MARKET SL + TAKE_PROFIT TP.

Her giriş ZORUNLU SL ile gelir (çıplak pozisyon yok). dry_run iken binance katmanı emir
göndermez (sentetik) — bu modül mantığı aynı çalışır, sadece gerçek emir kaçmaz.
"""


def _sides(direction):
    # LONG: giriş BUY, çıkış SELL. SHORT: tersi.
    return ("BUY", "SELL") if direction == "LONG" else ("SELL", "BUY")


def _emergency_close(cfg, binance, store, sym, exit_side, cand, err):
    # [INFO] ACİL KAPAT (Kritik #1 düzeltmesi): girişte SL borsaya YERLEŞEMEZSE, borsadaki GERÇEK pozisyonu okuyup
    # [INFO] reduceOnly-market ile kapatır + bekleyen emirleri iptal eder → çıplak pozisyon ASLA kalmaz. DB'ye OPEN
    # [INFO] YAZILMAZ; ABORT_NO_SL loglanır + Telegram uyarılır. (Eski davranış: SL fırlatınca çıplak+kayıtsız pozisyon kalıyordu.)
    closed = 0.0
    if not cfg.dry_run:
        try:
            for p in binance.positions(sym):
                amt = float(p["positionAmt"])
                if abs(amt) > 0:
                    binance.place_market(sym, exit_side, abs(amt), reduce_only=True)
                    closed = abs(amt)
        except Exception:
            pass
        try:
            binance.cancel_all(sym)   # yetim SL/TP kalmasın
        except Exception:
            pass
    try:
        store.log_decision(sym, cand.side, "ABORT_NO_SL",
                           f"SL güvenceye alınamadı → acil kapatıldı (amt={closed}): {err}")
    except Exception:
        pass
    try:
        from .notify import Notifier
        Notifier(cfg).send(f"⛔ <b>{sym} {cand.side}</b> giriş iptal — SL konamadı, "
                           f"pozisyon acil kapatıldı (çıplak bırakılmadı).\n{err}")
    except Exception:
        pass


def _acquire_fill(cfg, binance, sym, entry_side, exit_side, qty, mark, sl_price):
    """Giriş dolumunu al — MAKER-first (GTX post-only limit) + dolmazsa TAKER fallback (işlem ASLA kaçmaz).

    GÜVENLİK: İLK dolum algılandığında SL'yi HEMEN koyar → çıplak pozisyon penceresi yok.
    dry_run veya entry.maker_enabled=false → klasik MARKET (davranış birebir korunur).
    Döner: (filled_qty, avg_price, entry_order, sl_order|None, mode).
    """
    ecfg = cfg.get("entry", {}) or {}
    if cfg.dry_run or not ecfg.get("maker_enabled", False):
        o = binance.place_market(sym, entry_side, qty)
        return qty, mark, o, None, "taker"

    import time
    timeout = float(ecfg.get("maker_timeout_s", 10))
    poll = float(ecfg.get("maker_poll_s", 1.0))
    filled = 0.0
    avgpx = mark
    sl_order = None
    lo = None
    try:
        bt = binance.book_ticker(sym)
        # LONG → en iyi BID'e BUY (maker); SHORT → en iyi ASK'e SELL (maker). GTX = kesişirse reddedilir.
        px = float(bt["bidPrice"]) if entry_side == "BUY" else float(bt["askPrice"])
        px = binance.round_price(sym, px)
        lo = binance.place_limit(sym, entry_side, qty, px, tif="GTX")
        oid = lo.get("orderId")
        rejected = (oid is None) or (str(lo.get("status", "")).upper() in ("EXPIRED", "REJECTED"))
        deadline = time.time() + timeout
        while (not rejected) and oid is not None and time.time() < deadline:
            time.sleep(poll)
            od = binance.get_order(sym, oid)
            ex = float(od.get("executedQty", 0) or 0)
            if od.get("avgPrice") and float(od["avgPrice"]) > 0:
                avgpx = float(od["avgPrice"])
            if ex > filled:
                filled = ex
                if sl_order is None:   # İLK dolum → SL'yi HEMEN koy (çıplak pencere yok)
                    sl_order = binance.place_stop_market(sym, exit_side, sl_price, close_position=True)
            if str(od.get("status", "")).upper() == "FILLED":
                return filled, avgpx, lo, sl_order, "maker"
            if str(od.get("status", "")).upper() in ("CANCELED", "EXPIRED", "REJECTED"):
                break
        # timeout/iptal → kalan limiti iptal et, gerçekleşen dolumu oku
        if oid is not None:
            try:
                binance.cancel_order(sym, oid)
            except Exception:
                pass
            try:
                od = binance.get_order(sym, oid)
                filled = float(od.get("executedQty", 0) or 0)
                if filled > 0 and od.get("avgPrice") and float(od["avgPrice"]) > 0:
                    avgpx = float(od["avgPrice"])
            except Exception:
                pass
    except Exception:
        # maker yolu herhangi bir yerde patlarsa → tamamen taker'a düş (aşağıda)
        pass

    # TAKER top-up: kalan miktarı MARKET ile tamamla (tam boyut garantisi; işlem kaçmaz)
    remainder = binance.round_qty(sym, qty - filled)
    if remainder and remainder > 0:
        mo = binance.place_market(sym, entry_side, remainder)
        if sl_order is None:
            sl_order = binance.place_stop_market(sym, exit_side, sl_price, close_position=True)
        if filled > 0:
            avgpx = (avgpx * filled + mark * remainder) / (filled + remainder)
            return filled + remainder, avgpx, (lo or mo), sl_order, "maker+taker"
        return remainder, mark, mo, sl_order, "taker"
    # tamamı maker doldu (remainder ~0)
    return filled, avgpx, lo, sl_order, "maker"


def enter(cfg, binance, store, cand, sizing, mark, day):
    """Pozisyon aç + SL/TP bracket koy + store'a OPEN trade yaz. trade_id döndürür."""
    sym = cand.symbol
    entry_side, exit_side = _sides(cand.side)
    lev = getattr(cand, "leverage_used", None) or cfg.risk["leverage"]   # dinamik kaldıraç (risk.gate'te confidence'a göre)

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

    # [INFO] SL-GARANTİLİ GİRİŞ (Kritik #1): dolum + SL koymayı TEK korumalı blokta yapar. Herhangi bir adım (dolum ya da
    # [INFO] SL yerleştirme — örn. hızlı piyasada -2021 "would immediately trigger") hata verirse _emergency_close devreye
    # [INFO] girer, borsadaki gerçek pozisyonu kapatır ve None döner → ÇIPLAK pozisyon ASLA kalmaz, DB'ye OPEN yazılmaz.
    # Maker-first giriş (#2): GTX post-only limit (maker fee) + dolmazsa taker fallback (işlem KAÇMAZ). SL İLK dolumda konur.
    try:
        fill_qty, fill_px, entry_order, sl_order, entry_mode = _acquire_fill(
            cfg, binance, sym, entry_side, exit_side, sizing.qty, mark, sl_price)
        if sl_order is None:   # taker/dry yolu SL'yi koymadıysa şimdi koy (çıplak kalmaz)
            sl_order = binance.place_stop_market(sym, exit_side, sl_price, close_position=True)
    except Exception as e:
        _emergency_close(cfg, binance, store, sym, exit_side, cand, repr(e))
        return None

    # [INFO] TP EN-İYİ-ÇABA: TP koyamasak bile SL zaten borsada (pozisyon korumalı) + trailing 5R'yi yönetiyor →
    # [INFO] TP hatası girişi düşürmez/çıplak bırakmaz; TP'siz devam edilir.
    tp_order = {"orderId": None}
    if tp_price:
        try:
            tp_order = binance.place_take_profit_market(sym, exit_side, tp_price, close_position=True)
        except Exception:
            tp_order = {"orderId": None}

    tid = store.open_trade(
        symbol=sym, side=cand.side, qty=fill_qty, entry=fill_px, sl=sl_price, tp=tp_price,
        leverage=lev, risk_usdt=sizing.risk_usdt, regime=cand.regime,
        signal_type=cand.status, tape_verdict=cand.tape_verdict,
        mode=cfg.mode, dry_run=1 if cfg.dry_run else 0,
        entry_order_id=str(entry_order.get("orderId")),
        sl_order_id=str(sl_order.get("algoId") or sl_order.get("orderId")),
        tp_order_id=str(tp_order.get("algoId") or tp_order.get("orderId")),
        sl_init=sl_price, peak_price=fill_px, trail_state="INIT",   # Aşama 1: trailing başlangıç durumu
        liq_pull=getattr(cand, "liq_pull", 0.0), context_tilt=getattr(cand, "context_tilt", 1.0),  # bağlam ölçümü
        brain_conviction=getattr(cand, "brain_conviction", None),  # Faz1 M2 SHADOW: Claude konviksiyon/boyut ölçümü
        brain_size_hint=getattr(cand, "brain_size_hint", None),
        sizing_confidence=getattr(cand, "sizing_confidence", None),  # dinamik boyut: kazanç-ihtimali + kullanılan risk%
        risk_pct_used=getattr(cand, "risk_pct_used", None),
        cz_snapshot=getattr(cand, "cz_snapshot", None),  # coinalyze snapshot (SHADOW)
        ls_bias=getattr(cand, "ls_bias", 0.0),
        ls_tilt=getattr(cand, "ls_tilt", 1.0),
        ls_snapshot=getattr(cand, "ls_snapshot", None),
        sq_tilt=getattr(cand, "sq_tilt", 1.0),
        cvd15=getattr(cand, "cvd15", None), cvd30=getattr(cand, "cvd30", None),  # Faz 0 SHADOW
        cvd60=getattr(cand, "cvd60", None), funding=getattr(cand, "funding", None),
        basis_bps=getattr(cand, "basis_bps", None), flow_regime=getattr(cand, "flow_regime", None),
        confluence=getattr(cand, "confluence", None),
        squeeze_pct=getattr(cand, "squeeze_pct", None), atr_contraction=getattr(cand, "atr_contraction", None),  # Faz 1 SHADOW scalp
        oi_trend=getattr(cand, "oi_trend", None), cvd_divergence=getattr(cand, "cvd_divergence", None),
        book_asym=getattr(cand, "book_asym", None), vol_surge=getattr(cand, "vol_surge", None),
        range_pos=getattr(cand, "range_pos", None), scalp_score=getattr(cand, "scalp_score", None),
        derivs_bias=getattr(cand, "derivs_bias", None), derivs_score=getattr(cand, "derivs_score", None),  # SHADOW
        derivs_snapshot=getattr(cand, "derivs_snapshot", None),
        pnd_phase=getattr(cand, "pnd_phase", None), pnd_score=getattr(cand, "pnd_score", None),  # SHADOW (P&D)
        pnd_snapshot=getattr(cand, "pnd_snapshot", None),
    )
    store.log_decision(sym, cand.side, "ENTER",
                       f"qty={fill_qty} notional={sizing.notional} risk={sizing.risk_usdt} tape={cand.tape_verdict} giriş={entry_mode}",
                       {"entry": fill_px, "sl": sl_price, "tp": tp_price, "rr": cand.rr, "score": cand.score, "fill": entry_mode})

    # Notion İşlem Kayıt Defteri — giriş satırı (Sonuç=AÇIK). FAIL-SAFE: hata trade'i ETKİLEMEZ.
    try:
        from . import notion
        if notion.enabled(cfg):
            pid = notion.log_entry(cfg, store.get_trade(tid))
            if pid:
                store.set_notion_page_id(tid, pid)
    except Exception:
        pass
    return tid


def _realized_from_income(binance, sym, since_ts, tries=3, sleep_s=1.0):
    # [INFO] AUDIT #4 ground-truth: borsa income kayıtlarından (GET /fapi/v1/income) işlemin GERÇEK net'ini hesaplar.
    # [INFO] net = REALIZED_PNL + COMMISSION + FUNDING_FEE (hepsi işaretli; comm/funding negatif=maliyet). Kapanış kaydı
    # [INFO] (REALIZED_PNL) borsada hemen oluşmayabilir → has_close False ise kısa retry. Döner:
    # [INFO] (net, gross_realized, commission, funding, has_close) | None (hiç kayıt yoksa).
    import time as _t
    start_ms = int(since_ts * 1000) - 2000   # küçük tampon (giriş komisyonunu da kapsa)
    inc = None
    for i in range(tries):
        try:
            inc = binance.income(sym, start_ms=start_ms, limit=200)
        except Exception:
            inc = None
        has_close = bool(inc) and any(x.get("incomeType") == "REALIZED_PNL" for x in inc)
        if has_close or i == tries - 1:
            break
        _t.sleep(sleep_s)
    if not inc:
        return None
    g = lambda t: sum(float(x.get("income", 0) or 0) for x in inc if x.get("incomeType") == t)
    realized, comm, fund = g("REALIZED_PNL"), g("COMMISSION"), g("FUNDING_FEE")
    has_close = any(x.get("incomeType") == "REALIZED_PNL" for x in inc)
    return (realized + comm + fund, realized, comm, fund, has_close)


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
    # [INFO] TAHMİNİ PnL (fallback): fiyat-tabanlı, sabit ~%0.05 taker fee, FUNDING YOK. Yalnız income okunamazsa kullanılır.
    est_pnl = (exit_price - entry) * qty * direction
    est_fees = (entry + exit_price) * qty * 0.0005
    pnl, fees, funding, pnl_src = est_pnl, est_fees, None, "tahmin"

    # [INFO] GROUND-TRUTH PnL (AUDIT #4): canlıda borsadan GERÇEK net realize'i oku — REALIZED_PNL + COMMISSION +
    # [INFO] FUNDING_FEE (funding DAHİL). Kapanış kaydı (REALIZED_PNL) hazırsa onu KULLAN (kesin); değilse tahmine düş.
    # [INFO] learner/analyst/brain bundan sonra GERÇEK net R üzerinde öğrenir (funding-yoksay zehiri temizlenir).
    if not cfg.dry_run:
        try:
            res = _realized_from_income(binance, sym, trade["ts_open"])
            if res and res[4]:                 # has_close → REALIZED_PNL kaydı var (kapanış işlendi)
                net, _gross, comm, fund, _ = res
                pnl, fees, funding, pnl_src = net, abs(comm), fund, "income"   # comm negatif → pozitif fee gösterimi
        except Exception:
            pass

    risk = trade["risk_usdt"] or 0
    r_mult = (pnl / risk) if risk else 0.0

    store.close_trade(trade["id"], exit_price, reason, round(pnl, 4), round(r_mult, 3), round(fees, 4),
                      funding_usdt=(round(funding, 4) if funding is not None else None), pnl_src=pnl_src)
    st = store.apply_close_to_day(_day_of(trade), pnl)
    store.update_learning(trade["regime"], trade["side"], trade["signal_type"], r_mult)

    if notifier:
        ic = "🟢" if pnl > 0 else "🔴"
        fund_txt = f" | funding {funding:+.3f}" if funding is not None else ""
        src_txt = "✓gerçek" if pnl_src == "income" else "~tahmin"
        notifier.send(f"{ic} <b>{sym} {trade['side']}</b> {reason}\n"
                      f"PnL: {pnl:+.3f} USDT ({r_mult:+.2f}R) [{src_txt}] | fee {fees:.3f}{fund_txt}\n"
                      f"Gün: {st['realized_pnl']:+.2f} | ardışık-zarar: {st['consec_losses']}")

    # brain — işlem-sonrası post-mortem (fail-safe; DB'ye not yazar, emir ATMAZ)
    # item 6: losses_only → sadece kayıplara claude çağrısı (kazançlarda not yok; kontansiyon ↓)
    try:
        from . import brain
        _pm = brain._sub(cfg, "postmortem")
        if brain._feat(cfg, "postmortem") and not (_pm.get("losses_only", False) and pnl >= 0):
            note, tag = brain.postmortem(cfg, trade, exit_price, reason, pnl, r_mult)
            if note:
                store.set_trade_note(trade["id"], (f"[{tag}] {note}" if tag else note))
                if notifier and brain._sub(cfg, "postmortem").get("notify", False):
                    notifier.send(f"🧠 <i>{sym}: {note}</i>")
    except Exception:
        pass

    # Notion İşlem Kayıt Defteri — kapanışta satırı güncelle (Sonuç=KAZANÇ/KAYIP + çıkış + Brain Notu).
    # postmortem'den SONRA: llm_note dahil olur. FAIL-SAFE: hata kapanışı ETKİLEMEZ.
    try:
        from . import notion
        if notion.enabled(cfg):
            row = store.get_trade(trade["id"])
            pid = row["notion_page_id"] if (row is not None and "notion_page_id" in row.keys()) else None
            notion.log_exit(cfg, row, pid)
    except Exception:
        pass
    return pnl, r_mult, st


def _day_of(trade):
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(trade["ts_open"]))
