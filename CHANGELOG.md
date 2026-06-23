# YouTube Digest 改动日志

## 2026-06-22 — 分段分析并行提速 + 旧报告字号控件回填（用户拍板的两项）

- **① 分段分析并行（`processor.py`，提速）**：原来 12 段一段接一段顺序调 v4-pro（慢的根源）。各段相互独立 → 改为 `ThreadPoolExecutor` 并行（`_ANALYZE_CONCURRENCY=4` 路，可调），结果按原序回填（`_analyze_all_chunks`）。
  - 容错：新增 `_analyze_chunk_safe` 包 `_retry`（3 次退避）且**绝不抛**——单段持续失败只返回占位、不拖垮整篇（顺带给 DeepSeek 调用补上了原先没有的重试）。
  - 进度：按「完成数」递增发 `分析第 k/总 段完成…`，前端进度条照常单调推进。`client`(OpenAI/httpx) 线程安全、`progress_cb` 经 `call_soon_threadsafe` 线程安全。
  - 限流注意：4 路对 v4-pro 是较稳起点；遇 DeepSeek 限流可调小 `_ANALYZE_CONCURRENCY`。
  - 验证：8 段 stub 单测——保序✅、墙钟 0.61s（顺序需≈2.4s）证并行✅、单段 raise→占位且不影响其余+warn 汇总✅、进度日志 N 条✅。
- **② 旧报告字号控件回填（`scripts/retrofit_fontsize.py`，幂等）**：字号控件本只对新报告生效；写一次性脚本给 `output/` 里 44 个存量报告注入（`</head>` 前插 --fs+calc 缩放+控件样式+防闪烁脚本，`</body>` 前插控件 DOM+setFS；注入 `<style>` 排在原样式后故生效，无需 !important；`MARKER='class="fs-ctrl"'` 判幂等）。
  - 执行：44 个全部注入、复跑全跳过（幂等）。改前已备份 `_backups/2026-06-22-fontsize-retrofit/`（44 份）。
  - 验证：抽样 fs-ctrl/setFS/--fs/calc 各 1 次、结构完整；preview 实测真实旧报告——控件就位、`.sec-summary` 计算值 16.8px(大)/21px(超大)、截图过眼版式无破。
- **生效**：并行改在 `processor.py`（须完全退出重开 App）；旧报告注入已直接落盘（刷新旧报告即见字号控件，不依赖重开）。今日 processor.py 最终快照 `_backups/2026-06-22-sse-resume/processor.py.2026-06-22-final`。

## 2026-06-22 — 字幕偶发为空自动重抓 + 报告页字号选择控件（processor.py）

- **① 字幕偶发「明明有字幕却报无字幕」自动重试**：
  - 症状：处理某视频第一次报「没有任何字幕」，原样再点一次（同一视频）第二次就成功。
  - 诊断：`get_transcript_from_info` 是从已抓好的 `info` 里取字幕轨；YouTube 对同一请求时好时坏，偶尔返回的 `info` 字幕轨是空的。`extract_video_info` 外层只对**异常**重试，而「拿到了 info 但字幕轨为空」不是异常 → 不会重抓，于是抛「无字幕」。用户手动再点一次＝重抓了一份新 info，所以第二次好了。
  - 修法（`process_video`）：把取字幕包进 3 次重试循环——空了就 `time.sleep(4)` 后重新 `extract_video_info` 再试（并用新抓到的、通常更完整的 metadata），等价于自动化「手动再点一次」。Cookie 类提示不重试（重试无用）直接抛。
- **② 报告页字号选择控件（`generate_html`）**：用户反馈正文偏小、想能调大。
  - 新增 `:root{--fs}` 缩放变量 + 左下「字号」浮控（4 档：标准/大/特大/超大，四个递增的「A」按钮，当前档深蓝高亮），点选即缩放、`localStorage` 记忆（key `ytd_fs`），`<head>` 内联脚本防刷新闪烁。**默认「大」(1.12)**——直接比原来稍大，回应「太小」。
  - 缩放范围：只放大「阅读内容」——分段标题/正文简介/研究·例子详情/深挖/术语表/引用/精炼版抽屉/追问答案（复用 `--read` 那组选择器 + 分段标题），用 `calc(基准px*var(--fs))`；侧栏 TOC、序号、时间戳、chip、标签等「骨架」不缩放以保层级。控件置左下，避开右上「精炼版」与右下「笔记库」浮钮。
  - ⚠️ 只对**新生成**的报告生效（老报告是已写死的静态 HTML，与历来一致）。
