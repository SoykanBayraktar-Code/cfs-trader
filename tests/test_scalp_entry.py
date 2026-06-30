#!/usr/bin/env python3
"""Patlamadan-önce scalp giriş yolu: _scalp_entry_ok (sıkışma+kenar+yön+henüz-patlamamış) + risk.gate gevşetme (VETO YİNE bloklar)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import loop, risk


class Cfg:
    dry_run = True
    budget = 100.0
    risk = {"daily_max_loss_pct": 0, "max_consecutive_losses": 99, "max_concurrent": 99}
    signals = {"require_tape_confirm": True, "tape_min_score": 2.5,
               "scalp_entry": {"enabled": True, "max_squeeze_pct": 12, "max_atr_contraction": 0.9,
                               "edge_pct": 0.70, "min_book_asym": 0.10, "min_oi_trend": 0.5, "max_vol_surge": 1.8}}

    def get(self, k, d=None):
        return {"context": {"enabled": True, "skip_conflict_above": 0.3}}.get(k, d if d is not None else {})


class Store:
    def day_state(self, d):
        return {"halted": False, "realized_pnl": 0.0, "consec_losses": 0, "halt_reason": ""}
    def open_count(self):
        return 0
    def open_trades(self):
        return []


class Cand:
    def __init__(self, **k):
        # geçerli LONG ön-kırılma: sıkışık + kenara dayanmış + yön teyitli + henüz patlamamış
        self.symbol = "X"; self.side = "LONG"; self.tape_verdict = "CAUTION"; self.tape_score = 1.0
        self.squeeze_pct = 8.0; self.atr_contraction = 0.8; self.range_pos = 0.8
        self.book_asym = 0.2; self.oi_trend = 1.0; self.vol_surge = 1.0
        self.liq_pull = -0.5; self.min_tape_score = 0.0
        self.__dict__.update(k)


def main():
    ok = fail = 0
    def chk(name, cond):
        nonlocal ok, fail
        print(("✅" if cond else "❌") + " " + name); ok += bool(cond); fail += (not cond)

    cfg = Cfg()
    # ---- _scalp_entry_ok: PATLAMADAN ÖNCE ----
    chk("geçerli LONG ön-kırılma → True", loop._scalp_entry_ok(cfg, Cand()) is True)
    chk("range_pos 0.5 (kenarda değil) → False", loop._scalp_entry_ok(cfg, Cand(range_pos=0.5)) is False)
    chk("squeeze 20 (sıkışmamış) → False", loop._scalp_entry_ok(cfg, Cand(squeeze_pct=20)) is False)
    chk("atr_contraction 1.1 (daralmıyor) → False", loop._scalp_entry_ok(cfg, Cand(atr_contraction=1.1)) is False)
    chk("vol_surge 2.5 (PATLAMIŞ=geç) → False", loop._scalp_entry_ok(cfg, Cand(vol_surge=2.5)) is False)
    chk("yön teyidi yok (book 0 + oi 0) → False", loop._scalp_entry_ok(cfg, Cand(book_asym=0.0, oi_trend=0.0)) is False)
    chk("VETO → False", loop._scalp_entry_ok(cfg, Cand(tape_verdict="VETO")) is False)
    chk("geçerli SHORT ön-kırılma (dip+vakum) → True",
        loop._scalp_entry_ok(cfg, Cand(side="SHORT", range_pos=0.2, book_asym=-0.2)) is True)
    chk("range_pos None (veri yok) → False", loop._scalp_entry_ok(cfg, Cand(range_pos=None)) is False)
    cfg_off = Cfg(); cfg_off.signals = dict(cfg.signals, scalp_entry={"enabled": False})
    chk("disabled → False", loop._scalp_entry_ok(cfg_off, Cand()) is False)

    # ---- risk.gate gevşetme (scalp_ok param bağımsız) ----
    st = Store()
    g = risk.gate(cfg, st, None, Cand(tape_verdict="VETO"), 100, 100, "d", None, scalp_ok=True)
    chk("VETO + scalp_ok=True → YİNE bloklu (güvenlik)", (not g.ok) and "VETO" in g.reason)
    g2 = risk.gate(cfg, st, None, Cand(), 100, 100, "d", None, scalp_ok=False)
    chk("CAUTION-düşük + scalp_ok=False → tape RED", (not g2.ok) and "tape" in g2.reason)
    g3 = risk.gate(cfg, st, None, Cand(), 100, 100, "d", None, scalp_ok=True)
    chk(f"CAUTION-düşük + scalp_ok=True → tape GEÇTİ (reason={g3.reason!r})",
        (not g3.ok) and "tape" not in g3.reason and "mıknatıs" in g3.reason)

    print(f"\n=== {ok} geçti / {fail} kaldı ===")
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
