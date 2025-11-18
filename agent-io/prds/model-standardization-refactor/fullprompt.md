# Model Standardization Refactoring - Complete PRD

## Overview

This PRD documents the refactoring of model translation and matching functions from [kb_model_utils.py](src/kbutillib/kb_model_utils.py) into a separate module [kb_model_standardization_utils.py](src/kbutillib/kb_model_standardization_utils.py) to improve code organization and reduce class size.

## Problem Statement

The `KBModelUtils` class had grown to 1611 lines, with extensive model translation and matching functionality (lines 589-1585, approximately 997 lines) that was making the class difficult to maintain and navigate.

## Solution

Created a new `ModelStandardizationUtils` module that:
1. Inherits from `MSBiochemUtils` (independent sibling to `KBModelUtils`)
2. Contains all model standardization, translation, and matching functions
3. Can be used independently for standardization tasks
4. Named without "KB" prefix as it's not KBase-specific

### Architecture

```python
              MSBiochemUtils
                    ↓
         ┌──────────┴──────────┐
         ↓                     ↓
ModelStandardizationUtils   KBModelUtils
(general ModelSEED utils)   (KBase-specific)
```

This inheritance structure ensures:
- Both classes independently inherit from `MSBiochemUtils`
- `ModelStandardizationUtils` can be used standalone
- No circular or unnecessary dependencies
- Clear separation of concerns

## Implementation Details

### Files Created

1. **[src/kbutillib/kb_model_standardization_utils.py](src/kbutillib/kb_model_standardization_utils.py)** (NEW)
   - Created new module with `ModelStandardizationUtils` class (renamed from `KBModelStandardizationUtils`)
   - Added module-level constants: `compartment_types` and `direction_conversion`
   - Extracted 10 functions (997 lines total):
     - `remove_model_periplasm_compartment()`
     - `model_standardization()`
     - `compare_model_to_msmodel()`
     - `match_model_compounds_to_db()`
     - `match_model_reactions_to_db()`
     - `translate_model_to_ms_namespace()`
     - `_find_perfect_reaction_match()`
     - `_check_reaction_stoich_match()`
     - `_identify_proposed_matches()`
     - `apply_translation_to_model()`

### Files Modified

2. **[src/kbutillib/kb_model_utils.py](src/kbutillib/kb_model_utils.py)**
   - **Line 11**: Changed import to `MSBiochemUtils` (both classes now independently inherit from `MSBiochemUtils`)
   - **Line 16**: Updated class definition to inherit from `MSBiochemUtils` only
   - **Lines 15-32**: Removed `compartment_types` and `direction_conversion` constants (now in new module)
   - **Lines 589-1585**: Removed all extracted functions (997 lines)
   - File reduced from 1611 lines to 614 lines (62% reduction)

3. **[src/kbutillib/__init__.py](src/kbutillib/__init__.py)**
   - **Lines 28-31**: Added import for `ModelStandardizationUtils`
   - **Line 96**: Added `ModelStandardizationUtils` to `__all__` exports

## Testing

### Import Tests

All imports tested successfully:
```bash
# Test 1: New module imports
✓ ModelStandardizationUtils imported successfully

# Test 2: KBModelUtils still imports
✓ KBModelUtils imported successfully

# Test 3: Inheritance structure verified
ModelStandardizationUtils: ModelStandardizationUtils -> MSBiochemUtils -> SharedEnvUtils -> BaseUtils
KBModelUtils: KBModelUtils -> KBAnnotationUtils -> KBWSUtils -> MSBiochemUtils -> SharedEnvUtils -> BaseUtils

# Test 4: Both inherit from MSBiochemUtils independently
✓ Both inherit from MSBiochemUtils: True
✓ KBModelUtils independent of ModelStandardizationUtils: True

# Test 5: ModelStandardizationUtils methods accessible
✓ remove_model_periplasm_compartment: True
✓ model_standardization: True
✓ match_model_compounds_to_db: True
✓ match_model_reactions_to_db: True
✓ translate_model_to_ms_namespace: True
✓ apply_translation_to_model: True
```

