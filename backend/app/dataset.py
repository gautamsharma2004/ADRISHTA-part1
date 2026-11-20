"""Dataset storage and exports (server side only)."""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SC, N = 52, 3
FK = ["n", "mean", "std", "p2p", "rms", "maxdiff", "scstd"]
ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def clean(s, default):
    s = re.sub(r'[,"\r\n<>]', " ", str(s or "")).strip()[:40]
    return s or default


def features_of(pk):
    f = {k: [0.0] * N for k in FK}
    f["n"] = [0] * N
    for r in range(N):
        rows = [p[5] for p in pk if p[0] == r]
        f["n"][r] = len(rows)
        if len(rows) < 2:
            continue
        V = np.stack(rows).astype(np.float64)
        fm = V.mean(axis=1)
        f["mean"][r] = round(float(fm.mean()), 4)
        f["std"][r] = round(float(fm.std()), 4)
        f["p2p"][r] = round(float(fm.max() - fm.min()), 4)
        f["rms"][r] = round(float(np.sqrt((fm ** 2).mean())), 4)
        f["maxdiff"][r] = round(float(np.abs(np.diff(fm)).max()), 4)
        f["scstd"][r] = round(float(V.std(axis=0).mean()), 4)
    return f


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def make_trial(pk, label, subject, room, cal_id, thr, expected, auto=False):
    """pk: list of (rx, seq, device_ms, host_time_s, rssi, vector)."""
    f = features_of(pk)
    spans = []
    for r in range(N):
        ms = [p[2] for p in pk if p[0] == r]
        if len(ms) > 1:
            spans.append((max(ms) - min(ms)) / 1000.0)
    ts = [p[3] for p in pk]
    meta = {
        "id": "trial_" + uuid.uuid4().hex[:12], "label": label, "subject": subject, "room": room,
        "auto": auto, "demo": False, "startIso": _iso(min(ts)), "endIso": _iso(max(ts)),
        "duration": round(max(spans), 2) if spans else 0.0, "calId": cal_id,
        "thr": [float(x) for x in thr], "rssiMean": round(float(np.mean([p[4] for p in pk])), 1),
        "expected": list(expected), "feat": f,
    }
    raw = [{"rx": p[0], "seq": p[1], "ms": p[2], "t": int(p[3] * 1000), "rssi": p[4],
            "amps": [round(float(x), 1) for x in p[5]]} for p in pk]
    return meta, raw


class Store:
    def __init__(self, root):
        self.dir = Path(root) / "trials"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = []
        for p in sorted(self.dir.glob("*.meta.json")):
            try:
                self.meta.append(json.loads(p.read_text()))
            except Exception:
                pass
        self.meta.sort(key=lambda m: m.get("startIso", ""))

    def add(self, meta, raw):
        (self.dir / f"{meta['id']}.raw.json").write_text(json.dumps(raw, separators=(",", ":")))
        (self.dir / f"{meta['id']}.meta.json").write_text(json.dumps(meta, separators=(",", ":")))
        self.meta.append(meta)

    def delete(self, tid):
        if not ID_RE.match(tid or ""):
            return False
        for suffix in ("meta", "raw"):
            (self.dir / f"{tid}.{suffix}.json").unlink(missing_ok=True)
        self.meta = [m for m in self.meta if m["id"] != tid]
        return True

    def clear(self):
        for m in list(self.meta):
            self.delete(m["id"])

    def summary(self):
        return [{"id": m["id"], "label": m["label"], "subject": m["subject"], "room": m["room"],
                 "auto": m.get("auto", False), "duration": m.get("duration", 0),
                 "counts": m["feat"]["n"], "expected": m.get("expected", []),
                 "startIso": m.get("startIso", "")} for m in self.meta]

    def _raw(self, tid):
        try:
            return json.loads((self.dir / f"{tid}.raw.json").read_text())
        except Exception:
            return []

    def export_json(self):
        return json.dumps({"version": 1, "trials": [{**m, "raw": self._raw(m["id"])} for m in self.meta]})

    def raw_csv_iter(self):
        yield "trial_id,label,subject,room,rx_id,seq,device_ms,host_ms,rssi," + \
              ",".join(f"sc{i}" for i in range(SC)) + "\n"
        for m in self.meta:
            for p in self._raw(m["id"]):
                yield (f'{m["id"]},{m["label"]},{m["subject"]},{m["room"]},{p["rx"]},{p["seq"]},'
                       f'{p["ms"]},{p["t"]},{p["rssi"]},' + ",".join(str(x) for x in p["amps"]) + "\n")

    def features_csv(self):
        hdr = ["trial_id", "start", "duration_s", "label", "subject", "room", "auto", "cal_id", "rssi_mean"]
        for k in FK:
            hdr += [f"rx{r}_{k}" for r in range(N)]
        rows = [",".join(hdr)]
        for m in self.meta:
            row = [m["id"], m.get("startIso", ""), m.get("duration", 0), m["label"], m["subject"], m["room"],
                   1 if m.get("auto") else 0, m.get("calId", ""), m.get("rssiMean", 0)]
            for k in FK:
                row += [m["feat"][k][r] for r in range(N)]
            rows.append(",".join(str(x) for x in row))
        return "\n".join(rows)