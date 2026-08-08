from __future__ import annotations
"""静态报告生成器 - 专业体育分析仪表盘

展示: 预测概率/赔率对比/价值检测/xG/置信度/冷门风险/三票方案/熔断状态/决策链完整性
交互式: 点击展开比赛详情, 多Tab分析面板, 响应式布局
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
# 以脚本方式运行时 sys.path[0] 是 engine/ 目录，补上仓库根，保证 import engine.* 可用
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_site():
    """生成静态 HTML 报告（多日期）"""
    web_dir = ROOT / "web"
    web_dir.mkdir(parents=True, exist_ok=True)

    daily_root = ROOT / "data" / "daily"
    today = date.today().isoformat()

    # 收集所有有预测数据的日期
    all_dates = []
    if daily_root.exists():
        all_dates = sorted(
            [d.name for d in daily_root.iterdir() if d.is_dir() and (d / "predictions.json").exists()],
            reverse=True,
        )

    if not all_dates:
        all_dates = [today]

    # 构建全局结果索引（扫描所有日期目录的 results.json，按队名索引）
    all_results = _load_all_results(daily_root, all_dates)

    # 缓存 league_matrix 到本地（从 DJYY 获取）
    league_matrix_path = ROOT / "data" / "league_matrix.json"
    if not league_matrix_path.exists():
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://djyylive.com/data/league-matrix.json",
                headers={"User-Agent": "football-engine/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                league_matrix_path.parent.mkdir(parents=True, exist_ok=True)
                league_matrix_path.write_bytes(data)
        except Exception:
            pass

    # 为每个日期生成页面
    for target_date in all_dates:
        daily_dir = daily_root / target_date
        predictions = _load_json(daily_dir / "predictions.json", [])
        bundle = _load_json(daily_dir / f"decision_bundle_{target_date}.json", {})
        if not bundle:
            bundle = _load_json(daily_dir / f"decision_bundle_{target_date}_v1.json", {})
        ticket = _load_json(daily_dir / "ticket_plan.json", {})
        breaker = _load_json(ROOT / "data" / "state" / "circuit_breaker.json", {})
        health = _load_json(web_dir / "health-status.json", {"healthy": True})
        results = _load_json(daily_dir / "results.json", [])
        # 如果当日 results.json 为空，用全局索引匹配（仅对历史日期）
        from datetime import date as dt_date
        if not results and predictions:
            is_today = (target_date == dt_date.today().isoformat())
            if not is_today:
                results = _match_results_to_predictions(predictions, all_results)
        review_ledger = _load_ledger(ROOT / "data" / "state" / "review_ledger.jsonl", target_date)
        results_html_preds = predictions

        html = _render_html(target_date, predictions, bundle, ticket, breaker, health, results, results_html_preds, all_dates, review_ledger)

        # 最新日期写index.html, 所有日期写dated页面
        if target_date == all_dates[0]:
            (web_dir / "index.html").write_text(html, encoding="utf-8")
        (web_dir / f"{target_date}.html").write_text(html, encoding="utf-8")

    # 全局清理旧版决策包（所有日期保留最新3个，历史日期也清理）
    try:
        from engine.integrity.decision_bundle import DecisionBundle
        _pruned = DecisionBundle.prune_all_dates(daily_root, keep=3)
        if _pruned:
            print(f"[build_site] 🧹 已清理 {_pruned} 个旧决策包版本")
    except Exception as _e:
        print(f"[build_site] ⚠ 决策包清理跳过: {_e}")

    status = {
        "date": all_dates[0] if all_dates else today,
        "generated_at": datetime.now().isoformat(),
        "prediction_count": len(_load_json(daily_root / all_dates[0] / "predictions.json", [])) if all_dates else 0,
        "available_dates": all_dates,
        "healthy": True,
    }
    (web_dir / "report-status.json").write_text(json.dumps(status, indent=2))
    # EV 价值区报告（全量复盘分层 ROI，供页面与 Kelly 参考）
    try:
        from engine.review.ev_report import build_report
        build_report(daily_root, ROOT / "data" / "state" / "ev_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ EV 报告生成跳过: {e}")
    # 让球玩法回测报告（验证让球 EV 历史表现，从有让球赔率的场次积累）
    try:
        from engine.strategy.handicap_ev import build_handicap_report
        build_handicap_report(daily_root, ROOT / "data" / "state" / "handicap_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ 让球回测报告生成跳过: {e}")
    # 多玩法回测报告（总进球/波胆/半全场 ROI 积累）
    try:
        from engine.strategy.multi_play_ev import build_plays_report
        build_plays_report(daily_root, ROOT / "data" / "state" / "plays_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ 多玩法回测报告生成跳过: {e}")
    # 联赛分层价值报告（送钱区禁投依据）
    league_report = {}
    try:
        from engine.review.league_report import build_league_report
        # 老系统联赛复盘样本（世界杯积累的另一预测域不能搬参数，但同域联赛样本可合并）
        _legacy = []
        _legacy_path = ROOT / "data" / "state" / "legacy_league_samples.json"
        if _legacy_path.exists():
            _legacy = json.loads(_legacy_path.read_text(encoding="utf-8"))
        league_report = build_league_report(daily_root, ROOT / "data" / "state" / "league_report.json", _legacy)
        if _legacy:
            print(f"[build_site] 🧬 联赛分层已合并 {len(_legacy)} 场老系统历史复盘")
    except Exception as e:
        print(f"[build_site] ⚠ 联赛分层报告生成跳过: {e}")
    # 串关回测报告（2串1 能不能玩的实证）
    parlay_report = {}
    try:
        from engine.review.parlay_report import build_parlay_report
        parlay_report = build_parlay_report(daily_root, ROOT / "data" / "state" / "parlay_report.json")
    except Exception as e:
        print(f"[build_site] ⚠ 串关回测报告生成跳过: {e}")
    # 准确率趋势报告（回答"是否每天在提升"）
    try:
        from engine.review.accuracy_trend import build_accuracy_trend
        trend_report = build_accuracy_trend(ROOT / "data" / "state" / "review_ledger.jsonl", ROOT / "data" / "state" / "accuracy_trend.json")
    except Exception as e:
        print(f"[build_site] ⚠ 准确率趋势报告生成跳过: {e}")
        trend_report = {}
    # 每日简报（最新日期）
    try:
        if all_dates:
            _latest = all_dates[0]
            _lp = _load_json(daily_root / _latest / "predictions.json", [])
            _tp = _load_json(daily_root / _latest / "ticket_plan.json", {})
            _rv = _load_json(daily_root / _latest / "review.json", None)
            _brief = _build_daily_brief(web_dir, _latest, _lp, _tp, _rv, league_report, parlay_report, trend_report)
            print(f"[build_site] 📋 每日简报已生成: {_brief.name}")
    except Exception as e:
        print(f"[build_site] ⚠ 每日简报生成跳过: {e}")
    print(f"[build_site] 仪表盘已生成: {len(all_dates)} 个日期页面")


def _load_all_results(daily_root: Path, all_dates: list) -> dict:
    """扫描所有日期目录，构建全局 results 索引（按队名 + match_id）"""
    index = {}
    search_dates = set(all_dates)
    # 也扫描预测日期前后2天（结算可能跨天）
    for d in all_dates:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            for offset in range(-2, 3):
                from datetime import timedelta
                adj = (dt + timedelta(days=offset)).strftime("%Y-%m-%d")
                search_dates.add(adj)
        except Exception:
            pass

    for d in sorted(search_dates):
        results_file = daily_root / d / "results.json"
        if not results_file.exists():
            continue
        try:
            results = json.loads(results_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in results:
            mid = r.get("match_id", "")
            if mid:
                index[mid] = r
            hm = r.get("home_team", "")
            aw = r.get("away_team", "")
            if hm and aw:
                key = f"{hm}_vs_{aw}"
                # 不覆盖已有的精确 match_id 索引
                if key not in index:
                    index[key] = r
            # 场次号索引
            fixture = _extract_fixture(mid)
            if fixture and fixture not in index:
                index[fixture] = r
    return index


def _match_results_to_predictions(predictions: list, all_results: dict) -> list:
    """用全局索引为预测匹配赛果，返回匹配的 results 列表"""
    matched = []
    for p in predictions:
        mid = p.get("match_id", "")
        r = all_results.get(mid)
        if not r:
            fixture = _extract_fixture(mid)
            if fixture:
                r = all_results.get(fixture)
        if not r:
            hm = p.get("home_team", "")
            aw = p.get("away_team", "")
            if hm and aw:
                r = all_results.get(f"{hm}_vs_{aw}")
        if r:
            matched.append(r)
    return matched


def _load_ledger(ledger_path: Path, target_date: str) -> list:
    """从 review_ledger.jsonl 读取指定日期的复盘记录"""
    records = []
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("date") == target_date:
                    records.append(r)
            except Exception:
                pass
    return records


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _extract_fixture(match_id: str) -> str:
    """从 match_id 提取场次号，如 '2026-07-20_周日201' → '201'"""
    if not match_id:
        return ""
    import re
    parts = re.split(r'[_\-]', match_id)
    for part in reversed(parts):
        m = re.search(r'(\d+)$', part)
        if m:
            return m.group(1)
    return ""


def _extract_team_key(match_id: str) -> str:
    """从 match_id 提取队名键，如 '2026-07-21_周二201' → 从 predictions 中查找对应队名"""
    return ""


def _slug(s: str) -> str:
    """生成 CSS-safe slug"""
    import re
    return re.sub(r'[^a-zA-Z\u4e00-\u9fff]', '-', s)


def _render_html(today, predictions, bundle, ticket, breaker, health, results=None, results_preds=None, all_dates=None, review_ledger=None):
    # 计算摘要
    total = len(predictions)
    # EV 价值区报告（全量已结算预测的分层 ROI）
    try:
        _ev_path = ROOT / "data" / "state" / "ev_report.json"
        _ev_report = json.loads(_ev_path.read_text(encoding="utf-8")) if _ev_path.exists() else {}
    except Exception:
        _ev_report = {}
    ev_html = _ev_section(_ev_report)
    # 三票方案中的场次 = 真正的价值投注
    value_matches = set()
    for it in ticket.get("stable", []) + ticket.get("value", []):
        value_matches.add(it.get("match", ""))
    value_bets = [p for p in predictions if _is_value(p, value_matches)]
    avg_conf = sum(p.get("confidence", 0) for p in predictions) / max(1, total)
    total_stake = ticket.get("total_stake", 0)
    exp_roi = ticket.get("expected_roi", 0)
    breaker_mult = ticket.get("breaker_multiplier", 1.0)
    tier, tier_reason = _breaker_tier(breaker)

    # 联赛矩阵面板
    league_matrix = _load_league_matrix(ROOT / "data" / "league_matrix.json")
    league_matrix_html = _league_matrix_section(league_matrix, predictions)

    # 联赛分层价值报告（送钱区/价值区一目了然）
    league_html = ""
    try:
        _lrep_path = ROOT / "data" / "state" / "league_report.json"
        _lrep = json.loads(_lrep_path.read_text(encoding="utf-8")) if _lrep_path.exists() else {}
        _rows = _lrep.get("leagues", [])[:10]
        # 判平反馈（2026-08-05 结构升级：判平强度连续自适应，可核查学习过程）
        _lp_path = ROOT / "data" / "state" / "league_params.json"
        _lp = json.loads(_lp_path.read_text(encoding="utf-8")) if _lp_path.exists() else {}
        if _rows:
            def _draw_cell(league):
                p = _lp.get(league, {})
                dp = p.get("draw_predictions", 0)
                dh = p.get("draw_hits", 0)
                if dp == 0:
                    return '<td style="color:var(--dim)">—</td>'
                prec = dh / dp
                st = 0.85 if (dp >= 4 and dh >= 3) else (0.35 if dp >= 2 else 0.40)
                _c = "var(--green)" if prec >= 0.5 else ("var(--amber)" if prec > 0 else "var(--red)")
                return f'<td style="color:{_c}">{dh}/{dp} ({prec*100:.0f}%)<br><span style="font-size:0.62rem;color:var(--dim)">强度 {st:.2f}</span></td>'
            _rows_html = "".join(
                f'<tr><td>{r["league"]}</td><td>{r["n"]}</td>'
                f'<td>{r["hit_rate"]*100:.0f}%</td><td>{r["avg_odds"]:.2f}</td>'
                f'<td style="color:{"var(--green)" if r["roi"] > 0 else "var(--red)"};font-weight:700">{r["roi"]*100:+.1f}%</td>'
                f'<td>{_verdict_cell(r)}</td>'
                f'<td>{_recent_cell(r)}</td>'
                f'{_draw_cell(r["league"])}</tr>'
                for r in _rows if r["n"] >= 3
            )
            if _rows_html:
                league_html = f'''
  <div class="section-title">联赛分层（送钱区禁投 · 回暖自动解禁 · 判平强度自适应）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>联赛</th><th>场数</th><th>命中率</th><th>均赔</th><th>ROI</th><th>判断</th><th>近期窗口</th><th>判平(命中/次数·强度)</th></tr>
    {_rows_html}
  </table>
  <div style="padding:6px 2px;font-size:0.62rem;color:var(--dim)">回暖解禁 = 累计口径送钱区，但最近5场命中≥60% → 自动解除禁投观察；再拉胯自动打回送钱区。</div>
  </div>'''
    except Exception as e:
        print(f"⚠ 联赛分层区块跳过: {e}")

    # 高置信反向样本库（2026-08-06，借鉴 MBS 8/2 AIK 案例）
    hcr_html = ""
    try:
        _hcr_path = ROOT / "data" / "state" / "high_conf_reversals.jsonl"
        if _hcr_path.exists():
            _hcr_items = []
            for _line in _hcr_path.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _hcr_items.append(json.loads(_line))
                except Exception:
                    continue
            if _hcr_items:
                _hcr_rows = ""
                for _s in _hcr_items[-8:][::-1]:  # 最近 8 条，新的在前
                    _hcr_rows += (
                        f'<tr><td>{_s.get("date","")}</td><td>{_s.get("league","")}</td>'
                        f'<td>{_s.get("teams","")}</td>'
                        f'<td>预测{_s.get("direction","")} <b>{_s.get("conf",0)*100:.0f}%</b></td>'
                        f'<td style="color:var(--red)">实际{_s.get("actual_dir","")}</td></tr>'
                    )
                hcr_html = f'''
  <div class="section-title">高置信反向样本库（{len(_hcr_items)} 场 · 预测≥60% + 市场同向却翻车）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>日期</th><th>联赛</th><th>对阵</th><th>预测</th><th>实际</th></tr>
    {_hcr_rows}
  </table>
  <div style="padding:6px 2px;font-size:0.62rem;color:var(--dim)">独立归档复核（借鉴 MBS AIK 案例），
  不归因于模型-市场分歧；与 50-60% 段降档互补：60%+ 段虽整体命中最好，但反向样本单独跟踪。</div>
  </div>'''
    except Exception as e:
        print(f"⚠ 高置信反向样本库区块跳过: {e}")

    # 准确率趋势（诚实回答"是否每天在提升"）
    trend_html = ""
    try:
        _trend_path = ROOT / "data" / "state" / "accuracy_trend.json"
        _trend = json.loads(_trend_path.read_text(encoding="utf-8")) if _trend_path.exists() else {}
        _daily = _trend.get("daily", [])[-10:]
        if _daily:
            _v = _trend.get("verdict", "样本不足")
            _v_color = "var(--green)" if "提升" in _v else ("var(--red)" if "下降" in _v else "var(--amber)")
            _rows_html = "".join(
                f'<tr><td>{d["date"]}</td><td>{d["n"]}</td><td>{d["hits"]}/{d["n"]}</td>'
                f'<td style="font-weight:700">{d["hit_rate"]*100:.0f}%</td>'
                f'<td>{d["brier_final"]:.2f}</td><td>{d["cum_hit_rate"]*100:.0f}%</td>'
                f'<td style="color:{"var(--green)" if d["pnl"] > 0 else ("var(--red)" if d["pnl"] < 0 else "var(--dim)")}">{d["pnl"]:+.0f}</td></tr>'
                for d in reversed(_daily)
            )
            trend_html = f'''
  <div class="section-title">准确率趋势（最近7天 vs 前7天: <span style="color:{_v_color}">{_v}</span>）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>日期</th><th>场次</th><th>命中</th><th>命中率</th><th>Brier</th><th>累计命中率</th><th>盈亏</th></tr>
    {_rows_html}
  </table>
  </div>'''
    except Exception as e:
        print(f"⚠ 准确率趋势区块跳过: {e}")

    # 比分命中率分层（2026-08-05 闭环：主推前三不靠谱→前5的实证，账本可核查）
    score_trend_html = ""
    try:
        _ledger_recs = []
        _lp = ROOT / "data" / "state" / "review_ledger.jsonl"
        if _lp.exists():
            for _line in _lp.read_text(encoding="utf-8").strip().split("\n"):
                if _line.strip():
                    try:
                        _ledger_recs.append(json.loads(_line))
                    except Exception:
                        continue
        if _ledger_recs:
            _n = len(_ledger_recs)
            def _rate(cond):
                return sum(1 for r in _ledger_recs if cond(r)) / _n
            # 近7天
            _recent = [r for r in _ledger_recs if r.get("date", "") >= "2026-07-30"]
            _rn = len(_recent)
            def _rate7(cond):
                return sum(1 for r in _recent if cond(r)) / _rn if _rn else 0
            score_trend_html = f'''
  <div class="section-title">比分命中率（全量 {_n} 场 · 近7天 {_rn} 场）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>推荐档</th><th>全量命中</th><th>全量命中率</th><th>近7天命中</th><th>近7天命中率</th><th>说明</th></tr>
    <tr><td>主推 top1</td><td>{sum(1 for r in _ledger_recs if r.get('score_rank')==1)}</td><td style="font-weight:700">{_rate(lambda r: r.get('score_rank')==1)*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_rank')==1)}</td><td>{_rate7(lambda r: r.get('score_rank')==1)*100:.0f}%</td><td style="color:var(--dim)">只押最可能比分（太难）</td></tr>
    <tr><td>主推 top3</td><td>{sum(1 for r in _ledger_recs if r.get('score_top3_hit'))}</td><td style="font-weight:700">{_rate(lambda r: r.get('score_top3_hit'))*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_top3_hit'))}</td><td>{_rate7(lambda r: r.get('score_top3_hit'))*100:.0f}%</td><td style="color:var(--dim)">原主推 3 个（8/5 起改 5 个）</td></tr>
    <tr><td>主推 top5</td><td>{sum(1 for r in _ledger_recs if r.get('score_top5_hit'))}</td><td style="font-weight:700;color:var(--green)">{_rate(lambda r: r.get('score_top5_hit'))*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_top5_hit'))}</td><td style="color:var(--green)">{_rate7(lambda r: r.get('score_top5_hit'))*100:.0f}%</td><td style="color:var(--dim)">当前主推 5 个</td></tr>
    <tr><td>候选 top8</td><td>{sum(1 for r in _ledger_recs if r.get('score_top8_hit'))}</td><td style="font-weight:700">{_rate(lambda r: r.get('score_top8_hit'))*100:.0f}%</td><td>{sum(1 for r in _recent if r.get('score_top8_hit'))}</td><td>{_rate7(lambda r: r.get('score_top8_hit'))*100:.0f}%</td><td style="color:var(--dim)">DJYY 完整候选列表</td></tr>
  </table>
  </div>

  <div class="section-title">预测 vs 实际比分分布（2026-08-05 科学化：不再无脑押 1-0/1-1/2-0）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>统计</th><th>修复前（8/5 前）</th><th>修复后（8/5 起）</th><th>实际</th><th>说明</th></tr>
    <tr><td>top1 为 1-0/1-1/0-0/0-1 低比分</td><td style="color:var(--red);font-weight:700">86%</td><td style="color:var(--green)">随 xG 差调整</td><td style="color:var(--dim)">39%</td><td style="color:var(--dim)">碾压局不再押 1-1</td></tr>
    <tr><td>top1 为 ≥3 球比分</td><td style="color:var(--red)">7%</td><td style="color:var(--green);font-weight:700">31%</td><td style="color:var(--dim)">50%</td><td style="color:var(--dim)">xG差≥0.8 高比分×1.3 重排</td></tr>
    <tr><td>主推 top5 命中率</td><td>52.2%</td><td style="color:var(--green);font-weight:700">54.9%</td><td style="color:var(--dim)">—</td><td style="color:var(--dim)">walk-forward 113 场，命中不降反升</td></tr>
    <tr><td>top1 命中率</td><td>9.7%</td><td style="color:var(--green);font-weight:700">13.3%</td><td style="color:var(--dim)">—</td><td style="color:var(--dim)">重排后单场最可能比分更准</td></tr>
  </table>
  </div>'''

            # 双源比分命中对比（2026-08-05 结构升级：DJYY vs MC，谁准数据说话）
            _dj_items = [r for r in _ledger_recs if r.get('score_djyy_rank', -1) >= 0]
            _mc_items = [r for r in _ledger_recs if r.get('score_mc_rank', -1) >= 0]
            _src_rows = ""
            if _dj_items:
                _dh = sum(1 for r in _dj_items if 1 <= r.get('score_djyy_rank', 0) <= 5)
                _src_rows += f'<tr><td>DJYY 分析源</td><td>{_dh}/{len(_dj_items)}</td><td style="font-weight:700">{_dh/len(_dj_items)*100:.0f}%</td><td style="color:var(--dim)">分析文本提取的比分候选（8/5 起记录）</td></tr>'
            if _mc_items:
                _mh = sum(1 for r in _mc_items if 1 <= r.get('score_mc_rank', 0) <= 5)
                _src_rows += f'<tr><td>MC 模拟源</td><td>{_mh}/{len(_mc_items)}</td><td style="font-weight:700">{_mh/len(_mc_items)*100:.0f}%</td><td style="color:var(--dim)">泊松蒙特卡洛模拟（8/5 起记录）</td></tr>'
            if _src_rows:
                score_trend_html += f'''
  <div class="section-title">比分候选来源对比（DJYY vs MC，8/5 起累积 · 主推前5命中率）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>来源</th><th>命中/场次</th><th>命中率</th><th>说明</th></tr>
    {_src_rows}
  </table>
  </div>'''
            else:
                score_trend_html += '''
  <div class="section-title">比分候选来源对比（DJYY vs MC · 8/5 起记录，样本累积中）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>来源</th><th>命中/场次</th><th>命中率</th><th>说明</th></tr>
    <tr><td>DJYY 分析源</td><td colspan="3" style="color:var(--dim)">自 8/5 双源融合上线后的场次开始记录，结算几场后自动填充</td></tr>
    <tr><td>MC 模拟源</td><td colspan="3" style="color:var(--dim)">自 8/5 双源融合上线后的场次开始记录，结算几场后自动填充</td></tr>
  </table>
  </div>'''

            # 盘口信号命中率（2026-08-05 结构化验证：压缩比方向信号是否有效，累积说话）
            _ms_items = [r for r in _ledger_recs if r.get('market_signal_hit') is not None]
            if _ms_items:
                _msh = sum(1 for r in _ms_items if r.get('market_signal_hit'))
                _msr = _msh / len(_ms_items)
                _ms_color = "var(--green)" if _msr >= 0.45 else ("var(--red)" if _msr <= 0.35 else "var(--amber)")
                _ms_verdict = ("≥基线(44%)，信号有预测力" if _msr >= 0.45
                               else ("显著低于基线，信号待观察" if _msr <= 0.35 else "与基线接近，继续累积样本"))
                score_trend_html += f'''
  <div class="section-title">盘口信号命中率（压缩比资金流向，样本累积验证中）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>信号</th><th>命中/场次</th><th>命中率</th><th>模型方向基线</th><th>判断</th></tr>
    <tr><td>欧赔压缩方向</td><td>{_msh}/{len(_ms_items)}</td><td style="font-weight:700;color:{_ms_color}">{_msr*100:.0f}%</td><td>{sum(1 for r in _ledger_recs if r.get('hit'))}/{len(_ledger_recs)} ({sum(1 for r in _ledger_recs if r.get('hit'))/len(_ledger_recs)*100:.0f}%)</td><td style="color:{_ms_color}">{_ms_verdict}</td></tr>
  </table>
  </div>'''
    except Exception as e:
        print(f"⚠ 比分命中率区块跳过: {e}")

    # 水位监控汇总（2026-08-05 盘口系统修复）：当天各场水位时间序列累积情况
    # 由每次 fetch_sina_odds 追加快照形成；展示"哪场资金在动"，识别赛前资金流。
    odds_series_html = ""
    try:
        _os_rows = []
        for _p in predictions:
            _so = _p.get("sina_odds") or {}
            _ser = _so.get("series") or {}
            if _ser.get("points", 0) < 2:
                continue
            _parts = []
            for _side, _key in (("主", "recent_home"), ("客", "recent_away")):
                _v = _ser.get(_key)
                if _v is not None and abs(_v) >= 0.5:
                    _parts.append(f"{_side}{'↓' if _v < 0 else '↑'}{abs(_v):.1f}%")
            if not _parts:
                continue
            _os_rows.append(
                f'<tr><td>{_p.get("match_id", "").split("_")[-1]}</td>'
                f'<td>{_p.get("home_team", "")} vs {_p.get("away_team", "")}</td>'
                f'<td>{_ser.get("points", 0)}次</td>'
                f'<td>{" ".join(_parts)}</td>'
                f'<td style="color:var(--dim)">{_ser.get("span_min", "") and f"跨{_ser.get("span_min"):.0f}min" or ""}</td></tr>'
            )
        if _os_rows:
            odds_series_html = f'''
  <div class="section-title">水位监控（时间序列累积中：每次抓取追加快照，赛前资金流可查）</div>
  <div style="overflow-x:auto">
  <table class="edge-table">
    <tr><th>场次</th><th>对阵</th><th>快照数</th><th>近期水位变化</th><th>跨度</th></tr>
    {''.join(_os_rows)}
  </table>
  </div>'''
    except Exception as e:
        print(f"⚠ 水位监控区块跳过: {e}")

    # 渲染比赛卡片（按联赛分组）
    cards = ""
    results_map = {}
    if results:
        for r in results:
            mid = r.get("match_id", "")
            results_map[mid] = r
            fixture = _extract_fixture(mid)
            if fixture:
                results_map[fixture] = r
            # 队名索引（最可靠，跨数据源通用）
            hm = r.get("home_team", "")
            aw = r.get("away_team", "")
            if hm and aw:
                results_map[f"{hm}_vs_{aw}"] = r
    elif review_ledger:
        # 从 review_ledger 构建 results_map（含比分推断）
        for rl in review_ledger:
            mid = rl.get("match_id", "")
            goals = rl.get("total_goals_actual", 0)
            idx = rl.get("actual_idx", -1)
            if idx == 0:
                hs, aw = (goals, 0) if goals > 0 else (1, 0)
            elif idx == 1:
                half = max(1, goals // 2)
                hs, aw = (half, goals - half)
            elif idx == 2:
                hs, aw = (0, goals) if goals > 0 else (0, 1)
            else:
                hs, aw = (0, 0)
            entry = {"match_id": mid, "home_score": hs, "away_score": aw}
            results_map[mid] = entry
            fixture = _extract_fixture(mid)
            if fixture:
                results_map[fixture] = entry

    # 按 match_id 排序：周六201→周六202→...→周日201→周日202→...→周一201→...
    import re
    _day_map = {'周六': 0, '周日': 1, '周一': 2, '周二': 3, '周三': 4, '周四': 5, '周五': 6}
    def _match_sort_key(p):
        mid = p.get("match_id", "")
        m = re.search(r'(周[一二三四五六日])(\d+)', mid)
        if m:
            day = _day_map.get(m.group(1), 99)
            num = int(m.group(2))
            return (day, num)
        return (99, 0)
    sorted_preds = sorted(predictions, key=_match_sort_key)

    # 联赛筛选导航（仅用于过滤，不改排序）
    from collections import Counter
    league_counts = Counter(p.get("competition", "其他") for p in sorted_preds)
    if len(league_counts) > 1:
        cards += '<div class="league-nav">'
        cards += '<button class="league-btn active" data-league="all">全部</button>'
        for lg, cnt in league_counts.most_common():
            cards += f'<button class="league-btn" data-league="{_slug(lg)}">{lg}<span class="cnt">{cnt}</span></button>'
        cards += '</div>'

    # 平铺渲染，严格按 match_id 排序
    global_idx = 0
    for p in sorted_preds:
        lg = p.get("competition", "其他")
        cards += f'<div class="league-section" data-league="{_slug(lg)}">'
        cards += _match_card(p, value_matches, global_idx, results_map)
        cards += '</div>'
        global_idx += 1 

    # 三票方案
    ticket_html = _ticket_section(ticket, predictions)
    # 串关方案（2026-08-08 新增：竞彩实际玩法，校准 EV 驱动）
    parlay_html = _parlay_section(ticket, predictions)

    # 赛果复盘（优先用 results.json，fallback review_ledger）
    results_html = _results_section(results, results_preds or predictions, review_ledger)

    # 系统面板
    system_html = _system_panel(breaker, bundle, tier, breaker_mult, tier_reason)

    health_badge = '<span class="badge ok">系统正常</span>' if health.get("healthy") else '<span class="badge warn">降级</span>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>竞彩分析引擎 - {today}</title>
<style>
:root {{
  --bg: #0a0e13;
  --surface: #111820;
  --surface2: #1a2332;
  --surface3: #212d3d;
  --border: #263344;
  --border-light: #2f4258;
  --text: #e8edf4;
  --text-secondary: #94a8c0;
  --dim: #6b8299;
  --blue: #3b82f6;
  --blue-dim: #1e40af;
  --red: #ef4444;
  --red-dim: #7f1d1d;
  --green: #22c55e;
  --green-dim: #14532d;
  --amber: #f59e0b;
  --amber-dim: #78350f;
  --purple: #a855f7;
  --purple-dim: #581c87;
  --cyan: #06b6d4;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 4px 24px rgba(0,0,0,0.4);
  --transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text', 'Segoe UI', 'Inter', sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
.page {{ max-width: 1140px; margin: 0 auto; padding: 28px 20px 48px; }}

/* ===== HEADER ===== */
.header {{
  display: flex; justify-content: space-between; align-items: flex-start;
  margin-bottom: 28px; padding-bottom: 20px; border-bottom: 1px solid var(--border);
  flex-wrap: wrap; gap: 14px;
}}
.header-left h1 {{
  font-size: 1.6rem; font-weight: 800; letter-spacing: -0.8px;
  background: linear-gradient(135deg, #e8edf4 0%, #94a8c0 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.header-left .sub {{
  color: var(--dim); font-size: 0.78rem; margin-top: 5px;
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  letter-spacing: 0.3px;
}}
.header-right {{ display: flex; align-items: center; gap: 10px; }}
.date-nav {{
  display: flex; gap: 6px; padding: 10px 0; overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}}
.date-btn {{
  padding: 5px 14px; border-radius: 16px; font-size: 0.78rem; font-weight: 600;
  color: var(--dim); background: var(--card); border: 1px solid var(--border);
  text-decoration: none; white-space: nowrap; transition: all 0.2s;
}}
.date-btn.active {{ color: #fff; background: var(--blue); border-color: var(--blue); }}
.date-btn:hover {{ border-color: var(--blue); color: var(--blue); }}
.date-btn.active:hover {{ color: #fff; }}
.badge {{
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 12px; border-radius: 20px;
  font-size: 0.68rem; font-weight: 700; letter-spacing: 0.8px;
  text-transform: uppercase;
}}
.badge::before {{ content: ''; width: 6px; height: 6px; border-radius: 50%; }}
.badge.ok {{ background: var(--green-dim); color: var(--green); border: 1px solid #166534; }}
.badge.ok::before {{ background: var(--green); animation: pulse 2s infinite; }}
.badge.warn {{ background: var(--amber-dim); color: var(--amber); border: 1px solid #92400e; }}
.badge.warn::before {{ background: var(--amber); }}
@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}

/* ===== KPI STATS BAR ===== */
.stats {{
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px;
  margin-bottom: 28px;
}}
.stat {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 14px 16px;
  transition: var(--transition); position: relative; overflow: hidden;
}}
.stat::after {{
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--blue), transparent);
  opacity: 0; transition: opacity 0.3s;
}}
.stat:hover {{ border-color: var(--border-light); transform: translateY(-1px); }}
.stat:hover::after {{ opacity: 1; }}
.stat .label {{
  font-size: 0.62rem; color: var(--dim); text-transform: uppercase;
  letter-spacing: 1px; font-weight: 600; margin-bottom: 4px;
}}
.stat .value {{ font-size: 1.35rem; font-weight: 800; letter-spacing: -0.5px; }}
.stat .value.green {{ color: var(--green); }}
.stat .value.amber {{ color: var(--amber); }}
.stat .value.red {{ color: var(--red); }}
.stat .value.blue {{ color: var(--blue); }}

/* ===== SECTION TITLES ===== */
.section-title {{
  font-size: 0.85rem; font-weight: 700; margin: 32px 0 14px;
  padding-left: 12px; border-left: 3px solid var(--blue);
  color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1px;
}}

/* ===== LEAGUE NAV ===== */
.league-nav {{
  display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px;
}}
.league-btn {{
  padding: 5px 14px; border-radius: 16px; font-size: 0.72rem; font-weight: 600;
  color: var(--text-secondary); background: var(--surface2); border: 1px solid var(--border);
  cursor: pointer; transition: var(--transition); white-space: nowrap;
  font-family: inherit;
}}
.league-btn:hover {{ border-color: var(--blue); color: var(--blue); }}
.league-btn.active {{ background: var(--blue); border-color: var(--blue); color: #fff; }}
.league-btn .cnt {{
  font-size: 0.62rem; color: var(--dim); margin-left: 4px;
}}
.league-btn.active .cnt {{ color: rgba(255,255,255,0.7); }}

/* ===== LEAGUE SECTION ===== */
.league-section {{ margin-bottom: 8px; }}
.league-section.hidden {{ display: none; }}
.league-header {{
  font-size: 0.78rem; font-weight: 700; color: var(--cyan);
  padding: 8px 12px; margin: 14px 0 8px;
  background: rgba(6,182,212,0.06); border-left: 3px solid var(--cyan);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  display: flex; align-items: center; gap: 8px;
}}
.league-count {{
  font-size: 0.65rem; font-weight: 400; color: var(--dim);
}}


/* ===== LEAGUE MATRIX TABLE ===== */
.lm-wrap {{
  overflow-x: auto; -webkit-overflow-scrolling: touch;
  margin-bottom: 4px; border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}}
.lm-table {{
  width: 100%; border-collapse: collapse; font-size: 0.68rem;
  white-space: nowrap;
}}
.lm-table thead {{ position: sticky; top: 0; z-index: 2; }}
.lm-table th {{
  background: var(--surface2); color: var(--dim); font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.5px;
  padding: 8px 10px; text-align: center; border-bottom: 2px solid var(--border);
  font-size: 0.6rem;
}}
.lm-table th:first-child {{ text-align: left; padding-left: 12px; }}
.lm-table th:nth-child(2) {{ text-align: left; }}
.lm-table td {{
  padding: 6px 10px; text-align: center; border-bottom: 1px solid rgba(38,51,68,0.5);
  color: var(--text-secondary);
}}
.lm-table td:first-child {{ padding-left: 12px; }}
.lm-table td:nth-child(2) {{ text-align: left; font-weight: 600; color: var(--text); }}
.lm-row:hover td {{ background: rgba(59,130,246,0.08); }}
.lm-row.active td {{
  background: rgba(34,197,94,0.12);
  font-weight: 600;
}}
.lm-row.active td:first-child {{ border-left: 3px solid var(--green); }}
.lm-row.active td:nth-child(2) {{
  color: var(--green);
  font-weight: 700;
}}
.lm-row.active td:nth-child(2)::before {{
  content: '● ';
  font-size: 0.5rem;
  vertical-align: middle;
  animation: pulse-dot 1.5s ease-in-out infinite;
}}
@keyframes pulse-dot {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.3; }}
}}
.lm-name {{ font-weight: 600; }}
.lm-num {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.65rem; }}
.lm-pct {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.65rem; }}
.lm-cat {{
  display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 0.58rem; font-weight: 700; letter-spacing: 0.5px;
  text-transform: uppercase;
}}
.lm-cat-tier1 {{ background: rgba(34,197,94,0.12); color: var(--green); }}
.lm-cat-tier2 {{ background: rgba(245,158,11,0.12); color: var(--amber); }}
.lm-cat-world {{ background: rgba(59,130,246,0.12); color: var(--blue); }}
.lm-cat-other {{ background: rgba(148,168,192,0.1); color: var(--dim); }}
.lm-cat-cup {{ background: rgba(168,85,247,0.15); color: var(--purple); }}
#league-matrix.collapsed {{ display: none; }}

/* ===== LEAGUE STATS BAR ===== */
.lg-stats-bar {{
  display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px;
}}
.lg-stat {{
  font-size: 0.62rem; padding: 2px 8px; border-radius: 4px;
  font-weight: 600; white-space: nowrap;
  background: rgba(148,168,192,0.08); color: var(--text-secondary);
}}
.lg-stat.h {{ color: var(--blue); }}
.lg-stat.d {{ color: var(--dim); }}
.lg-stat.a {{ color: var(--red); }}

/* ===== MATCH CARDS ===== */
.match {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); margin-bottom: 10px;
  transition: var(--transition); overflow: hidden;
}}
.match:hover {{ border-color: var(--border-light); box-shadow: var(--shadow); }}
.match.value-pick {{ border-left: 3px solid var(--green); }}
.match-header {{
  padding: 16px 18px; cursor: pointer; user-select: none;
  display: flex; flex-direction: column; gap: 10px;
}}
.match-header:active {{ background: var(--surface2); }}
.match-top {{
  display: flex; justify-content: space-between; align-items: center;
}}
.league-tag {{
  font-size: 0.65rem; color: var(--cyan); background: rgba(6,182,212,0.1);
  padding: 2px 8px; border-radius: 4px; font-weight: 600;
  border: 1px solid rgba(6,182,212,0.2);
}}
.match-meta {{ display: flex; align-items: center; gap: 8px; }}
.match-id {{ font-size: 0.65rem; color: var(--dim); font-family: monospace; }}
.value-badge {{
  font-size: 0.6rem; font-weight: 800; color: var(--green);
  background: var(--green-dim); padding: 2px 7px; border-radius: 4px;
  letter-spacing: 0.5px; border: 1px solid #166534;
}}
.teams {{
  display: flex; justify-content: center; align-items: center; gap: 14px;
}}
.team {{ font-size: 1.08rem; font-weight: 700; min-width: 90px; }}
.team.home {{ text-align: right; }}
.team.away {{ text-align: left; }}
.vs {{ color: var(--dim); font-size: 0.7rem; font-weight: 600; }}

/* Prob bar */
.prob-row {{
  display: flex; height: 28px; border-radius: 6px; overflow: hidden;
  background: var(--surface3);
}}
.prob-seg {{
  display: flex; align-items: center; justify-content: center;
  font-size: 0.65rem; font-weight: 700; color: #fff;
  min-width: 38px; transition: width 0.5s ease;
  text-shadow: 0 1px 2px rgba(0,0,0,0.5);
}}
.prob-seg.h {{ background: linear-gradient(135deg, #2563eb, #3b82f6); }}
.prob-seg.d {{ background: linear-gradient(135deg, #4b5563, #6b7280); }}
.prob-seg.a {{ background: linear-gradient(135deg, #dc2626, #ef4444); }}

/* Prediction pick */
.pred-pick {{
  text-align: center; padding: 6px 0 2px; font-size: 0.82rem;
}}
.pick-label {{
  font-size: 0.65rem; color: var(--dim); font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px; margin-right: 6px;
}}
.hcap-label {{
  background: rgba(147,51,234,0.15); color: var(--purple);
  padding: 1px 6px; border-radius: 4px; border: 1px solid rgba(147,51,234,0.35);
  margin-left: 6px; text-transform: none; letter-spacing: 0;
}}
.pick-val {{
  font-weight: 800; font-size: 0.88rem; padding: 2px 10px;
  border-radius: 6px;
}}
.pick-val.home {{ color: var(--blue); background: rgba(59,130,246,0.1); }}
.pick-val.draw {{ color: var(--text-secondary); background: rgba(107,114,128,0.15); }}
.pick-val.away {{ color: var(--red); background: rgba(239,68,68,0.1); }}
.pred-score {{
  font-size: 0.72rem; color: var(--amber); font-weight: 700;
  margin-left: 10px; padding: 2px 8px; border-radius: 4px;
  background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.2);
}}

/* Actual result comparison */
.actual-result {{
  text-align: center; padding: 4px 0 2px; font-size: 0.78rem;
  display: flex; align-items: center; justify-content: center; gap: 8px;
}}
.ar-label {{
  font-size: 0.62rem; color: var(--dim); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.5px;
}}
.ar-score {{
  font-weight: 800; font-size: 0.92rem; color: var(--text);
  font-family: 'SF Mono', 'Fira Code', monospace;
}}
.ar-outcome {{
  font-size: 0.72rem; font-weight: 700; padding: 1px 8px;
  border-radius: 4px;
}}
.ar-outcome.home {{ color: var(--blue); background: rgba(59,130,246,0.1); }}
.ar-outcome.draw {{ color: var(--text-secondary); background: rgba(107,114,128,0.15); }}
.ar-outcome.away {{ color: var(--red); background: rgba(239,68,68,0.1); }}
.ar-hit {{
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%;
  font-size: 0.72rem; font-weight: 800;
}}
.ar-hit.hit {{ background: var(--green-dim); color: var(--green); }}
.ar-hit.miss {{ background: var(--red-dim); color: var(--red); }}

/* Collapsed info row */
.match-info-row {{
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 6px;
}}
.conf-meter {{ display: inline-flex; align-items: center; gap: 5px; font-size: 0.72rem; }}
.conf-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.conf-dot.high {{ background: var(--green); box-shadow: 0 0 6px rgba(34,197,94,0.5); }}
.conf-dot.med {{ background: var(--amber); box-shadow: 0 0 6px rgba(245,158,11,0.4); }}
.conf-dot.low {{ background: var(--dim); }}
.info-chip {{
  font-size: 0.68rem; color: var(--dim);
  display: inline-flex; align-items: center; gap: 3px;
}}
.info-chip b {{ color: var(--text-secondary); font-weight: 600; }}
.expand-icon {{
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: 6px;
  font-size: 0.68rem; font-weight: 600; color: var(--blue);
  background: rgba(59,130,246,0.1); border: 1px solid rgba(59,130,246,0.3);
  cursor: pointer; transition: var(--transition); white-space: nowrap;
}}
.expand-icon:hover {{ background: rgba(59,130,246,0.2); border-color: var(--blue); }}
.match.expanded .expand-icon {{ transform: none; color: var(--dim); background: var(--surface2); border-color: var(--border); }}

/* ===== EXPANDED DETAIL PANEL ===== */
.match-detail {{
  display: none; border-top: 1px solid var(--border);
  background: var(--surface2); padding: 0;
}}
.match.expanded .match-detail {{ display: block; }}

/* Tabs */
.tab-bar {{
  display: flex; border-bottom: 1px solid var(--border);
  background: var(--surface);
}}
.tab-btn {{
  flex: 1; padding: 10px 8px; text-align: center;
  font-size: 0.72rem; font-weight: 700; color: var(--dim);
  cursor: pointer; border: none; background: none;
  border-bottom: 2px solid transparent; transition: var(--transition);
  text-transform: uppercase; letter-spacing: 0.5px;
}}
.tab-btn:hover {{ color: var(--text-secondary); background: var(--surface2); }}
.tab-btn.active {{ color: var(--blue); border-bottom-color: var(--blue); background: var(--surface2); }}
.tab-content {{ display: none; padding: 16px 18px; }}
.tab-content.active {{ display: block; }}

/* Model comparison table */
.model-table {{ width: 100%; border-collapse: collapse; font-size: 0.72rem; }}
.model-table th {{
  text-align: left; padding: 6px 8px; color: var(--dim);
  font-weight: 600; font-size: 0.65rem; text-transform: uppercase;
  letter-spacing: 0.5px; border-bottom: 1px solid var(--border);
}}
.model-table td {{
  padding: 7px 8px; border-bottom: 1px solid rgba(38,51,68,0.5);
  vertical-align: middle;
}}
.model-table tr:last-child td {{ border-bottom: none; }}
.model-table .src-label {{ color: var(--text-secondary); font-weight: 600; white-space: nowrap; }}
.prob-bar-cell {{ width: 55%; }}
.mini-bar-wrap {{ display: flex; height: 16px; border-radius: 4px; overflow: hidden; background: var(--surface3); }}
.mini-bar {{
  display: flex; align-items: center; justify-content: center;
  font-size: 0.58rem; font-weight: 700; color: #fff; min-width: 24px;
}}
.mini-bar.h {{ background: var(--blue); }}
.mini-bar.d {{ background: #4b5563; }}
.mini-bar.a {{ background: var(--red); }}

/* Elo & Wilson */
.elo-row {{
  display: flex; gap: 16px; margin-top: 12px; flex-wrap: wrap;
}}
.elo-chip {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 8px 14px;
  font-size: 0.72rem; flex: 1; min-width: 120px;
}}
.elo-chip .elo-label {{ color: var(--dim); font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.5px; }}
.elo-chip .elo-val {{ font-size: 1.1rem; font-weight: 800; margin-top: 2px; }}
.wilson-meter {{ margin-top: 12px; }}
.wilson-label {{ font-size: 0.68rem; color: var(--dim); margin-bottom: 5px; }}
.wilson-track {{
  height: 8px; background: var(--surface3); border-radius: 4px; overflow: hidden;
}}
.wilson-fill {{
  height: 100%; border-radius: 4px;
  background: linear-gradient(90deg, var(--amber), var(--green));
  transition: width 0.6s ease;
}}
.wilson-val {{ font-size: 0.68rem; color: var(--text-secondary); margin-top: 3px; }}

/* Odds tab */
.odds-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px; margin-bottom: 14px;
}}
.odds-box {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 10px 12px;
}}
.odds-box .ob-label {{ font-size: 0.62rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; }}
.odds-box .ob-val {{ font-size: 1rem; font-weight: 700; margin-top: 2px; }}
.edge-table {{ width: 100%; border-collapse: collapse; font-size: 0.72rem; margin-top: 12px; }}
.edge-table th {{
  text-align: left; padding: 5px 8px; color: var(--dim);
  font-size: 0.62rem; text-transform: uppercase; border-bottom: 1px solid var(--border);
}}
.edge-table td {{ padding: 6px 8px; border-bottom: 1px solid rgba(38,51,68,0.4); }}
.edge-pos {{ color: var(--green); font-weight: 700; }}
.edge-neg {{ color: var(--red); font-weight: 600; }}
.reverse-box {{
  margin-top: 14px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 12px 14px;
}}
.reverse-box h5 {{ font-size: 0.7rem; color: var(--amber); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.reverse-row {{ display: flex; justify-content: space-between; font-size: 0.72rem; padding: 3px 0; }}
.reverse-row .rk {{ color: var(--dim); }}
.same-odds-box {{
  margin-top: 10px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 12px 14px;
}}
.same-odds-box h5 {{ font-size: 0.7rem; color: var(--cyan); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}

/* Distribution tab */
.scores-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 6px; margin-bottom: 14px;
}}
.score-cell {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 4px; text-align: center;
}}
.score-cell .sc-score {{ font-size: 0.8rem; font-weight: 800; }}
.score-cell .sc-prob {{ font-size: 0.6rem; color: var(--dim); margin-top: 1px; }}
.goals-bars {{ margin: 14px 0; }}
.goal-bar-row {{
  display: flex; align-items: center; gap: 8px; margin-bottom: 5px;
}}
.goal-bar-label {{ font-size: 0.68rem; color: var(--dim); width: 50px; text-align: right; flex-shrink: 0; }}
.goal-bar-track {{ flex: 1; height: 18px; background: var(--surface3); border-radius: 4px; overflow: hidden; }}
.goal-bar-fill {{
  height: 100%; border-radius: 4px; display: flex; align-items: center;
  padding-left: 6px; font-size: 0.6rem; font-weight: 700; color: #fff;
  background: linear-gradient(90deg, var(--purple-dim), var(--purple));
  transition: width 0.5s ease;
}}
.xg-compare {{ margin-top: 14px; }}
.xg-compare h5 {{ font-size: 0.7rem; color: var(--text-secondary); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }}
.xg-bar-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }}
.xg-bar-label {{ font-size: 0.68rem; color: var(--dim); width: 60px; text-align: right; flex-shrink: 0; }}
.xg-bar-track {{ flex: 1; height: 22px; background: var(--surface3); border-radius: 5px; overflow: hidden; position: relative; }}
.xg-bar-fill {{
  height: 100%; border-radius: 5px; display: flex; align-items: center;
  padding-left: 8px; font-size: 0.65rem; font-weight: 700; color: #fff;
  transition: width 0.5s ease;
}}
.xg-bar-fill.home {{ background: linear-gradient(90deg, var(--blue-dim), var(--blue)); }}
.xg-bar-fill.away {{ background: linear-gradient(90deg, var(--red-dim), var(--red)); }}

/* ===== TICKET SECTION ===== */
.ticket-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.ticket-card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; transition: var(--transition);
}}
.ticket-card:hover {{ border-color: var(--border-light); }}
.ticket-card h4 {{
  font-size: 0.75rem; margin-bottom: 10px; text-transform: uppercase;
  letter-spacing: 0.8px; font-weight: 800;
}}
.ticket-card h4.stable {{ color: var(--green); }}
.ticket-card h4.value {{ color: var(--blue); }}
.ticket-card h4.lottery {{ color: var(--purple); }}
.ticket-item {{
  display: flex; justify-content: space-between; align-items: center;
  font-size: 0.72rem; padding: 6px 0;
  border-bottom: 1px solid rgba(38,51,68,0.5);
}}
.ticket-item:last-child {{ border-bottom: none; }}
.ticket-item .ti-match {{ color: var(--text-secondary); }}
.ticket-item .ti-odds {{ color: var(--text); font-weight: 600; font-family: monospace; }}
.tag-warn {{ background: rgba(255, 170, 60, 0.15); color: #ffaa3c; border: 1px solid rgba(255, 170, 60, 0.35); border-radius: 4px; padding: 0 4px; font-size: 11px; margin-left: 4px; }}
.ticket-empty {{ font-size: 0.72rem; color: var(--dim); font-style: italic; padding: 8px 0; }}
.ticket-summary {{
  margin-top: 12px; display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px;
}}
.ts-chip {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 10px 12px; text-align: center;
}}
.ts-chip .ts-label {{ font-size: 0.6rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; }}
.ts-chip .ts-val {{ font-size: 1rem; font-weight: 800; margin-top: 2px; }}

/* ===== SYSTEM PANEL ===== */
.sys-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.sys-card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; transition: var(--transition);
}}
.sys-card:hover {{ border-color: var(--border-light); }}
.sys-card h4 {{
  font-size: 0.68rem; color: var(--dim); margin-bottom: 10px;
  text-transform: uppercase; letter-spacing: 1px; font-weight: 700;
}}
.sys-row {{
  display: flex; justify-content: space-between; align-items: center;
  font-size: 0.72rem; padding: 4px 0;
}}
.sys-row .k {{ color: var(--dim); }}
.sys-row .v {{ font-weight: 600; }}
.hash {{
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  font-size: 0.62rem; color: var(--purple); word-break: break-all;
  margin-top: 8px; padding: 8px; background: var(--surface2);
  border-radius: 6px; border: 1px solid var(--border);
}}
.tier-indicator {{
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 4px; font-size: 0.68rem; font-weight: 700;
}}
.tier-indicator.safe {{ background: var(--green-dim); color: var(--green); }}
.tier-indicator.caution {{ background: var(--amber-dim); color: var(--amber); }}
.tier-indicator.danger {{ background: var(--red-dim); color: var(--red); }}

/* ===== RESULTS REVIEW ===== */
.results-summary {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px; margin-bottom: 14px;
}}
.results-table-wrap {{ overflow-x: auto; }}
.results-table {{
  width: 100%; border-collapse: collapse; font-size: 0.72rem;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); overflow: hidden;
}}
.results-table th {{
  text-align: left; padding: 8px 10px; color: var(--dim);
  font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border); background: var(--surface2);
}}
.results-table td {{
  padding: 8px 10px; border-bottom: 1px solid rgba(38,51,68,0.4);
  vertical-align: middle;
}}
.results-table tr:last-child td {{ border-bottom: none; }}
.results-table tr.hit td {{ background: rgba(34,197,94,0.04); }}
.results-table tr.miss td {{ background: rgba(239,68,68,0.04); }}
.result-icon {{
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 50%; font-size: 0.7rem; font-weight: 800;
}}
.result-icon.hit {{ background: var(--green-dim); color: var(--green); }}
.result-icon.miss {{ background: var(--red-dim); color: var(--red); }}

/* ===== FOOTER ===== */
.footer {{
  margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--border);
  font-size: 0.7rem; color: var(--dim); line-height: 1.8;
}}
.footer .chain {{
  font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.65rem;
  color: var(--text-secondary); background: var(--surface);
  padding: 10px 14px; border-radius: var(--radius-sm);
  border: 1px solid var(--border); margin-bottom: 10px;
  overflow-x: auto; white-space: nowrap;
}}
.footer a {{ color: var(--blue); text-decoration: none; }}
.footer a:hover {{ text-decoration: underline; }}
.footer .disclaimer {{ margin-top: 8px; color: var(--dim); font-style: italic; }}

/* ===== RESPONSIVE ===== */
@media (max-width: 900px) {{
  .stats {{ grid-template-columns: repeat(3, 1fr); }}
  .ticket-grid {{ grid-template-columns: 1fr; }}
  .sys-grid {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 600px) {{
  .page {{ padding: 16px 12px 36px; }}
  .stats {{ grid-template-columns: repeat(2, 1fr); }}
  .teams {{ gap: 8px; }}
  .team {{ font-size: 0.92rem; min-width: 65px; }}
  .header-left h1 {{ font-size: 1.3rem; }}
  .odds-grid {{ grid-template-columns: 1fr 1fr; }}
  .scores-grid {{ grid-template-columns: repeat(auto-fill, minmax(60px, 1fr)); }}
  .match-header {{ padding: 12px 14px; }}
  .tab-content {{ padding: 12px 14px; }}
}}
</style>
</head>
<body>
<div class="page">
  <!-- HEADER -->
  <div class="header">
    <div class="header-left">
      <h1>竞彩分析引擎</h1>
      <div class="sub">{today} &middot; DC+MC &rarr; Shin去水 &rarr; 逆向赔率 &rarr; 四源融合 &rarr; LGBM &rarr; Isotonic校准 &rarr; Wilson信任</div>
    </div>
    <div class="header-right">
      {health_badge}
    </div>
  </div>

  <!-- 日期导航 -->
  <div class="date-nav">
    {''.join(f'<a href="{d}.html" class="date-btn {"active" if d == today else ""}">{d[5:]}</a>' for d in (all_dates or [today]))}
  </div>

  <!-- KPI STATS -->
  <div class="stats">
    <div class="stat"><div class="label">场次</div><div class="value">{total}</div></div>
    <div class="stat"><div class="label">价值注</div><div class="value green">{len(value_bets)}</div></div>
    <div class="stat"><div class="label">平均置信</div><div class="value blue">{avg_conf:.0%}</div></div>
    <div class="stat"><div class="label">总投入</div><div class="value amber">&yen;{total_stake:.0f}</div></div>
    <div class="stat"><div class="label">预期回报</div><div class="value {'green' if exp_roi > 1 else 'red'}">{exp_roi:.2f}x</div></div>
    <div class="stat"><div class="label">熔断器</div><div class="value {'green' if tier == 0 else 'red'}">T{tier} &middot; x{breaker_mult:.1f}</div></div>
  </div>

  <!-- LEAGUE MATRIX -->
  {league_matrix_html}

  <!-- MATCH PREDICTIONS -->
  <div class="section-title">比赛预测</div>
  {cards if cards else '<p style="color:var(--dim);padding:48px;text-align:center;font-size:0.85rem;">等待每日流水线运行...</p>'}

  <!-- BETTING PLAN -->
  {ticket_html}

  <!-- PARLAY (2026-08-08) -->
  {parlay_html}

  <!-- EV VALUE REPORT -->
  {ev_html}

  <!-- LEAGUE LAYERS -->
  {league_html}
  {hcr_html}

  <!-- ACCURACY TREND -->
  {trend_html}

  <!-- SCORE HIT RATE (2026-08-05) -->
  {score_trend_html}
  {odds_series_html}

  <!-- RESULTS REVIEW -->
  {results_html}

  <!-- SYSTEM STATUS -->
  {system_html}

  <!-- FOOTER -->
  <div class="footer">
    <div class="chain">DC(60%) + MC-50K(40%) &rarr; Shin去水 &rarr; 逆向赔率 &rarr; 同赔历史 &rarr; 融合(模型60% + 市场25% + DJYY15%) &rarr; LGBM(10%) &rarr; Isotonic校准 &rarr; Wilson信任</div>
    <p>数据源: 体彩 / 新浪 / 500万 / DJYY &middot; 零服务器 GitHub Actions &middot; <a href="https://github.com/wlrwx/football-engine">源代码</a></p>
    <p class="disclaimer">仅供研究学习，不构成任何投注建议。模型输出为概率估计，不保证准确性。</p>
  </div>
</div>

<script>
// ===== MATCH CARD EXPAND/COLLAPSE =====
document.querySelectorAll('.match-header').forEach(function(header) {{
  header.addEventListener('click', function() {{
    var card = this.closest('.match');
    card.classList.toggle('expanded');
  }});
}});

// ===== TAB SWITCHING =====
document.querySelectorAll('.tab-btn').forEach(function(btn) {{
  btn.addEventListener('click', function(e) {{
    e.stopPropagation();
    var panel = this.closest('.match-detail');
    var tabId = this.getAttribute('data-tab');
    // Deactivate all tabs in this panel
    panel.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    panel.querySelectorAll('.tab-content').forEach(function(c) {{ c.classList.remove('active'); }});
    // Activate clicked
    this.classList.add('active');
    panel.querySelector('#' + tabId).classList.add('active');
  }});
}});

// ===== LEAGUE FILTER =====
document.querySelectorAll('.league-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var league = this.getAttribute('data-league');
    // Update active button
    document.querySelectorAll('.league-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    this.classList.add('active');
    // Show/hide sections
    document.querySelectorAll('.league-section').forEach(function(sec) {{
      if (league === 'all' || sec.getAttribute('data-league') === league) {{
        sec.classList.remove('hidden');
      }} else {{
        sec.classList.add('hidden');
      }}
    }});
  }});
}});
</script>
</body>
</html>"""


