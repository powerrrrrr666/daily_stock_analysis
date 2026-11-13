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
# 做T所需的最小持股数：A股买入必须是 100 股整数倍，1/3 仓位不足 100 股就做不了。
# 300 股 × 1/3 = 100 股，正好是最小可买单位。
MIN_SHARES_FOR_T = 300
MID_ZONE_PCT = 3.0           # 距两边都超过该值 → 中间地带，边界不清

T_BUY_HIGH = "🟢 可做T（先卖后买·高抛）"
T_BUY_LOW = "🟢 可做T（先买后卖·低吸）"
T_NO = "⏸ 暂不做T"
T_FORBID_BREAK = "⚠️ 不宜做T（单边下跌）"
T_FORBID_SURGE = "⚠️ 不宜做T（单边上涨）"

# 止盈参数
# 浮盈达到该比例就先减一批落袋为安。个股 15%、ETF 10%——ETF 波动只有个股六七成，
# 用同一个门槛会等于永远不止盈。
TP_MIN_GAIN = 15.0
TP_MIN_GAIN_ETF = 10.0
# 减仓比例：一次 1/3，剩下的让利润奔跑，跌破 MA10 再走
TP_RATIO = 1 / 3

TP_NOW = "🟢 可止盈"
TP_TARGET = "🎯 目标止盈"
TP_WATCH = "👀 等反弹减压"
TP_NA = "⚪ 不适用"


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
def _forecast_one(price: float, market: Dict[str, Optional[float]], digits: int = 2) -> Dict[str, Any]:
    """digits：价格小数位。个股 2 位，ETF 3 位（0.549 这种价位四舍五入到 2 位会失真）。"""
    d = digits
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
    elif bias == "偏弱":
        range_low = support
        range_high = ma5
    else:
        range_low, range_high = support, resistance

    lo = f"{range_low:.{d}f}" if range_low else "—"
    hi = f"{range_high:.{d}f}" if range_high else "—"

    if bias == "偏强":
        note = (
            f"预计未来 3~5 个交易日在 **{lo}~{hi}** 区间内偏强运行；"
            f"**放量站上 {hi} 打开上行空间**，跌破 {lo}（MA5）则转弱"
        )
    elif bias == "偏弱":
        note = (
            f"预计未来 3~5 个交易日在 **{lo}~{hi}** 区间内弱势整理；"
            f"**站上 {hi}（MA5）才算止跌**，跌破 {lo} 加速下跌"
        )
    else:
        note = (
            f"预计在 **{lo}~{hi}** 区间内震荡；方向选择看能否站上 {hi} 或跌破 {lo}"
        )

    return {
        "bias": f"{emoji} {bias}",
        "score": round(score, 1),
        "range_low": round(range_low, d) if range_low else None,
        "range_high": round(range_high, d) if range_high else None,
        "note": note,
        "basis": " ｜ ".join(basis[:4]),
    }


def build_forecasts(results: List[Any], digits: int = 2) -> List[Dict[str, Any]]:
    """对每只票给出未来走势预判。ETF 走另建入口（sectors.build_etf_positions）。"""
    rows: List[Dict[str, Any]] = []
    for result in results or []:
        market = _market(result)
        price = market.get("price")
        if not price:
            continue
        try:
            forecast = _forecast_one(price, market, digits=digits)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[走势预判] %s 计算失败：%s", getattr(result, "code", ""), exc)
            continue
        rows.append({
            "code": _norm_code(getattr(result, "code", "")),
            "name": getattr(result, "name", "") or "",
            "price": round(price, digits),
            **forecast,
        })
    # 偏强在前，偏弱在后
    rows.sort(key=lambda r: -r.get("score", 0))
    return rows


