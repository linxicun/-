---
name: bilibili-manager
description: 把B站收藏视频和关注的UP主整理成本地 Markdown 知识库：收藏分类、工科教学视频详细知识卡片、UP主内容标签、知识思维导图、增量更新、学习复习闭环（到期卡片/Anki导出），并可把分类结果同步回B站收藏夹。触发词：B站、bilibili、收藏夹、收藏的视频、关注、UP主、博主、整理视频、视频总结、知识库、思维导图、复习、Anki。
---

# B站知识库管家

把用户的 B 站收藏视频与关注博主整理成本地 Markdown 知识库：工科教学视频建详细知识卡片，其他视频极简归档；为 UP 主打内容标签；按知识相关性自动生成可检索的思维导图与知识点索引；用「学习状态 + 复习队列」把收藏变成真正学完的资料，可导出 Anki。可选把分类结果同步回 B 站。

## 环境与路径

- Python：优先 `python`（Windows 也可试 `py`），要求 Python 3.10+；脚本为纯标准库，无需安装第三方依赖。
- 脚本：`<插件根目录>\scripts\bili.py`（本 SKILL.md 位于 `<插件根目录>\skills\bilibili-manager\`）
- 数据目录：**优先环境变量 `BILI_DATA_DIR`**，未设置时默认当前工作目录下的 `bili-data\`；也可每次用 `--data-dir` 显式指定。首次使用前设置一次（PowerShell：`$env:BILI_DATA_DIR = "你的目录"`，建议写入 `$PROFILE`），或在每条命令后加 `--data-dir "你的目录"`。**不用改本文件里的路径。** 运行 `status` 会打印实际数据目录。
- 目录结构：
  - `config.json` — 登录态（SESSDATA/bili_jct），**敏感，勿分享/备份到云端**
  - `settings.json` — 非敏感配置（重点领域 focus 等），可安全备份分享
  - `cache\` — API 缓存（含 WBI 密钥、进度 `progress.json`、BV→aid 映射、索引扫描缓存）、写操作日志 `write_log.json`
  - `library\` — Markdown 知识库（唯一允许写入的产出目录）
    - `分类\<类别>\` — 视频归档与知识卡片
    - `博主\<UP主>\` — UP 主档案
    - `思维导图\` — 知识地图与知识点索引（**由 `build-index` 自动生成**）
    - `总索引.md`

命令模板（PowerShell）：

```
& "<python路径>" "<插件根目录>\scripts\bili.py" --data-dir "$env:BILI_DATA_DIR" <子命令> [参数]
```

注意：`--data-dir` 必须放在子命令之前；已设置 `BILI_DATA_DIR` 时可省略。网络被沙箱拦截（code=-9998/请求异常）时，用提权方式重跑。

## 子命令速查

| 命令 | 作用 |
|---|---|
| `status` | 登录态（含 bili_jct 是否配置）、缓存、知识库统计、实际数据目录 |
| `setup --sessdata <值> [--bili-jct <值>]` | 保存登录 Cookie 并验证 |
| `sync-favorites [--folder <media_id>] [--no-prune]` | 同步收藏夹全部视频；**默认标记已取消收藏的条目为 removed**（有收藏夹拉取失败时自动跳过清理以免误删；`--no-prune` 可关闭） |
| `sync-followings [--continue-on-error]` | 同步关注列表；单页失败可 `--continue-on-error` 保留已抓部分 |
| `sync-uploader-videos [--limit N] [--max-videos 50]` | 同步关注 UP 主的投稿列表（单 UP 主失败不影响其余） |
| `subtitles --bvid BVxxx` | 抓取视频字幕（详细总结用） |
| `report` | 同步进度 + 断点位置 + 重点领域 + **复习到期数** |
| `pending --kind videos\|uploaders [--limit N --offset M]` | 分批获取未完成项（自动跳过已完成与 removed，含 `resume_after`） |
| `mark-done --kind video --bvid B [--title T]` | 视频建档完成后记录进度 |
| `mark-done --kind uploader --mid M [--uname U]` | UP主建档完成后记录进度 |
| `focus [--reset]` | 查看/重置重点知识领域（存于 settings.json） |
| `review [--all]` | 列出到期（未学或下次复习已到）的知识卡片；`--all` 列出全部 |
| `review --set --bvid B [--status 未学\|已学\|复习中] [--next YYYY-MM-DD\|+7d/+2w/+1m]` | 更新某张卡片的学习状态/下次复习日期（`--next` 支持相对日期） |
| `export-anki [--out 路径] [--domain 领域]` | 把卡片「核心知识点」导出为 Anki 可导入 CSV（UTF-8 BOM，默认 `library\anki_export.csv`；`--domain` 按卡片「领域」字段过滤） |
| `build-index` | 扫描知识卡片，自动重建 `思维导图\知识地图.md` 与 `知识点索引.md`（确定性、只读卡片） |
| `render-mindmap [--max-videos N] [--exclude 分类] [--dpi N] [--engineering] [--html]` | 用 matplotlib 把知识库**画成图片**思维导图（PNG+SVG，默认 `思维导图\知识地图.png`；兼容新旧卡片格式）；`--engineering` 只画工科教学类（按话题分类器过滤，结构为 分类→话题→视频）；`--html` 额外生成**交互式 HTML**（单文件：默认折叠、拖拽平移不选中文字、点节点折叠/展开、视频卡片带链接+简介、点视频跳B站） |
| `analyze [--top N]` | 分析知识库：找出工科教学类视频，按话题/分类/UP主统计分布（画图前先跑这个看数据） |
| `search --q 关键词 [--subtitles]` | 在知识库全文检索（返回文件+命中行）；加 `--subtitles` 同时检索字幕缓存 |
| `stats` | 知识库统计（领域分布/学习状态/收藏按月） |
| `create-folder --title T [--intro I] [--privacy 0\|1]` | 【写】在B站创建收藏夹（幂等，同名复用；默认私密） |
| `aid --bvid B` | 解析 bvid 的 aid |
| `plan-classify [--out 路径]` | **只读**：按本地分类自动生成同步回 B 站的移动计划（默认 `cache\classify_plan.json`） |
| `apply-plan --plan <文件> [--apply]` | 【写】按计划在B站移动视频；默认仅预览，`--apply` 才执行（aid 批量解析加速，输出 `unresolved_aids` 供核对） |
| `safe-name --text "..."` | 生成 Windows 安全文件名（含全角字符归一化） |

## 断点续传协议（必须遵守）

进度保存在 `cache\progress.json`。任何整理任务：

1. **开始时**先运行 `report`。若 `progress.last_video` / `progress.last_uploader` 非空，
   向用户报告“继续上次进度：从《X》之后开始（已完成 N 条）”。
2. 用 `pending` 取批次——已完成/removed 项自动排除，按收藏时间排序，从断点继续。
3. **每写完一个视频或一个 UP 主的文件，立即运行对应的 `mark-done`**。
4. 任务结束时报告本次完成数量与剩余 `pending_total`。

## 三档处理规则（总结深度分级）

默认重点领域：**电子类**、**嵌入式类**、**机械类**（`focus` 查看/重置，可在 `settings.json` 的 `focus` 字段自定义）。`pending` 每条视频带 `focus_hint`（关键词预判），最终由你按标题/简介/UP主语义判定。

| 档位 | 判定 | 处理方式 |
|---|---|---|
| A 非重点 | 游戏/娱乐/生活等 | **不写总结、不写备注**：仅一行追加到 `分类\<类别>\列表.md` |
| B 重点·非教学 | 工科资讯/评测/项目展示 | 独立文件 + 2~3 句中度总结，不展开知识点 |
| C 重点·教学 | 工科教程/课程/讲解 | 独立文件 + **详细知识卡片**（见模板），优先抓字幕深度提取 |

- 拿不准档位时按低档处理，并在回复中列出存疑项供用户定夺。
- A 档列表行格式：`- [标题](链接) | UP主 | YYYY-MM-DD | BV号`

## 首次设置

1. 设置数据目录环境变量（或每次用 `--data-dir`），然后运行 `status` 确认。
2. 若 `has_sessdata=false`，请用户提供 SESSDATA：
   浏览器登录 bilibili.com → F12 → Application → Cookies → `https://www.bilibili.com` → 复制 `SESSDATA` 的值。
