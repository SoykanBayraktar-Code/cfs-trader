#!/usr/bin/env python3
"""brain iyileştirmeleri testi (ağsız, claude ÇAĞRISI YOK) — breaker, guardian severity, shadow-metrik."""
import os
import sys
import types
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import brain


class FakeStore:
    def __init__(self, opens, consec=0):
        self._opens = opens; self._consec = consec
    def open_trades(self): return self._opens
    def day_state(self, day): return {"consec_losses": self._consec}


class FakeCfg:
    budget = 65.0
    risk = {"max_consecutive_losses": 3}
    def get(self, k, d=None):
        return {"guardian": {"risk_frac": 1.5}} if k == "brain" else (d or {})


def main():
    n_ok = n_fail = 0
    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name); n_ok += bool(cond); n_fail += (not cond)

    cfg = FakeCfg()

    # ---- item 3: devre-kesici ----
    p = tempfile.mktemp()
    chk("breaker başta kapalı", not brain._breaker_open(p))
    brain._breaker_record(False, path=p, max_fails=3, cooldown_min=15)
    brain._breaker_record(False, path=p, max_fails=3, cooldown_min=15)
    chk("2 hata < eşik → hâlâ kapalı", not brain._breaker_open(p))
    brain._breaker_record(False, path=p, max_fails=3, cooldown_min=15)
    chk("3. hata → devre-kesici AÇILDI", brain._breaker_open(p))
    brain._breaker_record(True, path=p)
    chk("başarı → sıfırlandı (kapandı)", not brain._breaker_open(p))

    # ---- item 7: guardian deterministik severity ----
    # 2 LONG, düşük risk, consec 0 → tek bayrak, medium (korelasyon)
    opens2L = [{"symbol": "A", "side": "LONG", "qty": 1, "entry": 10, "risk_usdt": 1},
               {"symbol": "A", "side": "LONG", "qty": 1, "entry": 10, "risk_usdt": 1}]
    st, flags = brain.risk_state(cfg, FakeStore(opens2L, consec=0))
    chk("yön-konsantrasyonu → tek bayrak medium", len(flags) == 1 and flags[0]["sev"] == "medium")
    # consec=2 (kill-switch eşiğine 1 kala) → high
    st, flags = brain.risk_state(cfg, FakeStore([{"symbol": "A", "side": "LONG", "qty": 1, "entry": 10, "risk_usdt": 1}], consec=2))
    chk("ardışık-zarar eşiğine yakın → high bayrak", any(f["sev"] == "high" for f in flags))
    # over-risk: 2×50=100 > 65×1.5=97.5 → high
    opensRisk = [{"symbol": "A", "side": "LONG", "qty": 1, "entry": 10, "risk_usdt": 50},
                 {"symbol": "B", "side": "SHORT", "qty": 1, "entry": 10, "risk_usdt": 50}]
    st, flags = brain.risk_state(cfg, FakeStore(opensRisk, consec=0))
    chk("toplam risk > bütçe×frac → high bayrak", any(f["sev"] == "high" for f in flags))
    # boş portföy → bayrak yok
    st, flags = brain.risk_state(cfg, FakeStore([], consec=0))
    chk("açık yok → bayrak yok", flags == [])

    # ---- item 8: daily izinli-param metni + analyst importları ----
    chk("_allowed_text dolu + bilinen param içeriyor", "risk.max_concurrent" in brain._allowed_text())
    chk("analyst validate/ALLOWED/write_overrides import edildi",
        callable(brain._analyst_validate) and bool(brain._ALLOWED) and callable(brain._analyst_write))

    # ---- item 4: shadow-metrik store ----
    from cfs_trader.store import Store
    dbp = tempfile.mktemp() + ".db"
    sdb = Store(dbp)
    cand = types.SimpleNamespace(symbol="XUSDT", side="LONG", status="FRESH", regime="TREND_UP",
                                 tape_verdict="CONFIRM", tape_score=3.0)
    tid = sdb.open_trade(symbol="XUSDT", side="LONG", qty=1, entry=10, sl=9, risk_usdt=1,
                         regime="TREND_UP", signal_type="FRESH", tape_verdict="CONFIRM")
    sdb.log_brain_decision("allow", "high", "tape güçlü", cand, trade_id=tid)
    sdb.close_trade(tid, 11, "TP", 1.0, 1.0)            # kazanç
    sdb.log_brain_decision("veto", "high", "tepe", cand)  # veto (işlem yok)
    gs = sdb.brain_gate_stats()
    chk("shadow: allow=1 veto=1", gs["allow"] == 1 and gs["veto"] == 1)
    chk("shadow: allow→kapanan n=1, ortR=1.0, kazanan=1",
        gs["allow_closed_n"] == 1 and abs(gs["allow_avg_r"] - 1.0) < 1e-9 and gs["allow_wins"] == 1)

    # ---- Faz1 M2: _parse_pretrade (karar-verici çıktı) ----
    d, c, sh, r = brain._parse_pretrade({"decision": "allow", "conviction": 0.8, "size_hint": 0.9, "reason": "güçlü"})
    chk("parse allow + konv 0.8 + size 0.9", d == "allow" and c == 0.8 and sh == 0.9)
    d, c, sh, r = brain._parse_pretrade({"decision": "veto", "conviction": 0.1, "size_hint": 0.5})
    chk("parse veto + düşük konv", d == "veto" and c == 0.1 and sh == 0.5)
    d, c, sh, r = brain._parse_pretrade({})
    chk("parse boş → allow/0.5/1.0 varsayılan", d == "allow" and c == 0.5 and sh == 1.0)
    d, c, sh, r = brain._parse_pretrade({"decision": "x", "conviction": 5, "size_hint": 0.1})
    chk("parse sınır: konv→1.0(clip), size→0.5(clip), geçersiz dec→allow", d == "allow" and c == 1.0 and sh == 0.5)
    chk("parse konv metin → 0.5 fallback", brain._parse_pretrade({"conviction": "yüksek"})[1] == 0.5)

    # ---- SLX post-mortem: _conviction_veto (sertleştirme) ----
    chk("konv 0.38 < 0.40 → allow VETO'ya çevrildi", brain._conviction_veto("allow", 0.38, 0.40) == "veto")
    chk("konv 0.50 ≥ 0.40 → allow kalır", brain._conviction_veto("allow", 0.50, 0.40) == "allow")
    chk("zaten veto → veto kalır", brain._conviction_veto("veto", 0.9, 0.40) == "veto")
    chk("eşik None → değişmez", brain._conviction_veto("allow", 0.1, None) == "allow")
    chk("konv None → değişmez", brain._conviction_veto("allow", None, 0.40) == "allow")

    # ---- _liq_text (prompt yorumu) ----
    chk("_liq_text + → boğa", "boğa" in brain._liq_text(0.6))
    chk("_liq_text − → ayı", "ayı" in brain._liq_text(-0.6))
    chk("_liq_text None → yok", brain._liq_text(None) == "yok")

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
