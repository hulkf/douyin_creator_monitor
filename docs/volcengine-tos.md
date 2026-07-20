# Volcengine TOS Temporary Audio Storage

本项目使用火山引擎 TOS 作为火山 ASR 2.0 的临时音频存储层：

```text
本地音频 -> 上传 TOS -> 生成临时预签名 URL -> 火山 ASR 拉取 URL -> 删除 TOS 对象
```

## 控制台准备

需要在火山引擎控制台准备：

- 一个 TOS Bucket。
- Bucket 所在 Region。
- TOS Endpoint。
- 一个具备该 Bucket `PutObject`、`GetObject`、`DeleteObject` 权限的 Access Key。

Bucket 可以保持私有。脚本会生成短期预签名 GET URL 给 ASR 使用。

## 本地配置

真实配置放在被 Git 忽略的文件：

```text
douyin_creator_monitor/local/volcengine.env.json
```

需要增加这些字段：

```json
{
  "VOLC_TOS_ACCESS_KEY_ID": "YOUR_TOS_ACCESS_KEY_ID",
  "VOLC_TOS_SECRET_ACCESS_KEY": "YOUR_TOS_SECRET_ACCESS_KEY",
  "VOLC_TOS_REGION": "cn-beijing",
  "VOLC_TOS_ENDPOINT": "https://tos-cn-beijing.volces.com",
  "VOLC_TOS_BUCKET": "YOUR_BUCKET_NAME",
  "VOLC_TOS_PREFIX": "douyin-asr/tmp",
  "VOLC_TOS_PRESIGN_EXPIRES": "3600"
}
```

`VOLC_TOS_PREFIX` 和 `VOLC_TOS_PRESIGN_EXPIRES` 可选。

## 依赖

```powershell
python -m pip install tos==2.9.2
```

## 单独测试上传

```powershell
python douyin_creator_monitor/scripts/volcengine_tos.py `
  douyin_creator_monitor/runtime/media/7661534736686337321.16k.wav `
  --json-output douyin_creator_monitor/runtime/media/tos-upload-test.json
```

命令会输出一个临时可下载 URL。

## 上传并转写

```powershell
python douyin_creator_monitor/scripts/transcribe_tos_audio_file.py `
  douyin_creator_monitor/runtime/media/7661534736686337321.16k.wav `
  --json-output douyin_creator_monitor/runtime/media/7661534736686337321.volc-tos.json `
  --text-output douyin_creator_monitor/runtime/media/7661534736686337321.volc-tos.txt `
  --upload-json-output douyin_creator_monitor/runtime/media/7661534736686337321.tos-upload.json
```

默认会在 ASR 完成后删除 TOS 临时对象。调试时可以加 `--keep-object` 保留对象。
