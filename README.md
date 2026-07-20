# Douyin Creator Monitor

这个目录用于管理“抖音达人日常作品监控”项目的脚本、说明和运行产物。

项目目标：

- 每天定时检查一批抖音达人主页是否有新作品。
- 从达人主页列表接口获取作品文案、发布时间、点赞数、评论数、收藏数、分享数等字段。
- 按“一个达人一个作品表”的规则写入飞书多维表格。
- 在达人基础信息表中维护达人资料、最近发稿时间、作品表名称、作品表 ID 和作品表链接。
- 后续再接入通义听悟转写、ima、百度网盘、本地知识库等下游流程。

## 当前状态

当前主链路：

1. 从飞书“达人基础信息表”读取新增达人主页链接。
2. 调用本机 MediaCrawler 项目框架抓取抖音达人作品。
3. 将 MediaCrawler 导出的作品数据规范化为本项目统一 JSON：`runtime/zhiliao-works-from-mediacrawler.json`。
4. 新建该达人专属作品表。
5. 用 `抖音作品ID` 做唯一键写入或覆盖更新作品记录。
6. 回填达人基础信息表中的基础字段和作品表关联字段。

历史上用 Chrome/Crawlio 验证过 `/aweme/v1/web/aweme/post/` 响应捕获方法，这套方法保留在 `docs/data-source.md` 作为排障兜底，不再作为默认采集底层。

## 飞书 Base 信息

飞书 Base token、table ID、view ID、wiki 链接等属于本地私有配置，不写入可提交文档。

本地配置建议放在：

```text
douyin_creator_monitor/local/feishu-ids.md
```

该路径已被 `.gitignore` 忽略，后续推送 GitHub 时不会提交。

## 目录约定

- `runtime/`: MediaCrawler 导出、浏览器抓取、飞书写入测试过程中产生的 JSON、JS、二维码、接口样本等运行产物。
- `docs/`: 项目说明、字段说明、接口观察结论。
- `scripts/`: 后续沉淀的可复用脚本。当前主要流程仍是手动验证和 CLI 命令组合。

从现在开始，这个项目新增的脚本、说明、模板、测试 JSON 和临时产物都放在 `douyin_creator_monitor/` 目录下，不再散落到仓库根目录。

## IMA 文案备份

火山 ASR 得到的 `.txt` 文案可以先保存到本地，再用 `scripts/backup_transcripts_to_ima.py` 备份到腾讯 IMA。博主和 IMA 知识库/文件夹的映射关系放在 `douyin_creator_monitor/local/ima_creator_mapping.json`，模板见 `config/ima_creator_mapping.example.json`。具体步骤见 `docs/ima-backup.md`。

## 夸克网盘文案备份

夸克网盘 CLI 已按本项目约定安装到本地 `tools/kuake-cli/`，真实登录态放在 `douyin_creator_monitor/local/kuake.env.json`。火山 ASR 得到的 `.txt` 文案可用 `scripts/backup_transcripts_to_kuake.py` 上传到指定夸克目录，默认按 `/视频文案备份/博主名/日期_视频ID_标题.txt` 组织。具体步骤见 `docs/kuake-backup.md`。


## 增量抓取与新增作品队列

采集器现在按达人维护一份全量基线标记。首次成功抓取全部历史作品后，状态写入：

~~~text
runtime/pipeline/collection/<达人key>.json
~~~

后续运行会自动切换为增量模式：先按发布时间检查最近至少 3 条作品；一旦遇到已经存在的作品边界，就停止继续翻页。如果最近 3 条全部是新作品，则继续向后检查，直到遇到已知作品；如果始终没有遇到已知作品，则继续抓到历史末尾，避免漏掉大批新增作品。最近检查到的已知作品仍会刷新点赞、评论、收藏和分享数据。

规范化作品文件继续保存完整历史，但流水线下游只选择 `pending_aweme_ids` 中尚未成功处理的新增作品。`--max-works` 只限制本轮处理数量，未处理完的 ID 会继续留在 pending 队列，下一次断点续跑，不会因为限流永久丢失。没有新增作品且 pending 为空时，会直接跳过飞书同步、ASR、IMA、夸克和 Obsidian。

如果已有作品 JSON 已确认包含该达人全部历史，可以不访问网络，直接建立基线标记：

