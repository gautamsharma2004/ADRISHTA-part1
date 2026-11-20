"""Adrishta sensing engine. All algorithms run here, on the server only."""
import base64
import math
import time
from collections import deque

import numpy as np

from .breathing import estimate as breath_estimate
from .dataset import make_trial
from .fall import FallDetector

SC, N, HIST = 52, 3, 300
MWIN, FWIN, MHIST = 50, 15, 300
PFX = bytes([67, 83, 73]).decode() + "_DATA,"          # incoming line prefix
LABELS = ["fall_forward", "fall_backward", "fall_sideways", "sit_down_fast", "lie_down_controlled",
          "bending_pickup", "object_drop", "walking_normal", "walking_two_people",
          "walking_three_people", "standing_still", "empty_room"]
CMDS = {"STATUS", "MAC_FILTER:1", "MAC_FILTER:0", "RESET_STATS"}


def std_win(m, win):
    """Gain-normalised temporal std averaged over subcarriers."""
    x = m[-win:].astype(np.float64)
    pm = x.mean(axis=1)
    pm[pm == 0] = 1.0
    x = x * (pm.mean() / pm)[:, None]
    return float(x.std(axis=0).mean())


def parse_line(line):
    """Returns None (not a data line), False (rejected) or (rx, seq, ms, rssi, vec)."""
    if not line.startswith(PFX):
        return None
    p = line[len(PFX):].split(",")
    L = len(p)
    try:
        if L == SC + 2:
            rx, seq, ms, rssi, i0 = 0, -1, float(p[0]), int(p[1]), 2
        elif L == SC + 3:
            rx, seq, ms, rssi, i0 = int(p[0]), -1, float(p[1]), int(p[2]), 3
        elif L == SC + 4:
            rx, seq, ms, rssi, i0 = int(p[0]), int(p[1]), float(p[2]), int(p[3]), 4
        else:
            return False
        v = np.array(p[i0:i0 + SC], dtype=np.float32)
    except ValueError:
        return False
    if not (0 <= rx < N) or not math.isfinite(ms) or not np.isfinite(v).all() or v.min() < 0 or v.max() > 2000:
        return False
    return rx, seq, ms, rssi, v


class Node:
    def __init__(self, i):
        self.id = i
        self.buf = np.zeros((HIST, SC), np.float32)
        self.head = 0
        self.count = 0
        self.seen = 0.0
        self.seq, self.seq_max, self.drops, self.rssi = -1, 0, 0, 0
        self.dev_ms = deque(maxlen=100)
        self.alive, self.rate, self.mot, self.fast, self.fr = False, 0.0, 0.0, 0.0, 0.0
        self.thr, self.base, self.cal, self.spike_at = 2.0, None, [], 0.0
        self.cur = None

    def push(self, v):
        self.buf[self.head] = v
        self.head = (self.head + 1) % HIST
        self.count += 1

    def ordered(self):
        return np.concatenate((self.buf[self.head:], self.buf[:self.head]))   # oldest → newest