def _odds_movement_chip(p):
    """赔率变动信号标签"""
    sina = p.get("sina_odds")
    if not sina or not sina.get("movement"):
        return ""
    mv = sina["movement"]
    comp = sina.get("compression", {})
    # 生成箭头
    arrows = []
    colors = {"down": "↓", "up": "↑", "flat": "→"}
    for sel in ["home", "draw", "away"]:
        direction = mv.get(sel, "flat")
        cr = comp.get(sel, 1.0)
        if abs(cr - 1.0) > 0.03:  # 变化超过3%才显示
            arrows.append(f"{sel[0].upper()}{colors.get(direction,'→')}{cr:.2f}")
    if not arrows:
        return ""
    return f'<span class="info-chip" style="color:var(--purple)">赔率变动 {" ".join(arrows)}</span>'


def _odds_series_chip(p):
    """水位时间序列标签（2026-08-05 盘口系统修复）：展示累积快照与近期资金流"""
    sina = p.get("sina_odds")
    if not sina:
        return ""
    ser = sina.get("series") or {}
    pts = ser.get("points", 0)
    if pts < 2:
        return ""
    parts = [f"水位监控 {pts}次"]
    for side, key in (("主", "recent_home"), ("客", "recent_away")):
        v = ser.get(key)
        if v is not None and abs(v) >= 0.5:
            arrow = "↓" if v < 0 else "↑"
            parts.append(f"{side}{arrow}{abs(v):.1f}%")
    span = ser.get("span_min")
    if span:
        parts.append(f"跨{span:.0f}min")
    return f'<span class="info-chip" style="color:var(--teal, #14b8a6)">{" · ".join(parts)}</span>'


