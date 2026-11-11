# -*- coding: utf-8 -*-
"""热门板块 + 对应 ETF 建议。

数据源（全部免密钥）：
  1. 东方财富概念板块排行 —— 找出今日最热的概念
  2. 腾讯行情 —— 取对应 ETF 的现价、涨跌幅、20 日均线位置

设计原则：
  - 任何异常都只记日志并返回空列表，**绝不阻断主报告**
  - 不给模糊建议，每个 ETF 必须落到一句可执行结论 + 价位
"""
import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
_TIMEOUT = 15

# 非主题类概念（涨停统计、资金属性、指数成分等），不参与「热门板块」
_BLOCK_WORDS = (
    "昨日", "今日", "东财", "融资融券", "深股通", "沪股通", "机构重仓", "基金重仓",
    "预盈", "预亏", "高送转", "次新股", "破净", "低价股", "百元股", "举牌",
    "股权转让", "参股", "转债", "标的", "标准普尔", "MSCI", "富时", "HS300",
    "上证", "深证", "创业板综", "题材股", "热股", "涨停", "跌停", "连板",
    "强势", "活跃", "壳资源", "ST股", "同花顺", "证金", "汇金", "QFII",
)

# 概念关键词 -> (ETF 代码, 简称)。按顺序匹配，先命中先用。
# 代码均经行情接口核对过名称一致（2026-11-09 验证 26 只）
_ETF_RULES: List[Tuple[Tuple[str, ...], str, str]] = [
    (("人形机器人", "机器人", "减速器", "伺服", "电机"), "562500", "机器人ETF"),
    (("人工智能", "AI", "算力", "大模型", "AIGC", "ChatGPT"), "159819", "人工智能ETF"),
    (("半导体", "芯片", "集成电路", "封测", "光刻", "存储"), "512480", "半导体ETF"),
    (("光通信", "光模块", "CPO", "6G", "5G", "通信"), "515880", "通信ETF"),
    (("消费电子", "苹果", "MR", "VR", "AR", "折叠屏"), "159732", "消费电子ETF"),
    (("云计算", "数据中心", "IDC", "东数西算"), "516510", "云计算ETF"),
    (("游戏", "传媒", "影视", "短剧", "元宇宙"), "159869", "游戏ETF"),
    (("汽车", "智能驾驶", "车联网", "无人驾驶"), "516110", "汽车ETF"),
    (("光伏", "HJT", "TOPCon", "硅料"), "515790", "光伏ETF"),
    (("锂电", "储能", "钠离子", "固态电池", "新能源"), "516160", "新能源ETF"),
    (("军工", "航空", "航天", "大飞机", "空间站", "卫星"), "512660", "军工ETF"),
    (("医药", "创新药", "医疗器械", "CRO", "疫苗", "中药"), "512010", "医药ETF"),
    (("白酒", "酿酒", "食品饮料", "消费"), "512690", "酒ETF"),
    (("证券", "券商"), "512000", "券商ETF"),
    (("银行",), "512800", "银行ETF"),
    (("煤炭", "焦煤"), "515220", "煤炭ETF"),
    (("有色", "稀土", "小金属", "铜", "铝", "锂矿"), "512400", "有色金属ETF"),
    (("钢铁",), "515210", "钢铁ETF"),
    (("化工", "化学", "磷", "氟"), "159870", "化工ETF"),
    (("农业", "种业", "养殖", "猪肉", "粮食"), "159825", "农业ETF"),
    (("旅游", "免税", "酒店", "消费复苏"), "159766", "旅游ETF"),
    (("电力", "电网", "特高压", "绿电"), "159611", "电力ETF"),
    (("黄金", "贵金属"), "518880", "黄金ETF"),
    (("红利", "高股息", "中特估"), "510880", "红利ETF"),
]


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _get_gbk(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("gbk", "replace")


def fetch_hot_concepts(top_n: int = 5) -> List[Dict[str, Any]]:
    """取今日涨幅靠前的概念板块（已剔除统计类/资金类概念）。"""
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:3"
        "&fields=f3,f12,f14,f104,f105,f128"
    )
    try:
        data = json.loads(_get(url))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[热门板块] 拉取概念排行失败：%s: %s", type(exc).__name__, exc)
        return []

    diff = ((data.get("data") or {}).get("diff")) or []
    out: List[Dict[str, Any]] = []
    for it in diff:
        name = str(it.get("f14") or "").strip()
        pct = it.get("f3")
        if not name or pct in (None, "-"):
            continue
        if any(w in name for w in _BLOCK_WORDS):
            continue
        up = it.get("f104") or 0
        dn = it.get("f105") or 0
        if up + dn < 10:  # 成分股太少的细分概念，参考意义弱
            continue
        try:
            out.append({
                "name": name,
                "pct": float(pct),
                "up": int(up),
                "dn": int(dn),
                "leader": str(it.get("f128") or ""),
            })
        except (TypeError, ValueError):
            continue
        if len(out) >= top_n:
            break
    return out


