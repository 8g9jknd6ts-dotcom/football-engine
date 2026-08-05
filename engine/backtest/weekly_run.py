"""每周回测 + 参数优化入口（workflow: backtest-weekly.yml）

职责:
1. Walk-forward 回测评估当前模型
2. 融合权重优化器走一步（champion/challenger 闭环）
3. 结果写入 data/state/weekly_backtest.json
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    print("=" * 60)
    print("  每周回测 + 参数优化")
    print("=" * 60)

    data_dir = ROOT / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    report = {"date": date.today().isoformat(), "steps": {}}

    # 1. Walk-forward 回测
    try:
        from engine.backtest.walk_forward import WalkForwardEvaluator
        evaluator = WalkForwardEvaluator(data_dir)
        wf_report = evaluator.evaluate()
        # WalkForwardEvaluator 可能返回 dict 或对象，兼容处理
        if isinstance(wf_report, dict):
            report["steps"]["walk_forward"] = {
                k: v for k, v in wf_report.items() if not isinstance(v, (dict, list))
            }
        else:
            report["steps"]["walk_forward"] = {
                "hit_rate": getattr(wf_report, "hit_rate", None),
                "rps": getattr(wf_report, "rps", None),
                "brier": getattr(wf_report, "brier", None),
                "n_matches": getattr(wf_report, "n_matches", None),
            }
        print(f"  ✓ Walk-forward: {json.dumps(report['steps']['walk_forward'], ensure_ascii=False)}")
    except Exception as e:
        print(f"  ⚠ Walk-forward 失败: {e}")
        report["steps"]["walk_forward"] = {"error": str(e)}

    # 2. 融合权重优化（走一步闭环）
    try:
        from engine.review.post_match import ReviewLedger
        from engine.learning.fusion_optimizer import FusionOptimizer

        # 2026-08-05 修复：账本实际文件名是 review_ledger.jsonl（append-only 滚动账本），
        # 之前误写成 .json → 读到 0 键 → 融合优化基于空账本做假决策。
        ledger = ReviewLedger(state_dir / "review_ledger.jsonl")
        opt = FusionOptimizer(state_dir / "fusion_weights.json", ledger)
        decision = opt.step()
        report["steps"]["fusion_optimizer"] = {
            "decision": getattr(decision, "action", str(decision)),
            "champion": getattr(decision, "champion", None),
            "message": getattr(decision, "message", None),
        }
        print(f"  ✓ 融合优化: {json.dumps(report['steps']['fusion_optimizer'], ensure_ascii=False)}")
    except Exception as e:
        print(f"  ⚠ 融合优化跳过: {e}")
        report["steps"]["fusion_optimizer"] = {"error": str(e)}

    # 保存报告
    out = state_dir / "weekly_backtest.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  ✓ 报告已保存: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
