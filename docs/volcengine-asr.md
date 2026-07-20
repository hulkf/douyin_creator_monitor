# Volcengine ASR

本项目默认使用火山引擎豆包录音文件识别模型 2.0 作为视频转文案的主识别服务。

## 控制台位置

开通页面：

```text
豆包语音 -> 新版 -> 系统管理 -> 开通管理 -> 录音文件识别2.0
```

API Key 页面：

```text
豆包语音 -> 新版 -> 系统管理 -> API Key管理
```

## 本地配置

真实密钥只放在被 Git 忽略的本地文件：

```text
douyin_creator_monitor/local/volcengine.env.json
```

需要的新版字段：

```json
{
  "VOLC_ASR_API_KEY": "YOUR_VOLCENGINE_API_KEY"
}
```

旧版 `VOLC_ASR_APP_ID`、`VOLC_ASR_ACCESS_TOKEN`、`VOLC_ASR_CLUSTER` 可以保留作兼容测试，但录音文件识别 2.0 优先使用 `VOLC_ASR_API_KEY`。

## 接口参数

新版录音文件识别 2.0 HTTP 标准版使用两阶段接口：

- Submit: `https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit`
- Query: `https://openspeech.bytedance.com/api/v3/auc/bigmodel/query`
- Resource ID: `volc.seedasr.auc`
- Auth header: `X-Api-Key`

请求体只接收公网可访问的 `audio.url`。如果抖音音频链接火山端无法直接读取，兜底流程是：

1. 临时下载音频。
2. 用 FFmpeg 转成 16k 单声道 WAV。
3. 上传到火山 TOS，拿到短期预签名 URL。
4. 再提交给火山 ASR。

TOS 上传与清理见 `docs/volcengine-tos.md`。

## 运行入口

```powershell
python douyin_creator_monitor/scripts/transcribe_douyin_audio_url.py `
  --audio-url "https://example.com/audio.wav" `
  --provider volcengine `
  --mode auto `
  --ffmpeg "C:\path\to\ffmpeg.exe"
```

如果要启用下载后临时上传兜底，需要设置上传命令，命令里用 `{file}` 表示临时 WAV 路径，并把公网 URL 输出到 stdout：

```powershell
$env:VOLC_UPLOAD_COMMAND = "your-upload-command --file {file}"
```
