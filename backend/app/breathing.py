"""Breathing-rate estimate (experimental, server side only)."""
import numpy as np

BF = np.arange(4.0, 40.001, 0.5)
_cache = {"M": 0, "C": None, "S": None, "W": None}


def estimate(br):
    n = len(br)
    if n < 200:
        return {"msg": "collecting data…"}
    items = list(br)
    ms = np.array([b[0] for b in items], dtype=np.float64) / 1000.0
    t1 = ms[-1]
    tw = min(40.0, t1 - ms[0])
    if tw < 30.0:
        return {"msg": f"collecting data… {max(0, int(tw))}/30 s"}
    fs = 10.0
    M = int(tw * fs)
    t0 = t1 - tw
    start = max(0, int(np.searchsorted(ms, t0)) - 1)
    ts = ms[start:]
    V = np.stack([b[1] for b in items[start:]]).astype(np.float64)
    tg = t0 + np.arange(M) / fs
    Y = np.empty((M, V.shape[1]))
    for c in range(V.shape[1]):
        Y[:, c] = np.interp(tg, ts, V[:, c])

    # remove slow drift (centered moving average of 101 samples), then window
    cs = np.vstack([np.zeros((1, Y.shape[1])), np.cumsum(Y, axis=0)])
    idx = np.arange(M)
    a, e = np.maximum(0, idx - 50), np.minimum(M, idx + 51)
    avg = (cs[e] - cs[a]) / (e - a)[:, None]
    hann = 0.5 - 0.5 * np.cos(2 * np.pi * idx / (M - 1))
    Y = (Y - avg) * hann[:, None]

    if _cache["M"] != M:
        ph = 2 * np.pi * (BF / 60.0)[:, None] * (idx / fs)[None, :]
        _cache.update(M=M, C=np.cos(ph), S=np.sin(ph))
    re, im = _cache["C"] @ Y, _cache["S"] @ Y
    P = re * re + im * im                              # (freqs, subcarriers)
    tot = P.sum(axis=0) + 1e-12
    band = (BF >= 8) & (BF <= 30)
    score = P[band].max(axis=0) / tot
    top = np.argsort(-score)[:6]
    spec = (P[:, top] / tot[top]).sum(axis=1)
    band_idx = np.nonzero(band)[0]
    pi = int(band_idx[np.argmax(spec[band])])
    mask = np.abs(np.arange(len(BF)) - pi) > 2
    quality = float(spec[pi] / (spec[mask].mean() + 1e-12))
    return {"spec": [round(float(x), 4) for x in spec], "peak_f": float(BF[pi]),
            "quality": quality, "tw": float(tw)}