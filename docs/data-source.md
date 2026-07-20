# Data Source Notes

## 当前默认：MediaCrawler 采集框架

从现在开始，本项目抓取抖音达人作品的默认底层使用外部 MediaCrawler 项目框架，不再把本项目自己的浏览器/Crawlio 捕获逻辑作为主路径。

本项目只保留一个薄适配层：

```text
douyin_creator_monitor/scripts/collect_douyin_creator_with_mediacrawler.py
```

职责边界：

1. 调用本机 MediaCrawler checkout，对抖音达人主页执行 `dy + creator` 类型采集。
2. 读取 MediaCrawler 导出的 JSONL/JSON/CSV 结果。
3. 规范化为本项目后续飞书同步脚本所需的统一结构。
4. 在写出结果前校验每条当前作品必须具备：`aweme_id`、`create_time`、`digg_count`、`comment_count`、`collect_count`、`share_count`。

默认输出：

```text
douyin_creator_monitor/runtime/zhiliao-works-from-mediacrawler.json
```

示例命令：

```powershell
$env:MEDIACRAWLER_DIR = "D:\path\to\MediaCrawler"
python douyin_creator_monitor\scripts\collect_douyin_creator_with_mediacrawler.py `
  --creator-url "https://www.douyin.com/user/REPLACE_WITH_SEC_USER_ID" `
  --expect-min-count 43 `
  --clean-media-output
```

如果 MediaCrawler 目录下存在 `.venv\Scripts\python.exe`，适配脚本会自动用该虚拟环境运行 MediaCrawler。也可以用 `MEDIACRAWLER_PYTHON` 或 `--media-crawler-python` 显式指定 Python。

如果 MediaCrawler 已经生成过导出文件，也可以只做规范化：

```powershell
python douyin_creator_monitor\scripts\collect_douyin_creator_with_mediacrawler.py `
  --creator-url "https://www.douyin.com/user/REPLACE_WITH_SEC_USER_ID" `
  --media-crawler-dir "D:\path\to\MediaCrawler" `
  --normalize-only
```

MediaCrawler checkout、登录态、Cookie、原始导出和本项目运行产物都属于本机状态，不提交到 Git。

## 统一作品字段映射

本项目下游统一使用以下字段，不关心底层是 MediaCrawler 原始字段还是抖音接口原始字段：

```text
aweme_id -> 抖音作品ID
desc -> 原始文案 / 作品标题 / 话题标签
create_time -> 发布时间（Unix 秒，写入前转北京时间）
digg_count -> 点赞数
comment_count -> 评论数
collect_count -> 收藏数
share_count -> 分享数
cover_url -> 封面图URL
url -> 作品链接
is_top -> 是否置顶
```

适配层默认要求 MediaCrawler 以 `jsonl` 保存，兼容 `json` 和 `csv`。调用时会显式传入 MediaCrawler 官方 CLI 参数：`--platform dy`、`--type creator`、`--creator_id`、`--crawler_max_notes_count`、`--save_data_option`、`--save_data_path`。

MediaCrawler 的 Douyin JSONL 写入器会在传入的 `--save_data_path` 下继续创建平台和格式子目录。达人作品的真实文件通常是：

```text
douyin_creator_monitor/runtime/mediacrawler-output/douyin/jsonl/creator_contents_YYYY-MM-DD.jsonl
```

不要只扫描 `mediacrawler-output/` 顶层；适配层必须递归读取该目录，并允许 `creator_contents_*.jsonl` 这类文件名。是否为抖音作品由记录内容规范化校验决定，而不是只靠文件名里的 `douyin` 或 `aweme` 判断。

适配层会兼容 MediaCrawler 常见等价字段，例如 `liked_count`、`collected_count`、`aweme_url`，也兼容抖音原始接口里的 `statistics.digg_count/comment_count/collect_count/share_count`。

正式写入飞书前必须先完成核心字段校验。缺少发布时间或互动数据时，不能把任务报告为完成，也不能用空值覆盖飞书历史数据。

## 历史兜底：抖音主页作品列表接口

以下内容是此前为排查和验证数据源记录的浏览器/Crawlio 方法。它证明核心数据来自达人主页作品列表接口，但不再作为默认采集底层。

当前验证可用的数据来源是达人主页加载时请求的列表接口：

```text
/aweme/v1/web/aweme/post/
```

该接口需要在已登录 Chrome 页面中由抖音前端生成签名参数后读取。已观察到的关键参数包括：

- `sec_user_id`
- `max_cursor`
- `count`
- `need_time_list`
- `a_bogus`
- `x-secsdk-web-signature`

## 作品字段映射

- `aweme_id`: 抖音作品 ID
- `desc`: 原始文案
- `create_time`: 发布时间，Unix 秒级时间戳，写入飞书前转为北京时间
- `statistics.digg_count`: 点赞数
- `statistics.comment_count`: 评论数
- `statistics.collect_count`: 收藏数
- `statistics.share_count`: 分享数
- `video.duration`: 视频时长，通常为毫秒
- `video.cover.url_list[0]`: 封面图
- `is_top`: 是否置顶

## 注意事项

- 不逐个打开作品详情页获取发布时间。
- 详情页里曾观察到过不可信的固定日期字段，不作为发布时间来源。
- 主页卡片可见数字已确认对应 `statistics.digg_count`，即点赞数。
- 测试流程可只写入少量样本作品；正式初始化再做分页全量抓取。

