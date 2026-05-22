import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import asyncpg

from app.transcoder import TranscodeOptions, TranscodeResult


class JobStore:
    """PostgreSQL-backed job store. Queues and process handles remain in-memory (runtime-only)."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._processes: dict = {}

    def set_pool(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @property
    def _db(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("JobStore pool not set — call set_pool() first")
        return self._pool

    async def reset_stale(self) -> None:
        """Fail any jobs left in a non-terminal state from a previous server run."""
        await self._db.execute("""
            UPDATE jobs
            SET status = 'failed',
                error = 'Server restarted while job was active',
                completed_at = now()
            WHERE status IN ('queued', 'processing')
        """)

    async def create(self, job_id: str, filename: str, options: TranscodeOptions) -> dict:
        stem = Path(filename).stem
        output_filename = f"{stem}_transcoded.{options.output_format}"
        await self._db.execute("""
            INSERT INTO jobs (job_id, status, original_filename, output_filename, options)
            VALUES ($1, 'queued', $2, $3, $4::jsonb)
        """, job_id, filename, output_filename, json.dumps(asdict(options)))
        self._queues[job_id] = asyncio.Queue(maxsize=200)
        return await self.get(job_id)

    async def get(self, job_id: str) -> Optional[dict]:
        row = await self._db.fetchrow("SELECT * FROM jobs WHERE job_id = $1", job_id)
        return _row_to_dict(row) if row else None

    async def set_processing(self, job_id: str) -> None:
        await self._db.execute(
            "UPDATE jobs SET status = 'processing' WHERE job_id = $1", job_id
        )

    async def set_completed(self, job_id: str, output_path: str, result: TranscodeResult) -> None:
        await self._db.execute("""
            UPDATE jobs
            SET status = 'completed', output_path = $2, completed_at = now(),
                input_size = $3, output_size = $4, compression_ratio = $5,
                size_reduction_pct = $6, duration_seconds = $7
            WHERE job_id = $1
        """, job_id, output_path, result.input_size, result.output_size,
            result.compression_ratio, result.size_reduction_pct, result.duration_seconds)

    async def set_failed(self, job_id: str, error: str) -> None:
        await self._db.execute("""
            UPDATE jobs SET status = 'failed', error = $2, completed_at = now()
            WHERE job_id = $1
        """, job_id, error)

    async def list_jobs(self) -> list[dict]:
        rows = await self._db.fetch("SELECT * FROM jobs ORDER BY created_at DESC")
        return [_row_to_dict(r) for r in rows]

    def get_queue(self, job_id: str) -> asyncio.Queue | None:
        return self._queues.get(job_id)

    def set_process(self, job_id: str, proc) -> None:
        self._processes[job_id] = proc

    async def set_blobs(self, job_id: str, input_blob: str, output_blob: str) -> None:
        await self._db.execute("""
            UPDATE jobs SET input_blob = $2, output_blob = $3 WHERE job_id = $1
        """, job_id, input_blob, output_blob)

    async def cancel(self, job_id: str) -> None:
        await self._db.execute("""
            UPDATE jobs SET status = 'cancelled', completed_at = now()
            WHERE job_id = $1 AND status IN ('queued', 'processing')
        """, job_id)
        proc = self._processes.pop(job_id, None)
        if proc and proc.returncode is None:
            proc.kill()


def _row_to_dict(row: asyncpg.Record) -> dict:
    d = dict(row)
    d["job_id"] = str(d["job_id"])
    for key in ("created_at", "completed_at"):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    return d