# ---------------------------------------------------------------- 做T建议
def _t_plan_one(price: float, cost: float, market: Dict[str, Optional[float]],
                shares: Optional[float] = None, digits: int = 2,
                min_amp: float = MIN_AMPLITUDE_FOR_T,
                surge_pct: float = 9.5,
                unit: str = "股") -> Dict[str, Any]:
    """做T计划。digits / min_amp / surge_pct / unit 用于适配 ETF：

    - ETF 报价三位小数（0.549），按 2 位四舍五入价位会算错
    - ETF 免印花税、佣金低，做T摩擦成本低于个股，振幅门槛可适当放宽
    - ETF 日内涨 6% 已属单边极端，沿用个股的 9.5% 永远不触发
    - 份额单位是「份」而不是「股」
    """
    d = digits
    ma5, ma20 = market.get("ma5"), market.get("ma20")
    support, resistance = market.get("support"), market.get("resistance")
    amp, pct_chg = market.get("amplitude"), market.get("pct_chg")

    base = {"price": round(price, d), "cost": cost,
            "pnl_pct": round((price / cost - 1) * 100, 2),
            "amplitude": round(amp, 2) if amp is not None else None}

    # 0) 计算本次可做T的数量：必须是 100 整数倍，且不超过总仓位 1/3
    #    例：400 股 -> 1/3 = 133 股 -> 向下取整为 100 股（不是 133）
    t_shares = None
    ratio = T_RATIO
    if shares is not None and shares > 0:
        lot = int(shares * T_RATIO) // 100 * 100
        if lot < 100:
            one_third = int(shares * T_RATIO)
            return {**base, "verdict": T_NO, "sell_at": None, "buy_at": None, "saving": None,
                    "t_shares": 0,
                    "reason": f"仅持 {int(shares)} {unit}，1/3 只有 {one_third} {unit}——"
                              f"买卖必须 100 {unit}起，卖了买不回等量，做T在物理上不成立。"
                              f"要么把仓位补到 {MIN_SHARES_FOR_T} {unit}以上，要么直接不动"}
        t_shares = lot
        ratio = lot / shares
    base["t_shares"] = t_shares
    base["t_ratio_pct"] = round(ratio * 100)

    # 1) 振幅不够 —— 空间被摩擦成本吃掉
    if amp is not None and amp < min_amp:
        return {**base, "verdict": T_NO, "sell_at": None, "buy_at": None, "saving": None,
                "reason": f"今日振幅仅 {amp:.1f}%，做T空间不够（价差会被手续费和滑点吃掉），等波动放大再做"}

    # 2) 破位单边下跌 —— 越 T 越低
    if support and price < support:
        return {**base, "verdict": T_FORBID_BREAK, "sell_at": None, "buy_at": None, "saving": None,
                "reason": f"已跌破 支撑位 {support}，属于单边下跌，做T容易越做越低，先处理仓位别急着摊薄"}

    # 2b) 当日大跌且贴着支撑 —— 破位下杀，此时"低吸"就是接刀
    if pct_chg is not None and pct_chg <= -2.0 and support and price > 0:
        dist_sup_now = (price / support - 1) * 100
        if dist_sup_now <= NEAR_LEVEL_PCT + 0.5:
            return {**base, "verdict": T_FORBID_BREAK, "sell_at": None, "buy_at": None, "saving": None,
                    "reason": f"当日跌 {pct_chg:.1f}% 且已贴到支撑位 {support}（距 {dist_sup_now:.1f}%），"
                              f"属于破位下杀，此时低吸等于接刀——**等出现放量止跌阳线再做**"}

    # 3) 涨停 / 单边强势 —— 卖出容易 T 飞
    if pct_chg is not None and pct_chg >= surge_pct:
        return {**base, "verdict": T_FORBID_SURGE, "sell_at": None, "buy_at": None, "saving": None,
                "reason": f"当日涨 {pct_chg:.1f}%，接近涨停/单边强势，做T卖出容易接不回来，宁可不动"}

    # 4) 贴压力位且当日上涨 → 高抛
    if resistance and price > 0:
        dist_res = (resistance / price - 1) * 100
        if dist_res <= NEAR_LEVEL_PCT and (pct_chg is None or pct_chg >= 0):
            sell_at = round(resistance, d)
            buy_back = round(ma5, d) if ma5 and ma5 < sell_at else round(price * 0.98, d)
            saving = round((sell_at - buy_back) * ratio, d + 1)
            qty = f"{t_shares} {unit}" if t_shares else f"{int(ratio * 100)}% 仓位"
            return {**base, "verdict": T_BUY_HIGH, "sell_at": sell_at, "buy_at": buy_back,
                    "saving": saving,
                    "reason": f"距压力位 {resistance:.{d}f} 仅 {dist_res:.1f}%，可先卖 {qty}；"
                              f"回落到 {buy_back}（MA5 附近）买回，成本约降 {saving} 元/{unit}。"
                              f"若放量突破 {sell_at} 不回落，就别急着买回（防止 T 飞）"}

    # 5) 贴支撑位且当日下跌 → 低吸
    if support and price > 0:
        dist_sup = (price / support - 1) * 100
        if dist_sup <= NEAR_LEVEL_PCT and (pct_chg is None or pct_chg <= 0):
            buy_at = round(support, d)
            sell_back = round(ma5, d) if ma5 and ma5 > buy_at else round(price * 1.02, d)
            saving = round((sell_back - buy_at) * ratio, d + 1)
            qty = f"{t_shares} {unit}" if t_shares else f"{int(ratio * 100)}% 仓位"
            return {**base, "verdict": T_BUY_LOW, "buy_at": buy_at, "sell_at": sell_back,
                    "saving": saving,
                    "reason": f"距支撑位 {support:.{d}f} 仅 {dist_sup:.1f}%，可先买 {qty}；"
                              f"反弹到 {sell_back}（MA5 附近）卖出，成本约降 {saving} 元/{unit}。"
                              f"若跌破 {buy_at} 就别买第二笔（防止越 T 越低）"}

    # 6) 中间地带 —— 边界不清
    return {**base, "verdict": T_NO, "sell_at": None, "buy_at": None, "saving": None,
            "reason": "现价处在支撑与压力中间，边界不清，做T容易两头挨打，等靠近某一边再做"}


