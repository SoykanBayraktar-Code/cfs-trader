"""brain — cfs-trader'ın Claude (LLM) zekâ katmanı. analyst.py'nin Max-CLI desenini paylaşır.

6 yetenek — hepsi config.brain altında, hepsi FAIL-SAFE (LLM hata/timeout → bot davranışı DEĞİŞMEZ):
  1) pretrade_review  — giriş öncesi 'ikinci göz' (VETO girişi engeller, ASLA giriş zorlamaz)
  7) tape qualitative — pretrade prompt'una ham order-flow (tape) verisi eklenir (1 ile birlikte)
  3) postmortem       — kapanan her işleme tek-cümle 'neden' notu (trades.llm_note)
  2) daily_review     — günlük strateji raporu → Telegram
  5) learning_guard   — kaybeden (rejim×yön×kaynak) kombinasyonları tespit (daily'de raporlanır)
  6) risk_guardian    — açık pozisyon yön-konsantrasyonu / maruziyet uyarısı

GÜVENLİK: brain ASLA emir göndermez/iptal etmez, config.yaml'a dokunmaz. Yalnız: bekleyen
girişi VETO eder, DB'ye not yazar, Telegram atar, günlük rapor üretir. Tüm çağrılar Max planı
`claude` CLI ile (ek API anahtarı / fatura GEREKMEZ).
"""
import os
import json
import time
import subprocess

from .analyst import _parse_json, validate as _analyst_validate, write_overrides as _analyst_write, ALLOWED as _ALLOWED

_BRAIN_CWD = "/tmp/cfs_brain"   # izole çalışma dizini — proje/CLAUDE.md bağlamı brain çağrılarına sızmasın


# ───────────────────────── config yardımcıları ─────────────────────────
def _bcfg(cfg):
    return (cfg.get("brain", {}) or {})


def _sub(cfg, name):
    f = _bcfg(cfg).get(name, {})
    return f if isinstance(f, dict) else {}


def _feat(cfg, name, default=False):
    """master switch (brain.enabled) AND özellik switch (brain.<name>.enabled)."""
    if not _bcfg(cfg).get("enabled", False):
        return False
    f = _bcfg(cfg).get(name, {})
    if isinstance(f, dict):
        return bool(f.get("enabled", default))
    return bool(f)


def _esc(s):
    """Telegram HTML için minimal kaçış."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ───────────────────────── devre-kesici (item 3) ─────────────────────────
# N ardışık claude hatası (rate-limit/timeout) → brain'i M dk geçici kapat (rate-limited Max hesabını dövme).
def _breaker_path(path=None):
    if path:
        return path
    from .cfg import _ROOT
    return os.path.join(_ROOT, "data", "brain_breaker.json")


def _breaker_open(path=None):
    """Devre-kesici açık mı (brain geçici kapalı mı)?"""
    try:
        b = json.load(open(_breaker_path(path)))
        return time.time() < float(b.get("until", 0))
    except Exception:
        return False


def _breaker_record(ok, path=None, max_fails=3, cooldown_min=15):
    """Başarı → sıfırla; hata → say, eşikte aç (until = now + cooldown)."""
    p = _breaker_path(path)
    try:
        b = json.load(open(p))
    except Exception:
        b = {}
    if ok:
        b = {"fails": 0, "until": 0}
    else:
        b["fails"] = int(b.get("fails", 0)) + 1
        if b["fails"] >= max_fails:
            b["until"] = time.time() + cooldown_min * 60
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(b, open(p, "w"))
    except Exception:
        pass


def _breaker_cfg():
    try:
        from .cfg import get
        s = (get().get("brain", {}) or {}).get("breaker", {}) or {}
        return int(s.get("max_fails", 3)), int(s.get("cooldown_min", 15))
    except Exception:
        return 3, 15


# ───────────────────────── LLM çağrısı (Max CLI) ─────────────────────────
def _ask(system, user, model="sonnet", timeout=60):
    """claude CLI headless → parse JSON. Devre-kesici + izole cwd + dinamik-bölüm hariç. Hata FIRLATIR."""
    if _breaker_open():
        raise RuntimeError("brain breaker açık (ardışık hata sonrası geçici kapalı)")
    mf, cd = _breaker_cfg()
    os.makedirs(_BRAIN_CWD, exist_ok=True)
    prompt = system + "\n\n" + user
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--model", model,
             "--exclude-dynamic-system-prompt-sections"],
            capture_output=True, text=True, timeout=timeout, cwd=_BRAIN_CWD,
        )
        if proc.returncode != 0:
            raise RuntimeError("claude CLI rc=%s: %s" % (proc.returncode, (proc.stderr or proc.stdout)[:200]))
        env = json.loads(proc.stdout)
        if env.get("is_error"):
            raise RuntimeError("claude is_error: %s" % str(env.get("result"))[:200])
        out = _parse_json(env["result"])
    except Exception:
        _breaker_record(False, max_fails=mf, cooldown_min=cd)
        raise
    _breaker_record(True)
    return out


# ═══════════════════════ 1 + 7) GİRİŞ ÖNCESİ İKİNCİ GÖZ ═══════════════════════
_PRETRADE_SYS = """Sen cfs-trader (~$65 bütçe, 5x kaldıraç, izole margin) Binance USD-M perpetual \
vadeli işlem botunun GİRİŞ-ÖNCESİ SON RİSK DENETÇİSİsin. Sana, botun teknik + order-flow (tape) + \
risk kapılarından ZATEN geçirdiği bir işlem adayı, ham tape verisi ve bu kombinasyonun geçmiş \
performansı veriliyor.

