#!/usr/bin/env python3
"""Canlı API anahtarını GÜVENLE doğrular — SADECE OKUMA (bakiye/pozisyon), HİÇ emir göndermez.

mode'u live'a zorlar ama dry_run açık kalır. (available_usdt/positions imzalı GET'ler — emir değil.)
Anahtar + IP-whitelist + Futures-aktif + bakiye'yi tek seferde doğrular.
"""
import sys
from cfs_trader.cfg import get
from cfs_trader.binance import Binance

cfg = get()
cfg._d["mode"] = "live"
cfg._d["dry_run"] = True          # canlı anahtar, ama sadece okuma
b = Binance(cfg)
k, s = cfg.api_keys()
if not (k and s):
    print("❌ Canlı API anahtarı secrets.env'de YOK (BINANCE_API_KEY / BINANCE_API_SECRET).")
    sys.exit(1)

print(f"anahtar: {k[:4]}...{k[-4:]} | base: {cfg.base_url}")
try:
    b.sync_time()
    bal = b.available_usdt()
    pos = b.positions()
    print(f"✅ ANAHTAR ÇALIŞIYOR | Futures availableUSDT: {bal:.2f}")
    print(f"açık pozisyon: {len(pos)}")
    if bal < 5:
        print("⚠️  Bakiye <$5 — Spot→Futures cüzdanına USDT transfer ettin mi?")
    elif bal < 45:
        print(f"⚠️  Bakiye ~{bal:.0f} (beklenen ~50). Devam edilebilir; risk config'teki budget=50'ye göre.")
    else:
        print("✅ Bakiye yeterli.")
    print("\n→ Doğrulama tamam. Canlıya almak için onay ver.")
except Exception as e:
    print(f"❌ Anahtar/IP doğrulaması BAŞARISIZ: {e!r}")
    print("Kontrol et: (1) Enable Futures açık mı  (2) IP-whitelist = 162.55.179.183 doğru mu  "
          "(3) anahtar/secret tam kopyalandı mı  (4) anahtar yeni, aktif mi")
    sys.exit(2)
