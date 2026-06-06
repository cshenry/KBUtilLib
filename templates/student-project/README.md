# {{project_name}}

## Quick start

1. Activate the project environment: `source activate.sh`
2. Run `claude` in the project root
3. Type `/kbu-start` and follow the menu

## Subproject layout

Each subproject lives under `subprojects/<name>/` with this structure:

```
subprojects/<name>/
├── notebooks/          # Analysis notebooks (01_*.ipynb, util.py)
│   └── nboutput/       # Notebook execution outputs
├── data/               # Input data (not committed by default)
│   └── user_data/      # User-supplied data files
├── figures/            # Generated figures
├── references.md       # Literature references (appended by /kbu-literature-review)
├── sessions/           # Local session YAML files (not committed by default)
└── kbu-subproject.toml # Subproject manifest (state machine + artifact tracking)
```

## State machine

Subprojects advance through 8 linear states — plan, p-review, build, b-review, run, synthesize, s-review, complete — with review steps that can route back on failure.

## Updating

To pull skill and template updates from the parent KBUtilLib install, run `/kbu-start` and select Update.
