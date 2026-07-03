#!/usr/bin/env python3
"""Thin LLM client with 3-tier fallback: Codex CLI → Config API → error.

Usage:
    from llm_client import request_json, request_text

This module provides a generic LLM access layer. It does NOT contain any
review-specific logic — that belongs in build_review.py / enrich_papers.py.

Fallback chain:
    1. Codex CLI (codex exec --full-auto)
    2. Config API (user-config.local.json → llm.api_key / llm.base_url / llm.model)
    3. Return empty result + print warning
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Lazy import — only needed when Config API tier is used
_openai_module: Any = None


def _get_openai():
    global _openai_module
    if _openai_module is not None:
        return _openai_module
    try:
        import openai
        _openai_module = openai
        return openai
    except ImportError:
        return None


# ── helpers ───────────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from potentially messy LLM output."""
    raw = normalize_text(text)
    if not raw:
        return {}
    candidates = [raw]
    # Strip markdown code fences
    if raw.startswith("```"):
        candidates.append(
            re.sub(r"^```(?:json)?\s*|\s*```$", "", raw,
                   flags=re.IGNORECASE | re.DOTALL).strip()
        )
    # Extract first {...} block
    if "{" in raw and "}" in raw:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


# ── LLM config from user-config ──────────────────────────────────────────────

def _load_llm_config() -> dict[str, str]:
    """Load LLM config from _shared/user-config*.json → llm section."""
    shared_dir = Path(__file__).resolve().parent.parent / "_shared"
    config: dict[str, str] = {}
    for filename in ("user-config.json", "user-config.local.json"):
        path = shared_dir / filename
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8-sig") as f:
                data = json.load(f)
            llm = data.get("llm", {})
            if isinstance(llm, dict):
                for key in ("api_key", "base_url", "model"):
                    val = str(llm.get(key, "") or "").strip()
                    if val:
                        config[key] = val
        except Exception:
            continue
    return config


# ── Tier 1: Codex CLI ────────────────────────────────────────────────────────

def _request_via_codex(prompt: str) -> str:
    """Call codex exec and return its text output. Empty string on failure."""
    codex_bin = (
        shutil.which("codex.cmd")
        or shutil.which("codex.exe")
        or shutil.which("codex")
    )
    if not codex_bin:
        return ""
    with tempfile.TemporaryDirectory(prefix="dp_llm_") as tmp:
        last_msg = Path(tmp) / "last_message.txt"
        cmd = [
            codex_bin, "exec", "--full-auto", "--skip-git-repo-check",
            "-C", str(Path.cwd()),
            "--output-last-message", str(last_msg),
            "-",
        ]
        try:
            result = subprocess.run(
                cmd, input=prompt,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=300,
            )
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        content = ""
        if last_msg.exists():
            try:
                content = last_msg.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
        if not content:
            content = (result.stdout or "") + "\n" + (result.stderr or "")
        return content.strip()


# ── Tier 2: Config API (OpenAI-compatible) ────────────────────────────────────

def _request_via_api(system_prompt: str, user_prompt: str,
                     max_tokens: int = 2000) -> str:
    """Call OpenAI-compatible API using config from user-config. Empty on fail."""
    openai_mod = _get_openai()
    if openai_mod is None:
        return ""

    llm_cfg = _load_llm_config()
    api_key = llm_cfg.get("api_key", "")
    # Also check env vars as a convenience
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY") or ""
    if not api_key:
        return ""

    kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = llm_cfg.get("base_url", "") or os.getenv("OPENAI_BASE_URL") or ""
    if base_url:
        kwargs["base_url"] = base_url
    model = llm_cfg.get("model", "") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

    client = openai_mod.OpenAI(**kwargs)
    # Try with json mode first, fallback without
    for use_json in (True, False):
        try:
            extra: dict[str, Any] = {}
            if use_json:
                extra["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                **extra,
            )
            content = resp.choices[0].message.content or ""
            if content.strip():
                return content.strip()
        except Exception:
            continue
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def request_json(system_prompt: str, user_prompt: str,
                 max_tokens: int = 2000) -> dict[str, Any]:
    """Request a JSON object from LLM. Returns {} if all tiers fail.

    Fallback chain: Codex CLI → Config API → empty dict + warning.
    """
    full_prompt = (
        f"{system_prompt}\n\n{user_prompt}\n\n"
        "请只输出一个 JSON 对象，不要输出 Markdown，不要输出解释。"
    )

    # Tier 1: Codex CLI
    text = _request_via_codex(full_prompt)
    result = _extract_json(text)
    if result:
        return result

    # Tier 2: Config API
    text = _request_via_api(system_prompt, user_prompt, max_tokens)
    result = _extract_json(text)
    if result:
        return result

    # Tier 3: No model access — fail loudly so callers know enrichment did not happen
    raise RuntimeError(
        "[llm_client] LLM 不可用：Codex CLI 和 Config API 均无响应。\n"
        "请检查：\n"
        "  1. codex CLI 是否已登录（codex --version 可用）\n"
        "  2. user-config.local.json 中 llm.api_key 是否已填写\n"
        "调用方应捕获此异常，并在推荐文件顶部写入错误横幅。"
    )


def request_text(system_prompt: str, user_prompt: str,
                 max_tokens: int = 2000) -> str:
    """Request free-form text from LLM. Returns '' if all tiers fail."""
    full_prompt = f"{system_prompt}\n\n{user_prompt}"

    text = _request_via_codex(full_prompt)
    if text:
        return text

    text = _request_via_api(system_prompt, user_prompt, max_tokens)
    if text:
        return text

    raise RuntimeError(
        "[llm_client] LLM 不可用：Codex CLI 和 Config API 均无响应。\n"
        "请检查 codex CLI 登录状态或 user-config.local.json 中 llm.api_key。"
    )