- **验证**：`ast.parse`+import 通过；用 mock 数据真实跑 `generate_html` 渲染报告，8 项结构自检全过（`--fs`/calc 缩放/控件 DOM/4 按钮/setFS/防闪烁脚本/默认大档）；preview 实测——默认档 `.sec-summary` 计算值 16.8px(=15×1.12)、超大档 21px(=15×1.4)、引用 17.5px，控件高亮正确，截图过眼：默认与超大两档版式无溢出、控件不撞其它浮钮。桌面预览 `~/Desktop/预览/yt-digest/报告字号示例.html` 可双击试玩。
- **生效**：改的是 `processor.py`（内置服务启动时加载），用户须**完全退出重开「笔记中心」**。备份：本次改后快照 `_backups/2026-06-22-sse-resume/processor.py.after-caption-fontsize`；改前较近的干净副本是 `_backups/2026-06-22-ui/processor.py.bak`（UI 批次时，早于 tv-client 字幕修复）。⚠️ 注：本次 processor.py 改前未即时备份（疏漏），上述 UI 副本为最近可用回退点。

## 2026-06-22 — 修复长视频处理「连接中断」：SSE 心跳加密 + 断线可续传（server.py + index.html）

- **症状**：处理某长视频（58:14、12 段、v4-pro）时进度跑到中途报「⚠️ 连接中断」，整单作废；试三次都断，且**每次断在不同段落**（11/12、其它段不一）。之前抓不到字幕的视频已能正常生成——这是另一类问题。
- **诊断（读源码定位，非盲改）**：是 **SSE 长连接被传输层掐断**，不是某段内容的代码 bug（所以断点随机）。四因素叠加：
  1. 服务端心跳太稀——`/stream` 只在队列空闲 **120s** 后才发 `ping`；而 v4-pro 分析某一段可静默 60–120s，WKWebView 内核 NSURLSession 默认 **60s** 空闲就掐连接，比 120s 先到 → 哪段先越线就断哪段（随机）。
  2. 前端 `onerror` 里直接 `es.close()`+重置，**一断就彻底放弃、不重连**。
  3. 服务端 `finally` 里 `jobs.pop`，**一断线就销毁任务**，连重连都没得连。
  4. 队列一次性消费，断线后已分析的段全丢、十几分钟白跑。
- **修法（两层，标本兼治）**：
  - **治本**（`server.py`）：心跳 `PING_INTERVAL` 120s → **15s**，远小于 60s 空闲超时，长段分析期间连接也不掉。
  - **兜底**（`server.py`）：把 `asyncio.Queue` 换成 `Job` **事件缓冲**——事件按下标存列表、SSE 带 `id:` 推送；`/stream` 读 `Last-Event-ID`（EventSource 重连时自动带）从断点**续传**，不丢段不重跑；断线**不再销毁任务**（完成后保留 `JOB_GRACE`=300s 供取回结果，再回收；`MAX_JOBS`=30 防内存涨）。新增 `retry: 3000` 让浏览器 3s 自动重连。
  - **前端**（`templates/index.html`）：`onerror` 区分 `readyState`——`CONNECTING`（自动重连中）只显示「网络波动，正在自动重连…」不放弃；仅 `CLOSED` 才算真失败；连续 20 次（~1 分钟）仍连不上才停。
- **验证**：① `ast.parse`+import 通过；② FastAPI TestClient（stub 掉真实抓取/DeepSeek）三项全过——任务不存在返回友好 error；**断线续传**：读到 id2 断开，凭 `Last-Event-ID:2` 重连从 id3 续传、0..9 连续**无缺口无重复**、收到 `done`；**心跳**：2.5s 静默期内收到 2 次 ping、连接保持到 done；③ 新 server.py 在真实 uvicorn（:8011 临时实例）下启动无报错、首页与 `/stream` 均正常。
- **生效**：改了 `server.py`（内置服务启动时才加载），用户需**完全退出并重开「笔记中心」**。前端 `index.html` 改动刷新即生效。备份 `_backups/2026-06-22-sse-resume/`。

## 2026-06-22 — 修复「明明有自动字幕却报无字幕」：player_client 改用 tv

