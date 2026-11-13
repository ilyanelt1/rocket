"""ASGI application entry point kept stable for Uvicorn and existing tooling."""

from .application import _get_session_secret, create_app

app = create_app()
