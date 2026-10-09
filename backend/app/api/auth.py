"""登录 HTTP 适配与整个 /api 命名空间的会话门禁。"""

import hmac
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.config import AuthConfig
from app.auth.identity import Principal
from app.auth.service import AuthService, LoginRejected
from app.config import Settings
from app.db.session import Database

router = APIRouter(prefix="/api/auth", tags=["登录与会话"])
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DOCUMENT_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def get_auth_service(request: Request) -> AuthService:
    settings: Settings = request.app.state.settings
    database: Database | None = getattr(request.app.state, "database", None)
    if settings.auth_config is None or database is None:
        raise HTTPException(503, "登录需要 AUTH_CONFIG 和 DATABASE_URL")
    return AuthService(database, settings.auth_config)


def require_principal(request: Request) -> Principal:
    principal: object = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise HTTPException(401, "请先登录")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


def check_origin(request: Request, config: AuthConfig) -> bool:
    origin = request.headers.get("origin")
    return (origin is None or origin == config.public_origin) and request.headers.get(
        "sec-fetch-site"
    ) != "cross-site"


class SessionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] not in {"http", "websocket"} or not (
            path == "/api" or path.startswith("/api/") or path.rstrip("/") in DOCUMENT_PATHS
        ):
            await self.app(scope, receive, send)
            return
        # 此阶段不开放 WebSocket，避免将来新增路由遗漏会话门禁。
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope, receive)
        error: HTTPException | None = None
        settings: Settings = request.app.state.settings
        login = path == "/api/auth/login" and request.method == "POST"
        try:
            if login:
                if settings.auth_config is None:
                    raise HTTPException(503, "登录需要 AUTH_CONFIG 和 DATABASE_URL")
                if (
                    not check_origin(request, settings.auth_config)
                    or request.headers.get("x-ops-login") != "1"
                ):
                    raise HTTPException(403, "登录来源校验失败，请携带 X-Ops-Login: 1")
            else:
                if settings.auth_config is None:
                    raise HTTPException(401, "请先登录")
                cookie = request.cookies.get(settings.auth_config.cookie_name, "")
                if not cookie:
                    raise HTTPException(401, "请先登录")
                service = get_auth_service(request)
                principal = await service.authenticate(cookie)
                if principal is None:
                    raise HTTPException(401, "会话无效或已过期，请重新登录")
                request.state.principal = principal
                if request.method not in SAFE_METHODS and (
                    not check_origin(request, settings.auth_config)
                    or not hmac.compare_digest(
                        request.headers.get("x-csrf-token", "").encode(),
                        principal.csrf_token.encode(),
                    )
                ):
                    raise HTTPException(403, "CSRF 校验失败")
        except HTTPException as caught:
            error = caught
        except (SQLAlchemyError, OSError, TimeoutError):
            error = HTTPException(503, "会话存储暂时不可用")
        if error is not None:
            await JSONResponse(
                {"detail": error.detail},
                status_code=error.status_code,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return

        async def no_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, no_cache)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=200, strict=True)
    password: SecretStr = Field(min_length=1, max_length=256)


class SessionResponse(BaseModel):
    actor: str
    expires_at: datetime
    csrf_token: str


def session_response(principal: Principal) -> SessionResponse:
    return SessionResponse(
        actor=principal.actor, expires_at=principal.expires_at, csrf_token=principal.csrf_token
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    login_marker: Annotated[Literal["1"], Header(alias="X-Ops-Login")],
) -> SessionResponse:
    try:
        issued = await service.login(body.username, body.password.get_secret_value())
    except LoginRejected as error:
        raise HTTPException(
            429 if error.locked else 401,
            str(error),
            headers={"Retry-After": str(service.config.login_lock_seconds)}
            if error.locked
            else None,
        ) from None
    except (SQLAlchemyError, OSError, TimeoutError):
        raise HTTPException(503, "会话存储暂时不可用") from None
    response.set_cookie(
        service.config.cookie_name,
        issued.cookie,
        max_age=service.config.session_ttl_seconds,
        httponly=True,
        secure=service.config.secure_cookie,
        samesite="strict",
        path="/",
    )
    return session_response(issued.principal)


@router.get("/me")
async def me(principal: CurrentPrincipal) -> SessionResponse:
    return session_response(principal)


@router.post("/logout", status_code=204)
async def logout(
    principal: CurrentPrincipal,
    service: Annotated[AuthService, Depends(get_auth_service)],
    csrf_token: Annotated[str, Header(alias="X-CSRF-Token")],
) -> Response:
    try:
        await service.logout(principal)
    except (SQLAlchemyError, OSError, TimeoutError):
        raise HTTPException(503, "会话存储暂时不可用") from None
    response = Response(status_code=204)
    response.delete_cookie(
        service.config.cookie_name,
        path="/",
        secure=service.config.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return response