- **症状**：某些视频（如 `ksRcFGLPoSk`，Codie Sanchez「I Used AI To Build A Business In 24 Hours」）在 YouTube 上明明能选自动英文字幕，工具却报「该视频没有可用字幕」，且日志里「时长 0:00」。换个视频又正常。
- **诊断（直接用项目 yt-dlp 复现多 client 对比，未盲改）**：
  - A 当前 `web`（+cookies）：`duration=None`、`automatic_captions=0` ← 残缺，正是报错来源；
  - C `ios` / D `android`：拿到时长，但 `automatic_captions` 仍 0；
  - **E `tv`（客厅端）：时长 1290s、自动字幕 157 种（含 `en-orig`/`en`）** ← 完整。
  - 结论：2026 起 YouTube 对 `web/ios/android` client 的字幕轨加了 PO-token 限制，这些 client 返回空字幕；`tv` client 不受限。
- **修法**：`_SAFE_YDL_OPTS` 增 `extractor_args={"youtube":{"player_client":["tv","web"]}}`（tv 主力拿时长+字幕，web 兜底）。`processor.py` 一处改动。
- **验证（真实代码路径 `extract_video_info`+`get_transcript_from_info`）**：问题视频现得 时长 21:30、字幕 en、673 条；老视频 CS50（`bB2o81DnKHk`）仍正常 1:03:46、1862 条，无回归。
- **生效**：服务端改动，用户需**重开「笔记中心」**（重启内置服务）后再处理该视频。

## 2026-06-22 — 修复 App 内粘贴失效 + 视频中文标题 + 日期更醒目 + 按日期检索视图

- **修复「笔记中心.app 里粘贴不进去」**（`dashboard/native/main.swift`，需 `bash dashboard/native/build.sh` 重编）：根因是原生壳的 `buildMenu()` 只建了 App 菜单（隐藏/退出），**没有「编辑」菜单**——macOS 把 ⌘X/⌘C/⌘V/⌘A/⌘Z 这些动作绑在编辑菜单项上、靠它沿响应链送进 WKWebView 的输入框；缺了菜单，输入框里按 ⌘V 完全没反应。修法：加标准「编辑」菜单（撤销/重做/剪切/复制/粘贴/全选，标准 `undo:`/`cut:`/`copy:`/`paste:`/`selectAll:` 选择器 + 快捷键）。已重编部署到 /Applications + 桌面；**用户需完全退出旧实例再重开新版**才生效。
- **视频中文标题**（`processor.py` + `templates/index.html`）：
  - 生成：`generate_overall_summary` 的 prompt + 返回新增 `title_cn`（≤28 字、可意译；同一次调用产出，几乎零额外成本）；`process_video` result 与 `_append_library_index` 都带上。
  - 存量回填：一次性脚本对 output/index.json 里 38 条无 `title_cn` 的历史视频做**单批翻译调用**（仅凭原标题，~23s 一次返回），补齐中文标题。
  - 展示：历史卡片以**中文标题为主**、原始（英文）标题作 `.lib-card-orig` 副标题；结果卡、最近视频链接同理；报告页 `generate_html` 用 `disp_title = title_cn or title` 渲染 hero/侧栏/`<title>`，hero 下方加 `.hero-orig` 原标题副标题。
- **日期更醒目**（`templates/index.html`）：历史卡片把原本埋在 meta 行尾的日期，提到 meta 行首做成**带日历图标的 `.date-chip`**（pale-navy 药丸），一眼可见。
- **按日期检索视图**（`templates/index.html`）：历史区头部加「卡片 / 按日期」视图切换（`.view-toggle` + `libView` 状态 + `setLibView`）。「按日期」时 `renderDateView` 把历史**按日期分组**（日期头含今天/昨天 + 数量角标），每条只显示**小封面 + 中文标题**、点开报告，紧凑利于回溯检索；缺日期的条目归到末尾（`未知日期` 排序修正）。
- **验证**：`ast.parse(processor.py)` 通过、swiftc 编译通过；真实库 38 篇渲染——卡片中文标题+英文副标题+日期 chip、按日期视图（11 组 38 行、首组今天/末组未知日期）、报告 hero 中文标题+英文副标题，全程截图过眼，控制台无报错。备份 `_backups/2026-06-22-ui/`（含 index.json、main.swift.bak）。

## 2026-06-22 — UI 视觉升级：线性图标 / 真实缩略图 / 霞鹜文楷 / 筛选合一 / 阶段进度 / 响应式（剩余 7 条一次做完）

