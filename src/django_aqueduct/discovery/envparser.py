"""Settings inspector for ``mitol.common.envs.EnvParser`` registries (v2 IR).

Part of the ``[mitol]`` optional extra. Requires ``mitol-django-common``::

    pip install django-aqueduct[mitol]

Emits :class:`~django_aqueduct.discovery.ir.SettingField` IR so it composes
with static discovery under the single v2 pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django_aqueduct.discovery.ir import (
    Default,
    DiscoveryMethod,
    Provenance,
    SettingField,
)
from django_aqueduct.discovery.secrets import looks_secret
from django_aqueduct.discovery.static import _literal_type

if TYPE_CHECKING:
    from mitol.common.envs import EnvParser  # pragma: no cover


def _load_env_parser() -> EnvParser:
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


class EnvParserInspector:
    """Discover settings registered with :class:`mitol.common.envs.EnvParser`.

    Reads the ``_configured_vars`` registry on the global ``env`` singleton and
    converts each ``EnvVariable`` into a
    :class:`~django_aqueduct.discovery.ir.SettingField`.

    Args:
        source_module: Provenance label applied to every discovered field.
    """

    def __init__(self, source_module: str = "mitol.common.envs") -> None:
        """Store the source module label."""
        self._source_module = source_module

    def discover(self) -> list[SettingField]:
        """Return one :class:`SettingField` per variable in the registry.

        Raises:
            ImportError: If ``mitol-django-common`` is not installed.
        """
        env = _load_env_parser()
        configured: dict[str, Any] = env._configured_vars

        fields: list[SettingField] = []
        for name in sorted(configured):
            fields.append(self._to_field(name, configured[name]))
        return fields

    def _to_field(self, name: str, env_var: Any) -> SettingField:
        prov = Provenance(
            source_module=self._source_module,
            method=DiscoveryMethod.ENVPARSER,
        )
        aliases = (name,)
        type_ref = _literal_type(
            env_var.value if env_var.value is not None else env_var.default
        )

        if looks_secret(name):
            if env_var.required:
                return SettingField(
                    name=name,
                    type=type_ref,
                    default=Default.required(),
                    env_aliases=aliases,
                    required=True,
                    description=env_var.description,
                    provenance=prov,
                    dev_only=env_var.dev_only,
                )
            return SettingField(
                name=name,
                type=type_ref.with_optional(),
                default=Default.redacted(),
                env_aliases=aliases,
                description=env_var.description,
                provenance=prov,
                dev_only=env_var.dev_only,
            )

        if env_var.required:
            default = Default.required()
            optional = False
        elif env_var.default is None:
            default = Default.literal_(None)
            optional = True
        else:
            factory = isinstance(env_var.default, list | dict | set)
            default = Default.literal_(env_var.default, factory=factory)
            optional = False

        return SettingField(
            name=name,
            type=type_ref.with_optional(optional=optional),
            default=default,
            env_aliases=aliases,
            required=env_var.required,
            description=env_var.description,
            provenance=prov,
            dev_only=env_var.dev_only,
        )
