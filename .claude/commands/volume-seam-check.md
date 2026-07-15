---
description: Adversarial wiring/freshness/scale audit of the newest volume-$ARGUMENTS work
---

Run the seam-check phase for volume $ARGUMENTS.

Follow `prompts/3-seam-check.md` exactly, with N = $ARGUMENTS. Stance:
assume the newest code is lying about being wired up and try to prove it —
producer→consumer sweep, live production check on quantstack-vm, end-to-end
freshness walk, scale spot-check, and DEBT/INVARIANTS reconciliation.
Blockers stop the build until fixed. Do not invent findings to look thorough.
