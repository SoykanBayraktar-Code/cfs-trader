# cfs-trader

`crypto-futures-scan` sinyallerini **Binance USD-M perpetual**'da otomatik yürüten execution botu.
Tarama skill'i DOKUNULMAZ — sinyal kütüphanesi olarak import edilir. Bot emir gönderir; her gerçek
emir **config (mode/dry_run) → risk kapısı → Testnet smoke** zincirinden geçer.

> ⚠️ Kullanıcının kendi forward-test verisi (paper_log 872 işlem) sistemi **net negatif** gösteriyor.
> Bu bot $50 ile "execution altyapısı + gerçek dolum verisi" kanıtı olarak çalıştırılıyor — kâr garantisi yok.

## Güvenlik mimarisi (3 kat)
1. **`mode`**: `testnet` (sahte para) | `live` (gerçek para). `base_url` buna göre.
2. **`dry_run`**: `true` iken `binance.py` HİÇBİR emir göndermez (sentetik yanıt). Çağıran bug'lasa bile emir kaçmaz.
3. **`risk.py` kapısı**: kill-switch durumları → max-eşzamanlı → tape=CONFIRM → max-SL → boyutlandırma. Hepsi geçmezse giriş yok.

## Risk rayları (config.yaml → risk)
| Ray | Değer |
|---|---|
| leverage | 5x |
| risk_per_trade_pct | %50 tavan (margin+SL ile pratikte ~%7-15) |
| max_concurrent | 1 |
| daily_max_loss_pct | %25 ($12.5) → o gün dur + düzleştir |
| max_consecutive_losses | 5 → dur + Telegram, manuel devam |
| max_sl_pct | %12 (geniş-SL = likidasyon riski → red) |

## Kurulum (sunucu, /root/cfs-trader)
```bash
cp secrets.env.example secrets.env   # doldur (testnet anahtarları + Telegram)
python3 tests/test_offline.py        # para-mantığı (ağsız) — 13/13 geçmeli
```

## Fazlar
### Faz 1 — Testnet smoke (GERÇEK PARAYA GEÇMEDEN ÖNCE ZORUNLU)
```bash
# secrets.env: BINANCE_TESTNET_API_KEY/SECRET dolu olmalı
# config.yaml: mode: testnet
python3 smoke_testnet.py BTCUSDT     # emir/bracket/iptal/reconciliation testnet'te uçtan uca
# "✅ SMOKE GEÇTİ" görmeden Faz 2'ye GEÇME.
```

### Paper (veri toplama — gerçek para yok)
```bash
# config.yaml: mode: testnet, dry_run: true
python3 run.py --once                # tek tik
python3 run.py                        # sürekli 15dk döngü (paper)
python3 run.py --status              # açık pozisyon + günlük durum
```

### Faz 2 — Mikro-gerçek ($50)
```bash
# 1) Binance'te API anahtarı: withdraw KAPALI + IP-whitelist 162.55.179.183 + Futures aç
# 2) secrets.env: BINANCE_API_KEY/SECRET
# 3) config.yaml: mode: live   + dry_run: false
# 4) servisi kur:
cp deploy/cfs-trader.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now cfs-trader
journalctl -u cfs-trader -f          # canlı log
```

### Faz 3 — Öğrenme
`config.yaml: learner.enabled: true` — yeterli canlı işlem birikince (rejim×yön×sinyal-tipi)
negatif-beklenti kombinasyonlarını bastırır.

## Acil durdurma
```bash
systemctl stop cfs-trader            # botu durdur
# açık pozisyon varsa Binance'ten elle kapat veya:
python3 -c "from cfs_trader.loop import Ctx; from cfs_trader import position_manager as pm; c=Ctx(); pm.flatten_all(c.cfg,c.binance,c.store,'MANUAL',c.notifier)"
```
