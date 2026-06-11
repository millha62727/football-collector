"""Optional OpenAI-compatible LLM client.

Activates only when all three env vars are present — "có thì xài, không có thì
thôi". Callers must guard with `is_configured()`; nothing here imports an SDK,
we speak the raw `/chat/completions` HTTP contract over aiohttp (already a dep),
so any OpenAI-compatible endpoint works (ai-box, OpenAI, vLLM, LM Studio, ...).

Config (.env):
  AI_BASE_URL     base URL up to and including the API version, e.g.
                  https://api.ai-box.vn/v1   (trailing slash optional)
  AI_API_KEY      bearer token
  AI_MODEL_UI     model name for UI-driven calls (analyzer page button)
  AI_MODEL_CRON   model name for background jobs (pattern sweep, etc.)
  AI_MODEL        LEGACY fallback — used for both UI and cron if the scoped
                  vars are not set. Lets existing deployments keep working.

Env is read lazily (not at import) so both entry points — uvicorn web server
and the collector process — pick up values without an early dotenv hook,
mirroring telegram.py's approach.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import aiohttp

# Generous default: LLMs can take several seconds. Connectivity check uses a
# tighter timeout of its own.
_TIMEOUT = 30

# Valid scope values for `_model(scope=)` and `is_configured(scope=)`. The UI
# scope drives the analyzer page button; the cron scope drives background jobs
# (pattern sweep). Adding more scopes later is a matter of extending the
# resolver, not touching call sites.
_VALID_SCOPES = ("ui", "cron")

# Cap on per-call model override length. 200 chars is well past any real model
# name and short enough to refuse obvious junk without being annoying.
_MAX_MODEL_LEN = 200


# ---------------------------------------------------------------------------
# Config resolvers (lazy, env-only)
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return (os.getenv("AI_BASE_URL", "") or "").strip().rstrip("/")


def _api_key() -> str:
    return (os.getenv("AI_API_KEY", "") or "").strip()


def _model(scope: str = "ui") -> str:
    """Resolve the configured model for a given scope.

    Order: scoped var (e.g. AI_MODEL_UI) → legacy `AI_MODEL` → empty. Scope
    must be in `_VALID_SCOPES`; any other value raises ValueError so a typo
    doesn't silently fall back to UI.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(f"invalid scope {scope!r}; expected one of {_VALID_SCOPES}")
    scoped_key = f"AI_MODEL_{scope.upper()}"
    return (os.getenv(scoped_key, "") or os.getenv("AI_MODEL", "") or "").strip()


# Valid reasoning_effort variants accepted by ai-box (probed against the live
# endpoint): low < medium < high < max < xhigh. Empty → omit the field entirely
# (let the model use its default; DeepSeek v4 reasons by default regardless).
_REASONING_LEVELS = ("low", "medium", "high", "max", "xhigh")


def _reasoning_effort() -> str:
    """Optional. When set to a valid level, sent as `reasoning_effort`.

    Invalid/empty values are dropped so a typo can't 400 every request.
    """
    v = (os.getenv("AI_REASONING_EFFORT", "") or "").strip().lower()
    return v if v in _REASONING_LEVELS else ""


