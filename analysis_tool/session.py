"""A persistent chat session for the web portal.

Holds the full conversation (system + user + assistant + tool messages) so the
LLM has context, plus a UI-facing `transcript` that is saved to disk — so the
portal always shows prior chat history when reopened. Streams a turn over an
async generator (SSE), and turns the blocking approval gate into asynchronous
"pending proposal" cards the coordinator approves/declines with a button.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from . import data, profiles
from .agent import LIMIT_SIGNAL, drive_turn, proposal_context, summarize_call
from .config import Config
from .prompts import build_system_prompt

_SENTINEL = object()


def _sanitize_messages(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a provider-valid copy of the model context.

    Fixes two states that make OpenRouter reject a request with invalid_request:
    - a tool-only assistant turn whose ``content`` is "" instead of null (some
      providers reject the empty string alongside tool_calls);
    - an assistant ``tool_calls`` turn not fully answered by following tool
      results — happens when a turn dies mid-round-trip — and orphan tool
      messages with no preceding call. Such incomplete turns are dropped whole.
    """
    cleaned: List[Dict[str, Any]] = []
    i, n = 0, len(msgs)
    while i < n:
        m = msgs[i]
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            if not m.get("content"):
                m["content"] = None
            call_ids = [tc.get("id") for tc in m["tool_calls"]]
            # Collect the tool responses that immediately follow this turn.
            j = i + 1
            responded = set()
            while j < n and msgs[j].get("role") == "tool":
                responded.add(msgs[j].get("tool_call_id"))
                j += 1
            if all(cid in responded for cid in call_ids):
                cleaned.append(m)
                cleaned.extend(msgs[i + 1 : j])
            # else: incomplete round-trip — drop the assistant turn and its
            # partial tool results to keep the sequence valid.
            i = j
        elif role == "tool":
            # Orphan tool message with no preceding assistant tool_call — drop.
            i += 1
        else:
            cleaned.append(m)
            i += 1
    return cleaned


