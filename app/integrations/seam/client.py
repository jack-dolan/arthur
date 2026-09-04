from __future__ import annotations

import logging

from seam import Seam

from app.preview import inert_client, is_preview_mode
from app.settings import settings

log = logging.getLogger(__name__)


def get_seam_client() -> Seam:
    """Return an authenticated Seam SDK client built from settings.seam_api_key.

    In preview mode this returns an inert stub instead: a preview must never
    program the physical door lock (app/preview.py).
    """
    if is_preview_mode():
        return inert_client("Seam")
    return Seam(api_key=settings.seam_api_key)
