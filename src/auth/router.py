"""
ALGO V1 - Auth Router
REST API: org application, login, invitations, students, diagnostics, groups
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query, UploadFile, File
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date, datetime
import os

from .service import AuthService
from .attendance_access import is_org_admin
from . import import_service
from .import_service import ImportPreviewResponse, ImportCommitResponse
from src.api.rate_limit import rate_limit
from .dependencies import AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME
from .schemas import (
    OrgApplicationRequest,
    LoginRequest,
    RefreshTokenRequest,
    ChangePasswordRequest,
    UpdateProfileRequest,
    InviteMemberRequest,
    AcceptInviteRequest,
    StudentCreateRequest,
    StudentUpdateRequest,
    StudentResponse,
    DiagnosticCreateRequest,
    DiagnosticQuickRequest,
    DiagnosticInviteResponse,
    GroupCreateRequest,
    GroupUpdateRequest,
    GroupResponse,
    BulkAddStudentsRequest,
    OrgUpdateRequest,
    AuthResponse,
    TokenPair,
    UserResponse,
    OrgResponse,
    MessageResponse,
    OrgApplicationPendingResponse,
    AdminApproveRequest,
    ActivateAccountRequest,
    ScheduleLessonResponse,
    ScheduleOccurrenceResponse,
    CreateLessonRequest,
    CreateOneOffLessonRequest,
    UpdateLessonRequest,
    RescheduleLessonRequest,
    ReviseRecurringLessonRequest,
    EndRecurringLessonRequest,
    RecurringLessonEndImpactResponse,
    EndRecurringLessonResponse,
    StartAttendanceRequest,
    SaveAttendanceRequest,
    ReconcileMissedLessonRequest,
    GroupAttendanceResponse,
    OpenAttendanceActionResponse,
)
from .dependencies import (
    get_current_user_payload,
    require_org_owner,
    require_org_admin,
    require_org_teacher,
    require_org_member,
    require_superadmin,
)

from src.core.database import get_db
from src.api.internal_router import require_internal_service_token

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ----------- cookie / env helpers ----------- #

_IS_PROD = os.getenv("ENVIRONMENT", "development") == "production"
_COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)  # e.g. ".algo.kz"
_ACCESS_MAX_AGE = 3600  # 1 hour
_REFRESH_MAX_AGE = 30 * 86400  # 30 days


def _set_auth_cookies(
    response: JSONResponse,
    access_token: str,
    refresh_token: str,
) -> JSONResponse:
    """Attach httpOnly JWT cookies to *response*."""
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=access_token,
        httponly=True,
        secure=_IS_PROD,
        samesite="lax",
        max_age=_ACCESS_MAX_AGE,
        path="/",
        domain=_COOKIE_DOMAIN,
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=_IS_PROD,
        samesite="lax",
        max_age=_REFRESH_MAX_AGE,
        # Actual mount point is /api/v1/auth/* (see main_v2.py) — a plain
        # "/auth" prefix would never match, so the browser would never send
        # this cookie back to /api/v1/auth/refresh at all.
        path="/api/v1/auth",
        domain=_COOKIE_DOMAIN,
    )
    return response


def _auth_response_with_cookies(auth: AuthResponse) -> JSONResponse:
    """Return AuthResponse JSON body **and** set httpOnly cookies."""
    body = auth.model_dump(mode="json")
    resp = JSONResponse(content=body)
    _set_auth_cookies(resp, auth.tokens.access_token, auth.tokens.refresh_token)
    return resp


def _token_response_with_cookies(tokens: TokenPair) -> JSONResponse:
    """Return TokenPair JSON body **and** set httpOnly cookies."""
    body = tokens.model_dump(mode="json")
    resp = JSONResponse(content=body)
    _set_auth_cookies(resp, tokens.access_token, tokens.refresh_token)
    return resp


def _svc(db: Session) -> AuthService:
    return AuthService(db)


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  PUBLIC ENDPOINTS (без JWT)                                              ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.post("/apply", response_model=OrgApplicationPendingResponse, status_code=201)
def apply_organization(
    request: OrgApplicationRequest,
    db: Session = Depends(get_db),
):
    """
    Регистрация организации + владелец.
    По умолчанию (auto_approve_organizations=True) организация активируется
    сразу — владелец может войти со своим email/паролем через /login. Токены
    здесь не выдаются (это не логин), только подтверждение регистрации.
    Ручной режим одобрения (approve_organization / approve_org.sh) остаётся
    рабочим — если флаг выключить, поведение вернётся к статусу pending.
    """
    try:
        result = _svc(db).apply_organization(request)
        return JSONResponse(content=result.model_dump(mode="json"), status_code=201)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/login", response_model=AuthResponse)
def login(
    request: LoginRequest,
    db: Session = Depends(get_db),
):
    """Вход в систему — возвращает JWT + org info"""
    try:
        result = _svc(db).login(request)
        return _auth_response_with_cookies(result)
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))


@router.post("/refresh", response_model=TokenPair)
def refresh_tokens(
    request: RefreshTokenRequest,
    raw_request: Request = None,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit("10/minute")),
):
    """Обновление пары токенов.
    Принимает refresh_token из тела ИЛИ из httpOnly cookie."""
    rt = request.refresh_token
    # Fallback: read from httpOnly cookie if body is empty
    if not rt and raw_request:
        rt = raw_request.cookies.get(REFRESH_COOKIE_NAME)
    if not rt:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh_token required")
    try:
        tokens = _svc(db).refresh_tokens(rt)
        return _token_response_with_cookies(tokens)
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))


@router.get("/invite/{code}")
def get_invite_info(code: str, db: Session = Depends(get_db)):
    """Публичная информация о приглашении (орг + роль) — для экрана принятия."""
    try:
        return _svc(db).get_invitation_info(code)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.post("/invite/accept", response_model=AuthResponse, status_code=201)
def accept_invite(request: AcceptInviteRequest, db: Session = Depends(get_db)):
    """Принять приглашение в организацию — создаёт аккаунт"""
    try:
        result = _svc(db).accept_invitation(request)
        resp = _auth_response_with_cookies(result)
        resp.status_code = 201
        return resp
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/logout")
def logout():
    """Очистить httpOnly auth cookies."""
    resp = JSONResponse(content={"message": "logged out", "success": True})
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/", domain=_COOKIE_DOMAIN)
    resp.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth", domain=_COOKIE_DOMAIN)
    return resp


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ADMIN APPROVAL & ACTIVATION                                            ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.post("/admin/approve/{org_id}")
def admin_approve_organization(
    org_id: str,
    request: AdminApproveRequest = AdminApproveRequest(),
    admin: dict = Depends(require_superadmin),
    db: Session = Depends(get_db),
):
    """
    Одобрить или отклонить заявку организации (superadmin).
    При одобрении на email владельца отправляется ссылка активации.
    """
    try:
        result = _svc(db).approve_organization(
            org_id,
            action=request.action,
            rejection_reason=request.rejection_reason,
        )
        return result
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/activate", response_model=AuthResponse)
def activate_account(
    request: ActivateAccountRequest,
    db: Session = Depends(get_db),
):
    """
    Активация аккаунта по токену из письма.
    Активирует организацию и владельца, возвращает JWT.
    """
    try:
        result = _svc(db).activate_account(request.token)
        return _auth_response_with_cookies(result)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  DIAGNOSTIC — PUBLIC (ученик без JWT переходит по QR/ссылке)             ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.get("/diagnostic/{code}")
def get_diagnostic_info(code: str, db: Session = Depends(get_db)):
    """
    Получить информацию о диагностике по коду (публичный).
    Ученик открывает QR → видит своё имя → подтверждает.
    """
    info = _svc(db).get_diagnostic_by_code(code)
    if not info:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Диагностика не найдена")
    return info


@router.post("/diagnostic/{code}/confirm")
def confirm_diagnostic(code: str, db: Session = Depends(get_db)):
    """Подтверждение по коду — переход pending → confirmed. Код сам по себе
    уникально определяет ученика, ввод имени не требуется."""
    try:
        return _svc(db).confirm_diagnostic(code)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("/public/groups/{group_id}/verify-student")
def verify_group_student(
    group_id: str,
    q: Optional[str] = Query(default=None),
    student_id: Optional[str] = Query(default=None),
    student_name: Optional[str] = Query(default=None),
    _: None = Depends(require_internal_service_token),
    db: Session = Depends(get_db),
):
    """Внутренняя проверка принадлежности ученика к группе для exam-service.

    Маршрут сохранён для совместимости с уже развёрнутым exam-service, но
    больше не является публичным: UUID группы и ФИО нельзя использовать как
    способ перечисления учеников.
    """
    return _svc(db).verify_group_student(
        group_id=group_id,
        query=q,
        student_id=student_id,
        student_name=student_name,
    )



# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  PROFILE — для аутентифицированных пользователей                         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.get("/me", response_model=UserResponse)
def get_profile(
    payload: dict = Depends(get_current_user_payload), db: Session = Depends(get_db)
):
    """Профиль текущего пользователя"""
    svc = _svc(db)
    user = svc.get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return svc._build_user_response(user)


@router.put("/me", response_model=UserResponse)
def update_profile(
    request: UpdateProfileRequest,
    payload: dict = Depends(get_current_user_payload),
    db: Session = Depends(get_db),
):
    """Обновить профиль"""
    try:
        svc = _svc(db)
        user = svc.update_profile(
            payload["sub"], **request.model_dump(exclude_none=True)
        )
        return svc._build_user_response(user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    request: ChangePasswordRequest,
    payload: dict = Depends(get_current_user_payload),
    db: Session = Depends(get_db),
):
    """Сменить пароль"""
    try:
        _svc(db).change_password(
            payload["sub"], request.old_password, request.new_password
        )
        return MessageResponse(message="Пароль успешно изменён")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/logout", response_model=MessageResponse)
def logout(
    payload: dict = Depends(get_current_user_payload), db: Session = Depends(get_db)
):
    """Выход — отзыв refresh token"""
    _svc(db).logout(payload["sub"])
    return MessageResponse(message="Вы вышли из системы")


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ORGANIZATION — управление орг. (owner/admin)                            ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.get("/org", response_model=OrgResponse)
def get_organization(
    user: dict = Depends(require_org_member), db: Session = Depends(get_db)
):
    """Получить данные организации"""
    try:
        return _svc(db).get_organization(user["org_id"])
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/org/stats")
def get_org_stats(
    user: dict = Depends(require_org_admin), db: Session = Depends(get_db)
):
    """Сводная статистика всей организации для owner/admin dashboard."""
    return _svc(db).get_org_stats(user["org_id"])


@router.put("/org", response_model=OrgResponse)
def update_organization(
    request: OrgUpdateRequest,
    user: dict = Depends(require_org_owner),
    db: Session = Depends(get_db),
):
    """Обновить данные организации (только owner)"""
    try:
        data = request.model_dump(exclude_none=True)
        return _svc(db).update_organization(user["org_id"], **data)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  INVITATIONS — приглашения сотрудников                                    ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.post("/org/invitations")
def create_invitation(
    request: InviteMemberRequest,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Создать приглашение (owner/admin → admin/teacher)"""
    try:
        return _svc(db).create_invitation(user["org_id"], user["user_id"], request)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/org/invitations")
