from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record_audit_event
from ..auth import get_current_user, verify_dummy_password, verify_password
from ..db import get_db
from ..models import User
from ..templating import templates

router = APIRouter()

@router.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.username == username.strip()))
    password_valid = (
        verify_password(password, user.password_hash)
        if user is not None and user.is_active
        else verify_dummy_password(password)
    )
    if user is None or not user.is_active or not password_valid:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль"},
            status_code=401,
        )
    request.session.clear()
    request.session["user_id"] = user.id
    record_audit_event(db, user.id, "auth.login", "user", user.id)
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record_audit_event(db, user.id, "auth.logout", "user", user.id)
    db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