> 用户拍板「全照新方案做」前，先用真实配色 + 真实视频数据出了一张「当前 emoji ↔ 新方案」对比 mockup 截图确认方向，再动两个生产文件。只改 `templates/index.html`（输入页）与 `processor.py` 的 `generate_html`（报告页），**未碰 server.py，也无需重编「笔记中心.app」**（原生壳只是 WKWebView 加载 :8000，内容变了即生效）。

- **#7 emoji → 线性图标（全站统一一套深藏青描边图标）**：
  - 输入页：页头品牌/按钮（视频/首页/书/设置）、横幅警告、卡片标题（最近视频=列表、历史=层叠、回收站=垃圾桶）、开始处理（播放）、结果与历史卡片的频道(电视)/时长(时钟)/段数(层叠) meta、操作按钮（报告=文档、编辑=铅笔、定位=文件夹、重跑=循环箭头、删除=垃圾桶、恢复=回转箭头、复制=叠层）。统一为内联 SVG（stroke=currentColor，1.6 描边），新增 JS `IC` 映射给动态卡片复用。
  - 报告页：hero meta（频道/时长/字数/日期/观看）、分段研究(烧瓶)/例子(灯泡)/深挖(放大镜)/原文(书)/追问(对话框)、术语表/精炼版本(书)、打开笔记库(文件夹)。新增模块级 `_ric()` + `IC_*` 常量。
  - 日志区与状态行里的 ✅❌⚠️ 保留（控制台式文本标记，部分来自后端消息，非 UI 图标）。
  - 顺手修：编辑/定位/删除/恢复等按钮在「确认/加载中」瞬时态后用 `innerHTML` 还原（原 `textContent` 还原会把图标 SVG 抹掉）。
- **#2 真实视频缩略图**：结果卡 + 历史卡 + 报告 hero 改用真实 YouTube 封面（`https://i.ytimg.com/vi/<id>/mqdefault.jpg`，真 16:9）+ 播放叠层 + 时长角标。**无需动后端**——库索引条目已存 `video_id`（老条目从 url 解析兜底），前端/模板直接拼 URL；拉不到原图时 `onerror` 回退到深蓝渐变 + 播放图标。
- **#4 报告正文换霞鹜文楷**：`:root` 加 `--read:"LXGW WenKai",…`（已装机），只作用于阅读正文（整体概述/要点/分段简介/研究例子 detail/原文整理/术语释义/精炼版/金句），标题/标签/序号/时间戳仍走 sans 维持层级。
- **#8 两套类别筛选合一**：原「粘贴区 chip 行」+「历史区下拉」两套各自独立 → 删下拉、移除粘贴区 chip，统一为历史区头部一行 chip，与列表共用单一 `currentCat` 状态。
- **#6 抓取阶段进度反馈**：进度条原只在「分析第 N/M 段」时动、抓取阶段死在 0%。新增阶段标签（脉动圆点 + 文案）+ 把已有日志消息映射成阶段与进度（抓取视频信息 10% → 字幕 22% → 切分 32% → 分析 32–88% → 总结 90% → 文件 97% → 完成 100%），抓取阶段进度条加流动微光。**纯前端**，未改 server/processor 的事件。
- **#10 输入卡瘦身 + 保存按钮层级**：类别浏览 chip 从粘贴卡移走（并入 #8 历史区），粘贴卡只剩 链接框 + 最近视频(折叠) + 粒度 + 开始；设置卡「保存设置」从次级浅蓝改为深蓝主色按钮。
- **#9 响应式缺口**：新增 `@media(max-width:600px)`——页头堆叠、设置两列改一列、结果/历史卡片换行且操作区铺满、粒度行标签不再被挤成竖排（换行 + label `nowrap`）；报告页 hero 在窄屏缩略图与文字上下堆叠。
- **验证**：对比 mockup + 真实库数据（38 篇）截图全程过眼——桌面/移动两宽度、报告页 hero 缩略图真实加载、分段图标、霞鹜文楷生效（computed `"LXGW WenKai"`）、#6 阶段（合成 `分析第 3/7 段`→「分析中 3/7 段」56%）、设置主色按钮。`ast.parse(processor.py)` 通过，浏览器控制台无报错，无悬挂引用残留。备份在 `_backups/2026-06-22-ui/`。

## 2026-06-22 — 切 v4-pro + 配色和谐化 + 标题中英并排 + 打包独立 App

