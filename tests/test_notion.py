#!/usr/bin/env python3
"""notion loglama testi — property builder + enabled mantığı + log_entry/exit + FAIL-SAFE (ağsız, _req mock)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import notion


class Cfg:
    def __init__(self, d):
        self._d = d
    def get(self, k, default=None):
        return self._d.get(k, default)


CFG = Cfg({"notion": {"enabled": True, "database_id": "DB123", "api_version": "2022-06-28", "timeout": 8}})

CLOSED = {  # kapanmış kaybeden SHORT
    "id": 50, "symbol": "SIRENUSDT", "side": "SHORT", "status": "CLOSED", "regime": "TREND_DOWN",
    "signal_type": "FRESH", "tape_verdict": "CONFIRM", "mode": "live", "entry": 0.0334, "sl": 0.03613,
    "sl_init": 0.03613, "tp": 0.02301, "leverage": 8, "qty": 14965, "risk_usdt": 32.2, "risk_pct_used": 50.0,
    "liq_pull": -0.9, "context_tilt": 1.0, "ls_bias": -1.0, "ls_tilt": 1.0, "sq_tilt": 1.0,
    "sizing_confidence": 1.0, "exit_price": 0.03527, "exit_reason": "SL", "pnl_usdt": -27.8,
    "r_multiple": -0.864, "fees_usdt": 0.51, "peak_price": 0.0334, "trail_state": "INIT",
    "ts_open": 1782750000, "ts_close": 1782771000, "llm_note": "[regime_flip] rejim erken döndü",
    "cz_snapshot": None, "ls_snapshot": '{"crowd":"KALABALIK-LONG"}', "brain_conviction": None,
    "brain_size_hint": None,
}
OPEN = {**CLOSED, "id": 55, "symbol": "NEARUSDT", "status": "OPEN", "pnl_usdt": None, "exit_price": None,
        "exit_reason": None, "r_multiple": None, "ts_close": None, "llm_note": None}


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)

    # ---- property builder: kapanmış ----
    p = notion._props(CLOSED)
    chk("title doğru", p["İşlem"]["title"][0]["text"]["content"] == "SIRENUSDT SHORT #50")
    chk("Sonuç=KAYIP (pnl<0)", p["Sonuç"]["select"]["name"] == "KAYIP")
    chk("Yön select=SHORT", p["Yön"]["select"]["name"] == "SHORT")
    chk("Giriş number", p["Giriş"]["number"] == 0.0334)
    chk("Çıkış Sebebi=SL", p["Çıkış Sebebi"]["select"]["name"] == "SL")
    chk("PnL number<0", p["PnL USDT"]["number"] == -27.8)
    chk("Açılış+Kapanış date var", "Açılış" in p and "Kapanış" in p and p["Açılış"]["date"]["start"].endswith("Z"))
    chk("Brain Notu rich_text", p["Brain Notu"]["rich_text"][0]["text"]["content"].startswith("[regime_flip]"))
    chk("Süre (saat) hesaplandı (~5.83)", abs(p["Süre (saat)"]["number"] - 5.83) < 0.05)
    chk("Notional=qty×entry", abs(p["Notional"]["number"] - round(14965 * 0.0334, 2)) < 0.01)

    # ---- property builder: açık ----
    po = notion._props(OPEN)
    chk("açık → Sonuç=AÇIK", po["Sonuç"]["select"]["name"] == "AÇIK")
    chk("açık → Çıkış Sebebi YOK", "Çıkış Sebebi" not in po and "PnL USDT" not in po and "Kapanış" not in po)

    # ---- enabled mantığı ----
    os.environ.pop("NOTION_TOKEN", None)
    chk("token yok → enabled False", notion.enabled(CFG) is False)
    os.environ["NOTION_TOKEN"] = "ntn_test"
    chk("token+config → enabled True", notion.enabled(CFG) is True)
    chk("config.enabled false → enabled False", notion.enabled(Cfg({"notion": {"enabled": False, "database_id": "X"}})) is False)

    # ---- log_entry / log_exit (mock _req) ----
    calls = []
    def fake_req(method, url, cfg, body=None):
        calls.append((method, url, body))
        return {"id": "PAGE123"}
    orig = notion._req
    notion._req = fake_req
    try:
        pid = notion.log_entry(CFG, OPEN)
        chk("log_entry page_id döndü", pid == "PAGE123")
        chk("entry: POST /pages + parent database_id", calls[-1][0] == "POST" and calls[-1][1].endswith("/pages")
            and calls[-1][2]["parent"]["database_id"] == "DB123")
        ok = notion.log_exit(CFG, CLOSED, "PAGE123")
        chk("exit(page_id): PATCH /pages/PAGE123", ok and calls[-1][0] == "PATCH" and calls[-1][1].endswith("/pages/PAGE123"))
        notion.log_exit(CFG, CLOSED, None)
        chk("exit(page_id yok): POST /pages (oluştur)", calls[-1][0] == "POST" and calls[-1][1].endswith("/pages"))
    finally:
        notion._req = orig

    # ---- FAIL-SAFE: _req patlasa bile sessiz ----
    def boom(*a, **k):
        raise RuntimeError("ağ hatası")
    notion._req = boom
    try:
        chk("entry hata → None (exception YOK)", notion.log_entry(CFG, OPEN) is None)
        chk("exit hata → False (exception YOK)", notion.log_exit(CFG, CLOSED, "P") is False)
    finally:
        notion._req = orig

    # ---- enabled=false → _req hiç çağrılmaz ----
    called = []
    notion._req = lambda *a, **k: called.append(1)
    try:
        notion.log_entry(Cfg({"notion": {"enabled": False, "database_id": "X"}}), OPEN)
        chk("enabled false → _req çağrılmadı", not called)
    finally:
        notion._req = orig

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
