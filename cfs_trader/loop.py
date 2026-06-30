"""loop — orkestratör. 15dk tarama tiki + döngü-arası pozisyon/exit izleme.

Tek servis (systemd): poll_seconds'ta bir reconcile (çıkış/kill-switch), loop_minutes'ta bir scan_tick.
"""
import time
import threading
from . import signals, risk, executor, position_manager, trailing, cz, ls
from .cfg import get as get_cfg
from .binance import Binance
from .store import Store
from .notify import Notifier
from .learner import Learner
from .lock import cross_lock

MAX_TAPE_CHECKS = 3   # tik başına en fazla kaç adayı 22s tape'den geçir (maliyet sınırı)


class Ctx:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_cfg()
        self.binance = Binance(self.cfg)
        self.store = Store(self.cfg.db_path)
        self.notifier = Notifier(self.cfg)
        self.learner = Learner(self.cfg, self.store)
        self.lock = threading.Lock()   # scan_tick + Telegram komutlarını serialize eder (yarış yok)
        try:
            self.binance.sync_time()
        except Exception:
            pass


def _utcday():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _equity(ctx):
    # CANLI KASA: kazandıkça büyür/kaybettikçe küçülür (bileşik boyutlama). dry_run/hata → budget fallback.
    # equity_cap ile fat-finger yatırıma karşı tavan; gerçek serbest-margin kontrolü risk.gate'te AYRICA.
    cfg = ctx.cfg
    sz = cfg.get("sizing", {}) or {}
    if cfg.dry_run or not sz.get("live_equity", True):
        return cfg.budget
    try:
        wb = ctx.binance.wallet_balance()
        if wb and wb > 0:
            cap = sz.get("equity_cap")
            return min(wb, float(cap)) if cap else wb
    except Exception as e:
        log(ctx, f"[equity] canlı kasa okunamadı ({e!r}) → budget {cfg.budget}")
    return cfg.budget


def log(ctx, msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)


def _scalp_entry_ok(cfg, cand):
    """PATLAMADAN HEMEN ÖNCE giriş (config-gated): aşırı sıkışma + kırılma kenarına dayanma + yön teyidi
    + HENÜZ patlamamış (vol_surge yüksek DEĞİL) + tape≠VETO. True → gate'te yalnız tape-CONFIRM gevşer;
    VETO ve diğer TÜM emniyetler aynen. NOT: erken giriş → kazanç-oranı düşer, R yükselir (eyes-open)."""
    sc = (cfg.signals.get("scalp_entry") or {})
    if not sc.get("enabled", False):
        return False
    if getattr(cand, "tape_verdict", "") == "VETO":
        return False
    side = (cand.side or "").upper()
    sq = getattr(cand, "squeeze_pct", None)
    atrc = getattr(cand, "atr_contraction", None)
    rpos = getattr(cand, "range_pos", None)
    ba = getattr(cand, "book_asym", None) or 0.0
    oi = getattr(cand, "oi_trend", None) or 0.0
    vs = getattr(cand, "vol_surge", None)
    # 1) YAY GERİLMİŞ: aşırı sıkışma + ATR daralma
    if sq is None or sq > sc.get("max_squeeze_pct", 12):
        return False
    if atrc is None or atrc > sc.get("max_atr_contraction", 0.9):
        return False
    # 2) KIRILMA KENARINA DAYANMIŞ (yönde): LONG tepe (rpos≥edge), SHORT dip (rpos≤1-edge)
    if rpos is None:
        return False
    edge = sc.get("edge_pct", 0.70)
    if side == "LONG" and rpos < edge:
        return False
    if side == "SHORT" and rpos > (1.0 - edge):
        return False
    # 3) YÖN TEYİDİ: defter vakumu YA DA OI yüklenmesi yönle uyumlu
    dba = sc.get("min_book_asym", 0.10)
    dir_ok = ((side == "LONG" and (ba >= dba or oi >= sc.get("min_oi_trend", 0.5))) or
              (side == "SHORT" and (ba <= -dba or oi >= sc.get("min_oi_trend", 0.5))))
    if not dir_ok:
        return False
    # 4) HENÜZ PATLAMAMIŞ: hacim aşırı sürmüşse (>max) GEÇ kaldık — alma (veri yoksa diğer sinyaller taşır)
    if vs is not None and vs > sc.get("max_vol_surge", 1.8):
        return False
    return True


