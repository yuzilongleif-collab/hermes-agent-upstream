# Archive progress: verified existing implementation

This branch is an immutable **artifact carrier**, not an implementation branch, upstream PR, merged change or installed client. It offers the existing author's work plus a bounded verification increment through NousResearch/hermes-agent#148270.

## Provenance and credit

- Tested official base: `NousResearch/hermes-agent@77a30e9127998c0277306c361663f63c0694b88b`.
- Existing production implementation: `kvnloo/hermes-agent#712@3624d085ac9258008f1ebb86fdbc47588293c9b0`, an open draft in that contributor's fork.
- kvnloo retains credit for the progressive loader/Settings implementation and its seven tests. Our increment adds twelve integration cases and native formatting corrections; it is not a third implementation.
- Original Hermes repository code and the contributed derivatives retain their MIT licensing and attribution.

## Choose exactly one patch

1. `candidate-on-main.patch` is the complete five-file tested composition against the official base above. In a disposable checkout at that exact commit, use `git apply --check candidate-on-main.patch` and then `git apply candidate-on-main.patch`.
2. `owner-increment.patch` is only the additional tests/formatting against the exact kvnloo fork head above. Apply it there instead. Do **not** apply this increment after the complete patch.

SHA-256:

- Complete patch: `407cbcb5d0371b28ff1a5e0bb39c49d731c327bcab729c7ad98d05bd895065c9`.
- Owner increment: `3bf9a6428dd6572c275297e228a199e3e127789376453fd6bc9fb12723101545`.

The complete patch was cold-applied on the declared official source tree and all five resulting files matched the tested composition. The owner increment passed applicability checking at its declared fork prerequisite. Carrier commit identity is separate from the tested implementation/source tree and inherits no CI result.

## Executed verification

Node 26.12.1 / pnpm 12.15.0 from the repository PM pin; frozen lockfile installation. The affected/adjacent selection passed **84 tests in 13 files**, including the author's seven and our twelve integration cases. Desktop typecheck, scoped ESLint/Prettier and six selected native structural/code-health checks passed. These are overlapping checks, not additive counts or whole-repository CI.

The new consumer test keeps the real Settings component, archive helper, list API and renderer API wrapper, with a synthetic final Desktop bridge. Mutation/confirmation operations and notification sinks use recording doubles. It checks:

- Five complete 500-row pages remain visible during and after failure of page six; the original error is forwarded and Retry is available.
- Retry restarts from offset zero and recovers precisely 3,000 unique rows.
- Owner-aware deduplication/removal, no resurrection from later snapshots, profile/connection changes, unmount, partial-profile failure and genuine empty results.
- Cancellation's actual limited behavior, described below.

A private headless Chrome also rendered the real component with official CSS and exercised seven scenarios, including a trusted pointer Retry and late replies after scope changes/unmount. No packaged Electron client, real backend, production chat data, actual network timeout or performance certification is claimed.

After applying the complete patch, from `apps/desktop` the focused new/existing verification can be run with the repository-pinned environment:

```sh
pnpm exec vitest run src/store/sidebar-archive-progress.test.ts src/app/settings/archived-progress.test.tsx src/app/settings/archive-integration.test.tsx --maxWorkers=2
pnpm run typecheck
```

## Cancellation is not equivalent

This implementation's local AbortSignal stops stale publication and further pages. It **does not cancel or immediately settle an in-flight REST/Desktop bridge request**. A held bridge promise was explicitly observed remaining unsettled after abort until the synthetic reply arrived.

The original #148270 candidate's stronger cancellation prerequisite remains the separate public stack ending at `yuzilongleif-collab/hermes-agent-upstream@238047491e4246989a8401062a4353fd8a979119`, explained in NousResearch/hermes-agent#134779 (comment 6538718911). That stack was not an ancestor of the tested official main. This artifact does not withdraw, silently weaken or certify that prerequisite. Maintainers can evaluate this narrower UI composition independently or retain the stronger requirement and integrate its transport prerequisite first.