GÖREVİN: Bu girişin AÇILMASINA izin ver (allow) mi, yoksa VETO mu? Sen SON kontrolsün — amacın \
edge yaratmak DEĞİL, yalnız BARİZ tuzakları elemek: tükenen/parabolik pump'ın tepesi, order-flow \
ile yön uyumsuzluğu, geçmişte bu kombinasyonun sürekli kaybetmesi, açık likidite tuzağı.

KESİN KURALLAR:
- ŞÜPHEDE İZİN VER (allow). Yalnız NET, açıklanabilir bir red sebebi varsa veto et.
- Aşırı-temkinli olup her şeyi veto ETME — bot işlem yapmak zorunda; gereksiz veto = kaçan fırsat.
- Gerçek para. Tek bariz hatayı önlersen değerlisin; iyi girişleri bloke edersen zararlısın.

Çıktı SADECE şu JSON (başka metin/markdown YOK):
{"decision":"allow|veto","confidence":"low|medium|high","reason":"<kısa Türkçe gerekçe, ≤20 kelime>"}"""


def pretrade_review(cfg, cand, tape_raw=None, store=None, model=None, timeout=None):
    """(decision, confidence, reason) döndürür. decision ∈ {allow, veto}. Çağıran fail-safe sarar."""
    s = _sub(cfg, "pretrade")
    model = model or s.get("model", "sonnet")
    timeout = timeout or s.get("timeout", 45)

    learn = ""
    if store is not None:
        try:
            exp, n = store.expectancy(cand.regime, cand.side, cand.status)
            if exp is not None:
                learn = "\nGEÇMİŞ bu kombinasyon (%s|%s|%s): beklenti %+.2fR, örneklem n=%d" % (
                    cand.regime, cand.side, cand.status, exp, n)
        except Exception:
            pass

    tape_txt = ""
    if tape_raw:
        keep = {k: tape_raw[k] for k in
                ("verdict", "score_avg", "cvd", "oi_delta", "oi_change", "taker_ratio",
                 "taker", "funding", "fund", "spread", "detail", "note", "reason")
                if k in tape_raw}
        if keep:
            tape_txt = "\nTAPE (ham order-flow): " + json.dumps(keep, ensure_ascii=False)[:600]

    user = (
        "İŞLEM ADAYI: %s %s | kaynak=%s | rejim=%s/%s\n"
        "entry=%s stop=%s tp=%s rr=%s atr%%=%s score=%s\n"
        "tape verdict=%s skor=%+.1f | risk_mult=%s%s%s\n\n"
        "Bu girişe izin ver mi, veto mu? JSON ver."
    ) % (cand.symbol, cand.side, cand.status, cand.regime, cand.bias,
         cand.entry, cand.stop, cand.tp, cand.rr, cand.atr_pct, cand.score,
         cand.tape_verdict, cand.tape_score, getattr(cand, "risk_mult", 1.0), tape_txt, learn)

    v = _ask(_PRETRADE_SYS, user, model=model, timeout=timeout)
    dec = str(v.get("decision", "allow")).strip().lower()
    if dec not in ("allow", "veto"):
        dec = "allow"
    return dec, str(v.get("confidence", "")).strip().lower(), str(v.get("reason", ""))[:300]


# ═══════════════════════ 3) İŞLEM-SONRASI POST-MORTEM ═══════════════════════
_POSTMORTEM_SYS = """Sen cfs-trader botunun İŞLEM-SONRASI ANALİSTİsin. Kapanan TEK bir işlemin \
sonucunu inceleyip EN OLASI nedeni tek cümlede (Türkçe, ≤20 kelime) söyle. Botun gelecekte \
öğrenmesi için kısa, kesin, eyleme dönük yaz.

