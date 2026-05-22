import asyncio
import json
import mimetypes
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

load_dotenv()

from app import database, storage
from app.jobs import JobStore
from app.transcoder import TranscodeOptions, VALID_PROFILES, transcode_video

UPLOAD_DIR = Path("/tmp/transcoder/uploads")
OUTPUT_DIR = Path("/tmp/transcoder/outputs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SUPPORTED_INPUT_FORMATS = {"mp4", "avi", "mov", "mkv", "webm", "flv", "m4v", "wmv", "ts"}
VALID_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower"}

job_store = JobStore()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await database.init_pool()
    job_store.set_pool(await database.get_pool())
    await job_store.reset_stale()
    yield
    await database.close_pool()


app = FastAPI(
    title="LEAP Video Transcoder",
    description="Transcode video files to lower bitrate for compression using FFmpeg.",
    version="1.0.0",
    lifespan=lifespan,
)


def _validate_upload(file: UploadFile) -> None:
    ext = Path(file.filename or "").suffix.lower().lstrip(".")
    if ext not in SUPPORTED_INPUT_FORMATS:
        raise HTTPException(
            400,
            f"Unsupported file type '{ext}'. Accepted: {sorted(SUPPORTED_INPUT_FORMATS)}",
        )


def _resolve_options(
    output_format: str,
    profile: str | None,
    video_bitrate: str | None,
    audio_bitrate: str | None,
    preset: str | None,
) -> TranscodeOptions:
    output_format = output_format.lower().lstrip(".")
    manual = {k: v for k, v in {"video_bitrate": video_bitrate, "audio_bitrate": audio_bitrate, "preset": preset}.items() if v is not None}

    if profile and manual:
        raise HTTPException(400, f"'profile' is mutually exclusive with: {', '.join(manual)}")

    if profile:
        if profile not in VALID_PROFILES:
            raise HTTPException(400, f"Unknown profile '{profile}'. Valid: {sorted(VALID_PROFILES)}")
        return TranscodeOptions(output_format=output_format, profile=profile)

    p = preset or "fast"
    if p not in VALID_PRESETS:
        raise HTTPException(400, f"Invalid preset '{p}'. Valid: {sorted(VALID_PRESETS)}")
    return TranscodeOptions(
        output_format=output_format,
        video_bitrate=video_bitrate or "1M",
        audio_bitrate=audio_bitrate or "128k",
        preset=p,
    )


def _save_upload(file: UploadFile, dest: Path) -> None:
    with open(dest, "wb") as buf:
        shutil.copyfileobj(file.file, buf)


def _unlink(*paths: Path | None) -> None:
    for p in paths:
        if p:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"])
def health_check():
    return {"status": "ok", "version": "1.0.0"}


@app.post(
    "/transcode",
    tags=["transcode"],
    summary="Upload a video and receive the transcoded file immediately",
    response_description="Transcoded video file",
)
async def transcode_sync(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Video file to transcode"),
    output_format: str = Query("mp4", description="Output container format"),
    profile: str | None = Query(None, description="Encoding profile (e.g. ai_video). Mutually exclusive with video_bitrate, audio_bitrate, preset."),
    video_bitrate: str | None = Query(None, description="Target video bitrate, e.g. 500k, 1M, 2M"),
    audio_bitrate: str | None = Query(None, description="Target audio bitrate, e.g. 64k, 128k, 192k"),
    preset: str | None = Query(None, description="FFmpeg encoding preset (speed vs. compression)"),
):
    _validate_upload(file)
    options = _resolve_options(output_format, profile, video_bitrate, audio_bitrate, preset)

    job_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{job_id}{Path(file.filename or 'input').suffix or '.mp4'}"
    output_path = OUTPUT_DIR / f"{job_id}.{options.output_format}"

    _save_upload(file, input_path)

    try:
        result = await transcode_video(str(input_path), str(output_path), options)
    except RuntimeError as exc:
        _unlink(input_path, output_path)
        raise HTTPException(500, f"Transcoding failed: {exc}") from exc

    blob_prefix = storage.make_blob_prefix(job_id)
    input_suffix = Path(file.filename or "input").suffix or ".mp4"
    background_tasks.add_task(
        _upload_and_cleanup,
        str(input_path), str(output_path),
        f"{blob_prefix}/original{input_suffix}",
        f"{blob_prefix}/transcoded.{options.output_format}",
    )

    stem = Path(file.filename or "video").stem
    return FileResponse(
        path=str(output_path),
        media_type=f"video/{options.output_format}",
        filename=f"{stem}_transcoded.{options.output_format}",
        headers={
            "X-Input-Size-Bytes": str(result.input_size),
            "X-Output-Size-Bytes": str(result.output_size),
            "X-Size-Reduction-Pct": str(result.size_reduction_pct),
            "X-Transcode-Duration-Sec": str(result.duration_seconds),
        },
    )


@app.post(
    "/jobs",
    status_code=202,
    tags=["jobs"],
    summary="Submit a transcoding job and receive a job ID (non-blocking)",
)
async def create_job(
    file: UploadFile = File(...),
    output_format: str = Query("mp4"),
    profile: str | None = Query(None, description="Encoding profile (e.g. ai_video). Mutually exclusive with video_bitrate, audio_bitrate, preset."),
    video_bitrate: str | None = Query(None),
    audio_bitrate: str | None = Query(None),
    preset: str | None = Query(None),
):
    _validate_upload(file)
    options = _resolve_options(output_format, profile, video_bitrate, audio_bitrate, preset)

    job_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{job_id}{Path(file.filename or 'input').suffix or '.mp4'}"
    output_path = OUTPUT_DIR / f"{job_id}.{options.output_format}"

    _save_upload(file, input_path)
    await job_store.create(job_id, file.filename or "video", options)

    asyncio.create_task(_run_job(job_id, input_path, output_path, options))

    return {"job_id": job_id, "status": "queued"}


@app.get(
    "/files",
    tags=["files"],
    summary="List all uploaded files with metadata and download links",
)
async def list_files():
    jobs = await job_store.list_jobs()
    return [_job_to_file(j) for j in jobs]


@app.delete(
    "/files/{job_id}",
    status_code=204,
    tags=["files"],
    summary="Delete a file and all related data (blobs + job record)",
)
async def delete_file(job_id: str):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(404, "File not found")

    delete_tasks = []
    if storage.is_configured():
        if job.get("input_blob"):
            delete_tasks.append(storage.delete(job["input_blob"]))
        if job.get("output_blob"):
            delete_tasks.append(storage.delete(job["output_blob"]))

    if delete_tasks:
        await asyncio.gather(*delete_tasks, return_exceptions=True)

    # Clean up local output file if it still exists (blob storage not configured)
    if job.get("output_path"):
        _unlink(Path(job["output_path"]))

    await job_store.delete(job_id)
    return Response(status_code=204)


@app.get("/jobs", tags=["jobs"], summary="List all jobs")
async def list_jobs():
    return await job_store.list_jobs()


@app.get("/jobs/{job_id}", tags=["jobs"], summary="Get job status and metadata")
async def get_job(job_id: str):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get(
    "/jobs/{job_id}/download",
    tags=["jobs"],
    summary="Download the output of a completed job",
    response_description="Transcoded video file",
)
async def download_job(job_id: str, background_tasks: BackgroundTasks):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "completed":
        raise HTTPException(409, f"Job is '{job['status']}', not 'completed'")

    if job.get("output_blob"):
        return RedirectResponse(storage.sas_url(job["output_blob"]), status_code=302)

    # Fallback: serve from local temp (blob storage not configured)
    output_path = Path(job["output_path"] or "")
    if not output_path.exists():
        raise HTTPException(410, "Output file has already been removed")

    background_tasks.add_task(_unlink, output_path)
    fmt = job["options"]["output_format"]
    return FileResponse(
        path=str(output_path),
        media_type=f"video/{fmt}",
        filename=job["output_filename"],
        headers={
            "X-Input-Size-Bytes": str(job["input_size"]),
            "X-Output-Size-Bytes": str(job["output_size"]),
            "X-Size-Reduction-Pct": str(job["size_reduction_pct"]),
        },
    )


@app.get(
    "/jobs/{job_id}/progress",
    tags=["jobs"],
    summary="SSE stream of transcoding progress",
)
async def job_progress(job_id: str, request: Request):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    async def stream():
        status = job["status"]
        if status == "completed":
            yield _sse({"type": "done", "total_sec": job["duration_seconds"],
                        "input_size": job["input_size"], "output_size": job["output_size"],
                        "compression_ratio": job["compression_ratio"],
                        "size_reduction_pct": job["size_reduction_pct"]})
            return
        if status == "cancelled":
            yield _sse({"type": "cancelled"})
            return
        if status == "failed":
            yield _sse({"type": "error", "detail": job["error"]})
            return

        queue = job_store.get_queue(job_id)
        if queue is None:
            return
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield _sse({"type": "heartbeat"})
                continue
            if event.get("type") == "__eof__":
                break
            yield _sse(event)
            if event.get("type") in ("done", "error", "cancelled"):
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete(
    "/jobs/{job_id}",
    status_code=204,
    tags=["jobs"],
    summary="Cancel a queued or processing job",
)
async def cancel_job(job_id: str):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("queued", "processing"):
        raise HTTPException(409, f"Job is '{job['status']}', cannot cancel")
    await job_store.cancel(job_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _upload_and_cleanup(
    input_path: str, output_path: str, input_blob: str, output_blob: str
) -> None:
    if storage.is_configured():
        await asyncio.gather(
            storage.upload(input_path, input_blob),
            storage.upload(output_path, output_blob),
            return_exceptions=True,
        )
    _unlink(Path(input_path), Path(output_path))


def _job_to_file(job: dict) -> dict:
    name = job.get("original_filename") or ""
    content_type = mimetypes.guess_type(name)[0] or "video/mp4"
    original_url = (
        storage.sas_url(job["input_blob"])
        if job.get("input_blob") and storage.is_configured()
        else None
    )
    transcoded_url = (
        storage.sas_url(job["output_blob"])
        if job.get("output_blob") and storage.is_configured()
        else None
    )
    return {
        "job_id": job["job_id"],
        "name": name,
        "uploaded_at": job.get("created_at"),
        "status": job["status"],
        "content_type": content_type,
        "original_size": job.get("input_size"),
        "transcoded_size": job.get("output_size"),
        "compression_ratio": job.get("compression_ratio"),
        "transcoding_time_sec": job.get("duration_seconds"),
        "original_download_url": original_url,
        "transcoded_download_url": transcoded_url,
    }


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _publish(queue: asyncio.Queue | None, event: dict) -> None:
    if queue:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

async def _run_job(
    job_id: str, input_path: Path, output_path: Path, options: TranscodeOptions
) -> None:
    queue = job_store.get_queue(job_id)

    # Job may have been cancelled before the task was scheduled
    job = await job_store.get(job_id)
    if job and job["status"] == "cancelled":
        _unlink(input_path)
        return

    await job_store.set_processing(job_id)

    def store_proc(proc) -> None:
        job_store.set_process(job_id, proc)

    blob_prefix = storage.make_blob_prefix(job_id)
    input_suffix = input_path.suffix or ".mp4"
    input_blob = f"{blob_prefix}/original{input_suffix}"
    output_blob = f"{blob_prefix}/transcoded.{options.output_format}"

    try:
        if storage.is_configured():
            await storage.upload(str(input_path), input_blob)

        result = await transcode_video(str(input_path), str(output_path), options, queue, store_proc)

        if storage.is_configured():
            await storage.upload(str(output_path), output_blob)
            await job_store.set_blobs(job_id, input_blob, output_blob)
            _unlink(output_path)

        await job_store.set_completed(job_id, str(output_path), result)
        _publish(queue, {
            "type": "done",
            "total_sec": result.duration_seconds,
            "input_size": result.input_size,
            "output_size": result.output_size,
            "compression_ratio": result.compression_ratio,
            "size_reduction_pct": result.size_reduction_pct,
        })
    except Exception as exc:  # noqa: BLE001
        current = await job_store.get(job_id)
        if current and current["status"] == "cancelled":
            _publish(queue, {"type": "cancelled"})
        else:
            await job_store.set_failed(job_id, str(exc))
            _publish(queue, {"type": "error", "detail": str(exc)})
    finally:
        _unlink(input_path)
        _publish(queue, {"type": "__eof__"})