def _etf_for(concept: str) -> Optional[Tuple[str, str]]:
    for keywords, code, short in _ETF_RULES:
        if any(k in concept for k in keywords):
            return code, short
    return None


def _quote_and_ma20(code: str) -> Optional[Dict[str, Any]]:
    """取 ETF 现价、今日涨跌幅、20 日均线。"""
    prefix = "sh" if code.startswith(("5", "6")) else "sz"
    try:
        txt = _get_gbk(f"https://qt.gtimg.cn/q={prefix}{code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[热门板块] ETF %s 行情失败：%s", code, exc)
        return None

    price = pct = name = None
    for block in txt.split(";"):
        block = block.strip()
        if not block or "=" not in block:
            continue
        payload = block.split('"')
        if len(payload) < 2:
            continue
        v = payload[1].split("~")
        if len(v) < 33:
            continue
        name = v[1]
        try:
            price = float(v[3])
            pct = float(v[32])
        except (TypeError, ValueError):
            continue
        break
    if not price:
        return None

    ma20 = None
    try:
        raw = _get(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,60,qfq"
        )
        d = json.loads(raw)
        rows = ((d.get("data") or {}).get(prefix + code) or {})
        rows = rows.get("qfqday") or rows.get("day") or []
        closes = [float(r[2]) for r in rows]
        if len(closes) >= 20:
            ma20 = round(sum(closes[-20:]) / 20, 3)
    except Exception:  # noqa: BLE001
        ma20 = None

    return {"name": name, "price": price, "pct": pct, "ma20": ma20}


def _advice(quote: Dict[str, Any], concept_pct: float) -> Tuple[str, str]:
    """返回 (结论标签, 一句话理由)。结论必须是可执行动作，不写"观望"。"""
    price = quote.get("price")
    pct = quote.get("pct") or 0.0
    ma20 = quote.get("ma20")

    if concept_pct >= 4.0 or pct >= 3.0:
        return "⚠️ 别追", f"板块今天已涨 {concept_pct:.1f}%、ETF 涨 {pct:.1f}%，追高容易买在情绪高点，等回踩再看"
    if ma20 and price and price < ma20:
        gap = (price / ma20 - 1) * 100
        if gap <= -8:
            return "🔻 趋势坏", (
                f"板块虽热（+{concept_pct:.1f}%），但 ETF 现价 {price} 比 20 日线 {ma20} 低 {abs(gap):.1f}%，"
                f"中期趋势明显走坏——**板块热 ≠ 趋势好**，别抄这个底，等站回 {ma20} 再谈"
            )
        return "⏸ 等站回", f"ETF 现价 {price} 仍在 20 日线 {ma20} 下方（{gap:.1f}%），站回 {ma20} 再买"
    if ma20 and price:
        gap = (price / ma20 - 1) * 100
        return "🟢 可小仓", (
            f"板块涨 {concept_pct:.1f}%、ETF 站上 20 日线 {ma20}（+{gap:.1f}%），"
            f"可小仓参与（≤总仓位 10%），跌破 {ma20} 走"
        )
    return "🟡 可关注", f"板块涨 {concept_pct:.1f}%，ETF 涨 {pct:.1f}%，均线数据缺失，先小仓试探"


