"""串关/波胆真实复盘结算（2026-08-10 新增）

用户：'过关和波胆，也应该复盘吧？'
此前只回测单关（review_ledger），串关（parlay）和比分串（score_parlay）
出票后从不结算 → 命中率/ROI 全黑。本模块把 ticket_plan.json 里真实出过
的每张串票，用当日 results.json 赛果逐腿结算：

- 胜平负腿（sel=home/draw/away）：按主客比分判方向
- 波胆腿（score='1-0'）：精确比分匹配
- 整票：全部腿中 → 命中（回报=potential）；任一腿错 → 输（回报=0）；
  有腿缺赛果 → pending（不进统计）

统计口径：
- 命中率 = 中票数 / 已结算票数
- ROI = (Σ回报 - Σ投入) / Σ投入
- 波胆额外给"单腿命中率"（精确比分命中极难，腿级信息量更大）

输出 data/state/parlay_settle.json，build_site 渲染复盘卡片。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _load_results(day_dir: Path) -> dict:
    """读当日 results.json → {match_id: {home_score, away_score}}"""
    rp = day_dir / "results.json"
    if not rp.exists():
        return {}
    try:
        rows = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(rows, dict):
        rows = rows.get("results", rows.get("matches", []))
    out = {}
    for r in rows if isinstance(rows, list) else []:
        mid = r.get("match_id")
        if mid and r.get("home_score") is not None:
            out[mid] = r
    return out


def settle_leg(leg: dict, res_map: dict) -> bool | None:
    """单腿结算：True=中 False=错 None=未开赛/缺赛果"""
    r = res_map.get(leg.get("match") or leg.get("match_id") or "")
    if not r:
        return None
    h, a = int(r["home_score"]), int(r["away_score"])
    if "sel" in leg and leg["sel"] in ("home", "draw", "away"):
        actual = "home" if h > a else ("draw" if h == a else "away")
        return leg["sel"] == actual
    if "score" in leg:
        return f"{h}-{a}" == leg["score"]
    return None


def settle_ticket(t: dict, res_map: dict) -> dict:
    """整票结算 → {won, pending, leg_results, return, note}"""
    legs = t.get("legs") or []
    res = [settle_leg(l, res_map) for l in legs]
    pending = any(x is None for x in res)
    won = (not pending) and all(x is True for x in res)
    return {
        "won": won,
        "pending": pending,
        "leg_results": res,
        "return": (t.get("potential", 0) or 0) if won else 0.0,
    }


def build_settle_report(
    daily_root: Path | None = None,
    out_path: Path | None = None,
) -> dict:
    daily_root = daily_root or Path("data/daily")
    out_path = out_path or Path("data/state/parlay_settle.json")

    by_kind = {"parlay": [], "score_parlay": []}
    by_date: dict[str, dict] = defaultdict(lambda: {"parlay": [], "score_parlay": []})
    for tp_path in sorted(daily_root.glob("*/ticket_plan.json")):
        day = tp_path.parent.name
        try:
            tp = json.loads(tp_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        res_map = _load_results(tp_path.parent)
        for kind in ("parlay", "score_parlay"):
            for t in tp.get(kind) or []:
                st = settle_ticket(t, res_map)
                rec = {
                    "date": day,
                    "type": t.get("type", kind),
                    "stake": t.get("stake", 0),
                    "potential": t.get("potential", 0),
                    "total_odds": t.get("total_odds", 0),
                    "odds_source": t.get("odds_source"),
                    "note": t.get("note", ""),
                    **st,
                    "legs": [
                        {
                            "match": l.get("match") or l.get("match_id"),
                            "home": l.get("home", ""),
                            "away": l.get("away", ""),
                            "sel": l.get("sel"),
                            "score": l.get("score"),
                            "odds": l.get("odds"),
                            "hit": rr,
                        }
                        for l, rr in zip(t.get("legs") or [], st["leg_results"])
                    ],
                }
                by_kind[kind].append(rec)
                by_date[day][kind].append(rec)

    def _stats(rows: list[dict]) -> dict:
        settled = [r for r in rows if not r["pending"]]
        won = [r for r in settled if r["won"]]
        stake = sum(r["stake"] for r in settled)
        ret = sum(r["return"] for r in settled)
        by_type = defaultdict(list)
        for r in settled:
            by_type[r["type"]].append(r)
        return {
            "n_tickets": len(rows),
            "n_settled": len(settled),
            "n_won": len(won),
            "hit_rate": round(len(won) / len(settled), 4) if settled else None,
            "stake": round(stake, 2),
            "return": round(ret, 2),
            "roi": round((ret - stake) / stake, 4) if stake else None,
            "avg_odds": round(sum(r["total_odds"] for r in settled) / len(settled), 2) if settled else None,
            "by_type": {
                k: {
                    "n": len(v),
                    "won": sum(1 for r in v if r["won"]),
                    "roi": round((sum(r["return"] for r in v) - sum(r["stake"] for r in v)) / sum(r["stake"] for r in v), 4) if sum(r["stake"] for r in v) else None,
                }
                for k, v in sorted(by_type.items())
            },
        }

    # 波胆额外统计单腿命中率
    sp_rows = by_kind["score_parlay"]
    leg_hits = [r for t in sp_rows for r in t["leg_results"] if r is not None]
    leg_wins = sum(1 for x in leg_hits if x)

    def _day_group(day: str) -> dict:
        """按出票日分组的复盘（当天页面只显示当天出的串票）。"""
        g = by_date.get(day)
        if not g:
            return {}
        out = {}
        for kind in ("parlay", "score_parlay"):
            rows = g[kind]
            if not rows:
                out[kind] = {"stats": None, "tickets": []}
                continue
            s = _stats(rows)
            if kind == "score_parlay":
                lh = [r for t in rows for r in t["leg_results"] if r is not None]
                s["leg_hit_rate"] = round(sum(1 for x in lh if x) / len(lh), 4) if lh else None
                s["n_legs_settled"] = len(lh)
            out[kind] = {"stats": s, "tickets": rows}
        return out

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parlay": _stats(by_kind["parlay"]),
        "score_parlay": {
            **_stats(sp_rows),
            "leg_hit_rate": round(leg_wins / len(leg_hits), 4) if leg_hits else None,
            "n_legs_settled": len(leg_hits),
        },
        "tickets": {
            "parlay": by_kind["parlay"],
            "score_parlay": sp_rows,
        },
        # 2026-08-10：按出票日分组，页面按当天渲染（复盘归属出票当天，不堆在最新页）
        "by_date": {d: _day_group(d) for d in sorted(by_date)},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/daily")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/state/parlay_settle.json")
    rep = build_settle_report(root, out)
    for kind in ("parlay", "score_parlay"):
        s = rep[kind]
        print(f"{kind}: 出票{s['n_tickets']} 结算{s['n_settled']} 中{s['n_won']} "
              f"命中率{s['hit_rate'] or '-'} 投入{s['stake']} 回报{s['return']} ROI{s['roi'] or '-'}")
        if kind == "score_parlay" and s.get("leg_hit_rate") is not None:
            print(f"  波胆单腿命中率: {s['leg_hit_rate']:.1%} ({s['n_legs_settled']} 腿)")
