# bilibili-manager（B站知识库管家）

Codex 插件：把 B 站收藏视频与关注的 UP 主整理成本地 Markdown 知识库，并带学习复习闭环。

## 功能

1. **收藏整理**：拉取全部收藏夹 → 语义分类 → 为每个视频生成内容总结 → 预留个人备注区
2. **博主建档**：拉取关注列表 → 抓取各 UP 主投稿 → 归纳创作方向 → 生成档案与视频列表
3. **增量更新**：只处理新增收藏/新投稿，用户手写备注永不覆盖；
   已取消收藏的视频自动标记 `removed`，不再反复出现在待办里
4. **学习复习闭环**：知识卡片带 `学习状态`/`下次复习` 字段，`review` 列出到期卡片，
   `export-anki` 把核心知识点导出为 Anki 可导入 CSV
5. **自动索引与检索**：`build-index` 扫描卡片自动生成知识思维导图与知识点索引；
   `render-mindmap` 用 matplotlib 把知识库**画成图片**（PNG+SVG）或生成**交互式 HTML**（可折叠/搜索/跳B站）；
   `analyze` 分析工科教学类内容分布；`search` 全文检索；`stats` 统计学习进度
6. **同步回 B 站**（可选写操作）：`plan-classify` 只读生成移动计划，确认后 `apply-plan` 执行

## 组件

- `scripts/bili.py` — B 站数据同步 + 知识库工具（纯标准库，无第三方依赖）
- `skills/bilibili-manager/SKILL.md` — Codex 工作流指令
- `tests/test_bili.py` — 离线冒烟测试（mock 网络，不访问 B 站）
- 数据目录：默认 `$env:BILI_DATA_DIR` 或 `./bili-data`（可用 `--data-dir` 指定）

## 数据目录

- `config.json` — 登录态（SESSDATA/bili_jct），**敏感，请加入 .gitignore，勿上传**
- `settings.json` — 非敏感配置（重点领域等），可安全备份
- `cache\` — API 缓存、进度、写操作日志
- `library\` — Markdown 知识库（分类 / 博主 / 思维导图 / 总索引）

## 安全说明

- 对 B 站只读，写操作仅「创建收藏夹 + 移动视频」，默认干跑预览、确认后执行、写日志留痕
- 内置限速（含随机抖动）与风控退避（-412/-509 自动重试）
- WBI 签名密钥本地缓存，减少接口请求次数

## 使用

安装插件后在 Codex 新任务中说：
- “整理我的B站收藏视频”
- “总结我最近收藏的视频”
- “更新B站知识库”
- “看看我有哪些到期要复习的知识卡片”

首次使用需提供 SESSDATA（浏览器登录 bilibili.com → F12 → Application → Cookies）。

## 开发

在插件根目录运行离线测试：

```
python -B -m unittest discover -s tests -v
```
