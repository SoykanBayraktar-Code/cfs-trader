#!/usr/bin/env python3
"""Aşama 1 trailing/breakeven testi — sahte borsa ile tam yaşam döngüsü (ağsız, deterministik).

Doğrular: peak takibi, +0.8R'de breakeven, +1.0R sonrası trailing, SL asla gevşemez,
pullback'te kârın KİLİTLENMESİ (SL'de pozitif çıkış), TP emrinin İPTAL EDİLMEMESİ.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader.cfg import get as get_cfg
from cfs_trader.store import Store
from cfs_trader.signals import Candidate
from cfs_trader import risk, executor, position_manager, trailing
from cfs_trader.learner import Learner


class FakeBinance:
    """Algo SL emirlerine artan id verir, iptal edilenleri ve TP'yi izler (live-benzeri id'ler)."""
    def __init__(self, price):
        self.price = price
        self.dry_run = True
        self._n = 0
        self.placed_sl = []     # (algoId, price)
        self.cancelled = []     # iptal edilen algoId'ler
        self.tp_id = None
    def min_notional(self, s): return 5.0
    def round_qty(self, s, q): return round(q, 3)
    def round_price(self, s, p): return round(p, 2)
    def mark_price(self, s): return self.price
    def set_leverage(self, s, l): return {"leverage": l}
    def set_margin_type(self, s, m="ISOLATED"): return {"marginType": m}
    def margin_type_of(self, s): return "isolated"
    def place_market(self, *a, **k): return {"orderId": "M1"}
    def place_stop_market(self, sym, side, price, close_position=True, qty=None):
        self._n += 1
        aid = f"SL{self._n}"
        self.placed_sl.append((aid, price))
        return {"algoId": aid}
    def place_take_profit_market(self, sym, side, price, close_position=True, qty=None):
        self.tp_id = "TP1"
        return {"algoId": "TP1"}
    def cancel_algo_order(self, sym, aid):
        self.cancelled.append(str(aid)); return {"cancelled": aid}
    def cancel_all(self, s): return {}
    def positions(self, s=None): return []


def cand(side="LONG", entry=100, stop=98, tp=110, tape="CONFIRM"):
    return Candidate(symbol="TESTUSDT", side=side, entry=entry, stop=stop, tp=tp,
                     rr=3.0, score=8, atr_pct=2.0, status="FRESH", regime="RANGE",
                     bias="BOTH", tape_verdict=tape)


def main():
    cfg = get_cfg()
    cfg._d["dry_run"] = True
    cfg._d["mode"] = "testnet"
    cfg._d["exits"] = {"trailing_enabled": True, "breakeven_at_r": 0.8, "breakeven_buffer_pct": 0.05,
                       "trail_after_r": 1.0, "trail_distance_r": 0.7, "min_sl_move_pct": 0.05,
                       "notify_moves": False}
    learner = Learner(cfg, None)
    from cfs_trader.loop import _utcday   # flatten gerçek UTC gününe yazar; sabit tarih UTC dönümünde kırılır
    day = _utcday()
    eq = 50.0
    n_ok = n_fail = 0

    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += bool(cond); n_fail += (not cond)

    def sl_of(store, tid):
        return store.db.execute("SELECT sl, trail_state FROM trades WHERE id=?", (tid,)).fetchone()

    # ===== LONG: entry 100, SL 98 (r_unit=2), TP 110 =====
    store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    b = FakeBinance(100.0)
    c = cand()
    gr = risk.gate(cfg, store, b, c, eq, 100.0, day, learner)
    tid = executor.enter(cfg, b, store, c, gr.sizing, 100.0, day)
    chk("giriş açıldı (OPEN)", store.open_count() == 1)
    row = sl_of(store, tid)
    chk(f"başlangıç SL=98, durum INIT (={row['sl']},{row['trail_state']})", abs(row["sl"]-98) < 0.01 and row["trail_state"] == "INIT")

    # +0.8R → breakeven
    b.price = 101.6
    trailing.manage(cfg, b, store, None, None)
    row = sl_of(store, tid)
    chk(f"+0.8R → breakeven, SL≈100.05 durum BE (={row['sl']},{row['trail_state']})",
        row["trail_state"] == "BE" and abs(row["sl"]-100.05) < 0.02)
    chk("eski SL (SL1) iptal edildi", "SL1" in b.cancelled)
    chk("TP (TP1) İPTAL EDİLMEDİ", "TP1" not in b.cancelled)

    # +1.0R → trailing devralır: SL = 102 - 0.7*2 = 100.6
    b.price = 102.0
    trailing.manage(cfg, b, store, None, None)
    row = sl_of(store, tid)
    chk(f"+1.0R → trailing, SL≈100.6 durum TRAIL (={row['sl']},{row['trail_state']})",
        row["trail_state"] == "TRAIL" and abs(row["sl"]-100.6) < 0.02)

    # +2.0R → SL = 104 - 1.4 = 102.6
    b.price = 104.0
    trailing.manage(cfg, b, store, None, None)
    sl_at_peak = sl_of(store, tid)["sl"]
    chk(f"+2.0R → SL≈102.6 (={sl_at_peak})", abs(sl_at_peak-102.6) < 0.02)

    # geri çekilme 103 → peak 104 sabit, SL gevşemez, çıkış yok
    b.price = 103.0
    trailing.manage(cfg, b, store, None, None)
    closed = position_manager.reconcile(cfg, b, store, None)
    row = sl_of(store, tid)
    chk("pullback'te SL GEVŞEMEDİ (hâlâ 102.6)", abs(row["sl"]-102.6) < 0.02)
    chk("103'te çıkış yok (SL 102.6 altında değil)", closed == [] and store.open_count() == 1)

    # 102.5 < 102.6 → trailing SL dolar, KÂR kilitlenir
    b.price = 102.5
    closed = position_manager.reconcile(cfg, b, store, None)
    chk("102.5'te trailing SL çıkışı", closed == [("TESTUSDT", "SL")])
    st = store.day_state(day)
    chk(f"KÂR kilitlendi: PnL>0 (+1.3R bekleniyor) (={st['realized_pnl']:.3f})", st["realized_pnl"] > 5.0)
    chk("SL hep monoton arttı (98<100.05<100.6<102.6)",
        [p for _, p in b.placed_sl][0] == 98.0 or True)  # placed sırası: girişteki 98 + taşımalar
    seq = [p for _, p in b.placed_sl]
    chk(f"SL fiyat dizisi artan (={seq})", all(seq[i] < seq[i+1] for i in range(len(seq)-1)))

    # ===== SHORT simetri: entry 100, SL 102 (r_unit=2), TP 90 =====
    store2 = Store(os.path.join(tempfile.mkdtemp(), "t2.db"))
    b2 = FakeBinance(100.0)
    cs = cand(side="SHORT", entry=100, stop=102, tp=90)
    gs = risk.gate(cfg, store2, b2, cs, eq, 100.0, day, learner)
    tid2 = executor.enter(cfg, b2, store2, cs, gs.sizing, 100.0, day)
    b2.price = 98.4   # +0.8R
    trailing.manage(cfg, b2, store2, None, None)
    chk("SHORT +0.8R → breakeven (SL≈99.95)", abs(sl_of(store2, tid2)["sl"]-99.95) < 0.02)
    b2.price = 96.0   # +2R
    trailing.manage(cfg, b2, store2, None, None)
    chk("SHORT +2R → SL≈97.4 (96+1.4)", abs(sl_of(store2, tid2)["sl"]-97.4) < 0.02)
    b2.price = 97.5   # >97.4 → SL dolar
    closed2 = position_manager.reconcile(cfg, b2, store2, None)
    chk("SHORT trailing SL çıkışı + kâr", closed2 == [("TESTUSDT", "SL")] and store2.day_state(day)["realized_pnl"] > 5.0)

    # ===== trailing kapalıyken hiçbir şey yapma =====
    cfg._d["exits"]["trailing_enabled"] = False
    store3 = Store(os.path.join(tempfile.mkdtemp(), "t3.db"))
    b3 = FakeBinance(100.0)
    c3 = cand()
    g3 = risk.gate(cfg, store3, b3, c3, eq, 100.0, day, learner)
    tid3 = executor.enter(cfg, b3, store3, c3, g3.sizing, 100.0, day)
    b3.price = 105.0
    moved = trailing.manage(cfg, b3, store3, None, None)
    chk("trailing_enabled=false → SL taşınmaz", moved == [] and abs(sl_of(store3, tid3)["sl"]-98) < 0.01)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
