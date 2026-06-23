"""datapool — kapalı/ücretsiz veri kaynaklarından HAM veri toplar (motoru DEĞİŞTİRMEDEN çağırır).

Ayrı `data/market_data.db`'ye yazar → trading DB'sine (trader_live.db) DOKUNMAZ, karar yoluna
BAĞLI DEĞİL (şimdilik yalnız SALT-TOPLAMA). Motor DOKUNULMAZ — signals._engine_cwd ile çağrılır.

Ücretsiz + anahtarsız kaynaklar (toplanır):
  macro_stablecoin (defillama) · macro_fng + macro_etf (macro_ctx) · liqmap (likidasyon haritası) ·
  cexflow (on-chain CEX akışı — YAVAŞ ~6s/parite, EVM-only, opsiyonel)
Anahtar/kurulum gerekenler (toplanmaz, raporda işaretlenir):
  coinalyze (ücretsiz key gerekir) · kronos (kurulum gerekir)

Çalıştırma: cd /root/cfs-trader && python3 -m cfs_trader.datapool --symbols BTCUSDT,ETHUSDT [--with-cexflow]
"""
import os
import json
import time
import sqlite3

from .cfg import _ROOT, get
from .signals import _engine_cwd

_DB = os.path.join(_ROOT, "data", "market_data.db")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    source  TEXT NOT NULL,      -- macro_stablecoin | macro_fng | macro_etf | liqmap | cexflow
    symbol  TEXT,               -- global kaynaklarda NULL
    data    TEXT NOT NULL       -- JSON
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(source, symbol, ts);
"""


def _db():
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    c = sqlite3.connect(_DB, timeout=30)
    c.executescript(_SCHEMA)
    return c


def _store(c, source, symbol, data):
    c.execute("INSERT INTO snapshots (ts,source,symbol,data) VALUES (?,?,?,?)",
              (int(time.time()), source, symbol, json.dumps(data, ensure_ascii=False, default=str)))
    c.commit()


def collect_macro(cfg, c, log=print):
    """Global makro (ücretsiz, anahtarsız): stablecoin akışı + Fear&Greed + ETF akışı."""
    out = {}
    with _engine_cwd(cfg.engine_path):
        import defillama, macro_ctx
        jobs = [("macro_stablecoin", lambda: defillama.stablecoin_flow(7)),
                ("macro_fng", lambda: macro_ctx.fear_greed()),
                ("macro_etf", lambda: macro_ctx.etf_flows(10))]
        for name, fn in jobs:
            try:
                d = fn(); _store(c, name, None, d); out[name] = d; log("  ✓ %s" % name)
            except Exception as e:
                out[name] = {"_error": repr(e)}; log("  ✗ %s: %r" % (name, e))
    return out


def collect_symbol(cfg, c, sym, with_cexflow=False, log=print):
    """Parite-başı (ücretsiz, anahtarsız): liqmap (hızlı) + opsiyonel cexflow (yavaş, EVM-only)."""
    out = {}
    with _engine_cwd(cfg.engine_path):
        import liqmap
        try:
            d = liqmap.magnets(sym); _store(c, "liqmap", sym, d); out["liqmap"] = d
        except Exception as e:
            out["liqmap"] = {"_error": repr(e)}; log("  ✗ liqmap %s: %r" % (sym, e))
        if with_cexflow:
            import onchain_cexflow
            try:
                d = onchain_cexflow.analyze(sym, hours=6); _store(c, "cexflow", sym, d); out["cexflow"] = d
            except Exception as e:
                out["cexflow"] = {"_error": repr(e)}; log("  ✗ cexflow %s: %r" % (sym, e))
    return out


def collect(symbols=None, with_cexflow=False, log=print):
    cfg = get()
    c = _db()
    log("== MAKRO (global, ücretsiz) ==")
    macro = collect_macro(cfg, c, log)
    syms = symbols or []
    log("== PARİTE (%d) | cexflow=%s ==" % (len(syms), "AÇIK" if with_cexflow else "kapalı"))
    per = {}
    for s in syms:
        per[s] = collect_symbol(cfg, c, s, with_cexflow, log)
        log("  ✓ %s" % s)
    total = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    by_src = {r[0]: r[1] for r in c.execute("SELECT source, COUNT(*) FROM snapshots GROUP BY source")}
    c.close()
    return {"macro": macro, "symbols": per, "total_rows": total, "by_source": by_src, "db": _DB}


def status():
    """Toplanan havuzun özeti (kaynak başına satır + en son ts)."""
    c = _db()
    rows = c.execute("SELECT source, COUNT(*) n, MAX(ts) last FROM snapshots GROUP BY source").fetchall()
    c.close()
    return [{"source": r[0], "rows": r[1], "last": time.strftime("%Y-%m-%d %H:%M", time.gmtime(r[2]))} for r in rows]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--with-cexflow", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2)); return
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    r = collect(syms, a.with_cexflow)
    print("\n=== TOPLANDI ===")
    print("DB:", r["db"])
    print("toplam satır:", r["total_rows"], "| kaynak başına:", r["by_source"])


if __name__ == "__main__":
    main()
