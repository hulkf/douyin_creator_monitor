#!/usr/bin/env python3
"""Generate a filled Markdown summary card from a transcript and prompt template."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


class SummaryGenerationError(RuntimeError):
    pass


def read_text(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig").strip("\ufeff\r\n")
    except FileNotFoundError as exc:
        raise SummaryGenerationError(f"{label}不存在: {path}") from exc
    except OSError as exc:
        raise SummaryGenerationError(f"读取{label}失败: {path}: {exc}") from exc
    if not text.strip():
        raise SummaryGenerationError(f"{label}为空: {path}")
    return text


def build_messages(
    *,
    template: str,
    transcript: str,
    creator_name: str = "",
    title: str = "",
    aweme_id: str = "",
) -> list[dict[str, str]]:
    metadata_lines = []
    if creator_name:
        metadata_lines.append(f"- 来源作者: {creator_name}")
    if title:
        metadata_lines.append(f"- 作品标题: {title}")
    if aweme_id:
        metadata_lines.append(f"- 作品ID: {aweme_id}")
    metadata = "\n".join(metadata_lines) if metadata_lines else "- 来源作者: 未注明"
    user_content = f"""请根据下方作者内容，严格按照系统提示词要求输出“已填写”的 Markdown 归档卡片。

要求：
- 只输出最终归档卡片，不要复述系统提示词、规则说明或原始正文。
- 不要保留占位符；作者没提供的数据写“未提供”。
- 保留必要作者原话，但不要整段复制原文。

作品信息：
{metadata}

作者原文内容：
{transcript}
"""
    return [
        {"role": "system", "content": template},
        {"role": "user", "content": user_content},
    ]


def request_chat_completion(
    *,
    messages: list[dict[str, str]],
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    max_attempts: int = 3,
    thinking: str = "",
    retry_base_seconds: float = 2.0,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    attempts = max(1, int(max_attempts))
    token_budget = max_tokens
    for attempt in range(attempts):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if token_budget > 0:
            payload["max_tokens"] = token_budget
        if thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": thinking}
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            transient = exc.code in {408, 409, 425, 429, 500, 502, 503, 504}
            if not transient or attempt + 1 >= attempts:
                raise SummaryGenerationError(f"总结模型请求失败 HTTP {exc.code}: {detail}") from exc
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After", "0") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            time.sleep(max(retry_after, min(retry_base_seconds * (2 ** attempt), 30.0)))
            continue
        except urllib.error.URLError as exc:
            if attempt + 1 >= attempts:
                raise SummaryGenerationError(f"总结模型请求失败: {exc}") from exc
            time.sleep(min(retry_base_seconds * (2 ** attempt), 30.0))
            continue

        try:
            response_payload = json.loads(body)
            choices = response_payload.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            finish_reason = str(choice.get("finish_reason") or "") if isinstance(choice, dict) else ""
        except (ValueError, AttributeError, IndexError) as exc:
            raise SummaryGenerationError(f"总结模型返回格式异常: {body}") from exc

        retryable_response = finish_reason in {"length", "network_error"} or not (
            isinstance(content, str) and content.strip()
        )
        if retryable_response and attempt + 1 < attempts:
            if finish_reason == "length" and token_budget > 0:
                token_budget = min(max(token_budget * 2, token_budget + 1024), 32768)
            time.sleep(min(retry_base_seconds * (2 ** attempt), 30.0))
            continue
        if finish_reason == "length":
            raise SummaryGenerationError(
                f"总结模型输出达到 token 上限（max_tokens={token_budget}），未生成完整内容。"
            )
        if finish_reason == "network_error":
            raise SummaryGenerationError("总结模型返回 network_error。")
        if not isinstance(content, str) or not content.strip():
            raise SummaryGenerationError(f"总结模型返回空内容: {body}")
        return content.strip("\ufeff\r\n")

    raise SummaryGenerationError("总结模型请求未返回可用内容。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按内容总结提示词生成已填写 Markdown 归档卡片")
    parser.add_argument("--transcript", type=Path, required=True, help="最终文案 TXT 文件")
    parser.add_argument("--template-file", type=Path, required=True, help="内容总结提示词模板")
    parser.add_argument("--output", type=Path, required=True, help="生成的 Markdown 总结文件")
    parser.add_argument("--creator-name", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--aweme-id", default="")
    parser.add_argument("--model", default=os.environ.get("SUMMARY_LLM_MODEL", ""))
    parser.add_argument("--base-url", default=os.environ.get("SUMMARY_LLM_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key-env", default=os.environ.get("SUMMARY_LLM_API_KEY_ENV", DEFAULT_API_KEY_ENV))
    parser.add_argument("--temperature", type=float, default=float(os.environ.get("SUMMARY_LLM_TEMPERATURE", "0.2")))
    parser.add_argument("--max-tokens", type=int, default=int(os.environ.get("SUMMARY_LLM_MAX_TOKENS", "1800")))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("SUMMARY_LLM_TIMEOUT", "120")))
    parser.add_argument("--retry-attempts", type=int, default=int(os.environ.get("SUMMARY_LLM_RETRY_ATTEMPTS", "3")))
    parser.add_argument(
        "--thinking",
        choices=["enabled", "disabled"],
        default=os.environ.get("SUMMARY_LLM_THINKING", "") or None,
        help="支持该参数的推理模型可显式开启或关闭思考模式",
    )
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=float(os.environ.get("SUMMARY_LLM_RETRY_BASE_SECONDS", "2")),
        help="429/网络错误与空响应重试的基础退避秒数",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将发送给模型的 messages JSON，不调用模型、不写 output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        transcript = read_text(args.transcript, "文案文件")
        template = read_text(args.template_file, "内容总结提示词模板")
        messages = build_messages(
            template=template,
            transcript=transcript,
            creator_name=args.creator_name,
            title=args.title,
            aweme_id=args.aweme_id,
        )
        if args.dry_run:
            print(json.dumps({"messages": messages}, ensure_ascii=False, indent=2))
            return 0
        model = str(args.model or "").strip()
        if not model:
            raise SummaryGenerationError("缺少总结模型名称，请设置 summary.model 或 SUMMARY_LLM_MODEL。")
        api_key = os.environ.get(str(args.api_key_env or "").strip())
        if not api_key:
            raise SummaryGenerationError(f"缺少总结模型 API Key 环境变量: {args.api_key_env}")
        summary = request_chat_completion(
            messages=messages,
            model=model,
            api_key=api_key,
            base_url=args.base_url,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            max_attempts=args.retry_attempts,
            thinking=args.thinking or "",
            retry_base_seconds=args.retry_base_seconds,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(summary.rstrip() + "\n", encoding="utf-8")
        temporary.replace(args.output)
        print(args.output)
        return 0
    except SummaryGenerationError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
