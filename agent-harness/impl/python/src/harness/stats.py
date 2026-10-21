"""Statistics (design §13.7): xoshiro256** + SplitMix64, paired bootstrap, gates. Left-to-right float sums only."""
from __future__ import annotations
import math
from .canonical import sha256_hex

M64 = (1 << 64) - 1


def splitmix64_stream(seed: int):
    s = seed & M64
    while True:
        s = (s + 0x9E3779B97F4A7C15) & M64
        z = s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
        yield z ^ (z >> 31)


def _rotl(x: int, k: int) -> int:
    return ((x << k) | (x >> (64 - k))) & M64


class Xoshiro256SS:
    def __init__(self, seed: int):
        sm = splitmix64_stream(seed)
        self.s = [next(sm) for _ in range(4)]

    def next(self) -> int:
        s = self.s
        result = (_rotl((s[1] * 5) & M64, 7) * 9) & M64
        t = (s[1] << 17) & M64
        s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3]
        s[2] ^= t
        s[3] = _rotl(s[3], 45)
        return result

    def index(self, n: int) -> int:
        return (self.next() * n) >> 64


def seed_for(run_id: str, gate_id: str) -> int:
    return int.from_bytes(bytes.fromhex(sha256_hex(f"{run_id}:{gate_id}"))[:8], "big")


def improvements(b: list[float], c: list[float], direction: str) -> list[float]:
    if len(b) != len(c):
        raise ValueError("paired samples must have equal length")
    return [ci - bi if direction == "maximize" else bi - ci for bi, ci in zip(b, c)]


def mean(x: list[float]) -> float:
    s = 0.0
    for v in x:
        s += v
    return s / len(x)


def bootstrap_means(x: list[float], seed: int, B: int = 10000) -> list[float]:
    """The sorted bootstrap distribution of the mean (one resample set can serve several confidence levels)."""
    n = len(x)
    if n == 0:
        raise ValueError("empty sample")
    rng = Xoshiro256SS(seed)
    means = []
    for _ in range(B):
        s = 0.0
        for _ in range(n):
            s += x[rng.index(n)]
        means.append(s / n)
    means.sort()
    return means


def interval_from_sorted(means: list[float], confidence: float) -> tuple[float, float, int, int]:
    B, a = len(means), 1 - confidence
    lo_i = math.floor((a / 2) * B)
    hi_i = math.ceil((1 - a / 2) * B) - 1
    return means[lo_i], means[hi_i], lo_i, hi_i


def bootstrap_ci(x: list[float], seed: int, confidence: float = 0.95, B: int = 10000) -> dict:
    means = bootstrap_means(x, seed, B)
    lo, hi, lo_i, hi_i = interval_from_sorted(means, confidence)
    return {"mean": mean(x), "lower": lo, "upper": hi, "lower_index": lo_i, "upper_index": hi_i}


def gate(run_id: str, gate_id: str, target: tuple[list[float], list[float], str],
         guardrails: list[dict] | None = None, confidence: float = 0.95, B: int = 10000) -> dict:
    """target=(baseline, candidate, direction); guardrail={metric, baseline, candidate, direction, margin}."""
    seed = seed_for(run_id, gate_id)
    t = bootstrap_ci(improvements(*target), seed, confidence, B)
    t["pass"] = t["lower"] > 0
    gr = []
    for g in guardrails or []:
        ci = bootstrap_ci(improvements(g["baseline"], g["candidate"], g["direction"]), seed, confidence, B)
        ci.update({"metric": g["metric"], "margin": g["margin"], "pass": ci["lower"] >= -g["margin"]})
        gr.append(ci)
    return {"target": t, "guardrails": gr, "accepted": t["pass"] and all(g["pass"] for g in gr), "seed": str(seed)}


_Z = {0.80: 1.2816, 0.90: 1.6449, 0.95: 1.96, 0.99: 2.5758}


def required_trials(pilot_sd: float, effect: float, confidence: float = 0.95, min_trials: int = 3) -> int:
    """Assumption (spec leaves the formula open): n = ceil((z * sd / effect)^2), floored at min_trials."""
    if confidence not in _Z:
        raise ValueError("confidence must be one of 0.80, 0.90, 0.95, 0.99")
    if effect <= 0:
        raise ValueError("effect must be positive")
    return max(min_trials, math.ceil((_Z[confidence] * pilot_sd / effect) ** 2))
