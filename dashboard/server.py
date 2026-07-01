#!/usr/bin/env python3
# [INFO] cfs-dashboard backend — canli izleme dashboard (AYRI servis, SALT-OKUMA, canli bota DOKUNMAZ).
# [INFO] stdlib http.server (sifir yeni dep). Token korumali (/d/<TOKEN>/...). SQLite(ro)+Binance(cache)+journald+config okur.
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__)).rsplit("/dashboard", 1)[0]
sys.path.insert(0, ROOT)   # [INFO] cfs_trader paketini import edebilmek icin (server.py dashboard/ alt-dizininde)
DB_PATH = os.path.join(ROOT, "data", "trader_live.db")
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))


def _load_secrets():
    # [INFO] secrets.env (KEY=VALUE) -> ortam degiskeni (DASHBOARD_TOKEN + Binance anahtarlari icin). ASLA loglanmaz.
    p = os.path.join(ROOT, "secrets.env")
    if not os.path.exists(p):
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v and not os.environ.get(k):
                os.environ[k] = v


_load_secrets()
TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
if not TOKEN:
    raise SystemExit("DASHBOARD_TOKEN yok (secrets.env'e ekle)")

# ---- Binance (canli equity/pozisyon/fiyat) — TTL cache, thread-safe, fail-soft ----
_bin = None
_bin_lock = threading.Lock()
_cache = {}
_cache_lock = threading.Lock()


def _binance():
    global _bin
    if _bin is None:
        with _bin_lock:
            if _bin is None:
                from cfs_trader.cfg import get as get_cfg
                from cfs_trader import binance as Bmod
                _bin = Bmod.Binance(get_cfg())
    return _bin


def _cached(key, ttl, fn):
    # [INFO] Basit TTL cache — Binance'i her poll'da dovmemek + dashboard'i hizli tutmak icin.
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = {"_err": repr(e)[:120]}
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def _db():
    # [INFO] SALT-OKUMA baglanti (WAL -> bot yazarken guvenli okuma). Her istekte taze (thread).
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _cfg_live():
    # [INFO] config.yaml'i her istekte oku (canli duzenlemeleri yansitir: paused/risk/esikler). Ucuz (~18KB).
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _utcday():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _window_start(window):
    # [INFO] Zaman filtresi -> unix baslangic. today=UTC gun basi, 7d=7*86400 once, all=0.
    if window == "all":
        return 0
    if window == "7d":
        return int(time.time()) - 7 * 86400
    return int(time.mktime(time.strptime(_utcday(), "%Y-%m-%d")) - time.timezone)  # UTC gun basi


# ---- Binance canli veri (cache'li) ----
def _live_wallet():
    return _cached("wallet", 5, lambda: {"equity": round(_binance().wallet_balance(), 2)})


def _live_positions():
    def fn():
        out = {}
        for p in _binance().positions():
            amt = float(p["positionAmt"])
            if abs(amt) > 0:
                out[p["symbol"]] = {"amt": amt, "mark": float(p.get("markPrice") or 0),
                                    "upnl": float(p.get("unRealizedProfit") or 0),
                                    "entry": float(p.get("entryPrice") or 0)}
        return out
    return _cached("positions", 5, fn)


def _last_tick():
    # [INFO] journald son "[tik]" satiri -> guncel rejim + aday kirilimi (SCAN asamasi). Cache 8s.
    def fn():
        out = subprocess.run(["journalctl", "-u", "cfs-trader", "-n", "120", "-o", "cat", "--no-pager"],
                             capture_output=True, text=True, timeout=8).stdout
        tick = None
        for line in reversed(out.splitlines()):
            if "[tik]" in line and "rejim" in line:
                tick = line
                break
        if not tick:
            return {}
        m = re.search(r"rejim (\w+) \((\w+)\) \| (\d+) aday \(([^)]*)\)", tick)
        if not m:
            return {"raw": tick[-120:]}
        srcs = {}
        for part in m.group(4).split():
            if ":" in part:
                k, v = part.rsplit(":", 1)
                srcs[k] = int(v) if v.isdigit() else v
        ts_m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", tick)
        return {"regime": m.group(1), "bias": m.group(2), "candidates": int(m.group(3)),
                "sources": srcs, "at": ts_m.group(1) if ts_m else None}
    return _cached("tick", 8, fn)


