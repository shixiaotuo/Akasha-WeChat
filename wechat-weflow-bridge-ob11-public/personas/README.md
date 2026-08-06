# personas/ —— 角色（人格）提示词目录

bot 的人格由 `wb_config.json` 的 `persona` 字段选择，解析逻辑见
`workbuddy_backend.py` 的 `resolve_system_prompt()`：

  优先级（从高到低）：
    1. config 显式 `system_prompt`        （最高优先）
    2. 本机 Skill  ~/.workbuddy/skills/<persona>/SKILL.md   ← 主力，自动同步你安装的 Skill
    3. 本目录      personas/<persona>.md                        ← 兜底
    4. 默认提示词

## 结论
- 正常情况 bot 直接读你**本机安装的张雪峰 Skill 全文**，本目录文件不会被使用。
- 本目录的 `zhangxuefeng.md` 只是**兜底**：仅当本机 Skill 被删除/改名时才回退。
  它是独立维护的精简版，不会随本机 Skill 更新自动同步。
- `nihaixia.md` 已删除（bot 当前只用 zhangxuefeng）。

## 如何更新人格
改本机 `~/.workbuddy/skills/zhangxuefeng-perspective/SKILL.md` 即可，
bot 重启后自动生效，无需维护本目录。
