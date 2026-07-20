#!/usr/bin/env python3
"""Call Volcengine ASR with a URL or local audio file.

Credentials are read from environment variables or the ignored local config
file `douyin_creator_monitor/local/volcengine.env.json`:

- VOLC_ASR_APP_ID
- VOLC_ASR_ACCESS_TOKEN
- VOLC_ASR_CLUSTER
- VOLC_ASR_ENDPOINT, optional
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v1/asr"
V3_SUBMIT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
V3_QUERY_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
V3_RESOURCE_ID = "volc.seedasr.auc"
LOCAL_ENV_PATH = Path(__file__).resolve().parents[1] / "local" / "volcengine.env.json"
DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "config" / "domain_glossary.json"
SUCCESS_CODES = {0, 1000, 20000000}
PENDING_CODES = {20000001, 20000002}
_LOCAL_ENV_LOADED = False


class AsrRequestError(RuntimeError):
    """Raised when Volcengine returns an HTTP or ASR-level failure."""


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


def build_base_payload(app_id: str, token: str, cluster: str) -> dict:
    return {
        "app": {
            "appid": app_id,
            "token": token,
            "cluster": cluster,
        },
        "user": {
            "uid": "douyin_creator_monitor",
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "sequence": 1,
            "nbest": 1,
            "workflow": "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate",
            "show_utterances": True,
            "result_type": "single",
        },
    }


def infer_url_format(audio_url: str) -> str:
    suffix = Path(urlparse(audio_url).path).suffix.lower().lstrip(".")
    return suffix or "m4a"


def load_glossary_context(glossary_path: Path | None = None) -> str:
    path = glossary_path or Path(optional_env("DOUYIN_GLOSSARY_PATH") or DEFAULT_GLOSSARY_PATH)
    if not path.exists():
        return ""

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    domains = payload.get("domains", []) if isinstance(payload, dict) else []
    hotwords: list[str] = []
    for domain in domains:
        if not isinstance(domain, dict):
            continue
        for word in domain.get("hotwords", []):
            if isinstance(word, str) and word.strip() and word.strip() not in hotwords:
                hotwords.append(word.strip())

    max_words = int(optional_env("DOUYIN_ASR_CONTEXT_WORD_LIMIT") or "160")
    selected = hotwords[:max_words]
    if not selected:
        return ""
    return json.dumps(
        {
            "context_type": "dialog_ctx",
            "context_data": [
                {
                    "text": (
                        "这是一段短视频行业监控音频，可能涉及抖店投放、巨量千川、"
                        "乘方广告、巨量引擎、AI 产品、AI 模型和行业资讯。"
                    )
                },
                {"text": "优先按以下专业词识别：" + "、".join(selected)},
            ],
        },
        ensure_ascii=False,
    )


def build_file_payload(audio_path: Path, app_id: str, token: str, cluster: str) -> dict:
    payload = build_base_payload(app_id, token, cluster)
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    payload["audio"] = {
        "format": audio_path.suffix.lower().lstrip(".") or "wav",
        "rate": 16000,
        "bits": 16,
        "channel": 1,
        "language": "zh-CN",
        "data": audio_b64,
    }
    return payload


def build_url_payload(audio_url: str, app_id: str, token: str, cluster: str) -> dict:
    payload = build_base_payload(app_id, token, cluster)
    payload["audio"] = {
        "format": infer_url_format(audio_url),
        "language": "zh-CN",
        "url": audio_url,
    }
    return payload


def ensure_success_response(response_text: str) -> None:
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return

    if not isinstance(payload, dict):
        return

    code = payload.get("code", payload.get("err_code", payload.get("status_code")))
    if code is not None:
        try:
            normalized_code = int(code)
        except (TypeError, ValueError):
            normalized_code = None
        if normalized_code is not None and normalized_code not in SUCCESS_CODES:
            message = payload.get("message") or payload.get("msg") or payload.get("error") or response_text
            raise AsrRequestError(f"ASR code {code}: {message}")

    error = payload.get("error")
    if error:
        raise AsrRequestError(str(error))


def post_payload(payload: dict, endpoint: str | None = None) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        endpoint or os.environ.get("VOLC_ASR_ENDPOINT", DEFAULT_ENDPOINT),
        data=body,
        headers={
            "Authorization": f"Bearer; {payload['app']['token']}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=120) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        raise AsrRequestError(f"HTTP {exc.code}: {response_text}") from exc

    ensure_success_response(response_text)
    return response_text


def build_v3_url_payload(audio_url: str) -> dict:
    request_payload = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "show_utterances": True,
    }
    context = load_glossary_context()
    if context:
        request_payload["context"] = context

    return {
        "user": {
            "uid": "douyin_creator_monitor",
        },
        "audio": {
            "format": infer_url_format(audio_url),
            "url": audio_url,
        },
        "request": request_payload,
    }


def v3_headers(api_key: str, task_id: str, sequence: str | None = None) -> dict:
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": optional_env("VOLC_ASR_RESOURCE_ID") or V3_RESOURCE_ID,
        "X-Api-Request-Id": task_id,
        "Content-Type": "application/json; charset=utf-8",
    }
    if sequence is not None:
        headers["X-Api-Sequence"] = sequence
    return headers


def post_v3(endpoint: str, headers: dict, payload: dict) -> tuple[int, str, str]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            status_code = int(response.headers.get("X-Api-Status-Code", response.status))
            message = response.headers.get("X-Api-Message", "")
            response_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        status_code = int(exc.headers.get("X-Api-Status-Code", exc.code))
        message = exc.headers.get("X-Api-Message", "")
        raise AsrRequestError(f"HTTP {exc.code}, ASR code {status_code}: {message or response_text}") from exc
    return status_code, message, response_text


def call_asr_url_v3(audio_url: str, submit_endpoint: str | None = None, query_endpoint: str | None = None) -> str:
    api_key = env("VOLC_ASR_API_KEY")
    task_id = str(uuid.uuid4())
    submit_status, submit_message, _ = post_v3(
        submit_endpoint or optional_env("VOLC_ASR_V3_SUBMIT_ENDPOINT") or V3_SUBMIT_ENDPOINT,
        headers=v3_headers(api_key, task_id, sequence="-1"),
        payload=build_v3_url_payload(audio_url),
    )
    if submit_status != 20000000:
        raise AsrRequestError(f"ASR submit code {submit_status}: {submit_message}")

    query_url = query_endpoint or optional_env("VOLC_ASR_V3_QUERY_ENDPOINT") or V3_QUERY_ENDPOINT
    last_message = ""
    for _ in range(int(optional_env("VOLC_ASR_QUERY_ATTEMPTS") or "60")):
        try:
            status_code, message, response_text = post_v3(
                query_url,
                headers=v3_headers(api_key, task_id),
                payload={},
            )
        except URLError as exc:
            last_message = str(exc)
            time.sleep(float(optional_env("VOLC_ASR_QUERY_INTERVAL_SECONDS") or "2"))
            continue
        last_message = message
        if status_code == 20000000:
            ensure_success_response(response_text)
            return response_text
        if status_code not in PENDING_CODES:
            raise AsrRequestError(f"ASR query code {status_code}: {message or response_text}")
        time.sleep(float(optional_env("VOLC_ASR_QUERY_INTERVAL_SECONDS") or "2"))

    raise AsrRequestError(f"ASR query timed out: {last_message or 'task still pending'}")


def call_asr(audio_path: Path, endpoint: str | None = None) -> str:
    payload = build_file_payload(
        audio_path=audio_path,
        app_id=env("VOLC_ASR_APP_ID"),
        token=env("VOLC_ASR_ACCESS_TOKEN"),
        cluster=env("VOLC_ASR_CLUSTER"),
    )
    return post_payload(payload, endpoint=endpoint)


def call_asr_url(audio_url: str, endpoint: str | None = None) -> str:
    if optional_env("VOLC_ASR_API_KEY"):
        return call_asr_url_v3(audio_url, submit_endpoint=endpoint)

    payload = build_url_payload(
        audio_url=audio_url,
        app_id=env("VOLC_ASR_APP_ID"),
        token=env("VOLC_ASR_ACCESS_TOKEN"),
        cluster=env("VOLC_ASR_CLUSTER"),
    )
    return post_payload(payload, endpoint=endpoint)


def extract_text(response_text: str) -> str:
    """Best-effort text extraction across common ASR response shapes."""
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return response_text.strip()

    candidates = [
        payload.get("text"),
        payload.get("result", {}).get("text") if isinstance(payload.get("result"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    utterances = None
    result = payload.get("result")
    if isinstance(result, dict):
        utterances = result.get("utterances")
    if utterances is None:
        utterances = payload.get("utterances")
    if isinstance(utterances, list):
        parts = [item.get("text", "") for item in utterances if isinstance(item, dict)]
        text = "\n".join(part.strip() for part in parts if part.strip())
        if text:
            return text

    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="Path to WAV/M4A audio.")
    parser.add_argument("--output", help="Where to write the raw ASR JSON.")
    parser.add_argument("--text-output", help="Where to write extracted transcript text.")
    parser.add_argument("--endpoint", default=os.environ.get("VOLC_ASR_ENDPOINT", DEFAULT_ENDPOINT))
    args = parser.parse_args()

    try:
        response_text = call_asr(Path(args.audio), endpoint=args.endpoint)
    except AsrRequestError as exc:
        raise SystemExit(str(exc)) from exc
    transcript = extract_text(response_text)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(response_text, encoding="utf-8")
    if args.text_output:
        Path(args.text_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.text_output).write_text(transcript, encoding="utf-8")
    print(transcript)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