Olası etiketler: late_entry (geç giriş, tepeden), tape_wrong (tape yanılttı), regime_flip \
(rejim döndü), tight_sl (SL çok sıkıydı, gürültüye yakalandı), wide_sl (SL geniş, fazla kayıp), \
normal_var (normal değişkenlik, sistemik sorun yok), good_exit (trailing iyi çalıştı, kâr alındı).

Çıktı SADECE JSON: {"note":"<tek cümle>","tag":"<yukarıdaki etiketlerden biri>"}"""


def postmortem(cfg, trade, exit_price, reason, pnl, r_mult, model=None, timeout=None):
    """(note, tag) döndürür. Çağıran fail-safe sarar."""
    s = _sub(cfg, "postmortem")
    model = model or s.get("model", "sonnet")
    timeout = timeout or s.get("timeout", 35)
    try:
        dur_min = max(0, int((time.time() - trade["ts_open"]) / 60))
    except Exception:
        dur_min = "?"
    user = (
        "KAPANAN İŞLEM: %s %s | kaynak=%s rejim=%s tape=%s\n"
        "entry=%s sl_init=%s tp=%s exit=%s çıkış-sebebi=%s\n"
        "SONUÇ: PnL=%+.3f USDT  R=%+.2f  trail=%s  süre~%sdk\n\n"
        "Bu işlem neden böyle bitti? JSON ver."
    ) % (trade["symbol"], trade["side"], trade["signal_type"], trade["regime"], trade["tape_verdict"],
         trade["entry"], _g(trade, "sl_init"), trade["tp"], exit_price, reason,
         pnl, r_mult, _g(trade, "trail_state"), dur_min)
    v = _ask(_POSTMORTEM_SYS, user, model=model, timeout=timeout)
    return str(v.get("note", ""))[:300], str(v.get("tag", ""))[:30]


def _g(row, key, default=None):
    try:
        return row[key]
    except Exception:
        return default


# ═══════════════════════ 5) LEARNING GUARD (deterministik) ═══════════════════════
def learning_guard(cfg, store, min_n=None, floor=None):
    """Kaybeden (rejim×yön×kaynak) kombinasyonlarını döndür. Saf-Python, LLM yok.
    [{key, n, exp, wins}] — exp = ortalama R-beklentisi, floor altındakiler."""
    s = _sub(cfg, "learning_guard")
    lc = cfg.get("learner", {}) or {}
    min_n = min_n if min_n is not None else int(s.get("min_samples", lc.get("min_samples", 20)))
    floor = floor if floor is not None else float(s.get("expectancy_floor", lc.get("suppress_below_expectancy", -0.10)))
    out = []
    try:
        rows = store.db.execute(
            "SELECT key,n,sum_r,wins FROM learning WHERE n>=? ORDER BY (sum_r*1.0/n) ASC", (min_n,)
        ).fetchall()
        for r in rows:
            exp = r["sum_r"] / r["n"] if r["n"] else 0
            if exp < floor:
                out.append({"key": r["key"], "n": r["n"], "exp": round(exp, 3), "wins": r["wins"]})
    except Exception:
        pass
    return out


# ═══════════════════════ 6) RİSK GUARDIAN ═══════════════════════
def risk_state(cfg, store):
    """Açık pozisyonların deterministik risk durumu + flag listesi. (state, flags). LLM yok."""
    opens = list(store.open_trades())
    sides = [t["side"] for t in opens]
    n = len(opens)
    longs = sides.count("LONG")
    shorts = sides.count("SHORT")
    total_notional = sum((t["qty"] or 0) * (t["entry"] or 0) for t in opens)
    total_risk = sum((t["risk_usdt"] or 0) for t in opens)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    st = store.day_state(day)
    consec = st["consec_losses"]
    budget = cfg.budget
    risk_frac = float(_sub(cfg, "guardian").get("risk_frac", 1.5))
    max_consec = cfg.risk.get("max_consecutive_losses", 3)

    # her bayrak {sev, text} — severity DETERMİNİSTİK (LLM'siz): maruziyet/kill-switch=high, korelasyon=medium
    flags = []
    if n >= 2 and (longs == 0 or shorts == 0):
        flags.append({"sev": "medium", "text": "yön-konsantrasyonu: %d pozisyonun hepsi %s (korelasyon riski)" % (n, "LONG" if longs else "SHORT")})
    if total_risk > budget * risk_frac:
        flags.append({"sev": "high", "text": "toplam açık risk %.1f USDT > bütçe×%.1f (%.1f)" % (total_risk, risk_frac, budget * risk_frac)})
    if consec >= max_consec - 1:
        flags.append({"sev": "high", "text": "ardışık zarara yakın: %d / %d (kill-switch eşiği)" % (consec, max_consec)})

    state = {"n_open": n, "longs": longs, "shorts": shorts,
             "total_notional": round(total_notional, 2), "total_risk_usdt": round(total_risk, 3),
             "consec_losses": consec, "symbols": [t["symbol"] for t in opens]}
    return state, flags


_GUARDIAN_SYS = """Sen cfs-trader botunun RİSK GÖZCÜSÜsün. Açık pozisyon portföyünde tespit edilen \
risk bayrakları veriliyor. Soykan'a (sahip) tek kısa paragraf (Türkçe, ≤45 kelime) uyarı yaz: \
risk ne, somut öneri ne (pozisyon kıs / yeni giriş bekle / izle). Abartma, panik yapma, net ol.
Çıktı SADECE JSON: {"warn":"<kısa uyarı>","severity":"low|medium|high"}"""


def _guardian_gate(root, sig, min_interval_min):
    """Aynı flag-imzasını min_interval içinde tekrar uyarmamak için durum dosyası. (uyarmalı_mı)."""
    p = os.path.join(root, "data", "brain_guardian.json")
    now = time.time()
    cur = {}
    if os.path.exists(p):
        try:
            cur = json.load(open(p))
        except Exception:
            cur = {}
    last_sig = cur.get("sig")
    last_ts = cur.get("ts", 0)
    if sig == last_sig and (now - last_ts) < min_interval_min * 60:
        return False
    try:
        json.dump({"sig": sig, "ts": now}, open(p, "w"))
    except Exception:
        pass
    return True


def risk_guardian(cfg, store, notifier=None, model=None, log=print):
    """Deterministik flag → (flag varsa) LLM uyarısı → Telegram. Rate-limited. (None | dict)."""
    from .cfg import _ROOT
    state, flags = risk_state(cfg, store)
    if not flags:
        return None
    texts = [f["text"] for f in flags]
    _rank = {"low": 0, "medium": 1, "high": 2}
    max_sev = max((f["sev"] for f in flags), key=lambda s_: _rank.get(s_, 1))
    sig = "|".join(sorted(texts))[:200]
    s = _sub(cfg, "guardian")
    if not _guardian_gate(_ROOT, sig, int(s.get("min_interval_min", 60))):
        log("guardian: flag aynı, interval içinde — sessiz")
        return {"state": state, "flags": texts, "notified": False}

    # item 7: LLM SADECE severity >= llm_min_severity ise (varsayılan high). Aksi → deterministik metin (claude çağrısı YOK).
    warn, sev = "; ".join(texts), max_sev
    llm_min = str(s.get("llm_min_severity", "high"))
    if _rank.get(max_sev, 1) >= _rank.get(llm_min, 2):
        try:
            v = _ask(_GUARDIAN_SYS,
                     "RİSK BAYRAKLARI:\n- " + "\n- ".join(texts) +
                     "\n\nPORTFÖY: " + json.dumps(state, ensure_ascii=False) + "\n\nUyarı JSON ver.",
                     model=model or s.get("model", "sonnet"), timeout=s.get("timeout", 45))
            warn = str(v.get("warn", warn))[:400]
            sev = str(v.get("severity", max_sev))
        except Exception as e:
            log("guardian LLM hata (deterministik flag yine de gönderiliyor): %r" % e)
    else:
        log("guardian: severity=%s < %s — deterministik (LLM atlandı)" % (max_sev, llm_min))

    icon = {"high": "🚨", "medium": "⚠️", "low": "🟡"}.get(sev, "⚠️")
    if notifier:
        notifier.send("%s <b>Brain Risk Gözcüsü</b>\n%s\n<i>açık: %d | risk: %.1f USDT</i>" % (
            icon, _esc(warn), state["n_open"], state["total_risk_usdt"]))
    log("guardian: %s | %s" % (sev, warn))
    return {"state": state, "flags": texts, "warn": warn, "severity": sev, "notified": True}


# ═══════════════════════ 2) GÜNLÜK STRATEJİ RAPORU ═══════════════════════
_DAILY_SYS = """Sen cfs-trader (~$65, 5x, izole) Binance perpetual botunun KIDEMLİ KANTİTATİF \
STRATEJİSTİSİN. Sana bugünün + son işlemlerin sonuçları, (rejim×yön×kaynak) öğrenme tablosu ve \
otomatik tespit edilen 'kanayan' kombinasyonlar veriliyor.

