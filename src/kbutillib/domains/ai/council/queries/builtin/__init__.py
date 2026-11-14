"""The query types, renderers and adapters this installation ships.

Two query types are built in -- ``go_mf``, at three versions (``@1``, ``@3``
and ``@4``), and ``reaction_pickup`` -- and each names a renderer and an
adapter as ``"<name>@<version>"``.  The version is
load-bearing: a spec read out of a shared PostgreSQL store was written by some
other installation, and an installation that does not have the version it
names REFUSES the query (:class:`..errors.UnknownRenderer` /
:class:`..errors.UnknownAdapter`) rather than rendering it with whatever
renderer of that name it happens to have.  A renderer IS the prompt, and an
adapter IS how the answer is counted; the wrong version of either asks or
scores a different question with nothing downstream able to tell.

A bare ``"go_mf_v3"`` with no ``@`` is accepted and means "whatever version is
installed", which is what a spec written by hand in a local YAML wants.

The QUERY TYPES are addressed the other way round, by an explicit
``(name, version)``: a renderer name has one installed version, but ``go_mf``
has three, and they ask different questions.  See
:data:`BUILTIN_SPEC_FILES` and :func:`load_builtin_spec`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from ..errors import UnknownAdapter, UnknownRenderer
from . import adapters as _adapters
from . import renderers as _renderers

__all__ = [
    "BUILTIN_QUERY_TYPES",
    "BUILTIN_SPEC_FILES",
    "BUILTIN_RENDERERS",
    "BUILTIN_ADAPTERS",
    "Builtin",
    "resolve_renderer",
    "resolve_adapter",
    "builtin_spec_paths",
    "builtin_versions",
    "load_builtin_spec",
]


class Builtin(NamedTuple):
    """One installed renderer or adapter, with the version specs may pin."""

    name: str
    version: str
    fn: Callable[..., Any]


#: ``(name, version)`` of every builtin query type YAML shipped beside this
#: module.  ``QueryRegistry.seed_builtins`` registers exactly these.
#:
#: A name may appear more than once: ``go_mf`` ships ``@1``, ``@3`` and ``@4``,
#: which are three DIFFERENT questions, not three spellings of one.  ``@4`` is
#: the version the 2026-10 GO corpus was decided under, so a bench that is not
#: run under it is not comparable to that baseline; ``@3`` is committed beside
#: it because the only difference between them is one schema field and keeping
#: both makes that provable (see ``tests/.../test_go_mf_v4_builtin.py``).
#: ``@2`` is deliberately absent -- it was an intermediate that nothing was
#: decided under.
BUILTIN_QUERY_TYPES: Tuple[Tuple[str, str], ...] = (
    ("go_mf", "1"),
    ("go_mf", "3"),
    ("go_mf", "4"),
    ("reaction_pickup", "1"),
)

#: ``(name, version)`` -> the YAML file beside this module that holds it.
#:
#: A MAP rather than a naming rule, because the rule would need a special case
#: already: ``go_mf@1`` and ``reaction_pickup@1`` predate versioned builtins
#: and are named for the query type alone.  Addressing a spec file by name
#: ALONE is the specific bug this replaces -- it loaded ``go_mf.yaml`` for
#: every ``go_mf`` entry, so a second version registered the FIRST version's
#: prompts under the second version's number, which nothing downstream could
#: have detected.  Every key here must appear in :data:`BUILTIN_QUERY_TYPES`
#: and vice versa.
BUILTIN_SPEC_FILES: Dict[Tuple[str, str], str] = {
    ("go_mf", "1"): "go_mf.yaml",
    ("go_mf", "3"): "go_mf_v3.yaml",
    ("go_mf", "4"): "go_mf_v4.yaml",
    ("reaction_pickup", "1"): "reaction_pickup.yaml",
}

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
    return [root.joinpath(BUILTIN_SPEC_FILES[entry]) for entry in BUILTIN_QUERY_TYPES]


def builtin_versions(name: str) -> List[str]:
    """Every shipped version of builtin query type *name*, in shipped order."""
    return [version for entry, version in BUILTIN_QUERY_TYPES if entry == name]


def load_builtin_spec(name: str, version: Optional[str] = None) -> Dict[str, Any]:
    """Read one shipped builtin query-type YAML into a spec mapping.

    *version* may be omitted only when the name ships exactly ONE version.
    Where it ships several -- ``go_mf`` ships ``@1``, ``@3`` and ``@4`` -- the
    omission is REFUSED rather than resolved to the newest, for the same
    reason a spec naming an absent renderer version is refused: the versions
    are different prompts and different answer schemas, so guessing hands the
    caller a different question under a name it thought it had pinned.

    Raises:
        UnknownRenderer: no such builtin name, no such version of it, or the
            name is ambiguous and *version* was not given.
    """
    from importlib.resources import files

    import yaml

    known = {entry for entry, _ in BUILTIN_QUERY_TYPES}
    if name not in known:
        raise UnknownRenderer(
            f"no builtin query type named {name!r}; available: {', '.join(sorted(known))}"
        )
    versions = builtin_versions(name)
    if version is None:
        if len(versions) != 1:
            raise UnknownRenderer(
                f"builtin query type {name!r} ships {len(versions)} versions "
                f"({', '.join(versions)}); name the one you want -- they are "
                "different questions, not spellings of one"
            )
        version = versions[0]
    version = str(version)
    filename = BUILTIN_SPEC_FILES.get((name, version))
    if filename is None:
        raise UnknownRenderer(
            f"builtin query type {name!r} has no version {version!r} in this "
            f"installation (it ships {', '.join(versions)})"
        )
    text = files(__package__).joinpath(filename).read_text()
    spec = dict(yaml.safe_load(text) or {})
    # The file is addressed by (name, version); it must also SAY so.  A map
    # entry pointing at the wrong file is otherwise invisible: the spec would
    # be stored under the key asked for while carrying another version's
    # prompts, and every prompt hash taken from it would be wrong.
    if (spec.get("name"), str(spec.get("version", "1"))) != (name, version):
        raise UnknownRenderer(
            f"builtin spec file {filename!r} declares "
            f"{spec.get('name')!r}@{spec.get('version')!r} but is registered "
            f"as {name!r}@{version!r}"
        )
    return spec
