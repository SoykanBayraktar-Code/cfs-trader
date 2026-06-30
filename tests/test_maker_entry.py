#!/usr/bin/env python3
"""#2 Maker giriş testi — GTX post-only + taker fallback, sahte borsa (ağsız, deterministik).

Doğrular: tam-maker dolum / dolmadı→taker / kısmi→taker-topup / GTX-red→taker;
HER senaryoda SL TAM 1 KEZ konur (çıplak pencere yok), kaydedilen qty/giriş-fiyatı DOĞRU,
TP 1 kez konur, tam boyut garantisi (işlem kaçmaz)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader.cfg import get as get_cfg
from cfs_trader.store import Store
from cfs_trader.signals import Candidate
from cfs_trader import risk, executor
from cfs_trader.learner import Learner
from cfs_trader.loop import _utcday


class FakeMakerBinance:
    """Maker-yolu sahte borsası. scenario ile limit dolum davranışı script'lenir.
    SL/TP/market/limit çağrılarını sayar; get_order ardışık dolum durumları döndürür."""
    def __init__(self, price, scenario="maker_full", qty=10.0):
        self.price = price
        self.dry_run = False
        self.scenario = scenario
        self.qty = qty
        self._n = 0
        self.sl_calls = []      # place_stop_market çağrıları (price)
        self.tp_calls = 0
        self.market_calls = []  # (side, qty)
        self.limit_calls = []   # (side, qty, price)
        self.cancelled_orders = []
        self._order_qty = 0.0       # place_limit'te GERÇEK emredilen qty yakalanır
        self._bid = round(price * 0.999, 4)   # LONG maker bid (markttan ucuz = iyi)
        self._ask = round(price * 1.001, 4)

    # market data / hesap
    def book_ticker(self, s): return {"bidPrice": self._bid, "askPrice": self._ask}
    def mark_price(self, s): return self.price
    def available_usdt(self): return 100000.0
    def positions(self, s=None): return []
    def min_notional(self, s): return 5.0
    def round_qty(self, s, q): return round(q, 3)
    def round_price(self, s, p): return round(p, 4)
    def set_leverage(self, s, l): return {"leverage": l}
    def set_margin_type(self, s, m="ISOLATED"): return {"marginType": m}
    def margin_type_of(self, s): return "isolated"

    # emirler
    def place_limit(self, sym, side, qty, price, tif="GTX", reduce_only=False):
        self.limit_calls.append((side, qty, price))
        self._order_qty = qty       # gerçek emredilen miktarı yakala (dinamik sizing)
        if self.scenario == "gtx_reject":
            return {"orderId": "L1", "status": "EXPIRED"}   # post-only reddi
        return {"orderId": "L1", "status": "NEW"}
    def get_order(self, sym, oid):
        q = self._order_qty
        if self.scenario == "maker_full":
            return {"executedQty": q, "status": "FILLED", "avgPrice": self._bid}
        if self.scenario == "maker_partial":
            return {"executedQty": round(q / 2, 3), "status": "PARTIALLY_FILLED", "avgPrice": self._bid}
        return {"executedQty": 0.0, "status": "NEW", "avgPrice": 0}   # maker_none / gtx_reject: dolum yok
    def cancel_order(self, sym, oid):
        self.cancelled_orders.append(oid); return {"orderId": oid}
    def place_market(self, sym, side, qty, reduce_only=False):
        self.market_calls.append((side, qty)); return {"orderId": "M1"}
    def place_stop_market(self, sym, side, price, close_position=True, qty=None):
        self._n += 1; self.sl_calls.append(price); return {"algoId": f"SL{self._n}"}
    def place_take_profit_market(self, sym, side, price, close_position=True, qty=None):
        self.tp_calls += 1; return {"algoId": "TP1"}
    def cancel_all(self, s): return {}


def cand(side="LONG", entry=100, stop=98, tp=110):
    return Candidate(symbol="TESTUSDT", side=side, entry=entry, stop=stop, tp=tp,
                     rr=3.0, score=8, atr_pct=2.0, status="FRESH", regime="RANGE",
                     bias="BOTH", tape_verdict="CONFIRM", tape_score=3.5)


def main():
    cfg = get_cfg()
    cfg._d["dry_run"] = False
    cfg._d["mode"] = "testnet"
    cfg._d["notion"] = {"enabled": False}
    cfg._d["exits"] = {"trailing_enabled": True, "breakeven_at_r": 0.6, "trail_after_r": 1.5,
                       "trail_distance_r": 1.0, "min_sl_move_pct": 0.05, "notify_moves": False,
                       "book_tp": {"enabled": False}}
    cfg._d["entry"] = {"maker_enabled": True, "maker_timeout_s": 0.06, "maker_poll_s": 0.01}
    cfg._d["brain"] = {}   # pretrade/guardian kapalı (enter brain çağırmaz zaten)
    learner = Learner(cfg, None)
    day = _utcday()
    eq = 50.0
    n_ok = n_fail = 0

    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += bool(cond); n_fail += (not cond)

    def run(scenario, side="LONG"):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        b = FakeMakerBinance(100.0, scenario=scenario)
        c = cand(side=side)
        gr = risk.gate(cfg, store, b, c, eq, 100.0, day, learner)
        assert gr.ok, f"gate açılmadı: {gr.reason}"
        tid = executor.enter(cfg, b, store, c, gr.sizing, 100.0, day)
        row = store.db.execute("SELECT qty, entry, sl, status FROM trades WHERE id=?", (tid,)).fetchone()
        return b, gr, row

    print("--- 1) TAM MAKER DOLUM ---")
    b, gr, row = run("maker_full")
    chk("SL TAM 1 kez kondu (çıplak pencere yok)", len(b.sl_calls) == 1)
    chk("TP 1 kez kondu", b.tp_calls == 1)
    chk("market emri YOK (saf maker)", len(b.market_calls) == 0)
    chk("kaydedilen qty = istenen", abs(row["qty"] - gr.sizing.qty) < 1e-6)
    chk("giriş fiyatı = maker bid (99.9, markttan iyi)", abs(row["entry"] - 99.9) < 0.01)

    print("--- 2) DOLMADI → TAKER FALLBACK ---")
    b, gr, row = run("maker_none")
    chk("limit iptal edildi", b.cancelled_orders == ["L1"])
    chk("market ile tamamlandı (1 market emri)", len(b.market_calls) == 1)
    chk("SL TAM 1 kez (taker yolunda)", len(b.sl_calls) == 1)
    chk("tam boyut korundu (işlem kaçmadı)", abs(row["qty"] - gr.sizing.qty) < 1e-6)
    chk("giriş fiyatı = mark (taker)", abs(row["entry"] - 100.0) < 0.01)

    print("--- 3) KISMİ DOLUM → TAKER TOPUP ---")
    b, gr, row = run("maker_partial")
    chk("SL TAM 1 kez (ilk kısmi dolumda, çıplak değil)", len(b.sl_calls) == 1)
    chk("kalan market ile tamamlandı", len(b.market_calls) == 1)
    chk("tam boyut korundu (kısmi+market = tam)", abs(row["qty"] - gr.sizing.qty) < 1e-6)
    chk("giriş fiyatı maker-taker arası (99.9<x<100)", 99.9 - 1e-9 <= row["entry"] <= 100.0 + 1e-9)

    print("--- 4) GTX REDDİ → TAKER ---")
    b, gr, row = run("gtx_reject")
    chk("market'e düşüldü", len(b.market_calls) == 1)
    chk("SL TAM 1 kez", len(b.sl_calls) == 1)
    chk("tam boyut", abs(row["qty"] - gr.sizing.qty) < 1e-6)

    print("--- 5) SHORT tarafı tam-maker (yön doğruluğu) ---")
    b, gr, row = run("maker_full", side="SHORT")
    chk("SHORT maker ask'e kondu (100.1)", b.limit_calls and abs(b.limit_calls[0][2] - 100.1) < 0.01)
    chk("SL 1 kez, TP 1 kez", len(b.sl_calls) == 1 and b.tp_calls == 1)

    print("--- 6) maker KAPALI → klasik market (davranış birebir) ---")
    cfg._d["entry"]["maker_enabled"] = False
    b, gr, row = run("maker_full")
    chk("maker kapalıyken limit YOK", len(b.limit_calls) == 0)
    chk("market ile açıldı + SL 1 + TP 1", len(b.market_calls) == 1 and len(b.sl_calls) == 1 and b.tp_calls == 1)
    cfg._d["entry"]["maker_enabled"] = True

    print(f"\n{'='*40}\nSONUÇ: {n_ok} geçti, {n_fail} kaldı")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