- **模型切到 deepseek-v4-pro**：经官方 `/models` 接口查证，账号实际可用的是 `deepseek-v4-pro` / `deepseek-v4-flash`；而旧配置 `deepseek-chat` 已被悄悄映射到 **v4-flash**（即一直没在用 pro）。config.yaml 改为 `deepseek-v4-pro`。
  - v4-pro 是推理模型：实测一次 chunk 分析 reasoning 占约 2300 token。为防正文被截断，三处调用 max_tokens 上调：analyze_chunk 4000→8000、generate_overall 2600→6000、generate_full_digest 3000→7000。`.message.content` 仍是干净 JSON，解析无需改动。
- **配色和谐化（去廉价亮蓝 + 统一静音盘）**，均在 templates/index.html：
  - 开始处理按钮 hover `#1d4ed8` → `--navy-dark`；库卡片阴影 `rgba(37,99,235)` → `rgba(30,58,95)`。
  - 进度日志区：深色终端 `#0f172a` + 霓虹色 → 暖米卡片 `#f4efe5` + 静音色（navy/muted/warn/red/green 变量）。
  - alert 三态边框/文字的 Tailwind 亮色 → amber/olive/red 静音体系（复用已有边框色）；error 卡片边框、错误结果内联色一并并入。
- **报告分段标题中英并排**（processor.py generate_html）：英文标题 `title_en` 从右侧 aside 移到中文标题 `h2` 正下方（新增 `.sec-title-en`，斜体次级），中文在前、英文紧随，便于快速抓信息。右侧 aside 仅保留英文金句 + 概念。
- **打包成独立 App**（两版迭代）：
  - 初版 shell 套壳 `YouTube Digest.app`，但有三个问题：① Dock 图标一直跳（shell 无事件循环，系统以为还在启动）；② 走系统默认浏览器（Chrome），非独立窗口；③ 只含油管，丢了文本精读。**已废弃删除。**
  - **终版：原生 Swift + WKWebView 的「笔记中心.app」**（源码 `~/Downloads/apply/dashboard/native/main.swift` + `build.sh`，改完跑 build.sh 重编部署）。一个真 Cocoa 窗口（不跳、不走浏览器），启动时拉起 :8000 + :8010 两个服务、加载 dashboard hub，原生工具栏（返回/笔记中心/YouTube Digest/文本精读）窗口内导航，退出 App 即停两个服务。Info.plist 用 `NSAllowsLocalNetworking` 放行 http://127.0.0.1；用 WKUserScript 注入把网页内会弹浏览器的 `goDashboard`/`openTextDigest` 改成发消息给原生、留在窗口内（**未改任何 web 代码**）。图标 SVG→qlmanage→iconutil。已部署 /Applications + 桌面，computer-use 实测：原生窗口正常、hub 两卡片在、点工具栏与网页按钮都在窗口内切换、不弹 Chrome。

## 2026-06-22 — 修复抓取失效：统一走 yt-dlp，砍掉裸奔的 youtube-transcript-api

- **根因**：抓取分两条路——元数据走 yt-dlp、字幕走 youtube-transcript-api。① yt-dlp 默认会解析「可下载格式」，撞上 YouTube 新的 player/PO-token 反爬，报 `Requested format is not available` / `Sign in to confirm you're not a bot`，连元数据都取不到；② 字幕那条路完全不带 cookie「裸奔」，被 YouTube 单独 IP 封锁报 `RequestBlocked`。实测确认两病并存。
- **修法（统一数据源）**：一次 `extract_info` 同时取元数据 + 字幕，彻底移除 youtube-transcript-api。
  - yt-dlp 选项加 `skip_download` + `ignore_no_formats_error`，走轻量提取路径、不解析格式 → 绕开反爬；
  - 字幕改从 `automatic_captions` / `subtitles` 取 json3 轨自行解析（`_parse_json3` 过滤滚动/空事件，另带 vtt 兜底），优先 手动英文 > 自动英文 > 任意；
  - 字幕轨返回的 json3 URL 自带授权 token，可直接 urllib 拉取。
- **cookie 更稳**：`_cookies_opt` 改为优先用项目目录下导出的 `cookies.txt`（不受 Chrome 是否运行 / cookie 库加锁 / 新版 macOS 应用绑定加密影响），没有该文件再回落到实时读浏览器。
- **错误提示**：取不到字幕且未设 cookie 时，明确提示「YouTube 现在要求登录态才返回字幕，请设置浏览器 Cookie」，不再甩看不懂的报错。
- 实测：之前一直失败的 `FsztuzyXdhY`（Trevor Noah×Diary of a CEO，2:38:57）现已跑通——元数据正确、字幕 4356 条、切 16 段共 16 万字符，无重试报错。

