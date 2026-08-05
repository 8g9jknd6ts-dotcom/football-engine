"""迁移老系统(world-cup-predictor)联赛复盘样本 → 线上联赛分层报告。

背景：世界杯积累的是另一预测域（国家队杯赛），参数不能搬；
但老系统 7/25 后预测的全是联赛（K1/瑞典超/挪超/芬超/巴甲），

⚠️ 已弃用（2026-08-05 死代码审计）：一次性迁移工具，使命已完成
（legacy_league_samples.json 已生成 29 条，见 data/state/），无 workflow 调用。
保留作历史参考。
与线上零重叠 → 同域样本合并，联赛分层判断更快收敛。

用法: python3 engine/review/import_legacy_league_samples.py [老系统路径]
输出: data/state/legacy_league_samples.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 老系统世界杯国家队名单（联赛场次 = 双方都不是国家队）
WC_TEAMS = {
    "波黑", "卡塔尔", "苏格兰", "巴西", "摩洛哥", "海地", "南非", "韩国",
    "捷克", "墨西哥", "厄瓜多尔", "德国", "库拉索", "科特迪瓦", "突尼斯",
    "荷兰", "日本", "瑞典", "巴拉圭", "英格兰", "阿根廷", "法国", "西班牙",
    "葡萄牙", "比利时", "克罗地亚", "瑞士", "乌拉圭", "哥伦比亚", "塞内加尔",
    "尼日利亚", "加纳", "喀麦隆", "澳大利亚", "美国", "加拿大",
}

# 老系统队名 → 线上联赛名（线上 competition 字段口径）
# 依据线上 review_ledger 已有联赛名，按球队归属映射
TEAM_LEAGUE = {
    # K1联赛
    "光州FC": "K1联赛", "济州SK": "K1联赛", "安养FC": "K1联赛", "江原FC": "K1联赛",
    "金泉尚武": "K1联赛", "大田市民": "K1联赛", "浦项制铁": "K1联赛", "全北现代": "K1联赛",
    # 瑞典超
    "赫根": "瑞典超", "索尔纳": "瑞典超", "布鲁马波": "瑞典超", "哈马比": "瑞典超",
    "天狼星": "瑞典超", "哥德堡": "瑞典超", "盖斯": "瑞典超", "哈尔姆斯": "瑞典超",
    "马尔默": "瑞典超", "埃夫斯堡": "瑞典超",
    # 挪超
    "罗森博格": "挪超", "腓特烈": "挪超", "布兰": "挪超", "瓦勒伦加": "挪超",
    "萨普斯堡": "挪超", "汉坎": "挪超", "奥斯KFUM": "挪超", "莫尔德": "挪超",
    "桑纳菲": "挪超", "博德闪耀": "挪超", "奥勒松": "挪超", "维京": "挪超",
    # 芬超
    "国际图尔": "芬超", "赫尔火花": "芬超", "坦山猫": "芬超", "拉赫蒂": "芬超",
    "赫尔辛基": "芬超", "TPS图尔": "芬超",
    # 巴甲
    "弗拉门戈": "巴甲", "圣保罗": "巴甲", "格雷米奥": "巴甲", "弗鲁米嫩": "巴甲",
    "巴西国际": "巴甲", "科林蒂安": "巴甲",
    # 美职联
    "洛城银河": "美职联", "达拉斯": "美职联", "波特兰": "美职联", "西雅图": "美职联",
    # 欧冠（资格赛）
    "里莫": "欧冠", "桑托斯": "欧冠", "奥胡斯": "欧冠", "萨巴赫": "欧冠",
    "费内巴切": "欧冠", "格风暴": "欧冠", "圣吉联合": "欧冠", "博德闪耀": "欧冠",
    "布斯巴达": "欧冠", "里昂": "欧冠",
}

DIRECTION_MAP = {"H": "home", "D": "draw", "A": "away"}


def score_to_outcome(score: str) -> str:
    try:
        h, a = map(int, score.split(":"))
    except Exception:
        return ""
    return "home" if h > a else ("draw" if h == a else "away")


def main(legacy_root: str) -> int:
    root = Path(legacy_root)
    preds_path = root / "data" / "predictions_store.json"
    results_path = root / "data" / "results_store.json"
    if not preds_path.exists() or not results_path.exists():
        print(f"✗ 老系统数据缺失: {preds_path} / {results_path}")
        return 1

    preds = json.loads(preds_path.read_text(encoding="utf-8"))["matches"]
    results = json.loads(results_path.read_text(encoding="utf-8"))["results"]
    res_map = {(r["home"], r["away"]): r for r in results}

    samples = []
    skipped = 0
    for m in preds:
        date = m.get("match_date") or ""
        home, away = m["home"], m["away"]
        # 只取联赛场次：有日期(7/25起) 且 双方不是国家队
        if not date or date < "2026-07-25":
            skipped += 1
            continue
        if home in WC_TEAMS or away in WC_TEAMS:
            skipped += 1
            continue
        r = res_map.get((home, away))
        if not r:
            skipped += 1
            continue
        actual = score_to_outcome(r.get("score", ""))
        outs = m.get("outcomes") or []
        if not outs or not actual:
            skipped += 1
            continue
        direction = DIRECTION_MAP.get(outs[0])
        if not direction:
            skipped += 1
            continue
        league = TEAM_LEAGUE.get(home) or TEAM_LEAGUE.get(away) or "未知"
        samples.append({
            "competition": league,
            "direction": direction,
            "actual": actual,
            "odds": 2.0,  # 老系统无赔率记录，用均赔近似（避免0）
            "confidence": 0.5,
            "home_team": home,
            "away_team": away,
            "score": r.get("score", ""),
            "date": date,
            "source": "legacy_worldcup_predictor",
        })

    out = Path("data/state/legacy_league_samples.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(samples, ensure_ascii=False, indent=2))
    print(f"✓ 迁移 {len(samples)} 场老系统联赛复盘 → {out}")
    print(f"  跳过 {skipped} 场（世界杯场次/无赛果/无方向）")
    return 0


if __name__ == "__main__":
    legacy = sys.argv[1] if len(sys.argv) > 1 else "/Users/jason/Downloads/world-cup-predictor"
    sys.exit(main(legacy))
