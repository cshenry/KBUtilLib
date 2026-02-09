# AI Curation Backends

The `AICurationUtils` module now supports two AI backends for running curation queries:

1. **Argo** (default) - Remote API service
2. **Claude Code** - Local CLI execution

## Configuration

Add to your `config.yaml`:

```yaml
ai_curation:
  backend: "argo"  # or "claude-code"
  claude_code_executable: "claude-code"  # Path to executable if not in PATH
```

## Usage

### Using Default Backend (from config)

```python
from kbutillib import AICurationUtils

# Uses backend from config.yaml (defaults to 'argo')
util = AICurationUtils()
```

### Specifying Backend at Runtime

```python
# Use Argo backend
util = AICurationUtils(backend="argo")

# Use Claude Code backend
util = AICurationUtils(backend="claude-code")
```

### Backend-Agnostic Analysis

All analysis methods work transparently with either backend:

```python
from kbutillib import AICurationUtils
import cobra

# Load a metabolic model
model = cobra.io.read_sbml_model("path/to/model.xml")

# Initialize with desired backend
util = AICurationUtils(backend="claude-code")  # or "argo"

# Analyze reactions - same code works with both backends
for rxn in model.reactions[:5]:
    result = util.analyze_reaction_directionality(rxn)
    print(f"{rxn.id}: {result['directionality']}")
```

## How It Works

### Argo Backend

The Argo backend sends requests to a remote API service:
- Uses the parent `ArgoUtils.chat()` method
- Requires network access to Argo API
- Supports multiple AI models (GPT, O-series, etc.)
- May require API key and proxy configuration

### Claude Code Backend

The Claude Code backend runs locally via the Claude Code CLI:

1. Creates a temporary directory for each query
2. Writes input data to `input.json`
3. Constructs a prompt with system message and instructions
4. Calls `claude-code -p "<prompt>" --read input.json --write output.json`
5. Reads the response from `output.json`
6. Returns the JSON response as a string

**Claude Code Workflow:**
```
User calls → chat() → _chat_via_claude_code()
                              ↓
                    Create temp directory
                              ↓
                    Write input.json (data)
                              ↓
                    Build prompt with:
                    - System message
                    - Instructions to read input.json
                    - Instructions to write output.json
                              ↓
                    Execute: claude-code -p "<prompt>"
                              ↓
                    Read output.json
                              ↓
                    Return JSON response
```

## Methods Using AI Backend

All of these methods use the configured backend transparently:

- `analyze_reaction_directionality(rxn)` - Evaluate reaction directionality
- `analyze_reaction_stoichiometry(rxn)` - Categorize stoichiometry components
- `evaluate_reaction_equivalence(rxn1, rxn2, evidence)` - Compare reactions
- `evaluate_reaction_gene_association(rxn, genedata)` - Validate gene associations

## Requirements

### Argo Backend
- Network access to Argo API
- Optional: API key (set via `ARGO_API_KEY` or in headers)
- Optional: SOCKS proxy (if needed - install with `pip install httpx[socks]`)

### Claude Code Backend
- Claude Code CLI installed and available in PATH
- Or full path specified in config: `ai_curation.claude_code_executable`

## Error Handling

The module includes proper error handling:

- **Missing Claude Code executable**: Raises `FileNotFoundError` with helpful message
- **Claude Code timeout**: 5 minute timeout with error logging
- **Missing output file**: Detects and reports if Claude Code doesn't create output
- **Invalid backend**: Raises `ValueError` if backend is not 'argo' or 'claude-code'

## Caching

Caching works the same way regardless of backend:
- Responses are cached by reaction ID
- Cache is stored in `~/.kbutillib/` directory
- Cache files: `AICurationCache<CacheName>.json`

## Example: Switching Backends

```python
from kbutillib import AICurationUtils

# Test with Argo
util_argo = AICurationUtils(backend="argo")
result1 = util_argo.chat("What is ATP?", system="You are a biochemist.")

# Test with Claude Code
util_claude = AICurationUtils(backend="claude-code")
result2 = util_claude.chat("What is ATP?", system="You are a biochemist.")

# Both return equivalent results, just processed differently
```

## Benefits of Multiple Backends

1. **Flexibility**: Choose between remote API or local execution
2. **Cost Management**: Claude Code runs locally (no API costs)
3. **Privacy**: Local execution keeps data on your machine
4. **Availability**: If Argo is down, use Claude Code (or vice versa)
5. **Testing**: Easy to compare results between backends

## Implementation Details

The `chat()` method is overridden in `AICurationUtils` to route requests:

```python
def chat(self, prompt: str, *, system: str = "") -> str:
    if self.ai_backend == "claude-code":
        return self._chat_via_claude_code(prompt, system)
    elif self.ai_backend == "argo":
        return super().chat(prompt, system=system)
    else:
        raise ValueError(f"Unknown AI backend: {self.ai_backend}")
```

All higher-level methods (`analyze_reaction_directionality`, etc.) call `self.chat()`, so they automatically use the configured backend without any code changes.
