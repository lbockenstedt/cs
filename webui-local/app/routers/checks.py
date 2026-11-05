"""Placeholder "checks" API: lists health checks (none are defined yet)."""
# FastAPI router class used to group the endpoints in this module.
from fastapi import APIRouter

# Router mounted by app.main; holds the /checks endpoint.
router = APIRouter()


@router.get("/checks")
def list_checks():
    """Return the list of configured checks; always empty until checks are implemented."""
    # No checks exist yet, so return an empty list to keep the frontend contract stable.
    return []
