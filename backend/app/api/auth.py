import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas import Credentials, TokenOut
from app.security import current_user, hash_password, issue_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILURES = 8
LOCKOUT_SECONDS = 15 * 60
# Per process, so a multi-worker deployment would want this in Redis.
_failures: dict[str, list[float]] = defaultdict(list)


def _recent_failures(email: str) -> list[float]:
    cutoff = time.monotonic() - LOCKOUT_SECONDS
    recent = [at for at in _failures[email] if at > cutoff]
    _failures[email] = recent
    return recent


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(body: Credentials, db: Session = Depends(get_db)) -> TokenOut:
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "That email already has an account")
    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    return TokenOut(token=issue_token(user.id), email=user.email)


@router.post("/login", response_model=TokenOut)
def login(body: Credentials, db: Session = Depends(get_db)) -> TokenOut:
    email = body.email.lower()
    if len(_recent_failures(email)) >= MAX_FAILURES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(user.password_hash, body.password):
        _failures[email].append(time.monotonic())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    _failures.pop(email, None)
    return TokenOut(token=issue_token(user.id), email=user.email)


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict[str, str]:
    return {"email": user.email}
