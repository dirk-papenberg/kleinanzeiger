"""Task queue manager for background job processing with retry logic."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Any

log = logging.getLogger("kleinanzeigen-agent.queue")


class JobStatus(Enum):
    """Job lifecycle status."""
    PENDING = "pending"           # Queued, waiting to execute
    PROCESSING = "processing"     # Currently being processed
    COMPLETED = "completed"       # Successfully completed
    FAILED = "failed"            # Failed, needs retry
    BACKOUT = "backout"          # Failed repeatedly or user not logged in


@dataclass
class QueuedJob:
    """A job that needs to be processed."""
    job_id: str                          # Unique ID
    chat_id: int                         # Telegram chat ID
    job_type: str                        # "publish_ad", etc.
    data: dict = field(default_factory=dict)  # Job-specific data
    status: str = JobStatus.PENDING.value
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 3


class QueueManager:
    """Manages task queue with file-based persistence."""

    def __init__(
        self,
        queue_dir: Path,
        pending_file: str = "pending.jsonl",
        backout_file: str = "backout.jsonl",
        completed_file: str = "completed.jsonl",
    ):
        self.queue_dir = Path(queue_dir)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.pending_file = self.queue_dir / pending_file
        self.backout_file = self.queue_dir / backout_file
        self.completed_file = self.queue_dir / completed_file

        # In-memory pending queue (loaded from disk on init)
        self.pending: dict[str, QueuedJob] = {}
        self.backout: dict[str, QueuedJob] = {}

        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Load pending and backout jobs from disk."""
        self.pending.clear()
        self.backout.clear()

        if self.pending_file.exists():
            for line in self.pending_file.read_text().splitlines():
                if line.strip():
                    try:
                        data = json.loads(line)
                        job = self._dict_to_job(data)
                        self.pending[job.job_id] = job
                    except Exception as e:
                        log.error("Failed to load pending job: %s", e)

        if self.backout_file.exists():
            for line in self.backout_file.read_text().splitlines():
                if line.strip():
                    try:
                        data = json.loads(line)
                        job = self._dict_to_job(data)
                        self.backout[job.job_id] = job
                    except Exception as e:
                        log.error("Failed to load backout job: %s", e)

    def _save_to_disk(self) -> None:
        """Persist pending and backout jobs to disk."""
        # Write pending jobs
        pending_lines = [
            json.dumps(asdict(job), ensure_ascii=False)
            for job in self.pending.values()
        ]
        self.pending_file.write_text("\n".join(pending_lines) + "\n" if pending_lines else "")

        # Write backout jobs
        backout_lines = [
            json.dumps(asdict(job), ensure_ascii=False)
            for job in self.backout.values()
        ]
        self.backout_file.write_text("\n".join(backout_lines) + "\n" if backout_lines else "")

    @staticmethod
    def _dict_to_job(data: dict) -> QueuedJob:
        """Convert dict back to QueuedJob dataclass."""
        return QueuedJob(
            job_id=data["job_id"],
            chat_id=data["chat_id"],
            job_type=data["job_type"],
            data=data.get("data", {}),
            status=data.get("status", JobStatus.PENDING.value),
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
        )

    def enqueue(
        self,
        job_id: str,
        chat_id: int,
        job_type: str,
        data: dict,
        max_retries: int = 3,
    ) -> QueuedJob:
        """Add a new job to the queue."""
        job = QueuedJob(
            job_id=job_id,
            chat_id=chat_id,
            job_type=job_type,
            data=data,
            max_retries=max_retries,
        )
        self.pending[job_id] = job
        self._save_to_disk()
        log.info(
            "[job=%s chat=%d] Enqueued: type=%s",
            job_id, chat_id, job_type,
        )
        return job

    def get_next_job(self) -> QueuedJob | None:
        """Get the next pending job from the queue."""
        for job in self.pending.values():
            if job.status == JobStatus.PENDING.value:
                return job
        return None

    def mark_processing(self, job_id: str) -> bool:
        """Mark a job as currently processing."""
        job = self.pending.get(job_id)
        if job:
            job.status = JobStatus.PROCESSING.value
            job.started_at = datetime.utcnow().isoformat()
            self._save_to_disk()
            return True
        return False

    def mark_completed(self, job_id: str) -> bool:
        """Mark a job as successfully completed."""
        job = self.pending.pop(job_id, None)
        if job:
            job.status = JobStatus.COMPLETED.value
            job.completed_at = datetime.utcnow().isoformat()
            # Append to completed file
            line = json.dumps(asdict(job), ensure_ascii=False)
            self.completed_file.write_text(
                self.completed_file.read_text() + line + "\n"
                if self.completed_file.exists()
                else line + "\n"
            )
            self._save_to_disk()
            log.info("[job=%s] Marked completed", job_id)
            return True
        return False

    def mark_failed(
        self,
        job_id: str,
        error: str,
        is_backout: bool = False,
    ) -> bool:
        """
        Mark a job as failed. If is_backout=True or retry limit exceeded, move to backout.
        Otherwise, keep in pending for retry.
        """
        job = self.pending.get(job_id)
        if not job:
            return False

        job.error = error
        job.retry_count += 1

        should_backout = is_backout or job.retry_count >= job.max_retries

        if should_backout:
            # Move to backout
            self.pending.pop(job_id)
            job.status = JobStatus.BACKOUT.value
            self.backout[job_id] = job
            log.warning(
                "[job=%s chat=%d] Moved to backout: %s",
                job_id, job.chat_id, error,
            )
        else:
            # Keep in pending for retry
            job.status = JobStatus.PENDING.value
            log.warning(
                "[job=%s chat=%d] Marked failed (retry %d/%d): %s",
                job_id, job.chat_id, job.retry_count, job.max_retries, error,
            )

        self._save_to_disk()
        return True

    def get_backout_jobs(self, chat_id: int | None = None) -> list[QueuedJob]:
        """Get all backout jobs, optionally filtered by chat_id."""
        jobs = list(self.backout.values())
        if chat_id is not None:
            jobs = [j for j in jobs if j.chat_id == chat_id]
        return sorted(jobs, key=lambda j: j.created_at)

    def retry_backout_job(self, job_id: str) -> bool:
        """Move a backout job back to pending for retry."""
        job = self.backout.pop(job_id, None)
        if job:
            job.status = JobStatus.PENDING.value
            job.retry_count = 0
            job.error = None
            self.pending[job_id] = job
            self._save_to_disk()
            log.info("[job=%s] Retried from backout", job_id)
            return True
        return False

    def get_pending_count(self) -> int:
        """Get number of pending jobs."""
        return sum(
            1 for j in self.pending.values()
            if j.status == JobStatus.PENDING.value
        )

    def get_backout_count(self) -> int:
        """Get number of backout jobs."""
        return len(self.backout)
