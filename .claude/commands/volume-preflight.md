---
description: Verify what earlier volumes actually deliver (code+tests+live) before starting volume $ARGUMENTS; produce GO/NO-GO report
---

Run the preflight phase for volume $ARGUMENTS.

Follow the instructions in `prompts/1-preflight.md` exactly, with N = $ARGUMENTS.
Read `prompts/INVARIANTS.md`, `prompts/DEBT.md`, and `prompts/VERIFY-COOKBOOK.md`
before doing anything else. Remember: no dependency counts as verified without
live evidence from quantstack-vm, and the phase ends with a written GO/NO-GO
report in docs/volumes/ plus a summary to the user.
