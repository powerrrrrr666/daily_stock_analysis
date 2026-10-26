# -*- coding: utf-8 -*-
"""个股走势预判 + 持仓股做T时机建议。

两个能力：

1. **走势预判**（``build_forecasts``）——对每只票给出未来 3~5 个交易日的方向倾向
   与预期运行区间，全部基于均线结构 / 位置 / 量能 / 距支撑压力的距离推导。
   ⚠️ 这是**规则推导的倾向判断**，不是"预测明天涨跌"，更不是投资建议。

2. **做T时机**（``build_t_plans``）——只针对有成本价的持仓股，判断当日是否值得
   高抛低吸来摊低持仓成本，并给出具体卖/买价位与预期摊薄幅度。

做T的核心前提是**当日振幅足够**（否则价差被手续费和滑点吃掉）；
破位单边下跌时做T容易"越 T 越低"，涨停单边上涨时卖出容易"T 飞"，这两种必须劝阻。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 走势预判评分阈值
BIAS_STRONG_SCORE = 3
BIAS_WEAK_SCORE = -2

# 做T参数
MIN_AMPLITUDE_FOR_T = 2.5    # 当日振幅低于该值，做T空间不够
T_RATIO = 1 / 3              # 单次做T建议比例（不超过原仓位 1/3）
NEAR_LEVEL_PCT = 2.0         # 距压力/支撑多近算"到位"
MID_ZONE_PCT = 3.0           # 距两边都超过该值 → 中间地带，边界不清

T_BUY_HIGH = "🟢 可做T（先卖后买·高抛）"
T_BUY_LOW = "🟢 可做T（先买后卖·低吸）"
T_NO = "⏸ 暂不做T"
T_FORBID_BREAK = "⚠️ 不宜做T（单边下跌）"
T_FORBID_SURGE = "⚠️ 不宜做T（单边上涨）"


def _num(value: Any) -> Optional[float]:
    """解析数字，兼容 '3.25%'、'12.3' 这类字符串。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text or text in ("N/A", "—", "-", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _norm_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    for prefix in ("SH", "SZ", "BJ", "HK", "US"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.lstrip(".")


def _market(result: Any) -> Dict[str, Optional[float]]:
    snap = getattr(result, "market_snapshot", None) or {}
    dash = getattr(result, "dashboard", None) or {}
    dp = (dash.get("data_perspective") or {}) if isinstance(dash, dict) else {}
    pp = (dp.get("price_position") or {}) if isinstance(dp, dict) else {}
    va = (dp.get("volume_analysis") or {}) if isinstance(dp, dict) else {}
    return {
        "price": _num(snap.get("price")) or _num(snap.get("close")) or _num(pp.get("current_price")),
        "pct_chg": _num(snap.get("pct_chg")),
        "amplitude": _num(snap.get("amplitude")),
        "high": _num(snap.get("high")),
        "low": _num(snap.get("low")),
        "prev_close": _num(snap.get("prev_close")),
        "ma5": _num(pp.get("ma5")),
        "ma10": _num(pp.get("ma10")),
        "ma20": _num(pp.get("ma20")),
        "support": _num(pp.get("support_level")),
        "resistance": _num(pp.get("resistance_level")),
        "volume_ratio": _num(va.get("volume_ratio")),
    }


# ---------------------------------------------------------------- 走势预判
def _forecast_one(price: float, market: Dict[str, Optional[float]]) -> Dict[str, Any]:
    ma5, ma10, ma20 = market.get("ma5"), market.get("ma10"), market.get("ma20")
    support, resistance = market.get("support"), market.get("resistance")
    vol_ratio, pct_chg = market.get("volume_ratio"), market.get("pct_chg")

    score = 0
    basis: List[str] = []

    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            score += 2
            basis.append("均线多头排列")
        elif ma5 < ma10 < ma20:
            score -= 2
            basis.append("均线空头排列")
        else:
            basis.append("均线交织")

    if ma20:
        if price > ma20:
            score += 1
            basis.append("站上MA20")
        else:
            score -= 1
            basis.append("在MA20下方")

    if ma5:
        bias5 = (price / ma5 - 1) * 100
        if bias5 >= 6:
            score -= 1
            basis.append(f"距MA5乖离+{bias5:.1f}%（过热）")
        elif bias5 <= -8:
            score += 0.5
            basis.append(f"距MA5乖离{bias5:.1f}%（超跌）")
        if price > ma5:
            score += 1
            basis.append("站上MA5")

    if vol_ratio and vol_ratio >= 1.5 and pct_chg is not None:
        if pct_chg > 0:
            score += 1
            basis.append(f"放量上涨（量比{vol_ratio:.1f}）")
        elif pct_chg < 0:
            score -= 1
            basis.append(f"放量下跌（量比{vol_ratio:.1f}）")

    if score >= BIAS_STRONG_SCORE:
        bias, emoji = "偏强", "🔺"
    elif score <= BIAS_WEAK_SCORE:
        bias, emoji = "偏弱", "🔻"
    else:
        bias, emoji = "震荡", "➖"

    if bias == "偏强":
        range_low = ma5 or support
        range_high = resistance
        note = (
            f"预计未来 3~5 个交易日在 **{round(range_low,2) if range_low else '—'}~{round(range_high,2) if range_high else '—'}** "
            f"区间内偏强运行；**放量站上 {round(range_high,2) if range_high else '—'} 打开上行空间**，"
            f"跌破 {round(range_low,2) if range_low else '—'}（MA5）则转弱"
        )
    elif bias == "偏弱":
        range_low = support
        range_high = ma5
        note = (
            f"预计未来 3~5 个交易日在 **{round(range_low,2) if range_low else '—'}~{round(range_high,2) if range_high else '—'}** "
            f"区间内弱势整理；**站上 {round(range_high,2) if range_high else '—'}（MA5）才算止跌**，"
            f"跌破 {round(range_low,2) if range_low else '—'} 加速下跌"
        )
    else:
        range_low, range_high = support, resistance
        note = (
            f"预计在 **{round(range_low,2) if range_low else '—'}~{round(range_high,2) if range_high else '—'}** "
            f"区间内震荡；方向选择看能否站上 {round(range_high,2) if range_high else '—'} 或跌破 "
            f"{round(range_low,2) if range_low else '—'}"
        )

    return {
        "bias": f"{emoji} {bias}",
        "score": round(score, 1),
        "range_low": round(range_low, 2) if range_low else None,
        "range_high": round(range_high, 2) if range_high else None,
        "note": note,
        "basis": " ｜ ".join(basis[:4]),
    }


def build_forecasts(results: List[Any]) -> List[Dict[str, Any]]:
    """对每只票给出未来走势预判。"""
    rows: List[Dict[str, Any]] = []
    for result in results or []:
        market = _market(result)
        price = market.get("price")
        if not price:
            continue
        try:
            forecast = _forecast_one(price, market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[走势预判] %s 计算失败：%s", getattr(result, "code", ""), exc)
            continue
        rows.append({
            "code": _norm_code(getattr(result, "code", "")),
            "name": getattr(result, "name", "") or "",
            "price": round(price, 2),
            **forecast,
        })
    # 偏强在前，偏弱在后
    rows.sort(key=lambda r: -r.get("score", 0))
    return rows


# ---------------------------------------------------------------- 做T建议
def _t_plan_one(price: float, cost: float, market: Dict[str, Optional[float]]) -> Dict[str, Any]:
    ma5, ma20 = market.get("ma5"), market.get("ma20")
    support, resistance = market.get("support"), market.get("resistance")
    amp, pct_chg = market.get("amplitude"), market.get("pct_chg")

    base = {"price": round(price, 2), "cost": cost,
            "pnl_pct": round((price / cost - 1) * 100, 2),
            "amplitude": round(amp, 2) if amp is not None else None}

    # 1) 振幅不够 —— 空间被摩擦成本吃掉
    if amp is not None and amp < MIN_AMPLITUDE_FOR_T:
        return {**base, "verdict": T_NO, "sell_at": None, "buy_at": None, "saving": None,
                "reason": f"今日振幅仅 {amp:.1f}%，做T空间不够（价差会被手续费和滑点吃掉），等波动放大再做"}

    # 2) 破位单边下跌 —— 越 T 越低
    if support and price < support:
        return {**base, "verdict": T_FORBID_BREAK, "sell_at": None, "buy_at": None, "saving": None,
                "reason": f"已跌破 20 日支撑 {support}，属于单边下跌，做T容易越做越低，先处理仓位别急着摊薄"}

    # 2b) 当日大跌且贴着支撑 —— 破位下杀，此时"低吸"就是接刀
    if pct_chg is not None and pct_chg <= -2.0 and support and price > 0:
        dist_sup_now = (price / support - 1) * 100
        if dist_sup_now <= NEAR_LEVEL_PCT + 0.5:
            return {**base, "verdict": T_FORBID_BREAK, "sell_at": None, "buy_at": None, "saving": None,
                    "reason": f"当日跌 {pct_chg:.1f}% 且已贴到 20 日支撑 {support}（距 {dist_sup_now:.1f}%），"
                              f"属于破位下杀，此时低吸等于接刀——**等出现放量止跌阳线再做**"}

    # 3) 涨停 / 接近涨停 —— 卖出容易 T 飞
    if pct_chg is not None and pct_chg >= 9.5:
        return {**base, "verdict": T_FORBID_SURGE, "sell_at": None, "buy_at": None, "saving": None,
                "reason": "接近涨停，单边强势，做T卖出容易接不回来，宁可不动"}

    # 4) 贴压力位且当日上涨 → 高抛
    if resistance and price > 0:
        dist_res = (resistance / price - 1) * 100
        if dist_res <= NEAR_LEVEL_PCT and (pct_chg is None or pct_chg >= 0):
            sell_at = round(resistance, 2)
            buy_back = round(ma5, 2) if ma5 and ma5 < sell_at else round(price * 0.98, 2)
            saving = round((sell_at - buy_back) * T_RATIO, 3)
            return {**base, "verdict": T_BUY_HIGH, "sell_at": sell_at, "buy_at": buy_back,
                    "saving": saving,
                    "reason": f"距压力位 {resistance} 仅 {dist_res:.1f}%，可先卖 {int(T_RATIO*100)}%；"
                              f"回落到 {buy_back}（MA5 附近）买回，成本约降 {saving} 元/股。"
                              f"若放量突破 {sell_at} 不回落，就别急着买回（防止 T 飞）"}

    # 5) 贴支撑位且当日下跌 → 低吸
    if support and price > 0:
        dist_sup = (price / support - 1) * 100
        if dist_sup <= NEAR_LEVEL_PCT and (pct_chg is None or pct_chg <= 0):
            buy_at = round(support, 2)
            sell_back = round(ma5, 2) if ma5 and ma5 > buy_at else round(price * 1.02, 2)
            saving = round((sell_back - buy_at) * T_RATIO, 3)
            return {**base, "verdict": T_BUY_LOW, "buy_at": buy_at, "sell_at": sell_back,
                    "saving": saving,
                    "reason": f"距 20 日支撑 {support} 仅 {dist_sup:.1f}%，可先买 {int(T_RATIO*100)}%；"
                              f"反弹到 {sell_back}（MA5 附近）卖出，成本约降 {saving} 元/股。"
                              f"若跌破 {buy_at} 就别买第二笔（防止越 T 越低）"}

    # 6) 中间地带 —— 边界不清
    return {**base, "verdict": T_NO, "sell_at": None, "buy_at": None, "saving": None,
            "reason": "现价处在支撑与压力中间，边界不清，做T容易两头挨打，等靠近某一边再做"}


def build_t_plans(results: List[Any], positions: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """只对**有成本价的持仓股**给出做T建议。"""
    positions = positions or {}
    if not positions:
        return []

    by_code: Dict[str, Any] = {}
    for result in results or []:
        by_code[_norm_code(getattr(result, "code", ""))] = result

    rows: List[Dict[str, Any]] = []
    for code, info in positions.items():
        result = by_code.get(code)
        if result is None:
            continue
        market = _market(result)
        price = market.get("price")
        cost = info.get("cost")
        if not price or not cost:
            continue
        try:
            plan = _t_plan_one(price, cost, market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[做T建议] %s 计算失败：%s", code, exc)
            continue
        rows.append({
            "code": code,
            "name": info.get("name") or getattr(result, "name", "") or code,
            "side": "high" if plan.get("verdict") == T_BUY_HIGH else ("low" if plan.get("verdict") == T_BUY_LOW else None),
            **plan,
        })

    # 可做的排前面
    order = {T_BUY_HIGH: 0, T_BUY_LOW: 1, T_NO: 2, T_FORBID_SURGE: 3, T_FORBID_BREAK: 4}
    rows.sort(key=lambda r: order.get(r.get("verdict", ""), 9))
    return rows
