---
description: Cumulative acceptance for volumes 1..$ARGUMENTS — live behavior tests, honest completion verdict, status update
---

Run the postflight phase for volume $ARGUMENTS.

Follow `prompts/4-postflight.md` exactly, with N = $ARGUMENTS. This phase
decides completion for the cumulative system (volumes 1..N together, live on
quantstack-vm, at production scale): spec-coverage table, end-to-end behavior
tests against real market history, full regression + performance check,
invariant/debt reconciliation, and a written verdict report in docs/volumes/.
Only a passing postflight may update roadmap/status claims. An honest
NOT COMPLETE is a valid outcome.
