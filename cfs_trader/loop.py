"""loop — orkestratör. 15dk tarama tiki + döngü-arası pozisyon/exit izleme.

Tek servis (systemd): poll_seconds'ta bir reconcile (çıkış/kill-switch), loop_minutes'ta bir scan_tick.
"""
import time
import threading
from . import signals, risk, executor, position_manager, trailing
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
    # Bot HER ZAMAN sabit budget ($50) ile boyutlanır — kasadaki gerçek bakiye ($202 vb.) bot'u İLGİLENDİRMEZ.
    # "Kasa müsaitse işlem açabilsin": gerçek serbest margin kontrolü risk.gate'te AYRICA yapılır.
    return ctx.cfg.budget


def log(ctx, msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)


def scan_tick(ctx):
    """Bir tarama döngüsü: tara → tape → risk kapısı → (geçerse) gir. Döngüde max 1 pozisyon."""
    cfg = ctx.cfg
    day = _utcday()
    st = ctx.store.day_state(day)
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

    taped = 0
    for cand in cands:
        if ctx.store.open_count() >= cfg.risk["max_concurrent"]:
            break
        tres = None
        # tape kapısı (pahalı) — sadece gerekiyorsa ve sınırı aşmadan
        if cfg.signals.get("require_tape_confirm", True):
            if taped >= cfg.signals.get("max_tape_checks", MAX_TAPE_CHECKS):
                break
            taped += 1
            tres = signals.confirm_tape(cfg, cand, dur=22)
            log(ctx, f"   tape {cand.symbol} {cand.side} → {cand.tape_verdict} ({cand.tape_score:+.1f})")

        try:
            mark = ctx.binance.mark_price(cand.symbol)
        except Exception as e:
            log(ctx, f"   {cand.symbol} mark fiyat hatası: {e}")
            continue

        gr = risk.gate(cfg, ctx.store, ctx.binance, cand, equity, mark, day, ctx.learner)
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

        # brain — giriş öncesi ikinci göz (fail-safe; VETO girişi ENGELLER, asla giriş ZORLAMAZ)
        brain_allow = None   # (conf, why) — allow ise; shadow-metrik için girişten sonra loglanır
        try:
            from . import brain
            if brain._feat(ctx.cfg, "pretrade"):
                only_b = brain._sub(ctx.cfg, "pretrade").get("only_borderline", False)
                if (not only_b) or cand.tape_verdict != "CONFIRM":
                    dec, conf, why = brain.pretrade_review(ctx.cfg, cand, tape_raw=tres, store=ctx.store)
                    if dec == "veto":
                        ctx.store.log_decision(cand.symbol, cand.side, "REJECT", f"brain VETO ({conf}): {why}")
                        try: ctx.store.log_brain_decision("veto", conf, why, cand)   # shadow-metrik
                        except Exception: pass
                        log(ctx, f"   🧠 BRAIN VETO {cand.symbol} {cand.side}: {why}")
                        ctx.notifier.send(f"🧠 <b>Brain VETO</b> {cand.symbol} {cand.side}\n{why} (güven: {conf})")
                        continue
                    brain_allow = (conf, why)
                    log(ctx, f"   🧠 brain ALLOW {cand.symbol} {cand.side}: {why}")
        except Exception as e:
            log(ctx, f"   🧠 brain pretrade hata (giriş izinli): {e!r}")

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