3. 若需要“同步分类回B站”，还需同一位置的 `bili_jct` 值，
   执行 `setup --sessdata "<值>" --bili-jct "<值>"`。
4. **回复中永远不要回显、引用或打印 Cookie 及 config.json 内容。**

## 任务一：整理收藏视频

1. `report` 检查：缓存缺失或超过 24 小时先 `sync-favorites`；读取断点位置。
   `sync-favorites` 会报告 `removed_this_run`（已取消收藏的视频），这类视频无需建档。
2. `pending --kind videos --limit 60` 分批。
3. 分类：若 `library\分类\` 已有类别目录则沿用；否则先提出 5~12 个类别方案，
   **经用户确认后**再写盘；后续批次复用现有类别。
4. 按“三档处理规则”处理。档位 C 的知识卡片模板：

```markdown
# <视频标题>

- BV号: BVxxxxxxxxxx
- 链接: https://www.bilibili.com/video/BVxxxxxxxxxx
- UP主: <UP主> | 时长: <mm:ss>
- 领域: <电子|嵌入式|机械> | 子领域: <如 STM32/模电/SolidWorks>
- 知识点标签: #<知识点1> #<知识点2>
- 收藏于: <YYYY-MM-DD> | 原收藏夹: <收藏夹名>
- 总结依据: <字幕全文提取 | 根据标题与简介推断>
- 学习状态: 未学
- 下次复习: <YYYY-MM-DD，未定时留空>

