import traceback
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from db import SessionLocal, log_api_error


ALLOWED_TOOLS = {
    "VICP_Attorney_Fees",
    "Settlement_Predictor",
    "Injury_Intelligence",
    "Demand_optimizer",
    "Demand_Quality_Assurance",
    "Vehicle_Loss_Analyzer",
    "Playbook_Chatbot",
    "casevalue",
    "Email2AI",
    "Vicp_Prediction",
    "FAA_MedCert_Case_Builder",
    "bookbuilder",
    "IntakeOps"
}

async def extract_tool_api(request: Request):
    path = request.url.path
    parts = path.strip("/").split("/")
    tool = parts[0] if parts and parts[0] else "root"
    return tool, path

async def save_error_to_db(**kwargs):
    tool = kwargs.get("tool")
    if tool not in ALLOWED_TOOLS:
        return
    db = SessionLocal()
    try:
        await log_api_error(db, **kwargs)
    finally:
        db.close()


# Unhandled crashes
async def unhandled_exception_handler(request: Request, exc: Exception):
    tool, api = await extract_tool_api(request)

    await save_error_to_db(
        tool=tool,
        api=api,
        error_type=type(exc).__name__,
        category="APP_CRASH",
        message=str(exc),
        stack_trace="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        status_code=500
    )

    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


# Explicit HTTPException
async def http_exception_handler(request: Request, exc: HTTPException):
    tool, api = await extract_tool_api(request)

    await save_error_to_db(
        tool=tool,
        api=api,
        error_type="HTTPException",
        category="HTTP_ERROR",
        message=str(exc.detail),
        status_code=exc.status_code
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )


# Validation errors
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    tool, api = await extract_tool_api(request)

    await save_error_to_db(
        tool=tool,
        api=api,
        error_type="RequestValidationError",
        category="VALIDATION_ERROR",
        message=str(exc.errors()),
        status_code=422
    )

    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()}
    )


# Framework errors (404, 405)
async def starlette_exception_handler(request: Request, exc: StarletteHTTPException):
    tool, api = await extract_tool_api(request)

    await save_error_to_db(
        tool=tool,
        api=api,
        error_type="StarletteHTTPException",
        category="FRAMEWORK_ERROR",
        message=str(exc.detail),
        status_code=exc.status_code
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )
