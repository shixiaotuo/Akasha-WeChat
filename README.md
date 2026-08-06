# Akasha-WeChat

让微信小号接入 LLM 大模型，通过 **OneBot v11 协议** 与模型通信。

```
微信 ←→ WeFlow ──SSE 实时推送──→ Akasha Bridge (OneBot v11 反向 WS 客户端)
                                            │
                                            ├─→ AstrBot (aiocqhttp, 反向 WS :11229)        【可选 LLM 大脑 A】
                                            │
                                            └─→ WorkBuddy 后端 (workbuddy_backend.py, 反向 WS :11229)  【可选 LLM 大脑 B】
                                                      └─→ codebuddy --serve (:8080)
                                            │
                                            └─→ 回发：uia_sender.py 用 Windows UI Automation 自动操作微信 4.0 发回
```

> Bridge 作为 OneBot v11 反向 WebSocket 客户端，连接到运行在 `127.0.0.1:11229` 的 OneBot 服务端。
> 该服务端 **二选一**：要么是 AstrBot(aiocqhttp)，要么是本仓库附带的 **WorkBuddy 后端**（见下文）。
> 回复走 `send_group_msg / send_private_msg` 经 `uia_sender.py` 通过 Windows UIA 自动化发回微信。

---

## 特性

- **消息接收** — WeFlow SSE 实时推送，无轮询无风控
- **AI 回复** — 通过 LLM 后端调用任意大模型（DeepSeek、Kimi、Claude、WorkBuddy hy3 等）
- **图片识别** — 支持 ollama llava / Kimi 等模型描述图片内容
- **三种群聊模式** — 仅 @ 回复 / 全部回复 / 批处理，Web 页面一键切换
- **Web 控制面板** — 粉白主题，启停控制、状态监控、日志查看
- **在线配置编辑** — 直接在网页上修改 `config.json`，无需碰文件（原子保存，已加固）
- **消息缓冲** — 多条消息合并后推送，减少 AI 调用次数
- **自回复防护** — 多层去重，防止 AI 和自己的消息循环
- **群名精准解析** — 自动拉取 WeFlow 联系人映射，群消息按真实群名定位，不再发错窗口
- **前后台 / 最小化切换** — 微信不在前台甚至被最小化时，也能自动置前并正确切窗发送
- **图片回复开关** — 默认关闭对图片消息的回复，避免无意义刷屏（可在配置中开启）
- **WorkBuddy 后端** — 可选以 WorkBuddy（`codebuddy --serve`）替代 AstrBot 作为 LLM 大脑，自带 OneBot WS 服务端，可自动拉起 serve

---

## 前置条件