GÖREVİN: Sahibe (Soykan) net, kısa, eyleme dönük bir GÜN SONU raporu üret. Rakamla konuş, genel \
geçer laf etme. Edge yoktan var etme — neyin çalıştığını, neyin kanadığını, hangi kaynağın/rejimin \
kısılması gerektiğini söyle.

Ayrıca: sana verilen 'AYARLANABİLİR PARAMLAR' listesinden, YALNIZ sınırlar içinde, veriye dayalı \
somut ayar değişiklikleri öner (param_changes). Yalnız o listedeki paramlar; gerek yoksa boş dizi. \
Güvenlik-kritik (mode/dry_run/budget/leverage) listede yok — onlara dokunamazsın.

Çıktı SADECE şu JSON:
{"summary":"<2-3 cümle: bugün ne oldu, genel gidişat>",
 "working":["<çalışan şey 1>", "..."],
 "bleeding":["<kanayan şey 1>", "..."],
 "actions":["<somut öneri (serbest metin)>"],
 "param_changes":[{"param":"<noktalı yol>","proposed":<sayı|bool>,"reason":"<gerekçe>"}],
 "confidence":"low|medium|high"}"""


def _allowed_text():
    """Daily prompt'una eklenecek izinli param + sınır listesi (analyst ALLOWED ile aynı)."""
    L = ["## AYARLANABİLİR PARAMLAR (param: [min,max] | bool):"]
    for p, b in _ALLOWED.items():
        L.append("  %s: %s" % (p, ("[%s,%s]" % (b[1], b[2])) if len(b) == 3 else "bool"))
    return "\n".join(L)


