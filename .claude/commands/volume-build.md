---
description: Build volume $ARGUMENTS chunk-by-chunk under the contract/compat/scale/live-verify template
---

Run the build phase for volume $ARGUMENTS.

Follow the template in `prompts/2-build.md` exactly, with N = $ARGUMENTS.
First confirm a GO preflight report for this volume exists in `docs/volumes/`
— if not, stop and tell the user to run /volume-preflight $ARGUMENTS first.
Every chunk must satisfy all five sections (contract, backward compat,
implementation, scale-realistic tests, live verification) before its commit.
After every 2-3 chunks, or after any chunk adding a new producer, run the
seam check (`prompts/3-seam-check.md`) before continuing.
