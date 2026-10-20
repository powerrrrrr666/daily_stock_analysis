# -*- coding: utf-8 -*-
"""每日「股票推荐」栏目：全市场选股后输出可买标的。

设计原则（重要）：
- **绝不阻断主流程**：选股涉及全市场快照与外部数据源，稳定性远低于自选股分析。
  任何异常都只记录日志并返回 None，主报告（个股+大盘）照常生成与推送。
- 可通过环境变量关闭：``DAILY_PICKS_ENABLED=false``
- 策略、数量均可配置：``DAILY_PICKS_STRATEGY`` / ``DAILY_PICKS_COUNT``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 策略 key -> 中文显示名（仅用于报告标题）
STRATEGY_DISPLAY_NAMES: Dict[str, str] = {
    "bull_trend": "多头趋势",
    "box_oscillation": "箱体震荡",
    "bottom_volume": "底部放量",
    "ma_golden_cross": "均线金叉",
    "volume_breakout": "量能突破",
    "shrink_pullback": "缩量回调",
    "hot_theme": "热点题材",
    "dragon_head": "龙头战法",
    "event_driven": "事件驱动",
    "growth_quality": "成长质量",
    "chan_theory": "缠论",
    "wave_theory": "波浪理论",
    "emotion_cycle": "情绪周期",
    "expectation_repricing": "预期重估",
    "one_yang_three_yin": "一阳三阴",
}

DEFAULT_STRATEGY = "bull_trend"
DEFAULT_COUNT = 5


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 20) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, min(maximum, int(str(raw).strip())))
    except (TypeError, ValueError):
        logger.warning("[每日推荐] %s 值非法，回退默认 %s", name, default)
        return default


def _as_list(value: Any, limit: int = 3) -> List[str]:
    """把可能是 list/str/None 的字段统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = [str(value)]
    out: List[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _fmt_num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_to_dict(pick: Any) -> Dict[str, Any]:
    """把 screening 的 Pick 转成模板可直接用的 dict。"""
    price = _fmt_num(getattr(pick, "price", None))
    atr_pct = _fmt_num(getattr(pick, "atr_20_pct", None))
    # 参考止损：1.5 倍 20 日 ATR（若 ATR 缺失则不推算，避免编造价位）
    stop_ref = None
    if price and atr_pct and 0 < atr_pct < 30:
        stop_ref = round(price * (1 - 1.5 * atr_pct / 100.0), 2)
    return {
        "rank": getattr(pick, "rank", 0),
        "code": getattr(pick, "code", ""),
        "name": getattr(pick, "name", ""),
        "score": _fmt_num(getattr(pick, "final_score", None)),
        "price": price,
        "change_pct": _fmt_num(getattr(pick, "change_pct", None)),
        "turnover_rate": _fmt_num(getattr(pick, "turnover_rate", None)),
        "volume_ratio": _fmt_num(getattr(pick, "volume_ratio", None)),
        "industry": getattr(pick, "industry", "") or "",
        "concepts": getattr(pick, "concepts", "") or "",
        "reason": getattr(pick, "ranking_reason", "") or "",
        "risk": getattr(pick, "risk_summary", "") or "",
        "ma_bullish": getattr(pick, "ma_bullish", None),
        "above_ma20": getattr(pick, "price_above_ma20", None),
        "change_60d": _fmt_num(getattr(pick, "change_60d", None)),
        "atr_pct": atr_pct,
        "stop_ref": stop_ref,
        # LLM 层给出的更具体依据（可能为空）
        "thesis": getattr(pick, "llm_thesis", "") or "",
        "catalysts": _as_list(getattr(pick, "llm_catalysts", None)),
        "risks": _as_list(getattr(pick, "llm_risks", None)),
        "invalidators": _as_list(getattr(pick, "llm_invalidators", None)),
        "watch_items": _as_list(getattr(pick, "llm_watch_items", None)),
        "risk_flags": _as_list(getattr(pick, "risk_flags", None)),
    }


def generate_daily_picks(
    *,
    count: Optional[int] = None,
    strategy: Optional[str] = None,
    use_llm: bool = True,
) -> Optional[Dict[str, Any]]:
    """执行全市场选股，返回可直接渲染的推荐数据；任何失败都返回 None。

    注意：选股引擎使用**它自己的配置类** ``src.services.screening.config.Config``
    （由 ``from_env()`` 从 LLM_CHANNELS / LITELLM_MODEL / DEEPSEEK_API_KEY 等环境变量解析），
    与应用层 ``src.config.Config`` 不是同一类型，**不能混用**。

    Args:
        count: 推荐数量，默认取 DAILY_PICKS_COUNT 或 5
        strategy: 策略 key，默认取 DAILY_PICKS_STRATEGY 或 bull_trend
        use_llm: 是否启用选股引擎的 LLM 排序（提升质量，多一次 LLM 调用）
    """
    if not _env_bool("DAILY_PICKS_ENABLED", True):
        logger.info("[每日推荐] 已通过 DAILY_PICKS_ENABLED 关闭，跳过")
        return None

    strategy_key = (strategy or os.getenv("DAILY_PICKS_STRATEGY") or DEFAULT_STRATEGY).strip()
    pick_count = count if count is not None else _env_int("DAILY_PICKS_COUNT", DEFAULT_COUNT, 1, 10)

    try:
        from src.services.screening.config import Config as ScreeningConfig
        from src.services.screening.pipeline import screen

        pipeline_config = ScreeningConfig.from_env()
        logger.info(
            "[每日推荐] 开始全市场选股：strategy=%s, count=%s, use_llm=%s, model=%s",
            strategy_key, pick_count, use_llm, getattr(pipeline_config, "llm_model", "?"),
        )
        result = screen(
            strategy_key,
            market="cn",
            max_output=pick_count,
            use_llm=use_llm,
            daily_enrich=True,
            config=pipeline_config,
        )
    except Exception as exc:  # noqa: BLE001 - 选股失败绝不能影响主报告
        logger.warning("[每日推荐] 选股执行失败，本次不输出推荐栏目：%s: %s", type(exc).__name__, exc)
        return None

    try:
        raw_picks = list(getattr(result, "picks", []) or [])
        picks = [_pick_to_dict(p) for p in raw_picks][:pick_count]

        payload: Dict[str, Any] = {
            "strategy": strategy_key,
            "strategy_display": STRATEGY_DISPLAY_NAMES.get(strategy_key, strategy_key),
            "snapshot_count": getattr(result, "snapshot_count", 0) or 0,
            "after_filter_count": getattr(result, "after_filter_count", 0) or 0,
            "market_view": getattr(result, "llm_market_view", "") or "",
            "selection_logic": getattr(result, "llm_selection_logic", "") or "",
            "portfolio_risk": getattr(result, "llm_portfolio_risk", "") or "",
            "picks": picks,
            "degradation": _as_list(getattr(result, "degradation", None), limit=5),
            "source_errors": _as_list(getattr(result, "source_errors", None), limit=3),
        }
        logger.info(
            "[每日推荐] 选股完成：快照 %s 只 → 过滤后 %s 只 → 入选 %s 只",
            payload["snapshot_count"], payload["after_filter_count"], len(picks),
        )
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.warning("[每日推荐] 结果解析失败，本次不输出推荐栏目：%s: %s", type(exc).__name__, exc)
        return None


def render_daily_picks(payload: Optional[Dict[str, Any]], report_date: str = "") -> str:
    """把推荐数据渲染成 markdown 段落；无数据时返回空串。"""
    if not payload:
        return ""
    try:
        from src.services.report_renderer import render

        out = render(
            platform="picks",
            results=[],
            report_date=report_date or None,
            extra_context={"picks_payload": payload},
        )
        return out or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("[每日推荐] 推荐栏目渲染失败：%s: %s", type(exc).__name__, exc)
        return ""
