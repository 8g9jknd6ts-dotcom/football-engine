"""时间衰减加权模块

参考 cnemri/world-cup-2026-predictor 的方法:
- 训练时给每场比赛加权 w = 0.5^(age_days / half_life_days)
- 近期比赛权重更高，远期比赛逐渐遗忘
- 用于 ELO 更新、Isotonic 校准、回测训练

用法:
    weights = time_decay_weights(match_dates, half_life_days=365)
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Sequence


def time_decay_weights(
    match_dates: Sequence[str | date | datetime],
    reference_date: date | None = None,
    half_life_days: int = 365,
) -> list[float]:
    """计算时间衰减权重

    Args:
        match_dates: 比赛日期列表 (ISO string 或 date 对象)
        reference_date: 参考日期 (默认今天)
        half_life_days: 半衰期天数 (默认365=1年，权重每过一年减半)

    Returns:
        权重列表，值域 (0, 1]
    """
    ref = reference_date or date.today()
    weights = []

    for d in match_dates:
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        elif isinstance(d, datetime):
            d = d.date()

        age_days = max(0, (ref - d).days)
        w = 0.5 ** (age_days / half_life_days)
        weights.append(w)

    return weights


def weighted_brier(
    probs: list[tuple[float, float, float]],
    actuals: list[int],
    weights: list[float],
) -> float:
    """加权 Brier Score"""
    if not probs:
        return 0.0

    total_w = sum(weights)
    if total_w == 0:
        return 0.0

    brier_sum = 0.0
    for i, (p, a) in enumerate(zip(probs, actuals)):
        actual_vec = [0.0, 0.0, 0.0]
        actual_vec[a] = 1.0
        brier = sum((pi - ai) ** 2 for pi, ai in zip(p, actual_vec))
        brier_sum += weights[i] * brier

    return brier_sum / total_w


def weighted_rps(
    probs: list[tuple[float, float, float]],
    actuals: list[int],
    weights: list[float],
) -> float:
    """加权 Ranked Probability Score"""
    if not probs:
        return 0.0

    total_w = sum(weights)
    if total_w == 0:
        return 0.0

    rps_sum = 0.0
    for i, (p, a) in enumerate(zip(probs, actuals)):
        cum_prob = [p[0], p[0] + p[1], 1.0]
        actual_vec = [0.0, 0.0, 0.0]
        actual_vec[a] = 1.0
        cum_actual = [actual_vec[0], actual_vec[0] + actual_vec[1], 1.0]
        rps = sum((cp - ca) ** 2 for cp, ca in zip(cum_prob, cum_actual)) / 2.0
        rps_sum += weights[i] * rps

    return rps_sum / total_w
