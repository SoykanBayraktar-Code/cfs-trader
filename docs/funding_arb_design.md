# Funding-Arb / Delta-Nötr Motoru — Tasarım (2026-06-25)

## Neden (kanıt)
cfs-trader yön-botu komisyon sonrası en iyi ~başabaş (5R swing +0.003R; scalp −0.032R = kaybeden).
**Funding-arb backtest'i (son 30g, delta-nötr, %0.20 roundtrip fee) GERÇEK pozitif beklenti gösterdi:**
ortalama ~+%15/yıl (ideal yön), SYN +%113, TRX/TON +%30, majörler +%1.5–2.5. Fiyat-NÖTR.
Bütün seansta bulunan TEK yapısal edge. (script: /tmp/fund_bt.py)

## Strateji
**Delta-nötr funding hasadı (cash-and-carry):** funding ödeyen tarafın TERSİNDE dur, funding TOPLA, fiyat riskini hedge'le.
- funding POZİTİF (longlar öder) → **perp SHORT + spot LONG** → funding al, fiyat-nötr
- funding NEGATİF (shortlar öder) → **perp LONG + spot SHORT** → funding al (spot-short borrow gerekir)

## ⚠️ Execution kısıtları (DÜRÜST — backtest'in göstermediği)
1. **Spot-short zorluğu:** Yüksek getiriler (SYN %113) NEGATİF-funding coinlerde = spot SHORT gerekir → küçük altlarda borrow YOK/pahalı. **İlk faz yalnız POZİTİF-funding + spot-long** (borrow gerekmez).
2. **Funding flip:** funding ters dönerse pozisyon ters tarafa düşer → rebalance (ek komisyon). İzle + eşik-bazlı çık/çevir.
3. **Basis riski:** perp-spot fiyat farkı; settlement'ta yakınsar ama arada dalgalanır. Likidasyon tamponu şart (perp bacağı).
4. **Sermaye verimliliği:** delta-nötr = 2 bacak (spot+perp) → sermaye 2'ye bölünür; getiri buna göre.

## Mimari (ayrı motor — yön-botuna DOKUNMAZ)
- `funding/` ayrı paket (cfs-trader yön-botundan bağımsız; kendi DB'si `funding.db`)
- **Tarayıcı:** coinalyze funding (zaten bağlı, çapraz-borsa) + Binance fundingRate → |funding| yüksek + tutarlı coinler
- **Filtre:** spot likidite + borrow uygunluğu (Faz1: yalnız spot-long/pozitif-funding) + perp OI eşiği
- **Pozisyon yöneticisi:** delta-nötr çift bacak aç (spot + perp ters), notional eşitle, likidasyon tamponu
- **Funding takip:** 8h funding tahsilatı + rebalance tetikleyici (funding eşik-altı/flip → çık)
- **P&L:** Σ funding − komisyon − basis-drift; günlük rapor
- **Güvenlik:** dry_run paper modu (önce), küçük sermaye, majör-only başlangıç

## Fazlar
- **Faz 0 (yapıldı):** tarihsel backtest → edge doğrulandı (+%15/yıl ideal)
- **Faz 1:** paper motor — pozitif-funding majörler (spot-long+perp-short), gerçek funding tahsilatını simüle et, 1-2 hafta ölç
- **Faz 2:** küçük gerçek sermaye, majör-only, manuel onay
- **Faz 3:** negatif-funding altlar (spot-short borrow çözülürse) + otomatik rebalance

## Dürüst beklenti
Majör-only/pozitif-funding güvenli versiyon **mütevazı** (~%2–5/yıl, piyasa-nötr). Yüksek getiri (altlar)
execution-zor. Ama yön-tahminsiz GERÇEK edge — "az ama kesin" > "çok ama kumar". Yön-botunun aksine
ölçeklenebilir ve gece-güvenli (fiyat-nötr).
