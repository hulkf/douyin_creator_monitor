# IMA 文案备份

本项目可以把火山 ASR 得到的本地 `.txt` 文案备份到腾讯 IMA 知识库。建议按“一个博主对应 IMA 中一个文件夹”的方式管理，映射关系放在本地配置里，避免后续自动化时猜错目标。

## 本地文件

文案本地保存建议使用：

```text
douyin_creator_monitor/output/transcripts/<博主名>/<作品ID>.txt
```

`output/` 已被 `.gitignore` 忽略，不会提交到仓库。

## 凭证配置

打开 IMA OpenAPI 页面获取 Client ID 和 API Key，然后二选一配置：

```powershell
$env:IMA_OPENAPI_CLIENTID = "你的 Client ID"
$env:IMA_OPENAPI_APIKEY = "你的 API Key"
```

或写入本地忽略文件：

```json
{
  "IMA_OPENAPI_CLIENTID": "你的 Client ID",
  "IMA_OPENAPI_APIKEY": "你的 API Key"
}
```

保存为：

```text
douyin_creator_monitor/local/ima.env.json
```

## 博主映射

先复制模板：

```powershell
Copy-Item `
  .\douyin_creator_monitor\config\ima_creator_mapping.example.json `
  .\douyin_creator_monitor\local\ima_creator_mapping.json
```

然后把每个博主对应到 IMA 的知识库和文件夹：

```json
{
  "creators": [
    {
      "creator_name": "某个博主",
      "aliases": ["账号昵称"],
      "knowledge_base_name": "抖音达人文案库",
      "knowledge_base_id": "IMA 知识库 ID",
      "folder_name": "某个博主",
      "folder_id": "IMA 文件夹 ID"
    }
  ]
}
```

如果暂时不分文件夹，可以留空 `folder_id`，脚本会上传到知识库根目录。

## 查找 IMA 目标

列出当前账号可见的知识库：

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_ima.py list-kbs
```

在某个知识库里搜索博主文件夹：

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_ima.py search-folder `
  --knowledge-base-id "IMA 知识库 ID" `
  --query "博主名"
```

把查到的知识库和文件夹填回 `local/ima_creator_mapping.json`，这份文件就是“博主是谁、对应 IMA 哪个文档/文件夹”的长期映射表。

## 上传文案

上传单个 TXT：

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_ima.py upload `
  --creator-name "某个博主" `
  --file ".\douyin_creator_monitor\output\transcripts\某个博主\作品ID.txt"
```

上传某个博主目录下所有 TXT：

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_ima.py upload-dir `
  --creator-name "某个博主" `
  --input-dir ".\douyin_creator_monitor\output\transcripts\某个博主"
```

同名文件默认会追加时间戳保留两份；也可以用 `--on-duplicate skip` 跳过，或用 `--on-duplicate fail` 直接报错。
## 与飞书达人基础信息表的关系

飞书「达人基础信息表」是后续自动化的主索引。IMA 的知识库和文件夹信息会回填到达人记录中：

- `IMA知识库名称`
- `IMA知识库ID`
- `IMA文件夹名称`
- `IMA文件夹ID`
- `IMA同步状态`

后续上传文案时，优先从达人基础信息表读取该达人对应的 IMA 目标；`local/ima_creator_mapping.json` 可以作为本地缓存或手动兜底映射。

当前已确认：

- 「知了-千川推商品」对应 IMA 文件夹「知了」
- 「糯米爸(付费流分享)」对应 IMA 文件夹「糯米爸」

新增达人时，流水线会在目标 IMA 知识库中精确查找同名文件夹；不存在时自动创建，并把知识库名称/ID、文件夹名称/ID和同步状态回填到达人基础信息表。同时会更新被 Git 忽略的 `local/ima_creator_mapping.json`，保证后续作品直接复用同一文件夹。

IMA 的 `create_folder` 属于已实测可用但未写入官方公开技能文档的接口。若后续接口发生变化，脚本会明确失败，不会退回知识库根目录上传，以免不同达人文案混放。
