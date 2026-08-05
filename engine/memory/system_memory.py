#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚡ 系统记忆管理器 - 预测系统的大脑记忆中枢

⚠️ 已弃用（2026-08-05 死代码审计）：engine.memory 整体未被 main.py 或任何
workflow 可达，SystemMemory 无引用方。历史记忆沉淀由 data/state/*.json 承担。

保留本文件仅作历史参考。
"""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class SystemMemory:
    """
    系统统一记忆管理器
    
    记忆层级:
    1. 即时记忆: 当日预测结果
    2. 短期记忆: 近30场比赛表现
    3. 长期记忆: 历史累计统计、联赛偏差、赔率区间准确率
    4. 经验记忆: 权重优化历史、错误归因记录
    """
    
    def __init__(self, data_root: Path = None):
        if data_root:
            self.root = data_root
        else:
            self.root = Path(__file__).parent.parent.parent / "data"
        self.state_dir = self.root / "state"
        self.daily_dir = self.root / "daily"
        self.models_dir = self.root / "models"
        
        # 记忆连接
        self.match_db = self.state_dir / "match_history.db"
        self.review_ledger = self.state_dir / "review_ledger.jsonl"
        self.fusion_weights = self.state_dir / "fusion_weights.json"
        self.cppi_state = self.state_dir / "cppi.json"
        self.circuit_breaker = self.state_dir / "circuit_breaker.json"
        self.team_ratings = self.models_dir / "team_ratings.json"
        
        # 缓存
        self._cache = {}
    
    # ============================================================
    # 📊 核心统计接口
    # ============================================================
    
    def get_hit_rate(self, days: int = None, n: int = None, league: str = None) -> Dict:
        """
        查询命中率统计
        
        Args:
            days: 最近N天
            n: 最近N场
            league: 指定联赛
        """
        conn = sqlite3.connect(self.match_db)
        cur = conn.cursor()
        
        where_clauses = ["score_home IS NOT NULL"]
        params = []
        
        if days:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            where_clauses.append(f"date >= '{cutoff}'")
        if league:
            where_clauses.append(f"league LIKE '%{league}%'")
        
        where_sql = " AND ".join(where_clauses)
        
        # 方向命中率
        cur.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN 
                       (score_home > score_away AND pred_home_prob > pred_draw_prob AND pred_home_prob > pred_away_prob) OR
                       (score_home = score_away AND pred_draw_prob > pred_home_prob AND pred_draw_prob > pred_away_prob) OR
                       (score_home < score_away AND pred_away_prob > pred_home_prob AND pred_away_prob > pred_draw_prob)
                       THEN 1 ELSE 0 END) as correct
            FROM match_history
            WHERE {where_sql}
        """)
        total, correct = cur.fetchone()
        
        # 比分命中率
        cur.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN 
                       pred_top_score LIKE '%[' || score_home || ', ' || score_away || ']%'
                       THEN 1 ELSE 0 END) as correct
            FROM match_history
            WHERE {where_sql}
        """)
        _, score_correct = cur.fetchone()
        
        conn.close()
        
        hit_rate = correct / total * 100 if total > 0 else 0
        score_hit_rate = score_correct / total * 100 if total > 0 else 0
        
        return {
            "total": total,
            "correct": correct,
            "hit_rate_pct": round(hit_rate, 1),
            "score_total": total,
            "score_correct": score_correct,
            "score_hit_rate_pct": round(score_hit_rate, 1),
            "range": f"最近{days}天" if days else f"最近{n}场" if n else "全部历史"
        }
    
    def get_league_performance(self, min_matches: int = 3) -> List[Dict]:
        """分联赛命中率排名"""
        conn = sqlite3.connect(self.match_db)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT league,
                   COUNT(*) as total,
                   SUM(CASE WHEN 
                       (score_home > score_away AND pred_home_prob > pred_draw_prob AND pred_home_prob > pred_away_prob) OR
                       (score_home = score_away AND pred_draw_prob > pred_home_prob AND pred_draw_prob > pred_away_prob) OR
                       (score_home < score_away AND pred_away_prob > pred_home_prob AND pred_away_prob > pred_draw_prob)
                       THEN 1 ELSE 0 END) as correct
            FROM match_history
            WHERE score_home IS NOT NULL
            GROUP BY league
            HAVING COUNT(*) >= ?
            ORDER BY correct * 100.0 / COUNT(*) DESC
        """, (min_matches,))
        
        results = []
        for league, total, correct in cur.fetchall():
            hit_rate = correct / total * 100
            results.append({
                "league": league,
                "total": total,
                "correct": correct,
                "hit_rate_pct": round(hit_rate, 1)
            })
        
        conn.close()
        return results
    
    def get_prob_band_performance(self) -> List[Dict]:
        """不同概率区间的命中率（验证校准质量）"""
        conn = sqlite3.connect(self.match_db)
        cur = conn.cursor()
        
        bands = [
            ("40%+", 0.4, 1.0),
            ("30-40%", 0.3, 0.4),
            ("25-30%", 0.25, 0.3),
            ("20-25%", 0.2, 0.25),
            ("<20%", 0, 0.2),
        ]
        
        results = []
        for name, lo, hi in bands:
            cur.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN 
                           (score_home > score_away AND pred_home_prob >= ? AND pred_home_prob < ?) OR
                           (score_home < score_away AND pred_away_prob >= ? AND pred_away_prob < ?)
                           THEN 1 ELSE 0 END) as actual_hits
                FROM match_history
                WHERE score_home IS NOT NULL
            """, (lo, hi, lo, hi))
            
            total, actual_hits = cur.fetchone()
            
            cur.execute("""
                SELECT 
                    AVG(CASE WHEN pred_home_prob >= ? AND pred_home_prob < ? THEN pred_home_prob
                             WHEN pred_away_prob >= ? AND pred_away_prob < ? THEN pred_away_prob
                             ELSE NULL END)
                FROM match_history
                WHERE score_home IS NOT NULL
            """, (lo, hi, lo, hi))
            
            avg_pred_prob = cur.fetchone()[0] or 0
            
            if total > 0:
                actual_hit_rate = actual_hits / total * 100
                avg_pred_pct = avg_pred_prob * 100
                calibration_error = actual_hit_rate - avg_pred_pct
                
                results.append({
                    "prob_band": name,
                    "total": total,
                    "predicted_pct": round(avg_pred_pct, 1),
                    "actual_hit_rate_pct": round(actual_hit_rate, 1),
                    "calibration_error_pct": round(calibration_error, 1),
                    "status": "✅ 校准良好" if abs(calibration_error) < 5 else 
                              "⚠️ 高估" if calibration_error < -5 else "⚠️ 低估"
                })
        
        conn.close()
        return results
    
    def get_streak(self) -> Dict:
        """当前连赢/连输记录"""
        conn = sqlite3.connect(self.match_db)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT date,
                   CASE WHEN 
                       (score_home > score_away AND pred_home_prob > pred_draw_prob AND pred_home_prob > pred_away_prob) OR
                       (score_home = score_away AND pred_draw_prob > pred_home_prob AND pred_draw_prob > pred_away_prob) OR
                       (score_home < score_away AND pred_away_prob > pred_home_prob AND pred_away_prob > pred_draw_prob)
                       THEN 1 ELSE 0 END as won
            FROM match_history
            WHERE score_home IS NOT NULL
            ORDER BY date DESC, home_team
        """)
        
        rows = cur.fetchall()
        conn.close()
        
        if not rows:
            return {"current_streak": 0, "streak_type": None, "max_win_streak": 0, "max_lose_streak": 0}
        
        # 当前连续
        current_won = rows[0][1]
        current_streak = 1
        for row in rows[1:]:
            if row[1] == current_won:
                current_streak += 1
            else:
                break
        
        # 历史最大
        max_win = 0
        max_lose = 0
        run = 1
        for i in range(1, len(rows)):
            if rows[i][1] == rows[i-1][1]:
                run += 1
            else:
                if rows[i-1][1] == 1:
                    max_win = max(max_win, run)
                else:
                    max_lose = max(max_lose, run)
                run = 1
        
        if rows[-1][1] == 1:
            max_win = max(max_win, run)
        else:
            max_lose = max(max_lose, run)
        
        return {
            "current_streak": current_streak,
            "streak_type": "连赢" if current_won == 1 else "连输",
            "max_win_streak": max_win,
            "max_lose_streak": max_lose
        }
    
    def get_pnl_summary(self) -> Dict:
        """累计盈亏统计"""
        if not self.cppi_state.exists():
            return {"total_pnl": 0, "roi_pct": 0, "bankroll": 10000}
        
        with open(self.cppi_state, encoding="utf-8") as f:
            state = json.load(f)
        
        current = state.get("current_bankroll", 10000)
        initial = state.get("initial_bankroll", 10000)
        pnl = current - initial
        roi = pnl / initial * 100
        
        return {
            "initial_bankroll": initial,
            "current_bankroll": current,
            "total_pnl": round(pnl, 2),
            "roi_pct": round(roi, 2)
        }
    
    # ============================================================
    # 📋 完整记忆报告
    # ============================================================
    
    def get_memory_report(self, detailed: bool = False) -> str:
        """生成系统记忆完整报告"""
        lines = []
        lines.append("=" * 70)
        lines.append("🧠 预测系统记忆报告")
        lines.append("=" * 70)
        lines.append(f"📅 报告时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        
        # 总体表现
        overall = self.get_hit_rate()
        recent_10 = self.get_hit_rate(n=10)
        
        lines.append("📊 总体表现")
        lines.append(f"  累计预测: {overall['total']} 场已结算")
        lines.append(f"  方向命中率: {overall['hit_rate_pct']}% ({overall['correct']}/{overall['total']})")
        lines.append(f"  比分命中率: {overall['score_hit_rate_pct']}%")
        lines.append("")
        
        # 盈亏
        pnl = self.get_pnl_summary()
        lines.append("💰 资金表现")
        lines.append(f"  初始资金: {pnl['initial_bankroll']:.0f} 元")
        lines.append(f"  当前资金: {pnl['current_bankroll']:.0f} 元")
        lines.append(f"  累计盈亏: {pnl['total_pnl']:+.2f} 元")
        lines.append(f"  ROI: {pnl['roi_pct']:+.2f}%")
        lines.append("")
        
        # 连续记录
        streak = self.get_streak()
        lines.append("🔥 连续记录")
        lines.append(f"  当前: {streak['streak_type']} {streak['current_streak']} 场")
        lines.append(f"  历史最长连赢: {streak['max_win_streak']} 场")
        lines.append(f"  历史最长连输: {streak['max_lose_streak']} 场")
        lines.append("")
        
        # 联赛排名
        if detailed:
            league_perf = self.get_league_performance()
            if league_perf:
                lines.append("🏆 联赛命中率排名")
                for lp in league_perf[:5]:
                    lines.append(f"  {lp['league']}: {lp['hit_rate_pct']}% ({lp['correct']}/{lp['total']})")
                lines.append("")
            
            # 概率校准
            calib = self.get_prob_band_performance()
            lines.append("🎯 概率校准质量")
            for cb in calib:
                lines.append(f"  {cb['prob_band']}: 预测{cb['predicted_pct']}% → 实际{cb['actual_hit_rate_pct']}% {cb['status']}")
            lines.append("")
        
        lines.append("=" * 70)
        
        return "\n".join(lines)


if __name__ == "__main__":
    # 快速测试
    mem = SystemMemory()
    print(mem.get_memory_report(detailed=True))