## 2026-06-04 — 第四轮补丁：回收站浮动到右下角 / 收起键修复 / 旧文件也能恢复 / toast 不遮挡

- **修复「收起」失灵**：`toggleHist` / `toggleTrash` 原判断 `|| !style.display` 在已展开（display 为空串）时恒为「展开」，导致再点只会保持展开。改为按 `display==='none'` 判断（hist 用 'flex'/'none'），回收站改为 `.open` class 切换，开合都正常。
- **回收站移到页面右下角浮动**：不再占主视线。默认是右下角小药丸「🗑 回收站 N」，点开在其上方弹出面板（列表 + 收起 + 打开文件夹），回收站为空时整个浮标自动隐藏。
- **旧文件（orphan）也能一键恢复**：`restore_entry` 对 orphan 改为尽力恢复——把 `_trash/` 里的报告 HTML 移回 `output/`（去删除时间戳前缀，重名加 `_restored`）并重建一条最简 index 条目（标题取自文件名，类别「其他」，无原始 url/简介）。回收站每条都给「↩ 恢复」按钮，orphan 仅额外标「旧文件」提示信息不全。实测删→恢复、orphan 恢复均 OK。
- **toast 不再遮挡回收站、按钮可点**：`showToast` 原 `bottom:24px;right:24px` 与右下角回收站浮标同位，淡出后(opacity:0)仍留在 DOM **拦截点击**——导致回收站按钮「点不中」；且「已移入回收站」文字盖住 🗑 图标。改为 `bottom:70px`（移到浮标上方，删除提示不挡图标，便于即时撤回）+ `pointer-events:none`（永不拦截下层点击）。实测：toast 底边在浮标顶边之上、`pointer-events:none`、恢复按钮 `elementFromPoint` 命中按钮本身。

## 2026-06-04 — 第四轮：笔记字数 / 分段简介加长 / 回收站可视可恢复

### 1. 阅读页顶部显示全文字数
- `generate_html` 统计这篇笔记的实际内容字数（`_count_words`：只数 CJK 汉字 + 英文单词，标点空白不计；覆盖整体概述/精炼版本/主题/要点/术语表 + 每段标题/简介/研究例子的 detail+dig），在 hero-meta 加「📝 全文约 N 字」。仅对**新生成**的报告生效。

### 2. 分段卡片「内容简介」加长
- `analyze_chunk` 的 `summary_cn` 提示由「3-5 句摘要」改为「6-10 句、一两段连贯中文，尽量详尽地把本段讲了什么完整呈现（观点/论证/怎么展开/关键人物例子结论），让没看视频的人也能充分了解」；仍严格只依据本段、不编造。max_tokens 已是 4000，足够。

### 3. 回收站（粘贴页可视化 + 一键恢复）
- **澄清**：删除是「软删除」，文件移到项目内 `output/_trash/`（HTML）和 vault `<folder>/_trash/`（md），**不是 macOS 废纸篓**，所以在系统废纸篓看不到。
- 粘贴页「最近处理的视频」下方新增折叠「🗑 回收站」：列出已删除笔记，每条「↩ 恢复」一键还原（HTML 移回 output、md 移回类别目录、重新写入 index.json）；底部「📂 打开回收站文件夹（在访达里）」。
- 删除时写入回收站清单 `output/_trash/_trash.json`（含原始索引条目，供恢复）。第三轮已删、无清单记录的旧文件标「旧文件」，提示去文件夹手动取回。
- 后端：`list_trash` / `restore_entry` + `/trash`、`/restore-entry`、`/reveal-trash`。

## 2026-06-04 — 第三轮：阅读页定位钮 / 精炼版本改名 / 折叠术语表 / 历史折叠分组 / 软删除

### 1. 阅读页右下浮动「打开笔记库」（reveal-fab）
- 每篇报告 HTML 右下角新增圆形浮动按钮：默认**只显示 📂 图标**，悬停展开显示「打开笔记库」文字（`.rf-tx` 默认 `max-width:0;opacity:0`，:hover 展开）。
- 点击 → JS `revealSelf()` 用 `decodeURIComponent(location.pathname.split('/').pop())` 推导本文件名 → POST `/reveal`，在访达里定位本文，方便随手分享。
- 优雅降级：file:// 打开（分享给朋友）时不调接口、给 toast 提示。

