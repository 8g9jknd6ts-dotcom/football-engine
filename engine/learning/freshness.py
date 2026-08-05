#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据新鲜度护栏（2026-08-06 新增，借鉴 MBS 概率系统方法论）

背景：MBS 概率系统对"长时间无正式比赛"的球队（季前赛、杯赛间歇、跨联赛
状态转移）明确降权 + 扩大不确定区间。我们系统此前完全没有此机制——
8/5 欧冠资格赛（里昂/奥林匹亚科斯等 79 天无正式比赛）翻车即同类问题。

实现：
1. 从 match_history.db 查询每队最后一场正式比赛日期
2. 预测时计算 days_since_last_match
3. 超过阈值 → 标记 freshness_risk 级别（watch/alert），并把方向概率向
   均势收缩（不确定性扩散），避免"旧状态当新状态"式高置信误判
4. 账本记录 freshness 信息，复盘按 risk 分层统计命中率 → 数据验证护栏有效性
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# 阈值（天）：与 MBS "79天无正式比赛→明确降权" 同类经验对齐
WATCH_DAYS = 30   # ≥30天无正式比赛 → 观察级（概率向均势轻微收缩）
ALERT_DAYS = 60   # ≥60天无正式比赛 → 预警级（概率明显收缩 + 页面警示）

# 均势参考概率（收缩目标）
EVEN_PROBS = (1 / 3, 1 / 3, 1 / 3)


@dataclass
class FreshnessInfo:
    """单场新鲜度信息"""
    home_days: int | None = None      # 主队距最后正式比赛天数
    away_days: int | None = None      # 客队距最后正式比赛天数
    risk: str = "ok"                  # ok / watch / alert
    shrink: float = 0.0               # 概率收缩强度 0.0~1.0（1.0=完全均势）

    def to_dict(self) -> dict:
        return {
            "home_days": self.home_days,
            "away_days": self.away_days,
            "risk": self.risk,
            "shrink": round(self.shrink, 3),
        }


class FreshnessTracker:
    """从 match_history.db 查询球队最后正式比赛日期"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._cache: dict[str, str] | None = None  # team -> last date

    def _load(self) -> dict[str, list[str]]:
        """team(归一化) -> 该队所有比赛日期 ISO 列表（升序）"""
        if self._cache is not None:
            return self._cache
        cache: dict[str, list[str]] = {}
        if not self.db_path.exists():
            self._cache = cache
            return cache
        try:
            conn = sqlite3.connect(self.db_path)
            conn.text_factory = str
            rows = conn.execute(
                "SELECT home_team, away_team, date FROM match_history "
                "WHERE date IS NOT NULL ORDER BY date"
            ).fetchall()
            conn.close()
            from ..team_aliases import normalize_team
            for home, away, d in rows:
                if not d:
                    continue
                # 取日期前缀（可能带时间）
                d = str(d)[:10]
                if home:
                    cache.setdefault(normalize_team(home), []).append(d)
                if away:
                    cache.setdefault(normalize_team(away), []).append(d)
        except Exception:
            pass
        self._cache = cache
        return cache

    def days_since(self, team: str, on_date: date) -> int | None:
        """球队在 on_date 当天距离【on_date 之前】最后一场正式比赛的天数

        只取 < on_date 的记录：预测时点的视角（当天比赛尚未发生，库里若有当天记录
        是结算后的回放，会污染回测）。实盘预测发生在结算前，库里本来就没有当天记录。
        """
        from ..team_aliases import normalize_team
        dates = self._load().get(normalize_team(team))
        if not dates:
            return None
        last = None
        for d in dates:
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            if dd < on_date:
                last = dd  # dates 升序，取最后一个 < on_date 的
        if last is None:
            return None
        delta = (on_date - last).days
        return max(delta, 0)

    def evaluate(self, home_team: str, away_team: str, on_date: date) -> FreshnessInfo:
        """评估一场比赛的新鲜度

        None（无近期记录）= 新鲜度未知 → watch 温和收缩：
        - 我们只预测竞彩场次，库里"无记录"可能是"真·长时间无正式比赛"（里昂/奥林匹亚科斯
          79 天无赛即此情形），也可能是"球队有比赛但我们没预测覆盖"（如对手 7/30 的欧冠）
        - 无证据时既不能当正常（会重蹈 8/5 欧冠翻车），也不能当 alert 强收缩（会误伤
          有近期比赛但未覆盖的队）→ 取中间 watch，温和向均势收缩
        """
        hd = self.days_since(home_team, on_date)
        ad = self.days_since(away_team, on_date)
        days = [d for d in (hd, ad) if d is not None]
        unknown = (hd is None) or (ad is None)

        # 任一队有近期记录且超阈值 → 按最久的那队定级
        if days:
            max_days = max(days)
            if max_days >= ALERT_DAYS:
                risk = "alert"
                # 收缩强度：60天→0.35，120天→0.55，封顶0.7（永远不完全归零）
                shrink = min(0.70, 0.35 + (max_days - ALERT_DAYS) / 200.0)
                return FreshnessInfo(home_days=hd, away_days=ad, risk=risk, shrink=shrink)
            if max_days >= WATCH_DAYS:
                return FreshnessInfo(home_days=hd, away_days=ad, risk="watch", shrink=0.20)

        # 任一队无近期记录（新鲜度未知）→ 温和降权：
        # 我们只预测竞彩场次，"无记录"可能是真·长时间无正式比赛（里昂/奥林匹亚科斯
        # 79 天无赛即此情形），也可能是球队有比赛但我们没预测覆盖 → 取中间 watch
        if unknown:
            return FreshnessInfo(home_days=hd, away_days=ad, risk="watch", shrink=0.15)

        return FreshnessInfo(home_days=hd, away_days=ad, risk="ok", shrink=0.0)

    def apply(self, probs: list[float], shrink: float) -> list[float]:
        """按收缩强度把概率向均势拉（不确定性扩散）"""
        if shrink <= 0 or len(probs) != 3:
            return probs
        out = [
            probs[i] * (1 - shrink) + EVEN_PROBS[i] * shrink
            for i in range(3)
        ]
        s = sum(out) or 1.0
        return [x / s for x in out]
