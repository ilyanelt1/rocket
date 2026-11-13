from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db import get_db

router = APIRouter()


@router.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@router.get("/health/ready", include_in_schema=False)
def health_ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return Response(
            content='{"status":"error","database":"unavailable"}',
            status_code=503,
            media_type="application/json",
        )
    return {"status": "ok", "database": "ok"}
