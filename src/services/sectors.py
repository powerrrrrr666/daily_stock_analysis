# -*- coding: utf-8 -*-
"""热门板块 + 对应 ETF 建议。

数据源：
  1. 东方财富概念板块排行 —— 找出今日最热的概念（涨跌家数 + 主力净流入）
  2. 腾讯行情 —— 取对应 ETF 的现价、涨跌幅、20 日均线位置
  3. Tavily 搜索 —— 取板块的消息面催化（需 TAVILY_API_KEYS，缺失时静默跳过）

设计原则：
  - 任何异常都只记日志并返回空列表，**绝不阻断主报告**
  - 不给模糊建议，每个 ETF 必须落到一句可执行结论 + 价位
  - 消息面只引用真实新闻标题，**不让 LLM 编产业逻辑**——编出来的原因无法验证
"""
import json
import logging
import os
import time
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
    # 2026-11-19 新增：这几类是资金属性/风格指数，不是可交易的产业主线
    "大基金", "做市商", "茅指数", "宁组合", "证金持股", "机构调研",
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


def _get(url: str, referer: Optional[str] = None) -> str:
    headers = dict(_UA)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _get_gbk(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("gbk", "replace")


def _fetch_from_eastmoney(top_n: int = 5) -> List[Dict[str, Any]]:
    """取今日涨幅靠前的概念板块（已剔除统计类/资金类概念）。"""
    # 2026-11-19 东财主域名返回 502（IP 被临时限流），整块热门板块直接消失。
    # 改成三个域名轮询 + 每个重试一次，任一个通就够用。
    path = (
        "/api/qt/clist/get?pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:3"
        "&fields=f3,f12,f14,f62,f104,f105,f128"
    )
    data = None
    last_err: Any = None
    for host in ("push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com"):
        for _ in range(2):
            try:
                data = json.loads(_get(f"https://{host}{path}", referer="https://quote.eastmoney.com/"))
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(1)
        if data:
            break
    if not data:
        logger.warning("[热门板块] 拉取概念排行失败（三个域名都试过）：%s: %s",
                       type(last_err).__name__, last_err)
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
        try:
            net_inflow = float(it.get("f62"))
        except (TypeError, ValueError):
            net_inflow = None
        if up + dn < 10:  # 成分股太少的细分概念，参考意义弱
            continue
        try:
            out.append({
                "name": name,
                "pct": float(pct),
                "up": int(up),
                "dn": int(dn),
                "leader": str(it.get("f128") or ""),
                "net_inflow": net_inflow,
            })
        except (TypeError, ValueError):
            continue
        if len(out) >= top_n:
            break
    return out


def _sector_news(concept: str, keys: Optional[List[str]] = None) -> Optional[str]:
    """取该板块最近的消息面标题。

    2026-11-20 从 Tavily 换成东方财富资讯搜索：Tavily 实跑两次都返回
    百科条目和股吧讨论区，中文财经场景不适用。东财是原生中文源，
    带精确发布时间，按时间倒序，且不需要密钥。
    keys 参数保留只是为了兼容旧调用，已不再使用。
    """
    try:
        from src.services.etf_profile import fetch_eastmoney_news
        # 板块名常带「概念/风格/板块」后缀，连在一起几乎不会出现在新闻标题里
        kw = concept
        for suffix in ("概念", "风格", "板块", "指数"):
            kw = kw.replace(suffix, "")
        items = fetch_eastmoney_news(kw or concept, days=3, limit=2)
        return "；".join(items) if items else None
    except Exception as exc:  # noqa: BLE001
        logger.info("[热门板块] %s 消息面获取失败：%s: %s", concept, type(exc).__name__, exc)
        return None


def _hot_why(pct: float, up: int, dn: int, leader: str,
             net_inflow: Optional[float]) -> str:
    """解释这个板块「为什么热」——只用可量化的盘面事实，不编产业逻辑。

    能确定的三件事：涨跌家数（广度）、主力资金净流入（钱从哪来）、领涨股（谁在带）。
    这三条凑起来，足以判断这波是「真普涨」还是「少数票拉指数」。
    """
    total = up + dn
    breadth = (up / total * 100) if total else 0
    parts: List[str] = []

    if breadth >= 80:
        parts.append(f"{up} 只涨 {dn} 只跌，几乎全线飘红")
    elif breadth >= 60:
        parts.append(f"{up} 涨 {dn} 跌，多数跟涨")
    elif breadth >= 40:
        parts.append(f"{up} 涨 {dn} 跌，内部分化明显")
    else:
        parts.append(f"只有 {up} 只涨、{dn} 只跌，靠少数票拉起来")

    if net_inflow:
        yi = net_inflow / 1e8
        rising = pct > 0
        if yi >= 10:
            parts.append(f"主力净流入 {yi:.1f} 亿，"
                         + ("属于资金抢筹" if rising else "跌的时候还有钱在接"))
        elif yi >= 3:
            parts.append(f"主力净流入 {yi:.1f} 亿，"
                         + ("钱在不断进来" if rising else "有资金逆势承接"))
        elif yi > 0:
            parts.append(f"主力净流入 {yi:.1f} 亿，力度一般")
        elif yi <= -3:
            parts.append(f"主力净流出 {abs(yi):.1f} 亿，"
                         + ("涨得有点虚" if rising else "资金在撤"))
        else:
            parts.append(f"主力小幅净流出 {abs(yi):.1f} 亿")

    if leader:
        parts.append(f"领涨 {leader}")

    return "，".join(parts)


def _hot_verdict(up: int, dn: int, net_inflow: Optional[float]) -> str:
    """一句话判断这波热度能不能延续。"""
    total = up + dn
    breadth = (up / total * 100) if total else 0
    inflow = (net_inflow or 0) / 1e8

    if breadth >= 70 and inflow >= 3:
        return "普涨 + 资金进场，这种热度通常能延续几天"
    if breadth >= 70 and inflow < 0:
        return "涨得广但资金在撤，多半是一日游，别追"
    if breadth < 50 and inflow >= 5:
        return "资金集中打少数票，跟风盘容易接在高位"
    if breadth < 50:
        return "少数票拉动，参与度低，不建议追"
    if inflow <= -2:
        return "涨面看着不错，但主力在净流出，涨势不牢，别追"
    return "温度中等，看明天量能能否跟上"


def _fetch_from_tencent(limit: int) -> List[Dict[str, Any]]:
    """腾讯概念板块排行 —— 海外 IP 也能拿到。

    为什么换源：东方财富的 push2 接口对 GitHub runner（美国 IP）持续返回 502，
    三个域名都试过。2026-11-19 两期报告的热门板块因此整块消失。
    腾讯这个接口对海外 IP 正常，且字段齐全（涨跌幅、涨跌家数、主力净流入、领涨股）。

    sort_type/direct 这两个参数在腾讯侧不生效，返回顺序是乱的，
    所以直接取 200 条回来自己按涨跌幅排序。
    """
    url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
           "?board_type=gn&sort_type=price&direct=down&offset=0&count=200")
    try:
        raw = _get(url, referer="https://stockapp.finance.qq.com/")
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[热门板块] 腾讯接口失败：%s: %s", type(exc).__name__, exc)
        return []

    out: List[Dict[str, Any]] = []
    for it in ((data.get("data") or {}).get("rank_list") or []):
        name = str(it.get("name") or "").strip()
        if not name or any(w in name for w in _BLOCK_WORDS):
            continue
        try:
            pct = float(it.get("zdf"))
        except (TypeError, ValueError):
            continue

        up = dn = 0
        zgb = str(it.get("zgb") or "")
        if "/" in zgb:
            try:
                a, b = zgb.split("/", 1)
                up, dn = int(a), int(b)
            except ValueError:
                up = dn = 0
        if up + dn < 10:      # 成分股太少的细分概念，参考意义弱
            continue

        try:
            # 腾讯 zljlr 单位是万元，统一换算成元，跟东财 f62 口径一致
            net_inflow = float(it.get("zljlr")) * 10000
        except (TypeError, ValueError):
            net_inflow = None

        leader = str(((it.get("lzg") or {}).get("name")) or "").strip()
        out.append({
            "name": name, "pct": pct, "up": up, "dn": dn,
            "leader": leader, "net_inflow": net_inflow,
        })

    out.sort(key=lambda r: -r["pct"])
    return out[:limit]