### 2. 「全文整理」→ 改名「精炼版本」
- 报告右上 fab 与右侧抽屉标题统一改为「📖 精炼版本」（功能不变，仅措辞）。

### 3. 关键概念 → 折叠「术语表」
- `generate_overall_summary` 提示新增 `glossary` 字段：从概念精选 6–10 个核心词，每条一句 20–45 字大白话定义，**只讲这个视频里怎么用/讲该概念**，不引入视频外知识（max_tokens 2000→2600）。
- 报告概览区原「关键概念」标签堆改为折叠卡片「📖 术语表 · N 个核心概念」：默认收起，点击展开；每个术语是锚点，点击跳到它首次出现的段落（`concept_first` 映射，按 `_concept_core` 去括号匹配 + 子串兜底）。无 glossary 时回退旧标签卡。

### 4. 粘贴页「最近处理的视频」折叠 + 按日期分组
- 默认折叠成一个条形（显示「· N 篇」+「点击展开 ▾」），点击展开/收起。
- 展开后按「今天 / 昨天 / 具体日期」分组（`dateLabel`）；每条左侧时间标签默认显示「MM-DD」日期，**点击切换显示具体 HH:MM**（仅新生成、带 `created_at` 的条目有分钟，老条目只有日期）。
- 新增 `created_at`（`process_video` 结果与 `_append_library_index` 写入 `YYYY-MM-DD HH:MM`）。

### 5. 文章库卡片「🗑 删除」（软删除）
- 库卡片操作区新增「🗑 删除」，**二次确认**（首次点变「确认删除？」，4 秒超时还原）。
- 后端 `delete_entry()` + `/delete-entry`：把报告 HTML 移到 `output/_trash/`、Obsidian 笔记移到 vault `<folder>/_trash/`、从 index.json 移除该条 → **可恢复**（不硬删）。
- 设计依据：粘贴页「最近链接」与文章库是**同一数据集**，故「清理历史」只做折叠（纯显示降噪），真正删除走可恢复的软删除。

### 设计约定（延续）
- 严格只改用户点名项；预生成内嵌（术语表/精炼版本）分享可用，定位/删除需本地服务（优雅降级）。
- 配色延续暖米底 `#faf8f4` + 深藏青 `#1e3a5f`，术语表/日期标签走低饱和协调。

## 2026-06-03 — 第二轮：全文整理 / 深挖整理 / 库内编辑 / 顶栏导航

### A. 最近链接条（粘贴区）
- 粘贴卡片里新增「最近处理过的链接」：每条链接**可直接点开**（新标签打开原视频），右侧「复制」按钮**单条复制**该链接。数据取自历史记录前 12 条。

### B. 全文整理（精读速览）
- 处理时预生成一篇 600–1500 字的连贯散文整理（`generate_full_digest`，只依据视频已梳理内容、不编外部知识），内嵌进 HTML，**分享出去也能看**。
- 报告右上角「📖 全文整理」浮动按钮 → 右侧滑入抽屉浮层展示，不打断主阅读、可随时开关（含遮罩 + Esc 关闭）。

### C. 类别 / 标签 / 导航
- **C1 库内编辑**：历史卡片新增「✎ 编辑分类」，内联弹出类别下拉（11 类）+ 可新建类别 + 标签输入。保存 → `/update-meta`：更新 index.json、把 Obsidian 笔记搬到新类别子文件夹、重写 frontmatter（category/tags）与正文类别行、尽力同步报告 HTML 里的类别/标签 chip。
- **C2 按类别浏览**：粘贴区新增「按类别浏览历史」chip 行，点一下即筛选下方历史记录并滚动过去。
- **C3 顶栏导航**：新增「← 笔记中心首页」（`/open-dashboard` 打开 dashboard/index.html）和「📖 文本精读」（开 localhost:8010）。

### D. 研究/例子更详细
- `analyze_chunk` 的 detail 由 2–4 句扩到 3–6 句（是什么/谁做的/数据/论点/结论），仍严格忠实原片。

### E. 深挖改版（关键）
- 旧版「深挖」给的是外部搜索链接 + 提示词；新版改为处理时**预生成的「原文整理还原」**（5–10 句，流畅复述原片是怎么聊这一段的，非逐字照搬，仅依据该段、未展开则注明），内嵌可展开，分享也能看。
- 想查证外部知识的需求移到「追问」区的快捷按钮（见 F）。

