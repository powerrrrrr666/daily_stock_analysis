# -*- coding: utf-8 -*-
"""持仓成本与「补仓机会」评估。

数据来源：仓库根目录 ``positions.json``（用户维护成本价）+ 当轮分析结果
（``AnalysisResult.market_snapshot`` 的现价、``dashboard.data_perspective`` 的均线与支撑）。

判断原则（**只做减风险的建议，不做加杠杆的鼓动**）：
1. 浮亏太小（< 8%）→ 不建议补，摊薄效果有限，留着子弹。
2. **已跌破 支撑位 → 明确劝阻**，跌破支撑后补仓等于接刀。
3. 浮亏 ≥ 8% 且距支撑 ≤ 3% → 「已到观察位」，但必须等止跌确认（放量阳线或站回 MA5）。
4. 浮亏 ≥ 8% 且已站上 MA5 → 「可小批补」，说明已脱离最低点。
5. 其余 → 「观察中」，等企稳信号。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 浮亏达到该比例才谈得上"补仓摊薄"
MIN_LOSS_FOR_ADD = 8.0
# 距支撑位的距离阈值（%）
NEAR_SUPPORT_PCT = 3.0
# 支撑位失守判定（%）
BREAK_SUPPORT_PCT = -0.5
# 距压力位多近时，不再建议补仓（空间太小）
NEAR_RESISTANCE_PCT = 2.0
STATE_ADD = "🟢 可小批补"
STATE_WATCH = "🟡 观察位"
STATE_HOLD = "⏸ 暂不补"
STATE_DANGER = "🔴 已破支撑"
STATE_UNKNOWN = "⚪ 数据不足"

STATUS_ORDER = {STATE_DANGER: 0, STATE_ADD: 1, STATE_WATCH: 2, STATE_HOLD: 3, STATE_UNKNOWN: 4}


def _norm_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    for prefix in ("SH", "SZ", "BJ", "HK", "US"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.lstrip(".")


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "" or value == "N/A":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_raw(path: Optional[Path], want_etf: bool) -> Dict[str, Dict[str, Any]]:
    """读取 positions.json 并按「是否 ETF」过滤。

    shares 可缺失（视为未知股数），填了才能判断做T是否具备条件：
    A股买入须为 100 股整数倍，1/3 仓位不足 100 股时实际无法做T。
    ETF 条目需带 "type": "etf"，个股流程会跳过它们（ETF 不在自选股里，没有 LLM 分析结果）。
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent.parent / "positions.json"
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[持仓] 读取 %s 失败：%s", path, exc)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for key, value in (raw or {}).items():
        if str(key).startswith("_"):
            continue
        if not isinstance(value, dict):
            continue
        is_etf = str(value.get("type") or "").strip().lower() == "etf"
        if is_etf != want_etf:
            continue
        cost = _num(value.get("cost"))
        if not cost or cost <= 0:
            continue
        code = _norm_code(key)
        if not code:
            continue
        out[code] = {
            "name": str(value.get("name") or ""),
            "cost": cost,
            "shares": _num(value.get("shares")),
        }
    return out