def fetch_hot_concepts(top_n: int = 5) -> List[Dict[str, Any]]:
    """今日热门概念。优先腾讯，失败再试东方财富。"""
    rows = _fetch_from_tencent(top_n * 10)
    if rows:
        return rows
    logger.info("[热门板块] 腾讯无数据，回退东方财富")
    return _fetch_from_eastmoney(top_n * 10)


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


def _advice(quote: Dict[str, Any], concept_pct: float, mood: str = "") -> Tuple[str, str]:
    """返回 (结论标签, 一句话理由)。

    mood 来自 _hot_verdict（板块自身的赚钱效应）。板块没参与度时，
    即使 ETF 站上 20 日线也不能说「可小仓」——2026-11-20 就出现过
    「板块 10 涨 42 跌、资金撤」却标 🟢 可小仓的自相矛盾。
    """
    price = quote.get("price")
    pct = quote.get("pct") or 0.0
    ma20 = quote.get("ma20")
    word = "涨" if concept_pct > 0 else "跌"
    weak = any(k in mood for k in ("不建议追", "别追", "接在高位", "涨势不牢"))

    if concept_pct >= 4.0 or pct >= 3.0:
        return "⚠️ 别追", f"板块今天已{word} {abs(concept_pct):.1f}%、ETF 涨 {pct:.1f}%，追高容易买在情绪高点，等回踩再看"
    if ma20 and price and price < ma20:
        gap = (price / ma20 - 1) * 100
        if gap <= -8:
            return "🔻 趋势坏", (
                f"ETF 现价 {price} 比 20 日线 {ma20} 低 {abs(gap):.1f}%，中期趋势明显走坏——"
                f"**板块热 ≠ 趋势好**，别抄这个底，等站回 {ma20} 再谈"
            )
        return "⏸ 等站回", f"ETF 现价 {price} 仍在 20 日线 {ma20} 下方（{gap:.1f}%），站回 {ma20} 再买"
    if ma20 and price:
        gap = (price / ma20 - 1) * 100
        if weak:
            return "⏸ 先别碰", (
                f"ETF 技术面站上 20 日线 {ma20}（+{gap:.1f}%），但板块{mood}——"
                f"**只是指数在撑，不是真有行情**，等板块赚钱效应回来再说"
            )
        return "🟢 可小仓", (
            f"板块{word} {abs(concept_pct):.1f}%、ETF 站上 20 日线 {ma20}（+{gap:.1f}%），"
            f"可小仓参与（≤总仓位 10%），跌破 {ma20} 走"
        )
    return "🟡 可关注", f"板块{word} {abs(concept_pct):.1f}%，ETF 涨 {pct:.1f}%，均线数据缺失，先小仓试探"


