# Domain Glossary

本项目维护一个可迭代的领域词库，用于减少 ASR 专业词错误，并在识别后做轻量纠错。

词库文件：

```text
douyin_creator_monitor/config/domain_glossary.json
```

当前分为两个方向：

- `douyin_shop_ads`: 抖音抖店推广系列，包含巨量千川、乘方广告、巨量引擎、投流、ROI、素材追投等。
- `ai_media`: AI 自媒体与行业信息，包含大模型、AI Agent、RAG、主流模型和工具名等。

## ASR 前置上下文

`volcengine_asr.py` 会自动读取词库，把专业词作为 ASR `context` 提交给火山录音文件识别 2.0。

可以通过环境变量调整：

```powershell
$env:DOUYIN_GLOSSARY_PATH = "douyin_creator_monitor/config/domain_glossary.json"
$env:DOUYIN_ASR_CONTEXT_WORD_LIMIT = "160"
```

## 识别后纠错

```powershell
python douyin_creator_monitor/scripts/correct_transcript.py `
  douyin_creator_monitor/runtime/media/7661534736686337321.volc-tos.txt `
  --domain douyin_shop_ads `
  --output douyin_creator_monitor/runtime/media/7661534736686337321.corrected.txt `
  --report-output douyin_creator_monitor/runtime/media/7661534736686337321.correction-report.json
```

脚本会：

1. 根据 `replacements` 做确定性替换。
2. 输出纠错后的文案。
3. 生成候选新词报告。

## 词库迭代

每天新增内容进入系统后，建议流程是：

```text
原始 ASR 文案 -> correct_transcript.py -> 候选新词报告 -> 人工确认 -> 加入 hotwords / replacements
```

需要把候选词追加到词库候选区时：

```powershell
python douyin_creator_monitor/scripts/correct_transcript.py `
  douyin_creator_monitor/runtime/media/latest.txt `
  --report-output douyin_creator_monitor/runtime/media/latest-candidates.json `
  --update-candidates
```

候选词会进入 `candidate_terms`，状态为 `pending`。确认后再移动到对应 domain 的 `hotwords` 或 `replacements`。

## 回写飞书作品记录

单独把已经生成的文案写回飞书：

```powershell
python douyin_creator_monitor/scripts/feishu_transcript_writer.py `
  --table-id "<达人作品表ID>" `
  --work-id "<抖音作品ID>" `
  --corrected-transcript-file douyin_creator_monitor/runtime/media/<作品ID>.corrected.txt
```

转写完成后自动回写：

```powershell
python douyin_creator_monitor/scripts/transcribe_tos_audio_file.py `
  douyin_creator_monitor/runtime/media/<作品ID>.16k.wav `
  --corrected-text-output douyin_creator_monitor/runtime/media/<作品ID>.corrected.txt `
  --correction-domain douyin_shop_ads `
  --feishu-table-id "<达人作品表ID>" `
  --feishu-work-id "<抖音作品ID>"
```

飞书默认只写入 `语音转写全文` 字段，内容为词库纠正后的最终文案。

脚本默认读取本地忽略文件 `douyin_creator_monitor/local/feishu-ids.md` 中的 `base_token`，也可以通过 `FEISHU_BASE_TOKEN` 或 `--base-token` 传入。