### F. 追问区增强
- 保留自由输入框（`/ask` 由本地助手依据字幕回答）。
- 新增以本段各「点」命名的快捷按钮行：点一下把该点自动填成**可复制的外部 AI 提示词**（去问 Claude/GPT 自行查证，不让 DeepSeek 答外部事实）；配「📋 复制提示词」按钮。

### G. 定位本地文件
- 历史卡片新增「📂 定位文件」→ `/reveal` 后端 `open -R` 在访达里定位该 HTML 报告，方便发微信分享（朋友手机浏览器可直接打开）。

### 设计约定（延续）
- 预生成 + 内嵌（深挖整理、全文整理、外部提示词静态化）→ 分享可用；自由追问 / 定位 / 编辑需本地服务（优雅降级提示）。
- 内部复述/问答可用 DeepSeek；外部知识只做关键词/提示词助手。
- 配色延续暖米底 `#faf8f4` + 深藏青 `#1e3a5f`，多色低饱和琥珀/橄榄协调。

## 2026-06-03 — 七大痛点优化

### 1. 修复分段粒度 BUG（核心）
- 现象：选了「每 5 分钟」分段，结果还是按 10 分钟出。
- 根因：前端 `startProcess()` 读了下拉框却没把值发给后端；`/process` 只收到 `{urls}`，后端始终用 config 里的默认 10 分钟。
- 修复：前端把 `chunk_minutes` 放进请求体；`ProcessRequest` 增加 `chunk_minutes`；`process_video(..., chunk_minutes=None)` 支持本次覆盖。设置页里那个会误导的"默认粒度"数字框删掉，改成开始处理区的「本次分段粒度」下拉（应用于本次全部链接）。

### 2. 按类别/标签归档
- `generate_overall_summary` 现在额外产出 `category`（从固定 11 类里选）+ `tags`（3–6 个）。
- 笔记自动存进 `vault/文件夹/<类别>/` 子目录，Obsidian 里天然分文件夹。
- 历史记录区新增「类别筛选」下拉，每张卡片显示类别 chip + 标签 chip。

### 3. 英文当"点心"
- 英文标题 + 英文金句从正文主栏移到分段卡片右侧 `English · 点心` 栏，想读才读，不挤占中文阅读。

### 4. 研究/数据/例子/故事讲透（样式 2）
- 每段的 research / examples 由「一句话」升级为对象 `{title, detail}`：标题一行，下面 2–4 句详述（是什么、谁做的、发现了什么、支撑什么观点）。
- 采用样式 2：详情直接铺在正文里、不折叠，一口气读完。
- 严格忠实原片：prompt 反复强调 detail 只依据视频内容，绝不编造数字/机构/结论；没讲清的就说"视频中未展开"。

### 5. 纸张质感底色
- 全站浅色底改用暖米 `#faf8f4`（卡片 `#fffdf9`），文字走暖近黑、线条走暖灰，降约 15% 对比度护眼。
- 强调色统一深邃藏青 `#1e3a5f`（替换原来发亮的塑料蓝 `#3b82f6`）；高亮笔性质用浅天蓝 `#cfe0ef`。

### 6. 延伸追问 / 延伸阅读（双轨）
- **视频内部追问**（可信）：每段一个「💬 追问这一段」框，POST `/ask` → DeepSeek 仅依据该段（含相邻段）字幕原文回答，原文没有就如实说"这段视频里没有提到"，不编外部知识。
- **外部延伸**（不让 DeepSeek 答）：每条研究/例子一个「🔍 深挖」按钮，弹出 Google / Google 学术 / 维基百科 搜索链接 + 一段可复制的提示词，拿去问更强的 AI 自己查证。
- 为支持追问，处理时把分段字幕存档到 `output/transcripts/<video_id>.json`。

### 7. 抓取报错修复（SSLEOFError / "not a bot"）
- `_retry` 指数退避重试（4 次）包裹取元数据和取字幕。
- 视频之间留 5s 间隔，避免一股脑打 YouTube 触发限流。
- 支持浏览器 Cookie：设置页选 chrome/safari/edge/brave/firefox，yt-dlp 用 `cookiesfrombrowser` 带上登录态绕过机器人验证。
- 命中"Sign in to confirm…/not a bot"时抛出友好提示，引导去设置里选浏览器 Cookie。
