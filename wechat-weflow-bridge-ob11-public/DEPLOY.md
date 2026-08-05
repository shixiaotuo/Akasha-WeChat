# Akasha-WeChat + WorkBuddy 微信 Bot 部署指南

把 WorkBuddy 接入**个人微信**，让它在群里（或私聊）回答别人 @ 它的问题。
本项目（Akasha-WeChat 的 Bridge）负责微信收发，我们新增的 `workbuddy_backend.py`
扮演「AI 后端」角色，把微信消息转交给 WorkBuddy 处理。

---

## 一、整体架构

```
微信(小号)  ←→  WeFlow(桌面微信增强)  ──SSE推送──▶  Akasha Bridge(Python)
                                                            │
                                            WebSocket 客户端 │ (反向 WS, ws://127.0.0.1:11229/ws)
                                                            ▼
                                              workbuddy_backend.py  ←─ 本项目新增
                                               (OneBot v11 服务端，替代 AstrBot)
                                                            │
                                                   调用 WorkBuddy CLI
                                                            ▼
                                                 WorkBuddy CLI (codebuddy -p)
                                                            │
                                                   回复文字（send_msg）
                                                            ▼
                                              Akasha Bridge ──▶ WeFlow ──▶ 微信
```

要点：**微信 ↔ OneBot 的全部脏活（接收、群 @ 检测、缓冲、图片描述、发送）都由
Akasha Bridge 完成**；我们只需提供一个「OneBot v11 服务端」接住消息、调用
WorkBuddy、把回复发回去。这正好是 Akasha-WeChat 原本给 AstrBot 预留的接入点。

---

## 二、前置条件

| 项目 | 说明 |
|------|------|
| Windows 10/11 | WeFlow 仅支持 Windows |
| 桌面微信 + WeFlow | 从 https://weflow.top 安装，登录微信并**开启 API 服务（端口 5031）** |
| Python 3.10+ | 运行 Bridge 与本项目后端 |
| WorkBuddy 桌面端 | **已登录并运行中**（CLI 复用其登录态，这是调用能成功的前提） |
| node | 运行 `codebuddy` CLI（WorkBuddy 自带或系统安装均可，需在 PATH） |

> ⚠️ **关键前提**：`workbuddy_backend.py` 通过 `codebuddy -p` 调用 WorkBuddy。
> CLI 需要 WorkBuddy 桌面端已登录的会话。请务必保持 WorkBuddy 桌面端打开并登录。

---

## 三、文件说明（本目录新增/修改）

| 文件 | 作用 |
|------|------|
| `workbuddy_backend.py` | **核心**：OneBot v11 反向 WS 服务端，接住 Bridge 消息 → 调 WorkBuddy → 回发 |
| `check_cli.py` | 部署前置检查：一键验证本机 CLI 能否返回回复 |
| `config.example.json` | Bridge 配置示例（原项目自带） |

---

## 四、部署步骤

### 1. 准备 Bridge 目录
仓库已克隆到 `D:/ms/Akasha-WeChat/wechat-weflow-bridge-ob11-public/`。
在该目录下打开终端，安装依赖：

```bash
pip install -r requirements.txt
```
（`websockets` 已包含，无需额外安装。）

### 2. 配置 Bridge（`config.json`）
复制示例并填写：

```bash
copy config.example.json config.json
```

需要修改的关键项：

```jsonc
{
  "weflow_base_url": "http://127.0.0.1:5031",   // WeFlow API 地址
  "access_token": "你的WeFlow_Access_Token",      // WeFlow 设置里获取
  "bot_nicknames": ["科塔娜"],                    // 机器人微信昵称（@ 检测用，必须和微信里一致）
  "bot_wxid": "",                                 // 机器人自己的 wxid（可选，防自回复）
  "send_method": "uia",                           // 微信 4.0+ 用 UIA 自动化发送
  "group_reply_mode": "mention",                 // mention=仅@回复 / all=全部回复 / batch=批处理
  "astrbot_ob_url": "ws://127.0.0.1:11229/ws",   // 指向本 WorkBuddy 后端（重点！）
  "buffer_seconds": 5                             // 多条消息合并等待秒数
}
```

> `astrbot_ob_url` 原本指向 AstrBot，现在改成我们的 `workbuddy_backend.py` 监听地址。
> 其余项保持默认即可；图片描述（`image_caption_*`）如不需要可忽略。

### 3. 运行前置检查
确认 WorkBuddy CLI 在本机可用（最重要的一步）：

