"""wsklines — Faz 1-A: WS-beslemeli bellek-içi kline deposu (REST-uyumlu) + güvenli servis.

NEDEN: scan'in ağır REST kline çekimini WS deposundan beslemek → (1) REST weight ~sıfır
  (rate-limit'e takılma), (2) anlık EN TAZE veri (forming bar canlı). Motor DOKUNULMAZ.
GÜVENLİK KATMANLARI:
  - enabled: depoyu çalıştırır (yalnız veri toplar, karara dokunmaz).
  - serve:  datahub+mtf.get_klines_mtf'i depodan besler — REST FALLBACK hep açık; config ile anında geri al.
  - throttle'lı backfill (rate-limit güvenliği) + reconnect'te yalnız BOŞLUK olan stream'leri çek.
  - ayrı thread + FAIL-SAFE: WS hatası trade döngüsünü ETKİLEMEZ; eksikte REST'e düşer.
  - WS kline → REST 12-alan dizisine birebir map (parity kanıtlandı: 60/60, 0 fark).
Bağımlılık: websockets + stdlib. no-sandbox şart.
"""
import asyncio
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import websockets

FAPI = "https://fapi.binance.com"
WS = "wss://fstream.binance.com/ws"
TFS = ("5m", "15m", "1h", "4h")
TF_SEC = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
MAXBARS = 150


def _rest_klines(sym, tf, limit=120, timeout=10):
    url = f"{FAPI}/fapi/v1/klines?symbol={urllib.parse.quote(sym)}&interval={tf}&limit={limit}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ev_to_arr(k):
    """WS kline 'k' → REST 12-alan dizisi (birebir tip/sıra)."""
    return [k["t"], k["o"], k["h"], k["l"], k["c"], k["v"], k["T"], k["q"], k["n"], k["V"], k["Q"], "0"]


def liquid_universe(cfg, min_vol=10_000_000):
    """Depo evreni: scan ile aynı likit USDT-perp listesi (tokenize-hisse hariç). FAIL-SAFE→[]."""
    try:
        from . import signals
        with signals._engine_cwd(cfg.engine_path):
            import datahub
            try:
                from coil_scanner import NON_CRYPTO
            except Exception:
                NON_CRYPTO = set()
            uni = datahub.get_ticker(min_vol=min_vol, exclude=NON_CRYPTO)
        return [r["symbol"] for r in uni]
    except Exception:
        return []


