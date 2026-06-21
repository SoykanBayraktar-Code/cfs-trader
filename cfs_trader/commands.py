"""commands — Telegram komut dinleyici. Bot'u uzaktan kumanda: tara / işlem aç / durum / dur-devam.

Ayrı thread'de Telegram long-polling yapar. YALNIZCA yetkili chat_id'den komut kabul eder.
Tarama/giriş işlemleri ctx.lock ile ana döngünün scan_tick'iyle serialize edilir (yarış yok).
"""
import time
import requests

POLL_TIMEOUT = 25

HELP = (
    "🤖 <b>cfs-trader komutları</b>\n"
    "/tara — tara + tape, adayları göster (işlem AÇMAZ)\n"
    "/islem — tara + CONFIRM varsa GERÇEK işlem aç\n"
    "/durum — bakiye, açık pozisyon, günlük PnL\n"
    "/dur — işlem açmayı duraklat\n"
    "/devam — duraklatmayı kaldır\n"
    "/yardim — bu liste"
)


def _get_updates(token, offset):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": POLL_TIMEOUT}, timeout=POLL_TIMEOUT + 10)
        return r.json().get("result", [])
    except Exception:
        return []


def run_listener(ctx):
    """Telegram komut dinleyici (daemon thread). Token/chat_id yoksa sessiz çıkar."""
    token, chat_id = ctx.cfg.telegram()
    if not (token and chat_id):
        return
    chat_id = str(chat_id)
    # başlangıçta birikmiş eski mesajları atla (sadece bundan sonrakileri işle)
    offset = 0
    init = _get_updates(token, 0)
    if init:
        offset = init[-1]["update_id"] + 1
    ctx.notifier.send("🤖 Komut dinleyici aktif. /yardim")
    while True:
        for u in _get_updates(token, offset):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != chat_id:
                continue  # YALNIZCA sahibinden
            text = (msg.get("text") or "").strip().lower().split("@")[0]  # /tara@bot -> /tara
            try:
                _dispatch(ctx, text)
            except Exception as e:
                ctx.notifier.send(f"⚠️ Komut hatası: {e!r}")
        time.sleep(1)


def _dispatch(ctx, text):
    if text in ("/yardim", "/help", "/start"):
        ctx.notifier.send(HELP)
    elif text in ("/durum", "/status"):
        _durum(ctx)
    elif text in ("/tara", "/scan"):
        _tara(ctx, enter=False)
    elif text in ("/islem", "/trade"):
        _tara(ctx, enter=True)
    elif text in ("/dur", "/pause"):
        from .loop import _utcday
        ctx.store.halt_day(_utcday(), "manuel /dur")
        ctx.notifier.send("⏸️ İşlem açma duraklatıldı. /devam ile aç.")
    elif text in ("/devam", "/resume"):
        from .loop import _utcday
        ctx.store.db.execute("UPDATE daily_state SET halted=0, halt_reason=NULL WHERE day=?", (_utcday(),))
        ctx.store.db.commit()
        ctx.notifier.send("▶️ İşlem açma devam ediyor.")
    elif text.startswith("/"):
        ctx.notifier.send("Bilinmeyen komut. /yardim")


def _durum(ctx):
    cfg = ctx.cfg
    from .loop import _utcday
    st = ctx.store.day_state(_utcday())
    try:
        bal = round(ctx.binance.available_usdt(), 2) if not cfg.dry_run else cfg.budget
    except Exception:
        bal = "?"
    opens = ctx.store.open_trades()
    lines = ["📊 <b>cfs-trader durum</b>",
             f"{cfg.mode} | budget {cfg.budget} {cfg.risk['leverage']}x | serbest {bal}",
             f"gün PnL {st['realized_pnl']:+.2f} | işlem {st['trades_count']} | "
             f"ardışık-zarar {st['consec_losses']} | halted {bool(st['halted'])}",
             f"açık pozisyon: {len(opens)}"]
    for t in opens:
        lines.append(f" • {t['symbol']} {t['side']} entry {t['entry']} SL {t['sl']} TP {t['tp']}")
    ctx.notifier.send("\n".join(lines))


def _tara(ctx, enter):
    """Tara + ilk N adayı tape'le. enter=True ise CONFIRM+gate geçen ilk adayda GERÇEK giriş."""
    from . import signals, risk, executor
    from .loop import _utcday, _equity
    cfg = ctx.cfg
    with ctx.lock:                       # ana döngü scan_tick'iyle serialize
        ctx.notifier.send("🔍 Tarama başladı… (~1-2 dk, derin tape)")
        regime, cands = signals.scan(cfg)
        n = cfg.signals.get("max_tape_checks", 6)
        lines = [f"📡 Rejim {regime['regime']} ({regime['bias']}) | {len(cands)} aday | ilk {min(n, len(cands))} tape"]
        if not cands:
            ctx.notifier.send("Aday yok."); return
        day = _utcday(); equity = _equity(ctx)
        entered = False
        for cand in cands[:n]:
            signals.confirm_tape(cfg, cand, dur=22)
            line = f"{cand.symbol} {cand.side}: tape {cand.tape_verdict} ({cand.tape_score:+.1f})"
            if enter:
                try:
                    mark = ctx.binance.mark_price(cand.symbol)
                    gr = risk.gate(cfg, ctx.store, ctx.binance, cand, equity, mark, day, ctx.learner)
                    if gr.ok:
                        tid = executor.enter(cfg, ctx.binance, ctx.store, cand, gr.sizing, mark, day)
                        lines.append(line + f"\n🟢 <b>GİRİŞ #{tid}</b> qty {gr.sizing.qty} "
                                            f"notional {gr.sizing.notional} risk {gr.sizing.risk_usdt}")
                        entered = True
                        break
                    line += f" → ⨯ {gr.reason}"
                except Exception as e:
                    line += f" → hata {e!r}"
            lines.append(line)
        if enter and not entered:
            lines.append("→ Temiz CONFIRM+geçer aday yok, işlem açılmadı.")
        ctx.notifier.send("\n".join(lines))
