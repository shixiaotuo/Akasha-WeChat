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
    # 角色（人格）选择：按名查找。
    # 查找顺序：显式 system_prompt ＞ 本机 Skill(~/.workbuddy/skills/<persona>/SKILL.md) ＞
    #           项目 personas/<persona>.md ＞ 兜底默认。
    # 优先读本机 Skill：bot 直接用你安装的「真实 Skill 全文」，且你更新 Skill 时自动同步。
    # 注：serve 的 generic adapter 不读取 body 里的 skills 字段，故通过
    #     --system-prompt-file 注入（解决长文本命令行长度上限）。
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


def _find_local_skill(name: str):
    """在本机 ~/.workbuddy/skills/ 下查找名为 <name> 的 Skill 的 SKILL.md。

    支持精确目录名匹配，以及「目录名 ↔ persona 名互相包含」的模糊匹配
    （兼容 persona='zhangxuefeng' 但目录实为 'zhangxuefeng-perspective' 的情况）。
    返回 SKILL.md 路径或 None。
    """
    base = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")
    if not os.path.isdir(base):
        return None
    exact = os.path.join(base, name, "SKILL.md")
    if os.path.exists(exact):
        return exact
    low_n = name.lower()
    for d in os.listdir(base):
        dp = os.path.join(base, d)
        if os.path.isdir(dp):
            low_d = d.lower()
            if low_n in low_d or low_d in low_n:
                p = os.path.join(dp, "SKILL.md")
                if os.path.exists(p):
                    return p
    return None


def resolve_system_prompt(cfg):
    """解析最终用于 system prompt 的文案。

    优先级：config 里的显式 system_prompt ＞
            本机 WorkBuddy Skill（~/.workbuddy/skills/<persona>/SKILL.md）＞
            项目内 personas/<persona>.md ＞ 兜底默认。
    优先读本机 Skill：这样 bot 直接用你安装的「真实 Skill 全文」，
    且你更新 Skill 时 bot 自动同步，不必再维护两份。
    """
    # 1) 显式 system_prompt 优先（兼容老习惯：有人在 config 里直接写整段提示词）
    sp = (cfg.get("system_prompt") or "").strip()
    if sp:
        return sp
    # 2) 按 persona 名查找：本机 Skill 优先，其次项目 personas
    name = (cfg.get("persona") or "").strip()
    if name:
        skill_path = _find_local_skill(name)
        candidates = []
        if skill_path:
            dirname = os.path.basename(os.path.dirname(skill_path))
            candidates.append((skill_path, f"本机 Skill ~/.workbuddy/skills/{dirname}/SKILL.md"))
        candidates.append((os.path.join(HERE, "personas", f"{name}.md"), f"项目 personas/{name}.md"))
        for p, label in candidates:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        log.info(f"[persona] 使用角色文件: {label}")
                        return content
                    log.warning(f"[persona] {label} 为空，尝试下一个")
                except Exception as e:
                    log.warning(f"[persona] 读取 {label} 失败: {e}")
        log.warning(f"[persona] 未找到 {name} 的 Skill / persona 文件，使用默认提示词")
    # 3) 兜底
    return DEFAULT_SYSTEM_PROMPT


# ============ 角色文本提取（防超长 Skill 撑爆上下文） ============
PERSONA_MAX_CHARS = 9000  # 约 6000-9000 token，hy3 上下文可控；超过则硬截断
# 人格核心章节白名单关键词（命中则保留）；其余章节（研究流程/时间线/谱系/附录/实测等）丢弃。
_PERSONA_KEEP = ["角色扮演", "身份", "心智模型", "启发式", "表达", "诚实", "框架",
                "世界观", "核心", "价值观", "我拒绝", "我追求", "我拒绝的"]
_PERSONA_SKIP = ["工作流", "协议", "checkpoint", "时间线", "谱系", "附录", "实测",
                 "反例", "研究", "来源", "引用", "动态", "最新", "关键引用", "调研"]


