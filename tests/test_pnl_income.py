#!/usr/bin/env python3
"""AUDIT #4 — ground-truth PnL (funding dahil) birim testi (ağsız, deterministik).

Doğrular: flatten() canlıda borsa income'ından GERÇEK net realize'i (REALIZED_PNL+COMMISSION+FUNDING_FEE)
kullanır + funding_usdt/pnl_src kaydeder; income yoksa/kapanış-kaydı yoksa fiyat-tahminine fail-safe düşer.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader.cfg import get as get_cfg
from cfs_trader.store import Store
from cfs_trader import executor


class FakeBinance:
    """Canlı-mod (dry_run=False) sahte borsa — yalnız flatten'ın SL-yolunda ihtiyaç duyduğu metotlar."""
    def __init__(self, income_records, price=100.0):
        self.dry_run = False
        self._income = income_records
        self.price = price
        self.income_calls = 0
    def income(self, symbol=None, start_ms=None, income_type=None, limit=200):
        self.income_calls += 1
        return list(self._income)
    def mark_price(self, s): return self.price
    def place_market(self, *a, **k): return {"orderId": "M1"}
    def cancel_all(self, s): return {}


def _cfg():
    cfg = get_cfg()
    cfg._d["dry_run"] = False           # income yolu YALNIZ canlıda
    cfg._d["mode"] = "testnet"
    cfg._d["brain"] = {}                # postmortem KAPALI (claude CLI çağrısı yok)
    cfg._d["notion"] = {"enabled": False}
    return cfg


def _open(store, side="LONG", entry=100.0, qty=1.0, risk=5.0):
    return store.open_trade(symbol="TESTUSDT", side=side, entry=entry, qty=qty, sl=98.0, tp=106.0,
                            leverage=5, risk_usdt=risk, regime="RANGE", signal_type="FRESH",
                            tape_verdict="CONFIRM", mode="testnet", dry_run=0,
                            sl_init=98.0, peak_price=entry, trail_state="INIT")


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += cond; n_fail += (not cond)

    # ---- 1) INCOME YOLU: gerçek net = REALIZED_PNL + COMMISSION + FUNDING_FEE ----
    inc = [
        {"incomeType": "COMMISSION", "income": "-0.12"},   # giriş komisyonu
        {"incomeType": "FUNDING_FEE", "income": "-0.15"},  # tutuş boyunca funding (ödendi)
        {"incomeType": "REALIZED_PNL", "income": "10.00"}, # kapanış gross
        {"incomeType": "COMMISSION", "income": "-0.18"},   # çıkış komisyonu
    ]
    net = 10.00 - 0.12 - 0.15 - 0.18   # = 9.55
    store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    cfg = _cfg()
    b = FakeBinance(inc)
    tid = _open(store)
    trade = store.get_trade(tid)
    pnl, r_mult, st = executor.flatten(cfg, b, store, trade, 110.0, "SL", None)
    row = store.get_trade(tid)
    chk(f"income-net PnL ({pnl:.4f} ≈ {net:.4f})", abs(pnl - net) < 1e-6)
    chk(f"pnl_src='income' (={row['pnl_src']})", row["pnl_src"] == "income")
    chk(f"funding_usdt kaydedildi (-0.15) (={row['funding_usdt']})", abs((row["funding_usdt"] or 0) + 0.15) < 1e-6)
    chk(f"fees = |commission| (0.30) (={row['fees_usdt']})", abs((row["fees_usdt"] or 0) - 0.30) < 1e-6)
    chk(f"R = net/risk ({r_mult:.3f} ≈ {net/5.0:.3f})", abs(r_mult - net / 5.0) < 1e-3)
    chk("DB pnl_usdt = net", abs((row["pnl_usdt"] or 0) - round(net, 4)) < 1e-6)

    # ---- 2) FUNDING net'i DEĞİŞTİRİR: funding olmadan vs ile ----
    inc2 = [{"incomeType": "REALIZED_PNL", "income": "5.00"},
            {"incomeType": "COMMISSION", "income": "-0.20"},
            {"incomeType": "FUNDING_FEE", "income": "+0.40"}]   # funding ALINDI (+)
    store2 = Store(os.path.join(tempfile.mkdtemp(), "t2.db"))
    b2 = FakeBinance(inc2)
    tid2 = _open(store2, risk=5.0)
    pnl2, _, _ = executor.flatten(_cfg(), b2, store2, store2.get_trade(tid2), 105.0, "TP", None)
    chk(f"funding net'e dahil (5.00-0.20+0.40=5.20, ={pnl2:.4f})", abs(pnl2 - 5.20) < 1e-6)

    # ---- 3) FALLBACK: income BOŞ → fiyat-tahmini, pnl_src='tahmin', funding=None ----
    store3 = Store(os.path.join(tempfile.mkdtemp(), "t3.db"))
    b3 = FakeBinance([])   # hiç income kaydı yok
    tid3 = _open(store3, side="LONG", entry=100.0, qty=1.0)
    est = (110.0 - 100.0) * 1.0 * 1   # +10.0
    pnl3, _, _ = executor.flatten(_cfg(), b3, store3, store3.get_trade(tid3), 110.0, "SL", None)
    row3 = store3.get_trade(tid3)
    chk(f"fallback tahmini PnL (={pnl3:.4f} ≈ {est:.4f})", abs(pnl3 - est) < 1e-6)
    chk(f"pnl_src='tahmin' (={row3['pnl_src']})", row3["pnl_src"] == "tahmin")
    chk(f"funding_usdt None (fallback) (={row3['funding_usdt']})", row3["funding_usdt"] is None)

    # ---- 4) HAS_CLOSE guard: REALIZED_PNL kaydı YOKSA (sadece funding/komisyon) → tahmine düş ----
    inc4 = [{"incomeType": "FUNDING_FEE", "income": "-0.10"},
            {"incomeType": "COMMISSION", "income": "-0.12"}]   # REALIZED_PNL YOK = kapanış henüz işlenmemiş
    store4 = Store(os.path.join(tempfile.mkdtemp(), "t4.db"))
    b4 = FakeBinance(inc4)
    tid4 = _open(store4, side="LONG", entry=100.0, qty=1.0)
    pnl4, _, _ = executor.flatten(_cfg(), b4, store4, store4.get_trade(tid4), 110.0, "SL", None)
    row4 = store4.get_trade(tid4)
    chk(f"has_close False → tahmine düştü (pnl={pnl4:.2f}, src={row4['pnl_src']})",
        abs(pnl4 - 10.0) < 1e-6 and row4["pnl_src"] == "tahmin")

    # ---- 5) _realized_from_income retry: ilk çağrı boş, sonra dolu (has_close beklemesi) ----
    class FlakyB:
        def __init__(self): self.n = 0
        def income(self, symbol=None, start_ms=None, income_type=None, limit=200):
            self.n += 1
            if self.n < 2: return []   # ilk deneme boş
            return [{"incomeType": "REALIZED_PNL", "income": "3.0"}]
    fb = FlakyB()
    res = executor._realized_from_income(fb, "X", 1000.0, tries=3, sleep_s=0.0)
    chk(f"retry: 2. denemede kapanış bulundu (calls={fb.n}, net={res[0] if res else None})",
        res is not None and abs(res[0] - 3.0) < 1e-6 and res[4] and fb.n == 2)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