~~~powershell
python .\douyin_creator_monitor\scripts\collect_douyin_creator_with_mediacrawler.py `
  --creator-url "<达人主页或 SecUID>" `
  --output-file .\douyin_creator_monitor\runtime\<达人key>-works-from-mediacrawler.json `
  --collection-state-file .\douyin_creator_monitor\runtime\pipeline\collection\<达人key>.json `
  --mark-existing-full
~~~

需要重新校验全部历史时，可以在完整流水线入口增加 `--force-full-collect`。配置模板中的 `collection.incremental_enabled` 默认是 `true`，`collection.incremental_probe_count` 默认是 `3`。每次 MediaCrawler 原始输出写入独立的 `runtime/mediacrawler-output-<达人key>/runs/<运行ID>/`，避免旧数据与本轮数据混合。

## 完整自动化流水线

统一入口：

~~~text
douyin_creator_monitor/scripts/run_creator_pipeline.py
~~~

它按以下顺序复用现有模块，不复制各平台的底层实现：

1. MediaCrawler 采集并规范化达人作品。
2. 按统一作品表字段契约校验结构，用「抖音作品ID」分页识别已有作品；批量新增、仅更新内容变化的记录，不删除历史记录。
3. 从作品数据读取 music_download_url，调用火山 ASR（或配置的其他 ASR）。
4. 按达人配置的领域词库执行文案纠正。
5. 把纠正后的全文回写飞书对应作品记录。
6. 优先复用 IMA、夸克网盘和 Obsidian 的达人目录映射缓存；缓存缺失、过期或显式刷新时并行确认目录，不存在时自动创建。
7. 合并检查三个平台的目录名称、ID（平台提供时）和路径，一次回写飞书达人基础信息表的差异字段。
8. 在飞书文案回写完成后，受控并行备份到 IMA、夸克网盘和 Obsidian；三个备份阶段失败互不影响。

视频转音频、火山 ASR 和文案纠正按作品受控并发，默认并发数为 4。IMA、夸克和 Obsidian 默认最多 3 路独立备份并发；飞书同表写入保持串行或批量，避免共享记录竞争。每条作品使用独立临时目录、产物文件和状态文件。

### 配置文件

可提交的模板：

~~~text
douyin_creator_monitor/config/pipeline.example.json
~~~

本机实际配置：

~~~text
douyin_creator_monitor/local/pipeline.json
~~~

本地配置已被 .gitignore 忽略，可填写真实的达人主页、飞书作品表 ID、MediaCrawler 路径和本地工具路径。飞书 Base token、IMA 凭证、夸克 Cookie 等仍放在原有环境变量或 local 私有文件中，不要直接写进可提交模板。

`feishu.creator_table_id` 必须配置为达人基础信息表 ID。流水线默认从 `creator_url` 提取 `SecUID` 定位达人记录；特殊达人可以额外设置 `feishu_match_field` 和 `feishu_match_value`。

每个达人至少配置：

- key：命令行选择达人时使用的稳定标识。
- creator_url：抖音达人主页或 SecUID。
- creator_name：飞书、IMA 使用的达人显示名称。
- creator_dir_name：夸克和 Obsidian 使用的目录名称。
- works_table_id：该达人的飞书作品表 ID。
- works_file：规范化作品 JSON 的输出位置。
- profile_file：Obsidian 顶部基础信息所需的达人资料，可选。
- correction_domain：如 douyin_shop_ads 或 ai_media。

全局并发数可在 `asr.max_workers` 中配置。火山账号配额较低或本机需要同时执行 FFmpeg 转码时，可以先设为 2；网络和配额稳定后再逐步提高。命令行 `--asr-workers` 会临时覆盖配置文件。

备份并发数通过 `backups.max_workers` 配置，命令行 `--backup-workers` 可临时覆盖。达人目录映射默认缓存 24 小时，由 `backups.mapping_cache_ttl_hours` 控制；需要立即重新确认远端目录时使用 `--refresh-mappings`。

### 运行命令

先检查单条作品的完整执行计划，不访问外部服务、不写流水线状态：

~~~powershell
python .\douyin_creator_monitor\scripts\run_creator_pipeline.py --creator zhiliao --aweme-id 7661192591962017065 --skip-collect --dry-run
~~~

