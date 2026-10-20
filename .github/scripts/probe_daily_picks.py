#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GitHub Actions 环境验证「每日推荐」选股链路。仅用于临时验证。"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())


def main() -> int:
    t0 = time.time()
    from src.services.daily_picks import generate_daily_picks, render_daily_picks  # noqa: E402

    print(f"LITELLM_MODEL={os.getenv('LITELLM_MODEL')}", flush=True)
    print(f"DAILY_PICKS_STRATEGY={os.getenv('DAILY_PICKS_STRATEGY')}", flush=True)
    print(f"DAILY_PICKS_COUNT={os.getenv('DAILY_PICKS_COUNT')}", flush=True)
    print("-" * 60, flush=True)

    payload = generate_daily_picks()
    elapsed = time.time() - t0

    print("-" * 60, flush=True)
    print(f"耗时: {elapsed:.1f} 秒", flush=True)

    if payload is None:
        print("RESULT: payload=None（选股失败或已关闭）", flush=True)
        print("VERDICT: fail", flush=True)
        return 0

    print(f"策略: {payload.get('strategy')} ({payload.get('strategy_display')})", flush=True)
    print(f"全市场快照: {payload.get('snapshot_count')}", flush=True)
    print(f"硬性筛后: {payload.get('after_filter_count')}", flush=True)
    picks = payload.get("picks") or []
    print(f"入选: {len(picks)} 只", flush=True)
    for p in picks:
        print(
            f"  #{p.get('rank')} {p.get('name')}({p.get('code')}) "
            f"价格={p.get('price')} 涨幅={p.get('change_pct')} 分={p.get('score')} "
            f"行业={p.get('industry')!r}",
            flush=True,
        )
        print(f"      理由={str(p.get('reason'))[:80]}", flush=True)
    if payload.get("degradation"):
        print(f"降级提示: {payload.get('degradation')}", flush=True)

    section = render_daily_picks(payload)
    print("-" * 60, flush=True)
    print(f"渲染段落字节数: {len(section.encode('utf-8'))}", flush=True)
    print(section[:1200], flush=True)

    print("-" * 60, flush=True)
    print(f"VERDICT: ok-picks{len(picks)}-{int(elapsed)}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