def _divergence_chip(p):
    """模型 vs 市场分歧信号：分歧大 = 模型独立判断（高信息量），分歧小 = 跟随市场"""
    model = p.get("model_raw") or {}
    market = p.get("market_fair")
    # 市场分歧候选（2026-08-05）：非模型方向但有显著正 EV 的赔率，被方向纪律拦下
    md = p.get("market_disagreement") or {}
    md_chips = []
    for sel, info in md.items():
        md_chips.append(f'{sel[0].upper()}+EV <b>{info["ev"]:.0%}</b>')
    md_html = ""
    if md_chips:
        md_html = f'<span class="info-chip" style="color:var(--amber)">市场分歧 {' '.join(md_chips)}（纪律拦下）</span>'
    if not model or not market or len(market) < 3:
        return md_html
    _probs = [model.get("home", 0), model.get("draw", 0), model.get("away", 0)]
    div = max(abs(_probs[i] - market[i]) for i in range(3))
    if div > 0.15:
        return md_html + f'<span class="info-chip" style="color:var(--red)">模型vs市场分歧 <b>{div:.0%}</b></span>'
    if div > 0.08:
        return md_html + f'<span class="info-chip" style="color:var(--amber)">分歧 <b>{div:.0%}</b></span>'
    return md_html
    return f'<span class="info-chip" style="color:var(--dim)">与市场一致 <b>{div:.0%}</b></span>'


