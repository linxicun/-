# 在其他 AI 助手里使用（Claude Code / Cursor / 任意编码 Agent）

本插件的核心是两个文件，任何「能读 Markdown 指令 + 能执行命令」的 Agent 都能用：

- `plugins\bilibili-manager\skills\bilibili-manager\SKILL.md` — 完整操作手册（怎么分类、怎么写卡片、怎么打标签、复习闭环、索引重建）
- `plugins\bilibili-manager\scripts\bili.py` — 纯 Python 标准库实现的 B 站 API 命令行（Python 3.10+，零依赖）
- `plugins\bilibili-manager\tests\test_bili.py` — 离线冒烟测试（可选）

## 用法

1. 把这三个文件（保持相对路径）放进对方 Agent 的工作目录。
2. 对 Agent 说：「阅读 SKILL.md 并按它整理我的B站收藏，数据目录用 `<你的目录>\bili-data`」。
3. 首次使用前确认两处：
   - 数据目录优先取环境变量 `BILI_DATA_DIR`；不设置则默认当前目录的 `bili-data`，
     也可每次在命令里加 `--data-dir <目录>`。**不需要改 SKILL.md 里的路径。**
   - 确认机器上有 `python` / `python3` / `py` 命令。
4. 让 Agent 运行 `bili.py status`，按提示提供 SESSDATA
   （浏览器登录 bilibili.com → F12 → Application → Cookies 中复制）。

## 新增命令（v0.2.0）

- `review` / `review --set --bvid B --status 已学 --next +7d` — 复习队列（支持相对日期）
- `export-anki` — 把卡片核心知识点导出为 Anki CSV
- `build-index` — 自动重建知识地图与知识点索引（不再靠 Agent 手工维护）
- `render-mindmap [--engineering] [--html]` — 画成图片/交互式 HTML 思维导图（需 `pip install matplotlib`；`--engineering` 只画工科教学类）
- `analyze` — 分析工科教学类视频的话题/分类/UP主分布
- `search --q 关键词 [--subtitles]`、`stats` — 检索（可含字幕缓存）与统计
- `plan-classify` — 只读生成"同步回 B 站"的移动计划，取代手工拼 JSON
- `sync-favorites` 现在会标记已取消收藏的视频（removed），不会再反复出现；
  `--folder` 部分同步时不会误清理其他收藏夹

## 安全说明

- `bili.py` 只实现只读 API 与两种写操作（创建收藏夹 / 移动视频），写操作默认干跑预览，
  加 `--apply` 才执行，并在 `write_log.json` 留痕。
- 没有删除收藏夹、删除收藏、取关等破坏性命令。
- SESSDATA 是 B 站登录凭证：只存放在本地 `config.json`，绝不写入知识库、绝不上传；
  `settings.json`（重点领域等）不含敏感信息。
