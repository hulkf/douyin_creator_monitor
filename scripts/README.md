# Scripts

这里用于放后续沉淀的可复用脚本。

当前项目还处在流程验证阶段，实际执行主要依赖：

- 本机 MediaCrawler 项目 checkout
- `tools/lark-cli/lark-cli.exe`
- `douyin_creator_monitor/runtime/` 中的 JSON 模板和测试产物

后续建议沉淀的脚本：

- `sync_new_creator.ps1`: 读取达人基础信息表中的新增主页链接，抓取基础信息，新建作品表并写入首批作品。
- `sync_creator_works.ps1`: 对已有达人检查新增作品并追加到对应作品表。
- `extract_post_list.ps1`: 通过浏览器上下文读取抖音主页作品列表接口。该方式现在只作为 MediaCrawler 异常时的排障兜底。
- `transcribe_with_tingwu.ps1`: 下载音频并对接通义听悟网页转写流程。

已开始沉淀的脚本：

- `download_douyin_media.py`: 使用已签名的抖音视频/音频 URL 下载样本，并用 FFmpeg 生成 16k 单声道 WAV。
- `collect_douyin_creator_with_mediacrawler.py`: 调用外部 MediaCrawler 项目框架采集抖音达人作品，默认让 MediaCrawler 输出 JSONL，再规范化为本项目统一作品 JSON。输出默认写入 `douyin_creator_monitor/runtime/zhiliao-works-from-mediacrawler.json`。
- `volcengine_asr.py`: 调用火山引擎 ASR 的本地入口，凭证从环境变量或本地忽略配置读取。
- `bailian_paraformer.py`: 调用阿里云百炼/DashScope Paraformer 的本地入口，凭证从环境变量读取。
- `transcribe_douyin_audio_url.py`: 正式转写入口，接收抖音音频 URL，默认使用火山引擎；百炼 Paraformer 仅作为低成本备选。
- `feishu_transcript_writer.py`: 按 `抖音作品ID` 定位飞书作品表记录，并把词库纠正后的最终文案写回 `语音转写全文` 字段。
- `sync_douyin_works_to_feishu.py`: 读取 MediaCrawler 适配层规范化后的作品 JSON，按 `抖音作品ID` 覆盖更新飞书作品表；只新增和更新，不删除飞书中已存在但本轮未抓到的历史记录。仍可通过 `--works-file` 指定历史 Crawlio 产物做兼容导入。
- `backup_transcripts_to_kuake.py`: 读取本地夸克登录态配置，把火山 ASR 得到的 `.txt` 文案上传到夸克网盘指定目录。

MediaCrawler 采集示例：

```powershell
$env:MEDIACRAWLER_DIR = "D:\path\to\MediaCrawler"
python douyin_creator_monitor\scripts\collect_douyin_creator_with_mediacrawler.py `
  --creator-url "https://www.douyin.com/user/REPLACE_WITH_SEC_USER_ID" `
  --expect-min-count 43 `
  --clean-media-output
```

如果 `D:\path\to\MediaCrawler\.venv\Scripts\python.exe` 存在，脚本会自动使用这个虚拟环境运行 MediaCrawler；也可以用 `MEDIACRAWLER_PYTHON` 或 `--media-crawler-python` 手动指定。

MediaCrawler 的实际原始 JSONL 不是写在输出目录顶层，而是在 `runtime/mediacrawler-output/douyin/jsonl/creator_contents_YYYY-MM-DD.jsonl`。后续排障时要优先检查这个文件；其中点赞/收藏字段可能已经被 MediaCrawler 转成 `liked_count`、`collected_count`，适配脚本会统一规范化回 `digg_count`、`collect_count` 后再同步飞书。

采集完成后写入飞书：

```powershell
$env:FEISHU_BASE_TOKEN = "..."
python douyin_creator_monitor\scripts\sync_douyin_works_to_feishu.py --table-id tbl3V4TExJxJEjC3
```

火山引擎凭证不要写入仓库。建议在本机环境变量或 `douyin_creator_monitor/local/volcengine.env.json` 下维护：

```powershell
$env:VOLC_ASR_APP_ID = "..."
$env:VOLC_ASR_ACCESS_TOKEN = "..."
$env:VOLC_ASR_CLUSTER = "..."
```

火山 AppID/Access Token 已验证需要使用 `Authorization: Bearer; token` 的格式。若返回 `requested resource not granted`，说明当前应用未开通对应语音识别资源，或 `VOLC_ASR_CLUSTER` 与控制台授权资源不匹配。

百炼/DashScope 如果需要保留对比，也可在本机环境变量或 `douyin_creator_monitor/local/bailian.env.json` 下维护：

```powershell
$env:DASHSCOPE_API_KEY = "sk-..."
$env:BAILIAN_ASR_MODEL = "paraformer-v2"
```

脚本会自动读取被 Git 忽略的本地配置文件。

正式流程里不需要保存视频或音频。音频 URL 只作为转写输入：

1. `auto` 模式先把音频 URL 交给火山引擎直接读取。
2. 如果火山读取不了 URL，再兜底走本机临时下载、FFmpeg 转 16k WAV、直接上传给火山 ASR。
3. 如果显式使用 `--provider bailian`，百炼兜底才需要上传到临时对象存储，拿到公网 URL 后再次提交百炼。
4. 临时文件会在脚本结束后自动删除；后续只把文案或摘要写入飞书。

下载兜底需要配置上传命令，命令中用 `{file}` 表示临时 WAV 文件路径，并把公网 URL 输出到 stdout：

```powershell
$env:BAILIAN_UPLOAD_COMMAND = "your-upload-command --file {file}"
```

## IMA 备份脚本

- `backup_transcripts_to_ima.py`: 把本地 `.txt` 文案按“博主 -> IMA 知识库/文件夹”映射备份到腾讯 IMA。真实凭证和映射文件放在 `douyin_creator_monitor/local/`，提交到 Git 的只有模板和说明。
- `sync_creator_backup_mapping_to_feishu.py`: 读取三个备份脚本生成的目录元数据，只把非空的目录名称、ID、路径和同步状态写回飞书达人基础信息表。
