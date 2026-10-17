"""
ALGO V1 - Auth Router
REST API: org application, login, invitations, students, diagnostics, groups
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query, UploadFile, File
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import Literal, Optional
from datetime import date, datetime
from uuid import UUID

from .service import (
    AuthService,
    BUSINESS_TIMEZONE,
    MembershipConflict,
    hash_token,
    verify_access_token,
)
from .attendance_access import is_org_admin, is_org_manager
from .models import DiagnosticInvite
from .student_archive import (
    ArchiveError,
    ArchiveStudentRequest,
    ArchiveStudentResponse,
    RestoreStudentRequest,
    RestoreStudentResponse,
    StudentArchiveService,
    actor_role_for_audit,
)
from . import import_service
from .import_service import ImportPreviewResponse, ImportCommitResponse
from src.api.rate_limit import client_bucket, client_ip, enforce_rate_limit, hit_window, peek_window, rate_limit
from src.core.config import get_settings
from src.core.exceptions import InvalidCredentialsError
from . import login_throttle
from .user_sessions import SessionClient
from .cookies import (
    AUTH_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    clear_auth_cookies,
    set_auth_cookies,
)
from .dependencies import security
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
    AddStudentToGroupRequest,
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
    TeacherDashboardSummaryResponse,
)
from .dependencies import (
    get_current_active_user_payload,
    require_org_owner,
    require_org_admin,
    require_org_teacher,
    require_org_member,
    require_superadmin,
    ensure_payments_access,
    has_payments_access,
)

from sqlalchemy.exc import OperationalError

from .payments_service import CONCURRENT_EDIT_MESSAGE, is_deadlock
from src.core.database import get_db
from src.api.internal_router import has_valid_internal_token, require_internal_service_token

router = APIRouter(prefix="/auth", tags=["Authentication"])

# /auth/refresh limits: per client address (generous) and per refresh token (tight).
REFRESH_IP_LIMIT = "120/minute"
REFRESH_TOKEN_LIMIT = "10/minute"

# Endpoints that check an invite code / activation token (a secret) but have no password to throttle:
# a modest limit per client address, so the code space cannot be probed at wire speed. Each endpoint counts
# in a bucket of its own: a school behind one NAT where ~15 teachers open the invite page and accept the
# invitation within one minute must not run into the limit that the other endpoints also spend.
SECRET_LOOKUP_IP_LIMIT = "60/minute"
INVITE_INFO_RATE_ENDPOINT = "auth-invite-info"
INVITE_ACCEPT_RATE_ENDPOINT = "auth-invite-accept"
ACTIVATE_RATE_ENDPOINT = "auth-activate"

# A diagnostic invite code (8 characters, 31-letter alphabet) is looked up by the student's browser without a login, and the
# answer carries the student's name: the code space must not be probed. What a guess costs is a FAILED lookup, so those are
# budgeted per client address; a valid lookup is not counted (a class behind one school NAT opens ~30 codes in a minute).
# Calls without X-Real-IP are not a browser: diagnostic-service asks server to server, from our own address, and throttles its
# own public entries per student address.
DIAGNOSTIC_CODE_FAIL_LIMITS = ((100, 60), (600, 3600))  # (failed lookups, window in seconds) per client address
DIAGNOSTIC_CODE_RATE_ENDPOINT = "auth-diagnostic-code-fail"

# ----------- cookie helpers: the policy itself is in cookies.py ----------- #


def _auth_response_with_cookies(auth: AuthResponse) -> JSONResponse:
    """Return AuthResponse JSON body **and** set httpOnly cookies."""
    body = auth.model_dump(mode="json")
    resp = JSONResponse(content=body)
    set_auth_cookies(resp, auth.tokens.access_token, auth.tokens.refresh_token)
    return resp


def _token_response_with_cookies(tokens: TokenPair) -> JSONResponse:
    """Return TokenPair JSON body **and** set httpOnly cookies."""
    body = tokens.model_dump(mode="json")
    resp = JSONResponse(content=body)
    set_auth_cookies(resp, tokens.access_token, tokens.refresh_token)
    return resp


def _session_client(request: Optional[Request]) -> Optional[SessionClient]:
    """Where a sign-in / refresh came from, for the session row: the TRUSTED client address (the same helper the
    login throttle counts with: X-Real-IP set by nginx, never a header the client chose) and the user agent."""
    if request is None:
        return None
    ip = client_ip(request)
    return SessionClient(ip=None if ip == "unknown" else ip, user_agent=request.headers.get("user-agent"))


def _svc(db: Session, request: Optional[Request] = None) -> AuthService:
    """The service; pass the HTTP request on every route that creates or rotates a session."""
    return AuthService(db, _session_client(request))


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
    bucket: str = Depends(login_throttle.enforce_login_ip_limits),
    db: Session = Depends(get_db),
    raw_request: Request = None,
):
    """Вход в систему — возвращает JWT + org info.

    Неизвестный email, отключённый аккаунт и неверный пароль дают один и тот же ответ (401, «Неверная почта
    или пароль») за одно и то же время.

    Защита от подбора пароля (login_throttle.py): лимит попыток на адрес клиента (30/мин, 300/час); пауза на
    пару «аккаунт + адрес» после 5 неудач подряд (1, 2, 4, 8, до 15 мин); пауза на весь аккаунт после 20
    неудач за 15 мин с любых адресов (адреса, с которых владелец уже входил за последние 30 дней, под эту
    паузу не попадают). Ответ 429 + Retry-After выдаётся до поиска пользователя и проверки пароля."""
    attempt = login_throttle.begin(login_throttle.SCOPE_LOGIN, request.email, bucket)
    try:
        result = _svc(db, raw_request).login(request)
    except (InvalidCredentialsError, ValueError) as e:
        attempt.failed()  # unknown, inactive and wrong-password accounts are all counted the same way
        if isinstance(e, ValueError):  # never echo a library message: the one standard refusal
            raise InvalidCredentialsError() from None
        raise
    except Exception:  # database down, a bug: says nothing about the password, so not counted
        attempt.aborted()
        raise
    attempt.succeeded()
    return _auth_response_with_cookies(result)


@router.post("/refresh", response_model=TokenPair)
def refresh_tokens(
    request: RefreshTokenRequest,
    raw_request: Request = None,
    db: Session = Depends(get_db),
    _rl=Depends(rate_limit(REFRESH_IP_LIMIT)),
):
    """Обновление пары токенов.
    Принимает refresh_token из тела ИЛИ из httpOnly cookie.

    Two limits: a generous one per client address (a school behind one NAT returns at 08:00 and
    every browser refreshes at once) and a tight one per refresh token (a client that hammers
    the same token is broken or hostile)."""
    rt = request.refresh_token
    # Fallback: read from httpOnly cookie if body is empty
    if not rt and raw_request:
        rt = raw_request.cookies.get(REFRESH_COOKIE_NAME)
    if not rt:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh_token required")
    enforce_rate_limit(hash_token(rt), REFRESH_TOKEN_LIMIT, endpoint="auth-refresh-token")
    try:
        tokens = _svc(db, raw_request).refresh_tokens(rt)
        return _token_response_with_cookies(tokens)
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))


@router.get(
    "/invite/{code}",
    dependencies=[Depends(rate_limit(SECRET_LOOKUP_IP_LIMIT, endpoint=INVITE_INFO_RATE_ENDPOINT))],
)
def get_invite_info(code: str, db: Session = Depends(get_db)):
    """Публичная информация о приглашении (орг + роль) — для экрана принятия."""
    try:
        return _svc(db).get_invitation_info(code)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.post(
    "/invite/accept",
    response_model=AuthResponse,
    status_code=201,
    dependencies=[Depends(rate_limit(SECRET_LOOKUP_IP_LIMIT, endpoint=INVITE_ACCEPT_RATE_ENDPOINT))],
)
def accept_invite(request: AcceptInviteRequest, db: Session = Depends(get_db), raw_request: Request = None):
    """Принять приглашение в организацию — создаёт аккаунт"""
    try:
        result = _svc(db, raw_request).accept_invitation(request)
        resp = _auth_response_with_cookies(result)
        resp.status_code = 201
        return resp
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


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


@router.post(
    "/activate",
    response_model=AuthResponse,
    dependencies=[Depends(rate_limit(SECRET_LOOKUP_IP_LIMIT, endpoint=ACTIVATE_RATE_ENDPOINT))],
)
def activate_account(
    request: ActivateAccountRequest,
    db: Session = Depends(get_db),
    raw_request: Request = None,
):
    """
    Активация аккаунта по токену из письма.
    Активирует организацию и владельца, возвращает JWT.
    """
    try:
        result = _svc(db, raw_request).activate_account(request.token)
        return _auth_response_with_cookies(result)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  DIAGNOSTIC — PUBLIC (ученик без JWT переходит по QR/ссылке)             ║
# ╚═══════════════════════════════════════════════════════════════════════════╝


def _diagnostic_code_budget_key(window: int, bucket: str) -> str:
    return f"rl:{DIAGNOSTIC_CODE_RATE_ENDPOINT}:{window}:{bucket}"


def _guard_diagnostic_code_lookup(request: Request) -> Optional[str]:
    """429 + Retry-After when this browser address has spent its failed-lookup budget; else its bucket (None = not a browser).

    Every window is read and the refusal carries the LONGEST wait among the spent ones.
    """
    if not request.headers.get("x-real-ip"):
        return None
    bucket = client_bucket(request)
    spent = []
    for limit, window in DIAGNOSTIC_CODE_FAIL_LIMITS:
        failures, left = peek_window(_diagnostic_code_budget_key(window, bucket))
        if failures >= limit:
            spent.append(left)
    if spent:
        left = max(spent)
        wait = f"{-(-left // 60)} мин" if left > 90 else "минуту"
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Слишком много попыток. Подождите {wait} и попробуйте снова.",
            headers={"Retry-After": str(left)},
        )
    return bucket


def _count_diagnostic_code_miss(bucket: Optional[str]) -> None:
    if bucket is None:
        return
    for _, window in DIAGNOSTIC_CODE_FAIL_LIMITS:
        hit_window(_diagnostic_code_budget_key(window, bucket), window)


def _diagnostic_invite_exists(db: Session, code: str) -> bool:
    """Whether an invite with this code exists in ANY state (pending, started, completed, expired, cancelled)."""
    return db.query(DiagnosticInvite.id).filter(DiagnosticInvite.code == code).first() is not None


@router.get("/diagnostic/{code}")
def get_diagnostic_info(code: str, request: Request, db: Session = Depends(get_db)):
    """
    Получить информацию о диагностике по коду (публичный).
    Ученик открывает QR → видит своё имя → подтверждает.
    Неудачные поиски считаются на адрес клиента (перебор кода): 100 в минуту и 600 в час, затем 429.
    """
    bucket = _guard_diagnostic_code_lookup(request)
    # Why an invite was cancelled (the student was archived) is for diagnostic-service only: a valid internal token.
    info = _svc(db).get_diagnostic_by_code(
        code, include_cancel_details=has_valid_internal_token(request.headers.get("x-internal-service-token"))
    )
    if not info:
        _count_diagnostic_code_miss(bucket)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Диагностика не найдена")
    return info


@router.post("/diagnostic/{code}/confirm")
def confirm_diagnostic(code: str, request: Request, db: Session = Depends(get_db)):
    """Подтверждение по коду — переход pending → confirmed. Код сам по себе
    уникально определяет ученика, ввод имени не требуется."""
    bucket = _guard_diagnostic_code_lookup(request)
    try:
        return _svc(db).confirm_diagnostic(code)
    except ValueError as e:
        # Only a code that exists nowhere is a guess. The service answers «не найдена или уже начата» for a code that is
        # started / completed / expired too (a cancelled one has a text of its own) - those are states a legitimate student can meet, and are not counted.
        if bucket is not None and "не найдена" in str(e) and not _diagnostic_invite_exists(db, code):
            _count_diagnostic_code_miss(bucket)
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
    payload: dict = Depends(get_current_active_user_payload), db: Session = Depends(get_db)
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
    payload: dict = Depends(get_current_active_user_payload),
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
    raw_request: Request,
    payload: dict = Depends(get_current_active_user_payload),
    db: Session = Depends(get_db),
):
    """Сменить пароль. Текущий пароль проверяется, поэтому подбор ограничен как при входе:
    те же паузы, что и при входе: на пару «пользователь + адрес» после 5 неверных попыток подряд и на
    пользователя после 20 с любых адресов (429 + Retry-After)."""
    attempt = login_throttle.begin(
        login_throttle.SCOPE_CHANGE_PASSWORD, str(payload["sub"]), client_bucket(raw_request)
    )
    try:
        _svc(db).change_password(
            payload["sub"], request.old_password, request.new_password
        )
    except ValueError as e:
        attempt.failed()
        raise HTTPException(400, str(e))
    except Exception:
        attempt.aborted()
        raise
    attempt.succeeded()
    return MessageResponse(message="Пароль успешно изменён")


def _is_cross_site_request(request: Request) -> bool:
    """A browser says this request was started by another site (CSRF-style).

    Sec-Fetch-Site: cross-site is sent by every current browser and cannot be set by a page's script;
    Origin (sent on every POST) must be one of the configured CORS origins. Requests without either
    header (curl, server-to-server, old browsers) are not judged here: they carry no ambient cookies of
    a third-party page's making.
    """
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin is not None:
        allowed = {o.rstrip("/") for o in get_settings().cors_origins}
        return "*" not in allowed and origin.strip().rstrip("/") not in allowed
    return False


def _access_subject(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials]
) -> Optional[str]:
    """The user a VALID access token (cookie or Bearer header) belongs to, else None."""
    for token in (request.cookies.get(AUTH_COOKIE_NAME), credentials.credentials if credentials else None):
        payload = verify_access_token(token) if token else None
        if payload and payload.get("sub"):
            return str(payload["sub"])
    return None


async def _refresh_token_in_body(request: Request) -> Optional[str]:
    """The ``refresh_token`` of a JSON body, as /auth/refresh accepts it. Lenient: no body, another content type or
    a body that is not a JSON object just mean "none" (logout has never answered 422)."""
    try:
        raw = await request.body()
        data = json.loads(raw) if raw else None
    except ValueError:  # not JSON / not UTF-8
        return None
    token = data.get("refresh_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token.strip() else None


@router.post("/logout", response_model=MessageResponse)
def logout(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    body_refresh: Optional[str] = Depends(_refresh_token_in_body),
    db: Session = Depends(get_db),
):
    """Revoke the server refresh token and clear both browser cookies.

    - Started by another site (Sec-Fetch-Site: cross-site or a foreign Origin): 403, no cookie
      is touched - no site may sign a visitor out.
    - No credential at all (no auth cookie, no Bearer header): nothing to clear, so 200 WITHOUT any
      Set-Cookie.
    - Otherwise both cookies are ALWAYS cleared, also when the access token inside the (30-day)
      cookie has expired. The session of THIS device (the user may be signed in on others) is revoked
      server-side when the refresh token is a live token of that session or was superseded at most 60 s
      ago (the browser just raced a refresh); an older superseded token, or any other cookie, is just
      removed from the browser. The refresh token is the cookie or, as in /auth/refresh, ``refresh_token``
      in the JSON body (the cookie wins when both are present). A request with an access token only
      identifies the user, not a device, so it revokes no session.
    """
    if _is_cross_site_request(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Запрос выхода должен приходить с сайта ALGO")

    refresh = request.cookies.get(REFRESH_COOKIE_NAME) or body_refresh
    has_credential = bool(
        request.cookies.get(AUTH_COOKIE_NAME) or refresh or (credentials and credentials.credentials)
    )
    resp = JSONResponse(content=MessageResponse(message="Вы вышли из системы").model_dump())
    if not has_credential:
        return resp

    svc = _svc(db)
    if refresh:
        # The refresh token (the cookie, else the JSON body) names this device's session: exactly that one is
        # revoked, by a live token or one superseded at most 60 s ago; an older or unknown token revokes nothing.
        # The user's other devices stay signed in (#101).
        svc.logout_by_refresh_token(refresh)
    else:
        user_id = _access_subject(request, credentials)
        if user_id:
            svc.logout(user_id)  # an access token names the user, not the device: nothing to revoke
    return clear_auth_cookies(resp)


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


@router.get("/org/groups-overview")
def get_org_groups_overview(
    user: dict = Depends(require_org_admin), db: Session = Depends(get_db)
):
    """Группы с средним θ (последний результат каждого активного ученика) для дашборда owner/admin."""
    return _svc(db).get_org_groups_overview(user["org_id"])


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


@router.get("/org/students")
def list_students(
    class_level: Optional[int] = None,
    group_id: Optional[str] = None,
    has_group: Optional[bool] = Query(None),
    student_status: Optional[str] = Query(None, alias="status"),
    exclude_status: Optional[str] = Query(None),
    has_primary_group: Optional[bool] = Query(None),
    search: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_meta: bool = Query(False),
    user: dict = Depends(require_org_teacher),
    db: Session = Depends(get_db),
):
    """Список учеников организации.

    По умолчанию — массив (прежний контракт). С ``include_meta=true`` —
    ``{items, total, limit, offset}``, где ``total`` — точное число учеников
    в выборке (для постраничной навигации).
    """
    items, total = _svc(db).list_students_page(
        user["org_id"],
        class_level=class_level,
        group_id=group_id,
        has_group=has_group,
        status=student_status,
        exclude_status=exclude_status,
        search=search,
        limit=limit,
        offset=offset,
        has_primary_group=has_primary_group,
        actor_id=user["user_id"],
        is_org_admin=is_org_admin(user),
    )
    if include_meta:
        return {"items": items, "total": total, "limit": limit, "offset": offset}
    return items


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
    except ArchiveError as e:  # the status goes through «В архив» / «Вернуть из архива» (422)
        raise HTTPException(e.http_status, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/students/{student_id}/archive", response_model=ArchiveStudentResponse)
def archive_student(
    student_id: str,
    request: Optional[ArchiveStudentRequest] = None,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Ученик ушёл из центра: в архив. Закрывает все членства, пишет журнал групп; ничего не удаляет."""
    try:
        return StudentArchiveService(db).archive(
            user["org_id"],
            student_id,
            actor_id=user["user_id"],
            actor_role=actor_role_for_audit(user),
            reason=request.reason if request else None,
        )
    except OperationalError as e:
        raise _concurrent_edit_or_raise(e, db)
    except ArchiveError as e:
        raise HTTPException(e.http_status, str(e))


