"""pnd_ctx — Pump & Dump faz tespiti (SHADOW-first). Kaynak: La Morgia ve dig. 2023 "Doge of Wall St".

Anormal "rush order" (ayni-ms market-buy spike) imzasini aggTrades'ten okuyup coin'in P&D fazini
siniflar: NONE / PUMP_EARLY / PUMP_LATE / DUMPING. SHADOW: loglar, KARARI DEGISTIRMEZ.
FAIL-OPEN: veri yoksa/429 -> NONE, ASLA raise etmez (dongu bloklanmaz). Esikler config'de (pnd:).

Kalibrasyon NOTU (Faz C backtest sonrasi): rush_z/price_pos/dump esikleri perp'e ayarlanacak;
makale spot-StdRushOrder=12.8 dogrudan kullanilmaz, goreli z-skor kullanilir.
"""
import json, time, urllib.request, urllib.parse

FAPI = "https://fapi.binance.com"
_cache = {}          # symbol -> (ts, trades)
_CACHE_TTL = 60.0


def _f(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _fetch_aggtrades(symbol, window_min, max_pages, timeout=6):
    """Son window_min dk agresif-trade'leri (paginated). Doner: [{T,p,q,m}]. FAIL-SAFE -> []."""
    now = _cache.get("_now") or int(time.time() * 1000)
    end = int(time.time() * 1000)
    start = end - int(window_min * 60 * 1000)
    out = []
    cur = start
    try:
        for _ in range(max_pages):
            url = "%s/fapi/v1/aggTrades?symbol=%s&startTime=%d&endTime=%d&limit=1000" % (
                FAPI, urllib.parse.quote(symbol), cur, end)
            with urllib.request.urlopen(url, timeout=timeout) as r:
                b = json.loads(r.read())
            if not b:
                break
            for t in b:
                out.append({"T": int(t["T"]), "p": float(t["p"]), "q": float(t["q"]), "m": bool(t["m"])})
            if len(b) < 1000:
                break
            nxt = int(b[-1]["T"]) + 1
            if nxt <= cur:
                break
            cur = nxt
    except Exception:
        return out   # eldeki kadariyla (fail-open)
    return out


def compute_metrics(trades, cfg):
    """SAF, test edilebilir. trades=[{T,p,q,m}] -> metrik dict | None (yetersiz veri)."""
    p = cfg.get("pnd", {}) or {}
    chunk_ms = int(p.get("chunk_s", 15)) * 1000
    recent_n = int(p.get("recent_chunks", 4))
    min_base = int(p.get("min_baseline_chunks", 20))
    if not trades or len(trades) < 20:
        return None
    t0 = trades[0]["T"]; t_end = trades[-1]["T"]
    nch = int((t_end - t0) // chunk_ms) + 1
    if nch < min_base + recent_n:
        return None
    chunks = [{"rb_cnt_ms": set(), "rb_vol": 0.0, "rs_vol": 0.0, "n": 0,
               "close": None, "high": -1e18, "low": 1e18} for _ in range(nch)]
    for tr in trades:
        idx = int((tr["T"] - t0) // chunk_ms)
        if idx < 0 or idx >= nch:
            continue
        c = chunks[idx]; c["n"] += 1; c["close"] = tr["p"]
        c["high"] = max(c["high"], tr["p"]); c["low"] = min(c["low"], tr["p"])
        if tr["m"] is False:        # agresif BUY (taker alici) = rush-buy
            c["rb_cnt_ms"].add(tr["T"]); c["rb_vol"] += tr["q"]
        else:                        # agresif SELL
            c["rs_vol"] += tr["q"]
    rb = [len(c["rb_cnt_ms"]) for c in chunks]    # ayni-ms rush-buy sayisi (makale: rush orders)
    base = rb[:-recent_n]; recent = rb[-recent_n:]
    import statistics as st
    bmean = st.mean(base) if base else 0.0
    bstd = (st.pstdev(base) if len(base) > 1 else 0.0) or 1e-9
    rush_z = (st.mean(recent) - bmean) / bstd
    # fiyat
    closes = [c["close"] for c in chunks if c["close"] is not None]
    highs = [c["high"] for c in chunks if c["high"] > -1e17]
    lows = [c["low"] for c in chunks if c["low"] < 1e17]
    if not closes:
        return None
    win_hi = max(highs); win_lo = min(lows); now_c = closes[-1]; win_open = closes[0]
    price_pos = (now_c - win_lo) / (win_hi - win_lo) if win_hi > win_lo else 0.5
    gain_pct = (now_c / win_open - 1) * 100 if win_open else 0.0
    rb_vol_r = sum(c["rb_vol"] for c in chunks[-recent_n:])
    rs_vol_r = sum(c["rs_vol"] for c in chunks[-recent_n:])
    bs_ratio = rb_vol_r / (rb_vol_r + rs_vol_r) if (rb_vol_r + rs_vol_r) > 0 else 0.5
    off_high = (win_hi - now_c) / win_hi * 100 if win_hi else 0.0
    return {"rush_z": round(rush_z, 2), "price_pos": round(price_pos, 3), "gain_pct": round(gain_pct, 2),
            "bs_ratio": round(bs_ratio, 3), "off_high_pct": round(off_high, 2),
            "n_chunks": nch, "base_mean": round(bmean, 2), "recent_rb": round(st.mean(recent), 2)}


def classify(m, cfg):
    """SAF. metrik -> (phase, score). score = rush_z (spike buyuklugu)."""
    if not m:
        return "NONE", 0.0
    p = cfg.get("pnd", {}) or {}
    z = m["rush_z"]; pos = m["price_pos"]; gain = m["gain_pct"]; bs = m["bs_ratio"]; off = m["off_high_pct"]
    z_th = float(p.get("rush_z_thresh", 3.0))
    pos_pump = float(p.get("price_pos_pump", 0.40)); pos_late = float(p.get("price_pos_late", 0.70))
    late_gain = float(p.get("late_gain_pct", 8.0))
    dump_ratio = float(p.get("dump_ratio", 0.45)); dump_drop = float(p.get("dump_drop_pct", 1.5))
    min_gain = float(p.get("min_gain_pct", 3.0))
    score = round(z, 2)
    # DUMP once: sell-baskin + bir pump olmus + tepeden dusus
    if bs < dump_ratio and gain > min_gain and off >= dump_drop:
        return "DUMPING", score
    # PUMP: anormal buy-rush spike + buy baskin
    if z >= z_th and bs >= 0.5:
        if pos <= pos_pump:
            return "PUMP_EARLY", score
        if pos >= pos_late or gain >= late_gain:
            return "PUMP_LATE", score
    return "NONE", score


def evaluate(cfg, cand):
    """cand.symbol icin P&D fazini hesapla -> (pnd_phase, pnd_score, snapshot_json). FAIL-OPEN."""
    p = cfg.get("pnd", {}) or {}
    try:
        sym = getattr(cand, "symbol", None)
        if not sym:
            return "NONE", 0.0, None
        now = time.time()
        hit = _cache.get(sym)
        if hit and now - hit[0] < _CACHE_TTL:
            trades = hit[1]
        else:
            trades = _fetch_aggtrades(sym, int(p.get("window_min", 20)), int(p.get("max_pages", 10)))
            _cache[sym] = (now, trades)
        m = compute_metrics(trades, cfg)
        phase, score = classify(m, cfg)
        snap = json.dumps({"phase": phase, "score": score, "m": m}, separators=(",", ":"))
        return phase, score, snap
    except Exception:
        return "NONE", 0.0, None


def evaluate_symbol(cfg, symbol):
    class _C:
        pass
    c = _C(); c.symbol = symbol
    return evaluate(cfg, c)


if __name__ == "__main__":
    import sys
    from .cfg import get
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    ph, sc, snap = evaluate_symbol(get(), sym)
    print("%s: phase=%s score=%s\n%s" % (sym, ph, sc, snap))
