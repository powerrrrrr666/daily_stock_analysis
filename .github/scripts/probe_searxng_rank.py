#!/usr/bin/env python3
"""按 search_service.py 相同逻辑，从 searx.space 选出成功率最高的公共实例。

仅供临时连通性测试使用。
"""
import json
import sys


def main() -> int:
    try:
        with open("out/instances.json", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"读取实例列表失败: {exc}")
        return 1

    instances = payload.get("instances")
    if not isinstance(instances, dict):
        print("实例列表格式异常")
        return 1

    rows = []
    for url, item in instances.items():
        if not isinstance(url, str) or not isinstance(item, dict):
            continue
        if item.get("network_type") != "normal":
            continue
        if (item.get("http") or {}).get("status_code") != 200:
            continue
        timing = (item.get("timing") or {}).get("search") or {}
        uptime = timing.get("success_percentage")
        if not isinstance(uptime, (int, float)) or float(uptime) <= 0:
            continue
        rows.append((float(uptime), url.rstrip("/")))

    rows.sort(key=lambda row: (-row[0], row[1]))
    top = rows[:12]

    with open("out/pool.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(url for _, url in top))

    with open("out/ranked.txt", "w", encoding="utf-8") as fh:
        for uptime, url in top:
            fh.write(f"{uptime:6.1f}%\t{url}\n")

    print(f"候选实例数: {len(top)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
