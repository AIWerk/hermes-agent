# Retained session-history behavior

This base-owned harness preserves the qualified session-history contract from
product tree `8929d00155c4fcc9fe6723c4f45ee8d3a8dec465`. It copies reviewed test
logic rather than importing candidate tests, fixtures, conftest, pytest config,
or plugins. Every parametrization has an explicit retained ID.

## Contract

The fixed `session-history` launcher profile requires exactly 198 unique passing
nodes, with no skips, failures, errors, missing/replaced nodes or foreign classes:

- 40 current/legacy owner recents → paginated HTTP detail and stored history;
- 40 malformed, incomplete, conflicting and foreign HTTP ownership denials;
- 56 HTTP-selected IDs → authenticated RPC dispatch → actual cold resume and
  stored message/count/role-equivalence assertions;
- 60 existing invalid-owner RPC denials (4007, session not found);
- 2 foreign compression-root/tip lineage denials.

Fixtures are local to this file. Authenticated identities are injected at the
existing HTTP middleware and RPC dispatch seams. Only background agent creation,
session-cap scheduling and auto-continue are disabled. Producer stamping, owner
lookup, authorization, cold restore, history and serialization remain real.
No universal RPC denial policy is asserted: historical admin/untagged/channel
fallbacks outside this qualified matrix remain outside this contract.

## Execution and authority

The shared `scripts/ci/honcho_behavior_gate.py --suite session-history` launcher
selects the fixed harness and exact node inventory from trusted base authority.
It constructs an effective merge tree from exact base/head **commits**, exports
candidate source as data, and runs it only inside restricted Docker. Never pass
a tree object as a hosted CLI commit or install candidate dependencies.

The dependency image reuses the unchanged `.ci/honcho-behavior/Dockerfile` and
trusted frozen dev+honcho lock export. Candidate execution has no network, runs
non-root with a read-only root/source/harness, no capabilities, no-new-privileges,
bounded CPU/memory/PIDs and tmpfs scratch/results. Pytest is imported before
candidate source is exposed; candidate configuration, conftest and automatic
plugins are disabled. No candidate packaging hooks or host imports execute.

The workflow runs unfiltered on main push and the specified pull_request_target
activities, checks out exact base authority with no persisted credentials, binds
exact candidate objects without checkout, and retains only bounded receipt/XML
artifacts. Stable job: **AIWerk session-history retained behavior**. The existing
Honcho workflow and default two-node suite are unchanged.

These files are S scaffolding, not enrollment or active required protection.
The product correction and launcher C transition must precede S; separate E
baseline enrollment, real approval identities, hosted qualification and explicit
app-bound required-check enrollment remain necessary. S cannot govern its own
introduction on a base where its workflow/harness does not yet exist.

Local tree diagnostics may use an evidence-only driver calling the same trusted
archive/sandbox/report primitives. They are not authoritative hosted PR results.
Candidate Python and tests share an interpreter; this is protected retained
regression detection, not proof against arbitrary hostile interpreter tampering.
