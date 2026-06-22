"""ctl — cfs-trader güvenli kontrol arayüzü (WOWAsistan / elle kumanda).

    cd /root/cfs-trader && python3 -m cfs_trader.ctl <komut> [arg...]

Komutlar:
  status                 mod, parametreler, bakiye, açık pozisyon, günlük PnL  (salt-okunur)
  show                   düzenlenebilir parametreler + sınırlar               (salt-okunur)
  set <param> <değer>    parametreyi değiştir (config.yaml yedeklenir + servis restart)
  pause | resume         işlem açmayı duraklat / devam ettir
  scan                   tara + ilk adayları tape'le, göster (İŞLEM AÇMAZ)
  trade                  tara → CONFIRM adayı tam risk-kapısından geçir → GERÇEK aç   [--yes]
  close <SEMBOL>         o sembolün açık pozisyonunu kapat                            [--yes]
  flatten                TÜM açık pozisyonları kapat                                  [--yes]

set parametreleri (sınır):
  leverage <1-20>            kaldıraç
  budget <5-100000>          işlem bütçesi USDT (boyutlandırma tabanı)
  risk_pct <1-100>           işlem başına risk-tavanı %
  max_concurrent <1-10>      eşzamanlı açık pozisyon sayısı
  daily_loss_pct <1-100>     günlük zarar kill-switch %
  consec_losses <1-50>       ardışık zarar kesici
  mode <testnet|live>        GERÇEK PARA anahtarı — TEHLİKELİ            [--yes, açık pozisyon yokken]
  dry_run <true|false>       emir gönder/gönderme — TEHLİKELİ           [--yes, açık pozisyon yokken]

Tehlikeli komutlar (set mode/dry_run, trade, close, flatten) `--yes` ZORUNLU.
Binance'e ASLA doğrudan dokunmaz — tüm güvenlik rayları (risk.py) korunur.
"""
import os
import re
import sys
import subprocess

from .cfg import Config

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG = os.path.join(_ROOT, "config.yaml")
_SERVICE = "cfs-trader.service"

# friendly_name -> (yaml_key, kind, min, max)  | kind: int|float|mode|bool
_PARAMS = {
    "leverage":       ("leverage", "int", 1, 20),
    "budget":         ("budget_usdt", "float", 5, 100000),
    "risk_pct":       ("risk_per_trade_pct", "float", 1, 100),
    "max_concurrent": ("max_concurrent", "int", 1, 10),
    "daily_loss_pct": ("daily_max_loss_pct", "float", 1, 100),
    "consec_losses":  ("max_consecutive_losses", "int", 1, 50),
    "mode":           ("mode", "mode", None, None),
    "dry_run":        ("dry_run", "bool", None, None),
}
_DANGER_SET = {"mode", "dry_run"}


# ----------------------------------------------------------------------------- yardımcılar
def _die(msg, code=2):
    print(f"HATA: {msg}")
    sys.exit(code)


def _ok(msg=""):
    if msg:
        print(msg)
    sys.exit(0)


def _backup_config():
    import time
    bak = f"{_CONFIG}.bak.{int(time.time())}"
    with open(_CONFIG) as f:
        data = f.read()
    with open(bak, "w") as f:
        f.write(data)
    return bak


def _set_yaml_line(key, new_value):
    """config.yaml'da `key:` satırının değerini değiştirir, GİRİNTİ + satır-içi yorumu korur.

    Anahtarlar dosyada benzersiz (mode/dry_run/budget_usdt + risk alt-anahtarları). Tam 1 eşleşme şart.
    """
    with open(_CONFIG) as f:
        lines = f.readlines()
    pat = re.compile(rf"^(\s*){re.escape(key)}:(\s*)(\S.*?)?(\s+#.*)?$")
    hits = [i for i, ln in enumerate(lines) if pat.match(ln.rstrip("\n"))]
    if len(hits) != 1:
        _die(f"config.yaml'da '{key}' için {len(hits)} eşleşme (1 bekleniyordu) — elle kontrol et")
    i = hits[0]
    m = pat.match(lines[i].rstrip("\n"))
    indent, _, _old, comment = m.groups()
    comment = comment or ""
    lines[i] = f"{indent}{key}: {new_value}{comment}\n"
    with open(_CONFIG, "w") as f:
        f.writelines(lines)


