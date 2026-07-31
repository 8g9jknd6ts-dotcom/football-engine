#!/usr/bin/env python3
"""新浪彩票赛果抓取 - 从 lotto.sina.cn 获取比赛结果

数据来源: lotto.sina.cn 公开 API（无需登录）
- 比赛列表: footballMatchListAll (含队名、比分、赔率、角球、红黄牌)
- 覆盖所有已完赛比赛，包括竞彩和非竞彩

用法:
    python scripts/fetch_sina_results.py                          # 抓取昨天赛果
    python scripts/fetch_sina_results.py --date 2026-07-30       # 指定日期
    python scripts/fetch_sina_results.py --date-from 2026-07-28 --date-to 2026-07-30  # 日期范围
"""

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
GATEWAY = "https://alpha.lottery.sina.com.cn/gateway/index/entry"
SX = {
    "format": "json",
    "__caller__": "wap",
    "__version__": "1.0.0",
    "__verno__": "10000",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://lotto.sina.cn/",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_match_list(target_date: str) -> list[dict]:
    """获取指定日期的比赛列表（含队名、比分、赔率）"""
    params = {**SX, "cat1": "footballMatchListAll", "date": target_date, "dpc": "1"}

    for attempt in range(3):
        try:
            resp = requests.get(GATEWAY, params=params, headers=HEADERS, timeout=15)
            data = resp.json()
            result = data.get("result", {})
            if result.get("status", {}).get("code") == 0:
                return result.get("data", [])
            print(f"  API error: {result.get('status', {}).get('msg')}")
            return []
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  ✗ Failed: {e}")
                return []


def extract_results(matches: list[dict]) -> list[dict]:
    """从比赛列表中提取已完赛的结果"""
    results = []
    for m in matches:
        status = m.get("status", "")
        # status: 2=in progress, 3=finished/ended, 4=not started
        if status not in ("2", "3"):
            continue

        score1 = m.get("score1", "")
        score2 = m.get("score2", "")
        if not score1 or not score2:
            continue

        try:
            home_score = int(score1)
            away_score = int(score2)
        except (ValueError, TypeError):
            continue

        results.append(
            {
                "match_id": m.get("matchId", ""),
                "home_team": m.get("team1", ""),
                "away_team": m.get("team2", ""),
                "home_score": home_score,
                "away_score": away_score,
                "half_home_score": int(m.get("halfScore1") or 0),
                "half_away_score": int(m.get("halfScore2") or 0),
                "league": m.get("league", ""),
                "match_time": m.get("matchTimeFormat", ""),
                "status": m.get("statusCn", ""),
                "status_detail": m.get("statusDetailCn", ""),
                "note": m.get("note", ""),
                "odds_home": float(m.get("euroO1") or 0),
                "odds_draw": float(m.get("euroO2") or 0),
                "odds_away": float(m.get("euroO3") or 0),
                "corner_home": m.get("cornerCount1", ""),
                "corner_away": m.get("cornerCount2", ""),
                "yellow_home": m.get("yellowCardCount1", ""),
                "yellow_away": m.get("yellowCardCount2", ""),
                "red_home": m.get("redCardCount1", ""),
                "red_away": m.get("redCardCount2", ""),
                "source": "sina",
            }
        )
    return results


def save_results(date_str: str, results: list[dict], output_dir: Path | None = None):
    """保存赛果到 data/daily/{date}/results.json（合并已有数据，队名去重）"""
    if output_dir is None:
        output_dir = ROOT / "data" / "daily" / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / "results_sina.json"
    
    # 合并已有数据（来自 DJYY 等源）
    existing = []
    if output_file.exists():
        try:
            existing = json.loads(output_file.read_text())
        except Exception:
            pass
    
    # 队名去重
    existing_teams = {(r.get("home_team"), r.get("away_team")) for r in existing}
    added = 0
    for r in results:
        if (r.get("home_team"), r.get("away_team")) not in existing_teams:
            existing.append(r)
            added += 1
    
    output_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    print(f"  ✓ {date_str}: {len(results)} 新浪 + {len(existing)-len(results)} 已有 = {len(existing)} 场 → {output_file}")
    return output_file


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Fetch Sina lottery results")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD)")
    parser.add_argument("--date-from", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--date-to", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", help="Output directory override")
    args = parser.parse_args()

    if args.date:
        dates = [args.date]
    elif args.date_from and args.date_to:
        start = datetime.strptime(args.date_from, "%Y-%m-%d")
        end = datetime.strptime(args.date_to, "%Y-%m-%d")
        dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days + 1)]
    else:
        # Default: yesterday
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        dates = [yesterday]

    print(f"[fetch_sina] Fetching {len(dates)} date(s) from Sina API...")

    output_dir = None
    if args.output_dir:
        output_dir = Path(args.output_dir)

    total = 0
    for d in dates:
        print(f"  → fetching {d}...")
        matches = fetch_match_list(d)
        if not matches:
            print(f"    ⚠ No matches for {d}")
            continue
        results = extract_results(matches)
        if results:
            save_results(d, results, output_dir)
            total += len(results)
        else:
            print(f"    ⚠ No completed matches for {d}")

    print(f"\n  ✓ Total: {total} results saved")
    return total


if __name__ == "__main__":
    main()