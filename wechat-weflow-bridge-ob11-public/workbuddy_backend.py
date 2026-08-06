# -*- coding: utf-8 -*-
"""
WorkBuddy OneBot v11 后端（替代 AstrBot 角色）。

架构：
    微信 → WeFlow → Akasha Bridge(OneBot WS 客户端)
         → 本程序(OneBot WS 服务端, 端口 11229)  ← 你正在看的这个文件
         → WorkBuddy --serve HTTP API (端口 8080)
         → 回复经 send_group_msg / send_private_msg 回流到微信

为什么不用 `codebuddy -p`：
    独立 `codebuddy -p` 子进程在真实环境会卡死（无法复用桌面端会话），
    而 `codebuddy --serve` 常驻 HTTP 服务稳定可用（已实测）。

调用链路：
    POST /api/v1/runs  (generic message: id/type/text/sender)
      → 返回 {"data":{"runId":...}}
    GET  /api/v1/runs/{runId}/stream  (SSE)
      → event: message  data: {"content":{"markdown":"..."},"status":"completed"}
      → event: done      data: {}
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import requests
from websockets.asyncio.server import ServerConnection, serve

# ============ 配置 ============
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = {
    # WorkBuddy --serve HTTP 服务
    "serve_host": "127.0.0.1",
    "serve_port": 8080,
    "auto_launch_serve": True,                 # 若 serve 未启动则自动拉起
    "cli_path": r"D:/WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy",
    "node_path": "node",
    "request_timeout": 600,                    # 单次问答整体/读取上限(秒)；hy3 推理模型思考期 SSE 静默，需留足余量
    # 角色（人格）选择：指向 personas/<persona>.md。也可在 config 里直接写 "system_prompt" 显式覆盖。
    # serve 的 generic adapter 不读取 body 里的 skills 字段，因此让 bot 具备某角色人格，
    # 最可靠的方式就是把该角色的浓缩提示词写进 system_prompt（由 personas/*.md 提供，启动时读取）。
    "persona": "zhangxuefeng",
    # 模型：通过启动 serve 时的 CODEBUDDY_MODEL 环境变量强制（已在 ServeManager 注入）。
    # 合法 id 是 "hy3-preview-agent"（或 "hy3-preview"）；"hy3" 不是合法 id，会被忽略。
    # 留空字符串 "" 则使用 serve 默认模型（Auto→本机默认 glm）。
    "model": "hy3-preview-agent",
    # OneBot WS 服务端（Bridge 会连这个地址，默认与 Bridge 配置一致）
    "ob_ws_host": "127.0.0.1",
    "ob_ws_port": 11229,
    # 回复行为
    "reply_chunk_size": 1000,                  # 单条微信消息最大字符数（超长自动分段）
    "max_concurrent": 4,                       # 并发处理消息数上限
}


def load_config():
    path = os.path.join(HERE, "wb_config.json")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[warn] 读取 wb_config.json 失败，使用默认配置: {e}")
    else:
        # 首次运行：写出默认配置，方便用户按需修改
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"[info] 已生成默认配置: {path}")
        except Exception:
            pass
    return cfg


# ============ 角色（人格）提示词解析 ============
# 兜底默认提示词：仅当既无显式 system_prompt、又无对应 personas/<name>.md 时使用。
DEFAULT_SYSTEM_PROMPT = (
    "你是微信里的助手「赛博老农」，由 WorkBuddy（hy3）驱动。"
    "用简体中文、面向群里普通读者，正常回答问题即可。"
)


def resolve_system_prompt(cfg):
    """解析最终用于 --system-prompt 的文案。

    优先级：config 里的显式 system_prompt ＞ personas/{persona}.md ＞ 兜底默认。
    """
    # 1) 显式 system_prompt 优先（兼容老习惯：有人在 config 里直接写整段提示词）
    sp = (cfg.get("system_prompt") or "").strip()
    if sp:
        return sp
    # 2) 否则按 persona 名读取 personas/<name>.md
    name = (cfg.get("persona") or "").strip()
    if name:
        p = os.path.join(HERE, "personas", f"{name}.md")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    return content
                log.warning(f"[persona] personas/{name}.md 为空，使用默认提示词")
            except Exception as e:
                log.warning(f"[persona] 读取 personas/{name}.md 失败: {e}")
        else:
            log.warning(f"[persona] 未找到 personas/{name}.md，使用默认提示词")
    # 3) 兜底
    return DEFAULT_SYSTEM_PROMPT


# ============ 日志 ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("workbuddy-ob11")


# ============ WorkBuddy HTTP 客户端 ============
class WorkBuddyClient:
    def __init__(self, host, port, timeout, cfg=None):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout
        self.headers = {
            "X-CodeBuddy-Request": "1",
            "Content-Type": "application/json",
        }
        self.session = requests.Session()

    def health(self) -> bool:
        try:
            r = self.session.get(
                self.base + "/api/v1/health", headers=self.headers, timeout=5
            )
            if r.status_code != 200:
                return False
            return r.json().get("data", {}).get("status") == "ok"
        except Exception:
            return False

    def ask(self, text: str) -> str:
        """发起一次 Agent 执行并流式取回回复文本。"""
        payload = {
            "id": f"u{int(time.time() * 1000)}",
            "type": "user",
            "text": text,
            "sender": {"id": "wechatbot", "name": "WeChatBot"},
        }
        # 注意：serve 的 generic adapter 的 parseInbound 只读取
        # id/type/source/payload(text)/action/callback/timeoutMs，
        # 并不读取 body 里的 model / skills 字段，因此模型与技能不能在
        # 这里注入，而必须靠启动 serve 时的环境变量（CODEBUDDY_MODEL）
        # 与 --system-prompt 来设定（见 ServeManager._launch）。
        resp = self.session.post(
            self.base + "/api/v1/runs",
            headers=self.headers,
            json=payload,
            timeout=(10, 30),  # POST 仅取 runId：连接10s、整体30s 快速失败
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"runs 错误: {body['error']}")
        run_id = body.get("data", {}).get("runId")
        if not run_id:
            raise RuntimeError(f"runs 未返回 runId: {body}")
        return self._stream(run_id)

    def _stream(self, run_id: str) -> str:
        url = self.base + f"/api/v1/runs/{run_id}/stream"
        reply = ""
        event_type = None
        with self.session.get(
            url,
            headers={**self.headers, "Accept": "text/event-stream"},
            stream=True,
            # timeout 为元组：(连接超时, 读取间隔超时)。
            # 连接要快失败；读取间隔用 request_timeout 撑过 hy3 推理模型的
            # 长思考静默期（期间 SSE 流可能长时间不推送事件）。
            timeout=(10, self.timeout),
        ) as resp:
            # 关键修复：SSE 流必须按 UTF-8 字节解码。
            # 不能依赖 requests 的 decode_unicode（服务端未声明 charset 时会
            # 默认用 ISO-8859-1 解码，导致中文变成 çèµ·æ¥ 这类乱码）。
            for raw in resp.iter_lines(decode_unicode=False):
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace")
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                    continue
                if line.startswith("data:"):
                    data_str = line[len("data:"):].strip()
                    if not data_str:
                        continue
                    try:
                        obj = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if event_type == "message":
                        content = obj.get("content", {}) or {}
                        piece = content.get("markdown") or content.get("text") or ""
                        if piece:
                            # 覆盖式累积：流式分片时取最新完整内容
                            reply = piece
                    elif event_type == "done":
                        break
        return reply.strip()


# ============ serve 进程管理 ============
class ServeManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        # 启动时解析一次角色提示词（personas/<name>.md 或显式 system_prompt）
        self.system_prompt = resolve_system_prompt(cfg)

    def ensure(self, client: WorkBuddyClient):
        """确保 serve 可用：已运行则直接返回；否则自动拉起。"""
        if client.health():
            log.info("[serve] 检测到已运行的 --serve 服务，复用之")
            return
        if not self.cfg.get("auto_launch_serve", True):
            raise RuntimeError("[serve] serve 未运行且 auto_launch_serve=false，请手动启动")

        log.info("[serve] 未检测到 serve，尝试自动拉起...")
        self._launch(with_prompt=True)

    def _launch(self, with_prompt: bool):
        cfg = self.cfg
        cmd = [
            cfg["node_path"], cfg["cli_path"],
            "--serve", "--port", str(cfg["serve_port"]),
        ]
        if with_prompt and self.system_prompt:
            cmd += ["--system-prompt", self.system_prompt]
        env = dict(os.environ)
        env["CODEBUDDY_GATEWAY_AUTH"] = "none"  # 本地无认证，最省心
        # 强制 serve 使用指定模型。源码中模型解析逻辑为
        #   el = process.env.CODEBUDDY_MODEL || settingsManager.get("model")
        # 即 CODEBUDDY_MODEL 优先级最高，会覆盖 settings 与默认模型；
        # 且它是进程级环境变量，只影响本 bot 的 serve 实例，
        # 不会改变你桌面端 WorkBuddy 的默认模型（实测默认是 "Auto"→glm）。
        model = (cfg.get("model") or "").strip()
        if model:
            env["CODEBUDDY_MODEL"] = model
        logfile = os.path.join(HERE, "logs", "workbuddy_serve.log")
        os.makedirs(os.path.dirname(logfile), exist_ok=True)
        log.info(f"[serve] 启动命令: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=open(logfile, "a", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
        )
        # 轮询健康检查
        for i in range(40):
            time.sleep(1)
            if os.path.exists("/dev/null") or True:  # 兼容 Windows
                pass
            if client_health(self.cfg):
                log.info("[serve] ✅ 启动成功")
                return
        # 带 system-prompt 失败 → 降级：不带提示词重试一次
        if with_prompt and self.system_prompt:
            log.warning("[serve] 带系统提示词启动失败，尝试不带提示词重拉...")
            self.stop()
            self._launch(with_prompt=False)
            return
        raise RuntimeError("[serve] 自动拉起失败，请查看 logs/workbuddy_serve.log")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def client_health(cfg):
    try:
        r = requests.get(
            f"http://{cfg['serve_host']}:{cfg['serve_port']}/api/v1/health",
            headers={"X-CodeBuddy-Request": "1"}, timeout=3,
        )
        return r.status_code == 200 and r.json().get("data", {}).get("status") == "ok"
    except Exception:
        return False


# ============ 文本工具 ============
def extract_text(message) -> str:
    """从 OneBot message 段提取纯文本（跳过 at 元素）。"""
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, list):
        return ""
    parts = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
        # at / face / image 等不计入提问文本
    return "".join(parts).strip()


def split_text(text: str, size: int = 1000):
    """按换行/句号分段，超长则硬切，避免单条微信消息过长。"""
    if not text:
        return [""]
    if len(text) <= size:
        return [text]
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if len(cur) + len(ln) + 1 <= size:
            cur = (cur + "\n" + ln) if cur else ln
        else:
            if cur:
                chunks.append(cur)
            if len(ln) > size:
                for i in range(0, len(ln), size):
                    chunks.append(ln[i:i + size])
                cur = ""
            else:
                cur = ln
    if cur:
        chunks.append(cur)
    return chunks or [text]


# ============ Bot 服务端 ============
class BotServer:
    def __init__(self, wb: WorkBuddyClient, cfg):
        self.wb = wb
        self.cfg = cfg
        self.connections = set()
        self.semaphore = asyncio.Semaphore(cfg["max_concurrent"])

    async def handler(self, ws: ServerConnection):
        peer = getattr(ws, "remote_address", "?")
        log.info(f"[ws] Bridge 已连接: {peer}")
        self.connections.add(ws)
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # 只处理消息事件；echo/应答由 Bridge 自发自收，忽略
                if data.get("post_type") == "message":
                    asyncio.create_task(self.handle_message(ws, data))
                else:
                    log.debug(f"[ws] 忽略非消息帧: {list(data.keys())}")
        except Exception as e:
            log.warning(f"[ws] 连接异常: {e}")
        finally:
            self.connections.discard(ws)
            log.info(f"[ws] Bridge 断开: {peer}")

    async def handle_message(self, ws: ServerConnection, event: dict):
        async with self.semaphore:
            text = extract_text(event.get("message", []))
            if not text:
                return
            mtype = event.get("message_type", "private")
            who = event.get("sender", {}).get("nickname", "") or event.get("user_id", "")
            log.info(f"[msg] 收到({mtype}) {who}: {text[:60]}")

            try:
                reply = await asyncio.to_thread(self.wb.ask, text)
            except Exception as e:
                log.error(f"[msg] WorkBuddy 调用失败: {e}")
                reply = "（AI 暂时开小差了，请稍后再试～）"

            if not reply:
                reply = "（没有得到回复）"
            await self.send_reply(ws, event, reply)

    async def send_reply(self, ws: ServerConnection, event: dict, reply: str):
        mtype = event.get("message_type", "private")
        if mtype == "group":
            action = "send_group_msg"
            target_key = "group_id"
            target = event.get("group_id")
        else:
            action = "send_private_msg"
            target_key = "user_id"
            target = event.get("user_id")

        if target is None:
            log.warning(f"[reply] 缺少目标 ID，无法回复: {event.get('post_type')}")
            return

        chunks = split_text(reply, self.cfg["reply_chunk_size"])
        for idx, ch in enumerate(chunks):
            echo = f"wb{int(time.time()*1000)}_{idx}"
            msg = {
                "action": action,
                "params": {
                    target_key: target,
                    "message": [{"type": "text", "data": {"text": ch}}],
                },
                "echo": echo,
            }
            try:
                await ws.send(json.dumps(msg, ensure_ascii=False))
            except Exception as e:
                log.error(f"[reply] 发送失败: {e}")
                break
            # 分段之间稍作停顿，避免微信风控/刷屏
            if len(chunks) > 1:
                await asyncio.sleep(0.8)
        log.info(f"[reply] 已回发 {len(chunks)} 段到 {mtype} target={target}")


# ============ 主流程 ============
def main():
    cfg = load_config()
    wb = WorkBuddyClient(cfg["serve_host"], cfg["serve_port"], cfg["request_timeout"], cfg)
    log.info(f"[config] 模型={cfg.get('model') or 'serve默认(CODEBUDDY_MODEL未设)'} "
              f"(经 CODEBUDDY_MODEL 环境变量强制)；nihaixia 人格经 --system-prompt 注入")
    serve_mgr = ServeManager(cfg)

    # 确保 serve 可用
    try:
        serve_mgr.ensure(wb)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)

    bot = BotServer(wb, cfg)

    # 退出时清理 serve 子进程
    def _cleanup(*_):
        log.info("[main] 正在退出，停止 serve 子进程...")
        serve_mgr.stop()
        os._exit(0)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    log.info(f"[main] 启动 OneBot WS 服务端 ws://{cfg['ob_ws_host']}:{cfg['ob_ws_port']}")

    async def _run():
        async with serve(
            bot.handler,
            cfg["ob_ws_host"],
            cfg["ob_ws_port"],
            ping_interval=20,
            ping_timeout=20,
        ):
            log.info("[main] ✅ 已监听，等待 Bridge 连接... (Ctrl+C 退出)")
            await asyncio.Future()  # 永久运行

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        _cleanup()


if __name__ == "__main__":
    main()
