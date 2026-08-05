"""准确率趋势报告 - 回答"系统是否每天在提升"

输入: data/state/review_ledger.jsonl（每场复盘明细）
输出: data/state/accuracy_trend.json
  - by_date: 每日 n/hits/hit_rate/brier(各源)/pnl/累计命中率
  - rolling: 滚动7天窗口的命中率/Brier
  - by_league / by_odds_band / by_conf_tier: 分层归因
  - verdict: 最近7天 vs 前7天 → 提升/持平/下降（诚实回答"是否在提升"）
"""
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _brier(final_prob, actual_idx) -> float | None:
    if not final_prob or len(final_prob) < 3 or actual_idx is None:
        return None
    return sum((p - (1.0 if i == actual_idx else 0.0)) ** 2
               for i, p in enumerate(final_prob))


def build_accuracy_trend(ledger_path: Path, out_path: Path) -> dict:
    if not ledger_path.exists():
        return {}
    lines = ledger_path.read_text(encoding="utf-8").strip().split("\n")
    rows = []
    for l in lines:
        try:
            rows.append(json.loads(l))
        except Exception:
            continue
    if not rows:
        return {}

    # 按日期聚合
    by_date: dict[str, dict] = {}
    for r in rows:
        d = r.get("date", "?")
        a = by_date.setdefault(d, {"n": 0, "hits": 0, "brier_final": [], "brier_model": [], "brier_market": [], "brier_djyy": [], "pnl": 0.0})
        a["n"] += 1
        a["hits"] += 1 if r.get("hit") else 0
        for k in ("brier_final", "brier_model", "brier_market", "brier_djyy"):
            v = r.get(k)
            if v is not None:
                a[k].append(v)
        a["pnl"] += r.get("pnl") or 0

    dates = sorted(by_date)
    daily = []
    cum_hits = cum_n = 0
    for d in dates:
        a = by_date[d]
        cum_hits += a["hits"]
        cum_n += a["n"]
        daily.append({
            "date": d,
            "n": a["n"],
            "hits": a["hits"],
            "hit_rate": round(a["hits"] / max(1, a["n"]), 4),
            "cum_hit_rate": round(cum_hits / max(1, cum_n), 4),
            "brier_final": round(sum(a["brier_final"]) / max(1, len(a["brier_final"])), 4) if a["brier_final"] else None,
            "brier_model": round(sum(a["brier_model"]) / max(1, len(a["brier_model"])), 4) if a["brier_model"] else None,
            "brier_market": round(sum(a["brier_market"]) / max(1, len(a["brier_market"])), 4) if a["brier_market"] else None,
            "brier_djyy": round(sum(a["brier_djyy"]) / max(1, len(a["brier_djyy"])), 4) if a["brier_djyy"] else None,
            "pnl": round(a["pnl"], 1),
        })

    # 滚动7天（按日期从新到旧取最近7个有预测的日期）
    recent = daily[-7:]
    prev = daily[-14:-7]
    def _agg(items):
        if not items:
            return None
        n = sum(i["n"] for i in items)
        hits = sum(i["hits"] for i in items)
        bfs = [i["brier_final"] for i in items if i.get("brier_final") is not None]
        pnl = sum(i["pnl"] for i in items)
        return {
            "n": n,
            "hit_rate": round(hits / max(1, n), 4),
            "brier_final": round(sum(bfs) / max(1, len(bfs)), 4) if bfs else None,
            "pnl": round(pnl, 1),
        }

    r7, p7 = _agg(recent), _agg(prev)
    verdict = "样本不足"
    if r7 and p7 and r7["n"] >= 5 and p7["n"] >= 5:
        d_hr = r7["hit_rate"] - p7["hit_rate"]
        if d_hr > 0.03:
            verdict = f"命中率提升 +{d_hr*100:.0f}pt"
        elif d_hr < -0.03:
            verdict = f"命中率下降 {d_hr*100:.0f}pt"
        else:
            verdict = "持平（±3pt 内）"

    # 分层归因
    def _layer(rows, key):
        out = {}
        for r in rows:
            v = r.get(key) or "?"
            a = out.setdefault(v, {"n": 0, "hits": 0, "brier_final": []})
            a["n"] += 1
            a["hits"] += 1 if r.get("hit") else 0
            b = r.get("brier_final")
            if b is not None:
                a["brier_final"].append(b)
        return [{"layer": k, "n": v["n"], "hit_rate": round(v["hits"]/max(1, v["n"]), 4),
                 "brier_final": round(sum(v["brier_final"])/max(1, len(v["brier_final"])), 4) if v["brier_final"] else None}
                for k, v in sorted(out.items(), key=lambda x: -x[1]["n"])]

    report = {
        "generated_at": date.today().isoformat(),
        "total_matches": len(rows),
        "date_range": [dates[0], dates[-1]],
        "overall": {
            "hit_rate": round(sum(1 for r in rows if r.get("hit")) / len(rows), 4),
            "brier_final": round(sum(r.get("brier_final") or 0 for r in rows) / len(rows), 4),
            "brier_model": round(sum(r.get("brier_model") or 0 for r in rows) / len(rows), 4),
            "brier_market": round(sum(r.get("brier_market") or 0 for r in rows) / len(rows), 4),
            "brier_djyy": round(sum(r.get("brier_djyy") or 0 for r in rows) / len(rows), 4),
        },
        "daily": daily,
        "rolling7": r7,
        "prev7": p7,
        "verdict": verdict,
        "by_league": _layer(rows, "league"),
        "by_odds_band": _layer(rows, "odds_band"),
        "by_conf_tier": _layer(rows, "confidence_tier"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    r = build_accuracy_trend(ROOT / "data" / "state" / "review_ledger.jsonl",
                             ROOT / "data" / "state" / "accuracy_trend.json")
    print(f"总场次 {r['total_matches']}, 总体命中率 {r['overall']['hit_rate']*100:.1f}%")
    print(f"趋势判定: {r['verdict']}")
    print("最近7天:", r["rolling7"], "| 前7天:", r["prev7"])
