# jev0 Validated Development Roadmap

**Validated:** 2026-10-02  
**Baseline:** `feat/versioned-policy-manifest` @ `e01d1c98aa94607d240663de5cfd242bd3f9ef0a`  
**Scope:** roadmap only. No merge, release, publish, deployment, or authority expansion is implied by this document.

## 1. Verified baseline

The current development baseline is materially ahead of `main`:

- PR #7 is open and mergeable.
- The branch is 31 commits ahead of `main` and 0 behind.
- GitHub Actions run #134 passed:
  - Ubuntu / Python 3.9
  - Ubuntu / Python 3.13
  - macOS / Python 3.9
  - macOS / Python 3.13
  - aggregate `jev0 gate`
- Inspected Ubuntu and macOS logs report **160 tests passed**.
- The baseline already includes:
  - versioned Layer 0 policy manifests
  - policy fingerprints and drift detection
  - server-side merge-base range guards
  - bounded Git/process reads with deadlines
  - canonical evidence JSON + deterministic SHA-256
  - evidence chain verification and boundary pinning
  - CI provenance envelopes
  - retained evidence artifacts
  - isolated Sigstore/GitHub artifact attestation

### Important claim boundaries

- `jev0` does **not** currently promise a 15 ms SLA.
- The documented local baseline is 54.43 ms median / 64.45 ms p95 for a fresh CLI process on one macOS benchmark.
- The external process evaluator and bounded stream readers are currently POSIX-only.
- Windows execution support is not yet implemented or CI-proven.
- A local Git hook is not an adversarial trust boundary. A local actor with write access can alter or remove it.
- `jev0` remains a development / Evolution Lab guard, not a fourth production authority core.

## 2. Architectural law

Keep the hot path deterministic, bounded, local, and dependency-free.

```text
Universal Capability Intelligence != authority
Extropy Kernel                != independent proof
ProofOS Truth Plane           != executor
jev0                           != production authority

Discovery != authorization
Execution != verification
Model confidence != evidence
Local hook != external trust root
```

Cross-project integrations must therefore preserve these boundaries:

- **jev0**: deterministic mutation/runtime guard and evidence producer.
- **entropy-loop-core**: deterministic failure-to-regression compiler.
- **Universal-Skill-Router**: capability/package identity, signed authority requirements, execution provenance, transparency.
- **Extropy**: production work authority, effective grants, execution routing, budgets, approvals, STOP.
- **ProofOS**: independent evidence verification where consequence-aware policy requires it.

---

# Priority roadmap

## P0 — Consolidate the verified trust kernel

**Goal:** make the cryptographically verifiable policy/evidence baseline the stable prerequisite for every later integration.

### Work

1. Resolve repository-governance blockers around PR #7 without weakening the intended trust boundary.
2. Preserve:
   - trusted base-policy evaluation
   - bounded resource semantics
   - evidence/provenance verification
   - Sigstore attestation isolation
3. Keep all new integration work stacked on or rebased onto the verified baseline until it is deliberately promoted.

### Exit criteria

- Exact candidate SHA has green CI.
- Evidence/provenance/attestation regression tests remain green.
- No new unbounded reads, shell expansion, network dependency, or authority surface is introduced.

---

## P1 — jev0 × entropy-loop-core: Failure Auto-Capture Pipeline

**Value rank:** 1  
**Goal:** turn blocked agent behavior into deterministic, replayable reliability assets without coupling `jev0` to another Python package.

### Design principle

Do **not** import `entropy-loop-core` from `jev0`.

Instead:

```text
agent mutation / command
        |
        v
jev0 deterministic guard
        |
        +-- PASS --> continue
        |
        +-- BLOCK --> jev0-failure/v1
                          |
                          v
                 local content-addressed spool
                          |
                          v
              entropy-loop-core adapter
                          |
                          v
               FailureTrace / RegressionCase
```

### New neutral contract: `jev0-failure/v1`

Recommended bounded fields:

```json
{
  "schema_version": 1,
  "kind": "jev0-failure",
  "failure_id": "<sha256>",
  "command": "workspace|staged|range|run",
  "violation_code": "MAX_LINES_EXCEEDED",
  "reason": "...",
  "repository_fingerprint": "...",
  "base_sha": null,
  "head_sha": null,
  "policy_sha256": null,
  "evidence_sha256": null,
  "changed_paths": [],
  "numstat": [],
  "diff_sha256": null,
  "stderr_prefix": null
}
```

### Privacy and safety invariants

