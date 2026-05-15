"""Queue-based worker that invokes Google's Gemini CLI and routes output.

CLI integration notes
---------------------
This executor wraps the ``gemini`` command from ``@google/gemini-cli``.  Key
assumptions (verify against ``gemini --help`` if anything breaks):

* ``gemini --output-format json --yolo "<prompt>"`` runs non-interactively and
  emits JSON events to stdout.  ``--yolo`` auto-approves tool calls so the
  agent never blocks on a confirmation prompt; ``--output-format json``
  switches stdout from human-formatted prose to structured JSONL.
* ``--model <id>`` selects the model (e.g. ``gemini-2.5-pro``).
* The CLI reads MCP server configuration from ``~/.gemini/settings.json``
  under the ``mcpServers`` key.
* The CLI honours a top-level ``GEMINI.md`` in the working directory as a
  system-prompt overlay (analogous to Codex's ``AGENTS.md``).

Session resume
--------------
Gemini CLI's native chat session resume has shifted between releases, so we
deliberately do NOT rely on it.  Instead, ``GeminiExecutor`` maintains its
own per-chat history file (``{home_dir}/history/<chat_id>.jsonl``) and
prepends the last few user/assistant turns to each new prompt.  This is
robust across CLI versions; the cost is that every turn re-sends recent
context, which Gemini's large context windows absorb without trouble.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from pathlib import Path

import httpx

from slim_agent_core import (
    Executor,
    ExecutionRequest,
    RecentTasksLog,
    SessionStore,
    SuperposClient,
    TaskSummary,
    TelegramGateway,
    TelegramStreamer,
    collect_mcp_servers,
    discover_modules,
    ensure_worktree,
    is_git_repo,
    worktree_path,
)

from .config import GeminiConfig
from .runtime_config import GeminiRuntimeConfig

log = logging.getLogger(__name__)

# ── Persona / history layout ──────────────────────────────────────────

_PERSONA_BEGIN = "<!-- PERSONA:BEGIN -->"
_PERSONA_END = "<!-- PERSONA:END -->"
_PERSONA_RE = re.compile(
    rf"{re.escape(_PERSONA_BEGIN)}.*?{re.escape(_PERSONA_END)}\n*", re.DOTALL,
)

# How many prior turns to replay as preamble on each Telegram message.
# Gemini's context window is huge so this can be generous; the limit is
# really about cost and how much "old conversation" the user wants in scope.
_HISTORY_REPLAY_TURNS = 10

# Cap prompt size — the CLI passes it as a positional arg, which must fit
# in ARG_MAX (~2MB, often less in containers).
_MAX_PROMPT_BYTES = 500_000


@dataclass
class _HistoryTurn:
    """One round-trip exchange that we may replay as context on a later turn."""

    role: str  # "user" or "assistant"
    text: str
    timestamp: float


def _write_mcp_settings(home_dir: str, mcp_servers: dict) -> None:
    """Write Gemini's MCP server configuration to ``{home_dir}/settings.json``."""
    settings_path = Path(home_dir) / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing["mcpServers"] = mcp_servers
    settings_path.write_text(json.dumps(existing, indent=2))


