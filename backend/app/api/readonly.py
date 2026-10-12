"""首次只读上线不接纳尚未开放的 AI 任务或内容修改；会话操作保留。"""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings


class ReadOnlyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope, receive)
            settings: Settings = request.app.state.settings
            if (
                settings.read_only
                and request.method not in {"GET", "HEAD", "OPTIONS"}
                and (request.url.path == "/api" or request.url.path.startswith("/api/"))
                and request.url.path not in {"/api/auth/login", "/api/auth/logout"}
            ):
                await JSONResponse(
                    {"detail": "当前为首次只读上线，仅开放查询；此操作尚未开放"},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)
