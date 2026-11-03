"""Small pure-Python statistics helpers (no numpy dependency)."""

from __future__ import annotations

import math
from collections.abc import Sequence


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def covariance(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    sx, sy = stdev(xs), stdev(ys)
    if not sx or not sy or math.isnan(sx) or math.isnan(sy):
        return float("nan")
    return covariance(xs, ys) / (sx * sy)


def ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    return pearson(ranks(xs), ranks(ys))


def ols(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float, float]:
    """Simple regression y = a + b x. Returns (alpha, beta, alpha_se, residual_sd)."""
    n = len(xs)
    if n < 3:
        return float("nan"), float("nan"), float("nan"), float("nan")
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return float("nan"), float("nan"), float("nan"), float("nan")
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    s2 = sum(e * e for e in resid) / (n - 2)
    a_se = math.sqrt(s2 * (1 / n + mx * mx / sxx))
    return a, b, a_se, math.sqrt(s2)


def max_drawdown(levels: Sequence[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for v in levels:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def cagr(first: float, last: float, years: float) -> float | None:
    if first is None or last is None or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def isotonic_fit(xs: Sequence[float], ys: Sequence[float]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators. Returns sorted (x_upper_bound, fitted_value) steps."""
    pts = sorted(zip(xs, ys))
    blocks: list[list[float]] = []  # [sum_y, count, max_x]
    for x, y in pts:
        blocks.append([y, 1, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c, mx = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
            blocks[-1][2] = mx
    return [(b[2], b[0] / b[1]) for b in blocks]


def isotonic_predict(steps: list[tuple[float, float]], x: float) -> float:
    for upper, value in steps:
        if x <= upper:
            return value
    return steps[-1][1]


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    return mean([(p - o) ** 2 for p, o in zip(probs, outcomes)])
