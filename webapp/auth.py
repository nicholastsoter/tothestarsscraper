"""HTTP Basic Auth for /api/* routes — a single shared username/password
pair from the environment. This is a small internal tool, not a multi-user
system, so there's no user table or session management."""

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    expected_user = os.environ.get("WEBAPP_USER", "")
    expected_password = os.environ.get("WEBAPP_PASSWORD", "")

    user_ok = secrets.compare_digest(credentials.username, expected_user)
    password_ok = secrets.compare_digest(credentials.password, expected_password)

    if not (expected_user and expected_password and user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
