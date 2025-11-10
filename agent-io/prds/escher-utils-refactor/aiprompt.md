# Escher Utils Refactoring - Enhanced PRD

## Problem Statement

The current escher_utils.py module has issues with displaying reverse direction reactions correctly on Escher maps. The module is overly complex with too many utility functions and sub-functions, making it difficult to maintain and debug.

## Core Requirements

### 1. In-Memory Map Modification for Flux Direction
**Primary Goal**: Adapt the map to fit the flux solution instead of trying to force the flux solution to fit the map.

**Implementation**:
- When create_map_html is called with a flux_solution, examine all reactions in the map
- For reactions with negative flux:
  - Multiply all metabolite stoichiometric coefficients by -1 (reverse the reaction)
  - Convert the flux value from negative to positive
- This transformation must happen in memory - never modify the original map file
- The modified map is then passed to Escher Builder for rendering

### 2. Simplified Architecture
**Goals**:
- Reduce the number of utility functions
- Consolidate related functionality
- Remove unnecessary complexity
- Focus on core functionality that works

**Keep**:
- Proteome/metabolome display functionality (simplified)
- Flux thresholding to eliminate small fluxes
- Custom coloring of fluxes
- Integration with Escher's native functionality (e.g., adding reactions)

**Remove/Simplify**:
- Complex directional visualization logic that interferes with core Escher behavior
- Redundant helper functions
- Over-engineered arrow enhancement features

### 3. Optional Model Synchronization
**Feature**: Optional parameter to also modify the model's reaction directionalities to match the map.

**Rationale**: If we reverse reactions in the map but not in the model, there will be a mismatch between model and visualization, potentially causing issues when using Escher's reaction addition features.

**Implementation**:
- Add `sync_model_directions` parameter (default: False)
- When True, create a modified copy of the model with reversed reactions matching the map
- Return both the HTML output and the modified model (if synced)

## Technical Design

### Key Functions to Implement

1. **_reverse_reaction_in_map(reaction_data, reaction_id)**
   - Takes map reaction data and reverses the stoichiometry
   - Returns modified reaction data

2. **_adapt_map_to_flux(map_data, flux_solution, threshold)**
   - Takes map JSON and flux solution
   - Identifies reactions with negative flux above threshold
   - Creates deep copy of map_data
   - Reverses reactions in the copy
   - Returns modified map with adjusted fluxes

3. **_sync_model_directions(model, flux_solution, threshold)**
   - Takes COBRApy model and flux solution
   - Creates copy of model
   - Reverses reactions in model that have negative flux
   - Returns modified model

4. **create_map_html (simplified)**
   - Main entry point
   - Handle input validation
   - Call _adapt_map_to_flux if flux_solution provided
   - Optionally call _sync_model_directions
   - Set up Escher Builder with simplified parameters
   - Apply proteome/metabolome/gene data
   - Generate output

### Parameters to Keep
- model: COBRApy model
- flux_solution: Flux data
- map_json: Map data or path
- output_file: Output path
- metabolomic_data: Metabolite concentrations
- transcriptomic_data: Gene expression
- proteomic_data: Protein abundance
- title: Page title
- map_name: Map name
- flux_threshold: Minimum flux to display
- reaction_scale: Custom reaction coloring
- metabolite_scale: Custom metabolite coloring
- gene_scale: Custom gene coloring
- height, width: Dimensions
- sync_model_directions: Whether to sync model (NEW)

### Parameters to Remove
- enhanced_arrows
- arrow_width_scale
- arrow_color_scheme
- arrow_directionality
- output_format (keep 'html' only for now)

## Success Criteria

1. Maps correctly display reactions with negative flux (reversed direction)
2. Code is significantly simpler with fewer functions
3. Proteome/metabolome display still works
4. Flux thresholding works
5. Custom coloring works
6. Escher's native features (like add reaction) still work
7. Optional model synchronization maintains consistency between model and map
