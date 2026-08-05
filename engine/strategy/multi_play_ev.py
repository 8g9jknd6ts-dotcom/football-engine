"""竞彩多玩法 EV 评估 — 总进球(ttg) / 波胆(crs) / 半全场(hafu)

原理：
- 模型已有三类分布：
  - total_goals: 总进球数分布 [[n, p], ...]（MC 蒙特卡洛真分布）
  - top_scores: 比分分布 [[主, 客, p], ...]（截断 top_n，需归一化）
  - htft: 半全场分布 {'HH': p, 'HD': p, ...}（完整，和=1）
- 竞彩官方赔率（sporttery 主源采集）：
  - ttg: {s0..s7} 总进球赔率（0球~7+球）
  - crs: {键: 赔率} 波胆（比分）
  - hafu: {hh,hd,ha,dh,dd,da,ah,ad,aa} 半全场
- 模型概率 vs 官方赔率 → edge → 正 EV 小注（冷门玩法，符合老系统
  "深冷赔率区 ROI 最高"实证：L5 深冷 +63.2%）

风控：
- 波胆/半全场赔率高（7~100+），单注极小（Kelly 分数 0.03）
- edge > 30% 视为脏数据（模型概率与赔率隐含概率严重背离）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PlayEV:
    """一场比赛的一个玩法 EV 评估"""
    match_id: str
    home_team: str
    away_team: str
    play: str                    # ttg / crs / hafu
    label: str                   # 玩法名
    probs: dict                  # 模型概率 {选项: p}
    odds: dict                   # 官方赔率 {选项: o}
    edges: dict = field(default_factory=dict)
    best_sel: str = ""
    best_edge: float = 0.0
    recommended: bool = False
    note: str = ""


# ---------- 官方赔率键解析 ----------

def parse_ttg_odds(raw: dict) -> dict:
    """官方总进球赔率 {s0..s7} → {0..7}（s7=7+球）。"""
    out = {}
    if not raw:
        return out
    for k, v in raw.items():
        m = re.search(r"\d+", str(k))
        if m:
            try:
                out[int(m.group())] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def parse_crs_odds(raw: dict) -> dict:
    """官方波胆赔率 → {(主, 客): odds}。

    键格式多变（s00s00 / 1:0 / 0100），兼容解析：
    - 含 ':' 按冒号分割
    - 形如 s01s00 取两组数字
    - 纯数字串 len>=4 前两位主后两位客
    """
    out = {}
    if not raw:
        return out
    for k, v in raw.items():
        ks = str(k).lower()
        score = None
        if ":" in ks:
            parts = ks.split(":")
            if len(parts) == 2:
                try:
                    score = (int(parts[0]), int(parts[1]))
                except ValueError:
                    score = None
        else:
            groups = re.findall(r"\d+", ks)
            if len(groups) >= 2 and len(groups[0]) <= 2 and len(groups[1]) <= 2:
                try:
                    score = (int(groups[0]), int(groups[1]))
                except ValueError:
                    score = None
            elif len(groups) == 1 and len(groups[0]) == 4:
                s = groups[0]
                score = (int(s[:2]), int(s[2:]))
        if score is not None:
            try:
                out[score] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def parse_hafu_odds(raw: dict) -> dict:
    """官方半全场赔率 {hh,hd,ha,...} → {'HH','HD',...}（转大写）。"""
    out = {}
    if not raw:
        return out
    for k, v in raw.items():
        ks = str(k).strip().upper()
        if len(ks) == 2 and ks[0] in "HDA" and ks[1] in "HDA":
            try:
                out[ks] = float(v)
            except (TypeError, ValueError):
                continue
    return out


# ---------- 模型概率归一化 ----------

def norm_scores(top_scores: list) -> dict:
    """比分分布 [(h, a, p)] → {(h, a): p}，截断概率按比例归一化。"""
    out = {}
    total = 0.0
    for row in top_scores or []:
        try:
            hs, as_, p = int(row[0]), int(row[1]), float(row[2])
        except (IndexError, TypeError, ValueError):
            continue
        if p > 0:
            out[(hs, as_)] = p
            total += p
    if total <= 0:
        return {}
    return {k: v / total for k, v in out.items()}


def norm_total_goals(tg: list) -> dict:
    """总进球分布 [(n, p)] → {n: p}，归一化。"""
    out = {}
    total = 0.0
    for row in tg or []:
        try:
            n, p = int(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if p > 0:
            out[n] = out.get(n, 0) + p
            total += p
    if total <= 0:
        return {}
    return {k: v / total for k, v in out.items()}


def merge_over7(probs: dict) -> dict:
    """7+ 球并入 7（竞彩 ttg 只有 0-6, 7+）。"""
    out = {k: v for k, v in probs.items() if k <= 6}
    out[7] = out.get(7, 0) + sum(v for k, v in probs.items() if k >= 7)
    return out


# ---------- 三玩法评估 ----------

def _finish(ev: PlayEV, min_edge: float, cap: float = 0.30) -> PlayEV:
    """共同收尾：算 edge、找最优、sanity 检查。"""
    best_sel, best_edge = "", -1.0
    for sel, o in ev.odds.items():
        p = ev.probs.get(sel, 0.0)
        if o is None or o <= 1.0 or p <= 0:
            continue
        edge = p * o - 1.0
        ev.edges[sel] = edge
        if edge > best_edge:
            best_edge, best_sel = edge, sel
    ev.best_sel, ev.best_edge = best_sel, best_edge
    ev.recommended = best_edge >= min_edge and best_edge <= cap
    return ev


def evaluate_ttg(pred: dict, min_edge: float = 0.03) -> PlayEV | None:
    """总进球玩法：模型 total_goals vs 官方 ttg 赔率。"""
    raw_odds = parse_ttg_odds(pred.get("ttg_odds"))
    if not raw_odds:
        return None
    probs = merge_over7(norm_total_goals(pred.get("total_goals")))
    if not probs:
        return None
    odds = {n: o for n, o in raw_odds.items() if o > 1.0}
    if not odds:
        return None
    return _finish(PlayEV(
        match_id=pred.get("match_id", ""),
        home_team=pred.get("home_team", ""),
        away_team=pred.get("away_team", ""),
        play="ttg", label="总进球",
        probs=probs, odds=odds,
    ), min_edge)


def evaluate_crs(pred: dict, min_edge: float = 0.03) -> PlayEV | None:
    """波胆玩法：模型比分分布 vs 官方 crs 赔率。"""
    raw_odds = parse_crs_odds(pred.get("crs_odds"))
    if not raw_odds:
        return None
    probs = norm_scores(pred.get("top_scores"))
    if not probs:
        return None
    odds = {}
    for score, o in raw_odds.items():
        if o > 1.0:
            odds[score] = o
    if not odds:
        return None
    return _finish(PlayEV(
        match_id=pred.get("match_id", ""),
        home_team=pred.get("home_team", ""),
        away_team=pred.get("away_team", ""),
        play="crs", label="波胆",
        probs=probs, odds=odds,
    ), min_edge)


def evaluate_hafu(pred: dict, min_edge: float = 0.03) -> PlayEV | None:
    """半全场玩法：模型 htft vs 官方 hafu 赔率。"""
    raw_odds = parse_hafu_odds(pred.get("hafu_odds"))
    if not raw_odds:
        return None
    htft = pred.get("htft") or {}
    probs = {}
    total = 0.0
    for k, p in htft.items():
        ks = str(k).strip().upper()
        if len(ks) == 2 and ks[0] in "HDA" and ks[1] in "HDA":
            probs[ks] = float(p)
            total += float(p)
    if total <= 0:
        return None
    probs = {k: v / total for k, v in probs.items()}
    odds = {k: o for k, o in raw_odds.items() if o > 1.0}
    if not odds:
        return None
    return _finish(PlayEV(
        match_id=pred.get("match_id", ""),
        home_team=pred.get("home_team", ""),
        away_team=pred.get("away_team", ""),
        play="hafu", label="半全场",
        probs=probs, odds=odds,
    ), min_edge)


def evaluate_all_plays(pred: dict, min_edge: float = 0.03) -> list[PlayEV]:
    """一场比赛全部可评估玩法。"""
    out = []
    for fn in (evaluate_ttg, evaluate_crs, evaluate_hafu):
        try:
            ev = fn(pred, min_edge=min_edge)
        except Exception:
            ev = None
        if ev:
            out.append(ev)
    return out


# ---------- 回测报告 ----------

def _actual_for_play(play: str, ah: int, aa: int) -> str | tuple:
    if play == "ttg":
        return min(7, ah + aa)
    if play == "crs":
        return (min(ah, 9), min(aa, 9)) if False else (ah, aa)
    if play == "hafu":
        hh, ha = 0, 0  # 半场比分在 predictions 里没有，跳过 hafu 回测
        return None
    return None


def build_plays_report(
    daily_root: Path | None = None, out_path: Path | None = None
) -> dict:
    """扫描已结算场次，回测 ttg/crs/hafu 玩法 ROI。"""
    daily_root = daily_root or Path(__file__).parent.parent.parent / "data" / "daily"
    out_path = out_path or daily_root.parent / "state" / "plays_report.json"

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
            for ev in evaluate_all_plays(p, min_edge=-1.0):
                actual = _actual_for_play(ev.play, int(ah), int(aa))
                if actual is None:
                    continue
                hit = ev.best_sel == actual
                pnl = (ev.odds[ev.best_sel] - 1) if hit else -1.0
                rows.append({
                    "date": f.parent.name,
                    "match_id": p.get("match_id", ""),
                    "play": ev.play,
                    "label": ev.label,
                    "best_sel": str(ev.best_sel),
                    "best_edge": round(ev.best_edge, 4),
                    "odds": ev.odds.get(ev.best_sel),
                    "hit": hit,
                    "pnl": round(pnl, 4),
                })

    report = {
        "n_matches": len(rows),
        "hits": sum(1 for r in rows if r["hit"]),
        "hit_rate": round(sum(1 for r in rows if r["hit"]) / len(rows), 4) if rows else 0,
        "roi": round(sum(r["pnl"] for r in rows) / len(rows), 4) if rows else 0,
        "by_play": {},
        "rows": rows,
    }
    for play in ("ttg", "crs", "hafu"):
        sub = [r for r in rows if r["play"] == play]
        if sub:
            report["by_play"][play] = {
                "n": len(sub),
                "hit_rate": round(sum(1 for r in sub if r["hit"]) / len(sub), 4),
                "roi": round(sum(r["pnl"] for r in sub) / len(sub), 4),
            }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    r = build_plays_report()
    print(f"多玩法回测: {r['n_matches']} 场, 命中率 {r['hit_rate']*100:.1f}%, ROI {r['roi']*100:+.1f}%")
    for play, st in r["by_play"].items():
        print(f"  {play}: {st['n']}场 命中率{st['hit_rate']*100:.0f}% ROI{st['roi']*100:+.1f}%")
