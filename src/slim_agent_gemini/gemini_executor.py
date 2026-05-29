"""Queue-based worker that invokes Google's Antigravity (`agy`) CLI and
routes output.

CLI integration notes
---------------------
This executor wraps the ``agy`` command from Google's Antigravity CLI
(installed in the Docker image via the vendored ``install-agy.sh`` —
a static native Go binary, no node runtime). Key assumptions (verify
against ``agy --help`` if anything breaks):

* ``agy --print --dangerously-skip-permissions "<prompt>"`` runs the
  prompt non-interactively and prints the response as plain text to
  stdout. ``--print`` (alias: ``-p``) is the non-interactive entry;
  ``--dangerously-skip-permissions`` auto-approves tool calls so the
  agent never blocks on a confirmation prompt.
* ``agy`` does NOT support a ``--model`` flag — model selection happens
  inside the CLI based on Antigravity's current default (gemini-3.0+).
  The ``model``/``effort`` runtime knobs on :class:`GeminiRuntimeConfig`
  are kept for ``/model``-command compatibility but the CLI ignores them.
* Output is plain text (NOT JSONL like ``@google/gemini-cli`` used to
  emit). We stream stdout directly to the Telegram streamer without
  parsing — there are no tool-use events on stdout in ``--print`` mode
  (agy logs tool activity to ``~/.gemini/antigravity-cli/cli.log``).
* MCP server config lives at ``~/.gemini/antigravity-cli/mcp_config.json``
  under the ``mcpServers`` key (same schema as gemini-cli, different path).
* Authentication is OAuth-based — the user must complete an interactive
  ``agy --prompt-interactive 'hi'`` login once. The resulting OAuth
  token persists under ``~/.gemini/antigravity-cli/`` which is
  bind-mounted out of the container so it survives restarts. There is
  NO ``GEMINI_API_KEY``-style env var auth mode for ``agy``.

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

from superpos_agent_core import (
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
    """Write the agy MCP server configuration.

    ``agy`` looks for MCP servers in ``~/.gemini/antigravity-cli/mcp_config.json``
    under the same ``mcpServers`` schema that ``@google/gemini-cli`` used.
    The path differs from gemini-cli's ``~/.gemini/settings.json`` — agy's
    own settings.json (colour scheme, trusted workspaces, etc.) is a
    separate file and is not the MCP config sink.
    """
    settings_path = Path(home_dir) / "antigravity-cli" / "mcp_config.json"
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
        """Verify agy CLI is installed and OAuth credentials work."""
        log.info("Verifying agy authentication...")
        try:
            env = {**os.environ}
            process = await asyncio.create_subprocess_exec(
                "agy", "--print", "--dangerously-skip-permissions", "hi",
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
                    "authentication", "unauthorized", "permission denied",
                    "not authenticated", "login", "sign in", "oauth",
                )):
                    print(_AUTH_HELP_INVALID_KEY, file=sys.stderr)
                    sys.exit(1)
                raise RuntimeError(
                    f"agy auth check failed (exit {process.returncode}): "
                    f"{stderr_str[:500]}"
                )
            log.info("agy authentication OK")
        except asyncio.TimeoutError:
            log.warning("agy auth check timed out (60s) — proceeding anyway")
        except FileNotFoundError:
            log.critical(
                "'agy' CLI not found on PATH. "
                "Install via the vendored installer: "
                "bash install-agy.sh -d /usr/local/bin"
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
            # Register with the base class so the /stop Telegram command can
            # find and cancel this in-flight work via cancel_chat(chat_id).
            # Auto-untracks via done callback — no cleanup needed in finally.
            self._track_chat_task(req.chat_id, inner_task)
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
                # ``agy --print`` emits plain text on stdout (NOT JSONL like
                # gemini-cli used to). Stream chunks straight into the
                # accumulator — no event parsing or dedup needed.
                while True:
                    chunk = await process.stdout.read(4096)
                    if not chunk:
                        break
                    full_text += chunk.decode(errors="replace")
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
        # `agy` uses OAuth (token persisted under ~/.gemini/antigravity-cli/);
        # there is no API-key env-var auth mode. ``google_api_key`` on
        # :class:`GeminiConfig` is therefore unused by the CLI — kept on the
        # dataclass only so existing operators with the env var set don't get
        # a missing-attribute error during config load.
        return env

    def _build_gemini_command(
        self,
        prompt: str,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
    ) -> list[str]:
        """Build the agy CLI command list.

        Name kept as ``_build_gemini_command`` for backwards compatibility
        with anything that patched / overrode it — semantically still
        "build the command that drives a Gemini model", just via the agy
        binary instead of the deprecated gemini-cli npm wrapper.
        """
        full_prompt = prompt
        if system_prompt_append:
            full_prompt = f"{system_prompt_append}\n\n---\n\n{prompt}"

        # ``agy`` has no --output-format / --model / -c knobs in print
        # mode — model and reasoning effort are baked into the CLI's
        # current default. The runtime knobs on GeminiRuntimeConfig are
        # accepted for /model command parity but the CLI ignores them.
        cmd = [
            "agy",
            "--print",
            "--dangerously-skip-permissions",
            full_prompt,
        ]
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

                log.debug("Running agy command: %s (cwd=%s)", cmd, effective_cwd)

                async def _drain_stdout():
                    nonlocal full_text
                    # ``agy --print`` streams plain text on stdout. Forward
                    # chunks straight to the Telegram streamer — no JSON-
                    # event parsing, no dedup, and no tool-use extraction
                    # (agy logs tool activity to its own log file under
                    # ~/.gemini/antigravity-cli/, not to stdout in --print
                    # mode). The chunked read here keeps per-token latency
                    # roughly the same as iterating by line did for
                    # gemini-cli's JSONL output.
                    while True:
                        chunk = await process.stdout.read(4096)
                        if not chunk:
                            break
                        text = chunk.decode(errors="replace")
                        full_text += text
                        await streamer.append(text)

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
                    raise _AgyProcessError(process.returncode, stderr_str)

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

            except _AgyProcessError as e:
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


# _EventDeduplicator was previously used to parse the JSONL stream emitted
# by ``@google/gemini-cli`` (streaming deltas + completed message events
# overlapped, so we deduplicated). ``agy --print`` emits plain text on
# stdout instead — no events, nothing to dedup. The drain functions
# above just append raw chunks to the streamer, so this class is no
# longer referenced. Removed in the gemini→agy vendor swap.


class _AgyProcessError(Exception):
    """Raised when the agy subprocess exits with non-zero status."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"agy process exited with code {returncode}: {stderr[:500]}"
        )


# Auth help message printed when preflight detects bad credentials.
# ``agy`` uses Google OAuth exclusively — there is no API-key fallback.
# The token persists under ~/.gemini/antigravity-cli/ which the compose
# file bind-mounts out of the container so this is a one-time setup.
_AUTH_HELP_INVALID_KEY = """
╔══════════════════════════════════════════════════════════════╗
║       agy authentication failed — cannot start               ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  agy uses Google OAuth (no API-key env var). One-time setup: ║
║                                                              ║
║    docker compose run --rm -it \\                             ║
║      --entrypoint agy gemini --prompt-interactive 'hi'       ║
║                                                              ║
║  Follow the URL it prints to sign in with the Google         ║
║  account that has Antigravity access. The OAuth token is     ║
║  written to ~/.gemini/antigravity-cli/ inside the container, ║
║  which the compose volume bind-mounts so it persists across  ║
║  restarts. After completing the flow once, the agent can     ║
║  start normally.                                             ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""
