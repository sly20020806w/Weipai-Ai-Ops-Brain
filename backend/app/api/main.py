"""API 入口；启动前校验环境配置。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.agent.client import GatewayError
from app.api.auth import SessionMiddleware
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.console import router as console_router
from app.api.events import router as events_router
from app.api.health import router
from app.api.operations import router as operations_router
from app.config import Settings
from app.db.session import Database
from app.graph.service import GraphNodeNotFound
from app.knowledge.service import KnowledgeEmbeddingError, KnowledgeNotFound
from app.runbooks.service import RunbookNotFound
from app.tasks.console_queries import ConsoleConflict, ConsoleNotFound


class AuthenticatedAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        # 让 FastAPI 自行管理路由版本缓存，再补充由 namespace middleware 实施的门禁。
        schema = super().openapi()
        settings: Settings = self.state.settings
        cookie_name = settings.auth_config.cookie_name if settings.auth_config else "ops_session"
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["SessionCookie"] = {
            "type": "apiKey",
            "in": "cookie",
            "name": cookie_name,
        }
        for path, methods in schema["paths"].items():
            if path == "/api" or path.startswith("/api/"):
                for method, operation in methods.items():
                    if method in {
                        "get",
                        "post",
                        "put",
                        "patch",
                        "delete",
                        "head",
                        "options",
                    } and not (path == "/api/auth/login" and method == "post"):
                        operation["security"] = [{"SessionCookie": []}]
        return schema


def create_app(settings: Settings | None = None) -> FastAPI:
    validated_settings = settings if settings is not None else Settings()
    validated_settings.validate_api_auth()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = (
            Database(validated_settings.require_database_url())
            if validated_settings.database_url is not None
            else None
        )
        application.state.database = database
        try:
            yield
        finally:
            if database is not None:
                await database.dispose()

    application = AuthenticatedAPI(title="Weipai AI Ops Brain", version="0.1.0", lifespan=lifespan)
    application.state.settings = validated_settings
    application.add_middleware(SessionMiddleware)
    application.include_router(auth_router)
    application.include_router(router)
    application.include_router(events_router)
    application.include_router(console_router)
    application.include_router(operations_router)
    application.include_router(chat_router)

    for missing_type in (GraphNodeNotFound, KnowledgeNotFound, RunbookNotFound):
        application.add_exception_handler(
            missing_type,
            lambda request, error: JSONResponse({"detail": str(error)}, status_code=404),
        )

    async def unavailable_gateway(request: Request, error: Exception) -> Response:
        return JSONResponse({"detail": "AI 向量服务暂时不可用，编辑未提交"}, status_code=503)

    application.add_exception_handler(GatewayError, unavailable_gateway)
    application.add_exception_handler(KnowledgeEmbeddingError, unavailable_gateway)

    @application.exception_handler(ConsoleNotFound)
    async def missing_record(request: Request, error: ConsoleNotFound) -> Response:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @application.exception_handler(ConsoleConflict)
    async def conflicting_operation(request: Request, error: ConsoleConflict) -> Response:
        return JSONResponse({"detail": str(error)}, status_code=409)

    @application.exception_handler(SQLAlchemyError)
    async def unavailable_storage(request: Request, error: SQLAlchemyError) -> Response:
        return JSONResponse({"detail": "数据存储暂时不可用"}, status_code=503)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> Response:
        if request.url.path == "/api/auth/login":
            # FastAPI 默认把无效字段原值放进 422；登录响应不能泄露密码或未知密钥。
            return JSONResponse(
                {
                    "detail": [
                        {key: item[key] for key in ("loc", "msg", "type")}
                        for item in error.errors()
                    ]
                },
                status_code=422,
            )
        return await request_validation_exception_handler(request, error)

    return application


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