def list_invitations(
    user: dict = Depends(require_org_admin), db: Session = Depends(get_db)
):
    """Список приглашений организации"""
    return _svc(db).list_invitations(user["org_id"])


@router.delete("/org/invitations/{invite_id}", response_model=MessageResponse)
def revoke_invitation(
    invite_id: str,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Отозвать приглашение"""
    try:
        _svc(db).revoke_invitation(user["org_id"], invite_id)
        return MessageResponse(message="Приглашение отозвано")
    except ValueError as e:
        raise HTTPException(400, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  MEMBERS — сотрудники организации                                        ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.get("/org/members")
def list_members(
    user: dict = Depends(require_org_admin), db: Session = Depends(get_db)
):
    """Список сотрудников организации"""
    return _svc(db).list_org_members(user["org_id"])


@router.put("/org/members/{member_id}/role")
def update_member_role(
    member_id: str,
    role: str = Query(..., description="New role: admin or teacher"),
    user: dict = Depends(require_org_owner),
    db: Session = Depends(get_db),
):
    """Изменить роль сотрудника (только owner)"""
    try:
        return _svc(db).update_member_role(user["org_id"], member_id, role)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/org/members/{member_id}", response_model=MessageResponse)
def remove_member(
    member_id: str,
    user: dict = Depends(require_org_owner),
    db: Session = Depends(get_db),
):
    """Удалить сотрудника (только owner)"""
    try:
        _svc(db).remove_member(user["org_id"], member_id)
        return MessageResponse(message="Сотрудник удалён")
    except ValueError as e:
        raise HTTPException(400, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  STUDENTS — ученики (без логина)                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.post("/org/students", response_model=StudentResponse, status_code=201)
def create_student(
    request: StudentCreateRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Добавить ученика"""
    return _svc(db).create_student(user["org_id"], request)


@router.get("/org/students", response_model=list[StudentResponse])
def list_students(
    class_level: Optional[int] = None,
    group_id: Optional[str] = None,
    has_group: Optional[bool] = Query(None),
    student_status: Optional[str] = Query(None, alias="status"),
    exclude_status: Optional[str] = Query(None),
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db),
):
    """Список учеников организации"""
    return _svc(db).list_students(
        user["org_id"],
        class_level=class_level,
        group_id=group_id,
        has_group=has_group,
        status=student_status,
        exclude_status=exclude_status,
        search=search,
        limit=limit,
        offset=offset,
        actor_id=user["user_id"],
        is_org_admin=is_org_admin(user),
    )