def _restart_service():
    r = subprocess.run(["systemctl", "restart", _SERVICE], capture_output=True, text=True)
    if r.returncode != 0:
        _die(f"servis restart başarısız: {r.stderr.strip() or r.stdout.strip()}", code=3)


def _flags(argv):
    yes = "--yes" in argv
    rest = [a for a in argv if a != "--yes"]
    return yes, rest


def _open_count_current():
    """Mevcut mod'un DB'sinde açık pozisyon sayısı (mode/dry_run değişiminden önce kontrol)."""
    from .store import Store
    cfg = Config(_CONFIG)
    st = Store(cfg.db_path)
    try:
        return st.open_count()
    finally:
        st.close()


# ----------------------------------------------------------------------------- komutlar
def cmd_show():
    cfg = Config(_CONFIG)
    r = cfg.risk
    print("cfs-trader düzenlenebilir parametreler")
    print(f"  mode           = {cfg.mode}            (testnet|live)  [TEHLİKELİ]")
    print(f"  dry_run        = {cfg.dry_run}          (true|false)    [TEHLİKELİ]")
    print(f"  budget         = {cfg.budget}          USDT  [5-100000]")
    print(f"  leverage       = {r['leverage']}              [1-20]")
    print(f"  risk_pct       = {r['risk_per_trade_pct']}             % [1-100]")
    print(f"  max_concurrent = {r['max_concurrent']}              [1-10]")
    print(f"  daily_loss_pct = {r['daily_max_loss_pct']}             % [1-100]")
    print(f"  consec_losses  = {r['max_consecutive_losses']}              [1-50]")
    print("değiştir:  python3 -m cfs_trader.ctl set <param> <değer> [--yes]")


def cmd_status():
    from .loop import Ctx, _utcday
    ctx = Ctx()
    cfg = ctx.cfg
    st = ctx.store.day_state(_utcday())
    try:
        bal = round(ctx.binance.available_usdt(), 2) if not cfg.dry_run else f"{cfg.budget} (paper)"
    except Exception as e:
        bal = f"? ({e})"
    opens = ctx.store.open_trades()
    print("cfs-trader DURUM")
    print(f"  mod: {cfg.mode} | dry_run: {cfg.dry_run} | bütçe: {cfg.budget} | kaldıraç: {cfg.risk['leverage']}x")
    print(f"  risk/işlem: %{cfg.risk['risk_per_trade_pct']} | max eşzamanlı: {cfg.risk['max_concurrent']}")
    print(f"  serbest bakiye: {bal} USDT")
    print(f"  gün PnL: {st['realized_pnl']:+.2f} | işlem: {st['trades_count']} | "
          f"ardışık-zarar: {st['consec_losses']} | halted: {bool(st['halted'])}"
          + (f" ({st['halt_reason']})" if st['halted'] else ""))
    print(f"  açık pozisyon: {len(opens)}")
    for t in opens:
        print(f"    #{t['id']} {t['symbol']} {t['side']} qty={t['qty']} entry={t['entry']} "
              f"SL={t['sl']} TP={t['tp']} risk={t['risk_usdt']}")


