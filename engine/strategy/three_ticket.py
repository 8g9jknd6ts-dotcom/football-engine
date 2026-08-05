"""
三票制资金管理 — 60/30/10 风险阶梯。

来源: football-analyzer 三票制
理念:
  将每轮投注分为三档:
    - 稳胆票 (60%): 高置信度场次，低赔率，追求命中
    - 搏冷票 (30%): 中等置信度，中高赔率，追求超额收益
    - 彩票票 (10%): 高赔率长串，小注博大奖

每档独立计算 Kelly 注额，再乘以档位比例。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ThreeTicketConfig:
    """三票制参数"""
    # 资金分配比例
    stable_ratio: float = 0.60    # 稳胆票
    value_ratio: float = 0.30     # 搏冷票
    lottery_ratio: float = 0.10   # 彩票票

    # 各档赔率范围
    stable_odds_range: tuple[float, float] = (1.20, 1.80)
    value_odds_range: tuple[float, float] = (1.80, 3.50)
    lottery_odds_range: tuple[float, float] = (3.50, 20.0)

    # 各档最低概率阈值
    stable_min_prob: float = 0.60
    value_min_prob: float = 0.40
    lottery_min_prob: float = 0.20

    # 各档最大注数
    stable_max_picks: int = 4
    value_max_picks: int = 3
    lottery_max_picks: int = 2

    # 单票最大占总资金比
    max_single_ratio: float = 0.08


@dataclass
class TicketPick:
    """单条选项"""
    match_id: str
    selection: str          # "home" / "draw" / "away"
    odds: float
    prob: float             # 模型估计概率
    kelly_fraction: float   # Kelly建议仓位
    ticket_type: str = ""   # "stable" / "value" / "lottery"
    stake: float = 0.0      # 实际注额
    stake_reduction: float = 1.0  # 注额缩减系数（50-60% 概率段彩票降半，2026-08-06）
    downgrade_reason: str = ""    # "prob_5060" / "league_60_risk"（E 规则，2026-08-06）


@dataclass
class TicketPlan:
    """一轮三票方案"""
    stable_picks: list[TicketPick]
    value_picks: list[TicketPick]
    lottery_picks: list[TicketPick]
    total_stake: float = 0.0
    expected_roi: float = 0.0


class ThreeTicketAllocator:
    """
    三票制资金分配器。

    用法:
        alloc = ThreeTicketAllocator(bankroll=10000)
        plan = alloc.allocate(candidates, kelly_fractions)
    """

    def __init__(
        self,
        bankroll: float,
        config: ThreeTicketConfig | None = None,
        breaker_multiplier: float = 1.0,
        limits: dict | None = None,
    ):
        self.bankroll = bankroll
        self.cfg = config or ThreeTicketConfig()
        self.breaker_multiplier = breaker_multiplier
        # 硬性风控限额（来自 strategy.json limits）
        self.limits = limits or {}
        self.max_daily = self.limits.get("max_daily_stake", 500)
        self.max_single = self.limits.get("max_single_stake", 200)
        self.max_match_exposure = self.limits.get("max_match_exposure", 200)
        self.max_singles_per_match = self.limits.get("max_singles_per_match", 1)

    def allocate(
        self,
        candidates: list[dict],
    ) -> TicketPlan:
        """
        将候选场次分配到三档。

        candidates: [{match_id, selection, odds, prob, kelly_fraction}]
        """
        stable, value, lottery = [], [], []

        for c in candidates:
            odds = c["odds"]
            prob = c["prob"]
            pick = TicketPick(
                match_id=c["match_id"],
                selection=c["selection"],
                odds=odds,
                prob=prob,
                kelly_fraction=c.get("kelly_fraction", 0.0),
            )

            if self.cfg.stable_odds_range[0] <= odds <= self.cfg.stable_odds_range[1]:
                if prob >= self.cfg.stable_min_prob:
                    pick.ticket_type = "stable"
                    stable.append(pick)
            elif self.cfg.value_odds_range[0] <= odds <= self.cfg.value_odds_range[1]:
                if prob >= self.cfg.value_min_prob:
                    pick.ticket_type = "value"
                    value.append(pick)
            elif self.cfg.lottery_odds_range[0] <= odds <= self.cfg.lottery_odds_range[1]:
                if prob >= self.cfg.lottery_min_prob:
                    pick.ticket_type = "lottery"
                    lottery.append(pick)

            # 50-60% 概率段降档（2026-08-06）：MBS 8/3 自检 + 本账本 113 场互证
            # 此段命中率 37.1%（整体 43.4%），35 场实际平局 16 场（45.7%）模型 0 场判平
            # = 平局盲点集中爆发区。处理：稳胆→搏冷、搏冷→彩票、彩票→减注 50%
            if c.get("prob_band_5060") and pick.ticket_type:
                pick.downgrade_reason = "prob_5060"
                if pick.ticket_type == "stable":
                    pick.ticket_type = "value"
                    stable = [s for s in stable if s is not pick]
                    value.append(pick)
                elif pick.ticket_type == "value":
                    pick.ticket_type = "lottery"
                    value = [s for s in value if s is not pick]
                    lottery.append(pick)
                else:  # lottery: 减注 50%
                    pick.stake_reduction = 0.5

            # E 规则（2026-08-06）：高置信反向样本 ≥2 场的联赛 → 60%+ 段降一档
            # 60%+ 段整体命中 52.2% 是最好段，但风险联赛（如巴甲 2 场 71%/74% 主胜→平）
            # 的高置信更易翻车。处理：稳胆→搏冷、搏冷→彩票、彩票→减注 30%（弱于 50-60% 段）
            if c.get("prob_band_60_risk") and pick.ticket_type and not c.get("prob_band_5060"):
                pick.downgrade_reason = "league_60_risk"
                if pick.ticket_type == "stable":
                    pick.ticket_type = "value"
                    stable = [s for s in stable if s is not pick]
                    value.append(pick)
                elif pick.ticket_type == "value":
                    pick.ticket_type = "lottery"
                    value = [s for s in value if s is not pick]
                    lottery.append(pick)
                else:  # lottery: 减注 30%
                    pick.stake_reduction = 0.7

        # 按 edge = prob*odds - 1 排序，取前N
        stable.sort(key=lambda p: p.prob * p.odds - 1, reverse=True)
        value.sort(key=lambda p: p.prob * p.odds - 1, reverse=True)
        lottery.sort(key=lambda p: p.prob * p.odds - 1, reverse=True)

        # 同一场只保留最高 edge 的一个方向（避免对冲：同场押 away+draw 必有一注浪费）
        def _dedup(picks: list[TicketPick]) -> list[TicketPick]:
            seen: set = set()
            out = []
            for p in picks:
                if p.match_id in seen:
                    continue
                seen.add(p.match_id)
                out.append(p)
            return out

        stable = _dedup(stable)[: self.cfg.stable_max_picks]
        value = _dedup(value)[: self.cfg.value_max_picks]
        lottery = _dedup(lottery)[: self.cfg.lottery_max_picks]

        # 计算注额（硬限额封顶：单日总注 ≤ max_daily_stake）
        effective_bankroll = self.bankroll * self.breaker_multiplier
        _cap = min(effective_bankroll, self.max_daily)  # 每日总注上限
        stable_pool = _cap * self.cfg.stable_ratio
        value_pool = _cap * self.cfg.value_ratio
        lottery_pool = _cap * self.cfg.lottery_ratio

        self._assign_stakes(stable, stable_pool)
        self._assign_stakes(value, value_pool)
        self._assign_stakes(lottery, lottery_pool)

        total = sum(p.stake for p in stable + value + lottery)
        exp_roi = (
            sum(p.stake * (p.prob * p.odds - 1) for p in stable + value + lottery)
            / max(total, 1)
        )

        return TicketPlan(
            stable_picks=stable,
            value_picks=value,
            lottery_picks=lottery,
            total_stake=round(total, 2),
            expected_roi=round(exp_roi, 4),
        )

    def _assign_stakes(self, picks: list[TicketPick], pool: float) -> None:
        """按Kelly比例分配池内资金（硬限额：单注 ≤ max_single_stake）"""
        if not picks:
            return
        total_kelly = sum(p.kelly_fraction for p in picks if p.kelly_fraction > 0)
        if total_kelly <= 0:
            return
        max_single = min(self.bankroll * self.cfg.max_single_ratio, self.max_single)

        for p in picks:
            if p.kelly_fraction <= 0:
                continue
            weight = p.kelly_fraction / total_kelly
            raw_stake = pool * weight
            raw_stake *= p.stake_reduction  # 50-60% 段彩票减注（2026-08-06）
            p.stake = round(min(raw_stake, max_single), 2)

    def summary(self, plan: TicketPlan) -> dict:
        """方案摘要"""
        def _pick(p):
            d = {"match": p.match_id, "sel": p.selection, "odds": p.odds, "stake": p.stake,
                 "prob": round(p.prob, 3)}
            # 降档标记（2026-08-06）：原搏冷降彩票 / 彩票减注 50%
            if p.stake_reduction < 1.0:
                d["downgraded"] = "stake_half"
            if p.downgrade_reason:
                d["downgrade_reason"] = p.downgrade_reason
            return d
        return {
            "stable": [_pick(p) for p in plan.stable_picks],
            "value": [_pick(p) for p in plan.value_picks],
            "lottery": [_pick(p) for p in plan.lottery_picks],
            "total_stake": plan.total_stake,
            "expected_roi": plan.expected_roi,
            "bankroll": self.bankroll,
            "breaker_multiplier": self.breaker_multiplier,
        }
