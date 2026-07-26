#!/usr/bin/env python3
"""Fail closed when the pipeline resolves an unexpected Feishu application."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_DIR / "local" / "pipeline.json"
AGENT_WORKSPACE_ENV_VARS = (
    "HERMES_HOME",
    "OPENCLAW_HOME",
    "LARK_CHANNEL",
)


class FeishuIdentityError(RuntimeError):
    pass


def isolated_lark_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment that cannot auto-select another Agent workspace."""

    env = dict(os.environ if source is None else source)
    for name in AGENT_WORKSPACE_ENV_VARS:
        env.pop(name, None)
    env.setdefault("LARKSUITE_CLI_CONFIG_DIR", str(Path.home() / ".lark-cli"))
    env.setdefault("LARKSUITE_CLI_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("LARKSUITE_CLI_NO_SKILLS_NOTIFIER", "1")
    return env


def resolve_project_path(value: Any, default: Path) -> Path:
    text = str(value or "").strip()
    path = Path(text) if text else default
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def load_feishu_config(config_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FeishuIdentityError(f"Pipeline config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise FeishuIdentityError(f"Invalid pipeline config: {config_path}") from exc
    feishu = payload.get("feishu")
    if not isinstance(feishu, dict):
        raise FeishuIdentityError("Missing feishu configuration section.")
    return feishu


def configured_lark_profile(config_path: Path = DEFAULT_CONFIG) -> str:
    profile = str(load_feishu_config(config_path).get("profile") or "").strip()
    if not profile:
        raise FeishuIdentityError("feishu.profile is required.")
    return profile


def scoped_lark_command(
    cli: str,
    args: list[str],
    *,
    profile: str | None = None,
    config_path: Path = DEFAULT_CONFIG,
) -> list[str]:
    """Pin every project lark-cli call to the configured local profile."""

    if "--profile" in args:
        return [cli, *args]
    selected = str(profile or "").strip() or configured_lark_profile(config_path)
    return [cli, *args, "--profile", selected]


def verify_feishu_identity(
    feishu: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    expected_app_id = str(feishu.get("expected_app_id") or "").strip()
    expected_app_name = str(feishu.get("expected_app_name") or "").strip()
    profile = str(feishu.get("profile") or "").strip()
    identity = str(feishu.get("as_identity") or "").strip()
    if not expected_app_id:
        raise FeishuIdentityError("feishu.expected_app_id is required.")
    if not profile:
        raise FeishuIdentityError("feishu.profile is required.")
    if identity not in {"user", "bot"}:
        raise FeishuIdentityError("feishu.as_identity must be user or bot.")

    cli = resolve_project_path(
        feishu.get("lark_cli"),
        PROJECT_DIR / "tools" / "lark-cli" / "lark-cli.exe",
    )
    command = scoped_lark_command(str(cli), [
        "auth",
        "status",
        "--json",
        "--verify",
    ], profile=profile)
    result = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=isolated_lark_env(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = result.stdout.strip() or result.stderr.strip()
    if result.returncode:
        raise FeishuIdentityError(
            f"lark-cli identity check failed with exit code {result.returncode}: {output[:500]}"
        )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise FeishuIdentityError("lark-cli identity check did not return JSON.") from exc

    actual_app_id = str(payload.get("appId") or "").strip()
    if actual_app_id != expected_app_id:
        raise FeishuIdentityError(
            f"Unexpected Feishu App ID: expected {expected_app_id}, got {actual_app_id or '<empty>'}."
        )
    identities = payload.get("identities")
    current = identities.get(identity) if isinstance(identities, dict) else None
    if not isinstance(current, dict) or not current.get("available") or not current.get("verified"):
        status = current.get("status") if isinstance(current, dict) else "missing"
        raise FeishuIdentityError(
            f"Feishu {identity} identity is not ready (status={status})."
        )
    bot = identities.get("bot") if isinstance(identities, dict) else None
    actual_app_name = str(bot.get("appName") or "").strip() if isinstance(bot, dict) else ""
    if expected_app_name and actual_app_name != expected_app_name:
        raise FeishuIdentityError(
            f"Unexpected Feishu app name: expected {expected_app_name}, "
            f"got {actual_app_name or '<empty>'}."
        )
    return {
        "app_id": actual_app_id,
        "app_name": actual_app_name or expected_app_name,
        "profile": profile,
        "identity": identity,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        feishu = load_feishu_config(args.config.resolve())
        result = verify_feishu_identity(feishu)
    except FeishuIdentityError as exc:
        print(f"Feishu identity preflight failed: {exc}")
        return 1
    print(
        "Feishu identity preflight passed: "
        f"app={result['app_name']} ({result['app_id']}), "
        f"profile={result['profile']}, identity={result['identity']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