class AnalysisSession:
    """One coordinator, one persistent conversation. Local single-user use."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
        self.history_path: Path = cfg.memory_dir / "chat_history.json"
        self.roster: List[str] = data.list_driver_names(cfg)
        self._system = {"role": "system", "content": build_system_prompt(cfg, self.roster)}
        # `messages` feeds the model; `transcript` feeds the UI.
        self.messages: List[Dict[str, Any]] = [self._system]
        self.transcript: List[Dict[str, Any]] = []
        self.pending: Dict[int, Dict[str, Any]] = {}
        self._pid = 0
        # One turn at a time per session (model calls are serialized).
        self._turn_lock = threading.Lock()
        self._load()

    # ------------------------------- persistence --------------------------- #

    def _load(self) -> None:
        if not self.history_path.exists():
            return
        try:
            saved = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        # Rebuild the model context from the saved transcript-bearing messages.
        msgs = saved.get("messages")
        if isinstance(msgs, list) and msgs:
            # Always use a freshly-built system prompt (roster/data may have changed),
            # and heal any provider-invalid state left by an older format or a
            # turn that died mid-tool-round-trip.
            kept = _sanitize_messages([m for m in msgs if m.get("role") != "system"])
            self.messages = [self._system] + kept
        if isinstance(saved.get("transcript"), list):
            self.transcript = saved["transcript"]
        if isinstance(saved.get("pending"), dict):
            self.pending = {int(k): v for k, v in saved["pending"].items()}
            self._pid = max(self.pending, default=0)

    def _save(self) -> None:
        payload = {
            "messages": self.messages,
            "transcript": self.transcript,
            "pending": {str(k): v for k, v in self.pending.items()},
        }
        tmp = self.history_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.history_path)

    # ------------------------------- read API ------------------------------ #

    def status(self) -> Dict[str, Any]:
        return {
            "model": self.cfg.model,
            "drivers": len(self.roster),
            "has_key": bool(self.cfg.api_key),
        }

    def history(self) -> Dict[str, Any]:
        return {
            "transcript": self.transcript,
            "pending": self._pending_list(),
            "status": self.status(),
        }

    def _pending_list(self) -> List[Dict[str, Any]]:
        return [dict(p) for p in self.pending.values()]

    # --------------------------- approval handling ------------------------- #

    def _record_pending(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self._pid += 1
        ctx = proposal_context(self.cfg, args)
        proposal = {"id": self._pid, **ctx}
        self.pending[self._pid] = proposal
        return proposal

    def approve(self, pid: int, yes: bool) -> Dict[str, Any]:
        p = self.pending.pop(int(pid), None)
        if p is None:
            return {"ok": False, "error": f"No pending proposal #{pid}.",
                    "pending": self._pending_list()}
        if yes:
            message = profiles.apply_summary_update(
                self.cfg, p["driver"], p["section"], p["proposed"]
            )
            note = f"Applied proposal #{pid}: {p['section']} for {p['driver']}."
        else:
            message = "Proposal declined. Profile prose left unchanged."
            note = f"Declined proposal #{pid}: {p['section']} for {p['driver']}."
        self.transcript.append({"role": "system", "content": note, "ts": time.time()})
        self._save()
        return {"ok": True, "message": message, "pending": self._pending_list()}

    def clear(self) -> Dict[str, Any]:
        """Wipe the conversation (history file + in-memory). Profiles are untouched."""
        self.messages = [self._system]
        self.transcript = []
        self.pending = {}
        self._pid = 0
        self._save()
        return self.history()

    # ------------------------------- the turn ------------------------------ #

    async def submit_stream(self, text: str) -> AsyncIterator[Dict[str, Any]]:
        """Run one turn, yielding SSE event dicts: delta / tool / proposal / final."""
        text = (text or "").strip()
        if not text:
            yield {"event": "final", "status": self.status(), "pending": self._pending_list()}
            return
        if not self.cfg.api_key:
            yield {"event": "error",
                   "message": "No OpenRouter API key. Set OPENROUTER_API_KEY in .env."}
            yield {"event": "final", "status": self.status(), "pending": self._pending_list()}
            return

        # Heal any dangling tool round-trip left by a previous failed turn so the
        # next request is a valid sequence (otherwise the error would be sticky).
        self.messages = _sanitize_messages(self.messages)
        self.messages.append({"role": "user", "content": text})
        self.transcript.append({"role": "user", "content": text, "ts": time.time()})

        q: "queue.Queue[Any]" = queue.Queue()
        parts: List[str] = []
        new_proposals: List[Dict[str, Any]] = []

        def on_delta(t: str) -> None:
            parts.append(t)
            q.put({"event": "delta", "text": t})

        def on_tool(name: str, args: Dict[str, Any]) -> None:
            if name == LIMIT_SIGNAL:
                q.put({"event": "tool", "summary": "reached the tool-iteration limit"})
            else:
                q.put({"event": "tool", "summary": summarize_call(name, args)})

        def approval(_cfg: Config, args: Dict[str, Any]) -> str:
            proposal = self._record_pending(args)
            new_proposals.append(proposal)
            q.put({"event": "proposal", "proposal": proposal})
            return (
                f"Proposal #{proposal['id']} has been shown to the coordinator for "
                "approval and is NOT applied yet. Do not claim the profile was "
                "changed; tell the coordinator it's awaiting their decision."
            )

        def on_retry(attempt: int, delay: float, err: Exception, discarded: int) -> None:
            # The retry regenerates the reply from scratch, so text the failed
            # attempt streamed is now garbage. Drop exactly those trailing chunks
            # from `parts` (which becomes the saved transcript) and tell the browser
            # to clear the live bubble — otherwise a truncated fragment gets
            # stitched onto the real answer and persisted as if the model said it.
            # Only the failed attempt's chunks go: `parts` spans the whole turn, so
            # prose from earlier tool-loop steps must survive.
            if discarded > 0:
                del parts[len(parts) - discarded:]
                q.put({"event": "reset"})
            q.put({"event": "notice",
                   "message": f"Upstream hiccup ({type(err).__name__}) — retrying "
                              f"({attempt}/{self.cfg.max_retries})…"})

        def worker() -> None:
            with self._turn_lock:
                try:
                    drive_turn(self.cfg, self.messages, approval=approval,
                               on_delta=on_delta, on_tool=on_tool, on_retry=on_retry)
                except Exception as exc:  # noqa: BLE001 — surface to the client
                    q.put({"event": "error", "message": str(exc)})
                finally:
                    q.put(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is _SENTINEL:
                break
            yield item

        full = "".join(parts).strip()
        if full:
            self.transcript.append({"role": "assistant", "content": full, "ts": time.time()})
        self._save()
        yield {"event": "final", "status": self.status(), "pending": self._pending_list()}
