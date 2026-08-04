#!/usr/bin/env python3
"""EV 价值区评估（2026-08-04 新增，方法论源自 world-cup-predictor 的 ev_evaluator）

核心问题：为什么"胜率高还亏钱"？
答案：钱亏在押注结构，不在预测能力——热门赔率太低，赢1场只赚0.5，输1场亏1。

本模块对全部已结算预测按"赔率区间 × 联赛"分层计算 ROI，
识别价值区（ROI>0）与送钱区（ROI<-10%），产出报告供页面展示与 Kelly 参考。

产出：data/state/ev_report.json（build_site 读取渲染）
"""
from __future__ import annotations
import json
import glob
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent


def layer_of(odds: float) -> str:
    """老系统 L1-L5 赔率分层口径（与竞彩实战一致）"""
    if odds < 1.5:
        return "L1 大热(<1.5)"
    if odds < 1.8:
        return "L2 热(1.5-1.8)"
    if odds < 2.2:
        return "L3 中(1.8-2.2)"
    if odds < 3.0:
        return "L4 冷(2.2-3.0)"
    return "L5 深冷(≥3.0)"


def _load_settled_predictions(daily_root: Path) -> list:
    """扫描所有 daily 目录已结算的 predictions（含方向/赔率/赛果）"""
    rows = []
    for f in sorted(glob.glob(str(daily_root / "*" / "predictions.json"))):
        try:
            preds = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for p in preds:
            ah, aa = p.get("actual_home_score"), p.get("actual_away_score")
            if ah is None or aa is None:
                continue
            actual = "home" if ah > aa else ("draw" if ah == aa else "away")
            direction = p.get("direction")
            if not direction:
                probs = [p.get("home_win_prob", 0), p.get("draw_prob", 0), p.get("away_win_prob", 0)]
                direction = ["home", "draw", "away"][probs.index(max(probs))]
            odds = p.get(f"{direction}_odds") or 0
            if not odds:
                continue
            rows.append({
                "date": Path(f).parent.name,
                "league": p.get("competition", "?"),
                "direction": direction,
                "actual": actual,
                "odds": float(odds),
                "match_id": p.get("match_id", ""),
            })
    return rows


def build_report(daily_root: Path | None = None, out_path: Path | None = None) -> dict:
    daily_root = daily_root or ROOT / "data" / "daily"
    out_path = out_path or ROOT / "data" / "state" / "ev_report.json"
    rows = _load_settled_predictions(daily_root)

    layers = {k: {"n": 0, "hits": 0, "pnl": 0.0}
              for k in ["L1 大热(<1.5)", "L2 热(1.5-1.8)", "L3 中(1.8-2.2)",
                        "L4 冷(2.2-3.0)", "L5 深冷(≥3.0)"]}
    leagues = defaultdict(lambda: {"n": 0, "hits": 0, "pnl": 0.0})
    total = {"n": 0, "hits": 0, "pnl": 0.0}

    for r in rows:
        won = r["direction"] == r["actual"]
        pnl = (r["odds"] - 1) if won else -1.0
        layers[layer_of(r["odds"])]["n"] += 1
        layers[layer_of(r["odds"])]["hits"] += won
        layers[layer_of(r["odds"])]["pnl"] += pnl
        leagues[r["league"]]["n"] += 1
        leagues[r["league"]]["hits"] += won
        leagues[r["league"]]["pnl"] += pnl
        total["n"] += 1
        total["hits"] += won
        total["pnl"] += pnl

    def _finalize(d: dict) -> dict:
        d["hit_rate"] = d["hits"] / d["n"] if d["n"] else 0
        d["roi"] = d["pnl"] / d["n"] if d["n"] else 0
        d["verdict"] = ("价值区 ✅" if d["roi"] > 0
                        else "送钱区 ❌" if d["roi"] < -0.10 else "中性")
        return d

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": _finalize(dict(total)),
        "layers": {k: _finalize(v) for k, v in layers.items() if v["n"] > 0},
        "leagues": {k: _finalize(v) for k, v in sorted(
            leagues.items(), key=lambda x: x[1]["pnl"] / max(x[1]["n"], 1)) if v["n"] >= 3},
        "takeaways": [],
    }

    # 自动生成结论
    t = report["total"]
    report["takeaways"].append(
        f"整体 {t['n']} 场命中率 {t['hit_rate']*100:.1f}%，ROI {t['roi']*100:+.1f}%"
    )
    value_layers = [k for k, v in report["layers"].items() if v["roi"] > 0]
    bad_layers = [k for k, v in report["layers"].items() if v["roi"] < -0.10]
    if value_layers:
        report["takeaways"].append(f"价值区: {', '.join(value_layers)}")
    if bad_layers:
        report["takeaways"].append(f"送钱区(回避): {', '.join(bad_layers)}")
    bad_leagues = [k for k, v in report["leagues"].items() if v["roi"] < -0.15 and v["n"] >= 5]
    if bad_leagues:
        report["takeaways"].append(f"送钱联赛(回避): {', '.join(bad_leagues)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    r = build_report()
    print(json.dumps(r, ensure_ascii=False, indent=2))
