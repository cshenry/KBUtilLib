"""``kbu buildplan`` — machine-readable build contract validator.

Validates ``subprojects/<name>/buildplan.json`` against the KBU conductor
schema.  All errors are collected before raising so the caller sees the full
list at once, mirroring AIAssistant's :class:`TaskPlanError` pattern.

Programmatic API::

    from kbutillib.cli.buildplan import load_buildplan, BuildPlanError
    try:
        load_buildplan("subprojects/my_sp/buildplan.json")
    except BuildPlanError as exc:
        for err in exc.errors:
            print(err)

CLI::

    kbu buildplan validate subprojects/my_sp/buildplan.json
"""

from __future__ import annotations

import json
from pathlib import Path

import click


# ── exception ─────────────────────────────────────────────────────────────────


class BuildPlanError(ValueError):
    """Raised when a buildplan fails validation.

    Carries the full list of collected error messages in ``errors``.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Invalid buildplan:\n  - " + "\n  - ".join(errors))


# ── validator ──────────────────────────────────────────────────────────────────

#: Valid values for ``test.data_source``.
_VALID_DATA_SOURCES = {"sampled-real", "synthetic"}


def validate_buildplan(data: dict) -> list[str]:
    """Validate an already-parsed buildplan dict.

    Returns a list of error strings (empty list means valid).  Never raises;
    the caller decides what to do with the errors.

    Validation rules:
    - Top-level ``subproject`` (string) and ``notebooks`` (list) are required.
    - Each notebook requires ``slug`` (string), ``purpose`` (string),
      ``depends_on`` (list), and ``helpers`` (list).
    - Duplicate notebook slugs are rejected.
    - ``depends_on`` entries must reference notebook slugs that appear STRICTLY
      EARLIER in the ``notebooks`` list (no self-reference, no forward
      reference, no cycles).
    - Each helper requires ``name`` (string), ``signature`` (string),
      ``contract`` (string), and ``test`` (dict).
    - Duplicate helper names within a notebook are rejected.
    - Each test requires ``data_source`` (one of ``sampled-real`` / ``synthetic``),
      ``data_spec`` (string), and ``assertions`` (non-empty list of strings).
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        errors.append("top-level buildplan must be a JSON object")
        return errors

    # ── top-level required fields ─────────────────────────────────────────────

    subproject = data.get("subproject")
    if subproject is None:
        errors.append("missing required top-level field 'subproject'")
    elif not isinstance(subproject, str) or not subproject.strip():
        errors.append("'subproject' must be a non-empty string")

    notebooks_raw = data.get("notebooks")
    if notebooks_raw is None:
        errors.append("missing required top-level field 'notebooks'")
        return errors  # cannot continue without notebooks list
    if not isinstance(notebooks_raw, list):
        errors.append("'notebooks' must be a list")
        return errors

    # ── per-notebook validation ───────────────────────────────────────────────

    seen_slugs: dict[str, int] = {}   # slug -> first index (for dup detection)
    # Build the ordered slug list for depends_on resolution; we walk notebooks in
    # order so earlier slugs are added incrementally.
    ordered_slugs: list[str] = []

    for nb_idx, nb in enumerate(notebooks_raw):
        nb_label = f"notebooks[{nb_idx}]"

        if not isinstance(nb, dict):
            errors.append(f"{nb_label}: must be a JSON object")
            ordered_slugs.append("")  # placeholder to keep indexing consistent
            continue

        slug = nb.get("slug")
        if slug is None:
            errors.append(f"{nb_label}: missing required field 'slug'")
            slug = ""
        elif not isinstance(slug, str) or not slug.strip():
            errors.append(f"{nb_label}: 'slug' must be a non-empty string")
            slug = ""

        nb_label_with_slug = f"notebooks[{nb_idx}] ({slug!r})" if slug else nb_label

        # Duplicate slug check
        if slug:
            if slug in seen_slugs:
                errors.append(
                    f"{nb_label_with_slug}: duplicate slug; first seen at index "
                    f"{seen_slugs[slug]}"
                )
            else:
                seen_slugs[slug] = nb_idx

        # purpose
        purpose = nb.get("purpose")
        if purpose is None:
            errors.append(f"{nb_label_with_slug}: missing required field 'purpose'")
        elif not isinstance(purpose, str) or not purpose.strip():
            errors.append(f"{nb_label_with_slug}: 'purpose' must be a non-empty string")

        # depends_on
        depends_on = nb.get("depends_on")
        if depends_on is None:
            errors.append(f"{nb_label_with_slug}: missing required field 'depends_on'")
            depends_on = []
        elif not isinstance(depends_on, list):
            errors.append(f"{nb_label_with_slug}: 'depends_on' must be a list")
            depends_on = []
        else:
            for dep in depends_on:
                if not isinstance(dep, str):
                    errors.append(
                        f"{nb_label_with_slug}: 'depends_on' entries must be strings"
                    )
                    continue
                # Must reference a slug that appears STRICTLY EARLIER
                if dep == slug:
                    errors.append(
                        f"{nb_label_with_slug}: 'depends_on' contains self-reference {dep!r}"
                    )
                elif dep not in ordered_slugs:
                    # Either unknown or a forward/non-existent reference
                    # Check if it appears later in the list
                    all_slugs_after = [
                        nb2.get("slug")
                        for nb2 in notebooks_raw[nb_idx + 1:]
                        if isinstance(nb2, dict)
                    ]
                    if dep in all_slugs_after:
                        errors.append(
                            f"{nb_label_with_slug}: 'depends_on' contains forward "
                            f"reference to {dep!r} (appears later in notebooks list)"
                        )
                    else:
                        errors.append(
                            f"{nb_label_with_slug}: 'depends_on' references unknown "
                            f"slug {dep!r}"
                        )

        # helpers
        helpers = nb.get("helpers")
        if helpers is None:
            errors.append(f"{nb_label_with_slug}: missing required field 'helpers'")
            helpers = []
        elif not isinstance(helpers, list):
            errors.append(f"{nb_label_with_slug}: 'helpers' must be a list")
            helpers = []
        else:
            _validate_helpers(helpers, nb_label_with_slug, errors)

        # Record this slug for subsequent depends_on checks
        ordered_slugs.append(slug)

    return errors


