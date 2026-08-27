# bilibili-manager 安装说明（给 Codex 用户）

把 B 站收藏视频与关注的 UP 主整理成本地 Markdown 知识库：
三档分类归档（非工科不写总结、工科教学写详细知识卡片）、UP 主内容标签、
自动生成的知识思维导图与知识点索引、学习复习闭环（到期卡片 + Anki 导出）、
断点续传、增量更新，并可在确认后把分类结果同步回 B 站收藏夹。

## 安装步骤（Codex CLI 或桌面版）

1. 把本压缩包解压到任意位置，例如 `D:\bili-share\`。
2. 注册本市场（指向解压后包含 `marketplace.json` 的目录）：
   ```
   codex plugin marketplace add "D:\bili-share\bilibili-manager-share"
   ```
3. 安装插件：
   ```
   codex plugin add bilibili-manager@bili-local
   ```
4. `codex plugin list` 应显示 `installed, enabled`。

## 首次使用

1. 设置数据目录环境变量（PowerShell，可写入 `$PROFILE` 永久生效）：
   ```
   $env:BILI_DATA_DIR = "D:\你的数据目录"
   ```
   不设置则默认使用当前工作目录下的 `bili-data`；也可每次用 `--data-dir` 指定。
2. 新建一个任务，说「整理我的B站收藏」或「更新B站知识库」。
3. 提供 SESSDATA：浏览器登录 bilibili.com → F12 → Application → Cookies →
   `https://www.bilibili.com` → 复制 `SESSDATA` 的值交给 Agent。
4. 如需「把分类同步回B站」，还需提供同一位置的 `bili_jct` 值。

## 说明

- 本包不含任何登录凭证，每位用户使用自己的 SESSDATA。
- 脚本为纯 Python 标准库实现（Python 3.10+），无需安装第三方依赖；
  自带离线冒烟测试（`python -B -m unittest discover -s tests -v`）。
- 写操作仅支持「创建收藏夹」与「移动收藏视频」，默认干跑预览，确认后才执行；
  不存在删除、取关等破坏性命令。
- 登录态存于数据目录 `config.json`（敏感），重点领域等非敏感配置存于 `settings.json`，
  两者已分离，备份/分享设置不会泄密。