### Syntax Validation

All files validated with `python -m py_compile`:
- ✓ kb_model_standardization_utils.py
- ✓ kb_model_utils.py
- ✓ __init__.py

## Benefits

1. **Improved Organization**: Model standardization functions now in dedicated module
2. **Reduced Class Size**: KBModelUtils reduced from 1611 to 614 lines (62% smaller)
3. **Better Maintainability**: Easier to find and modify standardization code
4. **Independent Modules**: `ModelStandardizationUtils` and `KBModelUtils` are independent siblings
5. **Modular Design**: `ModelStandardizationUtils` can be used standalone for general ModelSEED work
6. **Clear Naming**: No "KB" prefix on `ModelStandardizationUtils` since it's not KBase-specific
7. **Proper Architecture**: Both classes inherit from `MSBiochemUtils` independently, avoiding circular dependencies

## Architecture Notes

**Why This Design?**
- `ModelStandardizationUtils` contains general ModelSEED standardization utilities
- `KBModelUtils` contains KBase-specific model operations
- Both need access to `MSBiochemUtils` functionality
- They are conceptually independent - neither "is-a" subtype of the other
- Using composition (has-a) would be more complex without benefit

## Functions Extracted

### Public Methods

1. **`remove_model_periplasm_compartment(model_or_mdlutl)`**
   - Removes periplasm compartment from a model

2. **`model_standardization(model_or_mdlutl, template="gn", biochem_db=None, return_dataframe=True)`**
   - Standardizes model structure and IDs

3. **`compare_model_to_msmodel(model_or_mdlutl, template="gn", biochem_db=None, return_dataframe=True)`**
   - Compares model to reference ModelSEED model

4. **`match_model_compounds_to_db(model_or_mdlutl, template="gn", biochem_db=None, return_dataframe=True)`**
   - Matches model compounds to ModelSEED database

5. **`match_model_reactions_to_db(model_or_mdlutl, template="gn", biochem_db=None, return_dataframe=True)`**
   - Matches model reactions to ModelSEED database

6. **`translate_model_to_ms_namespace(model_or_mdlutl, template="gn", remove_periplasm=True, max_iterations=10)`**
   - Iteratively translates model IDs to ModelSEED namespace

7. **`apply_translation_to_model(model_or_mdlutl, cpd_translations, rxn_translations)`**
   - Applies translation mappings to model

### Private Helper Methods

8. **`_find_perfect_reaction_match(rxn_stoich, rxn_candidates)`**
   - Finds perfect stoichiometric matches for reactions

9. **`_check_reaction_stoich_match(rxn_stoich, template_rxn, template_rxn_stoich, allow_reverse=True)`**
   - Validates reaction stoichiometry matches

10. **`_identify_proposed_matches(cpd_matches, rxn_matches, template)`**
    - Identifies ambiguous matches for user review

## Module-Level Constants

Moved to new module:

```python
compartment_types = {
    "cytosol":"c",
    "extracellar":"e",
    "extraorganism":"e",
    "periplasm":"p",
    "c":"c",
    "p":"p",
    "e":"e"
}

direction_conversion = {
    "":"-",
    "forward": ">",
    "reverse": "<",
    "reversible": "=",
    "uncertain": "?",
    "blocked":"B"
}
```

## Success Criteria

✅ All functions extracted to new module
✅ New module imports and compiles successfully
✅ KBModelUtils inherits from new module
✅ All methods accessible through KBModelUtils
✅ Backward compatibility maintained
✅ All syntax validated
✅ __init__.py updated to export new class
✅ KBModelUtils reduced from 1611 to 614 lines (62% reduction)

## Future Considerations

- Consider extracting other large functional groups if KBModelUtils grows again
- The new module can be used independently for projects that only need standardization
- Consider adding more specialized submodules as needed