| 依赖 | 说明 |
|------|------|
| Windows 系统 | 需要桌面微信（4.0+，UIA 自动化发送） |
| [WeFlow](https://weflow.top) | 已安装并登录微信，开启 API 服务（端口 5031）。**注意：WeFlow HTTP API 经核实完全只读**（仅 `/api/v1/messages`、`/sessions`、`/contacts`、SSE 推送等 GET 接口，全局不处理 POST），不存在发送消息的接口，因此发送方式只能用 UIA |
| Python 3.10+ | 运行桥接脚本 |
| LLM 后端（二选一） | ① [AstrBot](https://github.com/AstrBotDevs/AstrBot)（启用 aiocqhttp 适配器）<br>② 本仓库自带 **WorkBuddy 后端**（`workbuddy_backend.py`，需本机安装 WorkBuddy，`codebuddy --serve` 可用） |

---

## 快速开始

> 💡 **一键启停（推荐）**：嫌开两个终端麻烦，直接双击仓库里的 `start.bat` 即可一键启动「后端 + 桥接」。脚本内置防重复启动（端口已占用则自动跳过），关闭时双击 `stop.bat`，会按 PID / 窗口标题清理全部进程（含后端自动拉起的 `codebuddy --serve`）。

### 1. 安装依赖

```bash
cd wechat-weflow-bridge-ob11-public
pip install -r requirements.txt
```

### 2. 启动桥接

```bash
python main.py
```

- 首次启动会自动从 `config.example.json` 创建 `config.json`，然后打开 Web 面板填配置。
- Web 控制面板：**http://127.0.0.1:8766** → 点「基础设置」→ 填写配置 → 保存配置 → 重启生效。
- `send_method` **必须填 `"uia"`**（WeFlow 只读，weflow_api 模式所有发送路径 404，sender 已做自动回退保护）。

### 3. 配置 LLM 后端（二选一）

#### 方案 A：AstrBot（默认）

在 AstrBot 中添加 aiocqhttp 适配器（WebUI 或 `cmd_config.json`）：

```json
{
    "id": "wechat_bridge",
    "type": "aiocqhttp",
    "enable": true,
    "ws_reverse_host": "0.0.0.0",
    "ws_reverse_port": 11229,
    "ws_reverse_token": ""
}
```

> 若 AstrBot 运行在 Docker 中，确保端口映射到宿主机：`-p 11229:11229`。配置后重启 AstrBot。

#### 方案 B：WorkBuddy 后端（免装 AstrBot）

直接用本仓库自带的 `workbuddy_backend.py` 作为 OneBot 服务端（它同时会按需拉起 `codebuddy --serve`）：

```bash
# 终端 1：启动 WorkBuddy 后端（监听 11229，并按需拉起 codebuddy --serve :8080）
python workbuddy_backend.py

# 终端 2：启动桥接
python main.py
```

相关配置见 `wb_config.json`：

| 字段 | 说明 |
|------|------|
| `serve_host` / `serve_port` | `codebuddy --serve` 的 HTTP 地址，默认 `127.0.0.1:8080` |
| `auto_launch_serve` | 为 `true` 时，若 serve 未启动会自动拉起（需 `cli_path` 指向本机 codebuddy） |
| `cli_path` | 本机 `codebuddy` CLI 路径（WorkBuddy 安装目录下的 `cli/bin/codebuddy`） |
| `system_prompt` | 机器人人格系统提示词（默认「赛博老农」+ 倪海厦经方派视角） |
| `model` | 模型 id，默认 `hy3-preview-agent`（`hy3` 不是合法 id，会被忽略） |
| `ob_ws_host` / `ob_ws_port` | OneBot 反向 WS 服务端监听地址，默认 `127.0.0.1:11229`（需与 bridge 的 `astrbot_ob_url` 一致） |
| `reply_chunk_size` | 单条微信消息最大字符数，超长自动分段（默认 1000） |
| `max_concurrent` | 并发处理消息数上限 |

### 4. 可选：图片描述

- **Ollama 本地模式**（默认）：需安装 [ollama](https://ollama.ai) 并拉取视觉模型：
  ```bash
  ollama pull llava:7b
  ```
- **OpenAI 兼容模式**：在 Web 面板中将 `图片描述 → 描述服务` 改为 `openai`，填入 API Key 和模型名（如 `kimi-k2.6`）。

---

## 配置项说明

所有配置可在 Web 面板「基础设置」中在线编辑（`config.json`）。

| 字段 | 说明 |
|------|------|
| `weflow_base_url` | WeFlow API 地址，默认 `http://127.0.0.1:5031` |
| `access_token` | WeFlow Access Token |
| `bot_nicknames` | 机器人微信昵称列表（群聊 @ 检测用），**必须与机器人微信小号昵称完全一致**（默认 `["赛博老农"]`） |
| `bot_wxid` | 机器人自己的 wxid（可选，防自回复） |
| `send_method` | **必须用 `"uia"`**（UIA 自动化）；`weflow_api` 因 WeFlow 只读已不可用，会自动回退 |
| `buffer_seconds` | 消息缓冲秒数，多条消息合并后推送（默认 5） |
| `group_reply_mode` | `"mention"`（仅 @ 回复，推荐）/ `"all"`（全部回复，易刷屏封号）/ `"batch"`（批处理） |
| `astrbot_ob_url` | 反向 WebSocket 地址，应填 `ws://127.0.0.1:11229/ws` |
| `image_caption_provider` | 图片描述服务：`"ollama"` 或 `"openai"` |
| `image_caption_model` | 视觉模型名，如 `llava:7b` / `kimi-k2.6` |
| `REPLY_TO_IMAGES` | **（config.py 中）是否回复图片消息，默认 `False`**；设 `true` 才会对 `[图片]` 消息触发 AI 描述与回复 |

---

## 角色（人格）配置

bot 的「角色人格」是**可配置、易修改**的，不用再改代码。

- 每个角色一个纯文本文件，放在 `personas/` 目录（`.md`，好编辑、不用转义 JSON 引号）：
  - `personas/zhangxuefeng.md` —— 张雪峰视角（升学 / 志愿 / 就业 / 阶层类问题）
  - `personas/nihaixia.md` —— 倪海厦经方派（中医 / 养生 / 方剂 / 辨证类问题）
- 在 `wb_config.json` 用一行选择角色：
  ```json
  "persona": "zhangxuefeng"
  ```
  改成 `"nihaixia"` 即切到倪海厦，以此类推。

**日常怎么改（都只动一个文件）：**
- 切换角色 → 改 `wb_config.json` 的 `"persona"` 一行。
- 调语气措辞 → 直接编辑对应的 `personas/*.md`。
- 新增角色 → 在 `personas/` 复制一个 `.md` 改名改内容，再把 `persona` 指过去。

> 优先级：`wb_config.json` 里显式写的 `system_prompt` ＞ `personas/{persona}.md` ＞ 代码内置兜底。一般用户只动 `persona` 和对应 `.md` 即可。
> **改完需重启后端**：双击 `stop.bat` 再 `start.bat`，新人格才会生效。

---

## 工作原理

1. **接收消息** — 连接 WeFlow SSE 推送，实时接收微信消息（群消息按联系人映射解析真实群名）
2. **缓冲合并** — 多条消息缓冲 N 秒后合并推送
3. **图片处理** — 图片消息自动下载 → 视觉模型描述 → 注入文本（受 `REPLY_TO_IMAGES` 开关控制）
4. **OneBot 推送** — 转 OneBot v11 事件，通过 WebSocket 推给 LLM 后端（AstrBot 或 WorkBuddy 后端）
5. **AI 处理** — 后端调用 LLM（AstrBot 插件流水线 / WorkBuddy codebuddy --serve）生成回复
6. **回复发送** — 后端调用 `send_msg` API，bridge 经 `uia_sender.py` 自动把微信窗口置前（即使最小化也行）、切到对应会话并发送

---

## 文件结构

```
Akasha-WeChat/
├── README.md                       # 本文件（项目总览）
└── wechat-weflow-bridge-ob11-public/   # 桥接组件目录
    ├── main.py              # 入口（启动桥接 + Web 服务）
    ├── state.py             # 共享全局状态（含 contact_name_map 群名映射）
    ├── config.py            # 配置加载（含 REPLY_TO_IMAGES 开关）
    ├── senders.py           # 消息发送器（UIA / WeFlow API，含 weflow_api 自动回退）
    ├── ob_client.py         # OneBot WebSocket 客户端
    ├── ob_protocol.py       # OneBot 协议处理（API 接收 + 事件推送）
    ├── bridge_core.py       # 桥接核心（缓冲 + SSE + 群名解析 + 图片门控）
    ├── web_panel.py         # Web 控制面板（粉白主题 + 在线配置编辑 + 原子保存）
    ├── uia_sender.py        # Windows UI Automation 发送器（前台/最小化切换逻辑）
    ├── workbuddy_backend.py # 【新增】WorkBuddy 后端（OneBot WS 服务端 + 拉起 codebuddy --serve）
    ├── wb_config.json       # 【新增】WorkBuddy 后端配置（persona / 模型 / 端口）
    ├── personas/            # 【新增】角色人格文件（每个角色一个 .md，纯文本好编辑）
    ├── test_backend.py      # 后端自测脚本
    ├── check_cli.py         # CLI 路径检测工具
    ├── config.json          # 配置文件（已 gitignore，需自行创建）
    ├── config.example.json  # 配置示例
    ├── requirements.txt     # Python 依赖
    ├── start.bat            # Windows 快捷启动
    ├── LICENSE              # MIT 许可证
    └── README.md            # 组件级说明
```

---

## Web 控制面板

访问 **http://127.0.0.1:8766**

- **控制面板** — 查看桥接 / LLM 后端 / WeFlow 连接状态、启停控制、群聊模式切换、实时日志
- **基础设置** — 在线编辑所有配置项，保存即**原子写入** `config.json`（临时文件 + `os.replace`，避免中途崩溃导致半写 / 权限错误）

---

## 近期改动记录

> 以下为相对于上游初始版本（`6e4f68b`）的本地化改动，已全部并入主线。

1. **集成 WorkBuddy 后端（替代 / 补充 AstrBot）**
   - 新增 `workbuddy_backend.py` + `wb_config.json`：以 WorkBuddy（`codebuddy --serve`）作为 LLM 大脑，自带 OneBot v11 反向 WS 服务端（端口 11229），可按 `auto_launch_serve` 自动拉起 serve。
   - 机器人人格统一为「赛博老农」，并以倪海厦（经方派）视角与心法作答（写入 system_prompt）。

2. **群聊真实群名解析**
   - WeFlow 群消息只下发 chatroom ID，新增 `contact_name_map` + `_refresh_contact_map`（拉 `/api/v1/contacts`，120s 节流）+ `_resolve_contact_name`；`add_to_buffer` 群分支按真实群名定位，避免发错窗口。

3. **前后台 / 最小化切换修复（`uia_sender.py`）**
   - 新增 `_wechat_foreground` 上下文管理器（进入置前、退出还焦点）与 `_force_foreground`（破除 `ForegroundLockTimeout` + 正确线程挂接 `AttachThreadInput` + `SW_RESTORE`）。
   - 重构 `_switch_contact` / `send_text` / `send_image`，微信不在前台甚至被最小化时也能正确切窗并发送。

4. **图片回复开关**
   - 新增配置 `REPLY_TO_IMAGES`（默认 `False`）并在 `bridge_core.py` 加门控：默认不对 `[图片]` 消息触发回复，避免无意义刷屏。

5. **Web 面板保存加固（`web_panel.py`）**
   - 新增 `_atomic_save_config()`（写临时文件 + `os.replace` 原子替换），修复偶发 `[Errno 13] Permission denied` 保存失败；两处保存入口（`/mode`、`/api/config`）均已改用。

6. **机器人身份更名**
   - `config.json` 的 `bot_nicknames` 与 `wb_config.json` / `workbuddy_backend.py` 的 system_prompt 统一为「赛博老农」（原为「赛博老中医」）。

7. **发送方式收敛**
   - WeFlow HTTP API 经核实完全只读（无发送接口），`weflow_api` 模式所有发送路径 404；`senders.py` 已对 `weflow_api` 做自动回退保护，`config.json` 注明 `send_method` 必须为 `uia`。

8. **角色（人格）配置化**
   - 新增 `personas/` 目录：每个角色一个纯文本 `.md`（`zhangxuefeng.md` 张雪峰视角、`nihaixia.md` 倪海厦经方派），好编辑、不用转义 JSON 引号。
   - `wb_config.json` 用 `"persona"` 字段选择角色（切换只改一行）；`workbuddy_backend.py` 移除硬编码 prompt、新增 `resolve_system_prompt()`（显式 `system_prompt` ＞ `personas/{persona}.md` ＞ 内置兜底）。
   - 日常修改只需动一个文件：切换角色改 config 一行、调措辞改对应 `.md`；改完重启后端生效。

> ⚠️ 已知限制：电脑锁屏（Win+L）后 UIA 自动化必然失效（Windows 会话锁定限制），属架构固有限制，需非 UIA 方案方可解决。

---

## 原作者与致谢

本项目基于原作者 **alingalingling** 的 [Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat) 进行二次开发与扩展。感谢原作者开源了这套「微信 + OneBot v11 桥接」方案，为后续的 WorkBuddy 后端集成、群名解析、前后台 / 最小化切换修复、图片回复开关以及 Web 面板原子保存等改动提供了坚实基础。

## 许可证

MIT
