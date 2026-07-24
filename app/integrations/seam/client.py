from __future__ import annotations

import logging

from seam import Seam

from app.settings import settings

log = logging.getLogger(__name__)


def get_seam_client() -> Seam:
    """Return an authenticated Seam SDK client built from settings.seam_api_key."""
    return Seam(api_key=settings.seam_api_key)