@router.get("/org/students/{student_id}", response_model=StudentResponse)
def get_student(
    student_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db),
):
    """Данные одного ученика"""
    try:
        return _svc(db).get_student(
            user["org_id"],
            student_id,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/org/students/{student_id}/profile")
def get_student_profile(
    student_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db),
):
    """Полный профиль ученика (группа, история диагностик, результаты)"""
    try:
        return _svc(db).get_student_profile(
            user["org_id"],
            student_id,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/org/students/{student_id}/attendance")
def get_student_attendance_summary(
    student_id: str,
    month: Optional[str] = Query(None, description="Месяц YYYY-MM"),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Месячная посещаемость ученика по всем группам, включая прошлые."""
    try:
        return _svc(db).get_student_attendance_summary(
            user["org_id"], student_id, month=month
        )
    except ValueError as e:
        raise HTTPException(404 if str(e) == "Ученик не найден" else 400, str(e))


@router.put("/org/students/{student_id}", response_model=StudentResponse)
def update_student(
    student_id: str,
    request: StudentUpdateRequest,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Обновить ученика"""
    try:
        return _svc(db).update_student(user["org_id"], student_id, request)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  DIAGNOSTICS — создание тестирований                                      ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.post(
    "/org/diagnostics", response_model=DiagnosticInviteResponse, status_code=201
)
def create_diagnostic(
    request: DiagnosticCreateRequest,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Создать диагностику для существующего ученика — возвращает код/QR"""
    try:
        return _svc(db).create_diagnostic_invite(
            user["org_id"], user["user_id"], request
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post(
    "/org/diagnostics/quick", response_model=DiagnosticInviteResponse, status_code=201
)
def create_quick_diagnostic(
    request: DiagnosticQuickRequest,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Быстро: создать ученика + диагностику за один запрос"""
    try:
        return _svc(db).create_quick_diagnostic(
            user["org_id"], user["user_id"], request
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/org/diagnostics")
def list_diagnostics(
    diagnostic_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_meta: bool = Query(False),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Список диагностик организации с безопасной опциональной пагинацией."""
    items, total = _svc(db).list_diagnostic_invites(
        user["org_id"],
        status=diagnostic_status,
        limit=limit,
        offset=offset,
    )
    if include_meta:
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    # Preserve the original list contract for existing callers.
    return items


@router.get("/org/diagnostics/{diagnostic_id}")
def get_diagnostic(
    diagnostic_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db),
):
    """One diagnostic report entry, scoped to the staff member's students."""
    try:
        return _svc(db).get_diagnostic_invite(
            user["org_id"],
            diagnostic_id,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/org/diagnostics/{diagnostic_id}/acknowledge-report")
def acknowledge_diagnostic_report(
    diagnostic_id: str,
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db),
):
    """Mark an initial report as reviewed by the teacher of the current group."""
    try:
        return _svc(db).acknowledge_initial_diagnostic_report(
            user["org_id"],
            diagnostic_id,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  GROUPS — группы                                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.post("/org/groups", response_model=GroupResponse, status_code=201)
def create_group(
    request: GroupCreateRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Создать группу в рамках полномочий текущего сотрудника."""
    try:
        return _svc(db).create_group(
            user["org_id"],
            request,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
            actor_role=user.get("org_role") or user.get("role"),
        )
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("/org/groups", response_model=list[GroupResponse])
def list_groups(
    teacher_id: Optional[str] = None,
    class_level: Optional[int] = None,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Список групп организации"""
    return _svc(db).list_groups(
        user["org_id"],
        teacher_id=teacher_id,
        class_level=class_level,
        actor_id=user["user_id"],
        is_org_admin=is_org_admin(user),
    )


@router.get("/org/groups/{group_id}")
def get_group(
    group_id: str,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Детали группы + список учеников (с последним θ/уровнем, если есть)."""
    try:
        return _svc(db).get_group_detail(
            user["org_id"],
            group_id,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.get("/org/groups/{group_id}/stats")
def get_group_stats(
    group_id: str,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Статистика по группе — диагностировано/среднее θ/распределение уровней."""
    try:
        return _svc(db).get_group_stats(
            user["org_id"],
            group_id,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.put("/org/groups/{group_id}", response_model=GroupResponse)
def update_group(
    group_id: str,
    request: GroupUpdateRequest,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Обновить группу"""
    try:
        return _svc(db).update_group(user["org_id"], group_id, request)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/groups/{group_id}/students/bulk")
def bulk_add_students(
    group_id: str,
    request: BulkAddStudentsRequest,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Массово добавить учеников в группу.

    Registered before /students/{student_id} — a generic path param would
    otherwise greedily match the literal "bulk" segment first.
    """
    try:
        return _svc(db).bulk_add_students(user["org_id"], group_id, request.student_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/groups/{group_id}/students/{student_id}")
def add_student_to_group(
    group_id: str,
    student_id: str,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Добавить ученика в группу"""
    try:
        return _svc(db).add_student_to_group(user["org_id"], group_id, student_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete(
    "/org/groups/{group_id}/students/{student_id}", response_model=MessageResponse
)
def remove_student_from_group(
    group_id: str,
    student_id: str,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Убрать ученика из группы"""
    try:
        _svc(db).remove_student_from_group(user["org_id"], group_id, student_id)
        return MessageResponse(message="Ученик удалён из группы")
    except ValueError as e:
        raise HTTPException(400, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  BULK IMPORT — xlsx (owner/admin)                                          ║
# ╚═══════════════════════════════════════════════════════════════════════════╝

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAX_IMPORT_BYTES = 5 * 1024 * 1024  # 5 MB is plenty for a roster


@router.get("/org/import/template")
def download_import_template(user: dict = Depends(require_org_admin)):
    """Скачать .xlsx-шаблон для массового импорта (Учителя/Группы/Ученики)."""
    data = import_service.build_template_xlsx()
    return Response(
        content=data,
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": 'attachment; filename="algo_import_template.xlsx"'},
    )


@router.post("/org/import/preview", response_model=ImportPreviewResponse)
async def import_preview(
    file: UploadFile = File(...),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Разобрать и проверить файл (dry-run) — ничего не записывается."""
    data = await _read_import_upload(file)
    return import_service.preview(user["org_id"], data, db)


@router.post("/org/import/commit", response_model=ImportCommitResponse)
async def import_commit(
    file: UploadFile = File(...),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Записать данные из файла в базу транзакционно (всё или ничего)."""
    data = await _read_import_upload(file)
    return import_service.commit(user["org_id"], user["user_id"], data, db)


async def _read_import_upload(file: UploadFile) -> bytes:
    if file is None:
        raise HTTPException(400, "Файл не передан.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Файл пустой.")
    if len(data) > _MAX_IMPORT_BYTES:
        raise HTTPException(413, "Файл слишком большой (макс. 5 МБ).")
    return data


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ADMIN — superadmin endpoints                                            ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


@router.get("/users", response_model=list[UserResponse])
def list_users(
    role: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin: dict = Depends(require_superadmin),
    db: Session = Depends(get_db),
):
    """Список всех пользователей (superadmin)"""
    svc = _svc(db)
    users = svc.list_users(role=role, limit=limit, offset=offset)
    return [svc._build_user_response(u) for u in users]


# ═══════════════════════════════════════════════════════════════════
# SCHEDULE & ATTENDANCE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════


@router.get("/org/schedule", response_model=list[ScheduleLessonResponse])
def get_schedule(
    teacher_id: Optional[str] = None,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Получить расписание уроков организации (или конкретного преподавателя)."""
    # Teacher никогда не может подменить teacher_id параметром запроса.
    tid = teacher_id if is_org_admin(user) else user["user_id"]

    return _svc(db).get_schedule(user["org_id"], teacher_id=tid)


@router.get("/org/schedule/occurrences", response_model=list[ScheduleOccurrenceResponse])
def get_schedule_occurrences(
    date_from: str = Query(..., description="Начало периода YYYY-MM-DD"),
    date_to: str = Query(..., description="Конец периода YYYY-MM-DD"),
    teacher_id: Optional[str] = None,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Конкретные уроки периода с учётом переносов и версий расписания."""
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(400, "date_from и date_to должны иметь формат YYYY-MM-DD") from exc
    tid = teacher_id if is_org_admin(user) else user["user_id"]
    try:
        return _svc(db).get_schedule_occurrences(
            user["org_id"], date_from=start, date_to=end, teacher_id=tid
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/schedule/lessons", response_model=ScheduleLessonResponse, status_code=201)
def create_lesson(
    request: CreateLessonRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Создать/добавить урок в расписание"""
    try:
        return _svc(db).create_lesson(
            user["org_id"],
            request,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post(
    "/org/schedule/occurrences",
    response_model=ScheduleOccurrenceResponse,
    status_code=201,
)
def create_one_off_lesson(
    request: CreateOneOffLessonRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Создать разовое занятие только на выбранную календарную дату."""
    try:
        return _svc(db).create_one_off_lesson(
            user["org_id"],
            request,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/org/schedule/lessons/{lesson_id}", response_model=ScheduleLessonResponse)
def update_lesson(
    lesson_id: str,
    request: UpdateLessonRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Deprecated: base rules are never rewritten in place.

    Keep this explicit HTTP boundary as well as the defensive service guard:
    a stale client cannot reinterpret past attendance by moving/removing a
    JSON schedule slot. The supported routes retain an audit trail.
    """
    del lesson_id, request, user, db
    raise HTTPException(
        status.HTTP_410_GONE,
        detail=(
            "Прямое редактирование расписания отключено. Перенесите один урок "
            "или измените постоянное расписание с будущей даты."
        ),
    )


@router.post("/org/schedule/lessons/{lesson_id}/reschedule", response_model=ScheduleOccurrenceResponse)
def reschedule_lesson_occurrence(
    lesson_id: str,
    request: RescheduleLessonRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Перенести сегодняшний экземпляр урока без изменения постоянного расписания."""
    try:
        return _svc(db).reschedule_lesson_occurrence(
            user["org_id"],
            lesson_id,
            request,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/org/schedule/lessons/{lesson_id}/recurrence", response_model=ScheduleLessonResponse)
def revise_recurring_lesson(
    lesson_id: str,
    request: ReviseRecurringLessonRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Изменить расписание будущих экземпляров урока с указанной даты."""
    try:
        return _svc(db).revise_recurring_lesson(
            user["org_id"],
            lesson_id,
            request,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get(
    "/org/schedule/lessons/{lesson_id}/recurrence/end-impact",
    response_model=RecurringLessonEndImpactResponse,
)
def get_recurring_lesson_end_impact(
    lesson_id: str,
    effective_from: date = Query(...),
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Show all future exam consequences before ending a recurring schedule."""
    try:
        return _svc(db).get_recurring_lesson_end_impact(
            user["org_id"],
            lesson_id,
            effective_from=effective_from,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post(
    "/org/schedule/lessons/{lesson_id}/recurrence/end",
    response_model=EndRecurringLessonResponse,
)
def end_recurring_lesson(
    lesson_id: str,
    request: EndRecurringLessonRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Stop future recurrences after an exam-safe, explicit confirmation."""
    try:
        return _svc(db).end_recurring_lesson(
            user["org_id"],
            lesson_id,
            request,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.delete("/org/schedule/lessons/{lesson_id}")
def delete_lesson(
    lesson_id: str,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Deprecated: physical schedule deletion would orphan operational facts."""
    del lesson_id, user, db
    raise HTTPException(
        status.HTTP_410_GONE,
        "Физическое удаление уроков отключено. Отмените один экземпляр или прекратите регулярные занятия с даты.",
    )


@router.get("/org/attendance/open-actions", response_model=list[OpenAttendanceActionResponse])
def get_open_attendance_actions(
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Started lessons that still need attendance or final closure.

    This intentionally returns only persisted ``in_progress`` lesson
    instances. A planned lesson is not an overdue attendance task until a
    teacher has explicitly begun it.
    """
    try:
        return _svc(db).get_open_attendance_actions(
            user["org_id"],
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))


@router.get("/org/groups/{group_id}/attendance", response_model=GroupAttendanceResponse)
def get_group_attendance(
    group_id: str,
    month: Optional[str] = Query(None, description="Месяц YYYY-MM"),
    date_from: Optional[str] = Query(None, description="Начало периода YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="Конец периода YYYY-MM-DD"),
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Журнал посещаемости группы за месяц или указанный период."""
    try:
        return _svc(db).get_group_attendance(
            user["org_id"],
            group_id,
            month=month,
            date_from=date_from,
            date_to=date_to,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/groups/{group_id}/attendance")
def save_group_attendance(
    group_id: str,
    request: SaveAttendanceRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Сохранить отметки посещаемости за урок/дату"""
    try:
        _svc(db).save_group_attendance(
            user["org_id"],
            group_id,
            request,
            marked_by=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
        return {"ok": True, "saved": True}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/org/groups/{group_id}/attendance/start")
def start_group_attendance(
    group_id: str,
    request: StartAttendanceRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Explicitly begin a lesson before any attendance can be marked."""
    try:
        _svc(db).start_group_attendance(
            user["org_id"],
            group_id,
            request,
            started_by=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
        return {"ok": True, "started": True}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/groups/{group_id}/attendance/reconcile")
def reconcile_missed_group_attendance(
    group_id: str,
    request: StartAttendanceRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Open the journal for a past occurrence that was never started."""
    try:
        _svc(db).reconcile_missed_group_attendance(
            user["org_id"],
            group_id,
            request,
            reconciled_by=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
        return {"ok": True, "reconciled": True}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/groups/{group_id}/attendance/reconcile/cancel")
def cancel_missed_group_attendance(
    group_id: str,
    request: ReconcileMissedLessonRequest,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Cancel a missed occurrence with a mandatory reason and audit trail."""
    try:
        _svc(db).cancel_missed_group_attendance(
            user["org_id"],
            group_id,
            request,
            cancelled_by=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
        return {"ok": True, "cancelled": True}
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
