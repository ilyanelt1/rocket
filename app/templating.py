"""Shared Jinja2 environment for all application routes."""

from fastapi.templating import Jinja2Templates


templates = Jinja2Templates(directory="templates")
