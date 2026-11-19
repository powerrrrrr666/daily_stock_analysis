# -*- coding: utf-8 -*-
"""ETF 底层资产画像 + 消息面定性。

解决的问题：报告里两只 ETF 一个受益、一个承压，但只写「震荡/偏弱」看不出差别。
这里补三层信息，**逐层降级，凑不出来就不显示**：

1. **装的是什么**（硬数据）
   天天基金季报 → 前十大持仓 + 占净值比例。抓不到（新基金未披露）就只写跟踪标的。

2. **在交易什么**（新闻事实）
   Tavily 搜近 7 天该主题的新闻，取标题原文，不做二次加工。

3. **方向判断**（只在逻辑清晰时给）
   用「产业链位置 × 信号类型」的矩阵。比如「上游制造 + 供给收缩」→ 偏多（卡的是海外，
   装的是替代方）；「中游运营 + 供给收缩」→ 偏空（上游断供，扩张受限）。
   位置判断不出来、或信号不明确时，**不给方向**，只写「多空交织，盯住 X 变量」。

设计原则：
- 任何异常都降级，绝不阻断报告
- 宁可少说，不硬凑。四级信息能出几级算几级
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
    "Referer": "https://fundf10.eastmoney.com/",
}
_TIMEOUT = 20

# ---------------------------------------------------------------- 产业链位置
# 用来判断「消息对这个品种是利多还是利空」。判不出来的就不给方向，
# 因为「影响」这件事高度依赖产业链位置，瞎套会得出反向结论。
_UPSTREAM_MFG = ("半导体", "芯片", "集成电路", "设备", "材料", "封测", "光刻")
_MIDSTREAM_OPS = ("算力", "数据中心", "IDC", "云计算", "服务器", "租赁")
_DOWNSTREAM = ("消费", "白酒", "食品", "医药", "医疗", "创新药", "家电", "旅游")
_CYCLICAL = ("有色", "煤炭", "钢铁", "化工", "稀土", "黄金", "贵金属")
_FINANCIALS = ("银行", "证券", "券商", "保险", "红利")

_POSITION_LABEL = {
    "upstream_mfg": "上游制造",
    "midstream_ops": "中游运营",
    "downstream": "下游消费",
    "cyclical": "周期资源",
    "financials": "金融",
    "unknown": "",
}

# 信号类型：从新闻标题里读出来的"发生了什么"
# 搜索结果里这些是常青内容（百科/教程/问答），对判断"今天为什么涨"毫无用处
_JUNK_TITLE = ("百科", "wiki", "维基", "知乎", "问答", "知道", "文库", "教程",
               "入门", "什么是", "全讲透", "一文", "官网", "首页")

_SIGNAL_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("supply_cut", ("出口管制", "出口限制", "禁令", "断供", "封锁", "许可证", "实体清单", "关税")),
    ("demand_boost", ("扩产", "订单", "招标", "需求爆发", "涨价", "提价", "供不应求")),
    ("policy_support", ("国产替代", "自主可控", "政策支持", "补贴", "大基金", "专项")),
    ("price_drop", ("降价", "价格战", "产能过剩", "去库存")),
]

# 「位置 × 信号」→ 方向。只写逻辑真正成立的组合，其余留空让调用方降级。
_IMPACT_MATRIX: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("upstream_mfg", "supply_cut"): ("偏多", "卡的是海外供给，这只装的是替代方，供给越紧订单越确定"),
    ("upstream_mfg", "policy_support"): ("偏多", "国产替代是它最直接的需求来源"),
    ("upstream_mfg", "demand_boost"): ("偏多", "订单端在放量，业绩能见度提升"),
    ("upstream_mfg", "price_drop"): ("偏空", "价格战会直接压毛利"),
    ("midstream_ops", "supply_cut"): ("偏空", "上游芯片供给受限，算力扩张节奏被压住"),
    ("midstream_ops", "price_drop"): ("偏空", "算力价格下行，转租模式利差收窄"),
    ("midstream_ops", "demand_boost"): ("偏多", "算力需求在起量，上架率是核心变量"),
    ("cyclical", "demand_boost"): ("偏多", "涨价直接改善资源品盈利"),
    ("cyclical", "price_drop"): ("偏空", "价格下行压制盈利"),
}


def _get(url: str, timeout: int = _TIMEOUT) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def classify_position(name: str) -> str:
    """由 ETF 名称判断产业链位置。判不出来返回 unknown。"""
    text = name or ""
    for keys in (_UPSTREAM_MFG,):
        if any(k in text for k in keys):
            return "upstream_mfg"
    if any(k in text for k in _MIDSTREAM_OPS):
        return "midstream_ops"
    if any(k in text for k in _DOWNSTREAM):
        return "downstream"
    if any(k in text for k in _CYCLICAL):
        return "cyclical"
    if any(k in text for k in _FINANCIALS):
        return "financials"
    return "unknown"


def query_of(name: str) -> str:
    """从 ETF 名称里抽出搜索用的主题词。

    「半导体ETF鹏华」→「半导体」，「创业板算力ETF广发」→「算力」。
    抽不出就退回名称去掉 ETF 字样。
    """
    for keys in (_UPSTREAM_MFG, _MIDSTREAM_OPS, _DOWNSTREAM, _CYCLICAL, _FINANCIALS):
        for k in keys:
            if k in (name or ""):
                return k
    return (name or "").replace("ETF", "").strip()


def fetch_holdings(code: str, top: int = 5) -> Optional[Dict[str, Any]]:
    """取 ETF 前十大持仓（季报）。新基金未披露时返回 None。"""
    url = (f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?"
           f"type=jjcc&code={code}&topline=10&year=&month=&rt=0.1")
    try:
        html = _get(url)
    except Exception as exc:  # noqa: BLE001
        logger.info("[ETF画像] %s 持仓获取失败：%s: %s", code, type(exc).__name__, exc)
        return None

    names = re.findall(r"<td class='tol'><a[^>]*>([^<]+)</a></td>", html)
    pcts = re.findall(r"<td class='tor'>([\d.]+)%</td>", html)
    if not names:
        return None

    quarter = re.search(r"(\d{4}年\d季度)", html)
    items = []
    for i, nm in enumerate(names[:top]):
        pct = pcts[i] if i < len(pcts) else None
        items.append((nm.strip(), pct))
    return {"items": items, "quarter": quarter.group(1) if quarter else ""}


def _signal_types(titles: List[str]) -> List[str]:
    joined = " ".join(titles)
    hits = []
    for sig, keys in _SIGNAL_RULES:
        if any(k in joined for k in keys):
            hits.append(sig)
    return hits


def fetch_news(query: str, days: int = 7, max_results: int = 5) -> List[str]:
    """搜该主题近 N 天新闻标题。失败返回空列表。"""
    keys = [k.strip() for k in (os.getenv("TAVILY_API_KEYS") or "").split(",") if k.strip()]
    if not keys:
        return []
    try:
        from tavily import TavilyClient
    except ImportError:
        return []
    try:
        client = TavilyClient(api_key=keys[0])
        # topic="news" 是关键：默认 general 会返回百科和教程，
        # 2026-11-19 首次实跑就搜回「半导体 - 维基百科」这种，全废。
        resp = client.search(
            query=query, search_depth="basic", topic="news",
            max_results=max_results + 3, days=days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[ETF画像] 新闻搜索失败：%s: %s", type(exc).__name__, exc)
        return []

    out: List[str] = []
    for r in (resp.get("results") or []):
        title = str(r.get("title") or "").strip().replace("\n", " ")
        if not title or title in out:
            continue
        if any(j in title for j in _JUNK_TITLE):
            continue
        out.append(title[:70] + ("…" if len(title) > 70 else ""))
        if len(out) >= max_results:
            break
    return out


def build_etf_thesis(name: str, code: str, query_hint: str = "") -> Dict[str, Any]:
    """产出 ETF 的三层信息。任何一层失败就少一层，不影响报告。

    返回字段：
      holdings_line  装的是什么（前五大持仓 + 季报季度）
      news_line      在交易什么（新闻标题原文，最多 2 条）
      thesis_line    方向判断（判不出来为空）
      position       产业链位置（内部用）
    """
    result: Dict[str, Any] = {"holdings_line": "", "news_line": "",
                              "thesis_line": "", "position": ""}

    # ---- 第一层：持仓
    hold = fetch_holdings(code)
    if hold:
        parts = [f"{n} {p}%" if p else n for n, p in hold["items"]]
        q = f"（{hold['quarter']}）" if hold.get("quarter") else ""
        result["holdings_line"] = "、".join(parts) + q
    else:
        result["holdings_line"] = "季报未披露，参见基金合同与跟踪标的成分"

    # ---- 第二层：新闻
    position = classify_position(name)
    result["position"] = _POSITION_LABEL.get(position, "")
    query = query_hint or f"{query_of(name)} 板块 最新消息"
    titles = fetch_news(query)
    if titles:
        result["news_line"] = "；".join(titles[:2])

    # ---- 第三层：方向（只在矩阵有解时给）
    sigs = _signal_types(titles)
    for sig in sigs:
        verdict = _IMPACT_MATRIX.get((position, sig))
        if verdict:
            direction, why = verdict
            result["thesis_line"] = f"{direction}——{why}"
            break
    if not result["thesis_line"] and titles:
        result["thesis_line"] = "多空交织，暂时给不出干脆方向，以盘面走势为准"

    return result
