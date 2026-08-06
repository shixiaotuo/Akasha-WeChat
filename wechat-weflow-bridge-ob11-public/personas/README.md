# 角色（人格）文件

bot 的「人格 / 角色」定义放在本目录，每个角色一个 `.md` 纯文本文件，方便直接编辑，不用处理 JSON 引号转义。

## 已有角色
- `zhangxuefeng.md` —— 张雪峰视角（升学 / 志愿 / 就业 / 阶层类问题）
- `nihaixia.md` —— 倪海厦经方派（中医 / 养生 / 方剂 / 辨证类问题）

## 怎么切换角色
打开 `wb_config.json`，改一行即可：
```json
"persona": "zhangxuefeng"
```
改成 `"nihaixia"` 就切到倪海厦，以此类推。**改完要重启后端**（双击 `stop.bat` 再 `start.bat`）。

## 怎么调措辞
直接编辑对应的 `.md` 文件内容即可，也是只改这一个文件。

## 怎么新增一个角色
1. 在本目录复制一个 `.md`（如 `newpersona.md`），改成你要的人设文案。
2. 在 `wb_config.json` 里把 `"persona"` 指向它：`"persona": "newpersona"`。
3. 重启后端生效。

## 优先级说明
后端解析顺序：`wb_config.json` 里的显式 `system_prompt` 字段（若填了）＞ 本目录 `personas/{persona}.md` ＞ 代码内置兜底提示词。
一般用户只动 `persona` 字段和对应 `.md` 就够了。