def _svc_active():
    def fn():
        r = subprocess.run(["systemctl", "is-active", "cfs-trader"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    return _cached("svc", 10, fn)["_err"] if isinstance(_cached("svc", 10, fn), dict) else _cached("svc", 10, fn)


# ---- Pipeline asama eslemesi ----
def _stage_of(action, reason):
    if action == "ENTER":
        return "enter"
    if action == "ABORT_NO_SL":
        return "abort"
    r = (reason or "").lower()
    for pat, st in (("zaten açık", "double"), ("eşzamanlı", "concurrent"), ("toplam maruziyet", "cap"),
                    ("mıknatıs", "magnet"), ("boyutland", "sizing"), ("kasa müsait", "margin"),
                    ("günlük zarar", "killswitch"), ("ardışık", "consec"), ("learner", "learner"),
                    ("tape", "tape")):
        if pat in r:
            return st
    return "other"


# ---- Endpoint veri fonksiyonlari ----
def api_status(q):
    cfg = _cfg_live()
    day = _utcday()
    con = _db()
    ds = con.execute("SELECT * FROM daily_state WHERE day=?", (day,)).fetchone()
    opn = con.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
    last_dec = con.execute("SELECT ts FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    w = _live_wallet()
    equity = w.get("equity")
    daily_pnl = round(ds["realized_pnl"], 2) if ds else 0.0
    kill_lim = (equity or 0) * cfg["risk"].get("daily_max_loss_pct", 0) / 100.0
    return {
        "service": _svc_active(),
        "mode": cfg.get("mode"),
        "paused": bool(cfg["signals"].get("trading_paused")),
        "equity": equity,
        "daily_pnl": daily_pnl,
        "daily_kill_limit": round(-kill_lim, 2) if kill_lim else 0,
        "consec_losses": ds["consec_losses"] if ds else 0,
        "max_consec": cfg["risk"].get("max_consecutive_losses"),
        "halted": bool(ds["halted"]) if ds else False,
        "halt_reason": ds["halt_reason"] if ds else None,
        "open_count": opn,
        "max_concurrent": cfg["risk"].get("max_concurrent"),
        "last_decision_ts": last_dec["ts"] if last_dec else None,
        "server_time": int(time.time()),
    }


def api_pipeline(q):
    start = _window_start(q.get("window", "today"))
    con = _db()
    stages = {}
    for row in con.execute("SELECT action, reason FROM decisions WHERE ts>=?", (start,)):
        s = _stage_of(row["action"], row["reason"])
        stages[s] = stages.get(s, 0) + 1
    # tape ayrimi (VETO/CAUTION/NODATA)
    tape_break = {"VETO": 0, "CAUTION": 0, "NODATA": 0, "zayıf": 0}
    for (reason,) in con.execute("SELECT reason FROM decisions WHERE ts>=? AND action='REJECT' AND lower(reason) LIKE '%tape%'", (start,)):
        rl = (reason or "")
        if "VETO" in rl:
            tape_break["VETO"] += 1
        elif "NODATA" in rl:
            tape_break["NODATA"] += 1
        elif "zayıf" in rl:
            tape_break["zayıf"] += 1
        else:
            tape_break["CAUTION"] += 1
    closed = con.execute("SELECT COUNT(*), SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) FROM trades WHERE status='CLOSED' AND ts_close>=?", (start,)).fetchone()
    trailing_moves = con.execute("SELECT COUNT(*) FROM trades WHERE trail_state IN ('BE','TRAIL')").fetchone()[0]
    con.close()
    tape_rej = tape_break["VETO"] + tape_break["CAUTION"] + tape_break["NODATA"] + tape_break["zayıf"]
    gate_rej = sum(stages.get(k, 0) for k in ("double", "concurrent", "cap", "magnet", "sizing", "margin", "killswitch", "consec", "learner"))
    return {
        "window": q.get("window", "today"),
        "scan": _last_tick(),
        "tape": {"rejected": tape_rej, "breakdown": tape_break},
        "gate": {"rejected": gate_rej, "breakdown": {k: stages.get(k, 0) for k in ("double", "concurrent", "cap", "magnet", "sizing", "margin", "killswitch", "consec", "learner")}},
        "enter": stages.get("enter", 0),
        "abort": stages.get("abort", 0),
        "manage": {"trailing_moves": trailing_moves},
        "exit": {"closed": closed[0] or 0, "wins": closed[1] or 0},
        "other": stages.get("other", 0),
    }


def api_positions(q):
    con = _db()
    rows = con.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY ts_open").fetchall()
    con.close()
    live = _live_positions()
    out = []
    now = int(time.time())
    for t in rows:
        sym = t["symbol"]
        lp = live.get(sym, {})
        mark = lp.get("mark") or 0
        upnl = lp.get("upnl")
        direction = 1 if t["side"] == "LONG" else -1
        risk = t["risk_usdt"] or 0
        if upnl is None and mark:
            upnl = (mark - t["entry"]) * t["qty"] * direction
        upnl_r = (upnl / risk) if (risk and upnl is not None) else None
        out.append({
            "symbol": sym, "side": t["side"], "entry": t["entry"], "mark": mark,
            "sl": t["sl"], "tp": t["tp"], "trail_state": t["trail_state"],
            "qty": t["qty"], "risk_usdt": round(risk, 2),
            "upnl": round(upnl, 3) if upnl is not None else None,
            "upnl_r": round(upnl_r, 2) if upnl_r is not None else None,
            "leverage": t["leverage"], "regime": t["regime"], "signal_type": t["signal_type"],
            "held_min": round((now - t["ts_open"]) / 60),
        })
    return {"positions": out}


def api_activity(q):
    con = _db()
    decs = con.execute("SELECT ts,symbol,side,action,reason FROM decisions ORDER BY id DESC LIMIT 40").fetchall()
    con.close()
    return {"decisions": [dict(d) for d in decs]}


def api_risk(q):
    cfg = _cfg_live()
    r = cfg["risk"]
    day = _utcday()
    con = _db()
    open_rows = con.execute("SELECT symbol,risk_usdt FROM trades WHERE status='OPEN'").fetchall()
    ds = con.execute("SELECT realized_pnl FROM daily_state WHERE day=?", (day,)).fetchone()
    con.close()
    equity = _live_wallet().get("equity") or 0
    open_risk = sum((x["risk_usdt"] or 0) for x in open_rows)
    return {
        "risk_per_trade_pct": r.get("risk_per_trade_pct"),
        "max_total_risk_pct": r.get("max_total_risk_pct"),
        "daily_max_loss_pct": r.get("daily_max_loss_pct"),
        "max_consecutive_losses": r.get("max_consecutive_losses"),
        "max_concurrent": r.get("max_concurrent"),
        "leverage": r.get("leverage"),
        "equity": round(equity, 2),
        "open_risk": round(open_risk, 2),
        "total_cap": round(equity * (r.get("max_total_risk_pct") or 0) / 100, 2),
        "open_positions": [{"symbol": x["symbol"], "risk": round(x["risk_usdt"] or 0, 2)} for x in open_rows],
        "daily_pnl": round(ds["realized_pnl"], 2) if ds else 0,
        "daily_kill": round(-equity * (r.get("daily_max_loss_pct") or 0) / 100, 2),
    }


def api_performance(q):
    start = _window_start(q.get("window", "all"))
    con = _db()
    rows = con.execute(
        "SELECT ts_close,symbol,side,regime,signal_type,pnl_usdt,r_multiple,funding_usdt,fees_usdt,pnl_src,exit_reason "
        "FROM trades WHERE status='CLOSED' AND ts_close>=? ORDER BY ts_close", (start,)).fetchall()
    con.close()
    curve, cum_r, cum_p = [], 0.0, 0.0
    by_side, by_regime, by_source = {}, {}, {}
    fund_sum, fee_sum, wins, best, worst = 0.0, 0.0, 0, None, None
    for t in rows:
        r = t["r_multiple"] or 0
        p = t["pnl_usdt"] or 0
        cum_r += r
        cum_p += p
        curve.append({"t": t["ts_close"], "cum_r": round(cum_r, 3), "cum_pnl": round(cum_p, 2), "r": round(r, 3)})
        for grp, key in ((by_side, t["side"]), (by_regime, t["regime"]), (by_source, t["signal_type"])):
            g = grp.setdefault(key or "?", {"n": 0, "r": 0.0, "wins": 0})
            g["n"] += 1
            g["r"] += r
            g["wins"] += 1 if p > 0 else 0
        fund_sum += t["funding_usdt"] or 0
        fee_sum += t["fees_usdt"] or 0
        wins += 1 if p > 0 else 0
        best = r if best is None or r > best else best
        worst = r if worst is None or r < worst else worst
    n = len(rows)
    gains = sum((t["r_multiple"] or 0) for t in rows if (t["pnl_usdt"] or 0) > 0)
    losses = -sum((t["r_multiple"] or 0) for t in rows if (t["pnl_usdt"] or 0) < 0)
    return {
        "window": q.get("window", "all"),
        "n": n, "net_r": round(cum_r, 2), "net_pnl": round(cum_p, 2),
        "win_rate": round(100 * wins / n, 1) if n else 0,
        "avg_r": round(cum_r / n, 3) if n else 0,
        "best_r": round(best, 2) if best is not None else 0,
        "worst_r": round(worst, 2) if worst is not None else 0,
        "profit_factor": round(gains / losses, 2) if losses else None,
        "funding_sum": round(fund_sum, 3), "fees_sum": round(fee_sum, 3),
        "curve": curve,
        "by_side": _fmt_group(by_side), "by_regime": _fmt_group(by_regime), "by_source": _fmt_group(by_source),
    }


def _fmt_group(g):
    return [{"key": k, "n": v["n"], "net_r": round(v["r"], 2),
             "win_rate": round(100 * v["wins"] / v["n"], 1) if v["n"] else 0} for k, v in sorted(g.items(), key=lambda x: -x[1]["r"])]


def api_trades(q):
    con = _db()
    rows = con.execute(
        "SELECT ts_close,symbol,side,entry,exit_price,r_multiple,pnl_usdt,funding_usdt,pnl_src,signal_type,exit_reason,regime "
        "FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT 20").fetchall()
    con.close()
    return {"trades": [dict(t) for t in rows]}


def api_shadow(q):
    con = _db()
    learn = con.execute("SELECT key,n,sum_r,wins FROM learning WHERE n>0 ORDER BY sum_r DESC").fetchall()
    brain = con.execute("SELECT decision, COUNT(*) n, AVG(confidence) c FROM brain_decisions GROUP BY decision").fetchall()
    # derivs/pnd shadow dagilimi (kapali islemlerde)
    derivs = con.execute("SELECT derivs_bias, COUNT(*) FROM trades WHERE derivs_bias IS NOT NULL GROUP BY derivs_bias").fetchall()
    pnd = con.execute("SELECT pnd_phase, COUNT(*) FROM trades WHERE pnd_phase IS NOT NULL AND pnd_phase!='NONE' GROUP BY pnd_phase").fetchall()
    con.close()
    return {
        "learning": [{"key": r["key"], "n": r["n"], "net_r": round(r["sum_r"] or 0, 2),
                      "win_rate": round(100 * (r["wins"] or 0) / r["n"], 1) if r["n"] else 0} for r in learn],
        "brain": [{"decision": r["decision"], "n": r["n"], "avg_conf": round(r["c"] or 0, 2)} for r in brain],
        "derivs": [{"bias": r[0], "n": r[1]} for r in derivs],
        "pnd": [{"phase": r[0], "n": r[1]} for r in pnd],
    }


ROUTES = {
    "status": api_status, "pipeline": api_pipeline, "positions": api_positions,
    "activity": api_activity, "risk": api_risk, "performance": api_performance,
    "trades": api_trades, "shadow": api_shadow,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # journald'i doldurma

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        prefix = f"/d/{TOKEN}"
        if not (self.path == prefix or self.path.startswith(prefix + "/") or self.path.startswith(prefix + "?")):
            self._send(404, "not found", "text/plain")
            return
        rest = self.path[len(prefix):].lstrip("/")
        path, _, qs = rest.partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if path in ("", "index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    html = f.read().replace(b"__TOKEN__", TOKEN.encode())
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, f"index yok: {e}", "text/plain")
            return
        if path.startswith("api/"):
            name = path[4:]
            fn = ROUTES.get(name)
            if not fn:
                self._send(404, json.dumps({"error": "bilinmeyen endpoint"}))
                return
            try:
                self._send(200, json.dumps(fn(q), default=str))
            except Exception as e:
                self._send(500, json.dumps({"error": repr(e)[:200]}))
            return
        self._send(404, "not found", "text/plain")


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"cfs-dashboard :{PORT}  ->  /d/<token>/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