def _pred_pick(p):
    """生成明确的预测结论"""
    ph = p.get("home_win_prob") or 0
    pd = p.get("draw_prob") or 0
    pa = p.get("away_win_prob") or 0
    if ph >= pd and ph >= pa:
        return f'<span class="pick-label">预测</span> <span class="pick-val home">主胜 {ph:.0%}</span>'
    elif pd >= ph and pd >= pa:
        return f'<span class="pick-label">预测</span> <span class="pick-val draw">平局 {pd:.0%}</span>'
    else:
        return f'<span class="pick-label">预测</span> <span class="pick-val away">客胜 {pa:.0%}</span>'


def _handicap_pick(p):
    """让球玩法预测结论（竞彩 hhad）"""
    hcap = p.get("handicap")
    if hcap is None:
        return ""
    hhp, hdp, hap = (p.get("handicap_home_prob") or 0, p.get("handicap_draw_prob") or 0, p.get("handicap_away_prob") or 0)
    if not (hhp or hdp or hap):
        return ""
    label = "主胜" if hhp >= hdp and hhp >= hap else ("平局" if hdp >= hhp and hdp >= hap else "客胜")
    prob = max(hhp, hdp, hap)
    hh_odds = p.get("handicap_home_odds") or p.get("handicap_draw_odds") or p.get("handicap_away_odds")
    edge = p.get("handicap_kelly_edge")
    cls = "home" if hhp >= hdp and hhp >= hap else ("draw" if hdp >= hhp and hdp >= hap else "away")
    edge_html = f' <span class="pick-val {cls}" style="font-size:.85em">让球EV {edge:+.0%}</span>' if edge is not None else ""
    sign = "+" if hcap > 0 else ""
    # 2026-08-06 UI 修复：让球盘明显化——独立标签+背景色+tooltip，与"预测（模型观点）"区分
    # 2026-08-06 v2：三方向 EV 全量展示——argmax 只显示概率最高方向，
    # 但让球平/让球主常有正 EV 藏在窄区间+高赔率里，逐个方向标出正 EV
    ev_html = ""
    _hev = p.get("handicap_ev") or {}
    _edges = _hev.get("edges") or {}
    if _edges:
        _ev_labels = {"home": "让球主", "draw": "让球平", "away": "让球客"}
        for _dir in ("home", "draw", "away"):
            _e = _edges.get(_dir)
            if _e is not None and _e > 0.005:  # 正 EV 才展示
                _ev_cls = "draw" if _dir == "draw" else _dir
                _star = " ⭐" if _dir == "draw" else ""  # 让球平正EV是隐藏价值，高亮
                # 2026-08-07：>30% edge 多为脏数据/赔率错位，sanity check 已设
                # recommended=False 不出注——页面需标注"超限"避免误读为强信号
                _over = " ⚠超限" if _e > 0.30 else ""
                ev_html += (f' <span class="pick-val {_ev_cls}" style="font-size:.78em;'
                            f'border-color:{"var(--purple)" if _dir=="draw" else {"home":"var(--green)","away":"var(--red)"}[_dir]};'
                            f'background:rgba(147,51,234,0.08)" '
                            f'title="模型概率×市场赔率-1。edge>30% 超出可信区间，系统不据此出注">'
                            f'{_ev_labels[_dir]} +EV {_e:+.0%}{_star}{_over}</span>')
    return (f'<span class="pick-label hcap-label" title="让球玩法（市场盘口观点）：主队让{hcap:g}球后的胜平负概率，与上方模型预测不同源">'
            f'让球盘 {sign}{hcap:g}</span> '
            f'<span class="pick-val {cls}" style="border-color:{ {"home":"var(--green)","draw":"var(--purple)","away":"var(--red)"}[cls] }">'
            f'{label} {prob:.0%}</span>{edge_html}{ev_html}')


def _pred_score(p):
    """预测比分（前5个最可能比分）"""
    # 2026-08-05: 3→5。账本 113 场实证：top3 命中 38% vs top5 52%，
    # 高进球场次(≥3球,占一半) top3 仅 16%——前3个被 1-0/1-1 占满，
    # DJYY 实际给了 8-10 个比分，主推 3 个浪费了能中的第4/5个。
    top_scores = p.get("top_scores")
    if not top_scores or not isinstance(top_scores, list) or len(top_scores) == 0:
        return ""
    scores = []
    for item in top_scores[:5]:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            scores.append(f"{item[0]}-{item[1]}")
    if scores:
        # 2026-08-06 UI 修复：明确"预测比分"（模型观点），避免与赛果混淆
        return f' <span class="pick-label" title="模型预测的最可能比分（非赛果）">预测比分</span> <span class="pred-score">{" / ".join(scores)}</span>'
    return ""


