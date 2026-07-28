"""Rho MLE 拟合器

参考 Gunnerista/worldcup-predictor 的方法:
- 不用论文的 -0.10，也不手调
- 用历史数据通过黄金分割搜索最大化对数似然来拟合 rho
- 拟合完成后写入 config，Dixon-Coles 模型使用拟合值

用法:
    fitter = RhoFitter(db_path)
    rho = fitter.fit()
    # → 写入 config/prediction.json
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path


class RhoFitter:
    """Dixon-Coles rho 参数 MLE 拟合器"""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def fit(self) -> dict:
        """从历史数据拟合 rho

        Returns:
            {"rho": float, "n_samples": int, "log_likelihood": float}
        """
        matches = self._load_matches()
        if len(matches) < 20:
            print(f"  [Rho MLE] 样本不足 ({len(matches)} < 20)，保持默认 rho")
            return {"rho": None, "n_samples": len(matches), "log_likelihood": 0.0}

        # 黄金分割搜索 rho in [-0.5, 0.1]
        lo, hi = -0.5, 0.1
        phi = (math.sqrt(5) - 1) / 2  # 0.618

        def neg_ll(rho):
            return -self._log_likelihood(rho, matches)

        for _ in range(50):
            a = hi - phi * (hi - lo)
            b = lo + phi * (hi - lo)
            if neg_ll(a) < neg_ll(b):
                hi = b
            else:
                lo = a

        best_rho = (lo + hi) / 2
        best_ll = self._log_likelihood(best_rho, matches)

        result = {
            "rho": round(best_rho, 4),
            "n_samples": len(matches),
            "log_likelihood": round(best_ll, 2),
        }
        print(f"  [Rho MLE] rho={best_rho:.4f} (n={len(matches)}, LL={best_ll:.1f})")
        return result

    def _log_likelihood(self, rho: float, matches: list[dict]) -> float:
        """计算 Dixon-Coles 对数似然"""
        ll = 0.0
        for m in matches:
            h_xg = m["home_xg"]
            a_xg = m["away_xg"]
            h_score = m["home_score"]
            a_score = m["away_score"]

            if h_xg is None or a_xg is None or h_score is None or a_score is None:
                continue

            # Dixon-Coles 比分概率
            p = self._score_prob(h_xg, a_xg, h_score, a_score, rho)
            if p > 0:
                ll += math.log(p)

        return ll

    @staticmethod
    def _score_prob(
        lambda_h: float, lambda_a: float,
        h: int, a: int, rho: float,
    ) -> float:
        """Dixon-Coles 单比分概率"""
        # 独立泊松
        p_h = math.exp(-lambda_h) * (lambda_h ** h) / math.factorial(h)
        p_a = math.exp(-lambda_a) * (lambda_a ** a) / math.factorial(a)
        p = p_h * p_a

        # 低比分修正
        if h == 0 and a == 0:
            p *= 1 - lambda_h * lambda_a * rho
        elif h == 0 and a == 1:
            p *= 1 + lambda_h * rho
        elif h == 1 and a == 0:
            p *= 1 + lambda_a * rho
        elif h == 1 and a == 1:
            p *= 1 - rho

        return max(1e-15, p)

    def _load_matches(self) -> list[dict]:
        """从 MatchDB 加载有 xG 和比分的比赛"""
        matches = []
        if not self.db_path.exists():
            return matches

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT pred_home_xg, pred_away_xg, score_home, score_away "
                "FROM match_history "
                "WHERE pred_home_xg IS NOT NULL "
                "AND pred_away_xg IS NOT NULL "
                "AND score_home IS NOT NULL "
                "AND score_away IS NOT NULL "
                "AND pred_home_xg > 0.1 AND pred_away_xg > 0.1"
            ).fetchall()
            for r in rows:
                matches.append({
                    "home_xg": r["pred_home_xg"],
                    "away_xg": r["pred_away_xg"],
                    "home_score": r["score_home"],
                    "away_score": r["score_away"],
                })
        except Exception:
            pass
        finally:
            conn.close()

        return matches