## 内容概要
<2~3 句：讲什么、解决什么问题>

## 核心知识点
1. **<知识点>**：<1~2 句解释，含关键参数/步骤/常见坑>
2. ...（教学视频提取 3~8 条，宁详勿略）

## 学习定位
- 难度: <入门|进阶|高级>
- 前置知识: <...>
- 涉及工具/硬件: <如 STM32F103、Keil5、示波器>
- 建议后续: <...>

## 知识关联
- 同 UP 主系列: <...>
- 相关卡片: <相对路径链接，如 ../分类/嵌入式/xxx.md>

## 我的备注
<!-- 用户手写区，重新同步时必须原样保留 -->
```

5. 总结深度：档位 C **优先深度版**——`subtitles --bvid` 抓字幕，读取 `saved_to` 全文后提取知识点；
   无字幕时退回标题/简介推断并如实注明。`attr != 0` 或标题含“已失效”：标记失效，跳过。
6. 每处理完一条立即 `mark-done`；每批完成后运行 `build-index` 重建索引与导图（见任务五）。

## 任务二：整理关注博主（内容标签制）

1. 缓存缺失/过期则 `sync-followings` + `sync-uploader-videos`（数量多先告知耗时，可 `--limit` 分批）。
2. `pending --kind uploaders` 取未建档 UP 主（无视频缓存者先 `sync-uploader-videos --mid <mid>`）。
3. 依据投稿标题与内容给 UP 主打标签，写 `library\博主\<UP主>\档案.md`：

```markdown
# UP主档案：<UP主名>

- mid: <mid> | 主页: https://space.bilibili.com/<mid>
- 签名: <sign>
- 内容标签: #<领域标签> #<形式标签> #<细分主题>…
- 内容领域: <主领域>（按投稿比重判定）
- 教学型: <是 ⭐ | 否>
- 覆盖知识点: <仅教学型：GPIO、串口、运放…>
- 已收录视频: <N> 条（其中收藏 <M> 条）

## 代表作
1. <标题>（<日期>）— <知识价值一句话>
```

   - 标签词表：领域（电子/嵌入式/机械/编程软件/其他）+ 形式（教学教程/项目实战/资讯评测/娱乐/其他）+ 细分主题（自由提炼，2~4 个）。
   - 教学型判定：投稿以教程/课程/讲解为主 → 是；这类 UP 主是知识库的重点来源。
4. 同时写 `视频列表.md`（标题、日期、时长、播放量、一句话主题；用户收藏过的加 ⭐）。
5. 每完成一个 UP 主立即 `mark-done --kind uploader`；`总索引.md` 按领域分组，教学型 ⭐ 置顶。

## 任务三：增量更新（“更新知识库”）

1. `report` 查看 pending 与断点；依次同步缺失数据；只处理未完成项。
2. **绝不覆盖用户手写内容**：修改已存在文件前先读原文，`## 我的备注` 段落原样保留；
   `学习状态` / `下次复习` 是学习维护字段，只在用户要求或 `review --set` 时改动。
3. 新增视频/UP主建档后运行 `build-index` 增量重建思维导图。

## 任务四：把分类同步回 B 站（写操作，最高安全级别）

前提：`status` 显示 `has_sessdata=true` 且 `has_bili_jct=true`；本地分类已完成。

