#!/usr/bin/env python3
"""Upload local audio to Volcengine TOS and print a temporary download URL.

Credentials are read from environment variables or the ignored local config
file `douyin_creator_monitor/local/volcengine.env.json`:

- VOLC_TOS_ACCESS_KEY_ID
- VOLC_TOS_SECRET_ACCESS_KEY
- VOLC_TOS_REGION
- VOLC_TOS_ENDPOINT
- VOLC_TOS_BUCKET
- VOLC_TOS_PREFIX, optional
- VOLC_TOS_PRESIGN_EXPIRES, optional, seconds
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import uuid
from pathlib import Path


LOCAL_ENV_PATH = Path(__file__).resolve().parents[1] / "local" / "volcengine.env.json"
_LOCAL_ENV_LOADED = False


class TosUploadError(RuntimeError):
    """Raised when TOS upload or URL signing fails."""


def load_local_env() -> None:
    global _LOCAL_ENV_LOADED
    if _LOCAL_ENV_LOADED:
        return
    _LOCAL_ENV_LOADED = True
    if not LOCAL_ENV_PATH.exists():
        return

    payload = json.loads(LOCAL_ENV_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Local config must be a JSON object: {LOCAL_ENV_PATH}")
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str) and key not in os.environ:
            os.environ[key] = value


def env(name: str) -> str:
    load_local_env()
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str) -> str:
    load_local_env()
    return os.environ.get(name, "").strip()


def import_tos():
    try:
        import tos  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("Missing TOS SDK. Install it with: python -m pip install tos==2.9.2") from exc
    return tos


def make_client():
    tos = import_tos()
    return tos.TosClientV2(
        ak=env("VOLC_TOS_ACCESS_KEY_ID"),
        sk=env("VOLC_TOS_SECRET_ACCESS_KEY"),
        endpoint=env("VOLC_TOS_ENDPOINT"),
        region=env("VOLC_TOS_REGION"),
    )


def make_object_key(audio_path: Path, key: str | None = None) -> str:
    if key:
        return key.replace("\\", "/").lstrip("/")
    prefix = optional_env("VOLC_TOS_PREFIX") or "douyin-asr/tmp"
    suffix = audio_path.suffix.lower() or ".wav"
    return f"{prefix.strip('/')}/{uuid.uuid4().hex}{suffix}"


def upload_file(audio_path: Path, key: str | None = None, expires: int | None = None) -> tuple[str, str]:
    if not audio_path.exists():
        raise TosUploadError(f"Audio file does not exist: {audio_path}")

    tos = import_tos()
    client = make_client()
    bucket = env("VOLC_TOS_BUCKET")
    object_key = make_object_key(audio_path, key=key)
    content_type = mimetypes.guess_type(str(audio_path))[0] or "application/octet-stream"

    client.put_object_from_file(
        bucket=bucket,
        key=object_key,
        file_path=str(audio_path),
        content_type=content_type,
    )
    signed = client.pre_signed_url(
        tos.HttpMethodType.Http_Method_Get,
        bucket=bucket,
        key=object_key,
        expires=expires or int(optional_env("VOLC_TOS_PRESIGN_EXPIRES") or "3600"),
    )
    return object_key, signed.signed_url


def delete_object(key: str) -> None:
    client = make_client()
    client.delete_object(bucket=env("VOLC_TOS_BUCKET"), key=key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="Local audio file to upload.")
    parser.add_argument("--key", help="Optional TOS object key.")
    parser.add_argument("--expires", type=int, help="Pre-signed URL expiry in seconds.")
    parser.add_argument("--json-output", help="Optional path for upload metadata.")
    args = parser.parse_args()

    try:
        object_key, url = upload_file(Path(args.audio), key=args.key, expires=args.expires)
    except TosUploadError as exc:
        raise SystemExit(str(exc)) from exc

    if args.json_output:
        output = {"key": object_key, "url": url}
        Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
