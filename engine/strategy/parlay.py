"""
串关（过关）方案生成器 — 竞彩实际玩法核心。

2026-08-08 新增：三票制是单场票，但竞彩玩家实际打票都是串关
（2串1/3串1/3串4 容错）。系统此前只有 parlay_report.py（历史回测），
无当日串关方案生成。strategy.json 已预留 max_parlay_stake/max_parlay_legs。

数学真相（账本 120 场校准，2026-08-08）：
- 方向命中率 43.3%；模型概率段校准：
  [0.50,0.55)=43.8% [0.55,0.60)=31.6%(塌陷区!) [0.60,0.65)=50% [0.65,0.70)=66.7% [0.70+)=50%
- 2串1 天然吃双重抽水（-10% 左右）；模型概率系统性高估 →
  按模型概率串关 = 送钱。唯一正确姿势：校准命中率算真实 EV，正 EV 才出串。

设计：
- 胆材：纯胜平负方向（排除让球/总进球等玩法），融合方向概率 ≥ min_prob(0.60)
  —— 0.55-0.60 是平局盲点塌陷区，**禁止入串**（three_ticket 同款纪律）
- 串法：2串1（全组合 EV 排序）/ 3串1 / 3串4 容错（错1场回血）
- 双 EV：model_ev（模型概率口径，展示用）+ cal_ev（账本校准口径，决策用）
- 推荐 = cal_ev > 0；否则标 ⚠ 负EV 不出注（页面展示但不推荐）
- 注额：串关池 = min(0.006×bankroll, max_parlay_stake)，1/4 Kelly×0.5
- 校准表从 review_ledger.jsonl 自动计算（样本<8 回退整体命中率），数据驱动
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

# 竞彩过关规则：每注 2 元起
STAKE_UNIT = 2.0


@dataclass
class ParlayConfig:
    min_prob: float = 0.60          # 腿最低融合方向概率（0.55-0.60 塌陷区禁入）
    max_odds: float = 3.00          # 单腿赔率上限（串关要低赔率腿）
    max_legs: int = 3               # 最高串数（受 strategy.json max_parlay_legs 约束）
    max_parlay_stake: float = 30.0  # 串关日总投入上限（strategy.json）
    kelly_discount: float = 0.5     # 串关波动大，Kelly 再打 5 折
    max_tickets: int = 3            # 最多展示几张串票
    cal_min_samples: int = 8        # 校准段最小样本（不足回退整体）
    cal_overall: float = 0.433      # 整体方向命中率（账本 120 场实测）
    cal_table: dict | None = None   # {下限: 命中率} 由账本自动计算
    # 市场腿（2026-08-08 新增）：融合概率被平局修正层层压低（8/4 后无腿可串），
    # 但账本实证"市场比模型准"（模型>市场命中 27.8% vs 模型<市场 52.3%）。
    # 市场公平概率 ≥ market_min_prob 的场次也入池（source='market'），
    # 用市场段实际命中率算期望，标注娱乐串（期望≈-15%~-40%，水钱）。
    market_min_prob: float = 0.55    # 市场腿最低市场公平概率（去水后）
    market_table: dict | None = None  # {下限: 市场段实际命中率} 账本实测


# 市场公平概率段 → 实际命中率（review_ledger 131 场实测，2026-08-08）
# [0.55,0.65): 47.1% (17场8中) | [0.65,0.75): 54.5% (11场6中) | [0.75+): 100% (2场2中，样本小)
# 合并 0.65+ = 61.5% (13场8中)
DEFAULT_MARKET_TABLE = {0.65: 0.615, 0.55: 0.471}


@dataclass
class ParlayLeg:
    match_id: str
    home_team: str
    away_team: str
    competition: str
    selection: str      # home / draw / away
    odds: float
    prob: float         # 融合方向概率（模型口径）
    cal_prob: float = 0.0   # 账本校准命中率（2026-08-08：模型概率系统性高估）
    market_prob: float = 0.0  # 市场公平概率（三方向赔率去水）
    source: str = "fusion"    # "fusion"（融合池）/ "market"（市场池）
    hit_prob: float = 0.0   # 该腿"实际命中率"：fusion=账本校准 / market=市场段实测


@dataclass
class ParlayTicket:
    parlay_type: str    # "2串1" / "3串1" / "3串4"
    legs: list[ParlayLeg] = field(default_factory=list)
    total_odds: float = 0.0     # 全中总赔率
    model_ev: float = 0.0       # 模型口径期望盈利（元）
    cal_ev: float = 0.0         # 实际口径期望盈利（元）——决策依据
    cal_roi: float = 0.0        # 实际 ROI
    hit_prob_cal: float = 0.0   # 实际全中概率
    recommended: bool = False   # cal_ev>0 才推荐
    stake: float = 0.0          # 投入（元）
    n_bets: int = 0             # 注数
    source: str = "calibrated"  # "calibrated"（校准正EV ⭐）/ "market"（娱乐串 🎯）
    market_ev: float = 0.0      # 市场口径期望盈利（元，展示用）
    market_roi: float = 0.0     # 市场口径 ROI
    potential: float = 0.0      # 理论最高奖金（元）
    worst_win: float = 0.0      # 最差命中回报（2串1/3串1=0；3串4=错1场中2串1）
    note: str = ""              # 容错/说明

    def to_dict(self) -> dict:
        return {
            "type": self.parlay_type,
            "legs": [{
                "match": l.match_id, "home": l.home_team, "away": l.away_team,
                "league": l.competition, "sel": l.selection,
                "odds": round(l.odds, 2), "prob": round(l.prob, 3),
                "cal_prob": round(l.cal_prob, 3),
                "market_prob": round(l.market_prob, 3),
                "source": l.source,
                "hit_prob": round(l.hit_prob, 3) if l.hit_prob else None,
            } for l in self.legs],
            "total_odds": round(self.total_odds, 2),
            "model_ev": round(self.model_ev, 2) if self.model_ev is not None else None,
            "cal_ev": round(self.cal_ev, 2),
            "cal_roi": round(self.cal_roi, 4),
            "market_ev": round(self.market_ev, 2) if self.market_ev is not None else None,
            "market_roi": round(self.market_roi, 4) if self.market_roi is not None else None,
            "hit_prob_cal": round(self.hit_prob_cal, 3),
            "recommended": self.recommended,
            "stake": round(self.stake, 2),
            "n_bets": self.n_bets,
            "potential": round(self.potential, 2),
            "worst_win": round(self.worst_win, 2),
            "note": self.note,
            "source": self.source,
        }


def load_calibration(
    ledger_path: str | Path = "data/state/review_ledger.jsonl",
    overall: float = 0.433,
    min_samples: int = 8,
) -> dict:
    """从结算账本自动计算概率段→命中率校准表。

    口径：final_prob[best_selection]（融合概率 argmax）按段统计实际方向命中率。
    样本不足的段回退整体命中率（保守：宁可低估不可高估，串关亏钱伤士气）。
    """
    recs = []
    try:
        with open(ledger_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        return {}
    if len(recs) < 20:
        return {}

    bins = [(0.35, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55),
            (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.01)]
    table = {}
    for lo, hi in bins:
        cnt = hit = 0
        for r in recs:
            fp = r.get("final_prob") or []
            bs = r.get("best_selection")
            if bs is None or bs >= len(fp):
                continue
            p = fp[bs]
            if lo <= p < hi:
                cnt += 1
                if r.get("hit"):
                    hit += 1
        if cnt >= min_samples:
            table[lo] = round(hit / cnt, 4)
        # 样本不足段不填 → 回退 overall
    return {"table": table, "overall": round(sum(1 for r in recs if r.get("hit")) / len(recs), 4),
            "n": len(recs), "min_samples": min_samples}


class ParlayBuilder:
    """当日串关方案生成器（校准 EV 驱动）"""

    def __init__(
        self,
        bankroll: float = 5000.0,
        limits: dict | None = None,
        config: ParlayConfig | None = None,
        calibration: dict | None = None,
    ):
        self.bankroll = bankroll
        self.limits = limits or {}
        self.cfg = config or ParlayConfig()
        _legs = self.limits.get("max_parlay_legs")
        if _legs:
            self.cfg.max_legs = min(int(_legs), 3)
        _stake = self.limits.get("max_parlay_stake")
        if _stake:
            self.cfg.max_parlay_stake = float(_stake)
        # 校准表：账本驱动
        self.cal = calibration or {}
        self.cal_table = self.cal.get("table") or self.cfg.cal_table or {}
        self.cal_overall = self.cal.get("overall") or self.cfg.cal_overall

    # ---------- 校准概率 ----------
    def _cal_prob(self, p: float) -> float:
        """模型概率 → 校准命中率（分段查表，样本不足/无表回退整体）"""
        if not self.cal_table:
            return self.cal_overall
        lo = None
        for k in sorted(self.cal_table):
            if p >= k:
                lo = k
            else:
                break
        if lo is None:
            return self.cal_overall
        return self.cal_table[lo]

    # ---------- 胆池 ----------
    def _market_prob(self, pred: dict, sel: str) -> float:
        """三方向赔率去水算市场公平概率（竞彩赔率含约 5-8% 水钱）"""
        key = f"{sel}_odds"
        odds = pred.get(key) or 0
        if not odds or odds <= 1.0:
            return 0.0
        inv = {"home": 1.0 / (pred.get("home_odds") or 0) if pred.get("home_odds") else 0.0,
               "draw": 1.0 / (pred.get("draw_odds") or 0) if pred.get("draw_odds") else 0.0,
               "away": 1.0 / (pred.get("away_odds") or 0) if pred.get("away_odds") else 0.0}
        total_inv = sum(inv.values())
        if total_inv <= 0:
            return 0.0
        return (1.0 / odds) / total_inv

    def _leg_hit_prob(self, leg: ParlayLeg) -> float:
        """腿的'实际命中率'：fusion 腿用账本校准表，market 腿用市场段实测表"""
        if leg.hit_prob:
            return leg.hit_prob
        if leg.source == "market":
            mtab = self.cfg.market_table or DEFAULT_MARKET_TABLE
            for lo in sorted(mtab, reverse=True):
                if leg.market_prob >= lo:
                    return mtab[lo]
            return self.cfg.cal_overall
        return leg.cal_prob or self.cfg.cal_overall

    def _build_pool(
        self, candidates: list[dict], predictions: list[dict] | None = None
    ) -> list[ParlayLeg]:
        """可串腿：纯胜平负。
        - fusion 腿：融合方向概率 ≥ min_prob（避开 0.55-0.60 塌陷区）
        - market 腿：市场公平概率 ≥ market_min_prob（2026-08-08 新增，
          融合概率被平局修正层层压低后无腿可串，但账本实证市场比模型准）

        队名优先取 candidates（main.py 构造时若带），否则用 match_id 反查
        predictions（2026-08-08 修复：main.py candidates 未带队名，串票腿曾显示空队名）。
        """
        pred_map = {p.get("match_id"): p for p in (predictions or [])}
        pool: list[ParlayLeg] = []
        for c in candidates:
            sel = c.get("selection", "")
            if sel not in ("home", "draw", "away"):
                continue
            odds = c.get("odds", 0) or 0
            prob = c.get("prob", 0) or 0
            if odds > self.cfg.max_odds:
                continue
            if odds < 1.10:
                continue
            match_id = c.get("match_id", "")
            pred = pred_map.get(match_id, {})
            market_prob = self._market_prob(pred, sel)
            # 融合腿（模型口径门槛）
            if prob >= self.cfg.min_prob:
                leg = ParlayLeg(
                    match_id=match_id,
                    home_team=c.get("home_team") or pred.get("home_team", ""),
                    away_team=c.get("away_team") or pred.get("away_team", ""),
                    competition=c.get("competition") or pred.get("competition", ""),
                    selection=sel, odds=odds, prob=prob,
                    cal_prob=self._cal_prob(prob),
                    market_prob=market_prob, source="fusion",
                )
                leg.hit_prob = leg.cal_prob
                pool.append(leg)
            # 市场腿（市场口径门槛；账本实证市场比模型准）
            elif market_prob >= self.cfg.market_min_prob:
                leg = ParlayLeg(
                    match_id=match_id,
                    home_team=c.get("home_team") or pred.get("home_team", ""),
                    away_team=c.get("away_team") or pred.get("away_team", ""),
                    competition=c.get("competition") or pred.get("competition", ""),
                    selection=sel, odds=odds, prob=prob,
                    cal_prob=self._cal_prob(prob),
                    market_prob=market_prob, source="market",
                )
                leg.hit_prob = self._leg_hit_prob(leg)  # 市场段实测命中率
                pool.append(leg)
        pool.sort(key=lambda l: max(l.prob, l.market_prob), reverse=True)
        return pool

    # ---------- 串票构造 ----------
    def _finish(self, t: ParlayTicket) -> ParlayTicket:
        """按腿实际命中率（fusion→账本校准 / market→市场段实测）填 EV 口径"""
        p_cal = 1.0
        p_mod = 1.0
        p_mkt = 1.0
        for l in t.legs:
            p_cal *= self._leg_hit_prob(l)
            p_mod *= l.prob
            p_mkt *= (l.market_prob or l.prob)
        o = t.total_odds
        t.hit_prob_cal = p_cal
        t.model_ev = t.potential * p_mod - t.stake
        t.cal_ev = t.potential * p_cal - t.stake
        t.cal_roi = o * p_cal - 1.0
        t.market_ev = t.potential * p_mkt - t.stake
        t.market_roi = o * p_mkt - 1.0
        # 推荐 = 实际 EV > 0；市场腿串票默认娱乐（几乎必然负 EV，标注即可）
        t.recommended = t.cal_ev > 0
        t.source = "calibrated" if all(l.source == "fusion" for l in t.legs) else "market"
        return t

    def _make_2in1(self, l1: ParlayLeg, l2: ParlayLeg) -> ParlayTicket:
        o = l1.odds * l2.odds
        stake = STAKE_UNIT
        t = ParlayTicket(
            parlay_type="2串1", legs=[l1, l2], total_odds=o,
            stake=stake, n_bets=1, potential=stake * o,
            worst_win=0.0, note="两场全中才赢",
        )
        return self._finish(t)

    def _make_3in1(self, legs: list[ParlayLeg]) -> ParlayTicket:
        o = 1.0
        for l in legs:
            o *= l.odds
        stake = STAKE_UNIT
        t = ParlayTicket(
            parlay_type="3串1", legs=legs, total_odds=o,
            stake=stake, n_bets=1, potential=stake * o,
            worst_win=0.0, note="三场全中才赢",
        )
        return self._finish(t)

    def _make_3in4(self, legs: list[ParlayLeg]) -> ParlayTicket:
        """3串4 容错：3 注 2串1 + 1 注 3串1 = 4 注，投入 8 元。错 1 场仍回血。"""
        l1, l2, l3 = legs
        o1, o2, o3 = l1.odds, l2.odds, l3.odds
        p1c, p2c, p3c = self._leg_hit_prob(l1), self._leg_hit_prob(l2), self._leg_hit_prob(l3)
        p1m, p2m, p3m = (l1.market_prob or l1.prob), (l2.market_prob or l2.prob), (l3.market_prob or l3.prob)
        n_bets = 4
        stake = STAKE_UNIT * n_bets
        # 每注期望回报（实际命中率口径）
        e12 = STAKE_UNIT * o1 * o2 * p1c * p2c
        e13 = STAKE_UNIT * o1 * o3 * p1c * p3c
        e23 = STAKE_UNIT * o2 * o3 * p2c * p3c
        e123 = STAKE_UNIT * o1 * o2 * o3 * p1c * p2c * p3c
        exp_return = e12 + e13 + e23 + e123
        # 市场口径（展示用）
        m12 = STAKE_UNIT * o1 * o2 * p1m * p2m
        m13 = STAKE_UNIT * o1 * o3 * p1m * p3m
        m23 = STAKE_UNIT * o2 * o3 * p2m * p3m
        m123 = STAKE_UNIT * o1 * o2 * o3 * p1m * p2m * p3m
        market_return = m12 + m13 + m23 + m123
        potential = STAKE_UNIT * (o1 * o2 + o1 * o3 + o2 * o3 + o1 * o2 * o3)
        worst = STAKE_UNIT * min(o1 * o2, o1 * o3, o2 * o3)
        t = ParlayTicket(
            parlay_type="3串4", legs=legs, total_odds=potential / stake,
            stake=stake, n_bets=n_bets, potential=potential, worst_win=worst,
            note=f"容错：错 1 场仍中 1 注 2串1（回 ¥{worst:.0f}），全中 ¥{potential:.0f}",
        )
        # 3串4 的期望/ROI 按全注口径单独算
        t.hit_prob_cal = p1c * p2c * p3c
        t.model_ev = None  # 3串4 模型口径不展示（用实际口径）
        t.cal_ev = exp_return - stake
        t.cal_roi = exp_return / stake - 1.0
        t.market_ev = market_return - stake
        t.market_roi = market_return / stake - 1.0
        t.recommended = t.cal_ev > 0
        t.source = "calibrated" if all(l.source == "fusion" for l in t.legs) else "market"
        return t

    # ---------- 主入口 ----------
    def build(
        self,
        candidates: list[dict],
        ticket_plan=None,
        predictions: list[dict] | None = None,
    ) -> list[ParlayTicket]:
        pool = self._build_pool(candidates, predictions)
        if not pool:
            return []
        n = min(len(pool), self.cfg.max_legs)
        pool = pool[:n]

        tickets: list[ParlayTicket] = []
        for l1, l2 in combinations(pool, 2):
            tickets.append(self._make_2in1(l1, l2))
        if n >= 3 and self.cfg.max_legs >= 3:
            for legs3 in combinations(pool, 3):
                tickets.append(self._make_3in1(list(legs3)))
            top3 = pool[:3]
            tickets.append(self._make_3in4(top3))

        if not tickets:
            return []

        # 排序：推荐优先，其次校准EV，最多 max_tickets
        tickets.sort(key=lambda t: (t.recommended, t.cal_ev), reverse=True)
        tickets = tickets[: self.cfg.max_tickets]

        # 注额：推荐串分配串关池（min(0.006×bankroll, max_parlay_stake)）
        pool_cap = min(self.bankroll * 0.006, self.cfg.max_parlay_stake)
        recs = [t for t in tickets if t.recommended]
        for t in tickets:
            if not t.recommended:
                # 娱乐串：1 注小注（2026-08-08：用户要看到实际可打的串；
                # 期望为负但小注参与，页面明确标注）
                t.stake = STAKE_UNIT
                t.potential = round(t.total_odds * t.stake, 2)
                t.cal_ev = round(t.stake * t.cal_roi, 2)
        if recs:
            weights = []
            for t in recs:
                denom = max(t.total_odds - 1.0, 0.1)
                kelly_f = max(t.cal_ev / (t.stake * denom), 0.0)
                weights.append(kelly_f * self.cfg.kelly_discount)
            wsum = sum(weights)
            if wsum <= 0:
                weights = [1.0 / len(recs)] * len(recs)
                wsum = 1.0
            for t, w in zip(recs, weights):
                raw = pool_cap * w / wsum
                t.stake = round(max(min(raw, pool_cap * 0.6), STAKE_UNIT), 2)
                # 重算注额口径 EV（2026-08-08 修复：potential/cal_ev 此前
                # 仍按 2 元口径，与实际 stake 不符；cal_roi 为每元回报率，
                # 三种串票统一 cal_ev = stake × cal_roi）
                t.potential = round(t.total_odds * t.stake, 2)
                t.cal_ev = round(t.stake * t.cal_roi, 2)
        return tickets


if __name__ == "__main__":
    import sys

    day = sys.argv[1] if len(sys.argv) > 1 else "2026-07-26"
    cal = load_calibration()
    print(f"校准表: {cal.get('n', 0)} 场 | 整体 {cal.get('overall', 0.433):.1%} | "
          f"分段 { {f'{k:.2f}+': f'{v:.0%}' for k, v in (cal.get('table') or {}).items()} }")
    preds = json.load(open(f"data/daily/{day}/predictions.json"))
    if isinstance(preds, dict):
        preds = preds.get("predictions", [])
    cands = []
    for p in preds:
        d = p.get("direction")
        if not d:
            continue
        prob = p.get("direction_prob") or p.get(f"{d}_win_prob", 0)
        odds = p.get(f"{d}_odds", 0)
        if not odds:
            continue
        cands.append({
            "match_id": p["match_id"],
            "home_team": p.get("home_team", ""),
            "away_team": p.get("away_team", ""),
            "competition": p.get("competition", ""),
            "selection": d, "odds": odds, "prob": prob,
        })
    b = ParlayBuilder(bankroll=5000, limits={"max_parlay_legs": 3, "max_parlay_stake": 30},
                      calibration=cal)
    tickets = b.build(cands, None, preds)
    pool = b._build_pool(cands, preds)
    print(f"{day}: 候选 {len(cands)} 场, 可串 {len(pool)} 腿, 出 {len(tickets)} 张串票")
    for t in tickets:
        legs = " + ".join(f"{l.home_team[:4]}({l.selection[:1]})@{l.odds:.2f}" for l in t.legs)
        flag = "⭐推荐" if t.recommended else "⚠负EV"
        print(f"  [{t.parlay_type}]{flag} {legs} | 总赔率{t.total_odds:.2f} "
              f"校准命中率{t.hit_prob_cal:.0%} 校准EV{t.cal_ev:+.1f}元 ROI{t.cal_roi:+.0%} "
              f"投入¥{t.stake:.0f} 最高¥{t.potential:.0f}")
