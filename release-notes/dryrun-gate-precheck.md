# The dry-run now refuses the calls it can recognise as risky

systemu validates a freshly-forged tool by **running its real body** — unattended, right
after the model writes it, and again from several background jobs. That path never
consulted the action gate, so it executed whatever it was given.

This adds a pre-check that mirrors the gate and **skips** instead of executing. It is
deliberately a skip rather than a prompt: the gate signals "ask the operator" by raising,
and this path would have swallowed that into a crash, marked the tool failed, disabled
it, and kicked off a re-forge — while the prompt itself went nowhere, because there is no
task for it to attach to.

## What it closes

- A tool declaring a **destructive local effect** no longer has its body run during an
  unattended dry-run.
- A tool whose **arguments** look destructive is caught even when it declares a
  `dry_run` parameter, which previously routed around the older guard.
- A tool that **declares one thing and imports another** — claiming `local_read` while
  importing a network client — is now caught by re-reading the source, because a
  declaration is exactly the thing a mis-authored tool gets wrong.

## What it does NOT close — read this before relying on it

A freshly forged tool has **no declared effects yet**, so for those the pre-check falls
back to reading the source. That scanner matches on **names**: it recognises a shell or
network call only when the module is spelled out literally at the call site. Ordinary
Python defeats it — not just adversarial code:

```python
from subprocess import check_output   # scans as "purely local"
import subprocess as sp               # scans as "purely local"
from os import system                 # scans as "purely local"
```

Such a body still **executes unattended**. This is not a regression — it did before this
change too — but the change does not fix it, and earlier drafts of these notes and
docstrings wrongly implied it did. That over-claim was the most dangerous part of this
work: the next person would have stopped looking.

Nor does the tmp-path redirection contain anything. It rewrites path-like *arguments*; a
body that builds its own absolute path writes wherever it likes.

The honest summary: **the dry-run now refuses what it can recognise, and recognition is
advisory.** Real containment is the sandboxing work, not this.

Nine strict expected-failure tests pin each known gap. They are strict on purpose — when
the classifier is fixed they will start passing, which fails the suite and forces these
notes and those docstrings to be corrected rather than quietly rotting.

## Found along the way

Two things this work uncovered rather than caused, both being tracked:

- A tool correctly tagged as **executing shell commands** scores "allow" at the live gate
  and runs with no prompt — that tag is treated as an ordinary local effect. The built-in
  shell tools are covered by a separate command-level gate; a forged one would not be.
- The claim that "the live gate will card it later anyway" is **conditional**: the
  effect-tag backfill prefers a tool's own declaration, so a tool that declares itself
  harmless is never carded.
