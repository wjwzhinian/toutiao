import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette import status


logger = logging.getLogger(__name__)


async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
    )


async def integrity_error_handler(request: Request, exc: IntegrityError):
    detail = "The submitted data conflicts with an existing record"
    message = str(exc.orig).lower()
    if "username_unique" in message:
        detail = "Username already exists"
    elif "foreign key" in message:
        detail = "Referenced data was not found"
    logger.info(
        "Database constraint rejected request: path=%s error_type=%s",
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"code": 400, "message": detail, "data": None},
    )


async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    logger.error(
        "Database request failed: path=%s error_type=%s",
        request.url.path,
        type(exc).__name__,
        exc_info=False,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": 500, "message": "Database operation failed", "data": None},
    )


async def general_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled request error: path=%s error_type=%s",
        request.url.path,
        type(exc).__name__,
        exc_info=False,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": 500, "message": "Internal server error", "data": None},
    )
