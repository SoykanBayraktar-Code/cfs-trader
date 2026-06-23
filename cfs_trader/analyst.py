"""analyst — Opus 4.8 governor. Periyodik olarak performansı inceler, SINIRLI param aralıklarında
ayar önerir (structured tool-output), guardrail'den geçirip data/analyst_overrides.json'a yazar.

GÜVENLİK: yalnız ALLOWED_PARAMS'taki paramları, yalnız sınırlar içinde değiştirir. mode/dry_run/
budget/leverage/margin_type/api ASLA dokunulmaz. Gerçek emir ASLA atmaz. config.yaml'a dokunmaz
(override dosyasına yazar). --apply olmadan sadece önerir + Telegram bildirir (güvenli varsayılan).

Çalıştırma:  cd /root/cfs-trader && python3 -m cfs_trader.analyst [--apply] [--yes] [--trades N]
"""
import os
import json
import time
import sqlite3
import argparse

MODEL = "claude-opus-4-8"

# Opus'un ayarlayabileceği TEK params + (tip, min, max). Burada olmayan HİÇBİR şey değiştirilemez.
# Güvenlik-kritik (mode/dry_run/budget/leverage/margin_type/max_sl_pct) KASITEN dışarıda.
ALLOWED = {
    "risk.daily_max_loss_pct":          ("num", 0.0, 50.0),
    "risk.max_consecutive_losses":      ("int", 2, 10),
    "risk.max_concurrent":              ("int", 1, 3),
    "risk.risk_per_trade_pct":          ("num", 1.0, 50.0),
    "signals.tape_min_score":           ("num", 1.5, 3.5),
    "signals.target_rr":                ("num", 1.0, 8.0),
    "signals.max_atr_pct":              ("num", 3.0, 12.0),
    "signals.scan_min_vol":             ("num", 5_000_000, 50_000_000),
    "signals.scan_pool":                ("int", 20, 100),
    "exits.breakeven_at_r":             ("num", 0.3, 1.5),
    "exits.trail_after_r":              ("num", 0.5, 3.0),
    "exits.trail_distance_r":           ("num", 0.3, 2.0),
    "momentum.enabled":                 ("bool",),
    "pullback_momentum.enabled":        ("bool",),
    "pullback_momentum.risk_mult":      ("num", 0.1, 1.0),
    "pullback_momentum.min_tape_score": ("num", 1.5, 3.5),
    "learner.enabled":                  ("bool",),
}

SYSTEM = """Sen cfs-trader adlı küçük-hesaplı (~$65 bütçe) otomatik Binance USD-M perpetual vadeli işlem botunun \
KANTİTATİF ANALİST/GOVERNOR'usun. Görevin: kapanan işlemlerin sonuçlarını inceleyip botun ayarlarını \
SINIRLI bir param kümesinde iyileştirmek (risk-ayarlı performansı artırmak, kaybeden desenleri kısmak).

BOT MİMARİSİ: scan_v3 (trend TA setup) + opsiyonel oi_surge/momentum/pullback-momentum kaynakları → \
tape (order-flow/CVD/OI) onayı → risk-gate → giriş (5x kaldıraç, izole margin) → trailing çıkış. \
'R' = risk-katı (PnL/risk_usdt). Rejimler: TREND_UP/TREND_DOWN/RANGE.

KESİN KURALLAR:
- YALNIZ sana verilen 'ayarlanabilir paramlar' listesindeki paramları, YALNIZ verilen [min,max] aralığında değiştir.
- Güvenlik-kritik hiçbir şeye dokunma (mode, dry_run, budget, leverage, margin_type, max_sl_pct). Bunlar listede yok.
- Edge yoktan var edemezsin. Sadece: kaybeden kaynak/rejim/yön kombinasyonlarını kıs, kazananı bırak, \
boyutu/sıklığı veriye göre ayarla. Kanıtsız agresif değişiklik yapma; az ve gerekçeli öner.
- Strateji KODU gerektiren fikirleri (ör. yeni bir rejim-filtresi gate'i) 'code_recommendations'a yaz \
(bunlar otomatik uygulanmaz, insana iletilir).
- Her param değişikliği için NET, veriye dayalı gerekçe ver. Değişiklik gerekmiyorsa boş 'param_changes' döndür.

Çıktını HER ZAMAN submit_analysis aracıyla ver."""

