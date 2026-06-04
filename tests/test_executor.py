"""Executor-level async tests for report_progress integration.

These tests verify that GeminiExecutor correctly wires up the
``report_progress`` coroutine from superpos_agent_core and that
claim-expiry cancellation propagates to in-flight work.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch, MagicMock

from superpos_agent_gemini import GeminiConfig, GeminiExecutor, GeminiRuntimeConfig
from superpos_agent_core import ExecutionRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(tmpdir: str, superpos: AsyncMock | None = None):
    """Build a GeminiExecutor with all external I/O patched out."""
    config = GeminiConfig(
        home_dir=tmpdir,
        gemini_model="gemini-2.5-pro",
        gemini_reasoning_effort="medium",
        executor_max_parallel=1,
        executor_working_dir=tmpdir,
    )
    runtime = GeminiRuntimeConfig(
        model="gemini-2.5-pro",
        effort="medium",
        path=os.path.join(tmpdir, "rt.json"),
    )
    with patch.object(GeminiExecutor, "_inject_persona_into_gemini_md"):
        executor = GeminiExecutor(
            config, runtime, superpos=superpos, gateway=None,
        )
    return executor


def _make_superpos_request(task_id: str = "task-123", prompt: str = "hello") -> ExecutionRequest:
    """Create an ExecutionRequest that looks like it came from Superpos."""
    return ExecutionRequest(
        prompt=prompt,
        chat_id="chat-1",
        source="superpos",
        superpos_task_id=task_id,
    )


# ---------------------------------------------------------------------------
# Test 1 — Happy path: report_progress is wired with correct arguments
# ---------------------------------------------------------------------------

async def test_run_background_calls_report_progress_with_correct_args():
    """run_background() should spawn report_progress(client, task_id, event)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        superpos = AsyncMock()
        superpos.complete_task = AsyncMock()
        superpos.fail_task = AsyncMock()
        superpos.update_status = AsyncMock()
        executor = _make_executor(tmpdir, superpos=superpos)

        captured_args: list = []

        async def fake_report_progress(client, task_id, claim_expired, **kwargs):
            """Capture the arguments and then just wait until cancelled."""
            captured_args.append((client, task_id, claim_expired))
            try:
                # Sit forever until the caller cancels us
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

        # Patch the agy subprocess to return immediately with empty output
        fake_process = AsyncMock()
        fake_process.stdout = AsyncMock()
        fake_process.stdout.read = AsyncMock(return_value=b"")
        fake_process.stderr = AsyncMock()
        fake_process.stderr.read = AsyncMock(return_value=b"")
        fake_process.wait = AsyncMock(return_value=0)
        fake_process.returncode = 0
        fake_process.pid = 999

        with patch(
            "superpos_agent_gemini.gemini_executor.report_progress",
            side_effect=fake_report_progress,
        ), patch(
            "asyncio.create_subprocess_exec",
            return_value=fake_process,
        ):
            await executor.run_background(
                task_id="task-abc",
                prompt="do something",
                task_type="dream",
                timeout_seconds=10,
            )

        # report_progress should have been called exactly once
        assert len(captured_args) == 1, (
            f"Expected report_progress called once, got {len(captured_args)}"
        )
        rp_client, rp_task_id, rp_event = captured_args[0]
        assert rp_client is superpos, "First arg should be the SuperposClient"
        assert rp_task_id == "task-abc", f"Second arg should be the task_id, got {rp_task_id!r}"
        assert isinstance(rp_event, asyncio.Event), "Third arg should be an asyncio.Event"


# ---------------------------------------------------------------------------
# Test 2 — Claim-expiry cancellation: inner work is aborted
# ---------------------------------------------------------------------------

async def test_run_background_cancels_inner_on_claim_expiry():
    """When report_progress signals claim expiry, the inner work should be cancelled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        superpos = AsyncMock()
        superpos.complete_task = AsyncMock()
        superpos.fail_task = AsyncMock()
        superpos.update_status = AsyncMock()
        executor = _make_executor(tmpdir, superpos=superpos)

        async def fake_report_progress_expire(client, task_id, claim_expired, **kwargs):
            """Immediately set claim_expired so the executor aborts."""
            claim_expired.set()

        # The subprocess should block long enough that expiry fires first.
        inner_started = asyncio.Event()

        async def slow_read(n=-1):
            inner_started.set()
            # Block until cancelled — simulates a long-running CLI
            await asyncio.Event().wait()

        fake_process = AsyncMock()
        fake_process.stdout = AsyncMock()
        fake_process.stdout.read = AsyncMock(side_effect=slow_read)
        fake_process.stderr = AsyncMock()
        fake_process.stderr.read = AsyncMock(return_value=b"")
        fake_process.wait = AsyncMock(return_value=0)
        fake_process.returncode = None  # process hasn't exited yet
        fake_process.pid = 999
        fake_process.kill = MagicMock()

        with patch(
            "superpos_agent_gemini.gemini_executor.report_progress",
            side_effect=fake_report_progress_expire,
        ), patch(
            "asyncio.create_subprocess_exec",
            return_value=fake_process,
        ):
            await executor.run_background(
                task_id="task-xyz",
                prompt="long running task",
                task_type="dream",
                timeout_seconds=30,
            )

        # The key assertion: complete_task should NOT have been called
        # because the claim expired and the work was cancelled.
        superpos.complete_task.assert_not_called()
