"""Archive and search strategy (§17.9, decision P19)."""
from __future__ import annotations
import math, re
from ..stats import Xoshiro256SS


def lsum(xs) -> float:
    """Plain left-to-right float addition (Python 3.12's sum() is compensated, which other languages would not reproduce)."""
    t = 0.0
    for x in xs:
        t += x
    return t


def shingles(text: str, k: int = 5) -> set[str]:
    t = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(t[i:i + k]) for i in range(max(1, len(t) - k + 1))} if t else set()


def jaccard(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def novelty_rejected(edit_text: str, archive_texts: list[str], k: int = 5, threshold: float = 0.8) -> int | None:
    """Index of the first archived candidate the edit is too similar to (Jaccard >= threshold), else None."""
    s = shingles(edit_text, k)
    for i, a in enumerate(archive_texts):
        if jaccard(s, shingles(a, k)) >= threshold:
            return i
    return None


def parent_weights(scores: list[float], children: list[int], tau: float) -> list[float]:
    """w_i = softmax(score_i / tau) / (1 + children_i). Max-shifted for stability."""
    z = [s / tau for s in scores]
    m = max(z)
    e = [math.exp(v - m) for v in z]
    tot = lsum(e)
    return [(e[i] / tot) / (1 + children[i]) for i in range(len(scores))]


def sample_parent(weights: list[float], rng: Xoshiro256SS) -> int:
    u = (rng.next() >> 11) / 9007199254740992.0 * lsum(weights)       # 53-bit uniform, exact in binary64
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if u < acc:
            return i
    return len(weights) - 1


def pareto_front(points: list[dict]) -> list[int]:
    """Non-dominated indices over (target maximised, cost minimised, latency minimised)."""
    out = []
    for i, p in enumerate(points):
        dom = False
        for j, q in enumerate(points):
            if i != j and q["target"] >= p["target"] and q["cost"] <= p["cost"] and q["latency"] <= p["latency"] and (q["target"] > p["target"] or q["cost"] < p["cost"] or q["latency"] < p["latency"]):
                dom = True; break
        if not dom:
            out.append(i)
    return out


def mean_pairwise_similarity(texts: list[str], k: int = 5) -> float:
    sh = [shingles(t, k) for t in texts]
    pairs = [(i, j) for i in range(len(sh)) for j in range(i + 1, len(sh))]
    return lsum([jaccard(sh[i], sh[j]) for i, j in pairs]) / len(pairs) if pairs else 0.0
