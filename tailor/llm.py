"""One structured-output call to Claude, on either backend.

plan  headless Claude Code (`claude -p`), billed to your Claude subscription
api   the Anthropic API, billed per token to your Console account
"""

import json
import os
import subprocess
import tempfile
from typing import Literal, get_args

import anthropic
from pydantic import BaseModel

Backend = Literal["plan", "api"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL = "claude-opus-5-5"
PLAN_TIMEOUT_S = 900


def backend(value: object) -> Backend:
    """Validate a backend name from config."""
    if value == "plan":
        return "plan"
    if value == "api":
        return "api"
    msg = f"backend must be one of {get_args(Backend)}, not {value!r}"
    raise ValueError(msg)


def effort(value: object) -> Effort:
    """Validate an effort level from config."""
    levels: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")
    for level in levels:
        if value == level:
            return level
    msg = f"effort must be one of {get_args(Effort)}, not {value!r}"
    raise ValueError(msg)


def call[T: BaseModel](  # noqa: PLR0913
    system: str,
    prompt: str,
    schema: type[T],
    *,
    via: Backend = "plan",
    model: str = DEFAULT_MODEL,
    effort: Effort = "high",
) -> tuple[T, str]:
    """Return the parsed output and a label for the model that served it."""
    if via == "plan":
        return _via_plan(system, prompt, schema, model, effort)
    return _via_api(system, prompt, schema, model, effort)


def _via_plan[T: BaseModel](system: str, prompt: str, schema: type[T], model: str, effort: Effort) -> tuple[T, str]:
    # Without ANTHROPIC_API_KEY, Claude Code falls back to the subscription login.
    # (--bare would force API-key auth, so it is deliberately not used.)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    # The prompt goes on stdin: Linux caps a single argv string at 128 KiB, and
    # postings can be long. The system prompt (facts + skill pool) stays well under.
    args = ["claude", "-p", "--model", model, "--effort", effort, "--system-prompt", system]
    args += ["--tools", "", "--no-session-persistence", "--output-format", "json"]
    # Keep the user's own setup (CLAUDE.md, MCP servers and claude.ai connectors, skills,
    # plugins, hooks) out of every call: none of it is used, but it adds to each prompt.
    args += ["--safe-mode", "--strict-mcp-config", "--disable-slash-commands"]
    args += ["--json-schema", json.dumps(schema.model_json_schema())]
    try:
        proc = subprocess.run(
            args,
            input=prompt,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=tempfile.gettempdir(),
            timeout=PLAN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        msg = f"claude -p timed out after {PLAN_TIMEOUT_S}s"
        raise RuntimeError(msg) from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        msg = f"claude -p failed ({proc.returncode}): {proc.stderr[-2000:] or proc.stdout[-2000:]}"
        raise RuntimeError(msg) from e
    if out.get("is_error") or not out.get("structured_output"):
        msg = f"claude -p returned no result: {out.get('subtype')} {str(out.get('result', ''))[:1000]}"
        raise RuntimeError(msg)
    return schema.model_validate(out["structured_output"]), f"{model} (plan)"


def _via_api[T: BaseModel](system: str, prompt: str, schema: type[T], model: str, effort: Effort) -> tuple[T, str]:
    resp = anthropic.Anthropic().beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        msg = f"model declined: {resp.stop_details}"
        raise RuntimeError(msg)
    if resp.stop_reason == "max_tokens":
        msg = "response hit max_tokens before finishing"
        raise RuntimeError(msg)
    if resp.parsed_output is None:
        msg = "response did not match the output schema"
        raise RuntimeError(msg)
    return resp.parsed_output, f"{resp.model} (api)"
