"""Back up local transcript text files to Tencent IMA knowledge bases.

Credentials are read from environment variables, a local ignored JSON file, or
the standard IMA config files:

- IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
- douyin_creator_monitor/local/ima.env.json
- ~/.config/ima/client_id and ~/.config/ima/api_key
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAPPING_PATH = PROJECT_ROOT / "local" / "ima_creator_mapping.json"
DEFAULT_LOCAL_ENV_PATH = PROJECT_ROOT / "local" / "ima.env.json"
DEFAULT_COS_UPLOAD_SCRIPT = Path.home() / ".agents" / "skills" / "ima-skill" / "knowledge-base" / "scripts" / "cos-upload.cjs"
IMA_BASE_URL = "https://ima.qq.com"
TXT_MEDIA_TYPE = 13
TXT_SIZE_LIMIT = 10 * 1024 * 1024


class ImaBackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImaCredentials:
    client_id: str
    api_key: str


@dataclass(frozen=True)
class CreatorTarget:
    creator_name: str
    knowledge_base_id: str
    knowledge_base_name: str
    folder_id: str = ""
    folder_name: str = ""
    folder_created: bool = False


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ImaBackupError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ImaBackupError(f"配置文件不是合法 JSON: {path}") from exc


def load_credentials(local_env_path: Path = DEFAULT_LOCAL_ENV_PATH) -> ImaCredentials:
    client_id = os.environ.get("IMA_OPENAPI_CLIENTID", "").strip()
    api_key = os.environ.get("IMA_OPENAPI_APIKEY", "").strip()

    if (not client_id or not api_key) and local_env_path.exists():
        data = load_json(local_env_path)
        client_id = client_id or str(data.get("IMA_OPENAPI_CLIENTID") or data.get("client_id") or "").strip()
        api_key = api_key or str(data.get("IMA_OPENAPI_APIKEY") or data.get("api_key") or "").strip()

    config_dir = Path.home() / ".config" / "ima"
    if not client_id:
        client_file = config_dir / "client_id"
        if client_file.exists():
            try:
                client_id = client_file.read_text(encoding="utf-8").strip()
            except OSError:
                client_id = ""
    if not api_key:
        api_key_file = config_dir / "api_key"
        if api_key_file.exists():
            try:
                api_key = api_key_file.read_text(encoding="utf-8").strip()
            except OSError:
                api_key = ""

    if not client_id or not api_key:
        raise ImaBackupError(
            "缺少 IMA 凭证。请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY，"
            "或在 douyin_creator_monitor/local/ima.env.json 中配置。"
        )
    return ImaCredentials(client_id=client_id, api_key=api_key)


def ima_api(credentials: ImaCredentials, path: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        f"{IMA_BASE_URL}/{path}",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "ima-openapi-clientid": credentials.client_id,
            "ima-openapi-apikey": credentials.api_key,
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ImaBackupError(f"IMA 请求失败 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ImaBackupError(f"IMA 网络请求失败: {exc.reason}") from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImaBackupError(f"IMA 返回了非 JSON 响应: {raw[:300]}") from exc

    if "retcode" in result:
        success = result.get("retcode") == 0
        error_message = result.get("errmsg")
    else:
        success = result.get("code") == 0
        error_message = result.get("msg")
    if not success:
        raise ImaBackupError(str(error_message or result))
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def get_target(mapping_path: Path, creator_name: str) -> CreatorTarget:
    data = load_json(mapping_path)
    default = data.get("default") if isinstance(data.get("default"), dict) else {}
    creators = data.get("creators")
    if not isinstance(creators, list):
        creators = []

    selected: dict[str, Any] | None = None
    normalized = creator_name.casefold()
    for item in creators:
        if not isinstance(item, dict):
            continue
        names = [str(item.get("creator_name", ""))]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            names.extend(str(alias) for alias in aliases)
        if any(name.casefold() == normalized for name in names if name):
            selected = item
            break

    merged = {**default, **(selected or {})}
    kb_id = str(merged.get("knowledge_base_id") or "").strip()
    if not kb_id or kb_id == "YOUR_IMA_KNOWLEDGE_BASE_ID":
        raise ImaBackupError(f"没有找到博主「{creator_name}」对应的有效 IMA 知识库 ID。")

    return CreatorTarget(
        creator_name=str(merged.get("creator_name") or creator_name),
        knowledge_base_id=kb_id,
        knowledge_base_name=str(merged.get("knowledge_base_name") or kb_id),
        folder_id=str(merged.get("folder_id") or "").strip(),
        folder_name=str(merged.get("folder_name") or "").strip(),
    )


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def folder_id_from_item(item: dict[str, Any]) -> str:
    value = str(item.get("folder_id") or item.get("media_id") or "").strip()
    return value if value.startswith("folder_") else ""


def find_exact_folder(credentials: ImaCredentials, target: CreatorTarget) -> str:
    matches: list[str] = []
    cursor = ""
    seen_cursors: set[str] = set()
    while cursor not in seen_cursors:
        seen_cursors.add(cursor)
        data = ima_api(
            credentials,
            "openapi/wiki/v1/search_knowledge",
            {"query": target.folder_name, "knowledge_base_id": target.knowledge_base_id, "cursor": cursor},
        )
        for item in data.get("info_list") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("title") or "").strip()
            folder_id = folder_id_from_item(item)
            if name == target.folder_name and folder_id:
                matches.append(folder_id)
        if data.get("is_end", True):
            break
        cursor = str(data.get("next_cursor") or "")
        if not cursor:
            break
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise ImaBackupError(
            f"IMA 知识库中存在多个同名文件夹「{target.folder_name}」，请人工清理后再运行。"
        )
    return unique[0] if unique else ""


def persist_target_mapping(mapping_path: Path, target: CreatorTarget) -> None:
    data = load_json(mapping_path)
    creators = data.get("creators")
    if not isinstance(creators, list):
        creators = []
        data["creators"] = creators
    normalized = target.creator_name.casefold()
    selected: dict[str, Any] | None = None
    for item in creators:
        if not isinstance(item, dict):
            continue
        names = [str(item.get("creator_name") or "")]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            names.extend(str(alias) for alias in aliases)
        if any(name.casefold() == normalized for name in names if name):
            selected = item
            break
    if selected is None:
        selected = {"creator_name": target.creator_name}
        creators.append(selected)
    selected.update(
        {
            "knowledge_base_id": target.knowledge_base_id,
            "knowledge_base_name": target.knowledge_base_name,
            "folder_id": target.folder_id,
            "folder_name": target.folder_name,
        }
    )
    write_json_atomic(mapping_path, data)


def ensure_creator_folder(
    credentials: ImaCredentials,
    mapping_path: Path,
    creator_name: str,
) -> CreatorTarget:
    target = get_target(mapping_path, creator_name)
    folder_name = target.folder_name or target.creator_name or creator_name
    if target.folder_id:
        return CreatorTarget(**{**target.__dict__, "folder_name": folder_name})

    searchable = CreatorTarget(**{**target.__dict__, "folder_name": folder_name})
    folder_id = find_exact_folder(credentials, searchable)
    created = False
    if not folder_id:
        data = ima_api(
            credentials,
            "openapi/wiki/v1/create_folder",
            {"knowledge_base_id": target.knowledge_base_id, "name": folder_name},
        )
        folder_id = str(data.get("folder_id") or data.get("media_id") or "").strip()
        if not folder_id.startswith("folder_"):
            raise ImaBackupError(f"IMA 创建文件夹成功但未返回有效 folder_id: {data}")
        created = True

    ensured = CreatorTarget(
        creator_name=target.creator_name,
        knowledge_base_id=target.knowledge_base_id,
        knowledge_base_name=target.knowledge_base_name,
        folder_id=folder_id,
        folder_name=folder_name,
        folder_created=created,
    )
    persist_target_mapping(mapping_path, ensured)
    return ensured


def target_metadata(target: CreatorTarget) -> dict[str, Any]:
    return {
        "platform": "ima",
        "creator_name": target.creator_name,
        "directory": {
            "name": target.folder_name,
            "id": target.folder_id,
            "path": f"{target.knowledge_base_name}/{target.folder_name}",
            "created": target.folder_created,
        },
        "feishu_fields": {
            "IMA知识库名称": target.knowledge_base_name,
            "IMA知识库ID": target.knowledge_base_id,
            "IMA文件夹名称": target.folder_name,
            "IMA文件夹ID": target.folder_id,
            "IMA同步状态": "已映射",
        },
    }


def write_metadata(path_value: str | None, target: CreatorTarget) -> None:
    if path_value:
        write_json_atomic(Path(path_value), target_metadata(target))


def preflight_txt(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        raise ImaBackupError(f"文件不存在: {file_path}")
    if not file_path.is_file():
        raise ImaBackupError(f"不是文件: {file_path}")
    if file_path.suffix.lower() != ".txt":
        raise ImaBackupError(f"当前备份脚本只处理 .txt 文案文件: {file_path.name}")
    file_size = file_path.stat().st_size
    if file_size > TXT_SIZE_LIMIT:
        raise ImaBackupError(f"TXT 文件超过 10MB 限制: {file_path.name}")
    return {
        "file_name": file_path.name,
        "file_ext": "txt",
        "file_size": file_size,
        "media_type": TXT_MEDIA_TYPE,
        "content_type": mimetypes.guess_type(file_path.name)[0] or "text/plain",
    }


def timestamped_name(file_name: str) -> str:
    path = Path(file_name)
    stamp = time.strftime("%Y%m%d%H%M%S")
    return f"{path.stem}_{stamp}{path.suffix}"


def check_repeated_name(credentials: ImaCredentials, target: CreatorTarget, file_name: str) -> bool:
    body: dict[str, Any] = {
        "params": [{"name": file_name, "media_type": TXT_MEDIA_TYPE}],
        "knowledge_base_id": target.knowledge_base_id,
    }
    if target.folder_id:
        body["folder_id"] = target.folder_id

    data = ima_api(credentials, "openapi/wiki/v1/check_repeated_names", body)
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return False
    first = results[0]
    return bool(isinstance(first, dict) and first.get("is_repeated"))


def hmac_sha1_hex(key: bytes | str, data: str) -> str:
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    return hmac.new(key_bytes, data.encode("utf-8"), hashlib.sha1).hexdigest()


def sha1_hex(data: str) -> str:
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def quote_header_value(value: str) -> str:
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    return "".join(ch if ch in safe else f"%{ord(ch):02X}" for ch in value)


def build_cos_authorization(
    *,
    secret_id: str,
    secret_key: str,
    method: str,
    pathname: str,
    headers: dict[str, str],
    start_time: int,
    expired_time: int,
) -> str:
    key_time = f"{start_time};{expired_time}"
    sign_key = hmac_sha1_hex(secret_key, key_time)
    header_keys = sorted(key.lower() for key in headers)
    http_headers = "&".join(f"{key}={quote_header_value(headers[key])}" for key in header_keys)
    http_string = f"{method.lower()}\n{pathname}\n\n{http_headers}\n"
    string_to_sign = f"sha1\n{key_time}\n{sha1_hex(http_string)}\n"
    signature = hmac_sha1_hex(sign_key, string_to_sign)
    header_list = ";".join(header_keys)
    return "&".join(
        [
            "q-sign-algorithm=sha1",
            f"q-ak={secret_id}",
            f"q-sign-time={key_time}",
            f"q-key-time={key_time}",
            f"q-header-list={header_list}",
            "q-url-param-list=",
            f"q-signature={signature}",
        ]
    )


def upload_to_cos(file_path: Path, credential: dict[str, Any], content_type: str) -> None:
    required = ["secret_id", "secret_key", "token", "bucket_name", "region", "cos_key"]
    missing = [field for field in required if not credential.get(field)]
    if missing:
        raise ImaBackupError(f"IMA create_media 返回的 COS 凭证缺少字段: {', '.join(missing)}")

    file_content = file_path.read_bytes()
    bucket = str(credential["bucket_name"])
    region = str(credential["region"])
    cos_key = str(credential["cos_key"])
    hostname = f"{bucket}.cos.{region}.myqcloud.com"
    pathname = "/" + cos_key.lstrip("/")
    start_time = int(credential.get("start_time") or int(time.time()))
    expired_time = int(credential.get("expired_time") or int(time.time()) + 3600)
    sign_headers = {"content-length": str(len(file_content)), "host": hostname}
    authorization = build_cos_authorization(
        secret_id=str(credential["secret_id"]),
        secret_key=str(credential["secret_key"]),
        method="PUT",
        pathname=pathname,
        headers=sign_headers,
        start_time=start_time,
        expired_time=expired_time,
    )
    request = Request(
        f"https://{hostname}{pathname}",
        data=file_content,
        method="PUT",
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(file_content)),
            "Authorization": authorization,
            "x-cos-security-token": str(credential["token"]),
        },
    )
    try:
        with urlopen(request, timeout=120) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ImaBackupError(f"COS 上传失败 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        if DEFAULT_COS_UPLOAD_SCRIPT.exists():
            run_node_cos_upload(file_path, credential, content_type)
            return
        raise ImaBackupError(f"COS 上传失败: {exc.reason}") from exc


def run_node_cos_upload(file_path: Path, credential: dict[str, Any], content_type: str) -> None:
    command = [
        "node",
        str(DEFAULT_COS_UPLOAD_SCRIPT),
        "--file",
        str(file_path),
        "--secret-id",
        str(credential["secret_id"]),
        "--secret-key",
        str(credential["secret_key"]),
        "--token",
        str(credential["token"]),
        "--bucket",
        str(credential["bucket_name"]),
        "--region",
        str(credential["region"]),
        "--cos-key",
        str(credential["cos_key"]),
        "--content-type",
        content_type,
        "--start-time",
        str(credential.get("start_time") or int(time.time())),
        "--expired-time",
        str(credential.get("expired_time") or int(time.time()) + 3600),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ImaBackupError(f"COS 上传失败: {detail}")


def upload_file(credentials: ImaCredentials, target: CreatorTarget, file_path: Path, on_duplicate: str) -> str | None:
    meta = preflight_txt(file_path)
    upload_name = str(meta["file_name"])
    if check_repeated_name(credentials, target, upload_name):
        if on_duplicate == "skip":
            return None
        if on_duplicate == "fail":
            raise ImaBackupError(f"IMA 中已存在同名文件: {upload_name}")
        upload_name = timestamped_name(upload_name)

    create_data = ima_api(
        credentials,
        "openapi/wiki/v1/create_media",
        {
            "file_name": upload_name,
            "file_size": meta["file_size"],
            "content_type": meta["content_type"],
            "knowledge_base_id": target.knowledge_base_id,
            "file_ext": meta["file_ext"],
        },
    )
    media_id = str(create_data.get("media_id") or "")
    credential = create_data.get("cos_credential")
    if not media_id or not isinstance(credential, dict):
        raise ImaBackupError("IMA create_media 返回缺少 media_id 或 cos_credential。")

    upload_to_cos(file_path, credential, str(meta["content_type"]))

    body: dict[str, Any] = {
        "media_type": meta["media_type"],
        "media_id": media_id,
        "title": upload_name,
        "knowledge_base_id": target.knowledge_base_id,
        "file_info": {
            "cos_key": credential["cos_key"],
            "file_size": meta["file_size"],
            "file_name": upload_name,
        },
    }
    if target.folder_id:
        body["folder_id"] = target.folder_id
    ima_api(credentials, "openapi/wiki/v1/add_knowledge", body)
    return upload_name


def iter_files(input_dir: Path, pattern: str) -> list[Path]:
    if not input_dir.exists() or not input_dir.is_dir():
        raise ImaBackupError(f"目录不存在: {input_dir}")
    return sorted(path for path in input_dir.rglob("*") if path.is_file() and fnmatch.fnmatch(path.name, pattern))


def list_knowledge_bases(credentials: ImaCredentials) -> None:
    data = ima_api(credentials, "openapi/wiki/v1/search_knowledge_base", {"query": "", "cursor": "", "limit": 20})
    items = data.get("info_list") or data.get("knowledge_base_list") or []
    if not isinstance(items, list) or not items:
        print("没有查到可见的 IMA 知识库。")
        return
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("kb_name") or ""
            kb_id = item.get("id") or item.get("kb_id") or ""
            print(f"{name}\t{kb_id}")


def search_folder(credentials: ImaCredentials, knowledge_base_id: str, query: str) -> None:
    data = ima_api(
        credentials,
        "openapi/wiki/v1/search_knowledge",
        {"query": query, "knowledge_base_id": knowledge_base_id, "cursor": ""},
    )
    items = data.get("info_list") or []
    found = False
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name") or ""
        folder_id = item.get("folder_id") or item.get("media_id") or ""
        if str(folder_id).startswith("folder_"):
            found = True
            print(f"{title}\t{folder_id}")
    if not found:
        print("没有查到匹配的文件夹。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up Douyin transcript TXT files to Tencent IMA.")
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING_PATH), help="Path to creator to IMA mapping JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-kbs", help="List visible IMA knowledge bases.")
    list_parser.set_defaults(func=handle_list_kbs)

    folder_parser = subparsers.add_parser("search-folder", help="Search folder names in an IMA knowledge base.")
    folder_parser.add_argument("--knowledge-base-id", required=True)
    folder_parser.add_argument("--query", required=True)
    folder_parser.set_defaults(func=handle_search_folder)

    upload_parser = subparsers.add_parser("upload", help="Upload one TXT transcript.")
    upload_parser.add_argument("--creator-name", required=True)
    upload_parser.add_argument("--file", required=True)
    upload_parser.add_argument("--on-duplicate", choices=["timestamp", "skip", "fail"], default="timestamp")
    upload_parser.add_argument("--metadata-output", help="Write ensured IMA folder metadata as JSON.")
    upload_parser.set_defaults(func=handle_upload)

    upload_dir_parser = subparsers.add_parser("upload-dir", help="Upload TXT transcripts under a directory.")
    upload_dir_parser.add_argument("--creator-name", required=True)
    upload_dir_parser.add_argument("--input-dir", required=True)
    upload_dir_parser.add_argument("--pattern", default="*.txt")
    upload_dir_parser.add_argument("--on-duplicate", choices=["timestamp", "skip", "fail"], default="timestamp")
    upload_dir_parser.add_argument("--metadata-output", help="Write ensured IMA folder metadata as JSON.")
    upload_dir_parser.set_defaults(func=handle_upload_dir)

    ensure_parser = subparsers.add_parser("ensure-folder", help="Find or create one creator folder in IMA.")
    ensure_parser.add_argument("--creator-name", required=True)
    ensure_parser.add_argument("--metadata-output", required=True)
    ensure_parser.set_defaults(func=handle_ensure_folder)
    return parser


def handle_list_kbs(args: argparse.Namespace) -> None:
    list_knowledge_bases(load_credentials())


def handle_search_folder(args: argparse.Namespace) -> None:
    search_folder(load_credentials(), args.knowledge_base_id, args.query)


def handle_ensure_folder(args: argparse.Namespace) -> None:
    target = ensure_creator_folder(load_credentials(), Path(args.mapping), args.creator_name)
    write_metadata(args.metadata_output, target)
    action = "已创建" if target.folder_created else "已定位"
    print(f"{action} IMA 达人文件夹: {target.knowledge_base_name}/{target.folder_name}")


def handle_upload(args: argparse.Namespace) -> None:
    credentials = load_credentials()
    target = ensure_creator_folder(credentials, Path(args.mapping), args.creator_name)
    write_metadata(args.metadata_output, target)
    uploaded = upload_file(credentials, target, Path(args.file), args.on_duplicate)
    if uploaded is None:
        print(f"已跳过同名文件: {Path(args.file).name}")
        return
    location = target.knowledge_base_name
    if target.folder_name:
        location = f"{location}/{target.folder_name}"
    print(f"已备份到 IMA: {uploaded} -> {location}")


def handle_upload_dir(args: argparse.Namespace) -> None:
    credentials = load_credentials()
    target = ensure_creator_folder(credentials, Path(args.mapping), args.creator_name)
    write_metadata(args.metadata_output, target)
    files = iter_files(Path(args.input_dir), args.pattern)
    if not files:
        print("没有找到需要上传的 TXT 文件。")
        return

    uploaded_count = 0
    skipped_count = 0
    for file_path in files:
        uploaded = upload_file(credentials, target, file_path, args.on_duplicate)
        if uploaded is None:
            skipped_count += 1
            continue
        uploaded_count += 1
        print(f"已备份: {uploaded}")
    print(f"完成: 上传 {uploaded_count} 个，跳过 {skipped_count} 个。")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ImaBackupError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
