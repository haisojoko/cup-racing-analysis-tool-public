"""LiteLLM streaming wrapper.

Streams a completion (so long answers don't hit request timeouts), printing
assistant text token-by-token, and assembles any tool calls into a standard
OpenAI-style assistant message for the agent loop to act on.

Transient upstream faults are retried with exponential backoff + jitter.
OpenRouter fans a single model out across many upstream providers, and one can
fail *mid-stream* — after HTTP 200, after tokens have already flowed. Because a
started stream can't retroactively change its HTTP status, OpenRouter reports
those as an error payload inside an SSE frame (often quoting `"code":400` in the
body). LiteLLM can't read a status off that, so it wraps it as
MidStreamFallbackError — a *ServiceUnavailableError*, NOT a BadRequestError.
Those are transient and worth retrying. Genuine client errors (auth, context
overflow, malformed request) are not: a retry just burns credits.
"""

from __future__ import annotations

import random
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set

import litellm

from .config import Config

# OpenRouter spreads a single model across several upstream providers, and not all
# of them honor tool_choice="required" — the ones that don't reject the request
# with a generic invalid_request BadRequestError. We can't know which provider a
# given call will land on, so we degrade to "auto" on rejection and remember the
# model here to skip the wasted forced-call round-trip for the rest of the process.
_NO_FORCED_TOOLS: Set[str] = set()

# Never retried: the request itself is the problem, so a retry can only fail the
# same way. Checked BEFORE _RETRYABLE because most of these ultimately subclass
# APIError (BadRequestError -> APIStatusError -> APIError), as do
# ContextWindowExceededError and ContentPolicyViolationError via BadRequestError.
_FATAL = (
    litellm.AuthenticationError,
    litellm.PermissionDeniedError,
    litellm.NotFoundError,
    litellm.BadRequestError,
    litellm.UnsupportedParamsError,
    litellm.BudgetExceededError,
)

# Transient: connection blips, timeouts, rate limits, provider capacity, and
# mid-stream provider faults. ServiceUnavailableError covers
# MidStreamFallbackError (which isn't exported at litellm's top level). Bare
# APIError is last as the catch-all: a mid-stream OpenrouterException with no
# extractable status code maps to exactly that.
_RETRYABLE = (
    litellm.Timeout,
    litellm.APIConnectionError,
    litellm.RateLimitError,
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
    litellm.APIError,
)


def _looks_like_forced_tool_rejection(err: Exception) -> bool:
    """Heuristic: did this BadRequest stem from tool_choice='required' support,
    rather than a genuinely malformed request (bad context length, etc.)?"""
    msg = str(err).lower()
    needles = ("tool_choice", "tool choice", "required", "function call", "invalid_request")
    return any(n in msg for n in needles)


def _is_retryable(err: Exception) -> bool:
    if isinstance(err, _FATAL):
        return False
    return isinstance(err, _RETRYABLE)


def _backoff_delay(cfg: Config, attempt: int) -> float:
    """Exponential backoff with full jitter, capped. `attempt` is 1-based.

    Jitter matters even for a single-user tool: a provider that just fell over
    is being retried by everyone at once, and un-jittered backoff marches the
    whole herd back in lockstep.
    """
    ceiling = min(cfg.retry_base_delay * (2 ** (attempt - 1)), cfg.retry_max_delay)
    return random.uniform(0, ceiling)


def stream_completion(
    cfg: Config,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    on_delta: Optional[Callable[[str], None]] = None,
    tool_choice: str = "auto",
    on_retry: Optional[Callable[[int, float, Exception, int], None]] = None,
) -> Dict[str, Any]:
    """Stream one model reply, retrying transient upstream failures.

    `on_retry(attempt, delay, err, discarded_chunks)` is called before each
    backoff sleep. `discarded_chunks` is how many `on_delta` calls the FAILED
    attempt made — that text is now garbage (the retry regenerates the reply from
    scratch) and the caller must drop exactly that many trailing chunks, or a
    truncated fragment gets stitched onto the complete answer. It counts this
    attempt only, never earlier steps of the same turn.
    """
    # If we've already learned this model's provider can't force tools, don't try.
    if tool_choice == "required" and cfg.model in _NO_FORCED_TOOLS:
        tool_choice = "auto"

    attempt = 0
    while True:
        # Authoritative count of how many chunks *we* pushed to the UI this
        # attempt — one increment per on_delta call, so a caller can pop exactly
        # that many. LiteLLM's own `is_pre_first_chunk` describes its view of the
        # wire, which isn't quite the same question.
        emitted = [0]
        try:
            return _run_completion(cfg, messages, tools, on_delta, tool_choice, emitted)
        except litellm.BadRequestError as err:
            # Forced-tool rejection from an upstream provider: degrade to auto so the
            # turn still succeeds. A forced call is rejected at validation time, before
            # any tokens stream, so retrying never double-emits text. This is a
            # capability downgrade, not a transient fault, so it doesn't spend an
            # attempt — and it can't loop, since tool_choice is no longer "required".
            if tool_choice == "required" and _looks_like_forced_tool_rejection(err):
                _NO_FORCED_TOOLS.add(cfg.model)
                tool_choice = "auto"
                continue
            raise
        except Exception as err:  # noqa: BLE001 — re-raised unless transient
            attempt += 1
            if attempt > cfg.max_retries or not _is_retryable(err):
                raise
            delay = _backoff_delay(cfg, attempt)
            if on_retry is not None:
                on_retry(attempt, delay, err, emitted[0])
            elif emitted[0] > 0:
                # Terminal default: already-printed text can't be unprinted, so
                # say plainly that it's being abandoned rather than let it read
                # as part of the answer.
                sys.stdout.write("\n[stream failed mid-answer — discarding the partial "
                                 "text above and retrying]\n")
                sys.stdout.flush()
            time.sleep(delay)


def _run_completion(
    cfg: Config,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    on_delta: Optional[Callable[[str], None]],
    tool_choice: str,
    emitted: List[int],
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "stream": True,
    }
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.api_base:
        kwargs["api_base"] = cfg.api_base
    # OpenRouter reads optional traffic attribution from these headers.
    headers = {}
    if getattr(cfg, "referer", None):
        headers["HTTP-Referer"] = cfg.referer
    if getattr(cfg, "title", None):
        headers["X-Title"] = cfg.title
    if headers:
        kwargs["extra_headers"] = headers

    response = litellm.completion(**kwargs)

    content_parts: List[str] = []
    tool_calls: Dict[int, Dict[str, str]] = {}
    printed = False

    for chunk in response:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = choices[0].delta
        text = getattr(delta, "content", None)
        if text:
            if on_delta is not None:
                on_delta(text)
            else:
                sys.stdout.write(text)
                sys.stdout.flush()
            content_parts.append(text)
            printed = True
            emitted[0] += 1
        for tc in getattr(delta, "tool_calls", None) or []:
            idx = tc.index if tc.index is not None else len(tool_calls)
            slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["args"] += fn.arguments

    if printed and on_delta is None:
        sys.stdout.write("\n")
        sys.stdout.flush()

    # Tool-only replies: send content as null, not "" — the OpenAI/OpenRouter
    # spec expects null when an assistant turn is purely tool_calls, and some
    # providers reject an empty string alongside tool_calls when it's replayed.
    text = "".join(content_parts)
    assistant: Dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        assistant["tool_calls"] = [
            {
                "id": slot["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
            }
            for idx, slot in sorted(tool_calls.items())
        ]
    return assistant