@router.post("/org/students/{student_id}/restore", response_model=RestoreStudentResponse)
def restore_student(
    student_id: str,
    request: Optional[RestoreStudentRequest] = None,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Вернуть из архива: статус и выбранные группы (заполненные или архивные идут в ``skipped``)."""
    try:
        return StudentArchiveService(db).restore(
            user["org_id"],
            student_id,
            actor_id=user["user_id"],
            actor_role=actor_role_for_audit(user),
            group_ids=request.group_ids if request else None,
        )
    except OperationalError as e:
        raise _concurrent_edit_or_raise(e, db)
    except ArchiveError as e:
        raise HTTPException(e.http_status, str(e))


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
    """Создать диагностику для существующего ученика — возвращает код/QR.

    Ученику в архиве ссылку не выдаём: 409 (ArchiveConflict), пока его не вернут из архива."""
    try:
        return _svc(db).create_diagnostic_invite(
            user["org_id"], user["user_id"], request
        )
    except ArchiveError as e:
        raise HTTPException(e.http_status, str(e))
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
    """Список групп организации (для денежных ролей — с ``price_current``, цена группы на сегодня)"""
    groups = _svc(db).list_groups(
        user["org_id"],
        teacher_id=teacher_id,
        class_level=class_level,
        actor_id=user["user_id"],
        is_org_admin=is_org_admin(user),
    )
    if groups and has_payments_access(user, db):
        # The money roles get ``price_current`` on every group.  The price is NOT a field of ``GroupResponse`` (the
        # group schema carries no money, a teacher's answer is that schema as it is): the answer is built here.
        from . import pricing  # lazy: money modules

        prices = pricing.current_group_prices(db, [g.id for g in groups])
        return JSONResponse(
            jsonable_encoder([{**g.model_dump(), "price_current": prices.get(UUID(g.id))} for g in groups])
        )
    return groups


@router.get("/org/groups/{group_id}")
def get_group(
    group_id: str,
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """Детали группы + список учеников (с последним θ/уровнем, если есть).

    Для денежных ролей — ещё ``price_current`` (цена группы на сегодня или null); остальным ключа нет."""
    try:
        detail = _svc(db).get_group_detail(
            user["org_id"],
            group_id,
            actor_id=user["user_id"],
            is_org_admin=is_org_admin(user),
        )
        if has_payments_access(user, db):
            from . import pricing  # lazy: money modules

            detail["price_current"] = pricing.current_group_prices(db, [group_id]).get(UUID(str(group_id)))
        return detail
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
        return _svc(db).update_group(
            user["org_id"],
            group_id,
            request,
            actor_id=user["user_id"],
            actor_role=user.get("org_role"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


def _concurrent_edit_or_raise(exc: OperationalError, db: Session) -> HTTPException:
    """A PostgreSQL deadlock with another edit of the same student is a 409 to repeat, never a 500."""
    if not is_deadlock(exc):
        raise exc
    db.rollback()
    return HTTPException(status.HTTP_409_CONFLICT, CONCURRENT_EDIT_MESSAGE)


def _check_fee_right(price_override: Optional[object], user: dict, db: Session) -> None:
    """An individual price is money: only a caller with the payments right may send it."""
    if price_override is not None:
        ensure_payments_access(user, db)


@router.post("/org/groups/{group_id}/students/bulk")
def bulk_add_students(
    group_id: str,
    request: BulkAddStudentsRequest,
    mode: Literal["transfer", "add"] = Query("transfer", description="transfer | add"),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Массово добавить учеников в группу.

    Registered before /students/{student_id} — a generic path param would
    otherwise greedily match the literal "bulk" segment first.
    """
    _check_fee_right(request.price_override, user, db)
    try:
        return _svc(db).bulk_add_students(
            user["org_id"], group_id, request.student_ids,
            mode=mode, price_override=request.price_override, actor_id=user["user_id"],
            payments_detail=has_payments_access(user, db),
        )
    except OperationalError as e:
        raise _concurrent_edit_or_raise(e, db)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/org/groups/{group_id}/students/{student_id}")
def add_student_to_group(
    group_id: str,
    student_id: str,
    request: Optional[AddStudentToGroupRequest] = None,
    mode: Literal["transfer", "add"] = Query("transfer", description="transfer | add"),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Добавить ученика в группу: перевести (по умолчанию) или добавить ещё одну группу."""
    price_override = request.price_override if request else None
    _check_fee_right(price_override, user, db)
    try:
        return _svc(db).add_student_to_group(
            user["org_id"], group_id, student_id,
            mode=mode, price_override=price_override, actor_id=user["user_id"],
            payments_detail=has_payments_access(user, db),
        )
    except MembershipConflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except OperationalError as e:
        raise _concurrent_edit_or_raise(e, db)
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
    except OperationalError as e:
        raise _concurrent_edit_or_raise(e, db)
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


@router.get("/org/teacher/dashboard/summary", response_model=TeacherDashboardSummaryResponse)
def get_teacher_dashboard_summary(
    business_date: Optional[str] = Query(None, description="Дата сводки YYYY-MM-DD"),
    user: dict = Depends(require_org_member),
    db: Session = Depends(get_db),
):
    """One operational snapshot for an actual teacher's dashboard.

    Administrators and owners use their own dashboards.  Keeping this route
    teacher-scoped prevents a direct /teacher URL from silently showing an
    empty dashboard filtered by a manager's user id.
    """
    if user.get("org_role") != "teacher":
        raise HTTPException(403, "Учительский дашборд доступен только преподавателю")
    try:
        selected_date = (
            date.fromisoformat(business_date)
            if business_date
            else datetime.now(BUSINESS_TIMEZONE).date()
        )
    except ValueError as exc:
        raise HTTPException(400, "business_date должен иметь формат YYYY-MM-DD") from exc

    return _svc(db).get_teacher_dashboard_summary(
        user["org_id"],
        actor_id=user["user_id"],
        business_date=selected_date,
    )


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
            is_org_manager=is_org_manager(user),
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
            is_org_manager=is_org_manager(user),
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
