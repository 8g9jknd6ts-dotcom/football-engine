"""每周 Champion/Challenger 评估入口（workflow: backtest-weekly.yml）

职责:
1. 加载 registry，评估当前 Champion vs Challenger
2. 决定是否晋升（evaluate_promotion）
3. 输出结果到 data/state/weekly_evaluate.json
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    print("=" * 60)
    print("  每周 Champion/Challenger 评估")
    print("=" * 60)

    state_dir = ROOT / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry_path = state_dir / "model_registry.json"

    result = {"date": date.today().isoformat(), "promoted": False, "message": ""}

    if not registry_path.exists():
        print("  ⚠ 无模型注册表（首次运行，初始化空 registry）")
        from engine.learning.champion_challenger import ChampionChallenger
        cc = ChampionChallenger(registry_path)
        cc.save()
        result["message"] = "初始化空 registry"
    else:
        try:
            from engine.learning.champion_challenger import ChampionChallenger
            cc = ChampionChallenger(registry_path)
            promoted, msg = cc.evaluate_promotion()
            result["promoted"] = promoted
            result["message"] = msg
            print(f"  {'✓ 晋升!' if promoted else '✗ 保持现状'}: {msg}")
        except Exception as e:
            print(f"  ⚠ 评估失败: {e}")
            result["message"] = f"error: {e}"

    out = state_dir / "weekly_evaluate.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"  ✓ 已保存: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
