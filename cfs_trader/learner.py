"""learner — hafif adaptif katman (Faz 3'te enabled). Negatif-beklentili kombinasyonları kıs.

Online RL DEĞİL: her kapanan işlem store.learning'e (rejim×yön×sinyal-tipi) yuvarlanan R yazar.
Yeterli örneklem (min_samples) biriken ve beklentisi eşiğin altındaki kombinasyonlar bastırılır.
Bu, negatif sinyali pozitife çevirmez — sadece en az-negatif dilime kayar (kaybı yavaşlatır).
"""


class Learner:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        lc = cfg.get("learner", {})
        self.enabled = bool(lc.get("enabled", False))
        self.min_samples = int(lc.get("min_samples", 20))
        self.threshold = float(lc.get("suppress_below_expectancy", -0.10))

    def suppressed(self, cand):
        """(bastırıldı_mı, sebep). enabled değilse veya store yoksa asla bastırmaz."""
        if not self.enabled or self.store is None:
            return False, ""
        exp, n = self.store.expectancy(cand.regime, cand.side, cand.status)
        if exp is None or n < self.min_samples:
            return False, ""
        if exp < self.threshold:
            return True, f"{cand.regime}|{cand.side}|{cand.status} exp {exp:.3f}R (n={n}) < {self.threshold}"
        return False, ""
