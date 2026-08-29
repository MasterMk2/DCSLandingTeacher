"""ACMI file import endpoints (background job based).

- ``POST /api/import``: multipart upload of a saved Tacview recording
  (``.acmi`` / ``.acmi.txt`` / ``.acmi.zip``). The file is streamed to a
  temporary location and processed in the background through the same
  ingest -> detection -> grading pipeline as the live stream; the job id is
  returned immediately.
- ``GET /api/imports``: list of import jobs (newest first).
- ``GET /api/imports/{job_id}``: progress / result summary of one job.

All three live on the token-protected router (Issue #8), like the landing
endpoints.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    UploadFile,
)

from app.api.auth import require_auth  # noqa: F401 -- used via Depends below
from app.api.schemas import (
    ImportJobListResponse,
    ImportJobOut,
    ImportStartResponse,
)
from app.importer import ImportJob, _remove_quietly

#: Accepted extensions for uploaded recordings (case-insensitive).
ALLOWED_SUFFIXES = {".acmi", ".txt", ".zip"}

#: Zip local-file header magic: Tacview often stores compressed data inside
#: plain ``.acmi`` files; renaming to ``.zip`` lets file_reader unpack it.
_ZIP_MAGIC = b"PK"

#: Upload streaming chunk size.
_CHUNK_SIZE = 1024 * 1024

# No prefix here (Issue #38): applied at include time in app.api.main.
router = APIRouter(dependencies=[Depends(require_auth)])


def _validate_filename(filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=(
                "unsupported file type: expected .acmi, .acmi.txt or .acmi.zip"
            ),
        )


async def _save_upload(upload: UploadFile, max_bytes: int) -> Path:
    """Stream the upload to a temp file, enforcing the size limit."""
    handle, raw_path = tempfile.mkstemp(prefix="dlt-import-")
    size = 0
    try:
        with os.fdopen(handle, "wb") as out:
            while chunk := await upload.read(_CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds the {max_bytes // (1024 * 1024)} MB limit",
                    )
                out.write(chunk)
    except BaseException:
        _remove_quietly(raw_path)
        raise

    path = Path(raw_path)
    with open(path, "rb") as head:
        is_zip = head.read(2) == _ZIP_MAGIC
    if is_zip:
        zip_path = path.with_suffix(".zip")
        os.replace(path, zip_path)
        return zip_path
    return path


@router.post("/import", response_model=ImportStartResponse, status_code=202)
async def import_acmi_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile,
) -> ImportStartResponse:
    """Accept an ACMI recording and start a background import job."""
    manager = getattr(request.app.state, "import_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="import service unavailable")

    filename = file.filename or "upload.acmi"
    _validate_filename(filename)

    settings = request.app.state.settings
    max_bytes = settings.import_max_upload_mb * 1024 * 1024

    # The Content-Length pre-check for Issue #29 lives in middleware
    # (app.api.main._reject_oversized_uploads), because by the time this
    # function runs FastAPI has already parsed the multipart body and spooled
    # the upload to disk. _save_upload enforces the real limit while
    # streaming, for clients that omit or forge Content-Length.
    temp_path = await _save_upload(file, max_bytes)

    job = manager.create_job(filename)
    background_tasks.add_task(manager.run, job, temp_path)
    return ImportStartResponse(id=job.id, filename=job.filename, status=job.status)


def _job_out(job: ImportJob) -> ImportJobOut:
    return ImportJobOut(**job.as_dict())


@router.get("/imports", response_model=ImportJobListResponse)
async def list_imports(request: Request) -> ImportJobListResponse:
    manager = getattr(request.app.state, "import_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="import service unavailable")
    return ImportJobListResponse(items=[_job_out(j) for j in manager.list_jobs()])


@router.delete("/imports/{job_id}", status_code=204)
async def discard_import(job_id: str, request: Request) -> None:
    """Drop an import and everything it created.

    Uploads are scratch data: this is what the UI calls when the user is done
    with a result, and what the unload beacon calls when they simply leave.
    """
    manager = request.app.state.import_manager
    await manager.discard(job_id)


@router.post("/imports/{job_id}/discard", status_code=204)
async def discard_import_beacon(job_id: str, request: Request) -> None:
    """Same as the DELETE, reachable by ``navigator.sendBeacon``.

    A page being unloaded cannot reliably finish a `fetch`, and sendBeacon
    only issues POST, so leaving a tab needs a POST route to land on.
    """
    manager = request.app.state.import_manager
    await manager.discard(job_id)


@router.get("/imports/{job_id}", response_model=ImportJobOut)
async def get_import(request: Request, job_id: str) -> ImportJobOut:
    manager = getattr(request.app.state, "import_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="import service unavailable")
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="import job not found")
    return _job_out(job)
