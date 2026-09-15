# Protected Honcho behavioral gate

The retained test runs root-policy and conflicting host-override-policy cases through real configuration parsing, Honcho formatting, MemoryManager aggregation, the agent turn, and final localhost LLM HTTP bytes. Disabled summary and representations must be absent; enabled user and AI cards must remain. Backend retrieval is a MagicMock: this is not Honcho SDK/service fidelity, dialectic retrieval coverage, or a live-provider test.

## Authority and activation boundary

This is a **local implementation proposal, not active required protection**. After separately authorized enrollment on protected main, pull_request_target loads the workflow, launcher, dependency lock and retained test from the exact base SHA. Candidate objects are fetched and archived as data, never checked out or installed on the authority runner. No changed-path classifier, paths filter, candidate test file, conftest or pytest configuration selects the retained nodes. Deleting or replacing candidate harness/workflow files therefore cannot change this PR run. Ordinary main pushes also execute the gate, without path filters.

The workflow grants only contents:read, persists no checkout credential, references no secrets, uses no environments, and never passes the GitHub token or runner environment into candidate execution. Network-enabled dependency installation uses only trusted-base lock data and a minimal Docker build context. Candidate packaging hooks are never run. The gate does not forward untrusted stdout/XML into GitHub workflow command parsing.

Before publication, enroll all six gate paths plus the trusted dependency owners in the existing protected content-loss control plane using its separate approval/compatibility/baseline stages. The pull_request_target workflow alone is not sufficient to protect a later removal of the base authority after merge. Branch rules must separately require the stable job name **AIWerk Honcho retained behavior**. Neither enrollment nor branch rules are changed by this patch. Existing static Honcho protection remains independent. The bootstrap PR cannot claim to be governed by a harness absent from its base.

## Restricted execution and limits

Candidate source is extracted as regular files/directories only; links, devices, traversal and Git metadata fail closed. A disposable Docker container uses network=none (loopback remains available), read-only root/source/harness, an unprivileged uid, all capabilities dropped, no-new-privileges, default Docker seccomp, bounded memory/PIDs/CPU/time, private tmpfs HOME and no host home, credential store or Docker socket. Only an empty, disposable report directory is writable on the host. Missing, malformed, skipped, duplicate, unexpected or incomplete test results, startup failure and timeout fail the gate. Results are bounded regular files opened without symlink following.

This is normal Docker namespace/seccomp containment, not a VM or a proof against kernel exploits. Candidate Python and the test framework necessarily share an interpreter: arbitrary hostile Python could tamper with in-process assertions/reporting. Exact result validation prevents accidental skip/noncompletion, not such adversarial forgery. The retained harness and selection are base-owned/read-only, but behavioral tests do not prove arbitrary candidate code honest. The real wire sentinels and historical RED qualify regression detection.

Local invocation (launcher and image must be trusted):

    python3 scripts/ci/honcho_behavior_gate.py --repo . --candidate <exact-40-hex-commit> --image <trusted-dependency-image> --evidence <outside-source-directory>

The evidence directory holds a host receipt bound to the candidate commit/tree, harness digest, resolved image ID, container argv and exit code, plus the untrusted XML test report. Do not treat the XML as executable content. Keep historical RED and candidate GREEN receipts with the same retained harness digest.
