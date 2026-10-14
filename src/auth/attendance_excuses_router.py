"""
ALGO - Attendance excuses router (owner/admin only).

Mounted under /api/v1/auth. Every handler is tenant-scoped by the org of the
token; a superadmin without an organisation gets 403 instead of a 500.
"""
from typing import Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

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
            user["org_id"], request.ids, user["user_id"], request.decision, request.note
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
            user["org_id"], excuse_id, user["user_id"], request.decision, request.note
        )
    except ExcuseError as exc:
        raise _http(exc)


@router.post("/org/attendance/excuses/{excuse_id}/reset", response_model=ExcuseItem)
def reset_excuse(
    excuse_id: UUID,
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Сбросить решение (заявка снова ожидает)."""
    try:
        return AttendanceExcuseService(db).reset(user["org_id"], excuse_id, user["user_id"])
    except ExcuseError as exc:
        raise _http(exc)


@router.post("/org/attendance/excuses/{excuse_id}/files", response_model=ExcuseFileInfo)
async def upload_excuse_file(
    excuse_id: UUID,
    file: UploadFile = File(...),
    user: dict = Depends(_admin),
    db: Session = Depends(get_db),
):
    """Приложить справку (JPEG/PNG/PDF до 2 МБ, не более 3 файлов)."""
    # Never read more than one byte past the limit into memory.
    data = await file.read(MAX_FILE_BYTES + 1)
    try:
        return AttendanceExcuseService(db).add_file(
            user["org_id"], excuse_id, user["user_id"], data, file.filename
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