def load_positions(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """个股持仓（不含 ETF）。"""
    return _load_raw(path, want_etf=False)


def load_etf_positions(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """ETF 持仓（positions.json 里 type=etf 的条目）。"""
    return _load_raw(path, want_etf=True)


def _extract_market(result: Any) -> Dict[str, Optional[float]]:
    """从 AnalysisResult 里取出评估所需的行情与均线数据。"""
    snap = getattr(result, "market_snapshot", None) or {}
    dash = getattr(result, "dashboard", None) or {}
    dp = (dash.get("data_perspective") or {}) if isinstance(dash, dict) else {}
    pp = (dp.get("price_position") or {}) if isinstance(dp, dict) else {}

    price = _num(snap.get("price")) or _num(snap.get("close")) or _num(pp.get("current_price"))
    return {
        "price": price,
        "pct_chg": _num(snap.get("pct_chg")),
        "ma5": _num(pp.get("ma5")),
        "ma10": _num(pp.get("ma10")),
        "ma20": _num(pp.get("ma20")),
        "support": _num(pp.get("support_level")),
        "resistance": _num(pp.get("resistance_level")),
        "amount": snap.get("amount"),
    }


def _fmt(value: Optional[float], digits: int = 2) -> str:
    return f"{round(value, digits)}" if value else "—"


def _evaluate(price: float, cost: float, ma5: Optional[float],
              ma10: Optional[float], ma20: Optional[float],
              support: Optional[float],
              resistance: Optional[float] = None,
              digits: int = 2,
              min_loss: float = MIN_LOSS_FOR_ADD) -> Dict[str, Any]:
    """产出补仓判断：state / reason / dist_support / pnl_pct。

    digits：价格小数位。个股 2 位，ETF 3 位（0.549 这类价位按 2 位算会把关键位置说错）。
    min_loss：浮亏达到多少才谈补仓。ETF 波动小，用 5% 比个股的 8% 更贴合实际。
    """
    pnl = (price / cost - 1.0) * 100.0
    dist_sup = (price / support - 1.0) * 100.0 if support else None
    dist_res = (resistance / price - 1.0) * 100.0 if resistance else None

    base = {"pnl_pct": round(pnl, 2), "dist_support": round(dist_sup, 2) if dist_sup is not None else None}
    sup = _fmt(support, digits)

    # 1) 已破支撑 —— 最强否决
    if dist_sup is not None and dist_sup < BREAK_SUPPORT_PCT:
        return {**base, "state": STATE_DANGER,
                "reason": f"已跌破支撑位 {sup}，此时补仓等于接刀。等重新站回支撑、或到更低的明确止跌位再考虑"}

    # 2) 亏损不够 / 已在盈利，都不值得补
    if pnl > -min_loss:
        if pnl >= 0:
            return {**base, "state": STATE_HOLD,
                    "reason": f"当前浮盈 {pnl:.1f}%，没有摊薄需求——在这个位置加仓只会抬高成本，不建议"}
        return {**base, "state": STATE_HOLD,
                "reason": f"浮亏仅 {abs(pnl):.1f}%，补仓摊薄效果有限，建议留着子弹等更低位"}

    # 2.5) 趋势破位：MA5 已下穿 MA10 且现价在 MA20 下方
    #      —— 与「操作信号」模块的破位判定保持一致，否则会出现
    #      「一边说该减仓、一边说可小批补」的矛盾（2026-11-05 高德红外实例）
    if ma5 and ma10 and ma20 and ma5 < ma10 and price < ma20:
        return {**base, "state": STATE_WATCH,
                "reason": f"MA5({_fmt(ma5, digits)}) 已下穿 MA10({_fmt(ma10, digits)})、"
                          f"现价仍在 MA20({_fmt(ma20, digits)}) 下方，"
                          f"趋势尚未修复——**这时候补仓是在下跌途中接刀**；"
                          f"等站上 MA20 且均线重新多头再谈"}

    # 3) 浮亏够了，已站上 MA5 → 已脱离最低点
    #    但若已贴近压力位，补仓空间太小，应先等突破（否则会和「做T高抛」自相矛盾）
    if ma5 and price > ma5:
        if dist_res is not None and 0 <= dist_res <= NEAR_RESISTANCE_PCT:
            return {**base, "state": STATE_WATCH,
                    "reason": f"浮亏 {abs(pnl):.1f}%，虽已站上 MA5({_fmt(ma5, digits)})，"
                              f"但距 20 日压力位 {_fmt(resistance, digits)} 仅 {dist_res:.1f}%——现在补空间太小；"
                              f"**等放量突破并站稳 {_fmt(resistance * 1.005, digits)} 再补**，或等回踩 MA5 附近"}
        return {**base, "state": STATE_ADD,
                "reason": f"浮亏 {abs(pnl):.1f}%，且已站上 MA5({_fmt(ma5, digits)})，脱离近期最低点，可小批补（不超过原仓位的 1/3）"}

    # 4) 贴近支撑 → 观察位
    if dist_sup is not None and dist_sup <= NEAR_SUPPORT_PCT:
        tight = ""
        if dist_sup <= 2.0:
            tight = "（已贴在支撑上，一旦跌破就是新一轮下跌，**现在抢补极易被埋**）"
        return {**base, "state": STATE_WATCH,
                "reason": f"浮亏 {abs(pnl):.1f}%，距支撑位 {sup} 仅 {dist_sup:.1f}%{tight}；"
                          f"**必须等止跌确认**（放量阳线收复 MA5，或连续 2 日不破支撑）才可分批，别抢"}

    # 5) 其余
    detail = f"距支撑位还有 {dist_sup:.1f}% 空间" if dist_sup is not None else "支撑位未知"
    return {**base, "state": STATE_WATCH,
            "reason": f"浮亏 {abs(pnl):.1f}%，但未站上 MA5，{detail}，未到补仓时机，等企稳信号"}


def build_position_rows(results: List[Any], positions: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """把持仓成本与当轮分析结果合并，产出报告用的行数据。"""
    positions = positions if positions is not None else load_positions()
    if not positions:
        return []

    by_code: Dict[str, Any] = {}
    for result in results or []:
        by_code[_norm_code(getattr(result, "code", ""))] = result

    rows: List[Dict[str, Any]] = []
    for code, info in positions.items():
        result = by_code.get(code)
        name = info.get("name") or (getattr(result, "name", "") if result else "") or code
        cost = info["cost"]
        row: Dict[str, Any] = {
            "code": code, "name": name, "cost": cost,
            "shares": info.get("shares"),
        }

        if result is None:
            row.update({"state": STATE_UNKNOWN, "reason": "本轮未取到该股数据，无法评估"})
            rows.append(row)
            continue

        market = _extract_market(result)
        price = market.get("price")
        if not price:
            row.update({"state": STATE_UNKNOWN, "reason": "未取到现价，无法评估"})
            rows.append(row)
            continue

        evaluated = _evaluate(price, cost, market.get("ma5"), market.get("ma10"),
                              market.get("ma20"), market.get("support"),
                              market.get("resistance"))
        # 仓位过小（不足 300 股）时，"不超过原仓位 1/3"这类分批建议无法落地——
        # A股买入必须是 100 股整数倍。补一句实话，避免给出做不成的建议。
        shares = info.get("shares")
        if shares is not None and 0 < shares < 300 and evaluated.get("state", "").startswith("🟢"):
            evaluated["reason"] = (
                f"{evaluated.get('reason', '')}（注：仅持 {int(shares)} 股，"
                f"补仓最少 100 股，相当于把仓位翻倍，别按「1/3」来算）"
            )
        row.update({
            "price": round(price, 2),
            "pct_chg": market.get("pct_chg"),
            "amount": market.get("amount"),
            "ma5": market.get("ma5"),
            "support": market.get("support"),
            "resistance": market.get("resistance"),
            # 浮亏/浮盈金额：有股数才算，否则为 None
            "pnl_amount": round((price - cost) * shares, 0) if shares else None,
            **evaluated,
        })
        rows.append(row)

    rows.sort(key=lambda r: (STATUS_ORDER.get(r.get("state", ""), 9), r.get("pnl_pct") or 0))
    return rows