def _extract_persona_core(text: str) -> str:
    """从本机 Skill 全文提取「人格核心」，避免把超大 Skill（如 nihaixia 32万字）全量灌入 system prompt。

    策略：按 '## ' 章节切分，只保留白名单章节（角色/身份/心智模型/启发式/表达/诚实/价值观等），
    其余（研究流程、时间线、谱系、附录、实测、反例黑名单等）丢弃；最后硬截断到 PERSONA_MAX_CHARS。
    """
    if "## " not in text:
        core = text
    else:
        sections, cur_h, cur_b = [], None, []
        for line in text.splitlines():
            if line.startswith("## ") and not line.startswith("### "):
                if cur_h is not None:
                    sections.append((cur_h, "\n".join(cur_b)))
                cur_h = line[3:].strip()
                cur_b = [line]
            elif cur_h is not None:
                cur_b.append(line)
        if cur_h is not None:
            sections.append((cur_h, "\n".join(cur_b)))
        kept = []
        for h, b in sections:
            if any(k in h for k in _PERSONA_SKIP):
                continue
            if any(k in h for k in _PERSONA_KEEP):
                kept.append(b)
        core = "\n\n".join(kept).strip() if kept else text
    if len(core) > PERSONA_MAX_CHARS:
        core = core[:PERSONA_MAX_CHARS] + "\n\n…（本机 Skill 内容较长，已截断保留人格核心；完整版见 ~/.workbuddy/skills）"
    return core


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
        self.cfg = cfg or {}
        self.mode = (self.cfg.get("llm_mode") or "serve").strip().lower()
        self.headers = {
            "X-CodeBuddy-Request": "1",
            "Content-Type": "application/json",
        }
        self.session = requests.Session()
        # 解析角色提示词（完整 raw）；agent 模式额外准备 system prompt 文件
        self.system_prompt = resolve_system_prompt(self.cfg)
        self.agent_sp_file = None
        if self.mode == "agent":
            self.agent_sp_file = self._write_agent_sysprompt()
        # 对话历史（按人记忆）：conv_key -> [{role,content}, ...]
        hcfg = (cfg or {}).get("history") or {}
        self.history_enabled = bool(hcfg.get("enabled", False))
        self.history_max_turns = int(hcfg.get("max_turns") or 20)
        self.history_by = (hcfg.get("by") or "person").strip().lower()
        self.history = {}

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

    def ask(self, text: str, conv_key: str = None) -> str:
        """发起一次 Agent 执行并取回回复文本。

        - serve 模式：POST /api/v1/runs + SSE 流式（generic adapter，无工具能力）
        - agent 模式：subprocess 调 `codebuddy -p` 完整 agent（自带 WebSearch 等工具，可联网）
        模式由 config 的 llm_mode 决定（"serve" 默认，或 "agent"）。
        conv_key 用于按人拼接历史上下文；None 时不记历史。
        """
        if self.mode == "agent":
            return self._ask_agent(text, conv_key)
        return self._ask_serve(text, conv_key)

    # —— 对话历史（按人记忆）——
    def _conv_text(self, text: str, conv_key) -> str:
        """把最近历史拼到当前问题前，作为一次性上下文前缀。"""
        if not self.history_enabled or not conv_key:
            return text
        hist = self.history.get(conv_key, [])
        if not hist:
            return text
        recent = hist[-(self.history_max_turns * 2):]
        lines = [f"【对话历史（最近 {len(recent)//2} 轮，仅用于理解上下文，勿复述历史全文）】\n"]
        for m in recent:
            role = "用户" if m["role"] == "user" else "助手"
            lines.append(f"{role}：{m['content']}")
        lines.append("【当前问题】")
        lines.append(text)
        return "\n".join(lines)

    def record_turn(self, conv_key, user_text, assistant_text=None):
        """记录一轮对话。assistant_text=None 表示本轮助手未成功回复（只记用户侧）。"""
        if not self.history_enabled or not conv_key:
            return
        h = self.history.setdefault(conv_key, [])
        h.append({"role": "user", "content": user_text})
        if assistant_text is not None:
            h.append({"role": "assistant", "content": assistant_text})
        max_msgs = self.history_max_turns * 2
        if len(h) > max_msgs:
            self.history[conv_key] = h[-max_msgs:]

    def _ask_serve(self, text: str, conv_key: str = None) -> str:
        full_text = self._conv_text(text, conv_key)
        payload = {
            "id": f"u{int(time.time() * 1000)}",
            "type": "user",
            "text": full_text,
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

    def _write_agent_sysprompt(self):
        """agent 模式专用 system prompt 文件：完整 raw（含工作流），超长则提取核心。"""
        path = os.path.join(HERE, "logs", "_agent_sysprompt.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = self.system_prompt
        content = raw if len(raw) <= 20000 else _extract_persona_core(raw)
        env_note = (
            "\n\n【运行环境说明】你运行在微信 bot 后端（codebuddy -p 完整 agent 模式），"
            "可以调用 WebSearch 等工具查询实时数据（院校、专业、就业、政策、新闻等）。"
            "请主动查证你不确定或有时间敏感性的事实；对最终仍无法核实的内容，坦诚说明不确定，不要编造。"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content + env_note)
        return path

    def _ask_agent(self, text: str, conv_key: str = None) -> str:
        """agent 模式：codebuddy -p 完整 agent，真正加载 Skill 工具（含 WebSearch），可联网查数据。

        关键参数：
        - --no-session-persistence：每次调用独立、不落盘，彻底避免与桌面端 WorkBuddy 会话冲突。
        - -y (bypassPermissions) + --disallowedTools 禁掉 Bash/Edit/Write 等危险操作，
          仅保留 WebSearch/Read 等只读/查询能力，防止微信群陌生消息诱导执行命令或删文件。
        - --max-turns：限制 agent 轮数，防止检索/推理失控死循环。
        """
        cfg = self.cfg
        acfg = cfg.get("agent") or {}
        max_turns = int(acfg.get("max_turns") or 10)
        timeout = int(acfg.get("timeout") or 120)
        disallowed = acfg.get("disallowed_tools") or "Bash,Edit,Write,PowerShell,Shell"
        full_text = self._conv_text(text, conv_key)
        cmd = [
            cfg["node_path"], cfg["cli_path"], "-p",
            "--system-prompt-file", self.agent_sp_file,
            "--no-session-persistence",
            "-y",
            "--max-turns", str(max_turns),
            "--disallowedTools", disallowed,
            "--output-format", "text",
            full_text,
        ]
        env = dict(os.environ)
        env["CODEBUDDY_GATEWAY_AUTH"] = "none"
        model = (cfg.get("model") or "").strip()
        if model:
            env["CODEBUDDY_MODEL"] = model
        log.info(f"[agent] 调用 codebuddy -p (max_turns={max_turns}, disallowed={disallowed})")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            log.error(f"[agent] 执行超时({timeout}s)，已中止")
            return "（这次想得有点久，超时啦，换个简短的问题试试～）"
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "")[:600].strip()
            log.error(f"[agent] 返回码 {proc.returncode}: {err}")
            return "（AI 出错了，请稍后再试～）"
        out = (proc.stdout or "").strip()
        if not out:
            out = "（没有得到回复）"
        log.info(f"[agent] 回复长度 {len(out)}")
        return out

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
        self.mode = (cfg.get("llm_mode") or "serve").strip().lower()
        self.use_serve = self.mode != "agent"
        # 启动时解析一次角色提示词（personas/<name>.md 或显式 system_prompt）
        self.system_prompt = resolve_system_prompt(cfg)

    def ensure(self, client: WorkBuddyClient):
        """确保 serve 可用：已运行则直接返回；否则自动拉起。
        agent 模式下不启动 --serve（由 WorkBuddyClient 走 codebuddy -p 子进程）。"""
        if not self.use_serve:
            log.info("[serve] agent 模式：不启动 --serve，改由 WorkBuddyClient 走 codebuddy -p 子进程")
            return
        if client.health():
            log.info("[serve] 检测到已运行的 --serve 服务，复用之")
            return
        if not self.cfg.get("auto_launch_serve", True):
            raise RuntimeError("[serve] serve 未运行且 auto_launch_serve=false，请手动启动")

        log.info("[serve] 未检测到 serve，尝试自动拉起...")
        self._launch(with_prompt=True)

    def _write_sysprompt_file(self):
        """把 system_prompt 写入文件，用 --system-prompt-file 注入。

        原因：本机真实 Skill 全文可能 >1 万字，远超 Windows 命令行参数长度上限；
        且需追加「无外部工具」的运行环境说明，防止 Skill 里「必须用 WebSearch」
        类指令在 serve 的 generic 模式下导致 bot 幻觉（谎称已查询）。
        """
        path = os.path.join(HERE, "logs", "_sysprompt.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        env_note = (
            "\n\n【运行环境说明】你当前运行在微信 bot 后端（codebuddy --serve 的 generic 模式），"
            "无法调用 WebSearch、文件读写等外部工具。涉及具体专业、院校、行业、政策的最新数据，"
            "若没有确切把握，请坦诚说明「这点我不太确定 / 我得查一下」，不要谎称已查询或使用编造的数据；"
            "其余人格表达与判断照常。"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(_extract_persona_core(self.system_prompt) + env_note)
        return path

    def _launch(self, with_prompt: bool):
        cfg = self.cfg
        cmd = [
            cfg["node_path"], cfg["cli_path"],
            "--serve", "--port", str(cfg["serve_port"]),
        ]
        if with_prompt and self.system_prompt:
            sp_file = self._write_sysprompt_file()
            cmd += ["--system-prompt-file", sp_file]
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


# ============ 黑白名单 ============
def _in_list(event: dict, cfg: dict, key: str) -> bool:
    """判断事件是否命中指定名单（blacklist / whitelist）。

    名单配置：{"enabled": bool, "users": [...], "groups": [...]}
    - users：按发送者 user_id 命中
    - groups：群消息时按 group_id 命中（整群）
    id 统一转 str 比较，兼容 int 与 str。
    """
    lst = cfg.get(key) or {}
    if not lst.get("enabled", False):
        return False
    users = {str(x) for x in (lst.get("users") or [])}
    groups = {str(x) for x in (lst.get("groups") or [])}
    if not users and not groups:
        return False
    uid = str(event.get("user_id", ""))
    if uid and uid in users:
        return True
    if event.get("message_type", "private") == "group":
        gid = str(event.get("group_id", ""))
        if gid and gid in groups:
            return True
    return False


def is_blacklisted(event: dict, cfg: dict) -> bool:
    """按黑名单拦截：命中则返回 True（bot 不回复）。"""
    return _in_list(event, cfg, "blacklist")


def blocked_reason(event: dict, cfg: dict) -> str:
    """综合判断拦截原因；未拦截返回空字符串。

    优先级：黑名单 > 白名单。
    - 命中黑名单 → "黑名单"
    - 白名单开启但未命中 → "白名单外"
    """
    if _in_list(event, cfg, "blacklist"):
        return "黑名单"
    wl = cfg.get("whitelist") or {}
    if wl.get("enabled", False) and not _in_list(event, cfg, "whitelist"):
        return "白名单外"
    return ""


# ============ Bot 服务端 ============
class BotServer:
    def __init__(self, wb: WorkBuddyClient, cfg):
        self.wb = wb
        self.cfg = cfg
        self.connections = set()
        self.semaphore = asyncio.Semaphore(cfg["max_concurrent"])

    @staticmethod
    def _conv_key(mtype, uid, gid):
        """会话 key（按人记忆）：私聊=uid；群聊=gid:uid。"""
        if mtype == "group":
            return f"g{gid}:u{uid}"
        return f"u{uid}"

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
            # —— 黑白名单拦截（在任何 LLM 调用之前，避免浪费额度）——
            reason = blocked_reason(event, self.cfg)
            if reason:
                bl_who = event.get("sender", {}).get("nickname", "") or event.get("user_id", "")
                log.info(f"[msg] 已拦截({reason}): user_id={event.get('user_id')} "
                          f"group_id={event.get('group_id', '')} {bl_who}")
                return

            text = extract_text(event.get("message", []))
            if not text:
                return
            mtype = event.get("message_type", "private")
            uid = event.get("user_id", "")
            gid = event.get("group_id", "") if mtype == "group" else ""
            who = event.get("sender", {}).get("nickname", "") or uid
            log.info(f"[msg] 收到({mtype}) user_id={uid} group_id={gid} {who}: {text[:60]}")

            # 计算会话 key（按人记忆）：私聊=uid；群聊=gid:uid
            conv_key = self._conv_key(mtype, uid, gid)
            try:
                reply = await asyncio.to_thread(self.wb.ask, text, conv_key)
                ok = True
            except Exception as e:
                log.error(f"[msg] WorkBuddy 调用失败: {e}")
                reply = "（AI 暂时开小差了，请稍后再试～）"
                ok = False

            if not reply:
                reply = "（没有得到回复）"
            await self.send_reply(ws, event, reply)

            # 记录对话历史（失败轮次只记用户侧，不记错误占位回复）
            self.wb.record_turn(conv_key, text, reply if ok else None)

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
    persona_name = cfg.get("persona") or "(未设 persona / 用显式 system_prompt)"
    mode = (cfg.get("llm_mode") or "serve").strip().lower()
    inject_desc = "经 --system-prompt-file 注入（-p agent 模式）" if mode == "agent" \
        else "经 --system-prompt 注入（--serve 模式）"
    log.info(f"[config] 模型={cfg.get('model') or 'serve默认(CODEBUDDY_MODEL未设)'} "
              f"(经 CODEBUDDY_MODEL 环境变量强制)；llm_mode={mode}；人格 persona={persona_name} {inject_desc}")
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
