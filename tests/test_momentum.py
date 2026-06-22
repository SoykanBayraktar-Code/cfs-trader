#!/usr/bin/env python3
"""Momentum/oi_surge saf-mantık testi (ağsız): seviye hesabı, yön çıkarımı, sınıf filtresi."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import signals, risk
from cfs_trader.cfg import get as get_cfg
from cfs_trader.store import Store
from cfs_trader.signals import Candidate
from cfs_trader.learner import Learner


class FakeBinance:
    def __init__(self, price):
        self.price = price; self.dry_run = True
    def min_notional(self, s): return 5.0
    def round_qty(self, s, q): return round(q, 3)
    def round_price(self, s, p): return round(p, 2)
    def mark_price(self, s): return self.price


def main():
    n_ok = n_fail = 0

    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += bool(cond); n_fail += (not cond)

    # ---- _mom_levels ----
    # LONG: px=100, atr=4% → atr=4. stop=100-1.5*4=94. risk=6. tp=100+2.5*6=115. rr=2.5
    lv = signals._mom_levels(100, 4.0, "LONG", 1.5, 2.5)
    chk(f"LONG seviye (entry,stop,tp,rr)={lv}", lv and abs(lv[0]-100)<1e-9 and abs(lv[1]-94)<1e-9 and abs(lv[2]-115)<1e-9 and lv[3]==2.5)
    # SHORT: px=100, atr=4% → stop=106, risk=6, tp=100-2.5*6=85
    lv = signals._mom_levels(100, 4.0, "SHORT", 1.5, 2.5)
    chk(f"SHORT seviye={lv}", lv and abs(lv[1]-106)<1e-9 and abs(lv[2]-85)<1e-9)
    # atr=0 → None
    chk("atr=0 → None", signals._mom_levels(100, 0, "LONG", 1.5, 2.5) is None)
    # stop pozitif kalmalı: aşırı geniş sl_mult negatif stop → None
    chk("negatif stop → None", signals._mom_levels(10, 80.0, "LONG", 2.0, 2.5) is None)

    # ---- _oi_direction ----
    chk("taker 1.3 fund 0.01 → LONG", signals._oi_direction(1.3, 0.01) == "LONG")
    chk("taker 1.3 fund 0.08 → None (funding yüksek)", signals._oi_direction(1.3, 0.08) is None)
    chk("taker 0.8 → SHORT", signals._oi_direction(0.8, 0.0) == "SHORT")
    chk("taker 1.0 → None (belirsiz)", signals._oi_direction(1.0, 0.0) is None)
    chk("taker None → None", signals._oi_direction(None, None) is None)

    # ---- _class_ok ----
    chk("🟢SESSIZ-BIRIKIM ∈ [SESSIZ,ERKEN]", signals._class_ok("🟢SESSIZ-BIRIKIM", ["SESSIZ", "ERKEN"]))
    chk("🔴GEC/ISINMIS ∉ [SESSIZ,ERKEN]", not signals._class_ok("🔴GEC/ISINMIS", ["SESSIZ", "ERKEN"]))
    chk("🟢 ATESLENIYOR ∈ [ATESLENIYOR,KOSUYOR]", signals._class_ok("🟢 ATESLENIYOR", ["ATESLENIYOR", "KOSUYOR"]))
    chk("🔴 TUKENMIS/tuzak ∉ izinli", not signals._class_ok("🔴 TUKENMIS/tuzak", ["ATESLENIYOR", "KOSUYOR"]))
    chk("None etiket → False", not signals._class_ok(None, ["SESSIZ"]))

    # ---- risk_mult: momentum yarım boyut ----
    cfg = get_cfg()
    cfg._d["dry_run"] = True
    cfg._d["mode"] = "testnet"
    cfg._d["risk"]["max_concurrent"] = 2
    cfg._d["signals"]["require_tape_confirm"] = True
    cfg._d["signals"]["tape_min_score"] = 2.7
    learner = Learner(cfg, None)
    b = FakeBinance(100.0)
    from cfs_trader.loop import _utcday
    day = _utcday()

    def mk(risk_mult=1.0, min_ts=0.0, tape="CONFIRM", tscore=5.0):
        return Candidate(symbol="TESTUSDT", side="LONG", entry=100, stop=98, tp=110, rr=2.5,
                         score=5, atr_pct=2.0, status="MOMENTUM", regime="RANGE", bias="BOTH",
                         tape_verdict=tape, tape_score=tscore, risk_mult=risk_mult, min_tape_score=min_ts)

    s_full = Store(os.path.join(tempfile.mkdtemp(), "f.db"))
    g_full = risk.gate(cfg, s_full, b, mk(risk_mult=1.0), 50.0, 100.0, day, learner)
    s_half = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    g_half = risk.gate(cfg, s_half, b, mk(risk_mult=0.5), 50.0, 100.0, day, learner)
    chk(f"risk_mult=0.5 → yarım risk ({g_half.sizing.risk_usdt} ≈ {g_full.sizing.risk_usdt}/2)",
        g_full.ok and g_half.ok and abs(g_half.sizing.risk_usdt - g_full.sizing.risk_usdt/2) < 0.3)

    # ---- min_tape_score: CONFIRM ama skor düşük → RED ----
    s2 = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    g_low = risk.gate(cfg, s2, b, mk(min_ts=4.5, tape="CONFIRM", tscore=3.2), 50.0, 100.0, day, learner)
    chk("min_tape_score=4.5, skor 3.2 → RED", (not g_low.ok) and "skoru zayıf" in g_low.reason)
    g_hi = risk.gate(cfg, s2, b, mk(min_ts=4.5, tape="CONFIRM", tscore=5.0), 50.0, 100.0, day, learner)
    chk("min_tape_score=4.5, skor 5.0 → GEÇER", g_hi.ok)

    # ---- gevşetilmiş tape kapısı (tape_min_score=2.7) — base aday (min_ts=0) ----
    s3 = Store(os.path.join(tempfile.mkdtemp(), "g.db"))
    g_conf = risk.gate(cfg, s3, b, mk(min_ts=0, tape="CONFIRM", tscore=3.5), 50.0, 100.0, day, learner)
    chk("CONFIRM her zaman geçer", g_conf.ok)
    s4 = Store(os.path.join(tempfile.mkdtemp(), "g2.db"))
    g_caut_ok = risk.gate(cfg, s4, b, mk(min_ts=0, tape="CAUTION", tscore=2.8), 50.0, 100.0, day, learner)
    chk("CAUTION skor 2.8 ≥ 2.7 → GEÇER (gevşetme)", g_caut_ok.ok)
    s5 = Store(os.path.join(tempfile.mkdtemp(), "g3.db"))
    g_caut_no = risk.gate(cfg, s5, b, mk(min_ts=0, tape="CAUTION", tscore=2.5), 50.0, 100.0, day, learner)
    chk("CAUTION skor 2.5 < 2.7 → RED", (not g_caut_no.ok) and "skor" in g_caut_no.reason)
    s6 = Store(os.path.join(tempfile.mkdtemp(), "g4.db"))
    g_veto = risk.gate(cfg, s6, b, mk(min_ts=0, tape="VETO", tscore=4.0), 50.0, 100.0, day, learner)
    chk("VETO yüksek skorda bile RED", (not g_veto.ok) and "VETO" in g_veto.reason)

    # ---- çift-giriş guard (DB): aynı sembol zaten açıksa RED ----
    s7 = Store(os.path.join(tempfile.mkdtemp(), "d.db"))
    s7.open_trade(symbol="TESTUSDT", side="LONG", qty=1.0, entry=100, sl=98, tp=110, risk_usdt=5.0)
    g_dup = risk.gate(cfg, s7, b, mk(min_ts=0, tape="CONFIRM", tscore=5.0), 50.0, 100.0, day, learner)
    chk("aynı sembol zaten açık (DB) → RED", (not g_dup.ok) and "çift-giriş" in g_dup.reason)

    # ---- günlük kill-switch KAPALI (daily_max_loss_pct=0) + ardışık 3 ----
    cfg._d["risk"]["daily_max_loss_pct"] = 0
    cfg._d["risk"]["max_consecutive_losses"] = 3
    s8 = Store(os.path.join(tempfile.mkdtemp(), "k.db"))
    s8.day_state(day)
    s8.db.execute("UPDATE daily_state SET realized_pnl=-100, consec_losses=0 WHERE day=?", (day,)); s8.db.commit()
    g_nokill = risk.gate(cfg, s8, b, mk(min_ts=0, tape="CONFIRM", tscore=5.0), 50.0, 100.0, day, learner)
    chk("günlük kill-switch KAPALI: -100 zararda bile halt YOK", g_nokill.ok)
    s9 = Store(os.path.join(tempfile.mkdtemp(), "k2.db"))
    s9.day_state(day)
    s9.db.execute("UPDATE daily_state SET consec_losses=3 WHERE day=?", (day,)); s9.db.commit()
    g_consec = risk.gate(cfg, s9, b, mk(min_ts=0, tape="CONFIRM", tscore=5.0), 50.0, 100.0, day, learner)
    chk("ardışık 3 zarar → halt", (not g_consec.ok) and g_consec.halt)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
