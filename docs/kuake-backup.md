# Quark Cloud Drive transcript backup

Volcengine ASR `.txt` transcripts can be backed up to Quark Cloud Drive with `scripts/backup_transcripts_to_kuake.py`.

## Local credentials

The Quark CLI uses browser cookie credentials. Store real credentials only in the ignored local file:

```text
douyin_creator_monitor/local/kuake.env.json
```

Use this committed template as reference:

```text
douyin_creator_monitor/config/kuake.env.example.json
```

Full-cookie format:

```json
{
  "kuake_cookie": "PASTE_YOUR_QUARK_COOKIE_HERE",
  "default_backup_dir": "/视频文案备份"
}
```

Minimal `__pus` / `__puus` format:

```json
{
  "kuake_pus": "PASTE___PUS_VALUE_HERE",
  "kuake_puus": "PASTE___PUUS_VALUE_HERE",
  "default_backup_dir": "/视频文案备份"
}
```

## Directory and naming convention

Transcript backups are organized by creator:

```text
/视频文案备份/知了/
/视频文案备份/糯米爸/
/视频文案备份/未来老赵/
```

Each video gets one TXT file. File names use:

```text
日期_视频ID_标题.txt
```

Example:

```text
/视频文案备份/知了/2026-07-18_738123456789_千川推商品复盘.txt
```

Characters that are unsafe in file names, such as `/`, `:`, `*`, `?`, `"`, `<`, `>` and `|`, are automatically replaced with `_`.

## Commands

List root directory:

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_kuake.py list /
```

Ensure a directory exists:

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_kuake.py ensure-dir "/视频文案备份/知了"
```

Upload one TXT file:

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_kuake.py upload `
  --file ".\douyin_creator_monitor\output\transcripts\知了\作品ID.txt" `
  --creator-name "知了" `
  --video-date "2026-07-18" `
  --video-id "作品ID" `
  --title "作品标题" `
  --create-dir
```

Upload all TXT files for one creator:

```powershell
python .\douyin_creator_monitor\scripts\backup_transcripts_to_kuake.py upload-dir `
  --input-dir ".\douyin_creator_monitor\output\transcripts\知了" `
  --creator-name "知了" `
  --create-dir
```

## Verified state

- The `kuake` CLI can read the Quark account.
- The root directory can be listed.
- The root directory contains `/视频文案备份` and several creator-like folders.

Cookie values are sensitive login state. Do not commit them or write them to logs.