def cmd_set(argv):
    yes, rest = _flags(argv)
    if len(rest) < 2:
        _die("kullanım: set <param> <değer> [--yes]")
    name, raw = rest[0], rest[1]
    if name not in _PARAMS:
        _die(f"bilinmeyen parametre '{name}'. Geçerli: {', '.join(_PARAMS)}")
    key, kind, lo, hi = _PARAMS[name]

    # değer doğrula + normalize
    if kind == "int":
        try:
            val = int(raw)
        except ValueError:
            _die(f"{name} tam sayı olmalı")
        if not (lo <= val <= hi):
            _die(f"{name} {lo}-{hi} aralığında olmalı (verilen {val})")
        out = str(val)
    elif kind == "float":
        try:
            val = float(raw)
        except ValueError:
            _die(f"{name} sayı olmalı")
        if not (lo <= val <= hi):
            _die(f"{name} {lo}-{hi} aralığında olmalı (verilen {val})")
        out = str(val)
    elif kind == "mode":
        val = raw.lower()
        if val not in ("testnet", "live"):
            _die("mode yalnız 'testnet' veya 'live' olabilir")
        out = val
    elif kind == "bool":
        val = raw.lower()
        if val not in ("true", "false"):
            _die("dry_run yalnız 'true' veya 'false' olabilir")
        out = val
    else:
        _die("iç hata: bilinmeyen tip")

    # TEHLİKELİ değişiklikler: --yes + açık pozisyon yok
    if name in _DANGER_SET:
        if not yes:
            _die(f"'{name}' GERÇEK-PARA davranışını değiştirir — onay için --yes ekle")
        oc = _open_count_current()
        if oc > 0:
            _die(f"{oc} açık pozisyon var — önce 'flatten --yes' ile kapat, sonra {name} değiştir")

    cfg = Config(_CONFIG)
    old = cfg.get(key) if key in ("mode", "dry_run", "budget_usdt") else cfg.risk.get(key)
    bak = _backup_config()
    _set_yaml_line(key, out)
    _restart_service()
    print(f"OK: {name} {old} -> {out}  (yedek: {os.path.basename(bak)}, servis yeniden başlatıldı)")


def cmd_pause():
    from .loop import Ctx, _utcday
    ctx = Ctx()
    ctx.store.halt_day(_utcday(), "manuel pause (ctl)")
    print("OK: işlem açma DURAKLATILDI. 'resume' ile aç.")


def cmd_resume():
    from .loop import Ctx, _utcday
    ctx = Ctx()
    ctx.store.db.execute("UPDATE daily_state SET halted=0, halt_reason=NULL WHERE day=?", (_utcday(),))
    ctx.store.db.commit()
    print("OK: işlem açma DEVAM ediyor.")


def cmd_scan():
    from . import signals
    from .loop import Ctx
    ctx = Ctx()
    cfg = ctx.cfg
    n = cfg.signals.get("max_tape_checks", 6)
    regime, cands = signals.scan_all(cfg)
    print(f"Rejim {regime['regime']} ({regime['bias']}) | {len(cands)} aday | ilk {min(n, len(cands))} tape")
    if not cands:
        print("Aday yok.")
        return
    for cand in cands[:n]:
        signals.confirm_tape(cfg, cand, dur=22)
        print(f"  {cand.symbol} {cand.side}: tape {cand.tape_verdict} ({cand.tape_score:+.1f}) | "
              f"RR {cand.rr} entry {cand.entry} SL {cand.stop} TP {cand.tp}")
    print("(scan İŞLEM AÇMAZ — açmak için: trade --yes)")


