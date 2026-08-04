from __future__ import annotations
"""主入口 - 每日预测流水线（增强版）

集成模块:
  - Dixon-Coles + Monte Carlo + Ensemble 预测
  - 多市场KL校准 + Shin去水 + 对数意见池
  - 逆向赔率分析（压缩比 + 级联漏斗 + 冷门风险）
  - 同赔历史匹配
  - Wilson信任度 + N维组合挖掘
  - 熔断机制 + CPPI + 三票制资金管理
  - Kelly准则 + 推荐引擎
  - SHA-256不可变决策链
"""
import argparse
import json
import sys
import numpy as np
from datetime import date, datetime
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.sources.manager import SourceManager
from engine.sources.base import MatchResult
from engine.sources.same_odds import SameOddsAnalyzer
from engine.prediction.ensemble import EnsembleModel
from engine.prediction.dixon_coles import DixonColesConfig
from engine.prediction.monte_carlo import MonteCarloConfig
from engine.prediction.base import TeamRating
from engine.prediction.calibration import (
    devig_shin,
    select_devig_method,
    multi_market_calibration,
    MarketOdds,
)
from engine.prediction.reverse_odds import ReverseOddsEngine, ReverseOddsInput
from engine.strategy.kelly import KellyStrategy
from engine.strategy.circuit_breaker import CircuitBreaker
from engine.strategy.three_ticket import ThreeTicketAllocator
from engine.strategy.cppi import CPPIStrategy
from engine.integrity.decision_bundle import DecisionBundle
from engine.integrity.plan_lock import PlanLock
from engine.learning.elo_updater import EloUpdater
from engine.learning.wilson_trust import TrustSystem
from engine.learning.combo_miner import ComboMiner
from engine.learning.online_weights import OnlineWeightLearner
from engine.prediction.lgbm_model import LGBMModel, LGBMConfig, build_features
from engine.prediction.isotonic_cal import IsotonicCalibrator, CalibrationConfig
from engine.prediction.temperature_scaling import TemperatureScaler
from engine.prediction.rho_fitter import RhoFitter
from engine.prediction.time_decay import time_decay_weights
from engine.learning.league_params import LeagueParamsManager
from engine.learning.fusion_optimizer import FusionOptimizer, FusionWeights
from engine.storage.match_db import MatchDB
from engine.prediction.htft_model import htft_probabilities, top_htft


def load_config(name: str) -> dict:
    path = ROOT / "config" / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _pick_direction(h: float, d: float, a: float, draw_alert=None) -> str:
    """从最终概率选方向（argmax），draw_alert 触发且平局概率接近最高时改判平局。

    与结算口径一致：修复预测时 direction 为空、复盘口径不一致的问题。
    """
    best = max(("home", h), ("draw", d), ("away", a), key=lambda x: x[1])
    if draw_alert and best[0] != "draw" and best[1] - d < 0.08 and d >= 0.26:
        return "draw"
    return best[0]


