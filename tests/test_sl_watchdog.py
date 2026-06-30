#!/usr/bin/env python3
"""AUDIT #5 — SL-watchdog birim testi (ağsız, deterministik).

Doğrular: açık pozisyonda borsada koruyucu STOP yoksa watchdog DB sl'den yeniden koyar; STOP varsa no-op;
dry_run no-op; pozisyon borsada kapalıysa (amt=0) dokunmaz (reconcile'a bırakır).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader.cfg import get as get_cfg
from cfs_trader.store import Store
from cfs_trader import position_manager as pm


class FakeBinance:
    def __init__(self, pos_amt, algo_orders, dry_run=False):
        self.dry_run = dry_run
        self._amt = pos_amt
        self._algos = algo_orders
        self.placed = []
    def positions(self, s=None):
        return [{"symbol": s or "TESTUSDT", "positionAmt": str(self._amt)}]
    def open_algo_orders(self, s=None):
        return list(self._algos)
    def place_stop_market(self, sym, side, price, close_position=True, qty=None):
        self.placed.append((sym, side, price))
        return {"algoId": "NEWSL1"}


def _cfg(dry=False):
    cfg = get_cfg()
    cfg._d["dry_run"] = dry
    cfg._d["mode"] = "testnet"
    return cfg


def _open(store, side="LONG", sl=98.0):
    return store.open_trade(symbol="TESTUSDT", side=side, entry=100.0, qty=1.0, sl=sl, tp=106.0,
                            leverage=5, risk_usdt=5.0, regime="RANGE", signal_type="FRESH",
                            tape_verdict="CONFIRM", mode="testnet", dry_run=0,
                            sl_init=sl, peak_price=100.0, trail_state="INIT", sl_order_id="OLD")


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += cond; n_fail += (not cond)

    # 1) Pozisyon açık + borsada STOP YOK → watchdog yeniden koyar
    store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    tid = _open(store, side="LONG", sl=98.0)
    b = FakeBinance(pos_amt=-63.0, algo_orders=[{"orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "120"}])  # sadece TP
    fixed = pm.ensure_protective_sl(_cfg(), b, store, None, None)
    chk(f"SL eksikti → yeniden kondu (fixed={fixed})", fixed == ["TESTUSDT"])
    chk(f"place_stop_market SELL@98 çağrıldı (={b.placed})", b.placed == [("TESTUSDT", "SELL", 98.0)])
    chk(f"DB sl_order_id güncellendi (={store.get_trade(tid)['sl_order_id']})",
        store.get_trade(tid)["sl_order_id"] == "NEWSL1")

    # 2) Borsada STOP VAR → no-op
    store2 = Store(os.path.join(tempfile.mkdtemp(), "t2.db"))
    _open(store2)
    b2 = FakeBinance(pos_amt=-63.0, algo_orders=[{"orderType": "STOP_MARKET", "triggerPrice": "98"}])
    fixed2 = pm.ensure_protective_sl(_cfg(), b2, store2, None, None)
    chk(f"STOP var → no-op (fixed={fixed2}, placed={b2.placed})", fixed2 == [] and b2.placed == [])

    # 3) SHORT yön → BUY stop
    store3 = Store(os.path.join(tempfile.mkdtemp(), "t3.db"))
    _open(store3, side="SHORT", sl=102.0)
    b3 = FakeBinance(pos_amt=63.0, algo_orders=[])
    fixed3 = pm.ensure_protective_sl(_cfg(), b3, store3, None, None)
    chk(f"SHORT eksik SL → BUY@102 (={b3.placed})", b3.placed == [("TESTUSDT", "BUY", 102.0)])

    # 4) dry_run → no-op
    store4 = Store(os.path.join(tempfile.mkdtemp(), "t4.db"))
    _open(store4)
    b4 = FakeBinance(pos_amt=-63.0, algo_orders=[])
    fixed4 = pm.ensure_protective_sl(_cfg(dry=True), b4, store4, None, None)
    chk(f"dry_run → no-op (placed={b4.placed})", fixed4 == [] and b4.placed == [])

    # 5) Pozisyon borsada KAPALI (amt=0) → dokunma (reconcile'a bırak)
    store5 = Store(os.path.join(tempfile.mkdtemp(), "t5.db"))
    _open(store5)
    b5 = FakeBinance(pos_amt=0.0, algo_orders=[])
    fixed5 = pm.ensure_protective_sl(_cfg(), b5, store5, None, None)
    chk(f"pozisyon kapalı → dokunma (placed={b5.placed})", fixed5 == [] and b5.placed == [])

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