- Raw diff content is **not captured by default**.
- Secrets are never intentionally serialized.
- Diff digest + bounded path/numstat evidence is the default.
- If raw diff capture is ever added, it must be explicit opt-in, byte-bounded, documented, and separately tested.
- No network call occurs in the blocking path.
- Failure storage is local-only by default.
- Prefer a Git-private location such as `.git/jev0/failures/` to avoid accidental commits.
- Use content-addressed filenames and atomic create semantics to make duplicate failures idempotent.

### entropy-loop-core adapter

Add an importer there, rather than a dependency here:

```text
jev0-failure/v1
  -> validate strict schema
  -> map violation_code to VerificationResult / FailureCategory
  -> create FailureTrace
  -> generate_regression_case()
  -> append/import RegressionSuite
```

### Suggested CLI surface

```sh
jev0 workspace --capture-failure
jev0 staged --capture-failure
jev0 range BASE HEAD --capture-failure
jev0 run --capture-failure --timeout 30 -- <command>

# Read-only support surface
jev0 failures list
jev0 failures show <failure-id>
```

### Verification targets

- schema drift rejection
- bounded field sizes
- no raw diff by default
- no secret-path leakage
- duplicate/idempotent capture
- interrupted write atomicity
- failure capture never converts a block into a pass
- entropy adapter round-trip produces stable RegressionCase output
- capture overhead benchmarked separately from guard latency

---

## P2 — Universal-Skill-Router × Extropy × jev0: Signed Least-Authority Execution Boundary

**Value rank:** 2  
**Goal:** connect signed capability requirements to deterministic local mutation enforcement without letting discovery metadata become authority.

Universal already has:

- Signed Skill Package / Package Authority Receipt
- `authority-grant/v1`
- exact / underprovisioned / escalation classification
- execution receipts
- replay guard
- Merkle transparency
- signed transparency checkpoints

Extropy already owns the production authority boundary and consumes Universal capability intelligence as advisory data only.

The missing high-value link is:

```text
Universal signed package + authority requirements
             |
             v
Extropy admitted Work Contract + effective grant
             |
             v
deterministic local execution policy projection
             |
             v
jev0 bounded mutation / command guard
             |
             v
execution evidence
```

### Required boundary

`jev0` must not independently decide that a Universal package is authorized.

It may only enforce a deterministic policy already derived from an admitted authority ceiling.

### Proposed portable projection

Define a small, versioned execution-policy projection that can be generated by an authorized control plane:

```json
{
  "schema_version": 1,
  "max_files": 20,
  "max_lines": 1200,
  "allow": ["src", "tests"],
  "command_timeout_seconds": 120,
  "artifact_deny": ["*.gguf", "*.bin"]
}
```

The existing jev0 policy loader remains the enforcement primitive. Avoid teaching jev0 about remote trust stores, package popularity, or production approval semantics.

### Exit criteria

- signed requirement verification remains in Universal/Extropy
- effective grant cannot exceed admitted Work Contract
- projected jev0 policy cannot widen the grant
- jev0 fails closed on invalid projection
- exact authority identity is carried into evidence as a digest, not as ambient permission

---

## P3 — jev0 × ProofOS: Promotion Evidence Gate, not default pre-commit dependency

**Value rank:** 3  
**Goal:** prevent false completion for high-consequence promotions while preserving ProofOS independence.

The original idea of making every local pre-commit require a ProofOS receipt is rejected for architecture reasons:

1. ProofOS receipts are evidence, not authority.
2. independent verification is consequence-aware, not mandatory for every edit.
3. many ProofOS receipts are produced in CI/deployment workflows after local commit time.
4. mandatory local ProofOS coupling would weaken jev0's offline, dependency-free contract.

### Recommended integration

Use ProofOS at CI / promotion boundaries:

```text
jev0 local/range evidence
       |
       v
tests/build/deploy evidence
       |
       v
ProofOS independent verification
       |
       v
ProofOS receipt / VERIFIED or ABSTAIN
       |
       v
promotion gate controlled by Extropy/operator policy
```

### jev0 role

jev0 may expose or bind deterministic evidence digests that ProofOS independently re-verifies.

It must not mint ProofOS truth or turn a receipt into merge/deploy authority by itself.

### Verification targets

- stale receipt rejection
- Git SHA / run identity binding
- evidence digest mismatch rejection
- receipt re-derivation
- missing required proof => fail closed at the configured promotion boundary

---

## P4 — Runtime portability and latency architecture

**Value rank:** foundation  
**Goal:** make the engine portable and create a measured path toward sub-process-spawn latency.

### 4A. Cross-platform process controller

Introduce an internal abstraction:

