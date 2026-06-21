"""notify — Telegram bildirim. Sessiz başarısızlık (bildirim çökmesi botu durdurmaz).

Gizli token loglanmaz. enabled=false veya token yoksa no-op.
"""
import requests


class Notifier:
    def __init__(self, cfg):
        self.enabled = bool(cfg.get("telegram", {}).get("enabled", False))
        self.token, self.chat_id = cfg.telegram()
        if not (self.token and self.chat_id):
            self.enabled = False

    def send(self, text):
        if not self.enabled:
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False
