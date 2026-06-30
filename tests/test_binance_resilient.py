#!/usr/bin/env python3
"""AUDIT #3 — binance istek katmanı dayanıklılığı (ağsız, deterministik).

Doğrular: 429/418 rate-limit'te backoff+retry, -1021 saat-kaymasında re-sync+retry, ağ hatasında
GET tekrar dener ama POST/DELETE FIRLATIR (çift-emir riski yok), diğer non-200 hata fırlatır.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cfs_trader.binance as bm
from cfs_trader.binance import Binance, BinanceError

bm.time.sleep = lambda *a, **k: None   # testte beklemeyi atla


class Resp:
    def __init__(self, status, text="", js=None, headers=None):
        self.status_code = status; self.text = text; self._js = js if js is not None else {}
        self.headers = headers or {}
    def json(self): return self._js


class FakeSession:
    """script: sırayla dönecek Resp veya fırlatılacak Exception listesi."""
    def __init__(self, script):
        self.script = list(script); self.calls = 0; self.headers = {}
    def _next(self):
        r = self.script[self.calls]; self.calls += 1
        if isinstance(r, Exception): raise r
        return r
    def request(self, method, url, timeout=None): return self._next()
    def get(self, url, params=None, timeout=None): return self._next()


class FakeCfg:
    base_url = "http://venue"
    dry_run = False
    risk = {"min_notional_usdt": 5.0}
    def api_keys(self): return ("k", "s")


def mk(script):
    b = Binance(FakeCfg())
    b._s = FakeSession(script)
    b.sync_time = lambda: setattr(b, "_synced", getattr(b, "_synced", 0) + 1) or 0
    return b


def main():
    import requests
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += cond; n_fail += (not cond)

    # 1) GET 429 → 200 (rate-limit backoff+retry)
    b = mk([Resp(429, headers={"Retry-After": "0"}), Resp(200, js={"ok": 1})])
    out = b._signed("GET", "/x")
    chk(f"429→200 retry başardı (calls={b._s.calls})", out == {"ok": 1} and b._s.calls == 2)

    # 2) signed -1021 → re-sync → 200
    b = mk([Resp(400, text='{"code":-1021,"msg":"timestamp"}'), Resp(200, js={"ok": 2})])
    out = b._signed("GET", "/x")
    chk(f"-1021→re-sync→200 (synced={getattr(b,'_synced',0)}, calls={b._s.calls})",
        out == {"ok": 2} and getattr(b, "_synced", 0) == 1 and b._s.calls == 2)

    # 3) GET ağ hatası → retry → 200
    b = mk([requests.exceptions.ConnectionError("blip"), Resp(200, js={"ok": 3})])
    out = b._signed("GET", "/x")
    chk(f"GET ağ-hatası→retry→200 (calls={b._s.calls})", out == {"ok": 3} and b._s.calls == 2)

    # 4) POST ağ hatası → FIRLAT (retry YOK, çift-emir riski yok)
    b = mk([requests.exceptions.ConnectionError("blip"), Resp(200, js={"ok": 4})])
    try:
        b._signed("POST", "/order")
        chk("POST ağ-hatası FIRLATIR (çift-emir yok)", False)
    except BinanceError:
        chk(f"POST ağ-hatası FIRLATIR (calls={b._s.calls}=1, retry yok)", b._s.calls == 1)

    # 5) diğer non-200 (örn -2010) → FIRLAT
    b = mk([Resp(400, text='{"code":-2010,"msg":"reject"}')])
    try:
        b._signed("POST", "/order"); chk("non-200 fırlatır", False)
    except BinanceError as e:
        chk(f"non-200 (-2010) fırlatır (={str(e)[:40]})", "-2010" in str(e) or "400" in str(e))

    # 6) public 429 → 200
    b = mk([Resp(429, headers={"Retry-After": "0"}), Resp(200, js={"p": 1})])
    out = b._public("/pub")
    chk(f"public 429→200 retry (calls={b._s.calls})", out == {"p": 1} and b._s.calls == 2)

    # 7) retries tükenirse fırlatır (sürekli 429)
    b = mk([Resp(429, headers={"Retry-After": "0"})] * 5)
    try:
        b._signed("GET", "/x"); chk("sürekli 429 sonunda fırlatır", False)
    except BinanceError:
        chk(f"sürekli 429 → 4 deneme sonra fırlatır (calls={b._s.calls}=4)", b._s.calls == 4)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