class KlineStore:
    def __init__(self, symbols, tfs=TFS, backfill_rate=5, log=None):
        # yalnız standart ASCII USDT-perp (币安人生USDT gibi tuhaf semboller → REST fallback'e bırakılır)
        self.symbols = [s.upper() for s in symbols if re.match(r"^[A-Z0-9]+USDT$", s.upper())]
        self.tfs = tuple(tfs)
        self.backfill_rate = max(1, int(backfill_rate))   # saniyede max REST backfill çağrısı
        self._log = log or (lambda m: None)
        self._closed = {}    # (SYM,tf) -> [arr...] kapanmış
        self._forming = {}   # (SYM,tf) -> arr | None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._th = None
        self.last_msg = 0.0

    # ---- backfill (throttle'lı; rate-limit güvenliği) ----
    def _is_stale(self, key):
        cl = self._closed.get(key)
        if not cl:
            return True
        tf = key[1]
        last_open = cl[-1][0] / 1000.0
        return (time.time() - last_open) > TF_SEC.get(tf, 300) * 2  # >2 bar geride = boşluk

    def backfill(self, only_stale=False):
        delay = 1.0 / self.backfill_rate
        n = 0
        for s in self.symbols:
            for tf in self.tfs:
                if self._stop.is_set():
                    return
                key = (s, tf)
                if only_stale and not self._is_stale(key):
                    continue
                try:
                    kl = _rest_klines(s, tf, 120)
                    rest_closed = [list(x) for x in kl[:-1]]
                    last_open = rest_closed[-1][0] if rest_closed else 0
                    with self._lock:
                        # MERGE: REST derin geçmiş + WS'in bu sırada yakaladığı DAHA YENİ barlar (üzerine yazma)
                        newer = [b for b in self._closed.get(key, []) if b[0] > last_open]
                        self._closed[key] = rest_closed + newer
                        if not self._forming.get(key):   # WS forming'i set ettiyse dokunma
                            self._forming[key] = list(kl[-1])
                    n += 1
                except Exception:
                    pass
                time.sleep(delay)   # throttle → weight güvenli
        self._log(f"[wsklines] backfill {'(gap)' if only_stale else '(full)'} {n} stream")

    def _apply(self, sym, tf, k):
        arr = _ev_to_arr(k)
        key = (sym, tf)
        with self._lock:
            if k["x"]:
                cl = self._closed.setdefault(key, [])
                if not cl or arr[0] > cl[-1][0]:
                    cl.append(arr)
                    if len(cl) > MAXBARS:
                        del cl[0:len(cl) - MAXBARS]
                elif arr[0] == cl[-1][0]:
                    cl[-1] = arr
                self._forming[key] = None
            else:
                self._forming[key] = arr

    async def _conn(self, chunk, cid):
        """Tek WS bağlantısı (≤~150 stream — Binance futures bağlantı-başı sınırı)."""
        subs = [json.dumps({"method": "SUBSCRIBE", "params": chunk[i:i + 150], "id": cid * 100 + i})
                for i in range(0, len(chunk), 150)]
        while not self._stop.is_set():
            try:
                async with websockets.connect(WS, ping_interval=15, close_timeout=5, max_queue=4096) as ws:
                    for s in subs:
                        await ws.send(s)
                        await asyncio.sleep(0.3)
                    self._log(f"[wsklines] conn{cid} bağlı ({len(chunk)} stream)")
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        self.last_msg = time.time()
                        msg = json.loads(raw)
                        d = msg.get("data") or msg
                        if isinstance(d, dict) and d.get("e") == "kline":
                            self._apply(d["s"], d["k"]["i"], d["k"])
            except Exception as e:
                if self._stop.is_set():
                    break
                self._log(f"[wsklines] conn{cid} koptu ({type(e).__name__}) — 3s reconnect")
                await asyncio.sleep(3)

    async def _gap_loop(self):
        """İlk full backfill (eşzamanlı), sonra ~5dk'da bir gap-backfill (reconnect boşluklarını kapatır)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.backfill, False)   # full
        while not self._stop.is_set():
            for _ in range(60):
                if self._stop.is_set():
                    return
                await asyncio.sleep(5)
            await loop.run_in_executor(None, self.backfill, True)   # yalnız boşluk

    async def _run(self):
        params = [f"{s.lower()}@kline_{tf}" for s in self.symbols for tf in self.tfs]
        CHUNK = 150
        chunks = [params[i:i + CHUNK] for i in range(0, len(params), CHUNK)]
        self._log(f"[wsklines] {len(params)} stream / {len(chunks)} bağlantı (≤{CHUNK}) + gap-backfill")
        await asyncio.gather(self._gap_loop(), *[self._conn(c, i) for i, c in enumerate(chunks)])

    def _thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception:
            pass

    def start(self):
        self._th = threading.Thread(target=self._thread, daemon=True, name="wsklines")
        self._th.start()
        return self

    def stop(self):
        self._stop.set()

    def get(self, sym, tf, limit=120):
        """REST get_klines_mtf gibi: son (limit-1) kapanmış + 1 forming bar. Hazır değilse None."""
        key = (sym.upper(), tf)
        with self._lock:
            cl = self._closed.get(key)
            if not cl:
                return None
            fm = self._forming.get(key)
            bars = cl[-(limit - 1):] + ([list(fm)] if fm else [])
            return [list(b) for b in bars]

    def coverage(self):
        with self._lock:
            ready = sum(1 for v in self._closed.values() if v)
            return {"streams": len(self.symbols) * len(self.tfs), "ready": ready,
                    "fresh": sum(1 for k in self._closed if not self._is_stale(k))}


# ---- serve: motoru depodan besle (REST FALLBACK'li monkeypatch) ----
def install_serve(cfg, store, log=None):
    """datahub.get_klines_mtf VE mtf.get_klines_mtf'i depodan besleyen sürümle değiştirir (REST fallback).
    mtf, ismi import-anında bağladığı için HER İKİSİ de patch'lenmeli. Geri-dönüş: config serve:false + restart."""
    log = log or (lambda m: None)
    from . import signals
    from collections import defaultdict
    with signals._engine_cwd(cfg.engine_path):
        import datahub
        import mtf
    _orig = datahub.get_klines_mtf

    def _patched(symbols, tfs=datahub.DEFAULT_TFS, limit=100, max_workers=20):
        out, miss = {}, []
        for s in symbols:
            d = {}
            for tf in tfs:
                bars = store.get(s, tf, limit)
                if bars and len(bars) >= min(limit, 50):   # yeterli derinlik yoksa REST'e düş
                    d[tf] = bars
                else:
                    miss.append((s, tf))
            if d:
                out[s] = d
        if miss:   # REST FALLBACK — eksik (sym,tf) çiftlerini orijinalden çek
            mb = defaultdict(list)
            for s, tf in miss:
                mb[s].append(tf)
            for s, tfs_ in mb.items():
                try:
                    r = _orig([s], tfs=tuple(tfs_), limit=limit, max_workers=max_workers)
                    for tf in tfs_:
                        out.setdefault(s, {})[tf] = (r.get(s) or {}).get(tf)
                except Exception:
                    pass
        return out

    datahub.get_klines_mtf = _patched
    mtf.get_klines_mtf = _patched   # KRİTİK: mtf kendi bağladığı ismi kullanır
    log("[wsklines] serve AKTİF — datahub+mtf.get_klines_mtf depodan besleniyor (REST fallback açık)")
    return _orig


# ---- parity self-test (standalone) ----
def _parity(symbols, secs=45):
    store = KlineStore(symbols, log=print).start()
    print(f"depo başladı: {len(symbols)} sembol × {TFS} — {secs}s topla...")
    time.sleep(secs)
    print("coverage:", store.coverage())
    fields = {4: "close", 7: "quoteVol", 10: "takerBuyQuote"}
    total = match = mism = 0
    for s in [x.upper() for x in symbols]:
        for tf in TFS:
            wb = store.get(s, tf, 120)
            try:
                rest = _rest_klines(s, tf, 120)
            except Exception as e:
                print(f"  {s} {tf}: REST hata {e!r}"); continue
            if not wb:
                print(f"  {s} {tf}: depo BOŞ"); continue
            wsc = {b[0]: b for b in wb[:-1]}
            ok = bad = 0
            for rb in rest[:-1][-5:]:
                m = wsc.get(rb[0])
                if m is None:
                    bad += 1; continue
                same = all(abs(float(m[i]) - float(rb[i])) < 1e-9 for i in fields)
                ok += same; bad += (not same)
            total += ok + bad; match += ok; mism += bad
            print(f"  {'✅' if bad == 0 else '❌'} {s:<10} {tf:<4}: {ok} eşleşti / {bad} fark")
    store.stop()
    print(f"\n=== PARITY: {match}/{total} eşleşti, {mism} fark ===")
    return mism == 0


if __name__ == "__main__":
    import sys
    syms = (sys.argv[1].split(",") if len(sys.argv) > 1 else ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 45
    sys.exit(0 if _parity(syms, secs) else 1)