def run_daily_pipeline(target_date: date, predict_only: bool = False):
    """执行每日完整流水线"""
    print(f"{'='*60}")
    print(f"  每日预测流水线 - {target_date.isoformat()}")
    print(f"{'='*60}")

    # 1. 加载配置
    pred_cfg = load_config("prediction")
    strat_cfg = load_config("strategy")

    # 2. 获取数据（三源融合模式: 体彩+500万+DJYY）
    print("\n[1/8] 获取赛程数据（三源融合）...")
    source_mgr = SourceManager(ROOT / "data")
    try:
        fixtures, manifest = source_mgr.fetch_merged_fixtures(target_date)
    except Exception:
        # 融合失败时降级为简单 fallback
        fixtures, manifest = source_mgr.fetch_fixtures(target_date)
    print(f"  ✓ 获取 {len(fixtures)} 场比赛 (来源: {manifest.source})")

    # 关键过滤：只预测"竞彩编号所属比赛日"== target_date 的场次
    # 竞彩跨日开售（周五开售周六/周日比赛），match_id 前缀 = 编号推断的比赛日。
    # 不过滤会导致同一场比赛出现在多个日期的预测页 + 复盘互相污染（历史痛点）。
    _before = len(fixtures)
    fixtures = [f for f in fixtures if f.match_id.startswith(target_date.isoformat())]
    _dropped = _before - len(fixtures)
    if _dropped:
        print(f"  ⏭ 跨日场次过滤: 丢弃 {_dropped} 场非 {target_date} 比赛日的场次")

    # 再过滤：开球时间已过的场次（防止把已开赛/已结束的比赛当未来场次预测）
    _now = datetime.now()
    _still = []
    for f in fixtures:
        _ko = (f.kickoff or "").strip()
        if _ko:
            try:
                if datetime.fromisoformat(_ko.replace(" ", "T")) <= _now:
                    print(f"  ⏭ 已开赛场次跳过: {f.match_id} ({_ko})")
                    continue
            except ValueError:
                pass
        _still.append(f)
    fixtures = _still

    if not fixtures:
        print("  ⚠ 今日无待预测场次（可能无在售比赛或全部已开赛）")
        return [], None

    # 2.5 DJYY增强: 获取第三方模型概率 + Pinnacle赔率 + xG
    print("\n[1.5/8] DJYY增强数据...")
    try:
        djyy_enrichment = source_mgr.enrich_from_djyy(fixtures, target_date)
        if djyy_enrichment:
            print(f"  ✓ DJYY增强: {len(djyy_enrichment)}/{len(fixtures)} 场匹配")
        else:
            print(f"  - DJYY无匹配（不影响主流程）")
    except Exception as e:
        djyy_enrichment = {}
        print(f"  - DJYY增强跳过: {e}")

    # 2.5b DJYY SSR 真实数据源 (赛前xG + Pinnacle赔率)
    from engine.sources.djyy_ssr import DJYYSSRSource
    djyy_ssr = DJYYSSRSource(ROOT / "data" / "djyy_matches.json")
    djyy_ssr_enriched = 0
    print(f"  DJYY SSR: {len(djyy_ssr.matches)} 场比赛数据可用")

    # 3. 加载球队评级
    print("\n[2/8] 加载球队评级...")
    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")

    # 4. 初始化增强模块
    print("\n[3/8] 初始化增强分析模块...")
    trust_system = TrustSystem()
    combo_miner = ComboMiner(ROOT / "data" / "state" / "combo_stats.json")
    same_odds = SameOddsAnalyzer(ROOT / "data" / "historical" / "odds.csv")
    reverse_engine = ReverseOddsEngine()
    print(f"  ✓ 同赔库 {same_odds.stats_summary()['total_records']} 条记录")

    # LightGBM 第三模型层
    lgbm_cfg = LGBMConfig(**{k: v for k, v in pred_cfg.get("lgbm", {}).items()
                             if k in LGBMConfig.__dataclass_fields__})
    lgbm_model = LGBMModel(ROOT / "data" / "models" / "lgbm_model.txt", config=lgbm_cfg)
    if lgbm_model.is_available:
        print(f"  ✓ LightGBM 已加载")
    else:
        print(f"  - LightGBM 未训练/未安装（跳过第三层）")

    # Isotonic 校准层
    cal_cfg = CalibrationConfig(**{k: v for k, v in pred_cfg.get("calibration", {}).items()
                                   if k in CalibrationConfig.__dataclass_fields__})
    calibrator = IsotonicCalibrator(
        ROOT / "data" / "models" / "isotonic_cal.pkl", config=cal_cfg
    )
    if calibrator.is_fitted:
        print(f"  ✓ Isotonic 校准已加载 (method={calibrator.method_used})")
    else:
        print(f"  - Isotonic 未拟合（原样输出）")

    # Temperature Scaling 校准层（在 Isotonic 之后应用）
    temp_scaler = TemperatureScaler(ROOT / "data" / "models" / "temperature.json")
    if temp_scaler.is_fitted:
        print(f"  ✓ Temperature Scaling 已加载 (T={temp_scaler.temperature_value:.3f})")
    else:
        print(f"  - Temperature Scaling 未拟合（跳过）")

    # 联赛独立参数
    league_mgr = LeagueParamsManager(ROOT / "data" / "state" / "league_params.json")
    # 尝试从 DJYY league-matrix 更新先验
    try:
        matrix = source_mgr.get_league_params()
        if matrix:
            league_mgr.update_from_league_matrix(matrix)
    except Exception:
        pass
    print(f"  ✓ 联赛参数: {len(league_mgr.summary())} 个联赛已配置")

    # MatchDB: 历史xG作为预测辅助
    match_db = MatchDB(ROOT / "data" / "state" / "match_history.db")

    # 5. 预测 + 增强分析
    print("\n[4/8] 运行预测模型 + 增强分析...")
    dc_cfg = DixonColesConfig(**{k: v for k, v in pred_cfg.get("prediction", {}).items()
                                  if k in DixonColesConfig.__dataclass_fields__})
    mc_cfg = MonteCarloConfig(**{k: v for k, v in pred_cfg.get("prediction", {}).items()
                                  if k in MonteCarloConfig.__dataclass_fields__})
    # 在线权重学习: 动态调整模型权重
    weight_learner = OnlineWeightLearner(ROOT / "data" / "state" / "online_weights.json")
    static_weights = pred_cfg.get("ensemble", {"dixon_coles_weight": 0.6, "monte_carlo_weight": 0.4})
    default_w = {
        "dixon_coles": static_weights.get("dixon_coles_weight", 0.6),
        "monte_carlo": static_weights.get("monte_carlo_weight", 0.4),
    }
    dynamic_weights = weight_learner.get_weights(default=default_w)
    print(f"  模型权重: DC={dynamic_weights.get('dixon_coles', 0.6):.3f}, "
          f"MC={dynamic_weights.get('monte_carlo', 0.4):.3f} "
          f"({'动态' if dynamic_weights != default_w else '静态'})")

    model = EnsembleModel(
        dc_config=dc_cfg,
        mc_config=mc_cfg,
        weights=dynamic_weights,
    )

    # 融合参数（可由 param_optimizer 自动调整，不写死）
    fusion_cfg = pred_cfg.get("fusion", {})
    fusion_cfg.setdefault("model_weight", 0.60)
    fusion_cfg.setdefault("market_weight", 0.25)
    fusion_cfg.setdefault("djyy_weight", 0.15)  # DJYY第三方模型权重
    fusion_cfg.setdefault("same_odds_max_adjust", 0.05)
    fusion_cfg.setdefault("same_odds_min_confidence", 0.3)
    fusion_cfg.setdefault("combo_boost_cap", 0.03)
    fusion_cfg.setdefault("trust_shrink_enabled", True)

    # 加载新浪赔率数据（初始+即时+变化历史）
    sina_odds_map = {}
    sina_odds_by_no = {}  # 按竞彩编号索引（优先匹配方式）
    sina_odds_file = ROOT / "data" / "daily" / target_date.isoformat() / "odds_sina.json"
    if sina_odds_file.exists():
        try:
            sina_data = json.loads(sina_odds_file.read_text())
            for m in sina_data:
                # 按队名索引（fallback）
                sina_odds_map[(m.get("home_team", ""), m.get("away_team", ""))] = m
                # 按竞彩编号索引（优先）
                match_no = m.get("match_no", "")
                if match_no:
                    sina_odds_by_no[match_no] = m
            print(f"  ✓ 新浪赔率: {len(sina_data)} 场 (编号匹配: {len(sina_odds_by_no)})")
        except Exception as e:
            print(f"  ⚠ 新浪赔率加载失败: {e}")

    # 自我革新: 读取优化器冠军权重覆盖静态默认
    from engine.learning.fusion_optimizer import FusionOptimizer, FusionWeights
    from engine.review.post_match import ReviewLedger
    _ledger = ReviewLedger(ROOT / "data" / "state" / "review_ledger.jsonl")
    _fusion_opt = FusionOptimizer(ROOT / "data" / "state" / "fusion_weights.json", _ledger, pred_cfg.get("optimizer", {}))
    _champion = _fusion_opt.get_champion()
    fusion_cfg["model_weight"] = _champion.model
    fusion_cfg["market_weight"] = _champion.market
    fusion_cfg["djyy_weight"] = _champion.djyy
    print(f"  融合权重(优化器): model={_champion.model:.3f} market={_champion.market:.3f} djyy={_champion.djyy:.3f}")

    predictions = []
    for fixture in fixtures:
        home_rating = elo_updater.get_rating(fixture.home_team)
        away_rating = elo_updater.get_rating(fixture.away_team)

        # DJYY form_xG 修正: 用真实近期xG替代默认ratings
        djyy_pre = djyy_enrichment.get(fixture.match_id, {})
        form_xg = djyy_pre.get("form_xg")
        if form_xg:
            base_goals = pred_cfg.get("prediction", {}).get("base_goals", 1.35)
            if home_rating.attack == 1.0 and form_xg.get("home_avg"):
                home_rating.attack = form_xg["home_avg"] / base_goals
            if away_rating.attack == 1.0 and form_xg.get("away_avg"):
                away_rating.attack = form_xg["away_avg"] / base_goals

        # MatchDB fallback: DJYY无数据时用历史积累xG
        base_goals = pred_cfg.get("prediction", {}).get("base_goals", 1.35)
        if home_rating.attack == 1.0:
            db_xg = match_db.get_team_xg(fixture.home_team, fixture.competition)
            if db_xg and db_xg.get("avg_xg_for"):
                home_rating.attack = db_xg["avg_xg_for"] / base_goals
        if away_rating.attack == 1.0:
            db_xg = match_db.get_team_xg(fixture.away_team, fixture.competition)
            if db_xg and db_xg.get("avg_xg_for"):
                away_rating.attack = db_xg["avg_xg_for"] / base_goals

        # xG校准反馈: 用历史偏差修正联赛级别系统误差
        if fixture.competition:
            cal = match_db.get_xg_calibration(league=fixture.competition, limit=50)
            if cal.get("n", 0) >= 5 and cal.get("avg_pred_total_xg"):
                # factor = 真实xG / 预测xG, >1说明低估, <1说明高估
                factor = cal["avg_actual_total_xg"] / cal["avg_pred_total_xg"]
                factor = max(0.80, min(1.20, factor))  # 防过矫
                if abs(factor - 1.0) > 0.03:  # 偏差>3%才修正
                    home_rating.attack *= factor
                    away_rating.attack *= factor

        # 赛程密度: 休息不足→疲劳惩罚 (attack下降)
        rest = djyy_pre.get("rest_days")
        if rest:
            home_rest = rest.get("home")
            away_rest = rest.get("away")
            # <3天休息: 每少1天扣5%攻击力, 最多扣15%
            if home_rest is not None and home_rest < 3:
                home_rating.attack *= max(0.85, 1.0 - (3 - home_rest) * 0.05)
            if away_rest is not None and away_rest < 3:
                away_rating.attack *= max(0.85, 1.0 - (3 - away_rest) * 0.05)

        # 伤停缺阵: 攻击型球员缺阵→下调attack
        inj = djyy_pre.get("injuries")
        if inj:
            home_miss = inj.get("home_attackers", 0)
            away_miss = inj.get("away_attackers", 0)
            # 每个缺阵攻击手扣4%, 最多扣12%
            if home_miss > 0:
                home_rating.attack *= max(0.88, 1.0 - home_miss * 0.04)
            if away_miss > 0:
                away_rating.attack *= max(0.88, 1.0 - away_miss * 0.04)

        # 查找新浪赔率数据（优先用竞彩编号匹配，fallback用队名）
        _sina_data = None
        # 从 match_id 提取竞彩编号: "2026-08-01_周六001" → "周六001"
        _match_no = fixture.match_id.split("_", 1)[-1] if "_" in fixture.match_id else ""
        _sina_match = sina_odds_by_no.get(_match_no) if _match_no else None
        if not _sina_match:
            # fallback: 队名精确匹配
            _sina_match = sina_odds_map.get((fixture.home_team, fixture.away_team))
        if not _sina_match:
            # fallback: 模糊匹配
            import re as _re
            def _norm_name(s):
                s = s.replace("FC", "").replace("队", "").replace("市", "")
                s = _re.sub(r'[^\u4e00-\u9fffa-zA-Z]', '', s)
                return s.strip()
            _ht = _norm_name(fixture.home_team)
            _at = _norm_name(fixture.away_team)
            for (sh, sa), v in sina_odds_map.items():
                _sh = _norm_name(sh)
                _sa = _norm_name(sa)
                if (_ht and _sh and (_ht in _sh or _sh in _ht)) and \
                   (_at and _sa and (_at in _sa or _sa in _at)):
                    _sina_match = v
                    break
        if _sina_match:
            _sina_data = {
                "initial_odds": _sina_match.get("euro", {}).get("initial"),
                "current_odds": _sina_match.get("euro", {}).get("current"),
                "movement": _sina_match.get("euro", {}).get("movement"),
                "compression": _sina_match.get("euro", {}).get("compression"),
                "odds_history_count": len(_sina_match.get("odds_history", [])),
                "asia": _sina_match.get("asia"),
                "totals": _sina_match.get("totals"),
                "match_time": _sina_match.get("match_time"),
            }

        market_odds = None
        if fixture.home_odds and fixture.draw_odds and fixture.away_odds:
            # 验证: 真实十进制赔率必须 > 1.0 (概率才 < 1.0)
            if all(o > 1.0 for o in (fixture.home_odds, fixture.draw_odds, fixture.away_odds)):
                market_odds = (fixture.home_odds, fixture.draw_odds, fixture.away_odds)
        if market_odds is None and djyy_pre.get("pinnacle_odds"):
            # 国内源被WAF挡时, 用DJYY的Pinnacle赔率作为fallback
            po = djyy_pre["pinnacle_odds"]
            _po_vals = None
            if isinstance(po, (list, tuple)) and len(po) >= 3:
                _po_vals = (float(po[0]), float(po[1]), float(po[2]))
            elif isinstance(po, dict):
                _po_vals = (float(po.get("home", 0)), float(po.get("draw", 0)), float(po.get("away", 0)))
            # DJYY有时返回概率(0-1)而非赔率(>1), 需验证
            if _po_vals and all(o > 1.0 for o in _po_vals):
                market_odds = _po_vals

        pred = model.predict(
            home=home_rating,
            away=away_rating,
            market_odds=market_odds,
            handicap=fixture.handicap,
        )
        pred.match_id = fixture.match_id
        pred.competition = fixture.competition

        # --- 增强: Shin去水 + 多市场校准 ---
        calibrated_probs = None
        if market_odds:
            fair_probs = select_devig_method(list(market_odds))
            # 多市场KL校准（如果有让球/大小球赔率）
            if fixture.handicap is not None:
                try:
                    mo = MarketOdds(
                        home_win=market_odds[0],
                        draw=market_odds[1],
                        away_win=market_odds[2],
                    )
                    cal_result = multi_market_calibration(
                        pred.home_xg, pred.away_xg, mo
                    )
                    calibrated_probs = cal_result.get("probs")
                except Exception:
                    pass
            if calibrated_probs is None:
                calibrated_probs = fair_probs

        # --- 增强: 逆向赔率分析 ---
        reverse_result = None
        if market_odds:
            try:
                ri = ReverseOddsInput(
                    had_odds=market_odds,
                    had_odds_initial=market_odds,  # 无初始赔率时用当前
                )
                reverse_result = reverse_engine.analyze(ri)
            except Exception:
                pass

        # --- 增强: 同赔分析 ---
        same_odds_result = None
        if market_odds:
            same_odds_result = same_odds.analyze(
                market_odds[0], market_odds[1], market_odds[2],
                league=fixture.competition,
            )

        # --- 增强: 组合挖掘加分 ---
        features = _extract_features(fixture, pred)
        combo_boost = combo_miner.get_boost(features)

        # --- 增强: Wilson信任度调整 ---
        # 用模型历史命中率（简化: 用confidence作为代理）
        trust_score = trust_system.compute_trust(
            hits=int(pred.confidence * 10),
            total=10,
        )

        # 综合概率（融合: 模型 + 市场校准 + DJYY第三方 + 同赔偏差 + 组合加分）
        # 所有融合参数从 config/prediction.json["fusion"] 读取，可由优化器自动调整
        final_h, final_d, final_a = pred.home_win_prob, pred.draw_prob, pred.away_win_prob

        # 获取DJYY增强数据
        djyy_data = djyy_enrichment.get(fixture.match_id, {})
        djyy_probs = djyy_data.get("model_probs")

        if calibrated_probs and djyy_probs and djyy_probs.get("home"):
            # 三路融合: 自有模型 + 市场校准 + DJYY模型
            mw = fusion_cfg["model_weight"]
            kw = fusion_cfg["market_weight"]
            dw = fusion_cfg["djyy_weight"]
            # 归一化权重（确保总和=1）
            total_w = mw + kw + dw
            mw, kw, dw = mw / total_w, kw / total_w, dw / total_w
            final_h = mw * pred.home_win_prob + kw * calibrated_probs[0] + dw * djyy_probs["home"]
            final_d = mw * pred.draw_prob + kw * calibrated_probs[1] + dw * djyy_probs["draw"]
            final_a = mw * pred.away_win_prob + kw * calibrated_probs[2] + dw * djyy_probs["away"]
        elif calibrated_probs:
            # 两路融合（无DJYY数据时）
            mw = fusion_cfg["model_weight"]
            kw = fusion_cfg["market_weight"]
            total_w = mw + kw
            mw, kw = mw / total_w, kw / total_w
            final_h = mw * pred.home_win_prob + kw * calibrated_probs[0]
            final_d = mw * pred.draw_prob + kw * calibrated_probs[1]
            final_a = mw * pred.away_win_prob + kw * calibrated_probs[2]
        elif djyy_probs and djyy_probs.get("home"):
            # 只有DJYY（无市场赔率时）
            mw = 1.0 - fusion_cfg["djyy_weight"]
            dw = fusion_cfg["djyy_weight"]
            final_h = mw * pred.home_win_prob + dw * djyy_probs["home"]
            final_d = mw * pred.draw_prob + dw * djyy_probs["draw"]
            final_a = mw * pred.away_win_prob + dw * djyy_probs["away"]

        # 同赔偏差微调
        if same_odds_result and same_odds_result.confidence > fusion_cfg["same_odds_min_confidence"]:
            adj_strength = fusion_cfg["same_odds_max_adjust"] * same_odds_result.confidence
            final_h += same_odds_result.home_bias * adj_strength
            final_d += same_odds_result.draw_bias * adj_strength
            final_a += same_odds_result.away_bias * adj_strength

        # 组合挖掘加分
        if combo_boost > 0:
            best_sel = max(
                [("H", final_h), ("D", final_d), ("A", final_a)],
                key=lambda x: x[1],
            )
            boost_amount = min(combo_boost, fusion_cfg["combo_boost_cap"])
            if best_sel[0] == "H":
                final_h += boost_amount
            elif best_sel[0] == "D":
                final_d += boost_amount
            else:
                final_a += boost_amount

        # --- LightGBM 第三层融合 ---
        if lgbm_model.is_available:
            lgbm_weight = fusion_cfg.get("lgbm_weight", 0.10)
            feature_dict = build_features(
                elo_home=home_rating.elo,
                elo_away=away_rating.elo,
                odds=market_odds,
                handicap=fixture.handicap,
                xg_home=getattr(fixture, "_xg_home", None),
                xg_away=getattr(fixture, "_xg_away", None),
                djyy_probs=djyy_probs,
            )
            lgbm_pred = lgbm_model.predict_single(feature_dict)
            if lgbm_pred:
                # 混合: (1-lgbm_weight)*当前 + lgbm_weight*lgbm
                final_h = (1 - lgbm_weight) * final_h + lgbm_weight * lgbm_pred[0]
                final_d = (1 - lgbm_weight) * final_d + lgbm_weight * lgbm_pred[1]
                final_a = (1 - lgbm_weight) * final_a + lgbm_weight * lgbm_pred[2]

        # 归一化
        total_prob = final_h + final_d + final_a
        if total_prob > 0:
            final_h /= total_prob
            final_d /= total_prob
            final_a /= total_prob

        # 平局先验修正: 泊松模型系统性低估平局
        # 策略: 当市场隐含平局概率 >= 25% 时，将模型平局概率向市场方向强力修正
        if calibrated_probs and calibrated_probs[1] >= 0.25:
            market_d = calibrated_probs[1]
            target_d = market_d * 0.90
            gap = target_d - final_d
            if gap > 0.005:
                final_d += gap
                total_ha = final_h + final_a
                if total_ha > 0:
                    final_h -= gap * (final_h / total_ha)
                    final_a -= gap * (final_a / total_ha)

        # --- 赔率变动信号修正 ---
        # 新浪赔率变化方向作为信号：赔率下降=资金涌入=庄家看好
        if _sina_data and _sina_data.get("movement"):
            mv = _sina_data["movement"]
            comp = _sina_data.get("compression", {})
            # 压缩比 < 0.95 表示赔率明显下降（资金涌入）
            # 压缩比 > 1.05 表示赔率明显上升（资金撤出）
            _signal_strength = 0
            if comp.get("home", 1.0) < 0.95:
                final_h += 0.02; _signal_strength += 1
            elif comp.get("home", 1.0) > 1.05:
                final_h -= 0.02; _signal_strength += 1
            if comp.get("away", 1.0) < 0.95:
                final_a += 0.02; _signal_strength += 1
            elif comp.get("away", 1.0) > 1.05:
                final_a -= 0.02; _signal_strength += 1
            if _signal_strength > 0:
                total_p = final_h + final_d + final_a
                if total_p > 0:
                    final_h /= total_p; final_d /= total_p; final_a /= total_p

        # --- Isotonic 校准（最终修正） ---
        if calibrator.is_fitted:
            final_h, final_d, final_a = calibrator.calibrate((final_h, final_d, final_a))

        # --- Temperature Scaling 校准 ---
        if temp_scaler.is_fitted:
            final_h, final_d, final_a = temp_scaler.calibrate((final_h, final_d, final_a))

        # --- 平局预警分类 ---
        # 冷门平局: 一方被市场看好但模型+市场证据显示存在平局风险
        # 均势平局: 双方实力接近、平局被市场低估
        draw_alert = None
        if calibrated_probs:
            market_h, market_d, market_a = calibrated_probs
            max_market = max(market_h, market_d, market_a)
            max_model = max(final_h, final_d, final_a)
            # 冷门平局: 市场强烈看好一方(>50%)，但平局概率>=25%
            if max_market > 0.50 and market_d >= 0.25:
                draw_alert = "cold_draw"  # 冷门平局
            # 均势平局: 双方接近(差距<15%)，平局概率>=26%
            elif abs(market_h - market_a) < 0.15 and market_d >= 0.26:
                draw_alert = "balanced_draw"  # 均势平局

        # 半全场概率 (基于最终xG)
        _htft = htft_probabilities(pred.home_xg, pred.away_xg)

        # 无真实赔率时, 优先用 DJYY SSR 真实赔率 (Pinnacle), 否则合成赔率
        _odds_synthetic = False
        _djyy_ssr = djyy_ssr.enrich_prediction(fixture.home_team, fixture.away_team)
        if _djyy_ssr:
            djyy_ssr_enriched += 1
            # 用 DJYY 真实赛前 xG 替代我们模型的 xG
            if _djyy_ssr.get("home_xg_djyy") and _djyy_ssr.get("away_xg_djyy"):
                pred.home_xg = _djyy_ssr["home_xg_djyy"]
                pred.away_xg = _djyy_ssr["away_xg_djyy"]
        if market_odds is None and _djyy_ssr and _djyy_ssr.get("home_odds_djyy"):
            # DJYY 真实 Pinnacle 赔率 → 不再合成
            market_odds = (
                _djyy_ssr["home_odds_djyy"],
                _djyy_ssr["draw_odds_djyy"],
                _djyy_ssr["away_odds_djyy"],
            )
        if market_odds is None and final_h > 0 and final_d > 0 and final_a > 0:
            # 合成赔率: 公平赔率 = 1/概率, 加 5% 庄家水位, 最低 1.01
            _margin_rate = 1.05
            market_odds = (
                max(1.01, round(1 / (final_h * _margin_rate), 2)),
                max(1.01, round(1 / (final_d * _margin_rate), 2)),
                max(1.01, round(1 / (final_a * _margin_rate), 2)),
            )
            _odds_synthetic = True

        predictions.append({
            "match_id": pred.match_id,
            "competition": pred.competition,
            "home_team": pred.home_team,
            "away_team": pred.away_team,
            # 最终融合概率
            "home_win_prob": round(final_h, 4),
            "draw_prob": round(final_d, 4),
            "away_win_prob": round(final_a, 4),
            # xG
            "home_xg": pred.home_xg,
            "away_xg": pred.away_xg,
            # 市场赔率 (真实或合成)
            "home_odds": market_odds[0] if market_odds else None,
            "draw_odds": market_odds[1] if market_odds else None,
            "away_odds": market_odds[2] if market_odds else None,
            "odds_synthetic": _odds_synthetic,
            "handicap": fixture.handicap,
            # 置信度
            "confidence": round(pred.confidence * trust_score, 4),
            "wilson_trust": round(trust_score, 4),
            # 模型信号分解
            "model_raw": {
                "home": round(pred.home_win_prob, 4),
                "draw": round(pred.draw_prob, 4),
                "away": round(pred.away_win_prob, 4),
            },
            "market_fair": (
                [round(x, 4) for x in calibrated_probs] if calibrated_probs else None
            ),
            # 概率分布（优先DJYY模型，fallback到MC模拟）
            "top_scores": (
                djyy_data.get("top_scores") if djyy_data and djyy_data.get("top_scores")
                else getattr(pred, "top_scores", None)
            ),
            "total_goals": (
                (lambda tg: tg if isinstance(tg, list) else (
                    [[int(float(k)), v[1] if isinstance(v, list) and len(v)>1 else (v if isinstance(v,(int,float)) else 0)] 
                     for k, v in tg.items()] if isinstance(tg, dict) else None
                ))(
                    djyy_data.get("totals") if djyy_data and djyy_data.get("totals")
                    else getattr(pred, "top_total_goals", None)
                )
            ),
            # 半全场概率
            "htft": _htft,
            "htft_top3": top_htft(_htft),
            # 逆向赔率
            "reverse_upset_risk": (
                reverse_result.direction.upset_risk if reverse_result else None
            ),
            "reverse_direction": (
                reverse_result.direction.label if reverse_result and hasattr(reverse_result.direction, 'label') else None
            ),
            "reverse_compression": (
                round(reverse_result.compression_ratio, 3) if reverse_result and hasattr(reverse_result, 'compression_ratio') else None
            ),
            # 同赔分析
            "same_odds_matched": (
                same_odds_result.matched_count if same_odds_result else 0
            ),
            "same_odds_confidence": (
                round(same_odds_result.confidence, 3) if same_odds_result else 0
            ),
            "same_odds_bias": (
                [round(same_odds_result.home_bias, 3), round(same_odds_result.draw_bias, 3), round(same_odds_result.away_bias, 3)]
                if same_odds_result else None
            ),
            # 组合加分
            "combo_boost": combo_boost,
            # DJYY增强
            "djyy_enriched": bool(djyy_probs and djyy_probs.get("home")),
            "djyy_model_prob": (
                djyy_probs if djyy_probs and djyy_probs.get("home") else None
            ),
            "_djyy_id": djyy_data.get("djyy_id") if djyy_data else None,
            # Elo
            "elo_home": round(home_rating.elo, 1),
            "elo_away": round(away_rating.elo, 1),
            # 平局预警
            "draw_alert": draw_alert,
            # 开赛时间（新浪有就用新浪的完整时间，否则用体彩 matchDate+matchTime）
            "kickoff": (_sina_data.get("match_time") if _sina_data else "") or fixture.kickoff,
            # 竞彩编号（如"周六001"），与新浪/赛果匹配的稳定键
            "match_no": fixture.match_id.split("_", 1)[-1] if "_" in fixture.match_id else "",
            # 新浪赔率数据（初始+即时+变化方向+压缩比+亚盘+大小球）
            "sina_odds": _sina_data,
            # 方向：预测时即写入（最终概率 argmax + 平局改判），与结算口径一致
            # （修复：预测时 direction 为空，页面/复盘拿不到方向）
            "direction": _pick_direction(final_h, final_d, final_a, draw_alert),
            "direction_prob": max(final_h, final_d, final_a),
        })

    print(f"  ✓ 完成 {len(predictions)} 场预测（含增强分析）")
    if djyy_ssr_enriched:
        print(f"  DJYY SSR 增强: {djyy_ssr_enriched}/{len(predictions)} 场匹配 (Pinnacle赔率+xG)")

    # 6. 资金管理 + 投注计划
    print("\n[5/8] 资金管理与投注计划...")

    # 熔断检查
    breaker = CircuitBreaker(ROOT / "data" / "state" / "circuit_breaker.json")
    bankroll = strat_cfg.get("bankroll", 10000)
    # 用 CPPI 实际资金池（而非永远 10000）
    cppi = CPPIStrategy(
        ROOT / "data" / "state" / "cppi.json",
        initial_bankroll=bankroll,
    )
    actual_bankroll = cppi.state.current_bankroll if cppi.state.current_bankroll > 0 else bankroll
    breaker_mult = breaker.get_multiplier(actual_bankroll)
    breaker_status = breaker.status_report()
    print(f"  熔断状态: tier={breaker_status['tier']}, "
          f"streak={breaker_status['current_streak']}, "
          f"multiplier={breaker_mult}")
    print(f"  💰 资金池: {actual_bankroll:.0f}")

    # 虚拟投注：不熔断，始终正常投注

    # 自适应置信阈值（连败收紧）
    # shadow(虚拟)模式放开；真实下注模式保留最低置信 0.25，避免 0.09 置信的重注
    _activation = strat_cfg.get("activation_mode", "shadow")
    conf_threshold = 0.25 if _activation != "shadow" else 0

    # CPPI风险预算（使用已加载的 cppi 实例）
    risk_budget = cppi.get_risk_budget()
    print(f"  CPPI: 安全垫={risk_budget['cushion']}, "
          f"风险预算={risk_budget['risk_exposure']}")

    # Kelly + 三票制
    strategy = KellyStrategy(ROOT / "config" / "strategy.json")
    plan = strategy.evaluate_candidates(predictions)
    plan.date = target_date.isoformat()

    # 三票制重分配
    effective_mult = 1.0  # 虚拟投注不降注
    allocator = ThreeTicketAllocator(
        bankroll=actual_bankroll,
        breaker_multiplier=effective_mult,
        limits=strat_cfg.get("limits", {}),
    )
    candidates = []
    filtered_count = 0
    for p in predictions:
        # 自适应置信阈值过滤（连败时收紧）
        if p.get("confidence", 0) < conf_threshold:
            filtered_count += 1
            continue
        is_synthetic = p.get("odds_synthetic", False)
        max_edge = -1.0  # 记录最大期望值 (prob * odds - 1)
        # 只押模型预测方向（8/3 教训：预测 home 押 away+draw 全输）
        _direction = p.get("direction")
        if not _direction:
            _probs = (p.get("home_win_prob", 0), p.get("draw_prob", 0), p.get("away_win_prob", 0))
            _direction = ["home", "draw", "away"][_probs.index(max(_probs))]
        for sel, prob, odds_key in [
            ("home", p["home_win_prob"], "home_odds"),
            ("draw", p["draw_prob"], "draw_odds"),
            ("away", p["away_win_prob"], "away_odds"),
        ]:
            if sel != _direction:
                continue  # 禁止押反方向，保证预测与投注一致
            odds = p.get(odds_key)
            if not odds:
                continue
            
            edge = prob * odds - 1  # 期望值
            if edge > max_edge:
                max_edge = edge

            if is_synthetic:
                # 合成赔率: 用置信度推荐 (概率>50%的最优选项)
                if prob > 0.50 and prob == max(p["home_win_prob"], p["draw_prob"], p["away_win_prob"]):
                    candidates.append({
                        "match_id": p["match_id"],
                        "selection": sel,
                        "odds": odds,
                        "prob": prob,
                        "kelly_fraction": 0.05,  # 保守固定仓位
                    })
            elif edge > 0:  # 真实赔率: 正期望
                kelly_f = edge / (odds - 1) * 0.25  # quarter-Kelly
                candidates.append({
                    "match_id": p["match_id"],
                    "selection": sel,
                    "odds": odds,
                    "prob": prob,
                    "kelly_fraction": kelly_f,
                })
        
        # 写回 kelly_edge 到预测字典，解决 Kelly=0 问题
        p["kelly_edge"] = round(max_edge, 4) if max_edge > -1.0 else 0.0

    if filtered_count > 0:
        print(f"  置信过滤: {filtered_count} 场低于阈值 {conf_threshold:.2f}，已跳过")

    ticket_plan = allocator.allocate(candidates)
    print(f"  ✓ 三票方案: 稳胆{len(ticket_plan.stable_picks)}场, "
          f"搏冷{len(ticket_plan.value_picks)}场, "
          f"彩票{len(ticket_plan.lottery_picks)}场, "
          f"总投入={ticket_plan.total_stake}元")

    # 7. 创建决策包 + 锁定
    print("\n[6/8] 创建不可变决策包...")
    bundle_mgr = DecisionBundle(ROOT / "data" / "daily" / target_date.isoformat())
    bundle = bundle_mgr.create(
        date_str=target_date.isoformat(),
        import_manifest=manifest.__dict__,
        predictions=predictions,
        betting_plan={
            "singles": [{"match_id": s.match_id, "selection": s.selection,
                         "stake": s.stake, "odds": s.odds} for s in plan.singles],
            "three_ticket": allocator.summary(ticket_plan),
            "breaker_status": breaker_status,
            "cppi_budget": risk_budget,
            "total_stake": plan.total_stake,
        },
        config_prediction=pred_cfg,
        config_strategy=strat_cfg,
    )
    print(f"  ✓ 决策包 SHA-256: {bundle['bundle_sha256'][:16]}...")
    # 清理旧版本决策包（保留最新3个），防止30分钟一次的流水线无限堆积
    try:
        _pruned = bundle_mgr.prune_old_versions(target_date.isoformat(), keep=3)
        if _pruned:
            print(f"  🧹 已清理 {_pruned} 个旧决策包版本")
    except Exception as _e:
        print(f"  ⚠ 决策包清理跳过: {_e}")

    # 8. 锁定计划
    print("\n[7/8] 锁定计划...")
    if predict_only:
        print("  ⏭ --predict-only 模式，跳过锁定")
    else:
        lock_mgr = PlanLock(ROOT / "data" / "daily" / target_date.isoformat())
        if not lock_mgr.is_locked(target_date.isoformat()):
            import hashlib
            plan_hash = hashlib.sha256(
                json.dumps([s.__dict__ for s in plan.singles], default=str).encode()
            ).hexdigest()
            lock_mgr.lock(
                date_str=target_date.isoformat(),
                plan_hash=plan_hash,
                bundle_hash=bundle["bundle_sha256"],
            )
            print(f"  ✓ 计划已锁定")
        else:
            print(f"  ⚠ 计划已存在锁定，跳过")

    # 保存预测结果
    print("\n[8/8] 保存结果...")
    output_dir = ROOT / "data" / "daily" / target_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "predictions.json").write_text(
        json.dumps(predictions, indent=2, ensure_ascii=False)
    )
    (output_dir / "ticket_plan.json").write_text(
        json.dumps(allocator.summary(ticket_plan), indent=2, ensure_ascii=False)
    )

    print(f"\n{'='*60}")
    print(f"  流水线完成 ✓")
    print(f"  预测: {len(predictions)} 场")
    print(f"  投注: {ticket_plan.total_stake} 元 (乘数={effective_mult:.2f})")
    print(f"{'='*60}")

    match_db.close()
    return predictions, plan


