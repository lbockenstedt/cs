"""QA router — token exchange and QA-specific endpoints.

POST /api/qa/auth
    Exchange a tenant-scoped QA API key for a short-lived JWT.
    The JWT grants read-only access to the key's tenant and is used by the
    QA runner for all subsequent API calls.
"""
from __future__ import annotations

# APIRouter groups the endpoints; HTTPException returns error responses.
from fastapi import APIRouter, HTTPException
# BaseModel validates the JSON request body.
from pydantic import BaseModel

# auth mints the JWT; store validates the QA API key.
from .. import auth, store

# Router mounted by app.main for the QA endpoints.
router = APIRouter()


class _QAAuthRequest(BaseModel):
    """Request body for /qa/auth: the raw QA API key issued to a tenant."""
    qa_api_key: str


@router.post("/qa/auth")
def qa_auth(body: _QAAuthRequest):
    """Exchange a QA API key for a short-lived, tenant-scoped JWT.

    The returned token is valid for 2 hours and grants read-only access to the
    tenant the key was created for.  Pass it as a standard Bearer token in the
    ``Authorization`` header for all QA runner calls.

    Returns 401 if the key is invalid or has been revoked.
    """
    # Look up the key; None means it does not exist or was revoked.
    qa_key = store.validate_qa_api_key(body.qa_api_key)
    # Reject unknown or revoked keys with 401 so the runner knows to stop.
    if not qa_key:
        raise HTTPException(status_code=401, detail="Invalid or revoked QA API key")

    # Mint a short-lived JWT bound to the key's tenant and the key id (for auditing/revocation).
    token = auth.create_qa_runner_token(
        tenant_id=qa_key.tenant_id,
        key_id=qa_key.id,
    )
    # Return an OAuth-style token response plus the tenant and lifetime so the runner can refresh.
    return {
        "access_token": token,
        "token_type": "bearer",
        "tenant_id": qa_key.tenant_id,
        "expires_in_minutes": auth._QA_TOKEN_EXPIRE_MINUTES,
    }
