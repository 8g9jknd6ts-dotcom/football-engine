"""每周模型评估入口（workflow: backtest-weekly.yml）

职责:
1. 从 fusion_optimizer 决策日志（optimizer_log.jsonl）读取真实决策流
2. 输出本周权重 vs 演化轨迹、Brier 趋势、hold/swap 判定
3. 结果写入 data/state/weekly_evaluate.json

⚠️ 2026-08-05 改造：原实现是 ChampionChallenger 空壳——model_registry.json
从未被任何代码写入（main 无引用、无影子数据维护），每周跑永远输出
"初始化空 registry"，是"看起来在学习实则空转"。融合权重学习已由
FusionOptimizer（optimizer_log.jsonl，106+ 条真实反事实验证决策）承担，
本模块改为直接读取该真实日志做周度报告，不再假装有独立的 champion/challenger。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    print("=" * 60)
    print("  每周模型评估（读 fusion_optimizer 决策日志）")
    print("=" * 60)

    state_dir = ROOT / "data" / "state"
    log_path = state_dir / "optimizer_log.jsonl"

    result = {"date": date.today().isoformat(), "source": "optimizer_log.jsonl"}

    if not log_path.exists():
        result["message"] = "无优化器日志（融合学习尚未启动）"
        (state_dir / "weekly_evaluate.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print("  ⚠ 无优化器日志")
        return 0

    # 读取全部决策
    decisions = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not decisions:
        result["message"] = "日志为空"
        (state_dir / "weekly_evaluate.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print("  ⚠ 日志为空")
        return 0

    # 统计决策类型
    actions = {}
    for d in decisions:
        actions[d.get("action", "?")] = actions.get(d.get("action", "?"), 0) + 1

    # 当前 champion（最新一条）
    latest = decisions[-1]
    champion = latest.get("champion", {})
    first = decisions[0]
    initial = first.get("champion", {})

    # Brier 趋势：取有 counterfactual Brier 的决策
    brier_series = []
    for d in decisions:
        m = d.get("metrics", {})
        if m.get("champion_brier") is not None:
            brier_series.append(
                {
                    "timestamp": d.get("timestamp", "")[:10],
                    "champion_brier": round(m["champion_brier"], 4),
                    "candidate_brier": round(m.get("candidate_brier", 0), 4),
                    "improvement": round(m.get("improvement", 0), 4),
                }
            )
    # 近 10 次平均改进
    recent = brier_series[-10:] if len(brier_series) >= 10 else brier_series
    avg_improvement = (
        sum(b["improvement"] for b in recent) / len(recent) if recent else None
    )

    # 权重演化轨迹
    weight_trace = []
    for d in decisions:
        c = d.get("champion", {})
        weight_trace.append(
            {
                "timestamp": d.get("timestamp", "")[:16],
                "model": round(c.get("model", 0), 4),
                "market": round(c.get("market", 0), 4),
                "djyy": round(c.get("djyy", 0), 4),
            }
        )

    result.update(
        {
            "n_decisions": len(decisions),
            "action_counts": actions,
            "champion_now": champion,
            "champion_initial": initial,
            "weight_trace": weight_trace,
            "brier_series": brier_series,
            "recent_avg_improvement": round(avg_improvement, 4) if avg_improvement is not None else None,
            "message": (
                f"{len(decisions)} 次决策: {actions}；"
                f"近10次平均 Brier 改进 {avg_improvement:.4f}（阈值 0.005，"
                f"{'达标可考虑换权' if avg_improvement and avg_improvement >= 0.005 else '未达标保持 hold'}）"
            ),
        }
    )

    out = state_dir / "weekly_evaluate.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"  ✓ 决策数: {len(decisions)} | 动作分布: {actions}")
    print(f"  ✓ 权重演化: model {initial.get('model')}→{champion.get('model')} | "
          f"market {initial.get('market')}→{champion.get('market')} | "
          f"djyy {initial.get('djyy')}→{champion.get('djyy')}")
    print(f"  ✓ 近10次平均改进: {avg_improvement:.4f}" if avg_improvement is not None else "  ✓ 无 Brier 数据")
    print(f"  ✓ 已保存: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
