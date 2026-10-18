"""The query types, renderers and adapters this installation ships.

Two query types are built in -- ``go_mf`` and ``reaction_pickup`` -- and each
names a renderer and an adapter as ``"<name>@<version>"``.  The version is
load-bearing: a spec read out of a shared PostgreSQL store was written by some
other installation, and an installation that does not have the version it
names REFUSES the query (:class:`..errors.UnknownRenderer` /
:class:`..errors.UnknownAdapter`) rather than rendering it with whatever
renderer of that name it happens to have.  A renderer IS the prompt, and an
adapter IS how the answer is counted; the wrong version of either asks or
scores a different question with nothing downstream able to tell.

A bare ``"go_mf_v3"`` with no ``@`` is accepted and means "whatever version is
installed", which is what a spec written by hand in a local YAML wants.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, NamedTuple, Tuple

from ..errors import UnknownAdapter, UnknownRenderer
from . import adapters as _adapters
from . import renderers as _renderers

__all__ = [
    "BUILTIN_QUERY_TYPES",
    "BUILTIN_RENDERERS",
    "BUILTIN_ADAPTERS",
    "Builtin",
    "resolve_renderer",
    "resolve_adapter",
    "builtin_spec_paths",
    "load_builtin_spec",
]


class Builtin(NamedTuple):
    """One installed renderer or adapter, with the version specs may pin."""

    name: str
    version: str
    fn: Callable[..., Any]


#: ``(name, version)`` of every builtin query type YAML shipped beside this
#: module.  ``QueryRegistry.seed_builtins`` registers exactly these.
BUILTIN_QUERY_TYPES: Tuple[Tuple[str, str], ...] = (
    ("go_mf", "1"),
    ("reaction_pickup", "1"),
)

BUILTIN_RENDERERS: Dict[str, Builtin] = {
    "go_mf_v3": Builtin("go_mf_v3", "1", _renderers.go_mf_v3),
    "reaction_pickup_v4": Builtin(
        "reaction_pickup_v4", "1", _renderers.reaction_pickup_v4
    ),
}

BUILTIN_ADAPTERS: Dict[str, Builtin] = {
    "go_mf_v3": Builtin("go_mf_v3", "1", _adapters.go_mf_v3),
    "reaction_pickup_v4": Builtin(
        "reaction_pickup_v4", "1", _adapters.reaction_pickup_v4
    ),
}


def _resolve(reference: str, table: Dict[str, Builtin], what: str, error) -> Builtin:
    name, _, version = str(reference or "").partition("@")
    name = name.strip()
    version = version.strip()
    entry = table.get(name)
    if entry is None:
        raise error(
            f"no builtin {what} named {name!r} in this installation; "
            f"available: {', '.join(sorted(table)) or '(none)'}"
        )
    if version and version != entry.version:
        raise error(
            f"{what} {name!r} version {version!r} is not available in this "
            f"installation (it ships {entry.version!r}); the query type is "
            "refused rather than rendered with a different version"
        )
    return entry


def resolve_renderer(reference: str) -> Callable[..., Tuple[str, str]]:
    """Return the render function for ``"<name>"`` or ``"<name>@<version>"``."""
    return _resolve(reference, BUILTIN_RENDERERS, "renderer", UnknownRenderer).fn


def resolve_adapter(reference: str) -> Callable[..., Dict[str, Any]]:
    """Return the postprocess function for ``"<name>"`` or ``"<name>@<version>"``."""
    return _resolve(reference, BUILTIN_ADAPTERS, "adapter", UnknownAdapter).fn


def builtin_spec_paths() -> List[Any]:
    """The shipped builtin query-type YAMLs, as importlib resources."""
    from importlib.resources import files

    root = files(__package__)
    return [root.joinpath(f"{name}.yaml") for name, _ in BUILTIN_QUERY_TYPES]


def load_builtin_spec(name: str) -> Dict[str, Any]:
    """Read one shipped builtin query-type YAML into a spec mapping."""
    from importlib.resources import files

    import yaml

    known = {entry for entry, _ in BUILTIN_QUERY_TYPES}
    if name not in known:
        raise UnknownRenderer(
            f"no builtin query type named {name!r}; available: {', '.join(sorted(known))}"
        )
    text = files(__package__).joinpath(f"{name}.yaml").read_text()
    return dict(yaml.safe_load(text) or {})
