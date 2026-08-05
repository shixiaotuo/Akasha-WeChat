# -*- coding: utf-8 -*-
"""
集成测试：模拟 Akasha Bridge 作为 OneBot WS 客户端，
向后端发送一条群消息事件，验证后端能否调用 WorkBuddy 并把回复
经 send_group_msg API 回传。

用法（需 venv 已装 websockets/requests）：
    venv\Scripts\python.exe test_backend.py
"""
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import websockets  # noqa: E402
import workbuddy_backend as wb  # noqa: E402


async def fake_bridge(uri, holder):
    async with websockets.connect(uri) as ws:
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 999,
            "user_id": 111,
            "message": [
                {"type": "at", "data": {"qq": 1}},
                {"type": "text", "data": {"text": " 张三在群测试中说：用一句话介绍你自己"}},
            ],
            "sender": {"nickname": "张三"},
        }
        await ws.send(json.dumps(event))
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
            except Exception:
                break
            obj = json.loads(raw)
            if obj.get("action") in ("send_group_msg", "send_private_msg"):
                holder.append(obj)
                print("FAKE_BRIDGE_RECV>", json.dumps(obj, ensure_ascii=False)[:240])


async def run():
    cfg = dict(wb.DEFAULT_CONFIG)
    cfg["serve_port"] = 8099          # 避开可能的残留 8080
    cfg["ob_ws_port"] = 11230
    cfg["auto_launch_serve"] = True
    cfg["system_prompt"] = ""         # 测试时不带提示词，减少变量

    client = wb.WorkBuddyClient(cfg["serve_host"], cfg["serve_port"], 150)
    mgr = wb.ServeManager(cfg)
    try:
        mgr.ensure(client)
    except Exception as e:
        print("SERVE_FAIL>", e)
        return 2

    bot = wb.BotServer(client, cfg)
    holder = []
    server = await wb.serve(
        bot.handler, cfg["ob_ws_host"], cfg["ob_ws_port"],
        ping_interval=20, ping_timeout=20,
    )
    try:
        await asyncio.wait_for(
            fake_bridge(f"ws://{cfg['ob_ws_host']}:{cfg['ob_ws_port']}", holder),
            timeout=130,
        )
    except Exception as e:
        print("TEST_ERR>", e)
    finally:
        server.close()
        mgr.stop()

    if holder:
        text = "".join(
            seg.get("data", {}).get("text", "")
            for m in holder
            for seg in m.get("params", {}).get("message", [])
            if isinstance(seg, dict)
        )
        print(f"VERDICT_OK reply_len={len(text)} sample={text[:140]}")
        return 0
    print("VERDICT_FAIL 未收到回复 API")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()) or 0)
