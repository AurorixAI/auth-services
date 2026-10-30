"""
ALGO - Attendance excuses router (owner/admin only).

Mounted under /api/v1/auth. Every handler is tenant-scoped by the org of the
token; a user without an organisation gets 403 instead of a 500.
"""
from typing import Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from src.core.database import get_db

from .attendance_excuses import (
    MAX_FILE_BYTES,
    AttendanceExcuseService,
    BulkReviewRequest,
    BulkReviewResponse,
    ExcuseError,
    ExcuseFileInfo,
    ExcuseItem,
    ExcuseListResponse,
    ExcuseSummaryResponse,
    ResetRequest,
    ReviewRequest,
)
from .dependencies import require_org_admin

router = APIRouter(prefix="/auth", tags=["Attendance excuses"])


def _admin(user: dict = Depends(require_org_admin)) -> dict:
    if not user.get("org_id"):
        raise HTTPException(403, "Нет организации")
    return user


def _http(exc: ExcuseError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


@router.get("/org/attendance/excuses", response_model=ExcuseListResponse)
def list_excuses(
    status: str = Query("pending", pattern="^(pending|confirmed|rejected|all)$"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    search: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Заявки на уважительный пропуск за период (по умолчанию — текущий месяц)."""
    try:
        return AttendanceExcuseService(db).list_excuses(
            user["org_id"],
            status=status,
            date_from=date_from,
            date_to=date_to,
            group_id=group_id,
            teacher_id=teacher_id,
            search=search,
            limit=limit,
            offset=offset,
        )
    except ExcuseError as exc:
        raise _http(exc)


@router.get("/org/attendance/excuses/summary", response_model=ExcuseSummaryResponse)
def excuses_summary(user: dict = Depends(_admin), db: Session = Depends(get_db)):
    """Счётчик ожидающих решения заявок (для меню)."""
    return AttendanceExcuseService(db).summary(user["org_id"])


# Registered before the "/{excuse_id}/..." routes: "review-bulk" must never be
# read as an id.
@router.post("/org/attendance/excuses/review-bulk", response_model=BulkReviewResponse)
def review_excuses_bulk(
    request: BulkReviewRequest,
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Подтвердить/отклонить несколько заявок; недоступные попадают в skipped."""
    try:
        return AttendanceExcuseService(db).review_bulk(
            user["org_id"], request.ids, user["user_id"], request.decision, request.note, request.expected_status
        )
    except ExcuseError as exc:
        raise _http(exc)


@router.post("/org/attendance/excuses/{excuse_id}/review", response_model=ExcuseItem)
def review_excuse(
    excuse_id: UUID,
    request: ReviewRequest,
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Подтвердить или отклонить заявку."""
    try:
        return AttendanceExcuseService(db).review(
            user["org_id"], excuse_id, user["user_id"], request.decision, request.note, request.expected_status
        )
    except ExcuseError as exc:
        raise _http(exc)


@router.post("/org/attendance/excuses/{excuse_id}/reset", response_model=ExcuseItem)
def reset_excuse(
    excuse_id: UUID,
    request: Optional[ResetRequest] = None,
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Сбросить решение (заявка снова ожидает).  Тело необязательно: ``expected_status`` — статус, который видел клиент."""
    try:
        return AttendanceExcuseService(db).reset(
            user["org_id"], excuse_id, user["user_id"], request.expected_status if request else None
        )
    except ExcuseError as exc:
        raise _http(exc)


# The request body is multipart: file + framing.  A declared length above this
# is refused before a single byte of the body is read.
MAX_BODY_BYTES = MAX_FILE_BYTES + 64 * 1024


@router.post(
    "/org/attendance/excuses/{excuse_id}/files",
    response_model=ExcuseFileInfo,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def upload_excuse_file(
    excuse_id: UUID,
    request: Request,
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Приложить справку (JPEG/PNG/PDF до 2 МБ, не более 3 файлов)."""
    # ``file: UploadFile`` would make FastAPI buffer the whole body before this
    # handler (and any dependency) runs, so the size is checked here first.
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        raise HTTPException(411, "Не указан размер файла")
    if declared > MAX_BODY_BYTES:
        raise HTTPException(413, "Файл слишком большой (макс. 2 МБ)")
    form = await request.form()
    try:
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise HTTPException(422, "Передайте файл в поле file")
        # Never read more than one byte past the limit into memory.
        data = await upload.read(MAX_FILE_BYTES + 1)
        filename = upload.filename
    finally:
        await form.close()
    try:
        # Blocking DB work runs in the thread pool, not on the event loop.
        return await run_in_threadpool(
            AttendanceExcuseService(db).add_file, user["org_id"], excuse_id, user["user_id"], data, filename
        )
    except ExcuseError as exc:
        raise _http(exc)


@router.get("/org/attendance/excuses/files/{file_id}")
def download_excuse_file(
    file_id: UUID,
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Отдать приложенный файл (только owner/admin своего центра)."""
    try:
        stored = AttendanceExcuseService(db).get_file(user["org_id"], file_id)
    except ExcuseError as exc:
        raise _http(exc)
    name = stored.filename or "spravka"
    ascii_name = name.encode("ascii", "ignore").decode() or "spravka"
    return Response(
        content=stored.data,
        media_type=stored.content_type,
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            # Even if a browser opens the file directly, nothing in it may run.
            "Content-Security-Policy": "sandbox",
        },
    )


@router.delete("/org/attendance/excuses/files/{file_id}")
def delete_excuse_file(
    file_id: UUID,
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Удалить приложенный файл."""
    try:
        AttendanceExcuseService(db).delete_file(user["org_id"], file_id, user["user_id"])
    except ExcuseError as exc:
        raise _http(exc)
    return {"ok": True}