class GeminiExecutor(Executor):
    """Concrete executor that drives Google's Gemini CLI."""

    def __init__(
        self,
        config: GeminiConfig,
        runtime: GeminiRuntimeConfig,
        superpos: SuperposClient | None,
        gateway: TelegramGateway | None,
        persona: str | None = None,
    ) -> None:
        super().__init__(max_parallel=config.executor_max_parallel)
        self._config = config
        self._runtime = runtime
        self._superpos = superpos
        self._gateway = gateway
        self._persona = persona

        # Persona is injected into GEMINI.md in the project root
        self._inject_persona_into_gemini_md()

        # Session marker (kept for /new) + per-chat history files
        self._sessions = SessionStore(
            path=os.path.join(config.home_dir, "session_store.json"),
        )
        self._history_dir = Path(config.home_dir) / "history"
        self._history_dir.mkdir(parents=True, exist_ok=True)
        self._recent_tasks = RecentTasksLog(max_per_chat=5)

        # Concurrency primitives
        self._semaphore = asyncio.Semaphore(config.executor_max_parallel)
        self._worktree_locks: dict[str, asyncio.Lock] = {}

        # Discover modules and merge their MCP servers into ~/.gemini/settings.json
        modules = discover_modules(config.modules_dir)
        mcp = collect_mcp_servers(modules)
        if mcp:
            _write_mcp_settings(config.home_dir, mcp)
            log.info("Wrote %d MCP server(s) to %s", len(mcp), config.home_dir)

    # ── Persona injection ─────────────────────────────────────────────

    def _gemini_md_path(self) -> str:
        return os.path.join(self._config.executor_working_dir, "GEMINI.md")

    def _inject_persona_into_gemini_md(self) -> None:
        """Prepend persona to GEMINI.md so Gemini picks it up as system prompt."""
        if not self._persona:
            return
        path = self._gemini_md_path()
        existing = ""
        if os.path.exists(path):
            with open(path, "r") as f:
                existing = f.read()
        persona_block = (
            f"{_PERSONA_BEGIN}\n"
            f"{self._persona}\n"
            f"{_PERSONA_END}\n\n"
        )
        if _PERSONA_BEGIN in existing:
            existing = _PERSONA_RE.sub(persona_block, existing)
            with open(path, "w") as f:
                f.write(existing)
        else:
            with open(path, "w") as f:
                f.write(persona_block + existing)
        log.info("Injected persona into %s", path)

    def update_persona(self, prompt: str | None, version: int | None = None) -> None:
        """Replace persona and re-inject into GEMINI.md.

        ``version`` is accepted for interface parity but Gemini doesn't track
        per-persona versions — the file is the single source of truth.
        """
        self._persona = prompt
        self._inject_persona_into_gemini_md()

    # ── Session / history management ──────────────────────────────────

    def clear_session(self, chat_id: int | str) -> None:
        """Drop session marker + history file for a chat."""
        self._sessions.clear(chat_id)
        history_path = self._history_path(chat_id)
        try:
            history_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Failed to remove history file %s", history_path, exc_info=True)

    def _history_path(self, chat_id: int | str) -> Path:
        return self._history_dir / f"{chat_id}.jsonl"

    def _load_recent_history(self, chat_id: int | str, max_turns: int) -> list[_HistoryTurn]:
        path = self._history_path(chat_id)
        if not path.exists():
            return []
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        turns: list[_HistoryTurn] = []
        for line in lines[-max_turns:]:
            try:
                row = json.loads(line)
                turns.append(_HistoryTurn(
                    role=row.get("role", ""),
                    text=row.get("text", ""),
                    timestamp=row.get("ts", 0.0),
                ))
            except json.JSONDecodeError:
                continue
        return turns

    def _append_history(self, chat_id: int | str, role: str, text: str) -> None:
        if not text.strip():
            return
        path = self._history_path(chat_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps({
                    "role": role,
                    "text": text[:50_000],  # cap per-turn so the file can't explode
                    "ts": time.time(),
                }) + "\n")
        except OSError:
            log.warning("Failed to append history for chat %s", chat_id, exc_info=True)

    def _render_history_preamble(self, chat_id: int | str) -> str | None:
        """Format the last few turns as a markdown preamble for context."""
        turns = self._load_recent_history(chat_id, _HISTORY_REPLAY_TURNS)
        if not turns:
            return None
        lines = [
            "## Previous Conversation",
            (
                "These messages are from the same Telegram thread; treat them "
                "as your own prior turns (you said the assistant lines)."
            ),
            "",
        ]
        for turn in turns:
            label = "User" if turn.role == "user" else "Assistant"
            text = turn.text.strip().replace("\n", " ")
            if len(text) > 2_000:
                text = text[:2_000] + "…"
            lines.append(f"**{label}:** {text}")
        return "\n".join(lines)

    # ── Preflight ─────────────────────────────────────────────────────

    async def preflight(self) -> None:
        """Verify Gemini CLI is installed and credentials work."""
        log.info("Verifying Gemini authentication...")
        try:
            env = {**os.environ}
            process = await asyncio.create_subprocess_exec(
                "gemini", "--output-format", "json", "--yolo", "hi",
                stdout=PIPE,
                stderr=PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=60,
            )
            if process.returncode != 0:
                stderr_str = stderr.decode(errors="replace")
                lower = stderr_str.lower()
                if any(s in lower for s in (
                    "authentication", "invalid api key", "unauthorized",
                    "permission denied", "not authenticated",
                )):
                    print(_AUTH_HELP_INVALID_KEY, file=sys.stderr)
                    sys.exit(1)
                raise RuntimeError(
                    f"Gemini auth check failed (exit {process.returncode}): "
                    f"{stderr_str[:500]}"
                )
            log.info("Gemini authentication OK")
        except asyncio.TimeoutError:
            log.warning("Gemini auth check timed out (60s) — proceeding anyway")
        except FileNotFoundError:
            log.critical(
                "'gemini' CLI not found on PATH. "
                "Install with: npm install -g @google/gemini-cli"
            )
            sys.exit(1)

    # ── Cleanup ───────────────────────────────────────────────────────

    def cleanup_stale_sessions(self, max_age_hours: int = 24) -> dict[str, int]:
        """Delete old Gemini cache + our history files older than max_age_hours."""
        counts = {"projects": 0, "session_env": 0, "bytes_freed": 0}
        cutoff = time.time() - (max_age_hours * 3600)

        # Our own per-chat history files
        if self._history_dir.is_dir():
            for entry in self._history_dir.iterdir():
                try:
                    if entry.stat().st_mtime < cutoff:
                        size = entry.stat().st_size
                        entry.unlink()
                        counts["projects"] += 1
                        counts["bytes_freed"] += size
                except OSError:
                    pass

        # Gemini CLI's own cache, if present
        cache_dir = Path(self._config.home_dir) / "cache"
        if cache_dir.is_dir():
            for entry in cache_dir.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        size = sum(
                            f.stat().st_size
                            for f in entry.rglob("*")
                            if f.is_file()
                        )
                        shutil.rmtree(entry)
                        counts["session_env"] += 1
                        counts["bytes_freed"] += size
                except OSError:
                    pass

        return counts

    # ── Worktree management ───────────────────────────────────────────

    def _get_worktree_lock(self, slot: str) -> asyncio.Lock:
        if slot not in self._worktree_locks:
            self._worktree_locks[slot] = asyncio.Lock()
        return self._worktree_locks[slot]

    def _resolve_slot(self, req: ExecutionRequest) -> str:
        if (
            req.branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            return worktree_path(self._config.executor_working_dir, req.branch)
        return "__main__"

    # ── Main consumer loop ────────────────────────────────────────────

    async def run(self) -> None:
        log.info(
            "Gemini executor started (max_parallel=%d)",
            self._config.executor_max_parallel,
        )
        while True:
            req = await self.queue.get()
            asyncio.create_task(self._run_one(req))

    async def _run_one(self, req: ExecutionRequest) -> None:
        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None

        # Start heartbeat IMMEDIATELY — before semaphore/worktree waits.
        # This keeps the server-side claim alive while queued.
        if req.source == "superpos" and req.superpos_task_id and self._superpos:
            progress_task = asyncio.create_task(
                self._report_progress(req.superpos_task_id, claim_expired)
            )

        try:
            async with self._semaphore:
                if claim_expired.is_set():
                    log.warning(
                        "Claim expired while waiting for semaphore: %s",
                        req.superpos_task_id,
                    )
                    return

                slot = self._resolve_slot(req)
                wt_lock = self._get_worktree_lock(slot)

                lock_acquired = False
                try:
                    lock_task = asyncio.create_task(wt_lock.acquire())
                    expire_task = asyncio.create_task(claim_expired.wait())
                    done, pending = await asyncio.wait(
                        [lock_task, expire_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                        try:
                            await p
                        except asyncio.CancelledError:
                            pass

                    if claim_expired.is_set():
                        if lock_task in done and lock_task.result():
                            wt_lock.release()
                        log.warning(
                            "Claim expired while waiting for worktree lock: %s",
                            req.superpos_task_id,
                        )
                        return

                    lock_acquired = True
                    await self._execute(req, claim_expired)
                finally:
                    if lock_acquired:
                        wt_lock.release()
        except asyncio.CancelledError:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            log.warning("Spurious CancelledError during execution (suppressed)")
        except Exception:
            log.exception("Execution failed for request: %s", req)
        finally:
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
            if req.superpos_task_id:
                self.remove_superpos_task(req.superpos_task_id)
            self.queue.task_done()

    async def _report_progress(
        self, task_id: str, claim_expired: asyncio.Event, interval: int = 30,
    ) -> None:
        """Send periodic progress updates to keep the Superpos task alive."""
        progress = 5
        while True:
            await asyncio.sleep(interval)
            progress = min(progress + 5, 95)
            try:
                await self._superpos.update_progress(task_id, progress)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    log.warning(
                        "Claim expired for task %s (409); aborting execution",
                        task_id,
                    )
                    claim_expired.set()
                    return
                log.debug("Progress update failed for task %s", task_id)
            except Exception:
                log.debug("Progress update failed for task %s", task_id)

    async def _execute(
        self, req: ExecutionRequest, claim_expired: asyncio.Event, retries: int = 3,
    ) -> None:
        self._active_count += 1
        if self._active_count == 1 and self._superpos:
            try:
                await self._superpos.update_status("busy")
            except Exception:
                log.debug("Failed to set agent status to busy")

        streamer = TelegramStreamer(self._gateway, req.chat_id)
        try:
            await streamer.start()
        except Exception:
            log.debug("Streamer start failed (non-fatal)")

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None:
                inner_task.cancel()

        try:
            inner_task = asyncio.create_task(
                self._execute_inner(req, streamer, retries)
            )
            if req.source == "superpos" and req.superpos_task_id:
                watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                await inner_task
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    log.warning(
                        "Execution aborted: claim expired for superpos task %s",
                        req.superpos_task_id,
                    )
                else:
                    raise
        finally:
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            try:
                await streamer.finish()
            except Exception:
                log.debug("Streamer finish failed (non-fatal)", exc_info=True)
            if req.image_paths:
                for p in req.image_paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            self._active_count -= 1
            if self._active_count == 0 and self._superpos:
                try:
                    await self._superpos.update_status("online")
                except Exception:
                    log.debug("Failed to set agent status to online")

    # ── Background tasks ──────────────────────────────────────────────

    async def run_background(
        self,
        task_id: str,
        prompt: str,
        task_type: str = "dream",
        timeout_seconds: int = 300,
    ) -> None:
        """Execute a background task (dream, knowledge_fillin, …) without streamer."""
        label = task_type.replace("_", " ")
        log.info("%s task %s starting in background", label.capitalize(), task_id)

        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None
        if self._superpos:
            progress_task = asyncio.create_task(
                self._report_progress(task_id, claim_expired)
            )

        full_text = ""

        async def _run_inner() -> None:
            nonlocal full_text
            cmd = self._build_gemini_command(prompt=prompt)
            env = self._build_env()

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=PIPE,
                stderr=PIPE,
                cwd=self._config.executor_working_dir,
                env=env,
                limit=16 * 1024 * 1024,
            )

            try:
                dedup = _EventDeduplicator()
                async for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = dedup.extract_text(event)
                    if text:
                        full_text += text
                await process.wait()
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except (asyncio.TimeoutError, Exception):
                        pass

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None and not inner_task.done():
                inner_task.cancel()

        expired = False
        timed_out = False
        try:
            inner_task = asyncio.create_task(_run_inner())
            watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                await asyncio.wait_for(inner_task, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                log.warning(
                    "%s task %s timed out after %ds — cancelling",
                    label.capitalize(), task_id, timeout_seconds,
                )
                inner_task.cancel()
                try:
                    await inner_task
                except (asyncio.CancelledError, Exception):
                    pass
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    expired = True
                    log.warning(
                        "%s task %s cancelled: claim expired",
                        label.capitalize(), task_id,
                    )
                else:
                    raise

            if expired:
                return

            if timed_out:
                if self._superpos and not claim_expired.is_set():
                    try:
                        await self._superpos.fail_task(
                            task_id,
                            f"{label.capitalize()} timed out after {timeout_seconds}s",
                        )
                    except Exception:
                        log.debug("Failed to mark timed-out task %s", task_id)
                return

            result = full_text[-2000:] if len(full_text) > 2000 else full_text
            summary = {
                "description": f"{label.capitalize()}: automated background task",
                "output_excerpt": full_text[:500] if full_text else None,
            }
            if self._superpos and not claim_expired.is_set():
                await self._superpos.complete_task(task_id, result, summary=summary)
            log.info("%s task %s completed", label.capitalize(), task_id)
        except Exception:
            log.warning("%s task %s failed", label.capitalize(), task_id, exc_info=True)
            if self._superpos and not claim_expired.is_set():
                try:
                    await self._superpos.fail_task(task_id, f"{label.capitalize()} failed")
                except Exception:
                    pass
        finally:
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

    # ── Command construction & inner execute ──────────────────────────

    def _build_env(self) -> dict[str, str]:
        env = {**os.environ}
        if self._config.google_api_key:
            # Gemini CLI accepts GEMINI_API_KEY; some versions also read GOOGLE_API_KEY.
            env.setdefault("GEMINI_API_KEY", self._config.google_api_key)
            env.setdefault("GOOGLE_API_KEY", self._config.google_api_key)
        return env

    def _build_gemini_command(
        self,
        prompt: str,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
    ) -> list[str]:
        """Build the gemini CLI command list."""
        full_prompt = prompt
        if system_prompt_append:
            full_prompt = f"{system_prompt_append}\n\n---\n\n{prompt}"

        cmd = [
            "gemini",
            "--output-format", "json",
            "--yolo",  # auto-accept tool calls; non-interactive
        ]
        if self._runtime.model:
            cmd.extend(["--model", self._runtime.model])
        # Gemini exposes reasoning depth via a config knob; pass it through
        # the prompt preamble if the CLI doesn't accept a flag directly.
        # (Newer CLI versions accept `-c thinking_budget=...`; older ones don't.)
        if self._runtime.effort:
            cmd.extend(["-c", f"thinking_effort={self._runtime.effort}"])
        cmd.append(full_prompt)
        return cmd

    async def _execute_inner(
        self,
        req: ExecutionRequest,
        streamer: TelegramStreamer,
        retries: int,
    ) -> None:
        t0 = time.monotonic()
        full_text = ""

        cwd_override: str | None = None
        if (
            req.branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            try:
                cwd_override = await ensure_worktree(
                    self._config.executor_working_dir, req.branch,
                )
            except Exception:
                log.warning(
                    "Failed to create worktree for branch %r; falling back to default cwd",
                    req.branch, exc_info=True,
                )

        # Build the system_prompt_append: worktree guidance + recent history
        system_prompt_append: str | None = None
        if (
            not req.branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            wt_base = self._config.executor_working_dir
            system_prompt_append = (
                "## Worktree Isolation\n"
                "When this task requires implementing code changes on a new branch:\n"
                f"1. First run `git -C {wt_base} fetch origin` to get latest refs.\n"
                f"2. Choose a branch name, then: `git worktree add {wt_base}/.worktrees/<branch> -b <branch> origin/main`\n"
                f"3. Do all file edits and git operations inside `{wt_base}/.worktrees/<branch>`\n"
                "4. Commit, push the branch, and open a PR from the worktree.\n"
                "IMPORTANT: Always branch from origin/main to avoid inheriting unrelated in-progress work.\n"
                "NEVER create branches from the current HEAD of the main workspace — it may be on an unmerged feature branch.\n"
                "For conversational replies or read-only tasks, skip this entirely."
            )

        # Telegram messages replay recent history; Superpos tasks run fresh.
        # Telegram also gets summaries of recent Superpos tasks so the user
        # can reference notifications from this chat in follow-up messages.
        if req.source == "telegram":
            history_preamble = self._render_history_preamble(req.chat_id)
            if history_preamble:
                system_prompt_append = (
                    f"{system_prompt_append}\n\n{history_preamble}"
                    if system_prompt_append else history_preamble
                )
            recent = self._recent_tasks.render(req.chat_id)
            if recent:
                system_prompt_append = (
                    f"{system_prompt_append}\n\n{recent}"
                    if system_prompt_append else recent
                )

        effective_cwd = cwd_override or self._config.executor_working_dir

        prompt_text = req.prompt
        if req.image_paths:
            image_refs = "\n".join(f"- {p}" for p in req.image_paths)
            prompt_text = (
                f"The user sent these images. Read them first, then respond.\n"
                f"{image_refs}\n\n{prompt_text}"
            )

        if len(prompt_text) > _MAX_PROMPT_BYTES:
            log.warning("Prompt too large (%dKB), truncating", len(prompt_text) // 1024)
            prompt_text = prompt_text[:_MAX_PROMPT_BYTES] + "\n... (truncated)"

        for attempt in range(1, retries + 1):
            try:
                cmd = self._build_gemini_command(
                    prompt=prompt_text,
                    cwd=effective_cwd,
                    system_prompt_append=system_prompt_append,
                )
                env = self._build_env()

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=PIPE,
                    stderr=PIPE,
                    cwd=effective_cwd,
                    env=env,
                    limit=16 * 1024 * 1024,
                )

                stderr_chunks: list[bytes] = []
                json_errors: list[str] = []

                log.debug("Running gemini command: %s (cwd=%s)", cmd, effective_cwd)

                async def _drain_stdout():
                    nonlocal full_text
                    dedup = _EventDeduplicator()
                    async for line in process.stdout:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            log.debug("Non-JSON line from gemini: %s", line[:200])
                            continue

                        if event.get("type") == "error":
                            json_errors.append(event.get("message", ""))
                        err_info = event.get("error")
                        if isinstance(err_info, dict):
                            json_errors.append(err_info.get("message", ""))

                        text = dedup.extract_text(event)
                        if text:
                            full_text += text
                            await streamer.append(text)

                        tool_info = dedup.extract_tool_use(event)
                        if tool_info:
                            await streamer.send_tool_notification(*tool_info)

                drain_task = asyncio.create_task(_drain_stdout())
                wait_task = asyncio.create_task(process.wait())

                _MAX_EXECUTION_SECS = 30 * 60
                try:
                    done, pending = await asyncio.wait_for(
                        asyncio.wait(
                            [drain_task, wait_task],
                            return_when=asyncio.ALL_COMPLETED,
                        ),
                        timeout=_MAX_EXECUTION_SECS,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "Gemini execution timed out after %ds — killing process (pid=%s)",
                        _MAX_EXECUTION_SECS, process.pid,
                    )
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    pending = {drain_task, wait_task}

                for p in pending:
                    if not p.done():
                        p.cancel()
                        try:
                            await asyncio.wait_for(p, timeout=5)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass

                if process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass

                try:
                    stderr_data = await asyncio.wait_for(
                        process.stderr.read(), timeout=10,
                    )
                    if stderr_data:
                        stderr_chunks.append(stderr_data)
                except asyncio.TimeoutError:
                    log.warning("Timed out reading stderr from gemini process")
                stderr_str = b"".join(stderr_chunks).decode(errors="replace")

                if not stderr_str.strip() and json_errors:
                    stderr_str = " | ".join(filter(None, json_errors))

                if process.returncode != 0:
                    raise _GeminiProcessError(process.returncode, stderr_str)

                await streamer.finish()

                # Persist the round-trip in chat history for next-turn context
                if req.source == "telegram":
                    self._append_history(req.chat_id, "user", req.prompt)
                    if full_text:
                        self._append_history(req.chat_id, "assistant", full_text)

                # Complete Superpos task if applicable
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    result = full_text[-2000:] if len(full_text) > 2000 else full_text
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "output_excerpt": full_text[:500] if full_text else None,
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.complete_task(
                            req.superpos_task_id, result, summary=summary,
                        )
                    except Exception:
                        log.warning(
                            "Failed to complete superpos task %s — claim may have expired",
                            req.superpos_task_id, exc_info=True,
                        )
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="succeeded",
                            detail=full_text[:500] if full_text else "",
                        ),
                    )
                return

            except _GeminiProcessError as e:
                err_str = str(e)
                lower = err_str.lower()
                is_rate_limit = (
                    "rate_limit" in lower
                    or "rate limit" in lower
                    or "quota" in lower
                    or "resource_exhausted" in lower
                    or "at capacity" in lower
                    or "overloaded" in lower
                )
                is_auth_error = (
                    "authentication" in lower
                    or "invalid api key" in lower
                    or "unauthorized" in lower
                    or "permission denied" in lower
                    or "not authenticated" in lower
                )

                if is_auth_error:
                    log.critical(
                        "Gemini authentication failed — API key invalid or not configured. "
                        "Shutting down."
                    )
                    sys.exit(1)

                is_api_500 = (
                    "internal server error" in lower
                    or "api_error" in lower
                    or "overloaded" in lower
                    or "service unavailable" in lower
                )
                if is_api_500 and attempt < retries:
                    wait = 30 * attempt
                    log.warning(
                        "API server error (attempt %d/%d), retrying in %ds: %s",
                        attempt, retries, wait, err_str[:100],
                    )
                    await streamer.append(f"\n⏳ API error, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue

                # Don't retry if execution already produced output — side
                # effects (commits, PRs, etc.) cannot be undone.
                if full_text.strip():
                    log.warning(
                        "Execution produced output but failed (attempt %d/%d); "
                        "not retrying to avoid duplicate side effects",
                        attempt, retries,
                    )
                elif is_rate_limit and attempt < retries:
                    wait = 30 * attempt
                    log.warning(
                        "Rate limited (attempt %d/%d), retrying in %ds",
                        attempt, retries, wait,
                    )
                    await streamer.append(f"\n⏳ Rate limited, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue

                log.error("Gemini process error (exit %d): %s", e.returncode, e.stderr)
                try:
                    await streamer.error(f"Error: {e}")
                except asyncio.CancelledError:
                    log.warning(
                        "CancelledError while sending error to Telegram (suppressed)"
                    )
                except Exception:
                    log.warning("Failed to send error notification", exc_info=True)
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "error": err_str[:500],
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.fail_task(
                            req.superpos_task_id, err_str, summary=summary,
                        )
                    except Exception:
                        log.warning(
                            "Failed to mark superpos task %s as failed",
                            req.superpos_task_id,
                        )
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="failed",
                            detail=err_str[:500],
                        ),
                    )
                return

            except Exception as e:
                err_str = str(e)
                log.exception("Unexpected error during execution")
                try:
                    await streamer.error(f"Error: {e}")
                except asyncio.CancelledError:
                    log.warning(
                        "CancelledError while sending error to Telegram (suppressed)"
                    )
                except Exception:
                    log.warning("Failed to send error notification", exc_info=True)
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "error": err_str[:500],
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.fail_task(
                            req.superpos_task_id, err_str, summary=summary,
                        )
                    except Exception:
                        log.warning(
                            "Failed to mark superpos task %s as failed",
                            req.superpos_task_id,
                        )
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="failed",
                            detail=err_str[:500],
                        ),
                    )
                return


# ─────────────────────────────────────────────────────────────────────
#  Event parsing & deduplication for Gemini CLI's JSONL output
# ─────────────────────────────────────────────────────────────────────


class _EventDeduplicator:
    """Filters duplicate text and tool events from Gemini's JSONL stream.

    Like every modern assistant CLI, Gemini emits overlapping events: streaming
    deltas AND a completed message summary containing the same text.  This
    class prefers deltas (lower latency) and skips the trailing duplicates.

    The exact event shape is still moving with Gemini CLI's versions; this
    extractor handles the patterns we've seen and falls back to ignoring
    unknown events.
    """

    def __init__(self) -> None:
        self._saw_delta = False
        self._seen_tool_keys: set[str] = set()

    def extract_text(self, event: dict) -> str:
        etype = event.get("type", "")

        if etype in ("response.created", "response.started", "turn.started"):
            self._saw_delta = False
            return ""

        # Streaming deltas — lowest latency, prefer these
        if etype in (
            "response.output_text.delta",
            "content_block_delta",
            "text_delta",
        ):
            self._saw_delta = True
            return event.get("delta", event.get("text", ""))

        if etype == "text" and "text" in event:
            self._saw_delta = True
            return event["text"]

        # Completed assistant message — only use if no deltas were seen
        if etype == "message" and event.get("role") == "assistant":
            if self._saw_delta:
                return ""
            parts = []
            for block in event.get("content", []):
                if isinstance(block, dict) and block.get("type") in (
                    "output_text", "text",
                ):
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)

        # Gemini CLI's "agent_message" shape (mirrors Codex CLI naming)
        if etype == "item.completed":
            if self._saw_delta:
                return ""
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                return item.get("text", "")

        return ""

    def extract_tool_use(self, event: dict) -> tuple[str, object] | None:
        etype = event.get("type", "")

        name: str | None = None
        args: object = {}
        call_id = ""

        if etype in ("function_call", "tool_call", "tool_use"):
            name = event.get("name", event.get("function", {}).get("name", "unknown"))
            args = event.get(
                "input",
                event.get(
                    "arguments",
                    event.get("function", {}).get("arguments", {}),
                ),
            )
            call_id = event.get("call_id", event.get("id", ""))
        elif etype == "item.started":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") in (
                "function_call",
                "tool_call",
                "tool_use",
            ):
                name = item.get("name", "unknown")
                args = item.get("input", item.get("arguments", {}))
                call_id = item.get("call_id", item.get("id", ""))
            elif isinstance(item, dict) and item.get("type") == "command_execution":
                cmd = item.get("command", "")
                if cmd.startswith("/bin/bash -lc '") and cmd.endswith("'"):
                    cmd = cmd[15:-1]
                elif cmd.startswith("/bin/bash -lc "):
                    cmd = cmd[14:]
                name = "run_shell_command"
                args = {"command": cmd}
                call_id = item.get("call_id", item.get("id", ""))
            else:
                return None
        else:
            return None

        if name is None:
            return None

        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}

        if call_id:
            dedup_key = f"id:{call_id}"
        else:
            args_str = str(args)[:200]
            dedup_key = f"na:{name}:{args_str}"

        if dedup_key in self._seen_tool_keys:
            return None
        self._seen_tool_keys.add(dedup_key)

        return (name, args)


class _GeminiProcessError(Exception):
    """Raised when the gemini subprocess exits with non-zero status."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"gemini process exited with code {returncode}: {stderr[:500]}"
        )


# Auth help message printed when preflight detects bad credentials.
_AUTH_HELP_INVALID_KEY = """
╔══════════════════════════════════════════════════════════════╗
║         Gemini authentication failed — cannot start          ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Option 1 — OAuth (gemini auth):                             ║
║                                                              ║
║    docker run -it \\                                          ║
║      -v gemini_auth:/home/agent/.gemini \\                    ║
║      --entrypoint gemini slim-gemini-agent auth login        ║
║                                                              ║
║    Follow the prompts to authenticate.                       ║
║    Then restart the agent (keep the -v flag).                ║
║                                                              ║
║  Option 2 — API key:                                         ║
║                                                              ║
║    Set GEMINI_API_KEY=... in your .env file.                 ║
║    Get a key from https://aistudio.google.com/app/apikey     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""
