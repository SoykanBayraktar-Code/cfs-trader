#!/usr/bin/env python3
"""Offline para-mantığı testi — sahte borsa ile tam işlem yaşam döngüsü (ağsız, deterministik).

Doğrular: risk boyutlandırma (margin-tavanı), giriş, SL/TP simülasyonu, PnL/R, kill-switch sayaçları,
gate redleri (max-eşzamanlı, tape≠CONFIRM, ardışık-zarar halt).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader.cfg import get as get_cfg
from cfs_trader.store import Store
from cfs_trader.signals import Candidate
from cfs_trader import risk, executor, position_manager
from cfs_trader.learner import Learner


class FakeBinance:
    def __init__(self, price):
        self.price = price
        self.dry_run = True
    def min_notional(self, s): return 5.0
    def round_qty(self, s, q): return round(q, 3)
    def round_price(self, s, p): return round(p, 2)
    def mark_price(self, s): return self.price
    def set_leverage(self, s, l): return {"leverage": l}
    def set_margin_type(self, s, m="ISOLATED"): return {"marginType": m}
    def place_market(self, *a, **k): return {"orderId": "M1"}
    def place_stop_market(self, *a, **k): return {"orderId": "S1"}
    def place_take_profit_market(self, *a, **k): return {"orderId": "T1"}
    def positions(self, s=None): return []
    def cancel_all(self, s): return {}


def cand(side="LONG", entry=100, stop=98, tp=106, tape="CONFIRM", tape_score=None):
    # CONFIRM→3.5 (confidence=1.0→conf_mult=1.0, temel sizing izole); CAUTION→1.0 (skor-kapısı 2.2 reddi)
    if tape_score is None:
        tape_score = 3.5 if tape == "CONFIRM" else 1.0
    return Candidate(symbol="TESTUSDT", side=side, entry=entry, stop=stop, tp=tp,
                     rr=3.0, score=8, atr_pct=2.0, status="FRESH", regime="RANGE",
                     bias="BOTH", tape_verdict=tape, tape_score=tape_score)


def main():
    cfg = get_cfg()
    cfg._d["dry_run"] = True       # offline test config.yaml mode/dry_run'dan BAĞIMSIZ — hep paper mantığı
    cfg._d["mode"] = "testnet"
    cfg._d["risk"]["max_concurrent"] = 1   # test config'ten BAĞIMSIZ (canlıda 3 olsa da senaryo 1 pozisyon)
    learner = Learner(cfg, None)  # enabled=False
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    store = Store(db)
    b = FakeBinance(100.0)
    # flatten gerçek günü (_utcday) kullanır; day_state aynı güne baksın diye sabit tarih DEĞİL bugünü al
    from cfs_trader.loop import _utcday
    day = _utcday()
    eq = 50.0
    n_ok = n_fail = 0

    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += cond; n_fail += (not cond)

    # 1) boyutlandırma: %50 tavan ama 5x*50=250 margin-tavanına kırpılır → risk ~$5 (%10), $25 değil
    c = cand()
    gr = risk.gate(cfg, store, b, c, eq, 100.0, day, learner)
    chk("gate OK (geçerli aday)", gr.ok)
    chk(f"notional margin-tavanı 250'ye kırpıldı (={gr.sizing.notional})", abs(gr.sizing.notional - 250) < 1)
    chk(f"gerçek risk ~5 USDT (%50 değil, margin-bağlı) (={gr.sizing.risk_usdt})", abs(gr.sizing.risk_usdt - 5.0) < 0.2)
    chk(f"qty=2.5 (={gr.sizing.qty})", abs(gr.sizing.qty - 2.5) < 0.01)

    # 2) giriş
    tid = executor.enter(cfg, b, store, c, gr.sizing, 100.0, day)
    chk("giriş → OPEN trade", store.open_count() == 1)

    # 3) max-eşzamanlı kapısı (1 dolu → ikinci reddedilir)
    gr2 = risk.gate(cfg, store, b, cand(), eq, 100.0, day, learner)
    chk("max-eşzamanlı RED", (not gr2.ok) and "eşzamanlı" in gr2.reason)

    # 4) SL tetikleme: fiyat 98'e düşsün → LONG SL → kapanış
    b.price = 98.0
    closed = position_manager.reconcile(cfg, b, store, None)
    chk("SL çıkışı tespit edildi", closed == [("TESTUSDT", "SL")])
    chk("pozisyon kapandı", store.open_count() == 0)
    st = store.day_state(day)
    chk(f"PnL ~ -5 USDT (={st['realized_pnl']})", abs(st['realized_pnl'] + 5.0) < 0.2)
    chk(f"ardışık-zarar=1 (={st['consec_losses']})", st['consec_losses'] == 1)

    # 5) tape≠CONFIRM kapısı
    gr3 = risk.gate(cfg, store, b, cand(tape="CAUTION"), eq, 100.0, day, learner)
    chk("tape CAUTION RED", (not gr3.ok) and "tape" in gr3.reason)

    # 6) ardışık-zarar halt: sayaç limite ulaşsın
    for _ in range(cfg.risk["max_consecutive_losses"] - 1):
        store.apply_close_to_day(day, -1.0)
    st = store.day_state(day)
    gr4 = risk.gate(cfg, store, b, cand(), eq, 100.0, day, learner)
    chk(f"ardışık-zarar={st['consec_losses']} → halt sinyali", (not gr4.ok) and gr4.halt)

    # 7) SHORT yön PnL işareti
    store2 = Store(os.path.join(tempfile.mkdtemp(), "t2.db"))
    cs = cand(side="SHORT", entry=100, stop=102, tp=94)
    grs = risk.gate(cfg, store2, b, cs, eq, 100.0, day, learner) if False else None
    # SHORT: fiyat 102'ye çıkınca SL
    b.price = 100.0
    gs = risk.gate(cfg, store2, b, cs, eq, 100.0, day, learner)
    executor.enter(cfg, b, store2, cs, gs.sizing, 100.0, day)
    b.price = 102.0
    position_manager.reconcile(cfg, b, store2, None)
    st2 = store2.day_state(day)
    chk(f"SHORT SL → negatif PnL (={st2['realized_pnl']})", st2['realized_pnl'] < 0)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
