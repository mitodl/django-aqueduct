"""Settings inspector for mitol.common.envs.EnvParser registries.

This inspector is part of the ``[mitol]`` optional extra. It requires
``mitol-django-common`` to be installed::

    pip install django-aqueduct[mitol]
"""

from typing import TYPE_CHECKING, Any

from django_aqueduct.discovery.base import DiscoveredField

if TYPE_CHECKING:
    from mitol.common.envs import EnvParser  # pragma: no cover

# Maps the name of the EnvParser method used to declare a variable to the
# corresponding Pydantic-compatible type annotation string.
_PARSER_TYPE_MAP: dict[str, str] = {
    "get_string": "str",
    "get_bool": "bool",
    "get_int": "int",
    "get_list_literal": "list[Any]",
    "get_delimited_list": "list[str]",
    "get_crontab_kwargs": "dict[str, Any]",
}


def _load_env_parser() -> "EnvParser":
    """Return the global EnvParser instance from mitol.common.envs.

    Raises:
        ImportError: With a helpful install hint when ``mitol-django-common``
            is not installed.
    """
    try:
        from mitol.common.envs import env  # noqa: PLC0415

        return env
    except ImportError as exc:
        raise ImportError(
            "EnvParserInspector requires 'mitol-django-common'. "
            "Install it with: pip install django-aqueduct[mitol]"
        ) from exc


def _annotation_for_var(env_var: Any) -> tuple[str, bool]:
    """Return the annotation string and needs_refinement flag for an EnvVariable.

    The parser function name is not stored directly on EnvVariable, so we
    infer the type from the runtime value as a fallback, then override with
    a precise annotation when we can identify the parser by checking which
    typed accessor was used (stored in _configured_vars metadata).

    In practice callers pass the raw EnvVariable and we check the value type
    since EnvVariable is a NamedTuple without parser metadata.  The value
    field already holds the parsed (typed) value, so standard type inference
    gives us the right result for bool/int/str.  list/dict need the parser
    name which we don't have here, so we fall back to list[Any]/dict[str,Any].

    Args:
        env_var: An ``EnvVariable`` namedtuple instance.

    Returns:
        A 2-tuple of annotation string and needs_refinement flag.
    """
    from django_aqueduct.discovery.type_inference import (
        infer_annotation,  # noqa: PLC0415
    )

    return infer_annotation(
        env_var.value if env_var.value is not None else env_var.default
    )


class EnvParserInspector:
    """Discover settings registered with :class:`mitol.common.envs.EnvParser`.

    Reads the ``_configured_vars`` registry on the global ``env`` singleton
    and converts each :class:`~mitol.common.envs.EnvVariable` into a
    :class:`~django_aqueduct.discovery.base.DiscoveredField`.

    Requires the ``[mitol]`` extra::

        pip install django-aqueduct[mitol]

    Args:
        source_module: Override the ``source_module`` label applied to every
            discovered field.  Defaults to ``"mitol.common.envs"``.

    Example::

        from django_aqueduct.discovery.envparser import EnvParserInspector

        inspector = EnvParserInspector()
        fields = inspector.discover()
    """

    def __init__(self, source_module: str = "mitol.common.envs") -> None:
        """Store the source module label."""
        self._source_module = source_module

    def discover(self) -> list[DiscoveredField]:
        """Return one field per variable registered with EnvParser.

        Returns:
            Fields sorted by name for deterministic output.

        Raises:
            ImportError: If ``mitol-django-common`` is not installed.
        """
        env = _load_env_parser()
        configured: dict[str, Any] = env._configured_vars

        fields: list[DiscoveredField] = []
        for name in sorted(configured):
            env_var = configured[name]
            annotation, needs_refinement = _annotation_for_var(env_var)

            fields.append(
                DiscoveredField(
                    name=name,
                    type_annotation=annotation,
                    default=env_var.default,
                    description=env_var.description,
                    required=env_var.required,
                    source_module=self._source_module,
                    dev_only=env_var.dev_only,
                    needs_refinement=needs_refinement,
                )
            )

        return fields