def run_settlement(target_date: date):
    """执行结算 + Elo 更新 + 熔断记录 + 组合挖掘更新（幂等）

    幂等设计（历史反复出问题的核心修复）：
      - 同一场比赛不会重复结算：以 (目录日期, 主队, 客队) 为幂等键，
        已存在于任意日期 results.json 的比赛跳过 Elo/熔断/权重/组合更新。
      - 赛果按"全局竞彩编号 → 预测所在目录"落盘，不再依赖接口返回的日期推断，
        解决跨周/跨日赛果存错目录导致的复盘漏结算。
      - review.json 每次结算都重新生成（不再被"已存在即跳过"冻结），
        复盘数据始终反映最新赛果。
      - 无新赛果时跳过重型校准（Temperature/Rho/Walk-forward），快速退出。
    """
    print(f"\n{'='*60}")
    print(f"  结算流水线 - {target_date.isoformat()}")
    print(f"{'='*60}")

    daily_root = ROOT / "data" / "daily"

    # 1) 全局预测索引：竞彩编号 → (目录日期, 完整match_id)；match_id → pred；队名 → pred
    pno_place: dict[str, tuple[str, str]] = {}
    pred_by_mid: dict[str, dict] = {}
    pred_by_team: dict[str, dict] = {}
    _norm = lambda s: (s or "").replace("迈阿密", "迈").replace("国际", "").replace("罗姆", "").replace("体育", "").replace("竞技", "").strip()
    if daily_root.exists():
        for folder in sorted(daily_root.iterdir()):
            if not folder.is_dir():
                continue
            pf = folder / "predictions.json"
            if not pf.exists():
                continue
            try:
                _preds = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            for _p in _preds:
                _mid = _p.get("match_id", "")
                if _mid:
                    pred_by_mid.setdefault(_mid, _p)
                    _pno = _mid.split("_", 1)[-1] if "_" in _mid else ""
                    if _pno:
                        pno_place.setdefault(_pno, (folder.name, _mid))
                _hk, _ak = _p.get("home_team", ""), _p.get("away_team", "")
                if _hk and _ak:
                    pred_by_team.setdefault(f"{_hk}_vs_{_ak}", _p)
                    pred_by_team.setdefault(f"{_norm(_hk)}_vs_{_norm(_ak)}", _p)
    print(f"  ✓ 全局预测索引: 编号 {len(pno_place)} / match_id {len(pred_by_mid)} / 队名 {len(pred_by_team)}")

    # 2) 幂等键：所有目录 results.json 中已记录的 (目录日期, 主队, 客队) → 比分
    #    比分也记录：若同一场比赛比分发生变化（如"进行中 0-0"被修正为终场 3-0），
    #    视为新增重新结算，而不是永远被旧记录挡住。
    settled_pairs: dict[tuple[str, str, str], tuple] = {}
    if daily_root.exists():
        for folder in daily_root.iterdir():
            if not folder.is_dir():
                continue
            rj = folder / "results.json"
            if not rj.exists():
                continue
            try:
                _rs = json.loads(rj.read_text(encoding="utf-8"))
            except Exception:
                continue
            for _x in _rs:
                if _x.get("home_team") and _x.get("away_team"):
                    settled_pairs[(folder.name, _x.get("home_team"), _x.get("away_team"))] = (
                        _x.get("home_score"), _x.get("away_score"))
    print(f"  ✓ 已结算基准: {len(settled_pairs)} 条 (results.json 幂等保护)")

    source_mgr = SourceManager(ROOT / "data")
    results = source_mgr.fetch_results(target_date)

    # 合并新浪赛果（互补数据源，同队名比分不同 → 覆盖修正）
    sina_file = ROOT / "data" / "daily" / target_date.isoformat() / "results_sina.json"
    if sina_file.exists():
        try:
            sina_results_raw = json.loads(sina_file.read_text(encoding="utf-8"))
            existing_teams = {(r.home_team, r.away_team): (r.home_score, r.away_score) for r in results}
            sina_added = 0
            sina_fixed = 0
            for sr in sina_results_raw:
                tkey = (sr.get("home_team"), sr.get("away_team"))
                if tkey in existing_teams:
                    # 同队名已存在：比分不同则用新浪赛果修正（终场为准）
                    if existing_teams[tkey] != (sr.get("home_score"), sr.get("away_score")):
                        for r in results:
                            if (r.home_team, r.away_team) == tkey:
                                print(f"  ↻ 赛果修正: {tkey[0]} vs {tkey[1]} "
                                      f"{existing_teams[tkey][0]}-{existing_teams[tkey][1]} → "
                                      f"{sr['home_score']}-{sr['away_score']}")
                                r.home_score = sr["home_score"]
                                r.away_score = sr["away_score"]
                                r.match_no = sr.get("match_no", r.match_no)
                                break
                        existing_teams[tkey] = (sr["home_score"], sr["away_score"])
                        sina_fixed += 1
                    continue
                results.append(MatchResult(
                    match_id=sr.get("match_id", f"{sr['home_team']}_vs_{sr['away_team']}"),
                    home_team=sr["home_team"],
                    away_team=sr["away_team"],
                    home_score=sr["home_score"],
                    away_score=sr["away_score"],
                    match_date=target_date.isoformat(),
                    competition=sr.get("league", ""),
                    match_no=sr.get("match_no", ""),
                ))
                existing_teams[tkey] = (sr["home_score"], sr["away_score"])
                sina_added += 1
            print(f"  ✓ 新浪补充: {sina_added} 新增, {sina_fixed} 比分修正 (total={len(results)})")
        except Exception as e:
            print(f"  ⚠ 新浪赛果合并失败: {e}")

    if not results:
        # 兜底: 外部赛果接口为空时，用本地 results.json 重建复盘（老日期也可用）。
        # 幂等保护仍在: 这些比赛已存在于 results.json，Elo/熔断/权重不会重复更新。
        _local = daily_root / target_date.isoformat() / "results.json"
        if _local.exists():
            try:
                _lr = json.loads(_local.read_text(encoding="utf-8"))
                for _x in _lr:
                    results.append(MatchResult(
                        match_id=_x.get("match_id", ""),
                        home_team=_x.get("home_team", ""),
                        away_team=_x.get("away_team", ""),
                        home_score=_x.get("home_score"),
                        away_score=_x.get("away_score"),
                        match_date=target_date.isoformat(),
                    ))
                print(f"  ✓ 外部赛果为空，使用本地 results.json 兜底: {len(results)} 场")
            except Exception as _e:
                print(f"  ⚠ 本地 results.json 读取失败: {_e}")
    if not results:
        print("  ⚠ 无比赛结果")
        return

    # 3) 归一化：确定每场比赛的 (目录日期, 完整match_id, 是否新增)
    norm = []
    for r in results:
        _rno = getattr(r, "match_no", "") or ""
        _placed = pno_place.get(_rno) if _rno else None
        if _placed:
            r_date, r_mid = _placed
        else:
            r_mid = getattr(r, "match_id", "") or ""
            r_date = ""
            if r_mid and "_" in r_mid and r_mid[:4].isdigit():
                r_date = r_mid.split("_")[0]
            if not r_date:
                r_date = getattr(r, "match_date", "") or target_date.isoformat()
        key = (r_date, r.home_team, r.away_team)
        old_score = settled_pairs.get(key)
        # 新增判定：无记录，或比分与已记录不同（进行中误抓被修正为终场）→ 重新结算
        is_new = old_score is None or old_score != (r.home_score, r.away_score)
        norm.append({
            "date": r_date, "match_id": r_mid, "r": r,
            "is_new": is_new, "key": key,
            "pred": pred_by_mid.get(r_mid) or pred_by_team.get(f"{r.home_team}_vs_{r.away_team}")
                    or pred_by_team.get(f"{_norm(r.home_team)}_vs_{_norm(r.away_team)}"),
        })

    # 3.5) 同一预测合并：DJYY 与新浪可能对同一场比赛返回不同队名/比分
    #     （如"佐加顿斯 vs 韦斯特罗 5-0" vs "佐加顿斯 vs 瓦斯特拉斯 6-0"）。
    #     以预测的竞彩编号为比赛身份，合并多条赛果：带竞彩编号(match_no)的新浪赛果优先，
    #     避免错误比分(5-0)覆盖权威终场比分(6-0)，也避免同一场双条目重复计 Elo。
    #     match_id 格式可能不同（"2026-08-03_周一001" vs "周一001"），统一取编号段。
    def _num_of(mid: str) -> str:
        return mid.split("_", 1)[-1] if mid and "_" in mid else (mid or "")

    _by_pred: dict = {}
    for n in norm:
        _k = (n["date"], _num_of(n["match_id"]))
        if _k not in _by_pred:
            _by_pred[_k] = n
            continue
        old = _by_pred[_k]
        _r, _nr = old["r"], n["r"]
        if _nr.match_no and not _r.match_no:
            _by_pred[_k] = n  # 新浪赛果替换 DJYY 赛果
        elif _nr.match_no and _r.match_no and \
                (_r.home_score, _r.away_score) != (_nr.home_score, _nr.away_score):
            # 两条都带编号但比分不同（如旧进行中 0-0 vs 终场 3-0）：取比分更新的
            # 无法判断新旧，保守取非零比分优先；仍冲突则保留先到的
            if (_nr.home_score, _nr.away_score) not in ((0, 0),) and (_r.home_score, _r.away_score) == (0, 0):
                _by_pred[_k] = n
    if len(_by_pred) < len(norm):
        print(f"  ✓ 同一预测赛果合并: {len(norm)} → {len(_by_pred)} 条")
    norm = list(_by_pred.values())
    # 合并后按 (日期, 队名) 再兜底去重：极端情况下不同编号但同队名的重复
    _seen_team: set = set()
    _dedup = []
    for n in norm:
        _tk = (n["date"], n["r"].home_team, n["r"].away_team)
        if _tk in _seen_team:
            continue
        _seen_team.add(_tk)
        _dedup.append(n)
    if len(_dedup) < len(norm):
        print(f"  ✓ 队名兜底去重: {len(norm)} → {len(_dedup)} 条")
    norm = _dedup
    new_items = [n for n in norm if n["is_new"]]
    print(f"  ✓ 赛果 {len(norm)} 场, 其中新增 {len(new_items)} 场（其余已结算过，跳过重复处理）")

    # 4) Elo 更新（只处理新增）
    print("\n[1/5] Elo 更新...")
    elo_updater = EloUpdater(ROOT / "data" / "models" / "team_ratings.json")
    for n in new_items:
        r = n["r"]
        elo_updater.update(r.home_team, r.away_team, r.home_score, r.away_score)
        print(f"  {r.home_team} {r.home_score}-{r.away_score} {r.away_team} ✓")
    elo_updater.save()
    print(f"  ✓ Elo 已更新 ({len(new_items)} 场新增)")

    # 5) 保存结果到 results.json（按目录日期，追加去重）
    results_by_date: dict[str, list] = {}
    for n in norm:
        r = n["r"]
        results_by_date.setdefault(n["date"], []).append({
            "match_id": n["match_id"] or f"{r.home_team}_vs_{r.away_team}",
            "home_score": r.home_score,
            "away_score": r.away_score,
            "home_team": r.home_team,
            "away_team": r.away_team,
        })
    stored_total = 0
    for r_date, r_list in results_by_date.items():
        r_dir = daily_root / r_date
        r_dir.mkdir(parents=True, exist_ok=True)
        r_file = r_dir / "results.json"
        existing = []
        if r_file.exists():
            try:
                existing = json.loads(r_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing_ids = {e.get("match_id") for e in existing}
        existing_by_team = {(e.get("home_team"), e.get("away_team")): e for e in existing}
        added = 0
        fixed = 0
        for item in r_list:
            _tkey = (item["home_team"], item["away_team"])
            old = existing_by_team.get(_tkey)
            if old is not None:
                # 同队名已存在：比分不同则覆盖（终场比分修正进行中误抓）
                if (old.get("home_score"), old.get("away_score")) != (item["home_score"], item["away_score"]):
                    old["home_score"] = item["home_score"]
                    old["away_score"] = item["away_score"]
                    old["match_id"] = item["match_id"] or old.get("match_id", "")
                    fixed += 1
                continue
            if item["match_id"] in existing_ids:
                # 同 match_id（同预测）已存在但队名不同（DJYY/新浪译名差异）：
                # 比分不同则覆盖，避免旧/错比分残留
                _hit = False
                for e in existing:
                    if e.get("match_id") == item["match_id"] and \
                            (e.get("home_score"), e.get("away_score")) != (item["home_score"], item["away_score"]):
                        e["home_score"] = item["home_score"]
                        e["away_score"] = item["away_score"]
                        e["home_team"] = item["home_team"]
                        e["away_team"] = item["away_team"]
                        fixed += 1
                        _hit = True
                        break
                if not _hit:
                    continue
                continue
            existing.append(item)
            existing_by_team[_tkey] = item
            existing_ids.add(item["match_id"])
            added += 1
        if added or fixed:
            r_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        stored_total += added + fixed
        print(f"  ✓ results.json 已保存到 {r_date} (+{added}, total={len(existing)})")
    print(f"  ✓ 赛果落盘: 新增 {stored_total} 条")

    # 6) MatchDB 数据积累（只处理新增）
    print("\n[1.5/5] MatchDB 数据积累...")
    pred_cfg = load_config("prediction")
    db = MatchDB(ROOT / "data" / "state" / "match_history.db")
    db_recorded = 0
    for n in new_items:
        r = n["r"]
        pred = n["pred"]
        # 尝试获取DJYY赛后真实xG
        actual_xg = None
        djyy_id = pred.get("_djyy_id") if pred else None
        if djyy_id:
            try:
                actual_xg = source_mgr._djyy.fetch_post_match_xg(djyy_id)
            except Exception:
                pass
            # 存储球员xG (积累关键球员数据)
            try:
                lineups = source_mgr._djyy.fetch_match_lineups(djyy_id)
                if lineups and lineups.get("available"):
                    league = pred.get("competition", "unknown") if pred else "unknown"
                    for side, team in [("home", r.home_team), ("away", r.away_team)]:
                        side_data = lineups.get(side, {})
                        players = []
                        for p in (side_data.get("starting") or []) + (side_data.get("bench") or []):
                            if p.get("xg") is not None:
                                players.append({
                                    "name": p.get("name_zh") or p.get("name"),
                                    "position": p.get("position"),
                                    "xg": p.get("xg"),
                                    "xgot": p.get("xgot"),
                                    "rating": p.get("rating"),
                                    "minutes": p.get("minutes"),
                                })
                        if players:
                            db.record_lineup_xg(team, league, n["date"], players)
            except Exception:
                pass

        # 记录到match_history
        if pred:
            db.record_match({
                "match_id": pred.get("match_id", f"{r.home_team}_vs_{r.away_team}"),
                "date": n["date"],
                "league": pred.get("competition"),
                "home_team": r.home_team,
                "away_team": r.away_team,
                "pred_home_prob": pred.get("home_win_prob"),
                "pred_draw_prob": pred.get("draw_prob"),
                "pred_away_prob": pred.get("away_win_prob"),
                "pred_home_xg": pred.get("home_xg"),
                "pred_away_xg": pred.get("away_xg"),
                "pred_top_score": pred.get("top_scores", [])[:1],
                "score_home": r.home_score,
                "score_away": r.away_score,
                "actual_home_xg": actual_xg.get("home_xg") if actual_xg else None,
                "actual_away_xg": actual_xg.get("away_xg") if actual_xg else None,
                "ht_home": actual_xg.get("ht_home") if actual_xg else None,
                "ht_away": actual_xg.get("ht_away") if actual_xg else None,
                "djyy_id": djyy_id,
            })
            db_recorded += 1

        # 更新球队赛季统计（无论有无预测都记录）
        league = pred.get("competition", "unknown") if pred else "unknown"
        home_xg = actual_xg.get("home_xg") if actual_xg else None
        away_xg = actual_xg.get("away_xg") if actual_xg else None
        db.update_team_stats(
            team_name=r.home_team, league=league,
            goals_for=r.home_score, goals_against=r.away_score,
            xg_for=home_xg, xg_against=away_xg,
        )
        db.update_team_stats(
            team_name=r.away_team, league=league,
            goals_for=r.away_score, goals_against=r.home_score,
            xg_for=away_xg, xg_against=home_xg,
        )

    # 同步联赛基线（从DJYY league-matrix）
    try:
        matrix = source_mgr.get_league_params()
        if matrix and isinstance(matrix, list):
            db.sync_league_baselines(matrix)
            print(f"  联赛基线已同步: {len(matrix)} 个联赛")
    except Exception:
        pass

    print(f"  ✓ MatchDB: {db_recorded} 场新增记录")
    db.close()

    # 7) 熔断 + 逐场结算（只处理新增）
    print("\n[2/5] 熔断 + 信任更新...")
    breaker = CircuitBreaker(ROOT / "data" / "state" / "circuit_breaker.json")
    strat_cfg = load_config("strategy")
    bankroll = strat_cfg.get("bankroll", 10000)
    # 从 CPPI 加载当前资金池（而非每次重置为 10000）
    cppi = CPPIStrategy(ROOT / "data" / "state" / "cppi.json", initial_bankroll=bankroll)
    running_bankroll = cppi.state.current_bankroll if cppi.state.current_bankroll > 0 else bankroll
    print(f"  💰 当前资金池: {running_bankroll:.0f}")

    # 读取投注计划（该日期目录的 ticket_plan）
    daily_dir = daily_root / target_date.isoformat()
    ticket_file = daily_dir / "ticket_plan.json"
    ticket_data = {}
    if ticket_file.exists():
        ticket_data = json.loads(ticket_file.read_text())
    ticket_map = {}
    for grp in ("stable", "value", "lottery"):
        for s in ticket_data.get(grp, []):
            ticket_map[s.get("match")] = s

    total_pnl = 0.0
    wins = 0
    losses = 0
    # 联赛自适应：结算时把每场赛果喂给联赛管理器（修复 league_params.json 永为空）
    league_mgr = LeagueParamsManager(ROOT / "data" / "state" / "league_params.json")
    league_fed = 0
    for n in new_items:
        r = n["r"]
        pred = n["pred"]
        if not pred:
            continue
        # 判断赛果
        if r.home_score > r.away_score:
            actual = "home"
        elif r.home_score == r.away_score:
            actual = "draw"
        else:
            actual = "away"
        # 检查是否命中（基于最大概率选项）
        best_sel = max(
            [("home", pred["home_win_prob"]),
             ("draw", pred["draw_prob"]),
             ("away", pred["away_win_prob"])],
            key=lambda x: x[1],
        )
        won = best_sel[0] == actual
        # 联赛参数记录：方向命中反馈（用本循环已算出的 won，避免 direction 未回写时误判）
        lg_name = pred.get("competition") or r.competition or "未知"
        try:
            league_mgr.record_result(league=lg_name, hit=won)
            league_fed += 1
        except Exception:
            pass
        # 计算PnL（基于Kelly plan）
        pnl = 0.0
        s = ticket_map.get(pred["match_id"])
        if s:
            if s.get("sel") == actual:
                pnl += s["stake"] * (s["odds"] - 1)
            else:
                pnl -= s["stake"]
        total_pnl += pnl
        if won:
            wins += 1
        else:
            losses += 1
        breaker.record_result(won=won, pnl=pnl, bankroll=running_bankroll)
        running_bankroll += pnl
    print(f"  ✓ 命中 {wins}/{wins+losses}, PnL={total_pnl:.2f}")
    print(f"  熔断状态: {breaker.status_report()}")

    # 联赛自适应调参（命中率<45% → 更信任市场；>60% → 更信任模型）
    if league_fed > 0:
        try:
            league_mgr.adapt_all()
            print(f"  ✓ 联赛自适应完成（{league_fed} 场反馈，{len(league_mgr.summary())} 个联赛）")
        except Exception as e:
            print(f"  ⚠️ 联赛自适应跳过: {e}")

    # 8) 在线权重学习 + 组合挖掘 + 赛果回写（只处理新增）
    if new_items:
        print("\n[2.5/5] 在线权重学习更新...")
        weight_learner = OnlineWeightLearner(ROOT / "data" / "state" / "online_weights.json")
        for n in new_items:
            r, pred = n["r"], n["pred"]
            if not pred:
                continue
            if r.home_score > r.away_score:
                actual_idx = 0  # home
            elif r.home_score == r.away_score:
                actual_idx = 1  # draw
            else:
                actual_idx = 2  # away
            # Brier Score: sum of (prob - actual)^2 for all 3 outcomes
            probs = [pred["home_win_prob"], pred["draw_prob"], pred["away_win_prob"]]
            actuals = [0.0, 0.0, 0.0]
            actuals[actual_idx] = 1.0
            brier = sum((p - a) ** 2 for p, a in zip(probs, actuals))
            best_sel_idx = probs.index(max(probs))
            hit = best_sel_idx == actual_idx
            # 更新ensemble整体表现
            weight_learner.update("ensemble", brier=brier, hit=hit)
        print(f"  ✓ 权重学习已更新: {weight_learner.get_weights()}")

        print("\n[3/5] 组合挖掘更新...")
        combo_miner = ComboMiner(ROOT / "data" / "state" / "combo_stats.json")
        for n in new_items:
            r, pred = n["r"], n["pred"]
            if not pred:
                continue
            if r.home_score > r.away_score:
                actual = "home"
            elif r.home_score == r.away_score:
                actual = "draw"
            else:
                actual = "away"
            best_sel = max(
                [("home", pred["home_win_prob"]),
                 ("draw", pred["draw_prob"]),
                 ("away", pred["away_win_prob"])],
                key=lambda x: x[1],
            )
            won = best_sel[0] == actual
            features = {
                "league": pred.get("competition", "unknown"),
                "prob_band": _prob_band(best_sel[1]),
                "odds_band": _odds_band(pred.get(f"{best_sel[0]}_odds", 2.0)),
            }
            combo_miner.record(features, won=won)
        print(f"  ✓ 组合统计已更新")

        # 将赛果写回 predictions.json（自愈: 写回预测所在目录）
        print("\n[3.5/5] 更新预测赛果...")
        _touch: dict[str, list] = {}
        for n in new_items:
            r, pred = n["r"], n["pred"]
            if not pred:
                continue
            pred["actual_result"] = f"{r.home_score}-{r.away_score}"
            pred["actual_home_score"] = r.home_score
            pred["actual_away_score"] = r.away_score
            best_sel = max(
                [("home", pred["home_win_prob"]),
                 ("draw", pred["draw_prob"]),
                 ("away", pred["away_win_prob"])],
                key=lambda x: x[1],
            )
            # 平局盲点修复：模型已预警平局风险(draw_alert) 且 平局概率接近最高(<8pt) 时，
            # direction 改判平局（否则纯 argmax 永远只选 H/A，109场只判2场平局 vs 实际29%平局率）
            if pred.get("draw_alert") and best_sel[0] != "draw":
                _best_p = best_sel[1]
                _draw_p = pred.get("draw_prob", 0)
                if _best_p - _draw_p < 0.08 and _draw_p >= 0.26:
                    best_sel = ("draw", _draw_p)
            if r.home_score > r.away_score:
                actual = "home"
            elif r.home_score == r.away_score:
                actual = "draw"
            else:
                actual = "away"
            pred["direction"] = best_sel[0]
            pred["direction_correct"] = best_sel[0] == actual
            top_scores = pred.get("top_scores", [])
            if top_scores and isinstance(top_scores[0], list):
                ps = f"{top_scores[0][0]}-{top_scores[0][1]}"
                pred["predicted_score"] = ps
                pred["score_correct"] = ps == f"{r.home_score}-{r.away_score}"
            _touch.setdefault(n["date"], []).append(pred["match_id"])
        _updated = 0
        for _d, _mids in _touch.items():
            _pf = daily_root / _d / "predictions.json"
            if not _pf.exists():
                continue
            try:
                _pl = json.loads(_pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            _by_mid = {p.get("match_id"): p for p in _pl}
            _chg = 0
            for _m in _mids:
                _src = pred_by_mid.get(_m)
                if _m in _by_mid and _src:
                    _new = _src.get("actual_result")
                    _cur = _by_mid[_m].get("actual_result")
                    # 修正条件：不仅 None 要写，比分变化（如进行中 0-0 → 终场 3-0）也要覆盖
                    if _new is not None and _new != _cur:
                        _by_mid[_m].update(_src)
                        _chg += 1
            _pf.write_text(json.dumps(_pl, ensure_ascii=False, indent=2))
            _updated += _chg
        print(f"  ✓ 已更新 {_updated} 场预测赛果")

    # 9) CPPI 更新（已在结算前加载，直接更新）
    print("\n[4/5] CPPI 资产更新...")
    cppi.update(running_bankroll)
    cppi.save()
    print(f"  ✓ 资产: {bankroll:.0f} → {running_bankroll:.0f}  (PnL: {total_pnl:+.2f})")

    # 10) 复盘（每次结算都重新生成，绝不因 review.json 存在而冻结）
    print("\n[5/5] 赛后复盘...")
    from engine.review.post_match import PostMatchReviewer, ReviewLedger
    reviewer = PostMatchReviewer(ROOT / "data", pred_cfg.get("review", {}))
    review_report = reviewer.review_day(target_date.isoformat())
    if review_report.get("n_matches", 0) > 0:
        print(f"  ✓ 复盘: {review_report['n_matches']}场, 命中率{review_report.get('hit_rate', 0):.0%}")
        src_b = review_report.get("source_brier", {})
        print(f"    Brier: model={src_b.get('model', '?')} market={src_b.get('market', '?')} djyy={src_b.get('djyy', '?')} final={src_b.get('final', '?')}")
        for bias in review_report.get("biases", []):
            print(f"    ⚠ 偏差: {bias['dimension']}:{bias['key']} {bias['outcome']} gap={bias['gap']:+.3f}")
    else:
        print(f"  - 无可复盘数据")

    # 11) 重型校准：仅在有新赛果时执行（避免每次定时任务都跑全套）
    if not new_items:
        print("\n  ⏭ 无新增赛果，跳过校准/优化步骤（快速退出）")
        print(f"\n{'='*60}")
        print(f"  结算完成 ✓ (无新增)")
        print(f"{'='*60}")
        return

    print("\n[6/6] 融合权重优化...")
    ledger = ReviewLedger(ROOT / "data" / "state" / "review_ledger.jsonl")
    fusion_opt = FusionOptimizer(
        ROOT / "data" / "state" / "fusion_weights.json",
        ledger,
        pred_cfg.get("optimizer", {}),
    )
    decision = fusion_opt.step()
    print(f"  决策: {decision.action} | 权重: {decision.champion}")
    print(f"  原因: {decision.reason}")
    if decision.guard_rails_applied:
        print(f"  守卫: {decision.guard_rails_applied}")

    # Temperature Scaling 重新拟合
    print("\n  [校准更新] Temperature Scaling...")
    ledger_path = ROOT / "data" / "state" / "review_ledger.jsonl"
    all_records = []
    if ledger_path.exists():
        for line in ledger_path.read_text().strip().split("\n"):
            if line.strip():
                try:
                    all_records.append(json.loads(line))
                except Exception:
                    continue
    if len(all_records) >= 30:
        ts_probs = np.array([r.get("final_prob", [0.33, 0.34, 0.33]) for r in all_records])
        ts_actuals = np.array([r.get("actual_idx", 0) for r in all_records])
        temp_scaler = TemperatureScaler(ROOT / "data" / "models" / "temperature.json")
        temp_scaler.fit(ts_probs, ts_actuals)
    else:
        print(f"    样本不足 ({len(all_records)} < 30)")

    # Rho MLE 拟合
    print("\n  [校准更新] Rho MLE...")
    rho_fitter = RhoFitter(ROOT / "data" / "state" / "match_history.db")
    rho_result = rho_fitter.fit()
    if rho_result["rho"] is not None:
        # 写入 config
        config_path = ROOT / "config" / "prediction.json"
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
            cfg.setdefault("prediction", {})["rho"] = rho_result["rho"]
            config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            print(f"    ✓ rho={rho_result['rho']} 已写入 config")

    # Walk-forward 回测
    print("\n  [校准更新] Walk-forward 回测...")
    from engine.backtest.walk_forward import WalkForwardEvaluator
    wf_eval = WalkForwardEvaluator(ROOT / "data")
    wf_report = wf_eval.evaluate()
    wf_path = ROOT / "data" / "state" / "walk_forward_report.json"
    wf_path.write_text(json.dumps(wf_report, indent=2, ensure_ascii=False))
    metrics = wf_report.get("metrics", {})
    draw_a = wf_report.get("draw_analysis", {})
    strat = wf_report.get("strategy_comparison", {})
    print(f"    命中率: {metrics.get('hit_rate', 0):.1%} | "
          f"Brier: {metrics.get('brier', 0):.4f} | "
          f"RPS: {metrics.get('rps', 0):.4f} | "
          f"ECE: {metrics.get('ece', 0):.4f}")
    print(f"    平局: 实际{draw_a.get('actual_draw_rate', 0):.0%} "
          f"预测{draw_a.get('predicted_draw_rate', 0):.0%} "
          f"最高{draw_a.get('max_draw_prob', 0):.0%}")
    best_strat = max(strat.items(), key=lambda x: x[1]["hit_rate"])
    print(f"    最优策略: {best_strat[0]} ({best_strat[1]['hit_rate']:.1%})")

    print(f"\n{'='*60}")
    print(f"  结算完成 ✓ ({len(new_items)} 场新增)")
    print(f"{'='*60}")


def _extract_features(fixture, pred) -> dict:
    """从比赛和预测中提取离散特征（用于组合挖掘）"""
    features = {
        "league": fixture.competition or "unknown",
        "prob_band": _prob_band(max(pred.home_win_prob, pred.draw_prob, pred.away_win_prob)),
    }
    if fixture.home_odds:
        features["odds_band"] = _odds_band(fixture.home_odds)
    if fixture.handicap is not None:
        features["handicap"] = str(fixture.handicap)
    return features


def _prob_band(prob: float) -> str:
    """概率分档"""
    if prob >= 0.65:
        return "high"
    elif prob >= 0.45:
        return "mid"
    else:
        return "low"


def _odds_band(odds: float) -> str:
    """赔率分档"""
    if odds < 1.5:
        return "1.0-1.5"
    elif odds < 2.0:
        return "1.5-2.0"
    elif odds < 3.0:
        return "2.0-3.0"
    elif odds < 5.0:
        return "3.0-5.0"
    else:
        return "5.0+"


def main():
    parser = argparse.ArgumentParser(description="Football Engine")
    parser.add_argument("--date", default="today", help="目标日期 (YYYY-MM-DD 或 today)")
    parser.add_argument("--settle", action="store_true", help="执行结算")
    parser.add_argument("--predict-only", action="store_true", help="仅预测不锁定")
    parser.add_argument("--backtest", action="store_true", help="回测历史表现")
    args = parser.parse_args()

    if args.backtest:
        from engine.backtest.runner import BacktestRunner
        runner = BacktestRunner(ROOT / "data")
        report = runner.run()
        print(report.summary())
        # 保存报告
        out = ROOT / "data" / "state" / "backtest_report.json"
        out.write_text(json.dumps({
            "n_matches": report.n_matches,
            "n_days": report.n_days,
            "hit_rate": report.hit_rate,
            "avg_brier": report.avg_brier,
            "roi": report.roi,
            "total_pnl": report.total_pnl,
            "by_league": report.by_league,
            "by_confidence": report.by_confidence,
            "calibration": report.calibration,
            "source_comparison": report.source_comparison,
        }, indent=2, ensure_ascii=False))
        print(f"\n报告已保存: {out}")
        return

    if args.date == "today":
        target = date.today()
    else:
        target = date.fromisoformat(args.date)

    if args.settle:
        run_settlement(target)
    else:
        run_daily_pipeline(target, predict_only=args.predict_only)


if __name__ == "__main__":
    main()
