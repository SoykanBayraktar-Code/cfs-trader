import time
from cfs_trader.cfg import get
from cfs_trader.store import Store
from cfs_trader.binance import Binance

cfg = get()
print("=== MOD / EMIR ===")
print("mode:", cfg.mode, "| dry_run:", cfg.dry_run, "| budget:", cfg.budget,
      "| leverage:", cfg.risk["leverage"], "| max_concurrent:", cfg.risk["max_concurrent"],
      "| margin_type:", cfg.risk.get("margin_type"))

print("\n=== SINYAL KAYNAKLARI ===")
m = cfg.get("momentum", {}) or {}
print("scan_v3 : AKTIF (cekirdek TA setup)")
print("momentum:", m.get("enabled"), "| sources:", m.get("sources"),
      "| momentum_scan:", (m.get("momentum_scan") or {}).get("classes"), "risk_x", (m.get("momentum_scan") or {}).get("risk_mult"))

print("\n=== CIKIS YONETIMI ===")
ex = cfg.get("exits", {}) or {}
print("trailing:", ex.get("trailing_enabled"), "| be@", ex.get("breakeven_at_r"), "R | trail@", ex.get("trail_after_r"), "R dist", ex.get("trail_distance_r"), "R")

print("\n=== TAPE + LEARNER ===")
s = cfg.signals
print("tape: require_confirm", s.get("require_tape_confirm"), "| tape_min_score", s.get("tape_min_score"), "| max_tape_checks", s.get("max_tape_checks"))
lc = cfg.get("learner", {}) or {}
print("learner:", lc.get("enabled"), "| min_samples", lc.get("min_samples"), "| suppress<", lc.get("suppress_below_expectancy"))

print("\n=== GUN DURUMU ===")
store = Store(cfg.db_path)
day = time.strftime("%Y-%m-%d", time.gmtime())
st = store.day_state(day)
print("gun", day, "| halted:", bool(st["halted"]), "| pnl:", round(st["realized_pnl"], 2), "| consec:", st["consec_losses"], "| trades:", st["trades_count"])
print("acik islem (DB):", store.open_count())

print("\n=== BORSA / KASA ===")
b = Binance(cfg)
b.sync_time()
avail = b.available_usdt()
need_one = min(cfg.risk["leverage"] * cfg.budget, cfg.risk["max_position_notional_usdt"]) / cfg.risk["leverage"]
print("serbest USDT:", round(avail, 2), "| 1 pozisyon icin ~margin:", round(need_one, 2))
can_open = avail >= need_one * 1.02
print("yeni pozisyon acabilir mi:", "EVET" if can_open else "HAYIR (serbest margin yetersiz)")
ps = b.positions()
print("borsada acik pozisyon:", len(ps))
for p in ps:
    print("  ", p["symbol"], p["positionAmt"], "entry", p.get("entryPrice"))

print("\n=== TELEGRAM ===")
tok, chat = cfg.telegram()
print("token:", "VAR" if tok else "YOK", "| chat_id:", "VAR" if chat else "YOK")

print("\n=== SONUC ===")
ready = (cfg.mode == "live" and not cfg.dry_run and not bool(st["halted"]) and can_open)
print("CANLI TRADE'E HAZIR:", "EVET ✅" if ready else "HAYIR ⚠️ (yukaridaki engele bak)")