def _recent_cell(r):
    """近期窗口列：最近5场命中（2026-08-06）"""
    n = r.get("recent_n", 0)
    if not n:
        return '<td style="color:var(--dim)">—</td>'
    rate = r.get("recent_hit_rate", 0)
    color = "var(--green)" if rate >= 0.6 else ("var(--amber)" if rate >= 0.4 else "var(--red)")
    return (f'<td style="color:{color}">近{n}场 {r.get("recent_hits", 0)}中 ({rate*100:.0f}%)'
            f'<br><span style="font-size:0.62rem;color:var(--dim)">近ROI {r.get("recent_roi", 0)*100:+.0f}%</span></td>')


def _verdict_cell(r):
    """联赛判断列渲染（2026-08-06 增加回暖解禁态）"""
    v = r.get("verdict", "观望")
    if v == "价值区":
        return '<td style="color:var(--green);font-weight:700">✅ 价值区</td>'
    if v == "送钱区":
        return '<td style="color:var(--red);font-weight:700">🚫 送钱区·禁投</td>'
    if v == "回暖解禁":
        return ('<td style="color:#ffaa3c;font-weight:700" title="累计口径送钱区，但最近5场命中≥60%，已自动解除禁投">'
                '✅ 回暖解禁·观察</td>')
    if v == "谨慎":
        return '<td style="color:var(--amber)">⚠️ 谨慎</td>'
    return '<td style="color:var(--dim)">👀 观望</td>'


def _load_league_matrix(path):
    """加载 DJYY 联赛矩阵数据"""
    import json
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {"leagues": []}


def _league_matrix_section(league_matrix, predictions):
    """联赛矩阵面板：展示联赛统计数据，高亮当天有预测的联赛"""
    if not league_matrix or not league_matrix.get("leagues"):
        return ""

    leagues = league_matrix["leagues"]
    predicted_leagues = set()
    # 联赛名称映射（竞彩名称 → DJYY 矩阵名称）
    name_map = {
        "K1联赛": "韩K联", "韩K联": "韩K联",
        "巴甲": "巴西甲", "巴西甲": "巴西甲",
        "K联赛": "韩K联",
    }
    for p in predictions:
        comp = p.get("competition", "")
        if comp:
            predicted_leagues.add(comp)
            mapped = name_map.get(comp, comp)
            if mapped != comp:
                predicted_leagues.add(mapped)

    sorted_leagues = sorted(leagues, key=lambda x: -x.get("avg_goals", 0))

    rows = ""
    for lg in sorted_leagues:
        name = lg["name_zh"]
        is_active = name in predicted_leagues
        row_cls = "active" if is_active else ""
        cat = lg.get("category", "other")
        cat_label = {"tier1": "顶级", "tier2": "次级", "world": "全球", "other": "其他"}.get(cat, cat)
        cat_cls = cat

        rows += '<tr class="lm-row ' + row_cls + '">'
        rows += '<td><span class="lm-cat lm-cat-' + cat_cls + '">' + cat_label + '</span></td>'
        rows += '<td class="lm-name">' + name + '</td>'
        rows += '<td class="lm-num">' + str(lg.get("matches", 0)) + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_goals", 0):.1f}' + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_xg", 0):.2f}' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("btts_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("home_win_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("draw_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("away_win_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-pct">' + f'{lg.get("clean_sheet_pct", 0):.0f}%' + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_corners", 0):.1f}' + '</td>'
        rows += '<td class="lm-num">' + f'{lg.get("avg_yellow", 0):.1f}' + '</td>'
        rows += '</tr>'

    # 补充 DJYY 矩阵中缺失的联赛（杯赛/国际赛事等）
    matrix_names = {lg["name_zh"] for lg in sorted_leagues}
    missing_comps = set()
    for p in predictions:
        comp = p.get("competition", "")
        if comp and comp not in matrix_names and name_map.get(comp, comp) not in matrix_names:
            missing_comps.add(comp)

    if missing_comps:
        for comp in sorted(missing_comps):
            rows += '<tr class="lm-row active">'
            rows += '<td><span class="lm-cat lm-cat-cup">杯赛</span></td>'
            rows += '<td class="lm-name">' + comp + '</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-pct" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '<td class="lm-num" style="color:var(--dim)">—</td>'
            rows += '</tr>'

    gen_time = league_matrix.get("generated_at", "")[:10]
    active_count = sum(1 for lg in sorted_leagues if lg["name_zh"] in predicted_leagues)
    active_count += len(missing_comps)

    # 今日赛事提醒横幅
    alert_html = ""
    if active_count > 0:
        alert_html = (
            '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;margin:0 0 2px;'
            'background:linear-gradient(135deg,rgba(34,197,94,0.12),rgba(6,182,212,0.08));'
            'border:1px solid rgba(34,197,94,0.25);border-radius:var(--radius-sm);font-size:0.78rem">'
            '<span style="display:inline-flex;align-items:center;gap:4px;font-weight:800;color:var(--green)">'
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);'
            'animation:pulse 2s infinite"></span>'
            '今日赛事</span>'
            '<span style="color:var(--text-secondary)">' + str(active_count) + ' 个联赛有比赛</span>'
            '<span style="font-size:0.62rem;color:var(--dim);margin-left:auto">数据源: DJYY 每日更新</span>'
            '</div>'
        )

    return (
        '<div class="section-title" onclick="document.getElementById(\'league-matrix\').classList.toggle(\'collapsed\')" style="cursor:pointer">'
        + '联赛矩阵 &middot; ' + str(len(leagues) + len(missing_comps)) + ' 联赛/杯赛 &middot; ' + gen_time
        + ' <span style="font-size:0.65rem;color:var(--dim)">&#9654; 点击展开</span></div>'
        + alert_html
        + '<div id="league-matrix" class="collapsed"><div class="lm-wrap"><table class="lm-table">'
        + '<thead><tr><th>级别</th><th>联赛</th><th>场次</th><th>场均进球</th><th>场均xG</th>'
        + '<th>BTTS</th><th>主胜</th><th>平局</th><th>客胜</th><th>零封</th>'
        + '<th>角球</th><th>黄牌</th></tr></thead>'
        + '<tbody>' + rows + '</tbody></table></div>'
        + '<div style="padding:8px 12px;font-size:0.62rem;color:var(--dim);display:flex;gap:12px;flex-wrap:wrap">'
        + '<span>数据来源: <a href="https://djyylive.com" style="color:var(--blue)">DJYY</a></span>'
        + '<span>&#x25cf; <span style="color:var(--green)">高亮行</span> = 当天有预测的联赛</span>'
        + '<span>BTTS = 双方进球率</span>'
        + '<span>⚡ 数据更新于 DJYY 每日流水线</span></div></div>'
    )


def _league_header_enriched(lg_name, count, league_matrix):
    """生成带联赛统计的增强版联赛头部"""
    if not league_matrix or not league_matrix.get("leagues"):
        return '<div class="league-header">' + lg_name + ' <span class="league-count">' + str(count) + ' 场</span></div>'

    lg_data = None
    name_map = {"K1联赛": "韩K联", "巴甲": "巴西甲", "K联赛": "韩K联"}
    search_name = name_map.get(lg_name, lg_name)
    for lg in league_matrix["leagues"]:
        if lg["name_zh"] == search_name or lg["name_zh"] == lg_name or lg.get("short_zh", "") == lg_name:
            lg_data = lg
            break

    if not lg_data:
        return '<div class="league-header">' + lg_name + ' <span class="league-count">' + str(count) + ' 场</span></div>'

    goals = lg_data.get("avg_goals", 0)
    btts = lg_data.get("btts_pct", 0)
    home = lg_data.get("home_win_pct", 0)
    draw = lg_data.get("draw_pct", 0)
    away = lg_data.get("away_win_pct", 0)
    xg = lg_data.get("avg_xg", 0)
    matches = lg_data.get("matches", 0)

    return (
        '<div class="league-header">'
        + '<span>' + lg_name + '</span>'
        + '<span class="league-count">' + str(count) + ' 场 &middot; 赛季 ' + str(matches) + '场</span>'
        + '<div class="lg-stats-bar">'
        + '<span class="lg-stat" title="场均进球">&#x26BD; ' + f'{goals:.1f}' + '</span>'
        + '<span class="lg-stat" title="场均xG">xG ' + f'{xg:.2f}' + '</span>'
        + '<span class="lg-stat" title="双方进球率">BTTS ' + f'{btts:.0f}%' + '</span>'
        + '<span class="lg-stat h" title="主胜率">主 ' + f'{home:.0f}%' + '</span>'
        + '<span class="lg-stat d" title="平局率">平 ' + f'{draw:.0f}%' + '</span>'
        + '<span class="lg-stat a" title="客胜率">客 ' + f'{away:.0f}%' + '</span>'
        + '</div></div>'
    )



def _match_card(p, value_matches, idx, results_map=None):
    """Render a single match card with expandable detail tabs."""
    hp = p.get("home_win_prob", 0) * 100
    dp = p.get("draw_prob", 0) * 100
    ap = p.get("away_win_prob", 0) * 100
    conf = p.get("confidence", 0)
    conf_cls = "high" if conf > 0.6 else "med" if conf > 0.4 else "low"
    is_val = _is_value(p, value_matches)
    match_id = p.get("match_id", "")
    uid = f"m{idx}"

    # Basic info
    odds_h = p.get("home_odds") or 0
    odds_d = p.get("draw_odds") or 0
    odds_a = p.get("away_odds") or 0
    xg_h = p.get("home_xg", 0)
    xg_a = p.get("away_xg", 0)

    # 实际赛果对比
    result_html = ""
    if results_map:
        r = results_map.get(match_id)
        if not r:
            # 用场次号匹配（跨日期）
            fixture = _extract_fixture(match_id)
            if fixture:
                r = results_map.get(fixture)
            if not r:
                # fallback: 旧格式匹配
                fixture2 = match_id.split("_", 1)[-1] if "_" in match_id else match_id
                r = results_map.get(fixture2)
            if not r:
                # fallback: 队名匹配（最可靠）
                home_team = p.get("home_team", "")
                away_team = p.get("away_team", "")
                if home_team and away_team:
                    team_key = f"{home_team}_vs_{away_team}"
                    r = results_map.get(team_key)
        if r and r.get("home_score") is not None:
            hs, as_ = r["home_score"], r["away_score"]
            if hs > as_:
                actual_label, actual_cls = "主胜", "home"
            elif hs == as_:
                actual_label, actual_cls = "平局", "draw"
            else:
                actual_label, actual_cls = "客胜", "away"
            # 判断命中
            ph = p.get("home_win_prob") or 0
            pd_ = p.get("draw_prob") or 0
            pa = p.get("away_win_prob") or 0
            if ph >= pd_ and ph >= pa:
                pred_outcome = "home"
            elif pd_ >= ph and pd_ >= pa:
                pred_outcome = "draw"
            else:
                pred_outcome = "away"
            hit = pred_outcome == ("home" if hs > as_ else "draw" if hs == as_ else "away")
            hit_icon = "✓" if hit else "✗"
            hit_cls = "hit" if hit else "miss"
            # 比分命中闭环展示（2026-08-05）：
            #   预测比分主推5个 + 实际比分 + 命中位置徽标（🎯1=top1命中 ... 🎯5=主推前5命中, 未进=✗）
            #   让"比分预测准但主推前三不靠谱"直观可见、可核查
            score_hit_html = ""
            score_rank_html = ""
            top_scores = p.get("top_scores") or []
            _rank = 0
            for _i, item in enumerate(top_scores):
                if isinstance(item, (list, tuple)) and len(item) >= 2 and int(item[0]) == hs and int(item[1]) == as_:
                    _rank = _i + 1
                    break
            if _rank > 0:
                _badge_cls = "hit" if _rank <= 5 else ""
                score_rank_html = f' <span class="ar-hit {_badge_cls}" title="比分命中候选第{_rank}位">🎯{_rank}</span>'
            else:
                score_rank_html = ' <span class="ar-hit miss" title="比分未进候选列表">比分✗</span>'
            # 预测比分主推（前5个）→ 实际比分对照，可核查
            _pred_scores = []
            for item in top_scores[:5]:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    _pred_scores.append(f"{item[0]}-{item[1]}")
            if _pred_scores:
                # 标注比分来源（8/5 起双源融合：DJYY+MC / DJYY / MC）
                _src_tag = p.get("score_sources") or ""
                _src_html = f' <span style="font-size:0.6rem;color:var(--purple)">[{_src_tag}]</span>' if _src_tag else ""
                score_hit_html = f' <span class="pred-score" style="font-size:0.66rem;color:var(--dim)">预测 {" / ".join(_pred_scores)}{_src_html}</span>'
            result_html = f'<div class="actual-result"><span class="ar-label">实际</span> <span class="ar-score">{hs}-{as_}</span> <span class="ar-outcome {actual_cls}">{actual_label}</span> <span class="ar-hit {hit_cls}">{hit_icon}</span>{score_rank_html}{score_hit_html}</div>'

    # Build detail tabs
    model_tab = _tab_model(p, uid)
    odds_tab = _tab_odds(p, uid)
    dist_tab = _tab_distribution(p, uid)

    return f"""
  <div class="match {'value-pick' if is_val else ''}">
    <div class="match-header">
      <div class="match-top">
        <span class="league-tag">{p.get('competition', '')}</span>
        <div class="match-meta">
          {'<span class="value-badge">价值精选</span>' if is_val else ''}
          {'<span class="draw-alert-badge" style="background:var(--purple);color:#fff;padding:2px 6px;border-radius:4px;font-size:0.65rem;margin-right:4px">⚠平局预警</span>' if p.get('draw_alert') else ''}
          {'<span style="background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.4);padding:2px 6px;border-radius:4px;font-size:0.65rem;margin-right:4px" title="送钱区联赛（累计ROI<-5%），此场不出注">🚫禁投联赛</span>' if p.get('league_forbidden') else ''}
          {'<span style="background:rgba(255,170,60,0.15);color:#ffaa3c;border:1px solid rgba(255,170,60,0.4);padding:2px 6px;border-radius:4px;font-size:0.65rem;margin-right:4px" title="该联赛累计送钱但最近5场命中≥60%，已自动解禁">✅回暖解禁</span>' if p.get('league_recovered') else ''}
          {'<span class="hcr-warn-badge" title="该联赛高置信反向样本≥2场，60%+段已降档" style="background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.4);padding:2px 6px;border-radius:4px;font-size:0.65rem;margin-right:4px">⚠高置信反向风险</span>' if p.get('prob_band_60_risk') else ''}
          {'<span class="hcr-warn-badge" title="50-60%概率段=平局盲点区，已降档" style="background:rgba(255,170,60,0.15);color:#ffaa3c;border:1px solid rgba(255,170,60,0.4);padding:2px 6px;border-radius:4px;font-size:0.65rem;margin-right:4px">⚠平局盲点段</span>' if p.get('prob_band_5060') else ''}
          {'<span class="fresh-warn" style="background:rgba(255,170,60,0.12);color:#ffaa3c;padding:2px 6px;border-radius:4px;font-size:0.65rem;margin-right:4px" title="数据新鲜度风险">⚠新鲜度</span>' if (p.get('freshness') or {}).get('risk') in ('watch','alert') else ''}
          <span class="match-id">{match_id.split('_', 1)[-1] if '_' in match_id else match_id}</span>
          {f'<span class="match-id" style="color:var(--amber)">{p.get("kickoff","")}</span>' if p.get('kickoff') else ''}
          <span class="expand-icon">详情 &#9660;</span>
        </div>
      </div>
      <div class="teams">
        <span class="team home">{p.get('home_team', '')}</span>
        <span class="vs">VS</span>
        <span class="team away">{p.get('away_team', '')}</span>
      </div>
      <div class="prob-row">
        <div class="prob-seg h" style="width:{hp:.1f}%">H {hp:.0f}%</div>
        <div class="prob-seg d" style="width:{dp:.1f}%">D {dp:.0f}%</div>
        <div class="prob-seg a" style="width:{ap:.1f}%">A {ap:.0f}%</div>
      </div>
      <div class="pred-pick">{_pred_pick(p)}{_pred_score(p)}{_handicap_pick(p)}</div>
      {result_html}
      {'<div class="draw-alert-info" style="background:rgba(147,51,234,0.1);border:1px solid var(--purple);border-radius:6px;padding:8px 12px;margin:8px 0;font-size:0.72rem"><b>⚠ 平局预警</b> — ' + ('冷门平局：一方被看好但平局风险偏高' if p.get('draw_alert') == 'cold_draw' else '均势平局：双方接近，平局被低估') + '</div>' if p.get('draw_alert') else ''}
      <div class="match-info-row">
        <span class="conf-meter"><span class="conf-dot {conf_cls}"></span><b>{conf:.0%}</b></span>
        <span class="info-chip">xG <b>{xg_h:.2f} - {xg_a:.2f}</b></span>
        <span class="info-chip">Odds <b>{odds_h}/{odds_d}/{odds_a}</b></span>
        {_odds_movement_chip(p)}
        {_odds_series_chip(p)}
        {_divergence_chip(p)}
      </div>
    </div>
    <div class="match-detail">
      <div class="tab-bar">
        <button class="tab-btn active" data-tab="{uid}-model">模型</button>
        <button class="tab-btn" data-tab="{uid}-odds">赔率</button>
        <button class="tab-btn" data-tab="{uid}-dist">分布</button>
      </div>
      <div class="tab-content active" id="{uid}-model">{model_tab}</div>
      <div class="tab-content" id="{uid}-odds">{odds_tab}</div>
      <div class="tab-content" id="{uid}-dist">{dist_tab}</div>
    </div>
  </div>"""


