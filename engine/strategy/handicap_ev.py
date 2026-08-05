"""让球胜平负 EV 评估 — 竞彩让球玩法的价值挖掘

原理：
- 模型已算出比分联合分布（top_scores: [主队进球, 客队进球, 概率]）
- 竞彩让球盘（hhad）：主队让出/受让 handicap 球后，胜平负重新划分
- 让球后概率 = 比分分布按 (主-客+handicap) 符号加总
- 对比官方让球赔率（handicap_home_odds 等）→ 找正 EV 场次

竞彩让球赔率通常接近 2.0（均衡盘），比胜平负大热盘（1.2-1.4）
更容易覆盖水钱，是模型优势最值得变现的玩法。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HandicapEV:
    """一场比赛的让球 EV 评估"""
    match_id: str
    home_team: str
    away_team: str
    handicap: float                # 让球线（主队视角，正=主让）
    probs: dict                    # 让球后胜平负概率 {home, draw, away}
    odds: dict                     # 官方让球赔率 {home, draw, away}
    edges: dict = field(default_factory=dict)   # 各方向 edge
    best_sel: str = ""
    best_edge: float = 0.0
    ev: float = 0.0
    recommended: bool = False


def handicap_probs_from_scores(
    top_scores: list, handicap: float
) -> dict | None:
    """从比分分布推导让球后胜平负概率。

    top_scores: [[主队进球, 客队进球, 概率], ...]
    handicap: 让球线（竞彩口径：主队让 handicap 球，胜 = 主队进球 - 客队进球 + handicap > 0）
    """
    if not top_scores or handicap is None:
        return None
    ph = pd_ = pa = 0.0
    for row in top_scores:
        try:
            hs, as_, p = row[0], row[1], row[2]
        except (IndexError, TypeError, ValueError):
            continue
        diff = hs - as_ + handicap
        if diff > 0:
            ph += p
        elif diff == 0:
            pd_ += p
        else:
            pa += p
    total = ph + pd_ + pa
    if total <= 0:
        return None
    return {"home": ph / total, "draw": pd_ / total, "away": pa / total}


def evaluate_handicap_ev(
    pred: dict, min_edge: float = 0.03
) -> HandicapEV | None:
    """评估一场预测的让球 EV。

    让球后概率优先用模型完整计算的 handicap_*_prob（DC/MC 基于官方
    让球线在完整比分矩阵上计算），缺失时 fallback 到 top_scores 推导。
    """
    handicap = pred.get("handicap")
    odds = {
        "home": pred.get("handicap_home_odds"),
        "draw": pred.get("handicap_draw_odds"),
        "away": pred.get("handicap_away_odds"),
    }
    if handicap is None or not any(o is not None and o > 1.0 for o in odds.values()):
        return None

    # 优先模型完整概率（和应≈1）；缺失/异常时用 top_scores 推导
    mprobs = {
        "home": pred.get("handicap_home_prob"),
        "draw": pred.get("handicap_draw_prob"),
        "away": pred.get("handicap_away_prob"),
    }
    if mprobs and all(p is not None for p in mprobs.values()) and 0.9 < sum(mprobs.values()) <= 1.05:
        probs = mprobs
    else:
        probs = handicap_probs_from_scores(pred.get("top_scores"), handicap)
    if not probs:
        return None

    ev = HandicapEV(
        match_id=pred.get("match_id", ""),
        home_team=pred.get("home_team", ""),
        away_team=pred.get("away_team", ""),
        handicap=handicap,
        probs=probs,
        odds=odds,
    )
    best_sel, best_edge = "", -1.0
    for sel in ("home", "draw", "away"):
        o = odds[sel]
        if o is None or o <= 1.0:
            continue  # 只评估有赔率的方向
        edge = probs[sel] * o - 1.0
        ev.edges[sel] = edge
        if edge > best_edge:
            best_edge, best_sel = edge, sel
    ev.best_sel, ev.best_edge, ev.ev = best_sel, best_edge, best_edge
    # sanity check: 模型概率与赔率隐含概率严重背离(>30% edge) 多为脏数据,
    # 不直接推荐重注，标记 recommended=False（回测积累后再放开）
    ev.recommended = best_edge >= min_edge and best_edge <= 0.30
    return ev


def scan_handicap_ev(
    predictions: list[dict], min_edge: float = 0.03
) -> list[HandicapEV]:
    """扫描全部预测，返回有让球数据的场次 EV 评估。"""
    out = []
    for p in predictions:
        ev = evaluate_handicap_ev(p, min_edge=min_edge)
        if ev:
            out.append(ev)
    return out


def build_handicap_report(
    daily_root: Path | None = None, out_path: Path | None = None
) -> dict:
    """扫描所有 daily 目录已结算场次，回测让球玩法 ROI。"""
    daily_root = daily_root or Path(__file__).parent.parent.parent / "data" / "daily"
    out_path = out_path or daily_root.parent / "state" / "handicap_report.json"

    rows = []
    for f in sorted(daily_root.glob("*/predictions.json")):
        try:
            preds = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in preds:
            ah, aa = p.get("actual_home_score"), p.get("actual_away_score")
            if ah is None or aa is None:
                continue
            ev = evaluate_handicap_ev(p, min_edge=-1.0)
            if not ev:
                continue
            actual = "home" if ah - aa + ev.handicap > 0 else (
                "draw" if ah - aa + ev.handicap == 0 else "away")
            hit = ev.best_sel == actual
            pnl = (ev.odds[ev.best_sel] - 1) if hit else -1.0
            rows.append({
                "date": f.parent.name,
                "match_id": p.get("match_id", ""),
                "league": p.get("competition", ""),
                "handicap": ev.handicap,
                "best_sel": ev.best_sel,
                "best_edge": round(ev.best_edge, 4),
                "actual": actual,
                "hit": hit,
                "odds": ev.odds[ev.best_sel],
                "pnl": round(pnl, 4),
            })

    report = {
        "n_matches": len(rows),
        "hits": sum(1 for r in rows if r["hit"]),
        "hit_rate": round(sum(1 for r in rows if r["hit"]) / len(rows), 4) if rows else 0,
        "roi": round(sum(r["pnl"] for r in rows) / len(rows), 4) if rows else 0,
        "rows": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    import sys
    from engine.main import load_config
    r = build_handicap_report()
    print(f"让球玩法回测: {r['n_matches']} 场, 命中率 {r['hit_rate']*100:.1f}%, ROI {r['roi']*100:+.1f}%")
