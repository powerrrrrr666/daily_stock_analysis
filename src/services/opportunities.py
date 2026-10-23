# -*- coding: utf-8 -*-
"""「操作信号」扫描：把每只票压缩成一句可执行结论 + 明确触发价位。

**为什么要有这个模块**：用户反馈「报告不是观望就是不买」。
原因是 LLM 的结论天然保守（趋势不好一律观望），且散落在每只票的长篇里。
这里用**确定性规则**做一次汇总扫描，只输出「能做什么 + 什么价位做 + 止损多少」，
并区分「已满足条件」与「等到某价位就满足」，让报告第一屏就是可执行信息。

数据来源：当轮分析结果 ``dashboard.data_perspective``（ma5/ma10/ma20/支撑/压力/量比）
与 ``market_snapshot``（现价/涨跌幅），不额外取数。

⚠️ 规则只做**技术面**判断，不构成投资建议；所有价位都是"如果…则…"的条件式表达。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ACTION_BUY = "🟢 可买入"
ACTION_ADD = "🟢 可补仓"
ACTION_NEAR = "🟡 等触发"
ACTION_TRIM = "🟠 该减仓"
ACTION_NONE = "⚪ 暂无信号"

# 阈值
BIAS5_OVERHEAT = 6.0      # 距 MA5 乖离超过该值 → 短期过热，该减
BIAS20_FRESH = 4.0        # 站上 MA20 但乖离不超过该值 → 刚转多，可买
PULLBACK_BIAS5 = 2.5      # 多头排列下贴近 MA5 的乖离上限
STOP_PCT = 2.0            # 以均线为止损时的下浮比例
NEAR_RESISTANCE_PCT = 2.0  # 距压力位小于该值 → 追进去空间太小
MAX_GAP_FOR_NEAR = 6.0     # 「等触发」的触发价距现价超过该值 → 视为暂无机会

GROUP_ORDER = [ACTION_BUY, ACTION_ADD, ACTION_NEAR, ACTION_TRIM, ACTION_NONE]


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "" or value == "N/A":
            return None
        return float(value)
    except (TypeError, ValueError):
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
        "ma5": _num(pp.get("ma5")),
        "ma10": _num(pp.get("ma10")),
        "ma20": _num(pp.get("ma20")),
        "support": _num(pp.get("support_level")),
        "resistance": _num(pp.get("resistance_level")),
        "volume_ratio": _num(va.get("volume_ratio")),
    }


def _evaluate(price: float, ma5: Optional[float], ma10: Optional[float], ma20: Optional[float],
              support: Optional[float], resistance: Optional[float],
              volume_ratio: Optional[float], held: bool = False) -> Dict[str, Any]:
    bias5 = (price / ma5 - 1) * 100 if ma5 else None
    bias20 = (price / ma20 - 1) * 100 if ma20 else None
    dist_res = (resistance / price - 1) * 100 if resistance else None

    def stop_of(level: Optional[float]) -> Optional[float]:
        return round(level * (1 - STOP_PCT / 100), 2) if level else None

    def near_or_none(level: float, stop: Optional[float], reason: str, gap_ok_reason: str = "") -> Dict[str, Any]:
        """距触发价太远就别标「等触发」，否则又会变成一堆无意义的观望。"""
        gap = (level / price - 1) * 100
        if abs(gap) > MAX_GAP_FOR_NEAR:
            return {"action": ACTION_NONE, "level": None, "stop": None,
                    "reason": gap_ok_reason or f"买点 {round(level, 2)} 距现价还有 +{gap:.1f}%，属于遥远目标，不构成近期机会"}
        return {"action": ACTION_NEAR, "level": round(level, 2), "stop": stop, "reason": reason}

    # ---------- 1. 保护性信号（只对**持仓**股成立）----------
    #   没持仓的票即使破位，也只是"不碰"，不该说"该减仓"——那是给有仓位的人看的
    if bias5 is not None and bias5 >= BIAS5_OVERHEAT:
        if held:
            return {"action": ACTION_TRIM,
                    "level": round(ma5, 2) if ma5 else None,
                    "stop": None,
                    "reason": f"距 MA5 乖离 +{bias5:.1f}%，短期涨得太急，冲高该兑现；跌破 {round(ma5,2) if ma5 else '—'} 减仓"}
        return near_or_none(ma5, stop_of(ma10),
                            f"距 MA5 已 +{bias5:.1f}%，涨得太急，**回踩 {round(ma5,2)} 才是买点**",
                            "短期涨幅过大且回踩位较远，现在追容易被闷，先放过")

    if ma5 and ma10 and ma20 and ma5 < ma10 and price < ma20:
        if held:
            return {"action": ACTION_TRIM,
                    "level": round(ma10, 2),
                    "stop": None,
                    "reason": f"MA5({ma5}) 已下穿 MA10({ma10}) 且现价跌破 MA20({ma20})，趋势破位，反弹到 {round(ma10,2)} 附近减仓"}
        return {"action": ACTION_NONE, "level": None, "stop": None,
                "reason": "趋势破位（MA5 下穿 MA10 且跌破 MA20），没持仓的不要碰"}

    # ---------- 2. 已贴近压力位 → 不是好的买点 ----------
    if dist_res is not None and 0 <= dist_res <= NEAR_RESISTANCE_PCT:
        return {"action": ACTION_NEAR,
                "level": round(resistance * 1.005, 2),
                "stop": stop_of(ma5),
                "reason": f"距压力位 {resistance} 仅 {dist_res:.1f}%，**现在买空间太小**；"
                          f"突破并站稳 {round(resistance * 1.005, 2)} 才是买点，或等回踩 MA5({round(ma5,2) if ma5 else '—'})再说"}

    # ---------- 3. 买入 / 补仓信号 ----------
    # 3a 标准多头排列 + 回踩 MA5：最健康的买点
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20 and bias5 is not None and -1.0 <= bias5 <= PULLBACK_BIAS5:
        return {"action": ACTION_BUY,
                "level": round(price, 2),
                "stop": stop_of(ma10),
                "reason": f"均线标准多头（MA5>{ma10}>MA20={ma20}），现价贴近 MA5({ma5})，回踩买点；"
                          f"止损 {stop_of(ma10)}（MA10 下方 {STOP_PCT:.0f}%）"}

    # 3b 站上 MA20 且未大幅偏离：趋势刚转多
    if ma20 and bias20 is not None and 0 <= bias20 <= BIAS20_FRESH:
        vol = f"，量比 {volume_ratio:.2f}" if volume_ratio else ""
        return {"action": ACTION_BUY,
                "level": round(price, 2),
                "stop": stop_of(ma20),
                "reason": f"现价站上 MA20({ma20}) 且未大幅偏离（+{bias20:.1f}%）{vol}；"
                          f"止损 {stop_of(ma20)}（MA20 下方 {STOP_PCT:.0f}%）"}

    # 3c 多头排列但已涨高 → 等回踩 MA5，别追
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20 and bias5 is not None and bias5 > PULLBACK_BIAS5:
        return near_or_none(ma5, stop_of(ma10),
                            f"多头排列但现价距 MA5 已 +{bias5:.1f}%，位置偏高；**回踩 {round(ma5,2)} 才是买点**")

    # 3d 已站上 MA5 但仍在 MA20 下方：修复中，等站上 MA20
    if ma5 and ma20 and price >= ma5 and price < ma20:
        return near_or_none(ma20, stop_of(ma5),
                            f"已站上 MA5({ma5}) 但还在 MA20({ma20}) 下方；**站上 {round(ma20,2)} 才是买点**")

    # 3e 还在 MA5 下方：等站回 MA5
    if ma5 and price < ma5:
        return near_or_none(ma5, stop_of(support),
                            f"现价在 MA5({ma5}) 下方，趋势未修复；**站回 {round(ma5,2)} 才谈买入**")

    return {"action": ACTION_NONE, "level": None, "stop": None,
            "reason": "均线数据不足或形态不明，暂不操作"}


def scan_opportunities(results: List[Any], positions: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """扫描全部标的，返回按信号分组的可执行清单。"""
    positions = positions or {}
    rows: List[Dict[str, Any]] = []

    for result in results or []:
        code = _norm_code(getattr(result, "code", ""))
        if not code:
            continue
        market = _market(result)
        price = market.get("price")
        if not price:
            continue

        held = code in positions
        evaluated = _evaluate(price, market.get("ma5"), market.get("ma10"), market.get("ma20"),
                              market.get("support"), market.get("resistance"),
                              market.get("volume_ratio"), held)
        action = evaluated["action"]
        # 持仓股把「可买入」表述为「可补仓」
        if held and action == ACTION_BUY:
            action = ACTION_ADD

        level = evaluated.get("level")
        rows.append({
            "code": code,
            "name": getattr(result, "name", "") or positions.get(code, {}).get("name", "") or code,
            "price": round(price, 2),
            "pct_chg": market.get("pct_chg"),
            "action": action,
            "level": level,
            "gap_pct": round((level / price - 1) * 100, 2) if level and price else None,
            "stop": evaluated.get("stop"),
            "reason": evaluated.get("reason", ""),
            "held": held,
            "cost": positions.get(code, {}).get("cost"),
        })

    actionable = [r for r in rows if r["action"] in (ACTION_BUY, ACTION_ADD, ACTION_NEAR, ACTION_TRIM)]
    actionable.sort(key=lambda r: (GROUP_ORDER.index(r["action"]) if r["action"] in GROUP_ORDER else 9,
                                   -(abs(r.get("gap_pct") or 0))))
    return {
        "actionable": actionable,
        "none_names": [r["name"] for r in rows if r["action"] == ACTION_NONE],
        "total": len(rows),
        "buy_count": len([r for r in rows if r["action"] in (ACTION_BUY, ACTION_ADD)]),
        "near_count": len([r for r in rows if r["action"] == ACTION_NEAR]),
        "trim_count": len([r for r in rows if r["action"] == ACTION_TRIM]),
    }