def _tab_model(p, uid):
    """模型 tab: model_raw vs market_fair vs djyy vs final, Elo, Wilson, + DJYY xG/ injuries."""
    model_raw = p.get("model_raw") or {}
    market_fair = p.get("market_fair")
    djyy = p.get("djyy_model_prob")
    final_h = p.get("home_win_prob") or 0
    final_d = p.get("draw_prob") or 0
    final_a = p.get("away_win_prob") or 0
    elo_home = p.get("elo_home")
    elo_away = p.get("elo_away")
    wilson = p.get("wilson_trust") or 0
    
    # ===== DJYY 增强数据 =====
    # xG 预期进球
    xg_home = 0
    xg_away = 0
    xg_html = ""
    djyy_xg = p.get("djyy_xg") or {}
    if djyy_xg:
        xg_home = float(djyy_xg.get("home_avg") or 0)
        xg_away = float(djyy_xg.get("away_avg") or 0)
        max_xg = max(2.0, xg_home, xg_away)
        h_pct = (xg_home / max_xg) * 100 if max_xg > 0 else 0
        a_pct = (xg_away / max_xg) * 100 if max_xg > 0 else 0
        xg_html = f"""
      <div class="xg-compare">
        <h5>xG 预期进球 (近5场平均)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">主队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill home" style="width:{h_pct}%">{xg_home:.2f}</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">客队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill away" style="width:{a_pct}%">{xg_away:.2f}</div></div>
        </div>
      </div>"""
    
    # 赛程密度/休息天数
    rest_html = ""
    rest_days = p.get("rest_days") or {}
    if rest_days:
        rh = rest_days.get("home") or 7
        ra = rest_days.get("away") or 7
        # 颜色: <=2天=红色(疲劳), 3-4天=橙色, >=5天=绿色(充足)
        h_color = "var(--red)" if rh <= 2 else "var(--amber)" if rh <= 4 else "var(--green)"
        a_color = "var(--red)" if ra <= 2 else "var(--amber)" if ra <= 4 else "var(--green)"
        rest_html = f"""
      <div class="xg-compare">
        <h5>赛程密度 (距上场天数)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">主队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill" style="width:{min(rh/7*100, 100)}%; background:{h_color}">{rh}天</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">客队</span>
          <div class="xg-bar-track"><div class="xg-bar-fill" style="width:{min(ra/7*100, 100)}%; background:{a_color}">{ra}天</div></div>
        </div>
      </div>"""
    
    # 伤停预警
    inj_html = ""
    injuries = p.get("injuries") or {}
    if injuries:
        h_cnt = injuries.get("home_count", 0)
        h_att = injuries.get("home_attackers", 0)
        a_cnt = injuries.get("away_count", 0)
        a_att = injuries.get("away_attackers", 0)
        # 前锋/中场缺阵 = 高亮预警
        h_warn = "⚠️" if h_att >= 1 else ""
        a_warn = "⚠️" if a_att >= 1 else ""
        inj_html = f"""
      <div class="reverse-box">
        <h5>伤停预警</h5>
        <div class="reverse-row"><span class="rk">主队</span><span>伤停{h_cnt}人{h_warn} (含攻击线{h_att}人)</span></div>
        <div class="reverse-row"><span class="rk">客队</span><span>伤停{a_cnt}人{a_warn} (含攻击线{a_att}人)</span></div>
      </div>"""

    rows = ""
    # Model Raw (DC+MC)
    mr_h = model_raw.get("home", 0) * 100 if model_raw else 0
    mr_d = model_raw.get("draw", 0) * 100 if model_raw else 0
    mr_a = model_raw.get("away", 0) * 100 if model_raw else 0
    rows += _model_row("DC+MC 原始", mr_h, mr_d, mr_a)

    # Market Fair (Shin)
    if market_fair and len(market_fair) >= 3:
        mf_h = market_fair[0] * 100
        mf_d = market_fair[1] * 100
        mf_a = market_fair[2] * 100
        rows += _model_row("Shin公平", mf_h, mf_d, mf_a)
    else:
        rows += _model_row_empty("Shin公平")

    # DJYY
    if djyy and djyy.get("home"):
        dj_h = djyy.get("home", 0) * 100
        dj_d = djyy.get("draw", 0) * 100
        dj_a = djyy.get("away", 0) * 100
        rows += _model_row("DJYY模型", dj_h, dj_d, dj_a)
    else:
        rows += _model_row_empty("DJYY模型")

    # Final Fused
    rows += _model_row("最终融合", final_h * 100, final_d * 100, final_a * 100)

    # Elo section
    elo_html = ""
    if elo_home is not None and elo_away is not None:
        elo_diff = (elo_home or 0) - (elo_away or 0)
        elo_html = f"""
      <div class="elo-row">
        <div class="elo-chip"><div class="elo-label">主队Elo</div><div class="elo-val" style="color:var(--blue)">{elo_home:.0f}</div></div>
        <div class="elo-chip"><div class="elo-label">客队Elo</div><div class="elo-val" style="color:var(--red)">{elo_away:.0f}</div></div>
        <div class="elo-chip"><div class="elo-label">Elo差值</div><div class="elo-val" style="color:{'var(--green)' if elo_diff > 0 else 'var(--red)'}">{elo_diff:+.0f}</div></div>
      </div>"""

    # Wilson trust
    wilson_pct = (wilson or 0) * 100
    wilson_html = f"""
      <div class="wilson-meter">
        <div class="wilson-label">Wilson信任分</div>
        <div class="wilson-track"><div class="wilson-fill" style="width:{wilson_pct:.0f}%"></div></div>
        <div class="wilson-val">{wilson_pct:.1f}% 置信权重</div>
      </div>"""

    return f"""
      <table class="model-table">
        <tr><th>信号源</th><th class="prob-bar-cell">主 / 平 / 客 概率分布</th></tr>
        {rows}
      </table>
      {xg_html}
      {rest_html}
      {inj_html}
      {elo_html}
      {wilson_html}"""


def _model_row(label, h, d, a):
    return f"""
        <tr>
          <td class="src-label">{label}</td>
          <td class="prob-bar-cell">
            <div class="mini-bar-wrap">
              <div class="mini-bar h" style="width:{h:.1f}%">{h:.0f}</div>
              <div class="mini-bar d" style="width:{d:.1f}%">{d:.0f}</div>
              <div class="mini-bar a" style="width:{a:.1f}%">{a:.0f}</div>
            </div>
          </td>
        </tr>"""


def _model_row_empty(label):
    return f"""
        <tr>
          <td class="src-label">{label}</td>
          <td class="prob-bar-cell"><span style="font-size:0.65rem;color:var(--dim);">暂无</span></td>
        </tr>"""


def _tab_odds(p, uid):
    """赔率 tab: market odds, shin fair, implied probs, edge, reverse, same-odds, + 四庄家对比."""
    odds_h = p.get("home_odds") or 0
    odds_d = p.get("draw_odds") or 0
    odds_a = p.get("away_odds") or 0
    handicap = p.get("handicap", "")
    market_fair = p.get("market_fair")
    final_h = p.get("home_win_prob", 0)
    final_d = p.get("draw_prob", 0)
    final_a = p.get("away_win_prob", 0)
    
    # 四庄家赔率对比
    is_synthetic = p.get("odds_synthetic", False)
    src_label = "模型合成" if is_synthetic else "体彩官方"
    # 1. 体彩官方 / 模型合成
    sporttery_odds = p.get("sporttery_odds") or {}
    st_h = sporttery_odds.get("home") or odds_h
    st_d = sporttery_odds.get("draw") or odds_d
    st_a = sporttery_odds.get("away") or odds_a
    
    # 2. Bet365
    bet365_odds = p.get("bet365_odds") or {}
    b365_h = bet365_odds.get("home") or odds_h
    b365_d = bet365_odds.get("draw") or odds_d
    b365_a = bet365_odds.get("away") or odds_a
    
    # 3. Pinnacle
    pinn_odds = p.get("pinnacle_odds") or {}
    pinn_h = pinn_odds.get("home") or odds_h
    pinn_d = pinn_odds.get("draw") or odds_d
    pinn_a = pinn_odds.get("away") or odds_a
    
    # 4. DJYY 模型赔率（反向计算）
    djyy_odds = p.get("djyy_model_prob") or {}
    djyy_h = 1 / djyy_odds.get("home") if djyy_odds.get("home") and djyy_odds.get("home") > 0 else None
    djyy_d = 1 / djyy_odds.get("draw") if djyy_odds.get("draw") and djyy_odds.get("draw") > 0 else None
    djyy_a = 1 / djyy_odds.get("away") if djyy_odds.get("away") and djyy_odds.get("away") > 0 else None
    
    # 赔率对比表格
    def _o(v):
        return f"{v:.2f}" if v and v > 0 else "-"
    
    bookies_html = f"""
      <table class="edge-table" style="margin-top: 16px;">
        <tr><th>庄家</th><th>主胜</th><th>平局</th><th>客胜</th></tr>
        <tr><td class="src-label">{src_label}</td><td>{_o(st_h)}</td><td>{_o(st_d)}</td><td>{_o(st_a)}</td></tr>
        <tr><td class="src-label">Bet365</td><td>{_o(b365_h)}</td><td>{_o(b365_d)}</td><td>{_o(b365_a)}</td></tr>
        <tr><td class="src-label">Pinnacle</td><td>{_o(pinn_h)}</td><td>{_o(pinn_d)}</td><td>{_o(pinn_a)}</td></tr>
        <tr><td class="src-label" style="color:var(--purple);">DJYY模型</td><td>{_o(djyy_h)}</td><td>{_o(djyy_d)}</td><td>{_o(djyy_a)}</td></tr>
      </table>"""

    # Implied probs from raw odds
    imp_h = (1 / odds_h * 100) if odds_h else 0
    imp_d = (1 / odds_d * 100) if odds_d else 0
    imp_a = (1 / odds_a * 100) if odds_a else 0

    # Shin fair probs
    sf_h = market_fair[0] * 100 if market_fair and len(market_fair) >= 3 else 0
    sf_d = market_fair[1] * 100 if market_fair and len(market_fair) >= 3 else 0
    sf_a = market_fair[2] * 100 if market_fair and len(market_fair) >= 3 else 0

    # Edge = model - implied
    edge_h = final_h * 100 - imp_h
    edge_d = final_d * 100 - imp_d
    edge_a = final_a * 100 - imp_a

    def _edge_cls(v):
        return "edge-pos" if v > 2 else "edge-neg" if v < -2 else ""

    # Reverse odds analysis
    upset = p.get("reverse_upset_risk") or 0
    direction = p.get("reverse_direction") or ""
    compression = p.get("reverse_compression") or 0

    # Same odds
    same_matched = p.get("same_odds_matched") or 0
    same_conf = p.get("same_odds_confidence") or 0
    same_bias = p.get("same_odds_bias") or ""
    combo_boost = p.get("combo_boost") or 0

    return f"""
      <div class="odds-grid">
        <div class="odds-box"><div class="ob-label">主胜</div><div class="ob-val">{odds_h:.2f}</div></div>
        <div class="odds-box"><div class="ob-label">平局</div><div class="ob-val">{odds_d:.2f}</div></div>
        <div class="odds-box"><div class="ob-label">客胜</div><div class="ob-val">{odds_a:.2f}</div></div>
        <div class="odds-box"><div class="ob-label">让球</div><div class="ob-val">{handicap if handicap else '暂无'}</div></div>
      </div>
      <h5 style="margin: 16px 0 8px 0; color: var(--text-secondary); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px;">四庄家赔率对比</h5>
      {bookies_html}
      <table class="edge-table">
        <tr><th>结果</th><th>隐含概率</th><th>Shin公平</th><th>模型</th><th>边际</th></tr>
        <tr>
          <td>主胜</td><td>{imp_h:.1f}%</td><td>{sf_h:.1f}%</td><td>{final_h*100:.1f}%</td>
          <td class="{_edge_cls(edge_h)}">{edge_h:+.1f}%</td>
        </tr>
        <tr>
          <td>平局</td><td>{imp_d:.1f}%</td><td>{sf_d:.1f}%</td><td>{final_d*100:.1f}%</td>
          <td class="{_edge_cls(edge_d)}">{edge_d:+.1f}%</td>
        </tr>
        <tr>
          <td>客胜</td><td>{imp_a:.1f}%</td><td>{sf_a:.1f}%</td><td>{final_a*100:.1f}%</td>
          <td class="{_edge_cls(edge_a)}">{edge_a:+.1f}%</td>
        </tr>
      </table>
      <div class="reverse-box">
        <h5>逆向赔率分析</h5>
        <div class="reverse-row"><span class="rk">冷门风险</span><span style="color:{'var(--red)' if upset > 40 else 'var(--text)'}; font-weight:700;">{upset:.0f}%</span></div>
        <div class="reverse-row"><span class="rk">方向</span><span>{direction if direction else '暂无'}</span></div>
        <div class="reverse-row"><span class="rk">压缩比</span><span>{compression:.2f}</span></div>
      </div>
      <div class="same-odds-box">
        <h5>同赔历史</h5>
        <div class="reverse-row"><span class="rk">匹配场次</span><span style="font-weight:700;">{same_matched}</span></div>
        <div class="reverse-row"><span class="rk">历史置信</span><span>{same_conf:.0%}</span></div>
        <div class="reverse-row"><span class="rk">偏差</span><span>{same_bias if same_bias else '中性'}</span></div>
        <div class="reverse-row"><span class="rk">组合加成</span><span style="color:{'var(--green)' if combo_boost > 0 else 'var(--dim)'}">{combo_boost:+.2f}</span></div>
      </div>"""