## 已验证：从现有 Chrome 页面定位完整签名请求

2026-07-16 在已登录的 Chrome 中重新验证了下列方法。该方法不需要手动拼接 `a_bogus`、`msToken` 或 `x-secsdk-web-signature`，也不需要逐个打开作品详情页。

1. 在已登录 Chrome 中打开目标达人主页。
2. 连接目标标签页后执行页面重载，等待“作品 N”标签出现，确认作品列表已加载。
3. 读取当前页面已观测到的资源清单（Chrome 控制器的 `pageAssets.list()`）。
4. 在资源 URL 中精确筛选 `/aweme/v1/web/aweme/post/`。
5. 使用资源清单返回的完整 URL，不要自行重签名或改变参数顺序。

已确认完整请求 URL 包含：

- `sec_user_id`
- `max_cursor`
- `count`
- `need_time_list=1`
- `msToken`
- `a_bogus`
- `timestamp`
- `x-secsdk-web-signature`

重要细节：

- 资源清单只保留当前标签页已观测到的请求。如果第一次搜索不到 `aweme/post`，先重载达人主页，再重新读取资源清单。
- 签名 URL 属于短期运行产物，只能临时保存在 `runtime/`，不得写入可提交文档，不得提交到 Git。
- 直接在新标签页导航到该 API 可能被 Chrome 客户端拦截。
- 将签名 URL 脱离浏览器会话后用普通 HTTP 客户端重放，已观察到 `HTTP 200` 但 `Content-Length: 0`。这表明仅有签名 URL 不足以获取响应，还必须保持原 Chrome 会话上下文。

## 已验证：Crawlio 捕获接口响应 body

2026-07-16 已在用户已登录的 Chrome 标签页中验证成功。目标主页为：

```text
https://www.douyin.com/user/REPLACE_WITH_SEC_USER_ID?from_tab_name=main
```

成功方法不是把签名 URL 拿到浏览器外重放，也不是在页面里直接 `fetch` 资源表中的 URL；这两种方式都可能返回 HTML。可靠方法是在页面真正触发接口前注入 `fetch`/`XMLHttpRequest` 响应体钩子，然后让抖音前端自己重新请求 `/aweme/v1/web/aweme/post/`。

操作顺序：

1. 用 Crawlio `list_tabs` 找到目标 Chrome 标签页。
2. 用 `connect_tab` 连接目标标签页。
3. 用 `browser_snapshot` 确认页面显示 `知了-千川推商品` 和 `作品 43`。
4. 用 `browser_evaluate` 在页面上下文注入钩子：
   - 初始化 `window.__capturedAwemePostBodies = []`。
   - 包装 `window.fetch`，命中 `/aweme/v1/web/aweme/post/` 时对 `Response.clone().text()` 保存响应体。
   - 包装 `XMLHttpRequest.prototype.open/send`，命中 `/aweme/v1/web/aweme/post/` 时在 `load` 事件中保存 `responseText`。
5. 触发页面重新请求作品列表。实测可通过切换主页上方 `推荐` 标签，再回到作品区域，或刷新有效的 `?from_tab_name=main` 标签页触发。
6. 用 `browser_evaluate` 读取并解析 `window.__capturedAwemePostBodies`。
7. 校验每页 JSON 包含 `aweme_list`、`has_more`、`max_cursor`，按 `aweme_id` 去重。

本次抓取结果：

- 共捕获 3 个 `/aweme/v1/web/aweme/post/` XHR 响应。
- 分页数量为 `18 + 18 + 7`。
- 去重后当前可见作品数为 `43`。
- 三页 `has_more` 依次为 `1, 1, 0`。
- 43 条作品的核心字段均完整：`aweme_id`、`create_time`、`statistics.digg_count`、`statistics.comment_count`、`statistics.collect_count`、`statistics.share_count`。

核心字段映射：

```text
aweme_id -> 抖音作品ID
desc -> 原始文案 / 作品标题 / 话题标签
create_time -> 发布时间（Unix 秒，写入前转北京时间）
statistics.digg_count -> 点赞数
statistics.comment_count -> 评论数
statistics.collect_count -> 收藏数
statistics.share_count -> 分享数
video.duration -> 视频时长（毫秒；若飞书表暂无该字段则不写）
video.cover.url_list[0] -> 封面图URL
is_top -> 是否置顶
```

注意事项：

- `performance.getEntriesByType("resource")` 可以拿到完整签名请求 URL，但只能作为定位线索；不要把其中的 `msToken`、`a_bogus`、`x-secsdk-web-signature` 写入可提交文档。
- Crawlio `start_network_capture` 能确认接口请求出现，但本环境中 `get_response_body` 对该 URL 返回过插件内部错误；实际成功路径是页面内 XHR/fetch 钩子。
- 直接在页面上下文 `fetch(签名 URL, { credentials: "include" })` 返回过 HTML，说明原始请求还依赖前端 XHR/SDK 上下文或请求头，不应作为正式方案。
- 原始响应和签名 URL 只能放在 `runtime/`，不得提交。
- 写入飞书前必须先校验核心字段完整；任一当前作品缺少发布时间或互动数据，都不能标记为已完成。
