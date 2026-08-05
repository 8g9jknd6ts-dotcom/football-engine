#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高置信反向样本库（2026-08-06 新增，借鉴 MBS 8/2 自检 AIK 案例）

背景：MBS 8/2 自检中最严重失误是 AIK 索尔纳 0-3 奥尔格里特——
C3 高概率（64.7%）+ 市场同向（64.4%）却 0-3 反向。MBS 的处理：
独立归档复核，不归因于模型-市场分歧（分歧样本与高置信反向是两类问题）。

本模块：
1. 扫描 review_ledger.jsonl，筛选"高置信 + 市场同向 + 结果反向"场次
2. 幂等归档到 data/state/high_conf_reversals.jsonl（按 match_id 去重）
3. 输出统计摘要（触发率、按联赛/置信度分层），供复盘与页面展示

判定条件（全部满足才算高置信反向样本）：
- 高置信：final_prob 最高项 ≥ 0.60（MBS 用 60%+ 段做高置信基准）
- 市场同向：market_fair 方向 == 模型方向（市场没有给出反向信号）
- 结果反向：actual_idx != 模型方向（best_selection）
"""
from __future__ import annotations

import json
from pathlib import Path

HIGH_CONF_THRESHOLD = 0.60

DIR_NAMES = {0: "主胜", 1: "平局", 2: "客胜"}


def _load_team_names(daily_dir: Path) -> dict[str, str]:
    """match_id -> '主队 vs 客队'（从每日 predictions.json 反查）"""
    out: dict[str, str] = {}
    if not daily_dir.exists():
        return out
    for p in sorted(daily_dir.glob("*/predictions.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("predictions", data.get("matches", []))
            if not isinstance(items, list):
                continue
            for it in items:
                mid = it.get("match_id")
                if mid and it.get("home_team") and it.get("away_team"):
                    out[mid] = f'{it["home_team"]} vs {it["away_team"]}'
        except Exception:
            continue
    return out


def _scan(ledger_path: Path, daily_dir: Path | None = None) -> list[dict]:
    """从账本扫描高置信反向场次（不写盘，纯扫描）"""
    if not ledger_path.exists():
        return []
    names = _load_team_names(daily_dir) if daily_dir else {}
    out = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            fp = r.get("final_prob")
            mf = r.get("market_fair")
            if not fp or not mf:
                continue
            best = r.get("best_selection")
            if best is None:
                continue
            conf = max(fp)
            if conf < HIGH_CONF_THRESHOLD:
                continue
            # 市场方向（market_fair 的 argmax）
            mkt_dir = max(range(3), key=lambda i: mf[i])
            if mkt_dir != best:
                continue  # 市场不同向 → 不是本类样本（是分歧，另一类）
            if r.get("actual_idx") == best:
                continue  # 结果同向 → 正常命中，不归档
            mid = r.get("match_id", "")
            out.append({
                "match_id": mid,
                "date": r.get("date"),
                "league": r.get("league"),
                "teams": r.get("teams") or names.get(mid, ""),
                "final_prob": [round(x, 3) for x in fp],
                "conf": round(conf, 3),
                "direction": DIR_NAMES.get(best, str(best)),
                "market_fair": [round(x, 3) for x in mf],
                "actual_idx": r.get("actual_idx"),
                "actual_dir": DIR_NAMES.get(r.get("actual_idx", -1), "?"),
                "score": r.get("score_text", ""),
                "hit": r.get("hit"),
            })
    return out


def archive(ledger_path: Path, archive_path: Path, daily_dir: Path | None = None) -> dict:
    """扫描 + 幂等归档，返回统计摘要"""
    samples = _scan(ledger_path, daily_dir)
    seen: set[str] = set()
    new_items: list[dict] = []
    if archive_path.exists():
        try:
            for line in archive_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line)["match_id"])
                except Exception:
                    continue
        except Exception:
            pass
    for s in samples:
        if s["match_id"] in seen:
            continue
        seen.add(s["match_id"])
        new_items.append(s)
    if new_items:
        with open(archive_path, "a", encoding="utf-8") as f:
            for s in new_items:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # 统计（含历史归档）
    all_items = []
    if archive_path.exists():
        try:
            for line in archive_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    all_items.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass
    return {
        "scanned": len(samples),
        "new_archived": len(new_items),
        "total_archived": len(all_items),
        "by_league": _by_league(all_items),
    }


def _by_league(items: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for s in items:
        lg = s.get("league", "?")
        e = out.setdefault(lg, {"n": 0, "matches": []})
        e["n"] += 1
        if len(e["matches"]) < 5:
            e["matches"].append(f'{s.get("date")} {s.get("teams")} '
                                f'({s.get("direction")} {s.get("conf"):.0%} '
                                f'→ 实际{s.get("actual_dir")})')
    return out


def league_risk(archive_path: Path, min_samples: int = 2) -> dict[str, int]:
    """从归档统计"高置信反向样本≥min_samples"的联赛 → 该联赛 60%+ 段降档依据

    E 规则（2026-08-06）：同联赛出现 ≥min_samples 场高置信+市场同向+反向样本，
    说明该联赛存在系统性高估风险，其 60%+ 段（整体命中最好但也最自信）
    降一档处理。当前 min_samples=2（巴甲 2 场 71%/74% 主胜→平局即触发）。
    """
    counts: dict[str, int] = {}
    if not archive_path.exists():
        return counts
    try:
        for line in archive_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lg = json.loads(line).get("league")
            except Exception:
                continue
            if lg:
                counts[lg] = counts.get(lg, 0) + 1
    except Exception:
        pass
    return {lg: n for lg, n in counts.items() if n >= min_samples}


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    stat = archive(
        root / "data" / "state" / "review_ledger.jsonl",
        root / "data" / "state" / "high_conf_reversals.jsonl",
        root / "data" / "daily",
    )
    print(json.dumps(stat, ensure_ascii=False, indent=2))
