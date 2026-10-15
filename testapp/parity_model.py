"""A tiny AqueductSettings model used to exercise the parity command."""

from pydantic import Field
from pydantic_settings import BaseSettings

from django_aqueduct.adapter import PLUGIN_MARKER, UNRESOLVED_MARKER


class AqueductSettings(BaseSettings):
    DEBUG: bool = False
    SITE_NAME: str = "app"
    ONLY_IN_MODEL: int = 1
    # Declared but withheld: the adapter leaves both of these to the base, so
    # the model makes no claim about them and neither can diverge from legacy.
    # One is bound in parity_legacy and one is not -- the two sides of the
    # asymmetry that filtering only the model's dump would introduce.
    WITHHELD_BOUND_IN_LEGACY: str | None = Field(
        default=None, json_schema_extra={UNRESOLVED_MARKER: True}
    )
    WITHHELD_ABSENT_FROM_LEGACY: bool = Field(
        default=True, json_schema_extra={PLUGIN_MARKER: "aqueduct-no-such-plugin"}
    )
