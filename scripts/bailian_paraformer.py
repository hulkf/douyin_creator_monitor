#!/usr/bin/env python3
"""Call Alibaba Cloud Bailian/DashScope Paraformer transcription.

Credentials are read from environment variables so secrets are not committed:

- DASHSCOPE_API_KEY
- BAILIAN_ASR_MODEL, optional, defaults to paraformer-v2
- BAILIAN_ASR_ENDPOINT, optional
- BAILIAN_TASK_ENDPOINT, optional

Paraformer transcription accepts audio URLs. The caller is responsible for
providing a URL that DashScope can read from Alibaba Cloud servers.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DEFAULT_MODEL = "paraformer-v2"
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
DEFAULT_TASK_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/tasks"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}
LOCAL_ENV_PATH = Path(__file__).resolve().parents[1] / "local" / "bailian.env.json"
_LOCAL_ENV_LOADED = False


class AsrRequestError(RuntimeError):
    """Raised when Bailian/DashScope returns an HTTP or ASR-level failure."""


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


def request_json(url: str, api_key: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if method == "POST":
        headers["X-DashScope-Async"] = "enable"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=120) as response:
            text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise AsrRequestError(f"HTTP {exc.code}: {text}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AsrRequestError(f"Non-JSON response: {text}") from exc


def fetch_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=120) as response:
            text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise AsrRequestError(f"HTTP {exc.code}: {text}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AsrRequestError(f"Non-JSON transcription result: {text}") from exc


def submit_task(audio_url: str, api_key: str, endpoint: str, model: str) -> str:
    payload = {
        "model": model,
        "input": {
            "file_urls": [audio_url],
        },
        "parameters": {
            "language_hints": ["zh"],
        },
    }
    response = request_json(endpoint, api_key=api_key, method="POST", payload=payload)
    output = response.get("output") if isinstance(response, dict) else None
    task_id = output.get("task_id") if isinstance(output, dict) else None
    if not task_id:
        raise AsrRequestError(f"Missing task_id in response: {json.dumps(response, ensure_ascii=False)}")
    return task_id


def wait_task(task_id: str, api_key: str, task_endpoint: str, poll_interval: float, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    url = f"{task_endpoint.rstrip('/')}/{task_id}"
    last_response: dict | None = None

    while time.monotonic() < deadline:
        response = request_json(url, api_key=api_key)
        last_response = response
        output = response.get("output") if isinstance(response, dict) else None
        status = output.get("task_status") if isinstance(output, dict) else None
        if status in TERMINAL_STATUSES:
            if status == "SUCCEEDED":
                return response
            raise AsrRequestError(f"Task {status}: {json.dumps(response, ensure_ascii=False)}")
        time.sleep(poll_interval)

    raise AsrRequestError(f"Task timed out after {timeout}s: {json.dumps(last_response, ensure_ascii=False)}")


def fetch_transcription_result(task_response: dict) -> dict:
    output = task_response.get("output") if isinstance(task_response, dict) else None
    results = output.get("results") if isinstance(output, dict) else None
    if not isinstance(results, list) or not results:
        return task_response

    first = results[0]
    if not isinstance(first, dict):
        return task_response

    transcription_url = first.get("transcription_url")
    if isinstance(transcription_url, str) and transcription_url:
        return fetch_json(transcription_url)
    return task_response


def call_asr_url(
    audio_url: str,
    endpoint: str | None = None,
    model: str | None = None,
    task_endpoint: str | None = None,
    poll_interval: float = 2.0,
    timeout: float = 600.0,
) -> str:
    api_key = env("DASHSCOPE_API_KEY")
    task_id = submit_task(
        audio_url=audio_url,
        api_key=api_key,
        endpoint=endpoint or os.environ.get("BAILIAN_ASR_ENDPOINT", DEFAULT_ENDPOINT),
        model=model or os.environ.get("BAILIAN_ASR_MODEL", DEFAULT_MODEL),
    )
    task_response = wait_task(
        task_id=task_id,
        api_key=api_key,
        task_endpoint=task_endpoint or os.environ.get("BAILIAN_TASK_ENDPOINT", DEFAULT_TASK_ENDPOINT),
        poll_interval=poll_interval,
        timeout=timeout,
    )
    return json.dumps(fetch_transcription_result(task_response), ensure_ascii=False)


def extract_text(response_text: str) -> str:
    """Best-effort text extraction across common DashScope result shapes."""
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return response_text.strip()

    transcripts = payload.get("transcripts") if isinstance(payload, dict) else None
    if isinstance(transcripts, list):
        transcript_texts = [
            item.get("text", "").strip()
            for item in transcripts
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text", "").strip()
        ]
        if transcript_texts:
            return "\n".join(transcript_texts)

    output = payload.get("output") if isinstance(payload, dict) else None
    if isinstance(output, dict):
        text = output.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()

    texts: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
            for key in ("sentences", "transcripts", "results"):
                child = value.get(key)
                if isinstance(child, list):
                    for item in child:
                        collect(item)
                elif isinstance(child, dict):
                    collect(child)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    if texts:
        return "\n".join(texts)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-url", required=True, help="Audio URL that Bailian/DashScope can access.")
    parser.add_argument("--output", help="Where to write the raw ASR JSON.")
    parser.add_argument("--text-output", help="Where to write extracted transcript text.")
    parser.add_argument("--endpoint", default=os.environ.get("BAILIAN_ASR_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--task-endpoint", default=os.environ.get("BAILIAN_TASK_ENDPOINT", DEFAULT_TASK_ENDPOINT))
    parser.add_argument("--model", default=os.environ.get("BAILIAN_ASR_MODEL", DEFAULT_MODEL))
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    try:
        response_text = call_asr_url(
            audio_url=args.audio_url,
            endpoint=args.endpoint,
            task_endpoint=args.task_endpoint,
            model=args.model,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
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