```python
class ProcessController(Protocol):
    def spawn(...): ...
    def capture_bounded(...): ...
    def terminate_tree(...): ...
    def wait(...): ...
```

Implementations:

```text
PosixProcessController
  - start_new_session
  - selectors
  - killpg

WindowsProcessController
  - CREATE_NEW_PROCESS_GROUP
  - bounded pipe handling
  - Job Object / process-tree termination strategy
```

### CI target

```text
ubuntu-latest  x Python 3.9 / 3.13
macos-latest   x Python 3.9 / 3.13
windows-latest x Python 3.9 / 3.13
```

Do not claim Windows support before this matrix is green.

### 4B. Evaluator transports

Keep the existing trusted in-process API and process evaluator, then add transport options only with measurements:

```text
GuardEvaluator
  +-- InProcessEvaluator
  +-- ProcessEvaluator
  +-- ResidentDaemonEvaluator
  +-- RemoteEvaluator (optional enterprise adapter)
```

A resident daemon is the correct direction if an actual sub-15 ms semantic-evaluator target is later adopted. The target must be benchmarked end-to-end rather than declared.

---

## P5 — Replace "kill-on-watch" with supervised execution

**Value rank:** 4  
**Goal:** stop runaway agent sessions without pretending filesystem polling can reliably attribute writes to a process.

A generic file watcher that sees a changed file cannot safely prove which external process caused the change. Blind `SIGSTOP` / `SIGTERM` therefore risks killing the wrong process.

Prefer:

```sh
jev0 supervise --timeout 600 --workspace-budget ... -- <agent command>
```

Because jev0 owns the child process tree, it can attribute termination correctly.

### Supervision loop

```text
spawn owned agent process group
        |
periodic bounded workspace check
        |
        +-- within budget --> continue
        |
        +-- violation --> terminate owned tree
                         capture jev0-failure/v1
                         return non-zero
```

### Claim boundary

- Polling interval is not a 15 ms guarantee.
- File changes may occur between samples.
- Detached/session-escaped descendants require OS-specific handling or an external sandbox.
- `watch` may exist as read-only observation, but enforcement should prefer supervised ownership.

---

## P6 — Skill packaging / ecosystem distribution

**Value rank:** 5  
**Goal:** distribute jev0's guard contract through agent ecosystems without making agent instructions the security boundary.

Possible adapters:

- Hermes skill
- Universal Skill package
- MCP tool
- Codex/Claude/terminal integration instructions

The skill should expose:

- `jev0 doctor --json`
- `jev0 workspace`
- `jev0 staged`
- policy budget status
- failure capture identifiers

But the skill is advisory orchestration. Enforcement remains in jev0 / CI / the authorized control plane.

---

# 3. Development sequence

```text
P0  verified trust-kernel consolidation
 |
 v
P1  jev0-failure/v1 + local atomic spool
 |
 +--> entropy-loop-core importer + replay regression
 |
 v
P2  signed least-authority policy projection
 |      Universal -> Extropy -> jev0
 |
 v
P3  ProofOS promotion evidence binding
 |
 v
P4  Windows process backend + measured evaluator transports
 |
 v
P5  supervised agent execution
 |
 v
P6  ecosystem skill/package adapters
```

## Why this order

P1 creates unique data value immediately while preserving jev0's zero-dependency design.

P2 connects the already-existing signed authority machinery to actual bounded mutation enforcement.

P3 completes the execution-versus-independent-verification boundary without dragging ProofOS into every local edit.

P4/P5 then improve portability and real-time containment on top of stable contracts instead of building a fast watcher with ambiguous authority semantics.

P6 comes last because distribution multiplies value only after the core contracts are trustworthy.

---

# 4. North-star architecture

```text
Signed capability/package intent
        Universal
            |
            v
Governed authority + effective grant
         Extropy
            |
            v
Deterministic local reflex
           jev0
            |
       +----+----+
       |         |
     PASS       BLOCK
       |         |
       |         v
       |   jev0-failure/v1
       |         |
       |         v
       |  entropy-loop-core
       |  regression compiler
       |
       v
execution evidence
       |
       v
ProofOS independent verification
when consequence policy requires it
       |
       v
verified promotion evidence
```

This preserves the key architectural separation:

> **Intent is not authority. Authority is not execution. Execution is not verification. Failure is not waste.**

The distinctive system-level value is the closed loop:

```text
BOUND -> EXECUTE -> PROVE -> CAPTURE FAILURE -> COMPILE REGRESSION -> BOUND BETTER
```

while keeping the deterministic guard path local, bounded, and independent of LLM inference.