def _tab_distribution(p, uid):
    """分布 tab: top_scores grid, total_goals bars, xG comparison."""
    top_scores = p.get("top_scores")
    total_goals = p.get("total_goals")
    xg_h = p.get("home_xg", 0)
    xg_a = p.get("away_xg", 0)

    # Top scores grid
    scores_html = ""
    if top_scores and isinstance(top_scores, list) and len(top_scores) > 0:
        cells = ""
        for item in top_scores[:12]:
            if isinstance(item, dict):
                score = item.get("score", "")
                prob = item.get("prob", 0)
                cells += f'<div class="score-cell"><div class="sc-score">{score}</div><div class="sc-prob">{prob*100:.1f}%</div></div>'
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                cells += f'<div class="score-cell"><div class="sc-score">{item[0]}-{item[1]}</div><div class="sc-prob">{item[2]*100:.1f}%</div></div>'
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                cells += f'<div class="score-cell"><div class="sc-score">{item[0]}</div><div class="sc-prob">{item[1]*100:.1f}%</div></div>'
        if cells:
            scores_html = f'<div style="margin-bottom:6px;font-size:0.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:0.5px;">最可能比分</div><div class="scores-grid">{cells}</div>'
    else:
        scores_html = '<div style="font-size:0.72rem;color:var(--dim);font-style:italic;margin-bottom:12px;">比分分布暂无数据</div>'

    # Total goals bars
    goals_html = ""
    # 兼容 dict 格式（旧数据：{'1.5': [under_prob, over_prob]})
    if isinstance(total_goals, dict):
        total_goals = [[int(float(k)), v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else (v if isinstance(v, (int, float)) else 0)]
                       for k, v in total_goals.items()]
    if total_goals and isinstance(total_goals, list) and len(total_goals) > 0:
        max_prob = max((item.get("prob", 0) if isinstance(item, dict) else (item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else 0) for item in total_goals), default=1) or 1
        bars = ""
        for item in total_goals:
            if isinstance(item, dict):
                label = item.get("goals", item.get("label", ""))
                prob = item.get("prob", 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                label = item[0]
                prob = item[1]
            else:
                continue
            pct = (prob / max_prob) * 100
            bars += f"""
          <div class="goal-bar-row">
            <span class="goal-bar-label">{label}</span>
            <div class="goal-bar-track"><div class="goal-bar-fill" style="width:{pct:.0f}%">{prob*100:.1f}%</div></div>
          </div>"""
        goals_html = f'<div style="margin:14px 0 6px;font-size:0.68rem;color:var(--dim);text-transform:uppercase;letter-spacing:0.5px;">总进球分布</div><div class="goals-bars">{bars}</div>'
    else:
        goals_html = '<div style="font-size:0.72rem;color:var(--dim);font-style:italic;margin:12px 0;">总进球分布暂无数据</div>'

    # xG comparison
    max_xg = max(xg_h, xg_a, 0.1)
    xg_h_pct = (xg_h / (max_xg * 1.3)) * 100
    xg_a_pct = (xg_a / (max_xg * 1.3)) * 100
    xg_html = f"""
      <div class="xg-compare">
        <h5>预期进球 (xG)</h5>
        <div class="xg-bar-row">
          <span class="xg-bar-label">{p.get('home_team', 'Home')[:8]}</span>
          <div class="xg-bar-track"><div class="xg-bar-fill home" style="width:{xg_h_pct:.0f}%">{xg_h:.2f}</div></div>
        </div>
        <div class="xg-bar-row">
          <span class="xg-bar-label">{p.get('away_team', 'Away')[:8]}</span>
          <div class="xg-bar-track"><div class="xg-bar-fill away" style="width:{xg_a_pct:.0f}%">{xg_a:.2f}</div></div>
        </div>
      </div>"""

    return f"{scores_html}{goals_html}{xg_html}"


def _ticket_section(ticket, predictions):
    if not ticket:
        return ""
    stable = ticket.get("stable", [])
    value = ticket.get("value", [])
    lottery = ticket.get("lottery", [])
    total_stake = ticket.get("total_stake", 0)
    exp_roi = ticket.get("expected_roi", 0)
    bankroll = ticket.get("bankroll", 0)
    breaker_mult = ticket.get("breaker_multiplier", 1.0)

    def _items(items):
        if not items:
            return '<div class="ticket-empty">暂无选择</div>'
        html = ""
        _hafu_name = {"HH": "胜胜", "HD": "胜平", "HA": "胜负", "DH": "平胜", "DD": "平平", "DA": "平负", "AH": "负胜", "AD": "负平", "AA": "负负"}
        for it in items:
            match_id = it.get("match", "")
            _play = ""
            if "#" in match_id:
                match_id, _play = match_id.split("#", 1)
            teams = match_id.split("_", 1)[-1] if "_" in match_id else match_id
            for p in predictions:
                if p.get("match_id") == match_id:
                    teams = f'{p["home_team"]} vs {p["away_team"]}'
                    break
            sel_map = {"home": "主胜", "draw": "平局", "away": "客胜"}
            sel = it.get("sel", "")
            if sel.startswith("hcap_"):
                sel_label = "让球·" + sel_map.get(sel[5:], sel[5:])
            elif sel.startswith("ttg_"):
                sel_label = "总进球" + (sel[4:] + "+" if int(sel[4:]) >= 7 else sel[4:]) + "球"
            elif sel.startswith("crs_"):
                _parts = sel[4:].split("_")
                sel_label = f"比分{_parts[0]}:{_parts[1]}" if len(_parts) == 2 else sel
            elif sel.startswith("hafu_"):
                sel_label = "半全场·" + _hafu_name.get(sel[5:], sel[5:])
            else:
                sel_label = sel_map.get(sel, sel)
            # 降档角标（2026-08-06）：50-60% 概率段 / E 规则高置信反向风险联赛
            _downgrade = ""
            _reason = it.get("downgrade_reason", "")
            if _reason == "prob_5060":
                _downgrade = ' <span class="tag-warn" title="50-60% 概率段（平局盲点区）已降档">降档·平局盲点</span>'
            elif _reason == "league_60_risk":
                _downgrade = ' <span class="tag-warn" title="联赛高置信反向样本≥2场，60%+段降一档">降档·联赛风险</span>'
            elif it.get("downgraded") == "stake_half":
                _downgrade = ' <span class="tag-warn" title="50-60% 概率段（平局盲点区）减注50%">减注</span>'
            elif it.get("prob") and 0.50 <= it.get("prob", 0) < 0.60:
                _downgrade = ' <span class="tag-warn" title="50-60% 概率段（平局盲点区）已降档">降档</span>'
            html += f'<div class="ticket-item"><span class="ti-match">{teams} [{sel_label}]{_downgrade}</span><span class="ti-odds">@{it.get("odds", 0):.2f} / &yen;{it.get("stake", 0):.0f}</span></div>'
        return html

    return f"""
  <div class="section-title">投注方案（三票制 60/30/10）</div>
  <div class="ticket-grid">
    <div class="ticket-card"><h4 class="stable">稳胆（60%）</h4>{_items(stable)}</div>
    <div class="ticket-card"><h4 class="value">搏冷（30%）</h4>{_items(value)}</div>
    <div class="ticket-card"><h4 class="lottery">彩票（10%）</h4>{_items(lottery)}</div>
  </div>
  <div class="ticket-summary">
    <div class="ts-chip"><div class="ts-label">总投入</div><div class="ts-val" style="color:var(--amber)">&yen;{total_stake:.0f}</div></div>
    <div class="ts-chip"><div class="ts-label">预期回报</div><div class="ts-val" style="color:{'var(--green)' if exp_roi > 1 else 'var(--red)'}">{exp_roi:.2f}x</div></div>
    <div class="ts-chip"><div class="ts-label">资金池</div><div class="ts-val">&yen;{bankroll:.0f}</div></div>
    <div class="ts-chip"><div class="ts-label">熔断系数</div><div class="ts-val" style="color:{'var(--green)' if breaker_mult >= 1 else 'var(--red)'}">x{breaker_mult:.2f}</div></div>
  </div>"""


def _parlay_section(ticket, predictions):
    """串关方案（2026-08-08 新增）— 竞彩实际玩法。

    数学纪律：串关吃双重抽水，模型概率高估（账本校准 0.55-0.60 段命中率仅 31.6%）。
    串票 EV 用账本校准命中率计算，只推荐 cal_ev>0 的；负 EV 串票 ⚠ 展示不出注；
    无腿/全负 → 空仓并展示校准表（为什么不该串）。
    """
    if not ticket:
        return ""
    parlay = ticket.get("parlay", [])
    cal = ticket.get("parlay_calibration", {})
    if not parlay and not cal:
        return ""

    sel_map = {"home": "主胜", "draw": "平局", "away": "客胜"}

    def _legs_html(t):
        html = ""
        for lg in t.get("legs", []):
            sel = lg.get("sel", "")
            html += (f'<div class="ticket-item"><span class="ti-match">{lg.get("home", "")} vs '
                     f'{lg.get("away", "")} <span style="opacity:.6">[{lg.get("league", "")}]</span> '
                     f'<span class="pick-val {"home" if sel=="home" else ("draw" if sel=="draw" else "away")}" '
                     f'style="font-size:.8em">{sel_map.get(sel, sel)} {lg.get("prob", 0)*100:.0f}%</span></span>'
                     f'<span class="ti-odds">@{lg.get("odds", 0):.2f}</span></div>')
        return html

    def _ticket_html(t):
        rec = t.get("recommended", False)
        ptype = t.get("type", "")
        badge = ('<span class="pick-val home" style="font-size:.75em">⭐ 推荐</span>' if rec else
                 '<span class="pick-val draw" style="font-size:.75em" title="校准后负EV，系统不据此出注">⚠ 负EV</span>')
        ev = t.get("cal_ev", 0)
        roi = t.get("cal_roi", 0)
        ev_html = (f'<span class="ts-chip"><div class="ts-label">校准EV</div>'
                   f'<div class="ts-val" style="color:{("var(--green)" if ev > 0 else "var(--red)")}">'
                   f'{"+" if ev > 0 else ""}{ev:.1f}元 ({roi:+.0%})</div></span>')
        return f"""
    <div class="ticket-card" style="{'border-color:var(--green)' if rec else 'border-color:var(--red);opacity:.75'}">
      <h4 class="{'stable' if rec else 'lottery'}" style="display:flex;justify-content:space-between;align-items:center">
        <span>{ptype} {badge}</span>
        <span style="font-size:.7rem;opacity:.7">全中@{t.get('total_odds', 0):.2f} · 校准命中率 {t.get('hit_prob_cal', 0)*100:.0f}%</span>
      </h4>
      {_legs_html(t)}
      <div class="ticket-summary" style="margin-top:6px">
        <div class="ts-chip"><div class="ts-label">投入</div><div class="ts-val">&yen;{t.get('stake', 0):.0f}</div></div>
        {ev_html}
        <div class="ts-chip"><div class="ts-label">最高奖金</div><div class="ts-val" style="color:var(--amber)">&yen;{t.get('potential', 0):.0f}</div></div>
        {f'<div class="ts-chip"><div class="ts-label">容错</div><div class="ts-val" style="color:var(--purple);font-size:.72rem">{t.get("worst_win", 0):.0f}元</div></div>' if t.get("worst_win") else ''}
      </div>
      <div style="font-size:.7rem;opacity:.65;margin-top:4px">{t.get('note', '')}</div>
    </div>"""

    cal_html = ""
    if cal:
        table = cal.get("table", {})
        overall = cal.get("overall", 0.433)
        n = cal.get("n", 0)
        rows = "".join(
            f"<tr><td>≥{float(k):.2f}</td><td>{v*100:.0f}%</td></tr>"
            for k, v in sorted(table.items(), key=lambda kv: float(kv[0]))
        )
        cal_html = f"""
  <details style="margin-top:8px;font-size:.75rem;color:var(--text-secondary)">
    <summary>串关为什么难赚钱？账本校准表（{n} 场，整体命中 {overall*100:.0f}%）</summary>
    <table style="margin-top:6px;border-collapse:collapse">
      <tr><th style="padding:2px 8px;text-align:left">模型概率段</th><th style="padding:2px 8px">实际命中率</th></tr>
      {rows}
      <tr><td style="padding:2px 8px">整体</td><td style="padding:2px 8px;text-align:center">{overall*100:.0f}%</td></tr>
    </table>
    <div style="margin-top:4px">串关 = 各腿命中率连乘 × 赔率连乘，天然吃双重抽水。模型概率系统性高估
    （0.70+ 段实际仅 50% 命中）→ 按模型概率串关 = 送钱。系统只推荐校准后正 EV 的串票。</div>
  </details>"""

    if not parlay:
        return f"""
  <div class="section-title">串关方案（过关玩法）</div>
  <div class="ticket-card" style="border-color:var(--amber);opacity:.9">
    <h4 class="lottery">🎯 今日无正 EV 串关 <span class="pick-val draw" style="font-size:.75em">空仓</span></h4>
    <div style="font-size:.8rem;opacity:.8;padding:4px 0">
      {'缺 ≥60% 置信场次（可串腿 0 场）' if not parlay else '校准后全部负 EV，不推荐出串（数据纪律）'}
      —— 竞彩串关吃双重抽水，宁可不串，不送钱。
    </div>
    {cal_html}
  </div>"""

    cards = "".join(_ticket_html(t) for t in parlay)
    n_rec = sum(1 for t in parlay if t.get("recommended"))
    return f"""
  <div class="section-title">串关方案（过关玩法）{'· ' + str(n_rec) + ' 张推荐' if n_rec else ''}</div>
  <div class="ticket-grid">{cards}</div>
  {cal_html}"""


def _ev_section(ev_report):
    """EV 价值区报告：全量已结算预测按赔率区间×联赛分层的 ROI"""
    if not ev_report or not ev_report.get("total"):
        return ""
    t = ev_report["total"]
    layers = ev_report.get("layers", {})
    leagues = ev_report.get("leagues", {})
    takeaways = ev_report.get("takeaways", [])

    def _cls(roi):
        return "green" if roi > 0 else ("red" if roi < -0.10 else "amber")

    rows = "".join(
        f"<tr><td>{k}</td><td>{v['n']}</td><td>{v['hit_rate']*100:.1f}%</td>"
        f"<td class='{_cls(v['roi'])}'>{v['roi']*100:+.1f}%</td>"
        f"<td class='{_cls(v['roi'])}'>{v['verdict']}</td></tr>"
        for k, v in layers.items()
    )
    lg_rows = "".join(
        f"<tr><td>{k}</td><td>{v['n']}</td><td>{v['hit_rate']*100:.1f}%</td>"
        f"<td class='{_cls(v['roi'])}'>{v['roi']*100:+.1f}%</td></tr>"
        for k, v in leagues.items()
    )
    tk = "".join(f"<li style='color:var(--text-secondary);font-size:0.82rem;margin:3px 0;'>{x}</li>" for x in takeaways)

    return f"""
  <div class="section-title">🎯 EV 价值区报告（全量复盘）</div>
  <div class="card" style="padding:20px;margin-bottom:14px;">
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:16px;">
      <div class="stat"><div class="label">已结算场次</div><div class="value">{t['n']}</div></div>
      <div class="stat"><div class="label">方向命中率</div><div class="value">{t['hit_rate']*100:.1f}%</div></div>
      <div class="stat"><div class="label">整体 ROI（每场押1单位）</div><div class="value {_cls(t['roi'])}">{t['roi']*100:+.1f}%</div></div>
    </div>
    <ul style="padding-left:18px;margin-bottom:16px;">{tk}</ul>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
      <div>
        <div style="font-weight:700;font-size:0.85rem;margin-bottom:8px;color:var(--text);">按赔率区间（押注结构诊断）</div>
        <table class="data-table" style="width:100%;font-size:0.82rem;">
          <tr><th>区间</th><th>场数</th><th>命中率</th><th>ROI</th><th>判读</th></tr>
          {rows}
        </table>
      </div>
      <div>
        <div style="font-weight:700;font-size:0.85rem;margin-bottom:8px;color:var(--text);">按联赛（≥3场）</div>
        <table class="data-table" style="width:100%;font-size:0.82rem;">
          <tr><th>联赛</th><th>场数</th><th>命中率</th><th>ROI</th></tr>
          {lg_rows}
        </table>
      </div>
    </div>
  </div>
"""


def _results_section(results, predictions, review_ledger=None):
    """赛果复盘: 预测 vs 实际结果对比（优先 results.json，fallback review_ledger）"""
    if not results and not review_ledger:
        return ""

    # 建立 match_id → prediction 多层索引（精确 + 场次号 + 队名）
    pred_map = {p.get("match_id", ""): p for p in predictions}
    pred_fixture_map = {}
    for p in predictions:
        mid = p.get("match_id", "")
        fixture = _extract_fixture(mid)
        if fixture:
            pred_fixture_map[fixture] = p
        # 队名索引（最可靠，跨数据源通用）
        hm = p.get("home_team", "")
        aw = p.get("away_team", "")
        if hm and aw:
            pred_map[f"{hm}_vs_{aw}"] = p

    # 如果没有 results.json，从 review_ledger 构建结果
    if not results and review_ledger:
        results = []
        for rl in review_ledger:
            goals = rl.get("total_goals_actual", 0)
            idx = rl.get("actual_idx", -1)
            if idx == 0:
                hs, as_ = (goals, 0) if goals > 0 else (1, 0)
            elif idx == 1:
                half = max(1, goals // 2)
                hs, as_ = (half, goals - half)
            elif idx == 2:
                hs, as_ = (0, goals) if goals > 0 else (0, 1)
            else:
                hs, as_ = (0, 0)
            results.append({
                "match_id": rl.get("match_id", ""),
                "home_score": hs,
                "away_score": as_,
                "pnl": rl.get("pnl", 0),
            })

    rows = ""
    hits = 0
    total_brier = 0.0
    total_pnl = 0.0
    matched = 0

    for r in results:
        mid = r.get("match_id", "")
        home_score = r.get("home_score")
        away_score = r.get("away_score")
        if home_score is None or away_score is None:
            continue

        pred = pred_map.get(mid)
        if not pred:
            # 模糊匹配：用场次号
            pred = pred_fixture_map.get(_extract_fixture(mid))
        if not pred:
            # 队名匹配（最可靠）
            hm = r.get("home_team", "")
            aw = r.get("away_team", "")
            if hm and aw:
                pred = pred_map.get(f"{hm}_vs_{aw}")
        if not pred:
            continue

        matched += 1
        # 实际结果
        if home_score > away_score:
            actual = "home"
            actual_label = "主胜"
        elif home_score == away_score:
            actual = "draw"
            actual_label = "平局"
        else:
            actual = "away"
            actual_label = "客胜"

        # 预测结果
        ph = pred.get("home_win_prob", 0)
        pd = pred.get("draw_prob", 0)
        pa = pred.get("away_win_prob", 0)
        if ph >= pd and ph >= pa:
            predicted = "home"
            pred_label = "主胜"
        elif pd >= ph and pd >= pa:
            predicted = "draw"
            pred_label = "平局"
        else:
            predicted = "away"
            pred_label = "客胜"

        hit = predicted == actual
        if hit:
            hits += 1

        # Brier score: sum of (prob - indicator)^2
        ind_h = 1.0 if actual == "home" else 0.0
        ind_d = 1.0 if actual == "draw" else 0.0
        ind_a = 1.0 if actual == "away" else 0.0
        brier = (ph - ind_h)**2 + (pd - ind_d)**2 + (pa - ind_a)**2
        total_brier += brier

        # 投注盈亏（如果在三票方案中）
        pnl = r.get("pnl", 0)
        total_pnl += pnl

        hit_cls = "hit" if hit else "miss"
        hit_icon = "✓" if hit else "✗"
        pnl_color = "var(--green)" if pnl > 0 else "var(--red)" if pnl < 0 else "var(--dim)"

        rows += f"""
        <tr class="{hit_cls}">
          <td>{pred.get('home_team', '')} vs {pred.get('away_team', '')}</td>
          <td style="font-weight:800;text-align:center;">{home_score}-{away_score}</td>
          <td>{actual_label}</td>
          <td>{pred_label} ({max(ph, pd, pa):.0%})</td>
          <td style="text-align:center;"><span class="result-icon {hit_cls}">{hit_icon}</span></td>
          <td style="font-family:monospace;font-size:0.68rem;">{brier:.3f}</td>
          <td style="color:{pnl_color};font-weight:600;">{'+' if pnl > 0 else ''}{pnl:.0f}</td>
        </tr>"""

    if matched == 0:
        return ""

    hit_rate = hits / matched
    avg_brier = total_brier / matched

    # 分层评价（2026-08-06 借鉴 MBS 方法论）：从 review.json 读 LogLoss/进球框架/概率分段
    _layered_html = ""
    _rv_date = ""
    if results and results[0].get("match_id", ""):
        _mid0 = results[0]["match_id"]
        # match_id 形如 "2026-08-04_周二001"，前10位是日期
        _rv_date = _mid0[:10] if len(_mid0) >= 10 and _mid0[4] == "-" else ""
    _rv = _load_json(ROOT / "data" / "daily" / _rv_date / "review.json", None) if _rv_date else None
    if _rv and _rv.get("layered"):
        _ly = _rv["layered"]
        _ll = _ly.get("log_loss_final")
        _gf = _ly.get("goal_framework", {})
        _bands = _ly.get("prob_bands", {})
        _fres = _ly.get("freshness_groups", {})
        _chips = ""
        if _ll is not None:
            _chips += f'<div class="ts-chip"><div class="ts-label">LogLoss(final)</div><div class="ts-val" style="color:{"var(--green)" if _ll < 0.9 else "var(--amber)"}">{_ll:.3f}</div></div>'
        if _gf.get("n"):
            _gf_rate = _gf["hits"] / _gf["n"]
            _chips += f'<div class="ts-chip"><div class="ts-label">进球框架</div><div class="ts-val" style="color:{"var(--green)" if _gf_rate >= 0.5 else "var(--amber)"}">{_gf_rate:.0%} ({_gf["hits"]}/{_gf["n"]})</div></div>'
        if _bands:
            _band_str = " · ".join(f"{k}:{v['hit_rate']:.0%}" for k, v in _bands.items())
            _chips += f'<div class="ts-chip"><div class="ts-label">概率分段</div><div class="ts-val" style="font-size:0.68rem;">{_band_str}</div></div>'
        if _fres:
            _fres_str = " · ".join(f"{k}:{v['hit_rate']:.0%}" for k, v in _fres.items())
            _chips += f'<div class="ts-chip"><div class="ts-label">新鲜度分层</div><div class="ts-val" style="font-size:0.68rem;">{_fres_str}</div></div>'
        if _chips:
            _layered_html = f'<div class="results-summary" style="margin-top:6px;">{_chips}</div>'

    return f"""
  <div class="section-title">赛果复盘</div>
  <div class="results-summary">
    <div class="ts-chip"><div class="ts-label">命中率</div><div class="ts-val" style="color:{'var(--green)' if hit_rate >= 0.5 else 'var(--red)'}">{hit_rate:.0%} ({hits}/{matched})</div></div>
    <div class="ts-chip"><div class="ts-label">平均Brier</div><div class="ts-val" style="color:{'var(--green)' if avg_brier < 0.5 else 'var(--amber)'}">{avg_brier:.3f}</div></div>
    <div class="ts-chip"><div class="ts-label">总盈亏</div><div class="ts-val" style="color:{'var(--green)' if total_pnl >= 0 else 'var(--red)'}">&yen;{total_pnl:+.0f}</div></div>
  </div>
  {_layered_html}
  <div class="results-table-wrap">
    <table class="results-table">
      <tr><th>比赛</th><th>比分</th><th>实际</th><th>预测</th><th>命中</th><th>Brier</th><th>盈亏</th></tr>
      {rows}
    </table>
  </div>"""


def _system_panel(breaker, bundle, tier, mult, tier_reason=""):
    streak = breaker.get("current_streak", 0)
    wins = breaker.get("total_wins", 0)
    losses = breaker.get("total_losses", 0)
    wr = wins / max(1, wins + losses)
    daily_pnl = breaker.get("daily_pnl", 0)
    weekly_pnl = breaker.get("weekly_pnl", 0)
    halted = breaker.get("halted", False)
    sha = bundle.get("bundle_sha256", "暂无")
    created = bundle.get("created_at", "")

    tier_cls = "safe" if tier <= 1 else "caution" if tier <= 2 else "danger"
    tier_label = f"T{tier}" + (" · " + tier_reason if tier_reason else "")

    return f"""
  <div class="section-title">系统状态</div>
  <div class="sys-grid">
    <div class="sys-card">
      <h4>熔断器</h4>
      <div class="sys-row"><span class="k">状态</span><span class="tier-indicator {tier_cls}">{tier_label} &middot; x{mult:.2f}</span></div>
      <div class="sys-row"><span class="k">连续</span><span class="v" style="color:{'var(--green)' if streak >= 0 else 'var(--red)'}">{streak:+d}</span></div>
      <div class="sys-row"><span class="k">胜率</span><span class="v">{wr:.1%} ({wins}胜 / {losses}负)</span></div>
      <div class="sys-row"><span class="k">日盈亏</span><span class="v" style="color:{'var(--green)' if daily_pnl >= 0 else 'var(--red)'}">&yen;{daily_pnl:+.0f}</span></div>
      <div class="sys-row"><span class="k">周盈亏</span><span class="v" style="color:{'var(--green)' if weekly_pnl >= 0 else 'var(--red)'}">&yen;{weekly_pnl:+.0f}</span></div>
    </div>
    <div class="sys-card">
      <h4>决策完整性</h4>
      <div class="sys-row"><span class="k">创建时间</span><span class="v">{created[:19] if created else '暂无'}</span></div>
      <div class="sys-row"><span class="k">版本</span><span class="v">{bundle.get('version', 'v1')}</span></div>
      <div class="sys-row"><span class="k">算法</span><span class="v">SHA-256</span></div>
      <div class="hash">{sha}</div>
    </div>
    <div class="sys-card">
      <h4>模型配置</h4>
      <div class="sys-row"><span class="k">集成</span><span class="v">DC 60% + MC 40%</span></div>
      <div class="sys-row"><span class="k">融合</span><span class="v">模型60 / 市场25 / DJYY15</span></div>
      <div class="sys-row"><span class="k">元学习器</span><span class="v">LGBM (10%)</span></div>
      <div class="sys-row"><span class="k">校准</span><span class="v">Isotonic</span></div>
      <div class="sys-row"><span class="k">MC模拟</span><span class="v">50,000次</span></div>
      <div class="sys-row"><span class="k">信任区间</span><span class="v">Wilson</span></div>
    </div>
  </div>"""


def _is_value(p, value_matches=None):
    """判断是否为价值投注: 仅当被三票方案选中（稳胆/搏冷）"""
    if value_matches and p.get("match_id") in value_matches:
        return True
    return False


def _breaker_tier(breaker):
    """返回熔断级别，区分真实 tier 和 halted 原因"""
    streak = abs(min(breaker.get("current_streak", 0), 0))
    actual_tier = max(0, breaker.get("tier", 0))
    if streak >= 15:
        return 4, "连败≥15 停注"
    if streak >= 12:
        return 3, ""
    if streak >= 6:
        return 2, ""
    if streak >= 3:
        return 1, ""
    if breaker.get("halted"):
        # 非连败触发的停注（日/周止损），显示实际tier
        return actual_tier, "周/日止损停注"
    return actual_tier, ""


def _build_daily_brief(
    web_dir: Path,
    target_date: str,
    predictions: list,
    ticket: dict,
    review: dict | None,
    league_report: dict | None,
    parlay_report: dict | None,
    trend_report: dict | None = None,
) -> Path:
    """生成每日简报 markdown（投注决策一目了然）"""
    lines = [
        f"# 竞彩投注简报 {target_date}\n",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}（北京时间）\n",
    ]

    # 投注方案
    bets = ticket.get("stable", []) + ticket.get("value", []) + ticket.get("lottery", [])
    total = ticket.get("total_stake", 0)
    if bets:
        lines.append("## 💰 今日投注方案\n")
        lines.append(f"总投入 **¥{total:.0f}**（资金池 ¥{ticket.get('bankroll', 0):.0f}）\n")
        _hafu_name = {"HH": "胜胜", "HD": "胜平", "HA": "胜负", "DH": "平胜", "DD": "平平", "DA": "平负", "AH": "负胜", "AD": "负平", "AA": "负负"}
        for it in bets:
            mid = it.get("match", "")
            if "#" in mid:
                mid = mid.split("#", 1)[0]
            sel = it.get("sel", "")
            if sel.startswith("hcap_"):
                label = {"home": "主让胜", "draw": "让平", "away": "让负"}.get(sel[5:], sel[5:])
            elif sel.startswith("ttg_"):
                label = f"总进球{sel[4:]}+球" if int(sel[4:]) >= 7 else f"总进球{sel[4:]}球"
            elif sel.startswith("crs_"):
                p = sel[4:].split("_")
                label = f"比分{p[0]}:{p[1]}" if len(p) == 2 else sel
            elif sel.startswith("hafu_"):
                label = "半全场·" + _hafu_name.get(sel[5:], sel[5:])
            else:
                label = {"home": "主胜", "draw": "平局", "away": "客胜"}.get(sel, sel)
            lines.append(f"- {mid} [{label}] @{it.get('odds', 0):.2f} × ¥{it.get('stake', 0):.0f}\n")
    else:
        lines.append("## 💰 今日投注\n")
        lines.append("**空仓不出手**（无正 EV 价值注，避免送钱）\n")

    # 预测清单
    if predictions:
        lines.append("\n## 🔮 今日预测\n")
        lines.append("| 场次 | 联赛 | 预测 | 置信 | 主推赔率 | 冷门风险 |\n")
        lines.append("|---|---|---|---|---|---|\n")
        for p in predictions:
            direction = p.get("direction", "?")
            dlabel = {"home": "主胜", "draw": "平局", "away": "客胜"}.get(direction, direction)
            odds = p.get(f"{direction}_odds", 0) or 0
            _conf = p.get("confidence") or 0
            _risk = p.get("reverse_upset_risk") or 0
            lines.append(
                f"| {p.get('match_id', '')} | {p.get('competition', '')} | {dlabel} "
                f"| {_conf:.0%} | {odds:.2f} | {_risk:.0f}% |\n"
            )

    # 联赛状态
    if league_report and league_report.get("leagues"):
        lines.append("\n## 🏆 联赛分层（哪个联赛值得投）\n")
        for row in league_report["leagues"]:
            if row["n"] < 5:
                continue
            icon = {"价值区": "✅", "送钱区": "🚫", "谨慎": "⚠️", "观望": "👀"}.get(row["verdict"], "·")
            lines.append(f"- {icon} **{row['league']}**：{row['n']}场 命中{row['hit_rate']*100:.0f}% 均赔{row['avg_odds']:.2f} ROI {row['roi']*100:+.1f}%\n")

    # 串关结论
    if parlay_report and parlay_report.get("verdict"):
        lines.append(f"\n## 🎰 串关评估\n{parlay_report['verdict']}\n")

    # 上期战绩（上一日期目录的 review）
    if review and review.get("n_matches"):
        lines.append(
            f"\n## 📊 上期战绩\n"
            f"命中 {review.get('hits', 0)}/{review.get('n_matches', 0)} "
            f"({review.get('hit_rate', 0)*100:.0f}%)，盈亏 **¥{review.get('total_pnl', 0):+.0f}**\n"
        )

    # 准确率趋势（诚实回答"是否每天在提升"）
    if trend_report and trend_report.get("daily"):
        v = trend_report.get("verdict", "")
        r7 = trend_report.get("rolling7") or {}
        p7 = trend_report.get("prev7") or {}
        lines.append("\n## 📈 准确率趋势\n")
        if v:
            lines.append(f"**判定：{v}**\n")
        if r7 and p7:
            lines.append(
                f"- 最近7天：{r7.get('n', 0)}场 命中率 {r7.get('hit_rate', 0)*100:.0f}% "
                f"Brier {r7.get('brier_final', 0):.2f}\n"
                f"- 前7天：{p7.get('n', 0)}场 命中率 {p7.get('hit_rate', 0)*100:.0f}% "
                f"Brier {p7.get('brier_final', 0):.2f}\n"
            )
        lines.append("\n| 日期 | 场次 | 命中 | 命中率 | 累计命中率 |\n")
        lines.append("|---|---|---|---|---|\n")
        for d in trend_report["daily"][-7:]:
            lines.append(f"| {d['date']} | {d['n']} | {d['hits']}/{d['n']} | {d['hit_rate']*100:.0f}% | {d['cum_hit_rate']*100:.0f}% |\n")

    # 比分命中率（2026-08-05 闭环：主推前三不靠谱→前5，账本可核查）
    _slp = web_dir.parent / "data" / "state" / "review_ledger.jsonl"
    if _slp.exists():
        _recs = []
        for _line in _slp.read_text(encoding="utf-8").strip().split("\n"):
            if _line.strip():
                try:
                    _recs.append(json.loads(_line))
                except Exception:
                    continue
        if _recs:
            _n = len(_recs)
            lines.append("\n## 🎯 比分命中率（历史可核查）\n")
            lines.append("| 推荐档 | 命中 | 命中率 |\n")
            lines.append("|---|---|---|\n")
            for _label, _cond in [
                ("主推 top1", lambda r: r.get("score_rank") == 1),
                ("主推 top3", lambda r: r.get("score_top3_hit")),
                ("主推 top5（当前）", lambda r: r.get("score_top5_hit")),
                ("候选 top8", lambda r: r.get("score_top8_hit")),
            ]:
                _h = sum(1 for r in _recs if _cond(r))
                _hl = "**" if _label.startswith("主推 top5") else ""
                lines.append(f"| {_label} | {_h}/{_n} | {_hl}{_h/_n*100:.0f}%{_hl} |\n")

            # 盘口信号命中率（8/5 起累积验证，数据说话盘口信号是否有效）
            _ms_items = [r for r in _recs if r.get("market_signal_hit") is not None]
            if _ms_items:
                _msh = sum(1 for r in _ms_items if r.get("market_signal_hit"))
                lines.append("\n## 📊 盘口信号命中率（累积验证中）\n")
                lines.append(f"- 欧赔压缩方向信号: **{_msh}/{len(_ms_items)} ({_msh/len(_ms_items)*100:.0f}%)**")
                lines.append(f"- 模型方向基线: {sum(1 for r in _recs if r.get('hit'))}/{_n} ({sum(1 for r in _recs if r.get('hit'))/_n*100:.0f}%)\n")

    out = web_dir / f"daily-brief-{target_date}.md"
    out.write_text("".join(lines), encoding="utf-8")
    return out


if __name__ == "__main__":
    build_site()