def scan_tick(ctx):
    """Bir tarama döngüsü: tara → tape → risk kapısı → (geçerse) gir. Döngüde max 1 pozisyon."""
    cfg = ctx.cfg
    day = _utcday()
    st = ctx.store.day_state(day)
    # [INFO] KALICI DURAKLATMA (07-01): config signals.trading_paused=true iken YENİ giriş açılmaz (audit süreci
    # [INFO] boyunca kullanıcı isteğiyle). halt_day'in aksine gün-dönümünde SIFIRLANMAZ + restart'a dayanıklı.
    # [INFO] poll_tick ETKİLENMEZ → açık pozisyonlar (trailing/SL/TP/reconcile) yönetilmeye DEVAM eder.
    if cfg.signals.get("trading_paused", False):
        log(ctx, "[tik] TRADING DURAKLATILDI (config trading_paused) — yeni giriş yok; açık pozisyonlar yönetiliyor")
        return
    if st["halted"]:
        log(ctx, f"[tik] gün durduruldu ({st['halt_reason']}) — tarama atlandı")
        return
    # brain — risk gözcüsü (deterministik flag + rate-limited LLM uyarısı; fail-safe, emir atmaz)
    try:
        from . import brain
        if brain._feat(ctx.cfg, "guardian"):
            brain.risk_guardian(ctx.cfg, ctx.store, ctx.notifier, log=lambda m: log(ctx, m))
    except Exception as e:
        log(ctx, f"[tik] brain guardian hata (yok sayıldı): {e!r}")
    if ctx.store.open_count() >= cfg.risk["max_concurrent"]:
        log(ctx, "[tik] kapasite dolu — tarama atlandı")
        return

    equity = _equity(ctx)
    regime, cands = signals.scan_all(cfg)
    by_src = {}
    for c in cands:
        by_src[c.status] = by_src.get(c.status, 0) + 1
    src_txt = " ".join(f"{k}:{v}" for k, v in by_src.items()) or "yok"
    log(ctx, f"[tik] rejim {regime['regime']} ({regime['bias']}) | {len(cands)} aday ({src_txt}) | eşik tape={cfg.signals.get('require_tape_confirm')}")

    # PARALEL TAPE (Faz 1): değerlendirilecek adayları TEK chdir altında EŞZAMANLI tape'le (66s→~22s).
    # Davranış korunur: tape gerekiyorsa yalnız ilk max_tape_checks aday değerlendirilir (eski sınır).
    _require_tape = cfg.signals.get("require_tape_confirm", True)
    _max_tape = cfg.signals.get("max_tape_checks", MAX_TAPE_CHECKS)
    head = cands[:_max_tape] if _require_tape else cands
    tape_raw = {}
    if _require_tape and head:
        tape_raw = signals.confirm_tape_batch(cfg, head, dur=22)
        for _c in head:
            log(ctx, f"   tape {_c.symbol} {_c.side} → {_c.tape_verdict} ({_c.tape_score:+.1f})")

    for cand in head:
        if ctx.store.open_count() >= cfg.risk["max_concurrent"]:
            break
        tres = tape_raw.get(cand.symbol)   # paralel tape'in ham sonucu (brain için)

        try:
            mark = ctx.binance.mark_price(cand.symbol)
        except Exception as e:
            log(ctx, f"   {cand.symbol} mark fiyat hatası: {e}")
            continue

        # bağlam — liq_pull yumuşak sizing-tilt (FAIL-SAFE; ≤1.0 → boyutu küçültebilir, cap'i ASLA aşmaz)
        try:
            ct, lp = signals.context_tilt(cfg, cand)
            cand.context_tilt = ct
            cand.liq_pull = lp if lp is not None else 0.0
            if ct < 1.0:
                log(ctx, f"   📐 bağlam {cand.symbol} {cand.side}: liq_pull={lp} → tilt={ct} (boyut küçültüldü)")
        except Exception as e:
            log(ctx, f"   📐 bağlam tilt hata (yok sayıldı): {e!r}")

        # coinalyze capraz-borsa snapshot (SHADOW: trade satirina, KARARA DOKUNMAZ; fail-safe)
        cand.cz_snapshot = cz.snapshot(cand.symbol)

        # top-trader L/S kalabalik-kontrarian yumusak tilt (SHADOW + boyut; FAIL-SAFE)
        try:
            lt, lb, lsnap = signals.ls_tilt(cfg, cand)
            cand.ls_tilt = lt
            cand.ls_bias = lb if lb is not None else 0.0
            cand.ls_snapshot = ls.snapshot_json(lsnap) if lsnap else None
            if lt < 1.0:
                log(ctx, f"   L/S {cand.symbol} {cand.side}: bias={lb} -> tilt={lt} (boyut kucultuldu)")
        except Exception as e:
            log(ctx, f"   L/S tilt hata (yok sayildi): {e!r}")

        # coinalyze squeeze-farkindalik yumusak tilt (cz_snapshot'tan; reduce-only; FAIL-SAFE)
        try:
            cand.sq_tilt = signals.squeeze_tilt(cfg, cand)
            if cand.sq_tilt < 1.0:
                log(ctx, f"   squeeze {cand.symbol} {cand.side}: tilt={cand.sq_tilt} (karsi-squeeze, boyut kucultuldu)")
        except Exception as e:
            cand.sq_tilt = 1.0
            log(ctx, f"   squeeze tilt hata (yok sayildi): {e!r}")

        # Faz 0/1 SHADOW: ek özellikler + scalp bağlamları — gate'ten ÖNCE (korumalı scalp yolu scalp_score/vol_surge'e bakar)
        try:
            from . import shadow
            for _k, _v in shadow.compute(cfg, cand).items():
                setattr(cand, _k, _v)
        except Exception as e:
            log(ctx, f"   shadow hesap hata (yok sayıldı): {e!r}")

        # derivs türev-confluence (SHADOW: cz/ls/oi/F&G'yi yönle kıyaslar → derivs_bias/score logla;
        # KARARA/BOYUTA DOKUNMAZ; fail-safe). Tüm girdiler (cz_snapshot/ls_bias/oi_trend) yukarıda hazır.
        try:
            _dc = cfg.get("derivs", {}) or {}
            if _dc.get("shadow", False) or _dc.get("enabled", False):
                from . import derivs_ctx
                _db, _dsc, _dsnap = derivs_ctx.evaluate(cfg, cand)
                cand.derivs_bias = _db; cand.derivs_score = _dsc; cand.derivs_snapshot = _dsnap
                if _db not in ("NEUTRAL", None):
                    log(ctx, f"   🧮 derivs {cand.symbol} {cand.side}: {_db} ({_dsc:+.2f}) [SHADOW]")
        except Exception as e:
            log(ctx, f"   derivs hata (yok sayıldı): {e!r}")

        # P&D faz tespiti (SHADOW: rush-order spike → pump/dump fazı logla; KARARA/BOYUTA DOKUNMAZ; fail-safe)
        try:
            _pc = cfg.get("pnd", {}) or {}
            if _pc.get("shadow", False) or _pc.get("enabled", False):
                from . import pnd_ctx
                _pp, _psc, _psnap = pnd_ctx.evaluate(cfg, cand)
                cand.pnd_phase = _pp; cand.pnd_score = _psc; cand.pnd_snapshot = _psnap
                if _pp not in ("NONE", None):
                    log(ctx, f"   🚨 P&D {cand.symbol}: {_pp} (z={_psc:+.1f}) [SHADOW]")
        except Exception as e:
            log(ctx, f"   pnd hata (yok sayıldı): {e!r}")

        # Korumalı scalp giriş yolu: coiled+yüklenmiş+ATEŞLEYEN setup tape-CONFIRM şartını gevşetir (VETO + diğer emniyetler aynen)
        scalp_ok = _scalp_entry_ok(cfg, cand)

        gr = risk.gate(cfg, ctx.store, ctx.binance, cand, equity, mark, day, ctx.learner, scalp_ok=scalp_ok)
        if not gr.ok:
            ctx.store.log_decision(cand.symbol, cand.side, "REJECT", gr.reason)
            log(ctx, f"   ⨯ {cand.symbol} {cand.side} RED: {gr.reason}")
            if gr.halt:
                position_manager.flatten_all(cfg, ctx.binance, ctx.store, "KILLSWITCH", ctx.notifier)
                ctx.store.halt_day(day, gr.reason)
                ctx.notifier.send(f"⛔ <b>KILL-SWITCH</b> — {gr.reason}. Bot bugünlük durdu.")
                log(ctx, f"[tik] ⛔ KILL-SWITCH: {gr.reason}")
                return
            continue

        # scalp-yolu işareti: CONFIRM olmadan girdiyse COIL-BREAKOUT olarak logla (performansı Notion'da ayrı ölçülür)
        if scalp_ok and cand.tape_verdict != "CONFIRM":
            cand.status = "COIL-BREAKOUT"
            log(ctx, f"   📈 SCALP-GİRİŞ {cand.symbol} {cand.side}: scalp={getattr(cand, 'scalp_score', None)}/6 "
                     f"vol_surge={getattr(cand, 'vol_surge', None)} tape={cand.tape_verdict} (CONFIRM gevşetildi)")

        # brain — giriş öncesi ikinci göz (fail-safe; VETO girişi ENGELLER, asla giriş ZORLAMAZ)
        brain_allow = None   # (conf, why) — allow ise; shadow-metrik için girişten sonra loglanır
        try:
            from . import brain
            if brain._feat(ctx.cfg, "pretrade"):
                only_b = brain._sub(ctx.cfg, "pretrade").get("only_borderline", False)
                if (not only_b) or cand.tape_verdict != "CONFIRM":
                    dec, conv, size_hint, why = brain.pretrade_review(ctx.cfg, cand, tape_raw=tres, store=ctx.store)
                    cand.brain_conviction = conv          # SHADOW: kaydedilir, henüz sizing'e UYGULANMAZ
                    cand.brain_size_hint = size_hint
                    if dec == "veto":
                        ctx.store.log_decision(cand.symbol, cand.side, "REJECT", f"brain VETO (k={conv}): {why}")
                        try: ctx.store.log_brain_decision("veto", str(conv), why, cand)   # shadow-metrik
                        except Exception: pass
                        log(ctx, f"   🧠 BRAIN VETO {cand.symbol} {cand.side}: {why}")
                        ctx.notifier.send(f"🧠 <b>Brain VETO</b> {cand.symbol} {cand.side}\n{why} (konviksiyon: {conv})")
                        continue
                    brain_allow = (str(conv), why)
                    log(ctx, f"   🧠 brain ALLOW {cand.symbol} {cand.side}: konv={conv} size_hint={size_hint} — {why}")
        except Exception as e:
            log(ctx, f"   🧠 brain pretrade hata (giriş izinli): {e!r}")

        # Defter-tabanlı TP (v1): sabit 5R yerine emir-defteri direncine göre gerçekçi TP (SL/risk DEĞİŞMEZ; None→5R).
        try:
            from . import booktp
            _btp = booktp.compute(cfg, cand.symbol, cand.side, mark, cand.stop, mark)
            if _btp:
                _old = cand.tp
                cand.tp = _btp
                _risk = abs(mark - cand.stop)
                cand.rr = round(abs(_btp - mark) / _risk, 2) if _risk > 0 else cand.rr
                log(ctx, f"   📖 defter-TP {cand.symbol}: {_old} → {round(_btp, 8)} ({cand.rr}R)")
        except Exception as e:
            log(ctx, f"   defter-TP hata (5R korunur): {e!r}")

        tid = executor.enter(cfg, ctx.binance, ctx.store, cand, gr.sizing, mark, day)
        if brain_allow is not None:
            try: ctx.store.log_brain_decision("allow", brain_allow[0], brain_allow[1], cand, trade_id=tid)
            except Exception: pass
        ctx.notifier.send(
            f"🟢 <b>GİRİŞ {cand.symbol} {cand.side}</b> (#{tid})\n"
            f"entry~{mark} | SL {cand.stop} | TP {cand.tp}\n"
            f"qty {gr.sizing.qty} | notional {gr.sizing.notional} | risk {gr.sizing.risk_usdt} USDT\n"
            f"tape {cand.tape_verdict} | RR {cand.rr} | {'DRY' if cfg.dry_run else cfg.mode}")
        log(ctx, f"   ✓ GİRİŞ {cand.symbol} {cand.side} #{tid} qty={gr.sizing.qty} risk={gr.sizing.risk_usdt}")
        break  # max_concurrent=1


