"""A tiny AqueductSettings model used to exercise the parity command."""

from pydantic_settings import BaseSettings


class AqueductSettings(BaseSettings):
    DEBUG: bool = False
    SITE_NAME: str = "app"
    ONLY_IN_MODEL: int = 1
