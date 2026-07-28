"""Walk-forward 回测增强

基于现有 ts_split.py，增加:
1. 时间衰减权重评估
2. RPS 指标输出（已有，增强）
3. ECE 校准误差
4. 平局专项分析
5. 混合策略对比

用法:
    from engine.backtest.walk_forward import WalkForwardEvaluator
    evaluator = WalkForwardEvaluator(data_dir)
    report = evaluator.evaluate()
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

from .ts_split import brier_score, ranked_probability_score, log_loss
from ..prediction.time_decay import time_decay_weights, weighted_brier, weighted_rps


class WalkForwardEvaluator:
    """Walk-forward 回测评估器

    比简单 backtest 更严格:
    - 按时间切分，杜绝未来信息泄露
    - 加入时间衰减权重（近期比赛更重要）
    - 专项评估平局预测能力
    - 对比纯模型 vs 市场赔率 vs 混合策略
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.daily_dir = data_dir / "daily"
        self.ledger_path = data_dir / "state" / "review_ledger.jsonl"

    def evaluate(self) -> dict:
        """执行完整评估"""
        records = self._load_records()
        if not records:
            return {"error": "no records"}

        # 按时间排序
        records.sort(key=lambda r: r.get("date", ""))

        # 基础指标
        probs = np.array([r["final_prob"] for r in records])
        actuals = np.array([r["actual_idx"] for r in records])

        brier = brier_score(probs, actuals)
        rps = ranked_probability_score(probs, actuals)
        ll = log_loss(probs, actuals)

        # 时间衰减加权
        dates = [r["date"] for r in records]
        weights = time_decay_weights(dates, half_life_days=180)
        w_brier = weighted_brier(
            [tuple(r["final_prob"]) for r in records],
            [r["actual_idx"] for r in records],
            weights,
        )
        w_rps = weighted_rps(
            [tuple(r["final_prob"]) for r in records],
            [r["actual_idx"] for r in records],
            weights,
        )

        # 命中率
        pred_outcomes = np.argmax(probs, axis=1)
        hits = (pred_outcomes == actuals).sum()
        hit_rate = hits / len(records)

        # 平局专项
        draw_analysis = self._draw_analysis(records)

        # 策略对比
        strategy_comparison = self._strategy_comparison(records)

        # ECE
        ece = self._compute_ece(probs, actuals)

        # 按联赛分组
        by_league = self._by_league(records)

        report = {
            "total_matches": len(records),
            "date_range": [records[0]["date"], records[-1]["date"]],
            "metrics": {
                "brier": round(brier, 4),
                "weighted_brier": round(w_brier, 4),
                "rps": round(rps, 4),
                "weighted_rps": round(w_rps, 4),
                "log_loss": round(ll, 4),
                "hit_rate": round(hit_rate, 4),
                "ece": round(ece, 4),
            },
            "draw_analysis": draw_analysis,
            "strategy_comparison": strategy_comparison,
            "by_league": by_league,
        }
        return report

    def _load_records(self) -> list[dict]:
        """从 review_ledger 加载有 final_prob 和 actual_idx 的记录"""
        if not self.ledger_path.exists():
            return []

        records = []
        for line in self.ledger_path.read_text().strip().split("\n"):
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("final_prob") and r.get("actual_idx") is not None:
                    records.append(r)
            except Exception:
                continue

        return records

    @staticmethod
    def _draw_analysis(records: list[dict]) -> dict:
        """平局预测专项分析"""
        total = len(records)
        actual_draws = sum(1 for r in records if r["actual_idx"] == 1)
        predicted_draws = sum(1 for r in records if r.get("best_selection") == 1)

        # 模型平局概率分布
        draw_probs = [r["final_prob"][1] for r in records]
        max_draw_prob = max(draw_probs) if draw_probs else 0
        avg_draw_prob = sum(draw_probs) / len(draw_probs) if draw_probs else 0

        # 市场平局概率对比
        market_draws = []
        for r in records:
            mf = r.get("market_fair")
            if mf and len(mf) >= 3:
                market_draws.append(mf[1])

        # 市场平局≥28%的实际平局率
        high_draw_market = [
            r for r in records
            if r.get("market_fair") and r["market_fair"][1] >= 0.28
        ]
        actual_in_high = sum(1 for r in high_draw_market if r["actual_idx"] == 1)

        return {
            "actual_draws": actual_draws,
            "actual_draw_rate": round(actual_draws / total, 4) if total else 0,
            "predicted_draws": predicted_draws,
            "predicted_draw_rate": round(predicted_draws / total, 4) if total else 0,
            "max_draw_prob": round(max_draw_prob, 4),
            "avg_draw_prob": round(avg_draw_prob, 4),
            "avg_market_draw": round(
                sum(market_draws) / len(market_draws), 4
            ) if market_draws else None,
            "market_high_draw_n": len(high_draw_market),
            "market_high_draw_actual": actual_in_high,
            "market_high_draw_rate": round(
                actual_in_high / len(high_draw_market), 4
            ) if high_draw_market else 0,
        }

    @staticmethod
    def _strategy_comparison(records: list[dict]) -> dict:
        """对比不同预测策略"""
        results = {}

        # 1. 纯模型（当前 final_prob argmax）
        model_hits = sum(1 for r in records if r.get("hit"))
        results["model"] = {
            "hit_rate": round(model_hits / len(records), 4),
            "n": len(records),
        }

        # 2. 纯市场赔率 argmax
        market_hits = 0
        market_n = 0
        for r in records:
            mf = r.get("market_fair")
            if mf and len(mf) >= 3:
                market_best = max(range(3), key=lambda i: mf[i])
                if market_best == r["actual_idx"]:
                    market_hits += 1
                market_n += 1
        results["market"] = {
            "hit_rate": round(market_hits / market_n, 4) if market_n else 0,
            "n": market_n,
        }

        # 3. 混合策略: 市场平局≥25%且排前二时选平局
        hybrid_hits = 0
        hybrid_n = 0
        for r in records:
            mf = r.get("market_fair")
            fp = r.get("final_prob", [0, 0, 0])
            if mf and len(mf) >= 3:
                # 市场平局排前二
                market_ranked = sorted(range(3), key=lambda i: mf[i], reverse=True)
                draw_in_top2 = 1 in market_ranked[:2]
                if mf[1] >= 0.25 and draw_in_top2:
                    pick = 1  # 选平局
                else:
                    pick = max(range(3), key=lambda i: fp[i])
                if pick == r["actual_idx"]:
                    hybrid_hits += 1
                hybrid_n += 1
        results["hybrid_v2"] = {
            "hit_rate": round(hybrid_hits / hybrid_n, 4) if hybrid_n else 0,
            "n": hybrid_n,
        }

        # 4. 市场平局≥28%选平局
        aggressive_hits = 0
        aggressive_n = 0
        for r in records:
            mf = r.get("market_fair")
            fp = r.get("final_prob", [0, 0, 0])
            if mf and len(mf) >= 3:
                if mf[1] >= 0.28:
                    pick = 1
                else:
                    pick = max(range(3), key=lambda i: fp[i])
                if pick == r["actual_idx"]:
                    aggressive_hits += 1
                aggressive_n += 1
        results["market_d28"] = {
            "hit_rate": round(aggressive_hits / aggressive_n, 4) if aggressive_n else 0,
            "n": aggressive_n,
        }

        return results

    @staticmethod
    def _compute_ece(probs: np.ndarray, actuals: np.ndarray, n_bins: int = 10) -> float:
        """Expected Calibration Error"""
        ece = 0.0
        n = len(probs)
        for idx in range(probs.shape[1]):
            y_pred = probs[:, idx]
            y_true = (actuals == idx).astype(float)
            bin_edges = np.linspace(0, 1, n_bins + 1)
            for b in range(n_bins):
                mask = (y_pred >= bin_edges[b]) & (y_pred < bin_edges[b + 1])
                if mask.sum() > 0:
                    ece += abs(y_pred[mask].mean() - y_true[mask].mean()) * mask.sum() / n
        return ece

    @staticmethod
    def _by_league(records: list[dict]) -> dict:
        """按联赛分组"""
        groups = defaultdict(list)
        for r in records:
            lg = r.get("league", "unknown")
            groups[lg].append(r)

        result = {}
        for lg, rs in sorted(groups.items(), key=lambda x: -len(x[1])):
            if len(rs) < 3:
                continue
            hits = sum(1 for r in rs if r.get("hit"))
            probs = np.array([r["final_prob"] for r in rs])
            actuals = np.array([r["actual_idx"] for r in rs])
            result[lg] = {
                "n": len(rs),
                "hit_rate": round(hits / len(rs), 4),
                "brier": round(brier_score(probs, actuals), 4),
                "rps": round(ranked_probability_score(probs, actuals), 4),
            }
        return result
