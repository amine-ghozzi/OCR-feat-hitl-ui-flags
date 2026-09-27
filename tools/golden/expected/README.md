# `expected/` — one whitelist per work package

A work package declares, **before** it is merged, which snapshot fields it will move and
on which documents. `golden_sweep.py compare` then judges every field-level diff against
that declaration: exit 0 means the change did only what it said it would; exit 1 means it
did something else. That is the whole value of the gate — attribution, not accuracy.

Name the file after the package: `2.1.yaml`, `2.4.yaml`, …

```yaml
change: WP-2.1                 # free text; echoed at the top of the compare output
tolerance:                     # a numeric field may drift this far without being a diff
  qcs: 0.005
docs:
  "*":                         # defaults for every document
    seconds: any
  fis_innov_invest_efd311223.pdf:   # per-document rules override "*" FIELD BY FIELD
    text_chars: {from: 29, min: 20000}
    table_count: {from: 0, to: 16}
    qcs: {min: 0.5}
    statements: any
    tier: any
```

## Rule forms

| Rule | Allows the diff when |
|---|---|
| `any` | anything |
| `added` | the field was empty/absent in the baseline and has a value now |
| `removed` | the reverse |
| `{to: X}` | the candidate value is exactly `X` |
| `{from: A, to: B}` | the baseline was `A` **and** the candidate is `B` |
| `{min: X}` | the candidate is numeric and `>= X` |
| `{max: X}` | the candidate is numeric and `<= X` |
| `{up: true}` | the value did not decrease (`{up: false}` — did not increase) |
| `{tol: X}` | the two differ by at most `X` |

Keys **combine**: `{from: 29, min: 20000}` requires both. A field with no rule and a
changed value is an unexpected diff.

## A word on `text_sha`

`snapshot` pins `PYTHONHASHSEED` (it re-executes itself with `0` when you have not set
it). Measured on this corpus: without that, two runs of *identical code* over the six
DOCX produce the same `text_chars` and the same `qcs` but a different `text_sha` —
something in the text post-processor iterates a `set`/`dict` and rides Python's
randomised string hash. With the seed pinned, two separate processes give byte-identical
records. Every snapshot carries the seed it ran under in `env.python_hash_seed`; compare
a baseline only against a candidate taken under the same one.

## Two things worth knowing

`seconds` and `env` are excluded from the compare unless you pass `--include-timing`, so
they need no rule. And rules are matched on the record's **top-level field name only** —
`statements`, `checks`, `table_shapes` and `ensemble` compare as whole values, so a rule
on one of them is necessarily `any` or a `{to: …}` of the entire structure. When a change
touches a statement's internals, whitelist `statements: any` and say in the package what
you expect to see; the reviewer reads the diff.
