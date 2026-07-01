# cfs-trader — Canlı İzleme Dashboard Tasarımı

- **Tarih:** 2026-07-01
- **Amaç:** Botun pipeline akışını (scan→tape→gate→giriş→trailing→çıkış), canlı verileri ve performans grafiklerini gerçek zamanlı, her yerden (telefon/PC) izlemek.
- **İlke:** Trading botuna ASLA dokunma. Ayrı, izole, salt-okuma servis.

## Mimari

- **İzole systemd servis:** `cfs-dashboard.service` — kendi process'i. Bot process'inden tamamen bağımsız; dashboard çökse/restart olsa bot etkilenmez.
- **Sıfır yeni bağımlılık:** backend = Python stdlib `http.server` (ThreadingHTTPServer). Flask/FastAPI yok (4GB ARM'de hafif, botun zero-dep felsefesi).
- **Frontend:** tek self-contained `dashboard/index.html` — vanilla JS + Chart.js (CDN). JSON endpoint'lerini poll'lar, canlı render. Karanlık/terminal-trading teması.
- **Konum:** repo'da `dashboard/` (server.py + index.html).

## Veri Kaynakları (hepsi SALT-OKUMA — bota yazma yok)

- **SQLite** `data/trader_live.db` (WAL → eşzamanlı okuma güvenli): trades, daily_state, decisions, learning, brain_decisions.
- **Binance API** (botun `binance.py`'sini reuse): wallet_balance, positions, mark price → canlı equity, uPnL, güncel fiyat.
- **journald** (`journalctl -u cfs-trader`): son scan tick'leri, tape sonuçları, gate RED sebepleri (parse + cache).
- **config.yaml:** güncel risk rayları, trading_paused, eşikler.

## Güvenlik

- `secrets.env` → `DASHBOARD_TOKEN` (rastgele, koda/git'e girmez).
- Tüm rotalar `/d/<TOKEN>/...` altında; geçersiz/eksik token → 404.
- Firewall (ufw): tek port (8787) açık. Sadece token bilen erişir.

## Paneller (A–K)

- **A) Üst durum çubuğu:** servis▲, mod (LIVE/PAUSED), canlı equity, bugünkü PnL, açık poz sayısı, kill-switch durumu, son scan.
- **B) PİPELİNE AKIŞ DİYAGRAMI (merkez):** SCAN→TAPE→GATE(9 kapı)→ENTER→MANAGE→EXIT; her aşamada canlı sayaç + son olay. GATE kapıları geçti/reddetti sayısı. trading_paused'da akış "durduruldu" gösterir.
- **C) Açık pozisyonlar (canlı):** sembol/yön/giriş/güncel-fiyat/SL/TP/trail/uPnL(R+USDT)/risk/süre.
- **D) Aktivite akışı:** son gate kararları (GİRİŞ/RED sebepleri) + son scan tick'leri (aday kırılımı).
- **E) Risk rayları:** config (risk/cap/kill-switch/ardışık) + toplam maruziyet vs cap barı + gün zararı vs kill-switch barı.
- **F) Equity/PnL eğrisi:** kümülatif R+USDT zaman serisi (line chart).
- **G) Kırılım barları:** yön (LONG/SHORT), rejim (UP/DOWN/RANGE), kaynak (FRESH/pullback/momentum/oi_surge) — net R + kazanma%.
- **H) Funding etkisi (#4):** kümülatif funding+komisyon; gerçek-net vs tahmin farkı.
- **I) Kazanma kartları:** toplam işlem, kazanma%, ort R, en iyi/kötü, ort tutuş, profit factor.
- **J) Son işlemler tablosu:** son 20 (sembol/yön/giriş-çıkış/R/PnL-funding-dahil/kaynak/çıkış-sebebi).
- **K) Shadow/brain (katlanabilir):** derivs/pnd/ls shadow, brain konviksiyon istatistikleri.

## JSON API Endpoint'leri (hepsi `/d/<token>/api/...`)

`status` · `pipeline` · `positions` · `activity` · `risk` · `performance` · `trades` · `shadow`

## Yenileme

- Üst çubuk + pozisyon + pipeline: ~5sn.
- Grafikler: ~15sn.
- Tarih filtresi: bugün / 7g / tümü.

## Test & Deploy Diretmi

Her adım: scratchpad Edit → scp → sunucuda test (curl endpoint'ler) → systemd servis → firewall port → tarayıcı doğrula → commit + GitHub push. Canlı bot ELLENMEZ.
