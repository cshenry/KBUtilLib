<!-- AUTO-GENERATED — do not edit manually.
     Regenerate with: python -m kbutillib.interfaces.docs.mcp_catalog -->

# MCP Tool Catalog

**8** tool(s) exposed over the MCP transport.

## Tools

| Tool | Description | Status |
|------|-------------|--------|
| [`biochem.get_compound_by_id`](#biochem-get-compound-by-id) | Retrieve a ModelSEED compound object by its ID (e.g. 'cpd00001'). | ⚠️ |
| [`biochem.get_reaction_by_id`](#biochem-get-reaction-by-id) | Retrieve a ModelSEED reaction object by its ID (e.g. 'rxn00001'). | ⚠️ |
| [`biochem.search_compounds`](#biochem-search-compounds) | Search ModelSEED biochemistry compounds by identifier, structure, or formula. | ⚠️ |
| [`cheminformatics.backend_status`](#cheminformatics-backend-status) | Return availability + capabilities for every backend. | ✅ |
| [`cheminformatics.expand`](#cheminformatics-expand) | Expand a seed compound set into a predicted reaction network. | ✅ |
| [`thermo.backend_status`](#thermo-backend-status) | Return availability + capabilities for every backend. | ✅ |
| [`thermo.compound_dgf`](#thermo-compound-dgf) | Estimate a compound's standard formation energy / microspecies. | ✅ |
| [`thermo.reaction_dg_prime`](#thermo-reaction-dg-prime) | Estimate a reaction's standard transformed Gibbs free energy. | ✅ |

## Tool Reference

### `biochem.get_compound_by_id` {#biochem-get-compound-by-id}

Retrieve a ModelSEED compound object by its ID (e.g. 'cpd00001').

**Input parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `compound_id` | `string` | ✅ | ModelSEED compound identifier (e.g. 'cpd00001' for water, 'cpd00072' for glucose-6-phosphate). |

<details><summary>Raw JSON Schema</summary>

```json
{
  "description": "Input parameters for ``MSBiochemUtilsImpl.get_compound_by_id``.\n\nMirrors the method signature::\n\n    get_compound_by_id(compound_id: str) -> Any | None",
  "properties": {
    "compound_id": {
      "description": "ModelSEED compound identifier (e.g. 'cpd00001' for water, 'cpd00072' for glucose-6-phosphate).",
      "title": "Compound Id",
      "type": "string"
    }
  },
  "required": [
    "compound_id"
  ],
  "title": "GetCompoundByIdInput",
  "type": "object"
}
```

</details>

---

### `biochem.get_reaction_by_id` {#biochem-get-reaction-by-id}

Retrieve a ModelSEED reaction object by its ID (e.g. 'rxn00001').

**Input parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reaction_id` | `string` | ✅ | ModelSEED reaction identifier (e.g. 'rxn00001' for phosphoglucose isomerase). |

<details><summary>Raw JSON Schema</summary>

```json
{
  "description": "Input parameters for ``MSBiochemUtilsImpl.get_reaction_by_id``.\n\nMirrors the method signature::\n\n    get_reaction_by_id(reaction_id: str) -> Any | None",
  "properties": {
    "reaction_id": {
      "description": "ModelSEED reaction identifier (e.g. 'rxn00001' for phosphoglucose isomerase).",
      "title": "Reaction Id",
      "type": "string"
    }
  },
  "required": [
    "reaction_id"
  ],
  "title": "GetReactionByIdInput",
  "type": "object"
}
```

</details>

---

### `biochem.search_compounds` {#biochem-search-compounds}

Search ModelSEED biochemistry compounds by identifier, structure, or formula.

**Input parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query_formula` | `any` | — | Molecular formula string (e.g. 'C6H12O6').  When provided, the search scores hits by element-count agreement. |
| `query_identifiers` | `array` | — | Free-text or structured identifiers to search by.  Accepts ModelSEED IDs (e.g. 'cpd00001'), names ('glucose'), synonyms, or cross-reference aliases (e.g. 'KEGG:C00031'). |
| `query_structures` | `array` | — | Chemical structure strings to search by (InChI, InChIKey, SMILES).  The hash type is auto-detected. |

<details><summary>Raw JSON Schema</summary>

```json
{
  "description": "Input parameters for ``MSBiochemUtilsImpl.search_compounds``.\n\nMirrors the method signature::\n\n    search_compounds(\n        query_identifiers: list[str] = [],\n        query_structures: list[str] = [],\n        query_formula: str | None = None,\n    ) -> dict[str, dict]",
  "properties": {
    "query_identifiers": {
      "description": "Free-text or structured identifiers to search by.  Accepts ModelSEED IDs (e.g. 'cpd00001'), names ('glucose'), synonyms, or cross-reference aliases (e.g. 'KEGG:C00031').",
      "items": {
        "type": "string"
      },
      "title": "Query Identifiers",
      "type": "array"
    },
    "query_structures": {
      "description": "Chemical structure strings to search by (InChI, InChIKey, SMILES).  The hash type is auto-detected.",
      "items": {
        "type": "string"
      },
      "title": "Query Structures",
      "type": "array"
    },
    "query_formula": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "Molecular formula string (e.g. 'C6H12O6').  When provided, the search scores hits by element-count agreement.",
      "title": "Query Formula"
    }
  },
  "title": "SearchCompoundsInput",
  "type": "object"
}
```

</details>

---

### `cheminformatics.backend_status` {#cheminformatics-backend-status}

Return availability + capabilities for every backend.

Useful for diagnostics and for tests that assert graceful degradation.

Returns:
    ``{name: {"available": bool, "reason": str|None,
    "capabilities": [str, ...]}}``.

**Input parameters:**

*(no parameters)*

<details><summary>Raw JSON Schema</summary>

```json
{
  "type": "object",
  "properties": {}
}
```

</details>

---

### `cheminformatics.expand` {#cheminformatics-expand}

Expand a seed compound set into a predicted reaction network.

Args:
    seed_smiles: Mapping of compound id -> SMILES for the seed set.
    generations: Number of expansion rounds to perform.
    backend: Force a single backend by name, or ``None`` to walk the
        default priority order (``pickaxe -> retrorules``).
    **kwargs: Forwarded to the backend (rule set selection, diameter,
        direction, processes, etc.).

Returns:
    An :class:`ExpansionResult`. If no backend produces a non-empty
    expansion, the result is empty with warnings listing what was tried.

**Input parameters:**

*(no parameters)*

<details><summary>Raw JSON Schema</summary>

```json
{
  "type": "object",
  "properties": {}
}
```

</details>

---

### `thermo.backend_status` {#thermo-backend-status}

Return availability + capabilities for every backend.

Useful for diagnostics and for tests that assert graceful degradation.

Returns:
    ``{name: {"available": bool, "reason": str|None,
    "capabilities": [str, ...]}}``.

**Input parameters:**

*(no parameters)*

<details><summary>Raw JSON Schema</summary>

```json
{
  "type": "object",
  "properties": {}
}
```

</details>

---

### `thermo.compound_dgf` {#thermo-compound-dgf}

Estimate a compound's standard formation energy / microspecies.

Args:
    compound_id: Compound identifier (namespace is backend-specific).
    backend: Force a single backend, or ``None`` for priority order.
    ph, ionic_strength, temperature: Conditions.
    **kwargs: Forwarded to the backend.

Returns:
    A :class:`CompoundThermoEstimate`. ``dgf`` is ``None`` (with
    warnings) if no backend produced a value.

**Input parameters:**

*(no parameters)*

<details><summary>Raw JSON Schema</summary>

```json
{
  "type": "object",
  "properties": {}
}
```

</details>

---

### `thermo.reaction_dg_prime` {#thermo-reaction-dg-prime}

Estimate a reaction's standard transformed Gibbs free energy.

Args:
    reaction_id: Reaction identifier. For the ModelSEED backend this is
        a ``rxnNNNNN`` accession; for equilibrator the ``stoichiometry``
        is used (namespaced compound ids) and this only labels the
        result.
    stoichiometry: Mapping of compound id -> signed coefficient. May be
        ``None`` when targeting the ModelSEED backend (which resolves the
        reaction by accession).
    backend: Force a single backend by name, or ``None`` to walk the
        default priority order.
    ph, ionic_strength, temperature, p_mg: Physiological conditions.
    **kwargs: Forwarded to the backend.

Returns:
    A :class:`ReactionThermoEstimate`. If no backend produces a value,
    ``dg_prime`` is ``None`` with warnings listing what was tried.

**Input parameters:**

*(no parameters)*

<details><summary>Raw JSON Schema</summary>

```json
{
  "type": "object",
  "properties": {}
}
```

</details>

---
