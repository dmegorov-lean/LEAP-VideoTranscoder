import asyncio
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from app.transcoder import TranscodeOptions, TranscodeResult


class JobStore:
    """In-memory job store. Replace with Redis/DB for multi-instance deployments."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._processes: dict = {}

    def create(self, job_id: str, filename: str, options: TranscodeOptions) -> dict:
        stem = Path(filename).stem
        job: dict = {
            "job_id": job_id,
            "status": "queued",
            "original_filename": filename,
            "output_filename": f"{stem}_transcoded.{options.output_format}",
            "options": asdict(options),
            "output_path": None,
            "error": None,
            "created_at": time.time(),
            "completed_at": None,
            "input_size": None,
            "output_size": None,
            "compression_ratio": None,
            "size_reduction_pct": None,
            "duration_seconds": None,
        }
        self._jobs[job_id] = job
        self._queues[job_id] = asyncio.Queue(maxsize=200)
        return job

    def get(self, job_id: str) -> Optional[dict]:
        return self._jobs.get(job_id)

    def set_processing(self, job_id: str) -> None:
        if job := self._jobs.get(job_id):
            job["status"] = "processing"

    def set_completed(self, job_id: str, output_path: str, result: TranscodeResult) -> None:
        if job := self._jobs.get(job_id):
            job["status"] = "completed"
            job["output_path"] = output_path
            job["completed_at"] = time.time()
            job["input_size"] = result.input_size
            job["output_size"] = result.output_size
            job["compression_ratio"] = result.compression_ratio
            job["size_reduction_pct"] = result.size_reduction_pct
            job["duration_seconds"] = result.duration_seconds

    def set_failed(self, job_id: str, error: str) -> None:
        if job := self._jobs.get(job_id):
            job["status"] = "failed"
            job["error"] = error
            job["completed_at"] = time.time()

    def list_jobs(self) -> list[dict]:
        return list(self._jobs.values())

    def get_queue(self, job_id: str) -> asyncio.Queue | None:
        return self._queues.get(job_id)

    def set_process(self, job_id: str, proc) -> None:
        self._processes[job_id] = proc

    def cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job["status"] = "cancelled"
        job["completed_at"] = time.time()
        proc = self._processes.pop(job_id, None)
        if proc and proc.returncode is None:
            proc.kill()