def _extra_body() -> dict[str, Any]:
    """Optional JSON object merged into every request body.

    Escape hatch for provider-specific params (e.g. {"thinking":{...}}) without
    a code change. Malformed JSON is ignored (logged-safe: returns {}).
    """
    raw = (os.getenv("AI_EXTRA_BODY", "") or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def is_configured(scope: str = "ui") -> bool:
    """True only when base URL, API key, and the scoped model are all set."""
    return bool(_base_url() and _api_key() and _model(scope))


def _mask(key: str) -> str:
    """Mask the API key for display — never leak the full secret to the UI."""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def diagnose() -> dict[str, Any]:
    """Surface resolved config so the UI can show a precise status/reason.

    Reports both scoped models (ui + cron) plus the legacy `model` field for
    callers that haven't migrated. The API key is masked; the base URL and
    model names are safe to show.
    """
    key = _api_key()
    model_ui = _model("ui")
    model_cron = _model("cron")
    return {
        # Legacy keys — preserved for callers (UI badge, sweep) that still
        # read a single `model` field. Mapped to the UI scope since the
        # analyzer page is the dominant caller.
        "configured": is_configured("ui"),
        "has_base_url": bool(_base_url()),
        "has_api_key": bool(key),
        "has_model": bool(model_ui),
        "base_url": _base_url(),
        "model": model_ui,
        "api_key_masked": _mask(key),
        "reasoning_effort": _reasoning_effort(),
        # New scoped keys — the UI badge can show the actual model in use,
        # and the cron sweep can verify its own model independently.
        "model_ui": model_ui,
        "model_cron": model_cron,
        "configured_ui": is_configured("ui"),
        "configured_cron": is_configured("cron"),
    }


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    max_tokens: int = 512,
    timeout: int = _TIMEOUT,
    reasoning_effort: Optional[str] = None,
    model: Optional[str] = None,
) -> dict[str, Any]:
    """Low-level chat completion. Raises RuntimeError on any failure.

    Returns the parsed JSON response body. Callers extract
    `data["choices"][0]["message"]["content"]` themselves.

    `reasoning_effort` overrides the AI_REASONING_EFFORT env default for this
    call only — pass an explicit level (low/medium/...) for tasks that don't
    need deep reasoning, or "" to force-omit the field. Invalid values fall
    back to the env default.

    `model` overrides the env-resolved model for this call only — pass a non-
    empty string to swap models per call (e.g. UI input box). Empty / None
    falls back to `_model("ui")` since chat() is invoked from the analyzer
    context, which is the UI scope. The override is sanitized (stripped, max
    _MAX_MODEL_LEN chars) so a typo or hostile payload can't 400 the request.
    """
    if not is_configured("ui"):
        raise RuntimeError(
            "AI chưa được cấu hình (cần AI_BASE_URL + AI_API_KEY + AI_MODEL_UI/AI_MODEL trong .env)"
        )
    url = _base_url() + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    # Resolve per-call model override. Empty/None → use env default (UI scope).
    # _clean_model returns the sanitized string or empty when nothing valid
    # was passed; ValueError is raised for over-long input.
    override = _clean_model(model)
    body = {
        "model": override or _model("ui"),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort is None:
        effort = _reasoning_effort()
    else:
        ro = (reasoning_effort or "").strip().lower()
        effort = ro if ro in _REASONING_LEVELS else (_reasoning_effort() if reasoning_effort != "" else "")
    if effort:
        body["reasoning_effort"] = effort
    # Provider-specific escape hatch — merged last so it can override anything.
    extra = _extra_body()
    if extra:
        body.update(extra)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {text[:300]}")
                return _parse_completion(text)
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Lỗi kết nối: {e}")
    except (TimeoutError, __import__("asyncio").TimeoutError):
        raise RuntimeError(f"Timeout sau {timeout}s")


def _clean_model(raw: Optional[str]) -> str:
    """Sanitize a per-call model override.

    Returns the trimmed non-empty string when it fits under _MAX_MODEL_LEN.
    Returns '' for None / empty so callers fall back to the env default.
    Raises ValueError for over-long input — the caller surfaces that as 400.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if len(s) > _MAX_MODEL_LEN:
        raise ValueError(f"model name quá dài (max {_MAX_MODEL_LEN} chars)")
    return s


def _parse_completion(text: str) -> dict[str, Any]:
    """Parse a completion response — handles BOTH non-streamed JSON and SSE.

    ai-box returns plain JSON for DeepSeek but Server-Sent-Events (`data: {...}`
    chunks) for Claude `*-thinking` models even without stream=true. We coalesce
    SSE deltas into a single OpenAI-shaped object so callers see one contract.
    Raises RuntimeError if neither shape yields content.
    """
    stripped = text.lstrip()
    # Plain JSON (DeepSeek path)
    if stripped.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Phản hồi không phải JSON hợp lệ: {e}")

    # SSE path: accumulate delta.content / delta.reasoning_content across chunks.
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    model_name = ""
    finish_reason = None
    usage: dict[str, Any] = {}
    saw_chunk = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        saw_chunk = True
        model_name = chunk.get("model") or model_name
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices", []):
            delta = ch.get("delta") or ch.get("message") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    if not saw_chunk:
        raise RuntimeError(f"Phản hồi không nhận dạng được: {text[:200]}")

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    return {
        "model": model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def extract_content(data: dict[str, Any]) -> str:
    """Pull assistant text from a completion response; '' if shape unexpected."""
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def extract_reasoning(data: dict[str, Any]) -> str:
    """Pull the model's reasoning/thinking trace; '' if absent.

    DeepSeek v4 and Claude `*-thinking` expose this as `reasoning_content`.
    """
    try:
        msg = data["choices"][0]["message"]
        return (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Connectivity check — a real round-trip
# ---------------------------------------------------------------------------


async def check() -> dict[str, Any]:
    """Probe the endpoint with a minimal real completion.

    Returns a status dict the UI renders directly:
      {ok, configured, latency_ms?, model?, base_url?, sample?, error?}
    Never raises — failures are reported in the dict.
    """
    diag = diagnose()
    if not diag["configured"]:
        missing = []
        if not diag["has_base_url"]:
            missing.append("AI_BASE_URL")
        if not diag["has_api_key"]:
            missing.append("AI_API_KEY")
        if not diag["has_model"]:
            missing.append("AI_MODEL_UI")
        return {
            "ok": False,
            "configured": False,
            "error": "Thiếu: " + ", ".join(missing),
            **diag,
        }

    t0 = time.perf_counter()
    try:
        data = await chat(
            [{"role": "user", "content": "ping — reply with the single word: OK"}],
            temperature=0.0,
            max_tokens=16,
            timeout=15,
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": True,
            "configured": True,
            "latency_ms": elapsed,
            "model": _model("ui"),
            "base_url": _base_url(),
            "sample": extract_content(data)[:50],
        }
    except Exception as e:  # noqa: BLE001 — surface any failure as a status field
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "configured": True,
            "latency_ms": elapsed,
            "model": _model("ui"),
            "base_url": _base_url(),
            "error": str(e)[:300],
        }

# ---------------------------------------------------------------------------
# Model listing — fetch from API
# ---------------------------------------------------------------------------

# Simple TTL cache: (timestamp, model_ids)
_models_cache: tuple[float, list[str]] | None = None
_MODELS_CACHE_TTL = 300  # 5 minutes


async def list_models() -> list[str]:
    """Fetch available models from the API's /v1/models endpoint.

    Returns a list of model IDs (strings). Returns [] if the API is not
    configured, the endpoint returns non-200, or parsing fails.
    Result is cached for _MODELS_CACHE_TTL seconds to avoid hammering the
    API on every page load.
    """
    global _models_cache
    now = time.time()
    if _models_cache and (now - _models_cache[0]) < _MODELS_CACHE_TTL:
        return _models_cache[1]

    if not is_configured():
        return []

    url = _base_url() + "/models"
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    try:
        client_timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                # OpenAI-compatible: {"object": "list", "data": [{"id": "...", ...}, ...]}
                model_ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
                # Filter out models we don't want surfaced in the UI (Gemini + 1M-context variants)
                model_ids = [
                    mid for mid in model_ids
                    if "gemini" not in mid.lower() and "[1m]" not in mid
                ]
                _models_cache = (now, model_ids)
                return model_ids
    except Exception:
        return []