TOOL = {
    "name": "submit_analysis",
    "description": "Performans analizini ve önerilen ayar değişikliklerini gönder.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Genel değerlendirme (2-4 cümle): ne çalışıyor, ne kanıyor."},
            "param_changes": {
                "type": "array",
                "description": "Önerilen ayar değişiklikleri (yalnız izinli paramlar, sınırlar içinde). Gerek yoksa boş.",
                "items": {
                    "type": "object",
                    "properties": {
                        "param": {"type": "string", "description": "noktalı yol, ör. risk.max_consecutive_losses"},
                        "proposed": {"description": "yeni değer (sayı/bool)"},
                        "reason": {"type": "string", "description": "veriye dayalı gerekçe"},
                    },
                    "required": ["param", "proposed", "reason"],
                },
            },
            "code_recommendations": {
                "type": "array",
                "description": "Kod gerektiren stratejik fikirler (otomatik uygulanmaz, insana iletilir).",
                "items": {"type": "string"},
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["summary", "param_changes", "code_recommendations", "confidence"],
    },
}


# ---------- context ----------
def build_context(cfg, db_path, n_trades=40):
    """DB + config'ten Opus'a verilecek veriyi topla (dict)."""
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    trades = [dict(r) for r in c.execute(
        "SELECT id,symbol,side,signal_type,regime,tape_verdict,status,exit_reason,pnl_usdt,r_multiple,"
        "risk_usdt,trail_state FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (n_trades,))]
    daily = [dict(r) for r in c.execute("SELECT * FROM daily_state ORDER BY day DESC LIMIT 7")]
    learning = [dict(r) for r in c.execute("SELECT key,n,sum_r,wins FROM learning ORDER BY n DESC")]
    c.close()
    tunable = {}
    for path in ALLOWED:
        sec, key = path.split(".", 1) if "." in path else (None, path)
        node = cfg.get(sec, {}) if sec else cfg._d
        # nested (ör. momentum.enabled)
        parts = path.split(".")
        d = cfg._d; val = None; ok = True
        for seg in parts:
            if isinstance(d, dict) and seg in d:
                d = d[seg]
            else:
                ok = False; break
        if ok and not isinstance(d, dict):
            val = d
        tunable[path] = val
    return {"tunable_params": tunable, "param_bounds": {k: v for k, v in ALLOWED.items()},
            "recent_closed_trades": trades, "daily_state_7d": daily, "learning_table": learning}


def _render(ctx):
    """context → kompakt metin (Opus user mesajı)."""
    lines = ["## AYARLANABİLİR PARAMLAR (mevcut değer + [min,max]):"]
    for p, val in ctx["tunable_params"].items():
        b = ALLOWED[p]
        rng = f"[{b[1]},{b[2]}]" if len(b) == 3 else "bool"
        lines.append(f"  {p} = {val}   {rng}")
    lines.append("\n## SON KAPANAN İŞLEMLER (yeni→eski):")
    lines.append("  id symbol side kaynak rejim tape çıkış pnl R risk trail")
    for t in ctx["recent_closed_trades"]:
        lines.append("  #%s %s %s %s %s %s %s pnl=%s R=%s risk=%s %s" % (
            t["id"], t["symbol"], t["side"], t["signal_type"], t["regime"], t["tape_verdict"],
            t["exit_reason"], t["pnl_usdt"], t["r_multiple"], t["risk_usdt"], t["trail_state"]))
    lines.append("\n## GÜNLÜK DURUM (7g):")
    for d in ctx["daily_state_7d"]:
        lines.append("  %s pnl=%.2f trades=%s consec=%s halted=%s" % (
            d["day"], d["realized_pnl"], d["trades_count"], d["consec_losses"], bool(d["halted"])))
    lines.append("\n## LEARNING (rejim|yön|kaynak → beklenti):")
    for l in ctx["learning_table"]:
        exp = l["sum_r"] / l["n"] if l["n"] else 0
        lines.append("  %s: n=%s beklenti=%.3fR kazanan=%s" % (l["key"], l["n"], exp, l["wins"]))
    lines.append("\nGörev: yukarıdaki veriye dayanarak submit_analysis çağır. Az, gerekçeli, sınır-içi öner.")
    return "\n".join(lines)


# ---------- guardrail ----------
def validate(param_changes):
    """Opus önerilerini ALLOWED'a + sınırlara göre doğrula/kıs. (kabul_listesi, red_listesi)."""
    accepted, rejected = [], []
    for ch in param_changes:
        p = ch.get("param"); proposed = ch.get("proposed")
        if p not in ALLOWED:
            rejected.append((p, "izinli değil")); continue
        spec = ALLOWED[p]
        if spec[0] == "bool":
            if not isinstance(proposed, bool):
                rejected.append((p, f"bool değil: {proposed}")); continue
            val = proposed
        else:
            try:
                val = float(proposed)
            except (TypeError, ValueError):
                rejected.append((p, f"sayı değil: {proposed}")); continue
            lo, hi = spec[1], spec[2]
            clamped = max(lo, min(hi, val))
            if spec[0] == "int":
                clamped = int(round(clamped))
            if clamped != val:
                ch["_clamped_from"] = val
            val = clamped
        accepted.append({"param": p, "value": val, "reason": ch.get("reason", ""),
                         "clamped_from": ch.get("_clamped_from")})
    return accepted, rejected


def write_overrides(db_root, accepted):
    """Kabul edilen değişiklikleri data/analyst_overrides.json'a YAZ (mevcutla birleştir)."""
    p = os.path.join(db_root, "data", "analyst_overrides.json")
    cur = {"params": {}, "history": []}
    if os.path.exists(p):
        try:
            cur = json.load(open(p))
        except Exception:
            pass
    cur.setdefault("params", {}); cur.setdefault("history", [])
    for a in accepted:
        cur["params"][a["param"]] = a["value"]
    cur["history"].append({"changes": [(a["param"], a["value"]) for a in accepted]})
    cur["history"] = cur["history"][-50:]
    tmp = p + ".tmp"
    json.dump(cur, open(tmp, "w"), indent=2, ensure_ascii=False)
    os.replace(tmp, p)
    return p


# ---------- Opus çağrısı ----------
def call_opus(ctx):
    """Opus 4.8'i çağır (cache'li sistem + tool-output). verdict dict döndürür."""
    import anthropic
    client = anthropic.Anthropic()   # ANTHROPIC_API_KEY env/secrets'tan
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "submit_analysis"},
        messages=[{"role": "user", "content": _render(ctx)}],
    )
    usage = msg.usage
    for block in msg.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            return block.input, usage
    raise RuntimeError("submit_analysis çağrısı dönmedi")


