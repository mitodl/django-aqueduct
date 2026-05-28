"""Runtime type inference for settings values."""

from typing import Any


def infer_annotation(value: Any) -> tuple[str, bool]:
    """Infer a Pydantic-compatible type annotation string from a runtime value.

    Returns a tuple of ``(annotation_string, needs_refinement)`` where
    ``needs_refinement`` is ``True`` when the annotation is a best-effort
    guess (e.g. the value is ``None`` or an unrecognised type).

    Note:
        ``bool`` is checked *before* ``int`` because ``bool`` is a subclass
        of ``int`` in Python.

    Args:
        value: The runtime value to inspect.

    Returns:
        A 2-tuple of the annotation string and a refinement flag.
    """
    # bool must precede int — bool is a subclass of int
    if isinstance(value, bool):
        return ("bool", False)
    if isinstance(value, int):
        return ("int", False)
    if isinstance(value, float):
        return ("float", False)
    if isinstance(value, str):
        return ("str", False)
    if isinstance(value, list):
        return ("list[Any]", False)
    if isinstance(value, dict):
        return ("dict[str, Any]", False)
    if value is None:
        return ("Any", True)
    # Unrecognised type — fall back to Any and flag for review
    return ("Any", True)