def cmd_trade(argv):
    yes, _ = _flags(argv)
    if not yes:
        _die("trade GERÇEK işlem açar — onay için --yes ekle")
    from . import signals, risk, executor
    from .loop import Ctx, _utcday, _equity
    from .lock import cross_lock
    ctx = Ctx()
    cfg = ctx.cfg
    day = _utcday()
    st = ctx.store.day_state(day)
    if st["halted"]:
        _die(f"gün durduruldu ({st['halt_reason']}) — 'resume' gerekebilir", code=1)
    if ctx.store.open_count() >= cfg.risk["max_concurrent"]:
        _die("kapasite dolu — açık pozisyon limitinde", code=1)

    with cross_lock(cfg):
        # kilit altında tekrar kontrol (döngü bu arada açmış olabilir)
        if ctx.store.open_count() >= cfg.risk["max_concurrent"]:
            _die("kapasite dolu (kilit altında) — işlem açılmadı", code=1)
        equity = _equity(ctx)
        regime, cands = signals.scan_all(cfg)
        n = cfg.signals.get("max_tape_checks", 6)
        print(f"Rejim {regime['regime']} ({regime['bias']}) | {len(cands)} aday")
        if not cands:
            _ok("Aday yok — işlem açılmadı.")
        for cand in cands[:n]:
            signals.confirm_tape(cfg, cand, dur=22)
            line = f"  {cand.symbol} {cand.side}: tape {cand.tape_verdict} ({cand.tape_score:+.1f})"
            try:
                mark = ctx.binance.mark_price(cand.symbol)
                gr = risk.gate(cfg, ctx.store, ctx.binance, cand, equity, mark, day, ctx.learner)
                if gr.ok:
                    tid = executor.enter(cfg, ctx.binance, ctx.store, cand, gr.sizing, mark, day)
                    msg = (f"🟢 GİRİŞ {cand.symbol} {cand.side} (#{tid})\n"
                           f"entry~{mark} | SL {cand.stop} | TP {cand.tp}\n"
                           f"qty {gr.sizing.qty} | notional {gr.sizing.notional} | risk {gr.sizing.risk_usdt} USDT\n"
                           f"tape {cand.tape_verdict} | RR {cand.rr} | {'DRY' if cfg.dry_run else cfg.mode}")
                    ctx.notifier.send(msg)
                    print(line)
                    _ok(f"AÇILDI #{tid}: {cand.symbol} {cand.side} qty={gr.sizing.qty} "
                        f"notional={gr.sizing.notional} risk={gr.sizing.risk_usdt}")
                print(line + f" -> RED: {gr.reason}")
            except Exception as e:
                print(line + f" -> hata {e!r}")
        _ok("Temiz CONFIRM+geçer aday yok — işlem açılmadı.")


def cmd_close(argv):
    yes, rest = _flags(argv)
    if not yes:
        _die("close GERÇEK pozisyon kapatır — onay için --yes ekle")
    if not rest:
        _die("kullanım: close <SEMBOL> --yes")
    sym = rest[0].upper()
    from . import executor
    from .loop import Ctx
    from .lock import cross_lock
    ctx = Ctx()
    cfg = ctx.cfg
    with cross_lock(cfg):
        targets = [t for t in ctx.store.open_trades() if t["symbol"].upper() == sym]
        if not targets:
            _die(f"{sym} için açık pozisyon yok", code=1)
        for t in targets:
            price = ctx.binance.mark_price(t["symbol"])
            pnl, r_mult, _ = executor.flatten(cfg, ctx.binance, ctx.store, t, price, "MANUAL", ctx.notifier)
            print(f"KAPATILDI {t['symbol']} {t['side']} @~{price}  PnL {pnl:+.3f} ({r_mult:+.2f}R)")
    _ok()


def cmd_flatten(argv):
    yes, _ = _flags(argv)
    if not yes:
        _die("flatten TÜM pozisyonları kapatır — onay için --yes ekle")
    from . import position_manager
    from .loop import Ctx
    from .lock import cross_lock
    ctx = Ctx()
    cfg = ctx.cfg
    with cross_lock(cfg):
        opens = ctx.store.open_trades()
        if not opens:
            _ok("Açık pozisyon yok.")
        position_manager.flatten_all(cfg, ctx.binance, ctx.store, "MANUAL", ctx.notifier)
        print(f"KAPATILDI: {len(opens)} pozisyon düzleştirildi.")
    _ok()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    dispatch = {
        "show": cmd_show, "status": cmd_status, "pause": cmd_pause, "resume": cmd_resume,
        "scan": cmd_scan,
    }
    if cmd in dispatch:
        dispatch[cmd]()
    elif cmd == "set":
        cmd_set(rest)
    elif cmd == "trade":
        cmd_trade(rest)
    elif cmd == "close":
        cmd_close(rest)
    elif cmd == "flatten":
        cmd_flatten(rest)
    else:
        _die(f"bilinmeyen komut '{cmd}'. 'help' ile listeyi gör.")


if __name__ == "__main__":
    main()