class Engine:
    def __init__(self, store):
        self.store = store
        self.nodes = [Node(i) for i in range(N)]
        self.rejected = self.total = self.last_rssi = 0
        self.last_ack = ""
        self.br = deque(maxlen=5000)
        self.pre = deque()                                   # last 10 s of raw packets
        self.mh = [deque([0.0] * MHIST, maxlen=MHIST) for _ in range(N)]
        self.fall = FallDetector()
        self.fall_info = {"state": "idle", "max_fr": 0, "agree": 0, "needed": 1, "p3": 0, "s3": "Standby", "detail": ""}
        self.spike, self.min_agree, self.auto_event = 4.0, 1, True
        self.cal_until, self.cal_id = 0.0, ""
        self.pres, self.hold_until = "nosignal", 0.0
        self.sm_ratio, self.walk_ref, self.walk_until, self.walk_samples = 0.0, 2.5, 0.0, []
        self.last = {"ratio": 0.0, "est": 0, "walk_left": 0.0, "cal_left": 0.0}
        self.breath_out = {"title": "Breathing rhythm (experimental): collecting data…",
                           "kpi": "breathing: collecting data…", "spec": None}
        self.breath_fresh = False
        self.levels = {}
        self.rec = None
        self.dataset_dirty = False
        self.events, self.logs = [], deque(maxlen=50)
        self.tick_n = 0

    # ---------- events ----------
    def emit(self, kind, **kw):
        ev = {"type": "event", "kind": kind, **kw}
        self.events.append(ev)
        if kind == "log":
            self.logs.append(ev)

    def drain_events(self):
        ev, self.events = self.events, []
        return ev

    # ---------- ingestion ----------
    def handle_line(self, line, now):
        if not line:
            return
        if line.startswith(("ACK,", "DBG,")):
            self.last_ack = line[:80]
            return
        r = parse_line(line)
        if r is None:
            return
        if r is False:
            self.rejected += 1
            return
        self.ingest(*r, now)

    def ingest(self, rx, seq, ms, rssi, v, now):
        n = self.nodes[rx]
        n.push(v)
        n.seen, n.rssi = now, rssi
        self.total += 1
        self.last_rssi = rssi
        if n.dev_ms and ms < n.dev_ms[-1] - 1000:
            n.dev_ms.clear()
        n.dev_ms.append(ms)
        if seq >= 0:
            n.seq_max = max(n.seq_max, seq)
            mod = 256 if n.seq_max < 256 else 65536
            if n.seq >= 0:
                gap = (seq - n.seq) % mod - 1
                if 0 < gap < 1000:
                    n.drops += gap
            n.seq = seq
        if rx == 0:
            if self.br and ms < self.br[-1][0] - 1000:
                self.br.clear()
            self.br.append((ms, v))
        self.pre.append((now, rx, seq, ms, rssi, v))
        while self.pre and self.pre[0][0] < now - 10:
            self.pre.popleft()
        rec = self.rec
        if rec and rec["phase"] == "run":
            rec["buf"].append((rx, seq, ms, now, rssi, v))
            if len(rec["buf"]) > 60000:
                self._finish_rec(now)

    # ---------- operator actions ----------
    def start_calibration(self, now):
        if not any(n.alive for n in self.nodes):
            self.emit("toast", msg="No live data to calibrate", level="warn")
            return
        for n in self.nodes:
            n.cal = []
        self.cal_until = now + 5
        self.emit("toast", msg="Calibrating for 5 s. Leave the room and keep it still.", level="info")

    def start_walk(self, now):
        if not any(n.alive for n in self.nodes):
            self.emit("toast", msg="No live data. Check the hardware gateway.", level="warn")
            return
        self.walk_samples, self.walk_until = [], now + 10
        self.emit("toast", msg="One person only: walk normally around the room for 10 s.", level="info")

    def set_threshold(self, v):
        for n in self.nodes:
            n.thr = v

    def start_recording(self, now, label, subject, room, seconds, countdown):
        if self.rec:
            self.emit("toast", msg="Already recording", level="warn")
            return
        alive = [n.id for n in self.nodes if n.alive]
        if not alive:
            self.emit("toast", msg="No live data. Check the hardware gateway.", level="warn")
            return
        self.rec = {"label": label, "subject": subject, "room": room, "seconds": seconds,
                    "phase": "countdown" if countdown else "run", "go": now + 2.0, "start": now,
                    "buf": [], "expected": alive}

    def stop_recording(self, now):
        if self.rec:
            self._finish_rec(now)

    def _finish_rec(self, now):
        rec, self.rec = self.rec, None
        if not rec or len(rec["buf"]) < 2:
            self.emit("toast", msg="No packets captured", level="warn")
            return
        meta, raw = make_trial(rec["buf"], rec["label"], rec["subject"], rec["room"],
                               self.cal_id or "none", [n.thr for n in self.nodes], rec["expected"])
        self.store.add(meta, raw)
        self.dataset_dirty = True
        missing = [i for i in rec["expected"] if meta["feat"]["n"][i] == 0]
        if missing:
            self.emit("toast", msg=f"Saved, but RX{', RX'.join(map(str, missing))} sent no packets", level="warn")
        else:
            self.emit("toast", msg=f"Saved window ({meta['label']}, {meta['duration']} s)", level="success")

    def save_event_window(self):
        pk = [(rx, seq, ms, t, rssi, v) for (t, rx, seq, ms, rssi, v) in self.pre]
        if len(pk) < 10:
            return
        meta, raw = make_trial(pk, "possible_fall_event", "auto", "auto", self.cal_id or "none",
                               [n.thr for n in self.nodes], [n.id for n in self.nodes if n.alive], auto=True)
        self.store.add(meta, raw)
        self.dataset_dirty = True
        self.emit("log", msg=f"Saved {meta['duration']} s event window (possible_fall_event, unverified)", alert=False)

    # ---------- main tick (10 Hz) ----------
    def tick(self, now):
        self.tick_n += 1
        alive = []
        for n in self.nodes:
            n.alive = n.count > 0 and (now - n.seen) < 2.0
            n.cur = n.ordered() if n.count > 0 else None
            n.rate = 0.0
            if n.alive and len(n.dev_ms) > 10:
                d = n.dev_ms[-1] - n.dev_ms[0]
                if d > 0:
                    n.rate = (len(n.dev_ms) - 1) * 1000.0 / d
            n.mot = n.mot * 0.6 + std_win(n.cur, MWIN) * 0.4 if (n.alive and n.count >= MWIN) else 0.0
            n.fast = std_win(n.cur, FWIN) if (n.alive and n.count >= FWIN) else 0.0
            n.fr = n.fast / n.thr
            self.mh[n.id].append(n.mot / n.thr if n.alive else 0.0)
            if n.alive:
                alive.append(n)

        # calibration (per node)
        calibrating, left = self.cal_until > 0, 0.0
        if calibrating:
            left = self.cal_until - now
            if left > 0:
                for n in alive:
                    if n.count >= MWIN:
                        n.cal.append(n.mot)
            else:
                self.cal_until, calibrating = 0.0, False
                done, worst = [], 0.0
                self.cal_id = "cal_" + time.strftime("%H%M%S")
                for n in self.nodes:
                    sc, n.cal = n.cal, []
                    if len(sc) < 6:
                        continue
                    a = np.array(sc)
                    mean, sd = float(a.mean()), float(a.std())
                    n.thr = round(max(mean + 4 * sd, mean * 1.5, 0.3), 2)
                    worst = max(worst, sd / (mean + 1e-9))
                    f = min(n.count, 250)
                    n.base = n.cur[-f:].mean(axis=0)
                    done.append(f"RX{n.id} {n.thr:.2f}")
                if done:
                    self.emit("toast", msg="Calibrated: " + ", ".join(done), level="success")
                    if worst > 0.35:
                        self.emit("toast", msg="Readings varied during calibration. Consider repeating with the room empty.", level="warn")
                else:
                    self.emit("toast", msg="Calibration failed: no live node had enough data", level="warn")

        # presence
        ratio = max((n.mot / n.thr for n in alive), default=0.0)
        fused = float(np.mean([n.mot / n.thr for n in alive])) if alive else 0.0
        if not alive:
            self.pres = "nosignal"
        elif calibrating:
            self.pres = "calibrating"
        elif ratio >= 1:
            self.pres, self.hold_until = "detected", now + 3
        elif self.pres == "detected" and (ratio >= 0.8 or now < self.hold_until):
            pass
        else:
            self.pres = "uncertain" if ratio >= 0.8 else "none"

        # one-person walking reference
        walk_left = 0.0
        if self.walk_until > 0:
            if now < self.walk_until:
                if alive:
                    self.walk_samples.append(fused)
                walk_left = self.walk_until - now
            else:
                self.walk_until = 0.0
                s = sorted(v for v in self.walk_samples if v >= 0.8)
                if len(s) >= 20:
                    self.walk_ref = max(1.0, s[len(s) // 2])
                    self.emit("toast", msg=f"Walking reference saved: {self.walk_ref:.2f}× threshold", level="success")
                else:
                    self.emit("toast", msg="Not enough motion captured. Walk normally and try again.", level="warn")

        # experimental people estimate: independent movers add motion variance
        self.sm_ratio = 0.85 * self.sm_ratio + 0.15 * (fused if alive else 0.0)
        est = 0
        if self.pres in ("detected", "uncertain"):
            est = max(1, min(6, round((self.sm_ratio / self.walk_ref) ** 2)))
        self.last = {"ratio": ratio, "est": est, "walk_left": walk_left, "cal_left": left}

        # breathing (once per second)
        if self.tick_n % 10 == 0:
            e, n0 = breath_estimate(self.br), self.nodes[0]
            if "msg" in e:
                self.breath_out = {"title": "Breathing rhythm (experimental): " + e["msg"],
                                   "kpi": "breathing: " + e["msg"], "spec": None}
            else:
                if not (n0.alive and n0.mot < n0.thr * 1.2):
                    msg = "hold still to estimate"
                elif e["quality"] >= 3.5:
                    msg = f"~{e['peak_f']:.1f} breaths/min (quality {e['quality']:.1f})"
                else:
                    msg = f"no reliable rhythm (quality {e['quality']:.1f})"
                self.breath_out = {"title": f"Breathing rhythm (experimental, {e['tw']:.0f} s window): {msg}",
                                   "kpi": "breathing: " + msg, "spec": e["spec"]}
            self.breath_fresh = True

        # fall detection
        info, logs, alert = self.fall.update(now, alive, self.nodes, self.spike, self.min_agree)
        self.fall_info = info
        for l in logs:
            self.emit("log", msg=l, alert=False)
        if alert:
            self.emit("log", msg="Possible fall event", alert=True)
            self.emit("toast", msg="Possible fall event", level="danger")
            if self.auto_event:
                self.save_event_window()

        # recorder phases
        rec = self.rec
        if rec:
            if rec["phase"] == "countdown" and now >= rec["go"]:
                rec["phase"], rec["start"], rec["buf"] = "run", now, []
            elif rec["phase"] == "run" and rec["seconds"] > 0 and now - rec["start"] >= rec["seconds"]:
                self._finish_rec(now)

    # ---------- outputs ----------
    def snapshot(self, now, gateway_connected):
        nodes = [{"id": n.id, "alive": n.alive, "count": n.count, "rate": round(n.rate, 1),
                  "mot": round(n.mot, 3), "ratio": round(n.mot / n.thr, 3) if n.alive else 0.0,
                  "drops": n.drops, "ago": round(now - n.seen, 1) if n.count else None} for n in self.nodes]
        seen = max(n.seen for n in self.nodes)
        rec = None
        if self.rec:
            r = self.rec
            rec = {"phase": r["phase"], "seconds": r["seconds"], "count": len(r["buf"]),
                   "elapsed": round(now - r["start"], 1) if r["phase"] == "run" else 0,
                   "countdown": round(max(0.0, r["go"] - now), 1) if r["phase"] == "countdown" else 0}
        st = {"type": "state",
              "gateway": {"connected": gateway_connected, "age": round(now - seen, 1) if seen else None},
              "pres": self.pres, "est": self.last["est"], "ratio": round(self.last["ratio"], 3),
              "cal_left": round(self.last["cal_left"], 1), "walk_left": round(self.last["walk_left"], 1),
              "walk_ref": round(self.walk_ref, 2), "cal_id": self.cal_id,
              "calibrated": any(n.base is not None for n in self.nodes),
              "thr": [n.thr for n in self.nodes], "nodes": nodes,
              "mot": [round(m[-1], 3) for m in self.mh],
              "info": {"rate": round(sum(n.rate for n in self.nodes), 1), "rssi": self.last_rssi,
                       "packets": self.total, "rejected": self.rejected},
              "fall": self.fall_info,
              "settings": {"spike": self.spike, "min_agree": self.min_agree, "auto_event": self.auto_event},
              "rec": rec, "ack": self.last_ack}
        if self.breath_fresh:
            st["breath"] = self.breath_out
            self.breath_fresh = False
        return st

    def init_payload(self):
        return {"type": "init", "motion_hist": [list(h) for h in self.mh], "breath": self.breath_out,
                "logs": [{"msg": l["msg"], "alert": l.get("alert", False)} for l in self.logs], "labels": LABELS}

    def heat(self, view, delta):
        """Spectrogram for one viewer selection, quantised to 0..255 (levels sent alongside)."""
        if view == "fused":
            al = [n for n in self.nodes if n.count > 0]
            if not al:
                return None
            filled = min(HIST, max(n.count for n in al))
            means = [float(n.cur[-min(n.count, HIST):].mean()) for n in al]
            gm = float(np.mean(means))
            acc = np.zeros((HIST, SC), np.float32)
            k = np.zeros((HIST, 1), np.float32)
            for n, mu in zip(al, means):
                f = min(n.count, HIST)
                acc[-f:] += n.cur[-f:] - mu
                k[-f:] += 1
            shown = np.where(k > 0, acc / np.maximum(k, 1) + gm, 0).astype(np.float32)
            delta_on = False
        else:
            n = self.nodes[int(view)]
            if n.count == 0:
                return None
            filled = f = min(n.count, HIST)
            shown = np.zeros((HIST, SC), np.float32)
            delta_on = bool(delta) and n.base is not None
            seg = n.cur[-f:]
            shown[-f:] = np.abs(seg - n.base) if delta_on else seg
        lo, hi = (float(x) for x in np.percentile(shown[HIST - filled:], [1, 99]))
        if delta_on:
            lo, hi = 0.0, max(hi, 3.0)
        elif hi - lo < 5:
            mid = (hi + lo) / 2
            lo, hi = mid - 2.5, mid + 2.5
        key = (view, delta_on)
        lv = self.levels.get(key)
        if lv is None:
            self.levels[key] = [lo, hi]
        else:
            lv[0] += 0.2 * (lo - lv[0])
            lv[1] += 0.2 * (hi - lv[1])
        lo, hi = self.levels[key]
        q = np.clip((shown - lo) / max(hi - lo, 0.1) * 255.0, 0, 255).astype(np.uint8)
        return {"type": "heat", "view": view, "delta": delta_on, "lo": round(lo, 2), "hi": round(hi, 2),
                "data": base64.b64encode(q.tobytes()).decode()}