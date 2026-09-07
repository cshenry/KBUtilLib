# KBUtilLib verAB O-Demethylase (kbu verab)

You have a `kbu` CLI on PATH. Its `kbu verab` verb group gives you
verAB O-demethylase (EC 1.14.13.82) reaction-discovery, enumeration,
screening, and KIND artifact emission over KBUtilLib's `VerabUtils` API.

## The canonical arc

```
discover → enumerate → screen → emit-kind
```

Run stages in that order. Each verb accepts `--json` for stable,
parseable output.

## Verbs

```
kbu verab discover   --generations N [--seeds SMILES,...] [--backend pickaxe|retrorules] [--json]
kbu verab enumerate  --result RESULT.json [--json]
kbu verab screen     --result RESULT.json [--threshold F] [--json]
kbu verab emit-kind  --result RESULT.json --out DIR [--json]
```

## Scientific context

verAB O-demethylase (EC 1.14.13.82) catalyses the O-demethylation of
aromatic methoxy compounds.  Canonical seed compounds: vanillate,
guaiacol, veratrate.

The `discover` verb expands from seed compounds using cheminformatics
network-expansion (Pickaxe or RetroRules backends) for the specified
number of generations.  Operators are drawn from the mechinformed rule
set when available, with automatic fallback to the metacyc_intermediate
rule set when the mechinformed TSV is not found.

The `screen` verb filters expansion products against the verAB activity
model, ranking hits by predicted demethylation likelihood.

The `emit-kind` verb writes a KING-ready artifact bundle (seeds.tsv,
seeds.csv, discovered_rules.tsv, target_transformation.txt, prompt.md,
manifest.json) into the specified output directory.
