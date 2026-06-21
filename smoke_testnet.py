#!/usr/bin/env python3
"""smoke_testnet.py — FAZ 1 KAPISI. Binance Futures TESTNET'te emir mantığını uçtan uca doğrular.

Gerçek-para kodunu gerçek hesaba bağlamadan ÖNCE bu GEÇMELİ. Sahte para (testnet faucet).
Adımlar: time-sync → bakiye → exchangeInfo/filtre → kaldıraç → MARKET giriş → SL+TP bracket →
pozisyon teyit → market-close + iptal → pozisyon düz mü.

Çalıştırma (testnet anahtarları secrets.env'de dolu olmalı):
  python3 smoke_testnet.py [SEMBOL]      # varsayılan BTCUSDT
GEREKSİNİM: mode=testnet. Bu script dry_run'ı KAPATIR (gerçek testnet emri gönderir).
"""
import sys
import time
from cfs_trader.cfg import get as get_cfg
from cfs_trader.binance import Binance, BinanceError

OK, FAIL = "✅", "❌"


def main():
    sym = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT").upper()
    cfg = get_cfg()
    if cfg.is_live:
        print(f"{FAIL} mode=live! Smoke test SADECE testnet'te çalışır. config.yaml mode:testnet yap.")
        sys.exit(1)
    # smoke test gerçek testnet emri gönderir → dry_run'ı zorla kapat
    cfg._d["dry_run"] = False
    b = Binance(cfg)
    if not (b.key and b.secret):
        print(f"{FAIL} testnet API anahtarı yok (secrets.env: BINANCE_TESTNET_API_KEY/SECRET).")
        sys.exit(1)

    print(f"=== TESTNET SMOKE — {sym} @ {cfg.base_url} ===")
    steps_ok = True

    def step(name, fn):
        nonlocal steps_ok
        try:
            r = fn()
            print(f"{OK} {name}: {r}")
            return r
        except Exception as e:
            print(f"{FAIL} {name}: {e!r}")
            steps_ok = False
            return None

    step("time-sync (offset ms)", b.sync_time)
    bal = step("bakiye (availableUSDT)", b.available_usdt)
    f = step("exchangeInfo/filtre", lambda: b.filters(sym))
    if not f:
        print(f"{FAIL} filtre alınamadı, durdu."); sys.exit(1)

    mark = step("mark fiyat", lambda: b.mark_price(sym))
    step("kaldıraç=2x", lambda: b.set_leverage(sym, 2))

    # min-notional'ı geçecek küçük qty (testnet bakiyesi bol)
    min_notional = b.min_notional(sym)
    target_notional = max(min_notional * 1.1, 20)
    qty = b.round_qty(sym, target_notional / mark)
    print(f"   → qty={qty} (notional ~{qty*mark:.2f}, min {min_notional})")

    entry = step("MARKET giriş (BUY)", lambda: b.place_market(sym, "BUY", qty))
    time.sleep(1.5)
    pos = step("pozisyon teyit", lambda: b.positions(sym))

    # bracket: SL %2 altı, TP %3 üstü
    sl_price = b.round_price(sym, mark * 0.98)
    tp_price = b.round_price(sym, mark * 1.03)
    step("SL (Algo STOP_MARKET, closePosition)", lambda: b.place_stop_market(sym, "SELL", sl_price).get("algoId"))
    step("TP (Algo TAKE_PROFIT_MARKET, closePosition)", lambda: b.place_take_profit_market(sym, "SELL", tp_price).get("algoId"))
    oo = step("açık algo emirler (SL+TP görünmeli)",
              lambda: [(o["orderType"], o["triggerPrice"]) for o in b.open_algo_orders(sym)])
    if not oo or len(oo) < 2:
        steps_ok = False
        print(f"{FAIL} SL+TP algo emirleri görünmedi (beklenen 2, gelen {len(oo or [])})")

    # temizlik: pozisyonu kapat + emirleri iptal
    step("pozisyon kapat (reduceOnly MARKET SELL)", lambda: b.place_market(sym, "SELL", qty, reduce_only=True))
    step("tüm emirleri iptal", lambda: b.cancel_all(sym))
    time.sleep(1.5)
    final = step("pozisyon düz mü (boş liste beklenir)", lambda: b.positions(sym))

    print("\n" + ("=" * 48))
    if steps_ok and not final:
        print(f"{OK} SMOKE GEÇTİ — emir/bracket/iptal/reconciliation çalışıyor. Faz 2'ye hazır.")
    else:
        print(f"{FAIL} SMOKE BAŞARISIZ — yukarıdaki adımı düzelt. GERÇEK PARAYA GEÇME.")
        sys.exit(2)


if __name__ == "__main__":
    main()
