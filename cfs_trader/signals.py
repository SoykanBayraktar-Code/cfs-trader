"""signals — crypto-futures-scan motorunu sinyal kütüphanesi olarak çağırır.

Motor cwd-duyarlı (sibling import + `sys.path.insert(0,".")` + kronos subprocess), o yüzden
engine çağrıları engine_path'e chdir edilerek yapılır (context manager), sonra geri dönülür.
Motor DOKUNULMAZ — sadece okunur/çağrılır.
"""
import os
import sys
import contextlib
from dataclasses import dataclass


@dataclass
class Candidate:
    symbol: str
    side: str            # LONG | SHORT
    entry: float
    stop: float
    tp: float
    rr: float
    score: int
    atr_pct: float
    status: str          # FRESH | IZLE-pullback | ...  (signal_type)
    regime: str
    bias: str
    tape_verdict: str = "?"
    tape_score: float = 0.0


@contextlib.contextmanager
def _engine_cwd(engine_path):
    old_cwd = os.getcwd()
    added = engine_path not in sys.path
    if added:
        sys.path.insert(0, engine_path)
    os.chdir(engine_path)
    try:
        yield
    finally:
        os.chdir(old_cwd)


def scan(cfg, min_vol=30_000_000, pool=40, top=12):
    """(regime_dict, [Candidate]) döndürür — config filtrelerini uygular (yön/RR/ATR)."""
    sig = cfg.signals
    with _engine_cwd(cfg.engine_path):
        import scan_v3
        r = scan_v3.run(min_vol=min_vol, pool=pool, top=top)
    regime = r["regime"]
    cands = []
    for s in r.get("setups", []):
        if s["direction"] not in sig["directions"]:
            continue
        if s.get("rr", 0) < sig["min_rr"]:
            continue
        if s.get("atr_pct", 99) > sig["max_atr_pct"]:
            continue
        cands.append(Candidate(
            symbol=s["symbol"], side=s["direction"],
            entry=float(s["entry"]), stop=float(s["stop"]), tp=float(s.get("tp2") or s.get("tp1")),
            rr=float(s.get("rr", 0)), score=int(s.get("score", 0)), atr_pct=float(s.get("atr_pct", 0)),
            status=s.get("status", "?"), regime=regime["regime"], bias=regime["bias"],
        ))
    return regime, cands


def confirm_tape(cfg, cand, dur=22):
    """Adayı 22s derin tape'den geçir; cand.tape_verdict/tape_score günceller ve verdict döndürür."""
    with _engine_cwd(cfg.engine_path):
        import tape
        res = tape.tape_check(cand.symbol, cand.side, dur=dur)
    cand.tape_verdict = res.get("verdict", "?")
    cand.tape_score = res.get("score_avg", 0.0)
    return res