运行全部已启用达人：

~~~powershell
python .\douyin_creator_monitor\scripts\run_creator_pipeline.py
~~~

只运行一个达人，最多选择最新 3 条：

~~~powershell
python .\douyin_creator_monitor\scripts\run_creator_pipeline.py --creator zhiliao --max-works 3
~~~

临时使用 6 路 ASR 并发：

~~~powershell
python .\douyin_creator_monitor\scripts\run_creator_pipeline.py --creator zhiliao --asr-workers 6
~~~

只规范化已经存在的 MediaCrawler 输出，不重新启动抓取：

~~~powershell
python .\douyin_creator_monitor\scripts\run_creator_pipeline.py --creator zhiliao --normalize-only
~~~

临时关闭某些步骤：

~~~powershell
python .\douyin_creator_monitor\scripts\run_creator_pipeline.py --skip-ima --skip-kuake
~~~

### 断点续跑和幂等

每条作品的阶段状态保存在：

~~~text
douyin_creator_monitor/runtime/pipeline/<达人key>/<作品ID>.json
~~~

单次运行摘要保存在：

~~~text
douyin_creator_monitor/runtime/pipeline/runs/<运行时间>.json
~~~

日志保存在：

~~~text
douyin_creator_monitor/logs/pipeline-YYYYMMDD-HHMMSS.log
~~~

默认会跳过已经成功的逐作品阶段，并复用已有的原始转写和纠正后文案。飞书同步会分页读取全部作品，但只更新内容发生变化的记录；新作品批量创建同时受“每批最多 200 条”和“单批 JSON 最多 20,000 字符”限制，避免 Windows `CreateProcess` 命令行过长，并把返回的 `record_id` 按作品顺序直接传给文案和状态回写。需要强制重跑某一步时使用：

~~~text
--force-stage transcribed
--force-stage corrected
--force-stage obsidian_exported --overwrite
--force-stage all
~~~

IMA 默认使用 on_duplicate=skip，避免同名文案重复上传。目录确认和映射检查属于达人级缓存步骤，不会随作品数量重复检查；缓存刷新时三个平台并行确认，并合并为一次飞书差异写入。夸克和 Obsidian 是否已成功以本地阶段状态为准；状态已成功时不会重复远程写入。

每个外部命令的毫秒级耗时、状态、失败调用数以及整轮墙钟时间都会写入运行摘要的 `timings` 和 `wall_seconds`；单作品阶段状态同时保存 `duration_seconds`，便于持续比较优化效果。

单条作品的转写或某个备份失败时，流水线会保留已经成功的结果，并继续其他备份和后续作品。普通完整流水线只要最终存在任一失败阶段，进程退出码就是 1，便于 Windows 任务计划程序识别失败。`--feishu-only` 只以飞书同步、最终文案回写、备份状态回写和达人目录映射是否成功判断本轮成败；本地 IMA 失败状态仍会如实写入飞书，但不会把“飞书补写任务”误判为执行失败。需要遇错立即停止时增加 `--fail-fast`。

### Windows 任务计划程序入口

程序/脚本填写本机 Python，例如：

~~~text
D:\Anaconda\python.exe
~~~

参数填写：

~~~text
D:\JR_project\douyin_creator_monitor\scripts\run_creator_pipeline.py
~~~

起始于填写：

~~~text
D:\JR_project\douyin_creator_monitor
~~~

建议先手动运行单条作品并确认飞书、IMA、夸克和 Obsidian 均正确，再接入每日调度。


## 历史作品补录与仅新增作品

日常运行默认使用增量采集：首次建立完整历史基线后，后续至少检查最新 3 条，遇到已知作品边界即停止，只把新增作品加入待处理队列。

如果本地作品文件已经存在，但历史作品尚未生成最终文案、尚未完成已启用备份，或飞书尚未完整回写，使用可断点续跑的历史补录模式：

~~~powershell
python .\scripts\run_creator_pipeline.py --creator aligc --skip-collect --backfill-existing --max-works 20
~~~