def build_t_plans(results: List[Any], positions: Optional[Dict[str, Dict[str, Any]]] = None,
                  **kwargs: Any) -> List[Dict[str, Any]]:
    """只对**有成本价的持仓股**给出做T建议。

    kwargs 透传给 ``_t_plan_one``（digits / min_amp / surge_pct / unit），ETF 用另一套阈值。
    """
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
            plan = _t_plan_one(price, cost, market, info.get("shares"), **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[做T建议] %s 计算失败：%s", code, exc)
            continue
        rows.append({
            "code": code,
            "name": info.get("name") or getattr(result, "name", "") or code,
            "shares": info.get("shares"),
            "side": "high" if plan.get("verdict") == T_BUY_HIGH else ("low" if plan.get("verdict") == T_BUY_LOW else None),
            **plan,
        })

    # 可做的排前面
    order = {T_BUY_HIGH: 0, T_BUY_LOW: 1, T_NO: 2, T_FORBID_SURGE: 3, T_FORBID_BREAK: 4}
    rows.sort(key=lambda r: order.get(r.get("verdict", ""), 9))
    return rows


# ---------------------------------------------------------------- 止盈建议
def _take_profit_one(price: float, cost: float, market: Dict[str, Optional[float]],
                     shares: Optional[float] = None, digits: int = 2,
                     unit: str = "股", min_gain: float = TP_MIN_GAIN,
                     resistance: Optional[float] = None,
                     preferred_level: Optional[float] = None) -> Dict[str, Any]:
    """止盈/减压计划 —— 回答「涨到哪里该卖」。

    分三种情形：
    1. 浮盈 ≥ min_gain：先减 1/3 落袋，剩下的用 MA10 做移动止盈（跌破全清）
    2. 0 < 浮盈 < min_gain：涨到第一压力位减 1/3
    3. 浮亏：不叫止盈，叫「反弹减压」——反弹到压力位减 1/3 降风险，
       若压力位高于成本，顺带提示到成本价也可先减一部分解套

    preferred_level：操作信号模块给出的减仓价。它比纯压力位更贴合当下的技术形态，
    传进来后优先使用，避免同一只票在报告里出现两个不同的卖出价。

    为什么只减 1/3：一次性清掉会错过后续趋势，全留着又容易坐过山车。
    """
    d = digits
    pnl = ((price / cost - 1) * 100) if cost else 0.0
    res = resistance if resistance is not None else market.get("resistance")
    # 操作信号给的减仓价优先；但它必须高于现价才有意义
    if preferred_level and preferred_level > price:
        res = preferred_level
    ma10 = market.get("ma10")
    ma5 = market.get("ma5")

    lot = int(shares * TP_RATIO) // 100 * 100 if shares else 0
    small = shares is not None and lot < 100
    if lot >= 100:
        qty = f"{lot} {unit}"
    elif small:
        qty = "全部仓位"
    else:
        qty = "1/3 仓位"

    def fmt(v: Optional[float]) -> str:
        return f"{v:.{d}f}" if v else "—"

    def trail_line(ratio: float) -> float:
        """移动止盈线：MA10 与回撤价取高者。

        优先 MA10（中期趋势线）。不用 MA5——它贴得太近，浮盈仓位一有正常波动就被震出去。
        但股价快速拉升时 MA10 会远在下方，「跌破 MA10 再走」等于没保护
        （40 元的票跌破 32.6 才走，利润已经回吐一大半），所以再叠一条回撤线兜底。
        """
        base = ma10 if (ma10 and ma10 < price) else (ma5 if (ma5 and ma5 < price) else None)
        floor = price * ratio
        return max(base, floor) if base else floor

    base = {"tp_state": TP_NA, "tp_price": None, "tp_qty": lot or None,
            "tp_line": "", "tp_reason": ""}

    # 1) 浮盈已达标 —— 先落袋一批，剩下的交给移动止盈
    if pnl >= min_gain:
        trail = trail_line(0.90)
        drop = (trail / price - 1) * 100
        line = (f"现价 **{fmt(price)}** 附近**先减 {qty}**落袋；剩下的用移动止盈——"
                f"**跌破 {fmt(trail)} 全清**（较现价 {drop:.1f}%），不跌破就继续拿")
        return {**base, "tp_state": TP_NOW, "tp_price": round(price, d), "tp_line": line,
                "tp_reason": f"浮盈 {pnl:.1f}%，已超过 {min_gain:.0f}% 的止盈线"}

    # 2) 有浮盈但没到止盈线 —— 到压力位减一批
    if pnl > 0:
        if res and res > price:
            gap = (res / price - 1) * 100
            if gap <= NEAR_LEVEL_PCT:
                line = (f"已贴到压力位 **{fmt(res)}**（距 {gap:.1f}%），可现价减 {qty}；"
                        f"**放量突破并站稳 {fmt(res * 1.005)} 就不减**")
            else:
                line = f"涨到 **{fmt(res)}** 减 {qty}（第一压力位，距现价 +{gap:.1f}%）"
            return {**base, "tp_state": TP_TARGET, "tp_price": round(res, d), "tp_line": line,
                    "tp_reason": f"浮盈 {pnl:.1f}%，压力位是这波的第一道坎"}

        # 刚站上压力位（2% 以内）—— 先看能不能站稳，别急着按回撤退
        if res and res <= price and (price / res - 1) * 100 <= NEAR_LEVEL_PCT:
            line = (f"已站上压力位 **{fmt(res)}**，浮盈 {pnl:.1f}%——"
                    f"**站稳 {fmt(res)} 就继续拿，跌回 {fmt(res)} 下方再减 {qty}**")
            return {**base, "tp_state": TP_TARGET, "tp_price": round(res, d), "tp_line": line,
                    "tp_reason": "刚突破压力位，先给一天确认时间，跌破再走"}

        # 压力位已经远离（或有突破但没数据）→ 改用回撤止盈
        trail = trail_line(0.93)
        drop = (trail / price - 1) * 100
        line = (f"上方暂无新压力位，浮盈 {pnl:.1f}%——改用回撤止盈："
                f"**跌破 {fmt(trail)} 减 {qty}**（较现价 {drop:.1f}%），不跌破继续拿")
        return {**base, "tp_state": TP_TARGET, "tp_price": round(trail, d), "tp_line": line,
                "tp_reason": "已站上原压力位，没有更高的参照价，按回撤幅度止盈"}

    # 3) 浮亏 —— 不是止盈，是反弹减压
    if res and res > price:
        gap = (res / price - 1) * 100
        # 压力位就在头顶（2% 内）——「反弹到 X 再减」等于说「马上就到」，改成直接说现价可减
        if gap <= NEAR_LEVEL_PCT:
            line = (f"浮亏 {abs(pnl):.1f}%，现价 **{fmt(price)}** 已贴压力位 **{fmt(res)}**"
                    f"（+{gap:.1f}%）——**{fmt(res)} 附近就可以先减 {qty}**，别指望一次冲过去")
            return {**base, "tp_state": TP_WATCH, "tp_price": round(res, d), "tp_line": line,
                    "tp_reason": "已贴压力位，上方空间不到 2%，先减一批降风险"}
        if cost and cost < res:
            need = (cost / price - 1) * 100
            if need <= 30:
                line = (f"浮亏 {abs(pnl):.1f}%，反弹到成本 **{fmt(cost)}**（+{need:.1f}%）"
                        f"先减 {qty}减压；若继续上攻到 **{fmt(res)}** 再减一批")
                return {**base, "tp_state": TP_WATCH, "tp_price": round(cost, d),
                        "tp_line": line,
                        "tp_reason": "成本价在压力位之下，先解套再谈减仓"}
        line = (f"浮亏 {abs(pnl):.1f}%，反弹到 **{fmt(res)}** 减 {qty}减压（+{gap:.1f}%）；"
                f"这条线过不去就别硬等成本价")
        return {**base, "tp_state": TP_WATCH, "tp_price": round(res, d), "tp_line": line,
                "tp_reason": "没有浮盈谈不上止盈，上方压力位是用来减仓降风险的"}

    return {**base, "tp_state": TP_NA, "tp_price": None,
            "tp_line": "上方压力位数据缺失，暂无明确减仓位；等反弹到 MA20 附近再评估",
            "tp_reason": "缺压力位，给不出可执行的价位"}


# ---------------------------------------------------------------- 持仓操作清单
def _fmt(value: Optional[float], digits: int = 2) -> str:
    return f"{value:.{digits}f}" if value else "—"


def build_next_action(add_state: str, price: Optional[float],
                      ma5: Optional[float], support: Optional[float],
                      resistance: Optional[float],
                      opp: Optional[Dict[str, Any]] = None,
                      digits: int = 2) -> str:
    """由「补仓状态 + 均线位置」推导出接下来该做什么。

    个股与 ETF 共用同一套口径，避免两边给出互相矛盾的结论。
    """
    opp = opp or {}
    sup = _fmt(support, digits)
    ma5s = _fmt(ma5, digits)

    if opp.get("action") == "🟠 该减仓" and opp.get("level"):
        return f"反弹到 **{opp['level']}** 附近减仓"
    if add_state.startswith("🔴"):
        return f"已跌破支撑 **{sup}**，反弹减仓为主，**别补**"
    if add_state.startswith("🟢"):
        return "可在现价附近补第一笔（不超过原仓位 1/3）"
    if add_state.startswith("🟡"):
        if ma5 and price and price > ma5:
            if resistance:
                return (f"等突破 **{_fmt(resistance * 1.005, digits)}** 或回踩 **{ma5s}**（MA5）再补；"
                        f"跌破 **{sup}** 减仓")
            return f"等回踩 **{ma5s}**（MA5）再补；跌破 **{sup}** 减仓"
        level = f"**{ma5s}**（MA5）" if ma5 else "关键均线"
        return f"等站上{level}再补；跌破 **{sup}** 减仓"
    return f"暂不加仓；跌破 **{sup}** 减仓"


def build_holdings_board(positions_map: Optional[Dict[str, Dict[str, Any]]],
                         positions_rows: Optional[List[Dict[str, Any]]],
                         t_plans: Optional[List[Dict[str, Any]]],
                         opportunities: Optional[Dict[str, Any]],
                         digits: int = 2,
                         unit: str = "股") -> List[Dict[str, Any]]:
    """把「接下来的操作 / 补仓判断 / 做T建议」合并成每只持仓股一条清单。"""
    positions_map = positions_map or {}
    if not positions_map:
        return []

    pos_by_code = {r.get("code"): r for r in (positions_rows or [])}
    t_by_code = {t.get("code"): t for t in (t_plans or [])}
    opp_by_code = {o.get("code"): o for o in ((opportunities or {}).get("actionable") or [])}

    rows: List[Dict[str, Any]] = []
    for code, info in positions_map.items():
        p = pos_by_code.get(code) or {}
        price = p.get("price")
        if not price:
            continue
        add_state = p.get("state") or "⚪ 数据不足"
        add_reason = p.get("reason") or ""
        opp = opp_by_code.get(code) or {}

        next_action = build_next_action(
            add_state, price, p.get("ma5"), p.get("support"),
            p.get("resistance"), opp, digits=digits,
        )

        t = t_by_code.get(code) or {}
        t_verdict = t.get("verdict") or "⚪ 无数据"
        t_line = ""
        if t.get("side") == "high" and t.get("sell_at") and t.get("buy_at"):
            t_line = (f"先卖 **{t['sell_at']}** → 回落到 **{t['buy_at']}** 买回，"
                      f"成本约降 **{t['saving']} 元/{unit}**")
        elif t.get("side") == "low" and t.get("buy_at") and t.get("sell_at"):
            t_line = (f"先买 **{t['buy_at']}** → 反弹到 **{t['sell_at']}** 卖出，"
                      f"成本约降 **{t['saving']} 元/{unit}**")

        # 止盈 / 反弹减压：回答「涨到哪里该卖」
        # 若操作信号已给出减仓价，沿用它——同一只票不能出现两个卖出价
        preferred = opp.get("level") if opp.get("action") == "🟠 该减仓" else None
        tp = _take_profit_one(
            price, info.get("cost") or 0,
            {"ma5": p.get("ma5"), "ma10": p.get("ma10"), "ma20": p.get("ma20"),
             "resistance": p.get("resistance")},
            info.get("shares"), digits=digits, unit=unit,
            preferred_level=preferred,
        )

        rows.append({
            "code": code,
            "name": info.get("name") or code,
            "cost": info.get("cost"),
            "shares": info.get("shares"),
            "price": price,
            "amount": p.get("amount"),
            "pnl_amount": p.get("pnl_amount"),
            "pnl_pct": p.get("pnl_pct"),
            "next_action": next_action,
            "add_state": add_state,
            "add_reason": add_reason,
            "t_verdict": t_verdict,
            "t_line": t_line,
            "t_reason": t.get("reason") or "",
            "tp_state": tp.get("tp_state"),
            "tp_price": tp.get("tp_price"),
            "tp_line": tp.get("tp_line"),
            "tp_reason": tp.get("tp_reason"),
        })

    rows.sort(key=lambda r: r.get("pnl_pct") if r.get("pnl_pct") is not None else 0)
    return rows
