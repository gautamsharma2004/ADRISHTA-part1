"""Possible-fall state machine (server side only)."""


class FallDetector:
    def __init__(self):
        self.cooldown_until = 0.0
        self.reset(clear_cooldown=True)

    def reset(self, clear_cooldown=False):
        self.state = "idle"
        self.t_spike = 0.0
        self.peak = 0.0
        self.agree_peak = 0
        self.calm_since = 0.0
        self.detail = ""
        if clear_cooldown:
            self.cooldown_until = 0.0

    def update(self, now, alive, nodes, spike, min_agree):
        logs, alert = [], False
        max_fr = 0.0
        for n in alive:
            max_fr = max(max_fr, n.fr)
            if n.fr >= spike:
                n.spike_at = now
        agree = sum(1 for n in nodes if n.alive and now - n.spike_at <= 0.6)
        needed = max(1, min(min_agree, len(alive)))
        p3, s3 = 0.0, "Standby"

        if self.state == "idle" and alive and now >= self.cooldown_until and agree >= needed:
            self.state = "check"
            self.t_spike, self.peak, self.agree_peak, self.calm_since = now, max_fr, agree, 0.0
            logs.append(f"Burst {max_fr:.1f}× on {agree} node(s). Checking for stillness…")

        if self.state == "check":
            el = now - self.t_spike
            self.peak = max(self.peak, max_fr)
            self.agree_peak = max(self.agree_peak, agree)
            if el < 1.0:
                s3 = "Settling…"
            else:
                calm = bool(alive) and max_fr < 1.3
                if calm:
                    if not self.calm_since:
                        self.calm_since = now
                else:
                    self.calm_since = 0.0
                if self.calm_since:
                    p3 = min(100.0, (now - self.calm_since) / 1.5 * 100.0)
                    s3 = f"Still for {now - self.calm_since:.1f} s"
                else:
                    s3 = "Movement continues"
                if self.calm_since and now - self.calm_since >= 1.5:
                    self.state = "alert"
                    self.cooldown_until = now + 20.0
                    alert = True
                    self.detail = (f"Burst {self.peak:.1f}× threshold on {self.agree_peak} node(s), "
                                   "then stillness. Not confirmed. Please check.")
                elif el > 6.0:
                    self.state = "idle"
                    logs.append("Movement continued. Treated as ordinary activity.")

        if self.state == "alert":
            p3, s3 = 100.0, "Possible fall"

        info = {"state": self.state, "max_fr": round(max_fr, 2), "agree": agree, "needed": needed,
                "p3": round(p3, 1), "s3": s3, "detail": self.detail}
        return info, logs, alert