def build_sector_picks(top_n: int = 5) -> List[Dict[str, Any]]:
    """热门概念 -> 对应 ETF -> 可执行建议。失败返回空列表。"""
    tavily_keys = [k.strip() for k in (os.getenv("TAVILY_API_KEYS") or "").split(",") if k.strip()]
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
        why = _hot_why(c["pct"], c["up"], c["dn"], c["leader"], c.get("net_inflow"))
        why_verdict = _hot_verdict(c["up"], c["dn"], c.get("net_inflow"))
        label, reason = _advice(quote, c["pct"], why_verdict)
        rows.append({
            "concept": c["name"],
            "concept_pct": round(c["pct"], 2),
            "up": c["up"],
            "dn": c["dn"],
            "leader": c["leader"],
            "why": why,
            "why_verdict": why_verdict,
            "etf_code": code,
            "etf_name": short,
            "etf_price": quote["price"],
            "etf_pct": quote["pct"],
            "ma20": quote.get("ma20"),
            "label": label,
            "reason": reason,
            "news": None,
        })

    # 消息面：并发搜前几个板块的驱动新闻，任一失败都不影响其他字段
    if rows:
        try:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=3) as pool:
                found = list(pool.map(lambda r: _sector_news(r["concept"]), rows))
            for row, news in zip(rows, found):
                row["news"] = news
            hit = sum(1 for n in found if n)
            logger.info("[热门板块] 消息面命中 %s/%s", hit, len(rows))
        except Exception as exc:  # noqa: BLE001
            logger.info("[热门板块] 消息面批量搜索失败：%s: %s", type(exc).__name__, exc)
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
    from src.services.etf_profile import build_etf_thesis
    from src.services.forecast import (_forecast_one, _t_plan_one, _take_profit_one,
                                       build_next_action, TP_MIN_GAIN_ETF)
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

            # 止盈目标用 20 日高点（不是近 5 日高）——做T看短线，止盈要看向上的空间
            tp = _take_profit_one(
                price, cost, market, shares, digits=ETF_DIGITS, unit="份",
                min_gain=TP_MIN_GAIN_ETF, resistance=detail.get("high20"),
            )

            etf_name = info.get("name") or detail.get("name") or code
            try:
                thesis = build_etf_thesis(etf_name, code, pct_chg=detail.get("pct"))
            except Exception as exc:  # noqa: BLE001
                logger.info("[ETF持仓] %s 画像失败：%s: %s", code, type(exc).__name__, exc)
                thesis = {}

            rows.append({
                "code": code,
                "name": etf_name,
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
                # 止盈
                "tp_state": tp.get("tp_state"),
                "tp_price": tp.get("tp_price"),
                "tp_line": tp.get("tp_line"),
                "tp_reason": tp.get("tp_reason"),
                # 底层资产画像（持仓 / 新闻 / 方向），任一缺失就少一行
                **thesis,
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
