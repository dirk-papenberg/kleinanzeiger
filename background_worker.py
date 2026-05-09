"""Background worker for processing queued jobs."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Awaitable

from queue_manager import QueueManager

log = logging.getLogger("kleinanzeigen-agent.worker")


class BackgroundWorker:
    """Processes jobs from the queue in the background."""

    def __init__(
        self,
        queue_manager: QueueManager,
        job_handlers: dict[str, Callable[[dict], Awaitable[tuple[bool, str]]]],
        on_job_completed: Callable[[str, int, bool, str], Awaitable[None]] | None = None,
    ):
        """
        Initialize worker.

        Args:
            queue_manager: The QueueManager instance
            job_handlers: Dict mapping job_type -> async handler function.
                         Handler should return (success, message).
            on_job_completed: Optional async callback called when job completes.
                            Signature: async def(job_id: str, chat_id: int, success: bool, message: str)
        """
        self.queue_manager = queue_manager
        self.job_handlers = job_handlers
        self.on_job_completed = on_job_completed
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background worker loop."""
        if self._running:
            log.warning("Worker already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._worker_loop())
        log.info("Background worker started")

    async def stop(self) -> None:
        """Stop the background worker loop."""
        self._running = False
        if self._task:
            await self._task
        log.info("Background worker stopped")

    async def _worker_loop(self) -> None:
        """Main worker loop: process jobs from queue."""
        while self._running:
            try:
                job = self.queue_manager.get_next_job()
                if not job:
                    # No pending jobs, sleep and retry
                    await asyncio.sleep(2)
                    continue

                log.info(
                    "[job=%s chat=%d] Processing: type=%s",
                    job.job_id, job.chat_id, job.job_type,
                )
                self.queue_manager.mark_processing(job.job_id)

                # Get the handler for this job type
                handler = self.job_handlers.get(job.job_type)
                if not handler:
                    error_msg = f"Unknown job type: {job.job_type}"
                    self.queue_manager.mark_failed(
                        job.job_id,
                        error_msg,
                        is_backout=True,
                    )
                    if self.on_job_completed:
                        await self.on_job_completed(job.job_id, job.chat_id, False, error_msg)
                    continue

                # Execute the handler
                try:
                    success, message = await handler(job.data)
                    if success:
                        self.queue_manager.mark_completed(job.job_id)
                        log.info("[job=%s] Completed: %s", job.job_id, message)
                    else:
                        self.queue_manager.mark_failed(job.job_id, message)
                        log.warning("[job=%s] Failed: %s", job.job_id, message)
                    
                    # Notify on completion (success or failure)
                    if self.on_job_completed:
                        await self.on_job_completed(job.job_id, job.chat_id, success, message)
                        
                except Exception as e:
                    error_msg = f"Handler exception: {e}"
                    self.queue_manager.mark_failed(job.job_id, error_msg)
                    log.exception("[job=%s] Handler exception", job.job_id)
                    if self.on_job_completed:
                        await self.on_job_completed(job.job_id, job.chat_id, False, error_msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Worker loop exception")
                await asyncio.sleep(5)  # Backoff on unexpected error

        log.info("Worker loop ended")