def build_sector_picks(top_n: int = 5) -> List[Dict[str, Any]]:
    """热门概念 -> 对应 ETF -> 可执行建议。失败返回空列表。"""
    # 多取一些概念，因为部分冷门概念没有对应 ETF，需要往下找补
    try:
        concepts = fetch_hot_concepts(top_n=top_n * 10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[热门板块] 失败：%s: %s", type(exc).__name__, exc)
        return []

    rows: List[Dict[str, Any]] = []
    seen_etf = set()  # 同一只 ETF 只保留排名最靠前的那个概念，避免「机器人执行器/减速器/人形机器人」重复三条
    for c in concepts:
        if len(rows) >= top_n:
            break
        match = _etf_for(c["name"])
        if not match:
            continue
        code, short = match
        if code in seen_etf:
            continue
        quote = _quote_and_ma20(code)
        if not quote:
            continue
        seen_etf.add(code)
        label, reason = _advice(quote, c["pct"])
        rows.append({
            "concept": c["name"],
            "concept_pct": round(c["pct"], 2),
            "up": c["up"],
            "dn": c["dn"],
            "leader": c["leader"],
            "etf_code": code,
            "etf_name": short,
            "etf_price": quote["price"],
            "etf_pct": quote["pct"],
            "ma20": quote.get("ma20"),
            "label": label,
            "reason": reason,
        })
    return rows


# ---------------------------------------------------------------- ETF 持仓
# ETF 与个股的三处硬差异，决定了它不能直接套用个股阈值：
#   1. 报价三位小数（0.559），按 2 位四舍五入会把支撑压力位算错
#   2. 免印花税、佣金低（万 0.5~2），做T的摩擦成本远低于个股，振幅门槛可放宽
#   3. 日内波动天然比个股小，涨 6% 已属单边极端，沿用 9.5% 的涨停阈值永远不触发
# 份额单位是「份」，买卖同样必须 100 份起。
ETF_DIGITS = 3
ETF_MIN_AMPLITUDE_FOR_T = 1.5
ETF_SURGE_PCT = 6.0
# ETF 一年波动通常只有个股的六七成，跌 8% 才谈补仓等于永远不补，放宽到 5%
ETF_MIN_LOSS_FOR_ADD = 5.0


def _pick_levels(price: float, ma5: Optional[float], ma10: Optional[float],
                 ma20: Optional[float], low5: Optional[float], high5: Optional[float],
                 low20: Optional[float], high20: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """ETF 的「最近支撑 / 最近压力」。

    直接用 20 日最低/最高会得到 0.481~0.604 这种 25% 宽的区间，
    做T判断永远是「中间地带不做」、走势预判也给不出有用信息。
    这里改用：支撑 = 现价下方最近的一条均线（MA20→MA10→MA5），
    压力 = 近 5 日高点。既贴近现价，又是交易上真正会起作用的价位。
    """
    support = None
    for level in (ma20, ma10, ma5):
        if level and price and level < price * 0.998:
            support = level
            break
    if not support:
        support = low5 if (low5 and price and low5 < price) else low20

    resistance = None
    if high5 and price and high5 > price * 1.002:
        resistance = high5
    elif high20 and price and high20 > price:
        resistance = high20
    return support, resistance


def _etf_detail(code: str) -> Optional[Dict[str, Any]]:
    """取 ETF 的现价、涨跌幅、振幅、量比、成交额与 MA5/MA10/MA20、20 日高低。"""
    prefix = "sh" if code.startswith(("5", "6")) else "sz"
    try:
        txt = _get_gbk(f"https://qt.gtimg.cn/q={prefix}{code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ETF持仓] %s 行情失败：%s", code, exc)
        return None

    price = pct = name = None
    amplitude = volume_ratio = amount_wan = None
    for block in txt.split(";"):
        block = block.strip()
        if "=" not in block:
            continue
        payload = block.split('"')
        if len(payload) < 2:
            continue
        v = payload[1].split("~")
        if len(v) < 33:
            continue
        name = v[1]
        try:
            price = float(v[3])
            pct = float(v[32])
        except (TypeError, ValueError):
            continue

        def _pick(idx: int) -> Optional[float]:
            try:
                return float(v[idx])
            except (TypeError, ValueError, IndexError):
                return None

        # 腾讯行情字段：37 成交额（万元）、43 振幅（%）、49 量比
        amount_wan, amplitude, volume_ratio = _pick(37), _pick(43), _pick(49)
        break
    if not price:
        return None

    ma5 = ma10 = ma20 = low20 = high20 = low5 = high5 = None
    try:
        raw = _get(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,60,qfq"
        )
        d = json.loads(raw)
        node = ((d.get("data") or {}).get(prefix + code) or {})
        rows = node.get("qfqday") or node.get("day") or []
        closes = [float(r[2]) for r in rows]
        highs = [float(r[3]) for r in rows]
        lows = [float(r[4]) for r in rows]
        if len(closes) >= 20:
            ma5 = round(sum(closes[-5:]) / 5, ETF_DIGITS)
            ma10 = round(sum(closes[-10:]) / 10, ETF_DIGITS)
            ma20 = round(sum(closes[-20:]) / 20, ETF_DIGITS)
            low20 = round(min(lows[-20:]), ETF_DIGITS)
            high20 = round(max(highs[-20:]), ETF_DIGITS)
            low5 = round(min(lows[-5:]), ETF_DIGITS)
            high5 = round(max(highs[-5:]), ETF_DIGITS)
    except Exception:  # noqa: BLE001
        pass

    return {
        "name": name, "price": price, "pct": pct,
        "amplitude": amplitude, "volume_ratio": volume_ratio,
        "amount_wan": amount_wan,
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "low5": low5, "high5": high5, "low20": low20, "high20": high20,
    }


def build_etf_positions(etf_positions: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """ETF 持仓 → 补仓判断 + 走势预判 + 做T建议 + 接下来怎么做。

    与个股走**同一套判断函数**（_evaluate / _forecast_one / _t_plan_one），
    只把精度、振幅门槛、单边强势阈值换成 ETF 口径，避免两边结论打架。
    失败返回空列表，不影响主报告。
    """
    from src.services.forecast import _forecast_one, _t_plan_one, build_next_action
    from src.services.positions import _evaluate

    rows: List[Dict[str, Any]] = []
    for code, info in (etf_positions or {}).items():
        try:
            detail = _etf_detail(code)
            if not detail:
                continue
            cost = float(info.get("cost") or 0)
            shares = info.get("shares")
            if cost <= 0:
                continue

            price = detail["price"]
            support, resistance = _pick_levels(
                price, detail.get("ma5"), detail.get("ma10"), detail.get("ma20"),
                detail.get("low5"), detail.get("high5"),
                detail.get("low20"), detail.get("high20"),
            )
            market: Dict[str, Any] = {
                "price": price,
                "pct_chg": detail.get("pct"),
                "amplitude": detail.get("amplitude"),
                "volume_ratio": detail.get("volume_ratio"),
                "ma5": detail.get("ma5"),
                "ma10": detail.get("ma10"),
                "ma20": detail.get("ma20"),
                "support": support,
                "resistance": resistance,
            }

            evaluated = _evaluate(price, cost, market["ma5"], market["ma10"],
                                  market["ma20"], market["support"],
                                  market["resistance"], digits=ETF_DIGITS,
                                  min_loss=ETF_MIN_LOSS_FOR_ADD)
            forecast = _forecast_one(price, market, digits=ETF_DIGITS)
            t_plan = _t_plan_one(price, cost, market, shares,
                                 digits=ETF_DIGITS,
                                 min_amp=ETF_MIN_AMPLITUDE_FOR_T,
                                 surge_pct=ETF_SURGE_PCT, unit="份")
            next_action = build_next_action(
                evaluated.get("state", ""), price, market["ma5"],
                market["support"], market["resistance"], None, digits=ETF_DIGITS)

            t_line = ""
            if t_plan.get("side") == "high" and t_plan.get("sell_at") and t_plan.get("buy_at"):
                t_line = (f"先卖 **{t_plan['sell_at']}** → 回落到 **{t_plan['buy_at']}** 买回，"
                          f"成本约降 **{t_plan['saving']} 元/份**")
            elif t_plan.get("side") == "low" and t_plan.get("buy_at") and t_plan.get("sell_at"):
                t_line = (f"先买 **{t_plan['buy_at']}** → 反弹到 **{t_plan['sell_at']}** 卖出，"
                          f"成本约降 **{t_plan['saving']} 元/份**")

            rows.append({
                "code": code,
                "name": info.get("name") or detail.get("name") or code,
                "cost": cost,
                "shares": shares,
                "price": price,
                "pct": detail.get("pct"),
                "pnl_pct": round((price / cost - 1) * 100, 2),
                "pnl_amount": round((price - cost) * shares, 2) if shares else None,
                # 补仓判断（与个股同源）
                "state": evaluated.get("state") or "⚪ 数据不足",
                "reason": evaluated.get("reason") or "",
                # 接下来怎么做
                "next_action": next_action,
                # 走势预判
                "fc_bias": forecast.get("bias"),
                "fc_note": forecast.get("note"),
                "fc_basis": forecast.get("basis"),
                # 做T
                "t_verdict": t_plan.get("verdict") or "⚪ 无数据",
                "t_line": t_line,
                "t_reason": t_plan.get("reason") or "",
                # 参考位
                "ma5": market["ma5"],
                "ma20": market["ma20"],
                "support": market["support"],
                "resistance": market["resistance"],
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ETF持仓] %s 计算失败：%s", code, exc)
            continue
    return rows
