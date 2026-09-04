"""The single Jinja2 environment every router renders through.

Both routers used to build their own ``Jinja2Templates``, which meant a global
registered for templates (the preview banner needs one) had to be registered
twice and could silently be missed on the third instance somebody added. One
instance, one place to register.
"""
from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.preview import register_template_globals

templates = Jinja2Templates(directory="app/templates")
register_template_globals(templates)
