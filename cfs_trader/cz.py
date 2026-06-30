"""coinalyze shadow snapshot — engine/coinalyze.py'yi yeniden kullanir (SHADOW + squeeze-tilt).
Trade satirina kaydedilir. Fail-safe: hata/429'da None doner, ASLA raise etmez (dongu bloklanmaz)."""
import os, sys, json, time, contextlib

_ENGINE = "/root/crypto-futures-scan/engine"
_cz = None
_macro = {"ts": 0.0, "data": None}
_MACRO_TTL = 1800.0

@contextlib.contextmanager
def _in_engine():
    cwd = os.getcwd()
    if _ENGINE not in sys.path:
        sys.path.insert(0, _ENGINE)
    try:
        os.chdir(_ENGINE)
        yield
    finally:
        os.chdir(cwd)

def _mod():
    global _cz
    if _cz is None:
        with _in_engine():
            import coinalyze as c
            _cz = c
    return _cz

def coin_of(symbol):
    return (symbol or "").replace("USDT", "").replace(".P", "").upper()

def _funding_oi(coin):
    c = _mod()
    out = {}
    with _in_engine():
        try:
            f = c.funding(coin)
            out["funding_avg"] = f.get("avg"); out["funding_binance"] = f.get("binance")
            out["funding_div"] = f.get("divergence"); out["n_exch"] = f.get("n_exch")
            out["all_neg"] = f.get("all_neg"); out["all_pos"] = f.get("all_pos")
        except Exception as e:
            out["funding_err"] = repr(e)[:50]
        try:
            oi = c.oi_total(coin)
            out["oi_total"] = oi.get("total"); out["oi_n_exch"] = oi.get("n_exch")
        except Exception as e:
            out["oi_err"] = repr(e)[:50]
    return out

def _liq(coin):
    """coinalyze liq_flow ozeti. imb>0=short-squeeze (yukari baski), <0=long-flush (asagi)."""
    c = _mod()
    with _in_engine():
        try:
            lf = c.liq_flow(coin, hours=6)
            return {"imb": lf.get("imbalance"), "n_bars": lf.get("n_bars"),
                    "long_liq": lf.get("long_liq"), "short_liq": lf.get("short_liq")}
        except Exception as e:
            return {"liq_err": repr(e)[:50]}

def macro():
    now = time.time()
    if _macro["data"] is not None and now - _macro["ts"] < _MACRO_TTL:
        return _macro["data"]
    m = {}
    for coin in ("BTC", "ETH"):
        d = _funding_oi(coin)
        m[coin] = {"funding_avg": d.get("funding_avg"), "funding_div": d.get("funding_div"),
                   "oi_total": d.get("oi_total")}
    _macro["ts"] = now; _macro["data"] = m
    return m

def snapshot(symbol):
    """JSON string: {c, coin:{funding/oi/liq}, macro}. Fail-safe -> None."""
    try:
        coin = coin_of(symbol)
        if not coin:
            return None
        cdata = _funding_oi(coin)
        cdata["liq"] = _liq(coin)
        return json.dumps({"c": coin, "coin": cdata, "macro": macro()}, separators=(",", ":"))
    except Exception:
        return None

def parse_squeeze(snap):
    """cz_snapshot (json str/dict) -> {imb, n_bars, oi}. Squeeze-tilt icin. None=veri yok."""
    try:
        d = json.loads(snap) if isinstance(snap, str) else snap
        if not isinstance(d, dict):
            return None
        coin = d.get("coin", {}) or {}
        liq = coin.get("liq", {}) or {}
        return {"imb": liq.get("imb"), "n_bars": liq.get("n_bars"), "oi": coin.get("oi_total")}
    except Exception:
        return None

if __name__ == "__main__":
    print(snapshot(sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"))
