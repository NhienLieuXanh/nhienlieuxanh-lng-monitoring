"""Gom router. Prefix /api được gắn ở main.py, không ở đây."""

from fastapi import APIRouter

from app.api.routers import auth, export, forecast, ops, telemetry, terminals

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(ops.router)
api_router.include_router(terminals.router)
api_router.include_router(telemetry.router)
api_router.include_router(forecast.router)
api_router.include_router(export.router)

__all__ = ["api_router"]
