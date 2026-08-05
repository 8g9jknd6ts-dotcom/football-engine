"""串关（2串1）回测评估 — 回答"竞彩串关能不能玩"

原理：
- 竞彩单关抽水约 5-8%（胜平负），串关连乘赔率但同样吃水
- 2串1 = 两场都命中才赢，赔率 = 两场主推赔率乘积
- 对比策略：
  A. 单关：每场押 1 单位（按模型主推方向）
  B. 2串1：每天从预测中取 top2（按置信度），两场都中赢 赔率积-1，否则输 1
- 结论：2串1 ROI vs 单关 ROI，决定是否值得玩串关

样本：历史 predictions（有赛果 + 赔率）按天分组。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def build_parlay_report(
    daily_root: Path | None = None,
    out_path: Path | None = None,
) -> dict:
    daily_root = daily_root or Path("data/daily")
    out_path = out_path or Path("data/state/parlay_report.json")

    # 按天收集已结算预测
    by_day: dict[str, list] = defaultdict(list)
    for pf in sorted(daily_root.glob("*/predictions.json")):
        day = pf.parent.name
        try:
            preds = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in preds:
            if p.get("actual_home_score") is None:
                continue
            hs, as_ = p["actual_home_score"], p["actual_away_score"]
            actual = "home" if hs > as_ else ("draw" if hs == as_ else "away")
            direction = p.get("direction")
            if not direction:
                probs = (p.get("home_win_prob", 0), p.get("draw_prob", 0), p.get("away_win_prob", 0))
                direction = ["home", "draw", "away"][probs.index(max(probs))]
            odds = p.get(f"{direction}_odds") or 0
            if odds <= 1.0:
                continue
            by_day[day].append({
                "direction": direction,
                "actual": actual,
                "odds": odds,
                "conf": p.get("confidence", 0),
                "hit": direction == actual,
                "league": p.get("competition", ""),
            })

    # 单关 ROI
    n = sum(len(v) for v in by_day.values())
    single_roi_sum = sum((m["odds"] - 1) if m["hit"] else -1.0 for v in by_day.values() for m in v)

    # 2串1：每天 top2（置信度最高），串成 1 注
    parlay_bets = []
    for day, ms in by_day.items():
        if len(ms) < 2:
            continue
        ms_sorted = sorted(ms, key=lambda m: -m["conf"])[:2]
        hit = all(m["hit"] for m in ms_sorted)
        odds = ms_sorted[0]["odds"] * ms_sorted[1]["odds"]
        parlay_bets.append({
            "day": day,
            "matches": [f"{m['league']}(@{m['odds']:.2f})" for m in ms_sorted],
            "odds": odds,
            "hit": hit,
            "pnl": (odds - 1) if hit else -1.0,
        })
    n_parlay = len(parlay_bets)
    parlay_roi_sum = sum(b["pnl"] for b in parlay_bets)

    # 变体：只串价值区联赛
    value_leagues = {"K1联赛", "挪超", "瑞超"}
    parlay_value = [b for b in parlay_bets if all("K1" in x or "挪" in x or "瑞" in x for x in b["matches"])]

    report = {
        "n_matches": n,
        "n_days": len(by_day),
        "single": {
            "n": n,
            "roi": round(single_roi_sum / n, 4) if n else 0,
        },
        "parlay_2in1": {
            "n": n_parlay,
            "hit_rate": round(sum(1 for b in parlay_bets if b["hit"]) / n_parlay, 4) if n_parlay else 0,
            "roi": round(parlay_roi_sum / n_parlay, 4) if n_parlay else 0,
            "avg_odds": round(sum(b["odds"] for b in parlay_bets) / n_parlay, 2) if n_parlay else 0,
        },
        "parlay_value_leagues": {
            "n": len(parlay_value),
            "roi": round(sum(b["pnl"] for b in parlay_value) / len(parlay_value), 4) if parlay_value else 0,
        },
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "verdict": "",
    }
    # 结论
    if report["parlay_2in1"]["roi"] > report["single"]["roi"] and report["parlay_2in1"]["roi"] > 0:
        report["verdict"] = "串关有价值：2串1 ROI 高于单关且为正，可小注试水"
    elif report["parlay_2in1"]["roi"] > 0:
        report["verdict"] = "串关 ROI 为正但低于单关：单关更稳，串关仅小注娱乐"
    else:
        report["verdict"] = "串关 ROI 为负：不建议玩串关（两场都中太难），专注单关价值区"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    r = build_parlay_report()
    s, p2 = r["single"], r["parlay_2in1"]
    print(f"样本: {r['n_matches']} 场 / {r['n_days']} 天")
    print(f"单关: {s['n']} 注 ROI {s['roi']*100:+.1f}%")
    print(f"2串1: {p2['n']} 注 命中率{p2['hit_rate']*100:.0f}% 均赔{p2['avg_odds']:.2f} ROI {p2['roi']*100:+.1f}%")
    print(f"价值区串: {r['parlay_value_leagues']['n']} 注 ROI {r['parlay_value_leagues']['roi']*100:+.1f}%")
    print(f"结论: {r['verdict']}")