```bash
python check_cli.py
```
- ✅ 输出一段测试回复 → 可以继续。
- ❌ 超时/报错 → 见末尾「排错」。常见原因：WorkBuddy 桌面端未登录/未运行、node 不在 PATH。

### 4. 启动顺序（很重要）
按以下顺序启动，缺一不可：

1. **WorkBuddy 桌面端**（打开并登录）
2. **WeFlow**（登录微信，开启 API 服务，端口 5031）
3. **WorkBuddy 后端**（本项目）：
   ```bash
   python workbuddy_backend.py
   ```
   看到 `WorkBuddy OneBot 后端启动  监听：ws://127.0.0.1:11229/ws` 即成功。
4. **Akasha Bridge**：
   ```bash
   python main.py
   ```
   Web 面板 http://127.0.0.1:8766 可查看连接状态、群聊模式、实时日志。

### 5. 测试
在微信里（群或私聊）**@机器人 你的问题**，例如「@科塔娜 帮我写一段 SQL」。
稍等十几秒，应收到 WorkBuddy 的回复。

---

## 五、配置要点

- **`bot_nicknames`**：必须和微信里机器人这个号的昵称完全一致，否则 @ 检测失效、群消息不回复。
- **`group_reply_mode`**：
  - `mention`（推荐）：只有被 @ 才回复，避免刷屏。
  - `all`：群里所有消息都回复（慎用，易刷屏/风控）。
  - `batch`：批处理模式。
- **`send_method`**：微信 4.0+ 用 `uia`（UI 自动化）。需要微信窗口处于可操作状态（不要最小化到托盘被拦截）。也可尝试 `weflow_api`（通过 WeFlow API 发送，更稳定）。
- **回复长度**：微信单条过长会被截断，后端已自动按 ~1500 字分段发送。

---

## 六、环境变量（可选，均有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `WORKBUDDY_NODE` | `node` | 运行 CLI 的 node 路径 |
| `WORKBUDDY_CLI` | `D:/WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy` | CLI 脚本路径 |
| `WORKBUDDY_MAX_TURNS` | `8` | 最大 Agent 轮数 |
| `WORKBUDDY_TIMEOUT` | `180` | CLI 调用超时（秒） |
| `WORKBUDDY_WX_MAX_LEN` | `1500` | 单条微信消息最大字数 |
| `WORKBUDDY_SYSTEM_PROMPT` | 内置角色设定 | 给 WorkBuddy 的系统提示 |

---

## 七、封号风险与合规提醒

- WeFlow 属于**非官方微信接入**（基于客户端 hook），微信官方不允许，**存在封号风险**。
- 建议使用**小号**运行 Bot；控制回复频率；避免营销、涉政、违规内容。
- 本项目**不存储**任何聊天记录（Bridge 仅内存缓冲用于合并发送）。
- 如果你所在组织有**企业微信**，那是官方接口、零封号，WorkBuddy 也原生支持
  `wecom` 渠道——更稳定，可作为进阶替代方案。

---

## 八、排错

**Q1：`check_cli.py` 超时 / CLI 无输出**
- 确认 WorkBuddy 桌面端已登录并运行（CLI 复用其会话）。
- 确认本机网络能访问 WorkBuddy 云端（如有代理，CLI 会自动走本地代理）。
- 确认 `node` 在 PATH；或用 `WORKBUDDY_NODE` 指定完整路径。

**Q2：Bridge 面板显示「无 AstrBot 客户端在线」**
- 说明 `workbuddy_backend.py` 没起来或端口不对。确认后端已启动、且
  `config.json` 的 `astrbot_ob_url` 是 `ws://127.0.0.1:11229/ws`。

**Q3：微信收到消息但没有回复**
- 群聊检查 `bot_nicknames` 是否与微信昵称一致、`group_reply_mode` 是否为 `mention`
  （mention 模式必须 @ 才回复）。
- 看 Bridge 面板日志和后端日志，是否有 CLI 调用报错。

**Q4：回复发不出去（停在 Bridge 侧）**
- 检查 WeFlow API 是否开启、`access_token` 是否正确。
- `send_method` 试切换 `uia` ↔ `weflow_api`。

**Q5：回复很长被截断**
- 后端已自动分段；如仍异常，调小 `WORKBUDDY_WX_MAX_LEN`。

---

## 九、与旧方案的关系

`D:/ms/wechat-bot/`（早期基于 wcferry 的方案）**已被本方案取代**，不再维护。
本方案改用 Akasha-WeChat 的 Bridge + WeFlow，微信接入更稳定（支持最新版微信），
且把 AI 后端从 AstrBot 替换为 WorkBuddy，架构更清晰。