def run(cfg, store, notifier=None, apply=False, n_trades=40, log=print):
    from .cfg import _ROOT
    ctx = build_context(cfg, cfg.db_path, n_trades)
    verdict, usage = call_opus(ctx)
    accepted, rejected = validate(verdict.get("param_changes", []))
    # maliyet
    cost = (getattr(usage, "input_tokens", 0) * 5 + getattr(usage, "output_tokens", 0) * 25
            + getattr(usage, "cache_read_input_tokens", 0) * 0.5
            + getattr(usage, "cache_creation_input_tokens", 0) * 6.25) / 1_000_000
    log("=== ANALİST ÖZETİ ===")
    log(verdict.get("summary", ""))
    log("güven: %s | maliyet ~$%.4f" % (verdict.get("confidence"), cost))
    log("önerilen değişiklik: %d kabul / %d red" % (len(accepted), len(rejected)))
    for a in accepted:
        cl = " (kırpıldı %s→%s)" % (a["clamped_from"], a["value"]) if a.get("clamped_from") is not None else ""
        log("  %s = %s%s — %s" % (a["param"], a["value"], cl, a["reason"]))
    for p, why in rejected:
        log("  ⨯ RED %s: %s" % (p, why))
    for rec in verdict.get("code_recommendations", []):
        log("  💡 KOD ÖNERİSİ: %s" % rec)

    applied = False
    if apply and accepted:
        write_overrides(_ROOT, accepted)
        applied = True
        log("✓ overrides yazıldı (data/analyst_overrides.json) — restart sonrası geçerli")
    elif accepted:
        log("(--apply yok → sadece öneri; uygulamak için --apply)")

    if notifier:
        chg = "\n".join("• %s=%s" % (a["param"], a["value"]) for a in accepted) or "(değişiklik yok)"
        recs = "\n".join("💡 %s" % r for r in verdict.get("code_recommendations", []))
        notifier.send("🧠 <b>Analist</b> (%s, ~$%.3f)\n%s\n<b>%s:</b>\n%s%s" % (
            verdict.get("confidence"), cost, verdict.get("summary", "")[:300],
            "UYGULANDI" if applied else "ÖNERİ", chg, ("\n" + recs) if recs else ""))
    return {"verdict": verdict, "accepted": accepted, "rejected": rejected, "applied": applied, "cost": cost}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="kabul edilen değişiklikleri override'a yaz")
    ap.add_argument("--yes", action="store_true", help="apply için onay (interaktif değilse)")
    ap.add_argument("--trades", type=int, default=40)
    ap.add_argument("--restart", action="store_true", help="apply sonrası cfs-trader.service restart")
    a = ap.parse_args()
    from .cfg import get
    from .store import Store
    from .notify import Notifier
    cfg = get()
    if a.apply and not a.yes:
        print("--apply GERÇEK ayar değiştirir — onay için --yes ekle"); raise SystemExit(1)
    store = Store(cfg.db_path)
    notifier = Notifier(cfg)
    res = run(cfg, store, notifier, apply=a.apply, n_trades=a.trades)
    if a.apply and res["applied"] and a.restart:
        os.system("systemctl restart cfs-trader.service")
        print("cfs-trader yeniden başlatıldı")


if __name__ == "__main__":
    main()