1. **生成方案**（只读）：运行 `plan-classify` 自动生成移动计划
   `cache\classify_plan.json`（`{"bvid","title","from","to"}` 数组；`to` 为 null 表示目标收藏夹尚不存在）。
   它会把 `分类\` 下每个类别映射到同名 B 站收藏夹，排除 removed 视频、已在目标夹的视频。
2. **展示方案并等待用户明确确认**：新建收藏夹清单（`new_folders_needed`，默认私密）、
   各类别移动数量（`moves_by_target_folder_id`）、示例标题；`untracked_sample` 为本地有卡片但缓存缺失的视频，需先补同步。
3. **执行**：对 `new_folders_needed` 逐个 `create-folder`（幂等）→ 回填计划中 `to` 为 null 的 media_id →
   `apply-plan` 先干跑预览 → 用户认可后加 `--apply` 执行；出错立即停止并汇报
   （`unresolved_aids` 非零时需查明原因再重试）。
4. **验证**：重新 `sync-favorites` 抽查，向用户汇报。写操作在 `cache\write_log.json` 留痕。

## 任务五：知识思维导图与索引（由 build-index 自动维护）

目标：用户想找某个知识点时，一搜就能看到对应视频与博主。**`build-index` 扫描 `分类\` 下知识卡片的
「领域/子领域/知识点标签」，自动重建两个文件**，每次建档批次后运行一次即可：

- `library\思维导图\知识地图.md` — Mermaid mindmap，层级固定为 领域 → 子领域 → 知识点 → 视频叶节点
  （叶节点 `<短标题> <UP主名>`，教学型加前缀 ⭐；每个知识点最多 3 个，超出写“等N个 见知识点索引”；
  某领域知识点超过 30 个时自动拆分为 `思维导图\<领域>.md` 子图并在主图挂链接）。
- `library\思维导图\知识点索引.md` — 扁平检索索引：`## 知识点` 下每行
  `- [标题](链接) — UP主 ⭐ | [知识卡片](../分类/.../xxx.md)`。
- `library\思维导图\知识地图.png / .svg / .html` — **图片版/交互式版思维导图**（`render-mindmap` 生成）：
  图片可直接用查看器打开；HTML 是单文件交互版（可折叠/搜索/点视频跳 B 站），浏览器打开即用。
  旧格式（只有分类没有标签）的卡片自动走 分类→视频 扁平分支。
- **工科教学专属图**：`analyze` 先看工科内容分布，再
  `render-mindmap --engineering --html` 只画工科教学类（分类→话题→视频，自动排除失效视频）。

注意事项：

- 这两个文件由脚本确定性生成，**手工改动会在下次 build-index 时被覆盖**；如需重命名知识点，
  请直接改卡片里的 `知识点标签` 再重建，或用 `search` 找到相关卡片。
- 只有带 `领域` 且带 `知识点标签` 的卡片会进入索引（A 档列表行、B 档简单文件不参与）。
- `总索引.md` 顶部应放置指向这两个文件的链接（人工维护一次即可）。

## 任务六：学习复习闭环（把收藏变成学完）

1. `report` 会给出 `review.due`（到期卡片数）；`review` 列出到期卡片（未学 + 下次复习已到），
   `review --all` 看全部。
2. 学完一张卡后：`review --set --bvid BVxxx --status 已学 --next +7d`（相对日期也可用 `+2w`/`+1m`）；
   想再复习：`--status 复习中`。到期未处理时脚本会继续把它列为 due。
3. 定期 `export-anki` 把卡片「核心知识点」批量导入 Anki 做间隔复习；导出后用
   `search --q <知识点>` 随时回到原视频/卡片。

## 红线

- Cookie（SESSDATA/bili_jct）与 config.json 是敏感信息：不显示、不引用、不上传、不写进知识库；
  settings.json 不含敏感信息，可放心备份。
- **写操作三原则**：① 默认干跑预览，用户明确确认后才 `--apply`；② 只允许创建收藏夹和移动视频，
  禁止删除收藏夹、删除收藏、取关等破坏性操作（脚本也未提供此类命令）；③ 出错即停，如实汇报。
- 分类方案写盘前必须经用户确认。
- 每建档一条必须 `mark-done`，不得跳过。
- 遵守限速：不要调低 `--delay`；批量任务分批执行并汇报进度。
- 本地文件写入仅限 `library\` 与 `cache\` 计划/日志文件。
- `build-index` 会覆盖 `思维导图\知识地图.md` 与 `知识点索引.md`：这两个文件不要手工维护。
