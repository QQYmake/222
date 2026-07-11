"""Auth middleware: constant-time token verification.

INPUT:  HTTP request headers, expected token(s)
OUTPUT: None (pass) or raise HTTPException 401/403

Two token types:
  - upload token: checked via X-Upload-Token header
  - read token:   checked via Authorization: Bearer <token>
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


def verify_upload_token(
    request: Request,
    expected_token: str,
) -> None:
    """Verify the X-Upload-Token header.

    INPUT:  Request (with headers), expected_token string
    OUTPUT: None if valid
    RAISES: HTTPException 401 if missing, 403 if mismatch
    """
    provided = request.headers.get("X-Upload-Token")
    if not provided:
        raise HTTPException(status_code=401, detail="Missing upload token")
    if not hmac.compare_digest(provided, expected_token):
        raise HTTPException(status_code=403, detail="Invalid upload token")


def verify_read_token(
    request: Request,
    expected_token: str,
) -> None:
    """Verify the Authorization: Bearer <token> header.

    INPUT:  Request (with headers), expected_token string
    OUTPUT: None if valid
    RAISES: HTTPException 401 if missing, 403 if mismatch
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    provided = auth[len("Bearer "):]
    if not hmac.compare_digest(provided, expected_token):
        raise HTTPException(status_code=403, detail="Invalid read token")
