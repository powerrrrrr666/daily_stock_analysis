#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GitHub Actions 环境验证「每日推荐」选股链路。仅用于临时验证。"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.getcwd())


def main() -> int:
    t0 = time.time()

    print(f"LITELLM_MODEL={os.getenv('LITELLM_MODEL')}", flush=True)
    print(f"DAILY_PICKS_STRATEGY={os.getenv('DAILY_PICKS_STRATEGY')}", flush=True)
    print(f"DAILY_PICKS_COUNT={os.getenv('DAILY_PICKS_COUNT')}", flush=True)
    print("-" * 60, flush=True)

    # ---- 步骤 1：确认选股配置对象本身是否正常 ----
    try:
        from src.services.screening.config import Config as ScreeningConfig

        cfg = ScreeningConfig.from_env()
        print(f"[1] ScreeningConfig.from_env() -> {type(cfg).__module__}.{type(cfg).__name__}", flush=True)
        print(f"    hasattr strategies_dir = {hasattr(cfg, 'strategies_dir')}", flush=True)
        if hasattr(cfg, "strategies_dir"):
            d = cfg.strategies_dir
            print(f"    strategies_dir = {d} | is_dir={d.is_dir() if hasattr(d, 'is_dir') else '?'}", flush=True)
        print(f"    llm_model = {getattr(cfg, 'llm_model', '?')}", flush=True)
        print(f"    llm_api_key set = {bool(getattr(cfg, 'llm_api_key', ''))}", flush=True)
    except Exception:
        print("[1] 配置构造失败：", flush=True)
        traceback.print_exc()
        print("VERDICT: fail-config", flush=True)
        return 0

    print("-" * 60, flush=True)

    # ---- 步骤 2：直接调用选股引擎，暴露完整堆栈 ----
    from src.services.screening.pipeline import screen

    try:
        result = screen(
            os.getenv("DAILY_PICKS_STRATEGY") or "bull_trend",
            market="cn",
            max_output=int(os.getenv("DAILY_PICKS_COUNT") or 5),
            use_llm=True,
            daily_enrich=True,
            config=cfg,
        )
    except Exception:
        print("[2] screen() 抛异常，完整堆栈：", flush=True)
        traceback.print_exc()
        print("VERDICT: fail-screen", flush=True)
        return 0

    elapsed = time.time() - t0
    picks = list(getattr(result, "picks", []) or [])
    print("-" * 60, flush=True)
    print(f"[2] screen() 成功，耗时 {elapsed:.1f}s", flush=True)
    print(
        f"    快照={getattr(result, 'snapshot_count', None)} "
        f"筛后={getattr(result, 'after_filter_count', None)} 入选={len(picks)}",
        flush=True,
    )
    for p in picks:
        print(
            f"    #{getattr(p,'rank',None)} {getattr(p,'name','')}({getattr(p,'code','')}) "
            f"价={getattr(p,'price',None)} 涨={getattr(p,'change_pct',None)} "
            f"分={getattr(p,'final_score',None)} 行业={getattr(p,'industry','')!r}",
            flush=True,
        )
        print(f"       理由={str(getattr(p,'ranking_reason',''))[:80]}", flush=True)
    deg = getattr(result, "degradation", None)
    if deg:
        print(f"    降级提示: {deg}", flush=True)

    # ---- 步骤 3：验证转换 + 渲染 ----
    from src.services.daily_picks import _pick_to_dict, render_daily_picks

    payload = {
        "strategy": os.getenv("DAILY_PICKS_STRATEGY") or "bull_trend",
        "strategy_display": "多头趋势",
        "snapshot_count": getattr(result, "snapshot_count", 0) or 0,
        "after_filter_count": getattr(result, "after_filter_count", 0) or 0,
        "market_view": getattr(result, "llm_market_view", "") or "",
        "picks": [_pick_to_dict(p) for p in picks],
    }
    section = render_daily_picks(payload)
    print("-" * 60, flush=True)
    print(f"[3] 渲染字节数 = {len(section.encode('utf-8'))}", flush=True)
    print(section[:1500], flush=True)
    print("-" * 60, flush=True)
    print(f"VERDICT: ok-picks{len(picks)}-{int(elapsed)}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
