#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
部署前置检查：验证本机 WorkBuddy `--serve` HTTP 链路是否可用。

本脚本会临时拉起一个 `codebuddy --serve` 服务（none 认证），
然后实测 health → runs → SSE stream 是否能拿到真实回复。
测试结束后自动关闭该临时服务。

用法：  venv\Scripts\python.exe check_cli.py
"""
import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests  # noqa: E402
from workbuddy_backend import (  # noqa: E402
    WorkBuddyClient, ServeManager, DEFAULT_CONFIG,
)


def main():
    print("=" * 54)
    print("WorkBuddy --serve 链路可用性检查")
    print("=" * 54)

    # 1. node
    node_path = shutil.which(DEFAULT_CONFIG["node_path"])
    print(f"[1] node 检测：{node_path or '未找到'}")
    if not node_path:
        print("    ❌ 请确保 node 在 PATH 中")
        return 1

    # 2. CLI 文件
    cli = DEFAULT_CONFIG["cli_path"]
    print(f"[2] CLI 路径：{cli}")
    if not os.path.isfile(cli):
        print("    ❌ CLI 文件不存在，请检查 wb_config.json 的 cli_path")
        return 1

    # 3. 临时拉起 serve 并测试
    print("[3] 临时拉起 codebuddy --serve 并测试（约 10~40 秒）...")
    cfg = dict(DEFAULT_CONFIG)
    wb = WorkBuddyClient(cfg["serve_host"], cfg["serve_port"], 120)
    mgr = ServeManager(cfg)
    try:
        mgr._launch(with_prompt=False)
    except Exception as e:
        print(f"    ❌ serve 拉起失败：{e}")
        print("        排查：WorkBuddy 桌面端是否已登录运行？node 是否 ≥18.20？")
        return 2

    try:
        # health
        if not wb.health():
            print("    ❌ health 检查失败")
            return 2
        print("    ✅ health 正常")

        # runs + stream
        probe = "请只回复四个字：连通正常"
        reply = wb.ask(probe)
        if not reply:
            print("    ❌ 拿到了 runId 但 stream 无回复内容")
            return 2
        print(f"    ✅ 真实回复（{len(reply)} 字）：{reply[:80]}")
    except Exception as e:
        print(f"    ❌ 调用异常：{e}")
        return 2
    finally:
        mgr.stop()

    print("-" * 54)
    print("    结论：WorkBuddy --serve 链路可用，可以继续部署微信 Bot。")
    print("    下一步：按 DEPLOY.md 启动 WeFlow → 本后端 → Bridge。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