def _validate_helpers(helpers: list, nb_label: str, errors: list[str]) -> None:
    """Validate the helpers list for one notebook, appending to *errors* in place."""
    seen_helper_names: dict[str, int] = {}

    for h_idx, helper in enumerate(helpers):
        h_label = f"{nb_label}.helpers[{h_idx}]"

        if not isinstance(helper, dict):
            errors.append(f"{h_label}: must be a JSON object")
            continue

        name = helper.get("name")
        if name is None:
            errors.append(f"{h_label}: missing required field 'name'")
            name = ""
        elif not isinstance(name, str) or not name.strip():
            errors.append(f"{h_label}: 'name' must be a non-empty string")
            name = ""

        h_label_with_name = f"{nb_label}.helpers[{h_idx}] ({name!r})" if name else h_label

        # Duplicate helper name check (within this notebook)
        if name:
            if name in seen_helper_names:
                errors.append(
                    f"{h_label_with_name}: duplicate helper name; first seen at index "
                    f"{seen_helper_names[name]}"
                )
            else:
                seen_helper_names[name] = h_idx

        # signature
        signature = helper.get("signature")
        if signature is None:
            errors.append(f"{h_label_with_name}: missing required field 'signature'")
        elif not isinstance(signature, str) or not signature.strip():
            errors.append(f"{h_label_with_name}: 'signature' must be a non-empty string")

        # contract
        contract = helper.get("contract")
        if contract is None:
            errors.append(f"{h_label_with_name}: missing required field 'contract'")
        elif not isinstance(contract, str) or not contract.strip():
            errors.append(f"{h_label_with_name}: 'contract' must be a non-empty string")

        # test
        test = helper.get("test")
        if test is None:
            errors.append(f"{h_label_with_name}: missing required field 'test'")
        elif not isinstance(test, dict):
            errors.append(f"{h_label_with_name}: 'test' must be a JSON object")
        else:
            _validate_test(test, h_label_with_name, errors)


def _validate_test(test: dict, h_label: str, errors: list[str]) -> None:
    """Validate a single helper test dict, appending to *errors* in place."""
    # data_source
    data_source = test.get("data_source")
    if data_source is None:
        errors.append(f"{h_label}.test: missing required field 'data_source'")
    elif data_source not in _VALID_DATA_SOURCES:
        errors.append(
            f"{h_label}.test: 'data_source' must be one of "
            f"{sorted(_VALID_DATA_SOURCES)!r}; got {data_source!r}"
        )

    # data_spec
    data_spec = test.get("data_spec")
    if data_spec is None:
        errors.append(f"{h_label}.test: missing required field 'data_spec'")
    elif not isinstance(data_spec, str) or not data_spec.strip():
        errors.append(f"{h_label}.test: 'data_spec' must be a non-empty string")

    # assertions
    assertions = test.get("assertions")
    if assertions is None:
        errors.append(f"{h_label}.test: missing required field 'assertions'")
    elif not isinstance(assertions, list):
        errors.append(f"{h_label}.test: 'assertions' must be a list")
    elif len(assertions) == 0:
        errors.append(f"{h_label}.test: 'assertions' must not be empty")
    else:
        for a_idx, assertion in enumerate(assertions):
            if not isinstance(assertion, str) or not assertion.strip():
                errors.append(
                    f"{h_label}.test.assertions[{a_idx}]: must be a non-empty string"
                )


# ── public programmatic entry points ──────────────────────────────────────────


def load_buildplan(path: str | Path) -> dict:
    """Load and validate a buildplan.json file.

    Args:
        path: Path to the ``buildplan.json`` file.

    Returns:
        The parsed buildplan dict (guaranteed valid).

    Raises:
        BuildPlanError: If the buildplan is malformed or violates any rule.
            ``exc.errors`` contains the full list of collected error strings.
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    path = Path(path)
    raw_text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise BuildPlanError([f"not valid JSON: {exc}"]) from exc

    errors = validate_buildplan(data)
    if errors:
        raise BuildPlanError(errors)
    return data


# ── Click command group ────────────────────────────────────────────────────────


@click.group(name="buildplan")
def buildplan_cmd() -> None:
    """Manage and validate kbu buildplan.json files."""


@buildplan_cmd.command(name="validate")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.pass_context
def validate_cmd(ctx: click.Context, path: str) -> None:
    """Validate a buildplan.json at PATH.

    Exits 0 and prints a success message when the buildplan is valid.
    Exits 1 and prints ALL validation errors (not just the first) when
    the buildplan is invalid.
    """
    try:
        load_buildplan(path)
    except BuildPlanError as exc:
        for err in exc.errors:
            click.echo(f"  - {err}", err=True)
        click.echo(
            f"buildplan validation FAILED: {len(exc.errors)} error(s) in {path}",
            err=True,
        )
        ctx.exit(1)
        return
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"Error reading {path}: {exc}", err=True)
        ctx.exit(1)
        return

    click.echo(f"buildplan OK: {path}")
