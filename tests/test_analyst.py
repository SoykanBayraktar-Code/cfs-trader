#!/usr/bin/env python3
"""Analist guardrail + override testi (ağsız, API ÇAĞRISI YOK) — saf doğrulama mantığı."""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cfs_trader import analyst


def main():
    n_ok = n_fail = 0

    def chk(name, cond):
        nonlocal n_ok, n_fail
        print(("✅" if cond else "❌") + " " + name)
        n_ok += bool(cond); n_fail += (not cond)

    # ---- validate: izinli + sınır içi → kabul ----
    acc, rej = analyst.validate([{"param": "risk.max_concurrent", "proposed": 2, "reason": "x"}])
    chk("izinli int sınır-içi kabul (=2)", len(acc) == 1 and acc[0]["value"] == 2 and not rej)

    # ---- clamp: sınır üstü kırpılır ----
    acc, rej = analyst.validate([{"param": "risk.max_concurrent", "proposed": 9, "reason": "x"}])
    chk("int sınır-üstü 9 → 3'e kırpıldı", acc[0]["value"] == 3 and acc[0]["clamped_from"] == 9)
    acc, _ = analyst.validate([{"param": "signals.tape_min_score", "proposed": 0.5, "reason": "x"}])
    chk("num sınır-altı 0.5 → 1.5'e kırpıldı", abs(acc[0]["value"] - 1.5) < 1e-9)

    # ---- bool ----
    acc, rej = analyst.validate([{"param": "momentum.enabled", "proposed": False, "reason": "x"}])
    chk("bool false kabul", acc[0]["value"] is False)
    acc, rej = analyst.validate([{"param": "momentum.enabled", "proposed": "hayır", "reason": "x"}])
    chk("bool olmayan değer RED", not acc and rej)

    # ---- izinli olmayan param RED (güvenlik) ----
    acc, rej = analyst.validate([
        {"param": "risk.leverage", "proposed": 10, "reason": "x"},
        {"param": "mode", "proposed": "live", "reason": "x"},
        {"param": "risk.budget_usdt", "proposed": 1000, "reason": "x"},
    ])
    chk("güvenlik-kritik paramlar RED (leverage/mode/budget)", not acc and len(rej) == 3)

    # ---- num int yuvarlama ----
    acc, _ = analyst.validate([{"param": "risk.max_consecutive_losses", "proposed": 3.4, "reason": "x"}])
    chk("int param yuvarlandı (3.4→3)", acc[0]["value"] == 3)

    # ---- write_overrides merge ----
    root = tempfile.mkdtemp(); os.makedirs(os.path.join(root, "data"))
    analyst.write_overrides(root, [{"param": "signals.tape_min_score", "value": 2.0}])
    analyst.write_overrides(root, [{"param": "risk.max_concurrent", "value": 2}])
    ov = json.load(open(os.path.join(root, "data", "analyst_overrides.json")))
    chk("override merge: iki param birikti", ov["params"]["signals.tape_min_score"] == 2.0 and ov["params"]["risk.max_concurrent"] == 2)

    # ---- cfg override uygulaması (noktalı yol) ----
    from cfs_trader.cfg import _ROOT, Config
    odir = os.path.join(_ROOT, "data"); os.makedirs(odir, exist_ok=True)
    opath = os.path.join(odir, "analyst_overrides.json")
    backup = opath + ".testbak"
    had = os.path.exists(opath)
    if had:
        os.replace(opath, backup)
    try:
        json.dump({"params": {"signals.tape_min_score": 2.9, "momentum.enabled": False}}, open(opath, "w"))
        c = Config()
        chk("cfg override: signals.tape_min_score=2.9 uygulandı", abs(c.signals["tape_min_score"] - 2.9) < 1e-9)
        chk("cfg override: momentum.enabled=False uygulandı", c.get("momentum")["enabled"] is False)
    finally:
        os.remove(opath)
        if had:
            os.replace(backup, opath)

    print(f"\n=== {n_ok} geçti / {n_fail} kaldı ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