def _daily_context(cfg, store, n_trades=30):
    db = store.db
    today = time.strftime("%Y-%m-%d", time.gmtime())
    closed = [dict(r) for r in db.execute(
        "SELECT id,symbol,side,signal_type,regime,tape_verdict,exit_reason,pnl_usdt,r_multiple,"
        "trail_state,llm_note FROM trades WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (n_trades,))]
    daily = [dict(r) for r in db.execute("SELECT * FROM daily_state ORDER BY day DESC LIMIT 7")]
    learning = [dict(r) for r in db.execute("SELECT key,n,sum_r,wins FROM learning ORDER BY n DESC")]
    guard = learning_guard(cfg, store)
    # bugünün özeti
    todays = [t for t in closed]
    return today, closed, daily, learning, guard


def _render_daily(today, closed, daily, learning, guard):
    L = ["## SON KAPANAN İŞLEMLER (yeni→eski):"]
    for t in closed[:30]:
        note = (" | not: " + t["llm_note"]) if t.get("llm_note") else ""
        L.append("  #%s %s %s %s/%s tape=%s çıkış=%s PnL=%s R=%s%s" % (
            t["id"], t["symbol"], t["side"], t["signal_type"], t["regime"], t["tape_verdict"],
            t["exit_reason"], t["pnl_usdt"], t["r_multiple"], note))
    L.append("\n## GÜNLÜK DURUM (son 7g):")
    for d in daily:
        L.append("  %s pnl=%.2f trades=%s ardışık-zarar=%s halted=%s" % (
            d["day"], d["realized_pnl"], d["trades_count"], d["consec_losses"], bool(d["halted"])))
    L.append("\n## ÖĞRENME TABLOSU (rejim|yön|kaynak → beklenti R):")
    for l in learning:
        exp = l["sum_r"] / l["n"] if l["n"] else 0
        L.append("  %s: n=%s beklenti=%+.3fR kazanan=%s" % (l["key"], l["n"], exp, l["wins"]))
    if guard:
        L.append("\n## ⚠️ OTOMATİK TESPİT — KANAYAN KOMBİNASYONLAR (n≥eşik, negatif beklenti):")
        for g in guard:
            L.append("  %s: beklenti %+.3fR (n=%s, kazanan=%s) — kısılması değerlendirilmeli" % (
                g["key"], g["exp"], g["n"], g["wins"]))
        L.append("  (Not: learner.enabled açıksa bu kombinasyonlar zaten otomatik bastırılıyor.)")
    L.append("\n" + _allowed_text())
    L.append("\nGörev: yukarıdaki veriye dayanarak gün sonu raporu + (varsa) sınır-içi param_changes JSON'u ver.")
    return "\n".join(L)


def daily_review(cfg, store, notifier=None, model=None, log=print):
    s = _sub(cfg, "daily")
    today, closed, daily, learning, guard = _daily_context(cfg, store)
    user = "GÜN: %s\n\n%s" % (today, _render_daily(today, closed, daily, learning, guard))
    v = _ask(_DAILY_SYS, user, model=model or s.get("model", "opus"), timeout=s.get("timeout", 240))

    summary = str(v.get("summary", ""))[:600]
    working = v.get("working", []) or []
    bleeding = v.get("bleeding", []) or []
    actions = v.get("actions", []) or []
    conf = str(v.get("confidence", ""))

    # item 8: param_changes → analyst guardrail (validate/clamp) → (auto_apply ise) override yaz
    accepted, rejected = _analyst_validate(v.get("param_changes", []) or [])
    applied = False
    if accepted and s.get("auto_apply", False):
        try:
            from .cfg import _ROOT
            _analyst_write(_ROOT, accepted)
            applied = True
        except Exception as e:
            log("daily param uygula hatası: %r" % e)

    log("=== BRAIN GÜNLÜK RAPOR (%s) ===" % today)
    log(summary)
    for x in working:
        log("  ✅ " + str(x))
    for x in bleeding:
        log("  🩸 " + str(x))
    for x in actions:
        log("  🔧 " + str(x))
    for a in accepted:
        log("  ⚙️ %s=%s — %s%s" % (a["param"], a["value"], a["reason"], " [UYGULANDI]" if applied else " [öneri]"))

    if notifier:
        parts = ["🧠 <b>Brain — Gün Sonu Strateji Raporu</b> (%s)" % today, _esc(summary)]
        if working:
            parts.append("\n<b>✅ Çalışan:</b>\n" + "\n".join("• " + _esc(x) for x in working[:5]))
        if bleeding:
            parts.append("\n<b>🩸 Kanayan:</b>\n" + "\n".join("• " + _esc(x) for x in bleeding[:5]))
        if actions:
            parts.append("\n<b>🔧 Öneri:</b>\n" + "\n".join("• " + _esc(x) for x in actions[:6]))
        if accepted:
            tag = "UYGULANDI" if applied else "öneri (analyst --apply ile uygula)"
            parts.append("\n<b>⚙️ Ayar (%s):</b>\n" % tag +
                         "\n".join("• %s=%s — %s" % (_esc(a["param"]), _esc(a["value"]), _esc(a["reason"])) for a in accepted[:6]))
        if guard:
            parts.append("\n<i>Otomatik tespit: %d kanayan kombinasyon (learner bastırıyor).</i>" % len(guard))
        try:
            gs = store.brain_gate_stats()
            if gs["veto"] or gs["allow"]:
                gl = "\n<i>Gate: %d veto / %d allow" % (gs["veto"], gs["allow"])
                if gs["allow_closed_n"]:
                    gl += " | allow→kapanan ort %sR (%d/%d kazanç)" % (gs["allow_avg_r"], gs["allow_wins"], gs["allow_closed_n"])
                parts.append(gl + "</i>")
        except Exception:
            pass
        parts.append("\n<i>güven: %s | Max planı / faturasız</i>" % conf)
        notifier.send("\n".join(parts))
    return {"summary": summary, "working": working, "bleeding": bleeding, "actions": actions,
            "param_changes": accepted, "applied": applied, "guard": guard}


# ───────────────────────── CLI (cron) ─────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser(description="cfs-trader brain (LLM zekâ katmanı)")
    ap.add_argument("cmd", choices=["daily", "guardian", "shadow", "test"],
                    help="daily=gün sonu rapor, guardian=risk gözcü, shadow=gate edge metriği, test=bağlantı testi")
    a = ap.parse_args()
    from .cfg import get
    from .store import Store
    from .notify import Notifier
    cfg = get()
    store = Store(cfg.db_path)
    notifier = Notifier(cfg)
    if a.cmd == "daily":
        daily_review(cfg, store, notifier)
    elif a.cmd == "guardian":
        res = risk_guardian(cfg, store, notifier)
        print(json.dumps(res, ensure_ascii=False, default=str) if res else "flag yok")
    elif a.cmd == "shadow":
        st = store.brain_gate_stats()
        print("=== PRETRADE GATE SHADOW-METRİK (item 4) ===")
        print("veto: %d | allow: %d" % (st["veto"], st["allow"]))
        if st["allow_closed_n"]:
            wr = 100.0 * st["allow_wins"] / st["allow_closed_n"]
            print("allow→kapanan: n=%d | ort R=%s | kazanma=%.0f%% (%d/%d)" % (
                st["allow_closed_n"], st["allow_avg_r"], wr, st["allow_wins"], st["allow_closed_n"]))
            print("Yorum: allow'ların ort R'si sistem geneliyle kıyasla gate'in seçiciliğini, veto sayısı müdahale sıklığını gösterir.")
        else:
            print("(henüz kapanan allow işlemi yok)")
        cs = store.context_stats()
        print("\n=== BAĞLAM (liq_pull) TİLT ÖLÇÜMÜ ===")
        ag, di = cs["agree"], cs["disagree"]
        print("liq_pull UYUŞAN:  n=%s kazanma=%s%% toplam=%sR" % (ag["n"], ag["winrate"], ag["sum_r"]))
        print("liq_pull ÇELİŞEN: n=%s kazanma=%s%% toplam=%sR" % (di["n"], di["winrate"], di["sum_r"]))
        print("(liq_pull kayıtlı kapanan işlem: %d — örneklem büyüdükçe güvenilir olur)" % cs["total_with_liq"])
    elif a.cmd == "test":
        v = _ask("Sen bir testsin.", 'SADECE şu JSON: {"ok": true}', model="sonnet", timeout=60)
        print("CLI OK:", v)


if __name__ == "__main__":
    main()
