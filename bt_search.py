#!/usr/bin/env python3
"""Ollama chat with web search — provides search-augmented chat via
Ollama's tool calling API and cloud web search endpoints."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("bt_search")

_MAX_TOOL_ITERATIONS = 5
_MAX_RESULT_CHARS = 2000


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env file for OLLAMA_API_KEY and other vars."""
    try:
        from dotenv import load_dotenv
        load_dotenv("/home/pi/.device-radar.env")
    except ImportError:
        env_path = Path("/home/pi/.device-radar.env")
        if not env_path.exists():
            return
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


_load_env()


# ---------------------------------------------------------------------------
# Ollama library detection
# ---------------------------------------------------------------------------

try:
    import ollama as _ollama
    _HAS_OLLAMA = True
except ImportError:
    _ollama = None  # type: ignore[assignment]
    _HAS_OLLAMA = False

_web_search_fn: Any = None
_web_fetch_fn: Any = None
_HAS_WEB_TOOLS = False

if _HAS_OLLAMA:
    try:
        from ollama import web_search as _ws, web_fetch as _wf
        _web_search_fn = _ws
        _web_fetch_fn = _wf
        _HAS_WEB_TOOLS = True
    except (ImportError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _search_enabled(config: dict[str, Any]) -> bool:
    """Check whether web search should be active."""
    return (
        _HAS_WEB_TOOLS
        and config.get("web_search_enabled", False)
        and bool(os.environ.get("OLLAMA_API_KEY"))
    )


def _is_tools_unsupported_error(exc: Exception) -> bool:
    """Check if an exception indicates the model doesn't support tools."""
    msg = str(exc).lower()
    return "does not support tools" in msg or "tools is not supported" in msg


_SEARCH_SYSTEM_SUFFIX = (
    " You have access to a web_search tool. You MUST call the web_search tool "
    "whenever the user asks about current events, recent news, real-time "
    "information, wars, politics, sports results, weather, or anything that "
    "may have changed after your training data. Never say you cannot access "
    "real-time information — always use web_search instead. When in doubt "
    "about whether information is current, search the web."
)


def _inject_search_instructions(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append web search instructions to the system message."""
    result = []
    for msg in messages:
        if msg["role"] == "system":
            msg = {**msg, "content": msg["content"] + _SEARCH_SYSTEM_SUFFIX}
        result.append(msg)
    return result


def _use_thinking(config: dict[str, Any]) -> bool | None:
    """Determine whether to enable thinking mode.

    Returns ``None`` to leave the default, ``False`` to explicitly disable.
    Thinking models (e.g. qwen3) are very slow on CPU-only devices like
    the Raspberry Pi, so thinking is disabled by default unless the config
    explicitly enables it.
    """
    return config.get("ollama_think", False)


def _execute_tool(tool_call: Any) -> str:
    """Execute a single tool call, return result as string."""
    tools: dict[str, Any] = {}
    if _web_search_fn is not None:
        tools["web_search"] = _web_search_fn
    if _web_fetch_fn is not None:
        tools["web_fetch"] = _web_fetch_fn

    fn = tools.get(tool_call.function.name)
    if fn is None:
        return f"Tool {tool_call.function.name} not found"

    try:
        result = fn(**tool_call.function.arguments)
        return str(result)[:_MAX_RESULT_CHARS]
    except Exception as exc:
        logger.warning("Tool %s failed: %s", tool_call.function.name, exc)
        return f"Tool error: {exc}"


# ---------------------------------------------------------------------------
# Fallback: raw httpx /api/generate (when ollama package unavailable)
# ---------------------------------------------------------------------------

def _build_generate_payload(
    messages: list[dict[str, str]], config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build /api/generate payload from messages list."""
    model = config.get("ollama_model", "qwen2.5:1.5b")
    parts: list[str] = []
    system_prompt = ""
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            system_prompt = content
        elif role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
    parts.append("Assistant:")
    prompt = "\n".join(parts)

    payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    if system_prompt:
        payload["system"] = system_prompt
    return config.get("ollama_url", "http://localhost:11434"), payload


def _fallback_generate_sync(
    messages: list[dict[str, str]], config: dict[str, Any],
) -> str | None:
    """Synchronous chat via /api/generate (no tool calling)."""
    base_url, payload = _build_generate_payload(messages, config)
    timeout = config.get("ollama_timeout_seconds", 60)
    try:
        resp = httpx.post(
            f"{base_url}/api/generate", json=payload, timeout=timeout,
        )
        resp.raise_for_status()
        result = resp.json().get("response", "").strip()
        return result or None
    except httpx.TimeoutException:
        logger.warning("Ollama timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.error("Ollama error: %s", exc)
        return None


async def _fallback_generate_async(
    messages: list[dict[str, str]], config: dict[str, Any],
) -> str | None:
    """Async chat via /api/generate (no tool calling)."""
    base_url, payload = _build_generate_payload(messages, config)
    timeout = config.get("ollama_timeout_seconds", 15)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/api/generate", json=payload, timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except httpx.TimeoutException:
        logger.warning("Ollama timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.error("Ollama error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Chat with search (public API)
# ---------------------------------------------------------------------------

def chat_with_search_sync(
    messages: list[dict[str, str]], config: dict[str, Any],
) -> tuple[str | None, bool]:
    """Synchronous chat with optional web search tool calling.

    Returns ``(response_text, searched)`` where *searched* indicates
    whether web search was invoked during the conversation.
    """
    if not _HAS_OLLAMA:
        return _fallback_generate_sync(messages, config), False

    host = config.get("ollama_url", "http://localhost:11434")
    model = config.get("ollama_model", "qwen2.5:1.5b")
    timeout = config.get("ollama_timeout_seconds", 60)
    use_search = _search_enabled(config)

    client = _ollama.Client(host=host, timeout=timeout)
    tools = [_web_search_fn, _web_fetch_fn] if use_search else []

    chat_messages: list[Any] = [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]
    if use_search:
        chat_messages = _inject_search_instructions(chat_messages)
    searched = False

    try:
        for _ in range(_MAX_TOOL_ITERATIONS):
            kwargs: dict[str, Any] = {
                "model": model, "messages": chat_messages,
                "think": _use_thinking(config),
            }
            if tools:
                kwargs["tools"] = tools

            try:
                response = client.chat(**kwargs)
            except Exception as exc:
                if tools and _is_tools_unsupported_error(exc):
                    logger.warning(
                        "Model %s does not support tools — retrying without search",
                        model,
                    )
                    tools = []
                    response = client.chat(
                        model=model, messages=chat_messages,
                        think=_use_thinking(config),
                    )
                else:
                    raise

            chat_messages.append(response.message)

            if response.message.tool_calls:
                searched = True
                for tc in response.message.tool_calls:
                    logger.info(
                        "Tool call: %s(%s)", tc.function.name, tc.function.arguments,
                    )
                    result = _execute_tool(tc)
                    chat_messages.append({"role": "tool", "content": result})
            else:
                text = (response.message.content or "").strip()
                return text or None, searched

        # Exhausted iterations — return whatever we have
        text = (response.message.content or "").strip()
        return text or None, searched

    except Exception as exc:
        logger.error("Chat with search error: %s", exc)
        return None, False


async def chat_with_search_async(
    messages: list[dict[str, str]], config: dict[str, Any],
) -> tuple[str | None, bool]:
    """Async chat with optional web search tool calling.

    Returns ``(response_text, searched)`` where *searched* indicates
    whether web search was invoked during the conversation.
    """
    if not _HAS_OLLAMA:
        result = await _fallback_generate_async(messages, config)
        return result, False

    host = config.get("ollama_url", "http://localhost:11434")
    model = config.get("ollama_model", "qwen2.5:1.5b")
    timeout = config.get("ollama_timeout_seconds", 15)
    use_search = _search_enabled(config)

    client = _ollama.AsyncClient(host=host, timeout=timeout)
    tools = [_web_search_fn, _web_fetch_fn] if use_search else []

    chat_messages: list[Any] = [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]
    if use_search:
        chat_messages = _inject_search_instructions(chat_messages)
    searched = False

    try:
        for _ in range(_MAX_TOOL_ITERATIONS):
            kwargs: dict[str, Any] = {
                "model": model, "messages": chat_messages,
                "think": _use_thinking(config),
            }
            if tools:
                kwargs["tools"] = tools

            try:
                response = await client.chat(**kwargs)
            except Exception as exc:
                if tools and _is_tools_unsupported_error(exc):
                    logger.warning(
                        "Model %s does not support tools — retrying without search",
                        model,
                    )
                    tools = []
                    response = await client.chat(
                        model=model, messages=chat_messages,
                        think=_use_thinking(config),
                    )
                else:
                    raise

            chat_messages.append(response.message)

            if response.message.tool_calls:
                searched = True
                loop = asyncio.get_running_loop()
                for tc in response.message.tool_calls:
                    logger.info(
                        "Tool call: %s(%s)", tc.function.name, tc.function.arguments,
                    )
                    result = await loop.run_in_executor(None, _execute_tool, tc)
                    chat_messages.append({"role": "tool", "content": result})
            else:
                text = (response.message.content or "").strip()
                return text or None, searched

        # Exhausted iterations — return whatever we have
        text = (response.message.content or "").strip()
        return text or None, searched

    except Exception as exc:
        logger.error("Async chat with search error: %s", exc)
        return None, False