def poll_tick(ctx):
    """Açık pozisyonları yönet (Aşama 1: breakeven/trailing SL) → sonra çıkışları kontrol et."""
    # 1) trailing/breakeven: SL'leri lehte taşı (reconcile'dan ÖNCE — güncel SL ile çıkış değerlendirilsin)
    try:
        trailing.manage(ctx.cfg, ctx.binance, ctx.store, ctx.notifier, log=lambda m: log(ctx, m))
    except Exception as e:
        log(ctx, f"[poll] trailing hatası: {e!r}")
    # 2) çıkış tespiti (SL/TP doldu mu)
    closed = position_manager.reconcile(ctx.cfg, ctx.binance, ctx.store, ctx.notifier)
    for sym, reason in closed:
        log(ctx, f"[poll] çıkış {sym} → {reason}")
    # 3) SL-WATCHDOG (AUDIT #5): hâlâ açık pozisyonda borsada koruyucu SL var mı; yoksa yeniden koy (çıplak kalma)
    try:
        position_manager.ensure_protective_sl(ctx.cfg, ctx.binance, ctx.store, ctx.notifier, log=lambda m: log(ctx, m))
    except Exception as e:
        log(ctx, f"[poll] SL-watchdog hatası: {e!r}")


def run_forever(ctx=None):
    ctx = ctx or Ctx()
    cfg = ctx.cfg
    loop_s = cfg.signals["loop_minutes"] * 60
    poll_s = cfg.signals["poll_seconds"]
    log(ctx, f"=== cfs-trader başladı | mode={cfg.mode} dry_run={cfg.dry_run} budget={cfg.budget} "
             f"lev={cfg.risk['leverage']}x | tarama {cfg.signals['loop_minutes']}dk poll {poll_s}s ===")
    ctx.notifier.send(f"▶️ cfs-trader başladı ({cfg.mode}{'/DRY' if cfg.dry_run else ''}) "
                      f"budget {cfg.budget} {cfg.risk['leverage']}x")
    # Telegram komut dinleyici (ayrı daemon thread — /tara /islem /durum /dur /devam)
    from . import commands
    threading.Thread(target=commands.run_listener, args=(ctx,), daemon=True).start()

    # WS kline deposu (Faz 1-A) — enabled ise başlat; serve ise motoru depodan besle (REST fallback).
    # FAIL-SAFE: herhangi bir hata → bot REST ile çalışmaya DEVAM eder.
    ctx.wsklines = None
    _wk = cfg.get("wsklines", {}) or {}
    if _wk.get("enabled"):
        try:
            from . import wsklines
            _syms = wsklines.liquid_universe(cfg, _wk.get("min_vol", 10_000_000))
            if _syms:
                ctx.wsklines = wsklines.KlineStore(_syms, backfill_rate=_wk.get("backfill_rate", 5),
                                                   log=lambda m: log(ctx, m)).start()
                log(ctx, f"[wsklines] depo başladı: {len(_syms)} sembol × 4 TF (serve={_wk.get('serve', False)})")
                if _wk.get("serve"):
                    wsklines.install_serve(cfg, ctx.wsklines, log=lambda m: log(ctx, m))
            else:
                log(ctx, "[wsklines] evren boş — depo başlatılmadı (REST sürüyor)")
        except Exception as e:
            log(ctx, f"[wsklines] başlatma hatası (yok sayıldı, REST sürüyor): {e!r}")

    last_scan = 0.0
    while True:
        try:
            with ctx.lock, cross_lock(cfg):      # komutlarla + süreçler-arası serialize
                poll_tick(ctx)
                if time.time() - last_scan >= loop_s:
                    scan_tick(ctx)
                    last_scan = time.time()
        except Exception as e:
            log(ctx, f"[HATA] döngü: {e!r}")
        time.sleep(poll_s)
