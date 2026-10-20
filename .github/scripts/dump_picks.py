#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出「今日推荐」选股结果为 JSON（供本地合并进报告）。"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.getcwd())


def main() -> int:
    from src.services.daily_picks import generate_daily_picks

    try:
        payload = generate_daily_picks()
    except Exception:
        traceback.print_exc()
        print("VERDICT: fail", flush=True)
        return 0

    if payload is None:
        print("VERDICT: nopayload", flush=True)
        return 0

    with open("out/picks.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    picks = payload.get("picks") or []
    print(
        f"snapshot={payload.get('snapshot_count')} "
        f"filtered={payload.get('after_filter_count')} picks={len(picks)}",
        flush=True,
    )
    print(f"VERDICT: ok-picks{len(picks)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
