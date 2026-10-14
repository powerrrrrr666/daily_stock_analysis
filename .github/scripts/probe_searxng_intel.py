#!/usr/bin/env python3
"""使用仓库自身的 SearchService 验证 SearXNG 公共实例是否真能搜到 A 股新闻。

仅用于临时验证，验证后删除。
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from src.search_service import SearchService  # noqa: E402

STOCKS = [
    ("002414", "高德红外"),
    ("300308", "中际旭创"),
    ("600418", "江淮汽车"),
]


def main() -> int:
    svc = SearchService(
        searxng_base_urls=None,
        searxng_public_instances_enabled=True,
    )

    available = [p.name for p in svc._providers if p.is_available]
    print(f"可用搜索提供者: {available}")
    print("-" * 60)

    total_ok = 0
    total_dim = 0

    for code, name in STOCKS:
        print(f"\n===== {name}({code}) =====")
        try:
            resp_map = svc.search_comprehensive_intel(code, name, max_searches=3)
        except Exception as exc:  # noqa: BLE001
            print(f"  搜索异常: {type(exc).__name__}: {exc}")
            continue

        for dim, resp in resp_map.items():
            total_dim += 1
            mark = "OK " if (resp.success and resp.results) else "MISS"
            if resp.success and resp.results:
                total_ok += 1
            print(f"  [{mark}] {dim} | provider={resp.provider} | 结果数={len(resp.results or [])}")
            for item in (resp.results or [])[:2]:
                title = getattr(item, "title", None) or (
                    item.get("title") if isinstance(item, dict) else ""
                )
                print(f"        - {str(title)[:80]}")
            if not resp.success:
                print(f"        err={str(resp.error_message)[:120]}")

    print("-" * 60)
    print(f"SUMMARY dims_with_results={total_ok} total_dims={total_dim}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