`--backfill-existing` 每轮会选择缺少非空 `runtime/media/<作品ID>.final.txt`、缺少已启用备份成功状态或缺少飞书同步/回写状态的作品；成功作品下一轮会自动退出选择范围，因此 `--max-works` 可以安全分批，不会永远重复选择最新一批。三位固定博主入口也必须保留飞书基础同步、最终文案、备份状态和达人目录映射回写。

如果本地作品、最终文案和三处备份已经完成，只需要补齐或校验飞书，使用：

~~~powershell
python .\scripts\run_creator_pipeline.py --creator aligc --feishu-only
~~~

`--feishu-only` 跳过采集、ASR、文案纠正和三处重复上传，但会处理该达人本地作品文件中的全部作品，写入作品基础数据、最终文案、IMA/夸克/Obsidian 实际状态，并回写达人备份目录映射。它不会把本地失败状态虚报为成功。

视频没有独立音乐地址时会尝试作品视频地址；图文作品或确实没有可转写音轨的作品会使用作品发布文案生成原始文本，再继续纠正和备份，保证每条作品都有对应的最终文案文件。
## 2026-07 流水线并发与批量交付策略

- 达人目录映射确认与该达人 ASR 同时启动；首条作品进入交付前只等待目录映射完成。
- ASR/纠正文案按配置并发执行，哪条先完成就先进入串行交付消费者；最终摘要仍按原作品顺序输出。
- IMA 和本地知识库在单条作品就绪后立即处理，不再等待该达人全部 ASR 完成。
- 当前达人某个交付目标出现确定的凭证、权限、配置或目录映射永久错误后，仅熔断该达人该目标；剩余作品记录相同失败原因，其他目标及下一达人继续。超时、429、5xx、DNS 和临时连接错误不会熔断。
- 夸克上传按达人生成精确 manifest，一次确认目录并批量上传本轮待处理文案，逐作品返回成功或失败。
- 夸克批次每完成一条就原子保存 checkpoint；子进程意外中断时，父流程只恢复相同 `batch_id` 的已完成结果，未完成项明确记为失败并可精确续跑。
- 飞书最终文案、IMA 状态、夸克状态、本地知识库状态和最后更新时间按达人调用 Base `records/batch_update` 真正批量写回，每批最多 200 条；批次 checkpoint 支持部分成功和失败项精确续跑。
- 夸克和飞书的真实批次墙钟耗时记录在达人级 `phase_timings`；作品级 `duration_seconds` 按批次作品数分摊，同时保留 `batch_duration_seconds`，避免汇总时把同一批次耗时重复放大。
- 达人之间的文案处理仍严格串行：当前达人全部作品成功或明确失败后，才进入下一达人。

## 2026-07 达人并发采集与串行降级

- 信息采集默认使用 3 个并发 worker，可通过 `--collect-workers N` 调整；`--fail-fast` 模式仍保持严格串行和立即停止语义。
- 每个达人使用独立的 MediaCrawler 输出目录、采集状态、运行 bootstrap 和持久化浏览器 profile，避免并发时配置、产物或 Chromium profile 锁互相冲突；首次创建新 profile 时可能需要重新确认登录授权。
- 每个达人使用独立临时 CDP 端口。默认从 `collection.cdp_port_start=9222` 开始，按配置中的达人顺序以 `collection.cdp_port_stride=10` 递增；例如前三位达人使用 `9222/9232/9242`。也可在达人配置中用 `browser_profile_key` 和 `cdp_port` 单独覆盖。
- 浏览器和 CDP 端口只在该达人采集期间占用；采集进程退出后自动释放。串行补采复用该达人原有 profile 和端口，不创建新的登录环境。
- 首轮并发采集失败的达人会在其他并发任务结束后逐个串行补采一次；成功达人不会重复采集。
- 采集成功的达人立即进入单消费者文案队列，达人之间的文案处理仍严格串行，同一达人内部 ASR 仍按配置并发。
- 运行摘要记录 `collection_attempts`、`fallback_to_serial`、`parallel_collection_seconds` 和可选的 `serial_retry_seconds`。
- 2026-07-20 真实三达人并发验证 `run_id=20260720-121425`：首轮 3/3 成功，均为 `collection_attempts=1`、`fallback_to_serial=false`；墙钟耗时 `105.795` 秒，相比隔离前基准 `725.965` 秒减少约 `85.43%`。本轮没有新增作品，因此未触发 ASR 和交付阶段。
