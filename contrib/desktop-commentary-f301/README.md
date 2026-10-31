# Current-baseline regression supplement for upstream PR #132913

This is a source-review branch for the existing Oidaladn0 contribution, not a competing PR or a release. The follow-up makes minimal structural extractions to meet the repository's code-health ratchet; it introduces no new deduplication algorithm.

- Fixed official baseline: `f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f`.
- Original six-file contribution: NousResearch/hermes-agent#132913, head `16415106e0a3d9a81e370eaa9d8fdae5dde49cae`.
- `0686fab7ae912c943a925a7f8710f34e3801b73e`: full original-author work integrated on the fixed baseline.
- `46d82aa1312c2a392c4b40262ff5a7a8a3b25268`: three-file test-only extension, dependent on the complete preceding integration.
- The subsequent structural follow-up moves the complete 30-case commentary refresh matrix into `commentary-history-refresh.test.tsx`, shares the existing `ResumeHarness` through `test-harness.tsx`, extracts resume reconciliation and transcript-expectation phases into the existing `resume-reconcile.ts`, and names the painted-row and snapshot-projection phases inside `persisted-live-turn.ts`. Assertions, source-row/tool identity requirements, completion freshness, valid repeated prose and tool results are retained. No ratchet policy or exceptions are changed.

## Consume the final branch

Use this branch's full Git diff from the fixed baseline, including newly added files, or the complete final patch supplied with the review. Do not combine this representation with cherry-picking the same commits. The full branch, not the historical embedded replay payload, is the final source candidate.

With existing compatible workspace dependencies and Node 26+, run from `apps/desktop`:

```sh
node ../../node_modules/vitest/vitest.mjs run --project ui --configLoader runner --no-cache --maxWorkers 1 --no-file-parallelism \
  src/app/session/hooks/use-session-actions/commentary-history-refresh.test.tsx \
  src/app/session/hooks/use-session-actions/commentary-case-repro.test.ts \
  src/app/session/hooks/use-session-actions/cached-commentary-case-repro.test.ts \
  --testNamePattern 'empty snapshot: true|reconcile cached commentary|control:|Codex commentary'
```

Omit the name filter and select only `commentary-history-refresh.test.tsx` for all 30 hook variants. Neighbor selections are `coverage.test.ts`, `live-turn-remainder.test.ts`, `resume-structural-parts.test.ts`, `streamed-reply-survives-sync.test.ts`, and `resume-empty-prose-pairing.test.ts` in their existing modules. The six hydration/cache cases remain in `use-session-actions.test.tsx`; the extracted transcript-expectation predicate is also covered by that file's failure-latch cases. Use the repository's existing renderer TypeScript and ESLint commands for the changed surface.

From the repository root, judge the exact final committed tree with all 14 native checks:

```sh
python scripts/check --base f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f --commit "$(git rev-parse HEAD)"
```

This check is local lint/admission evidence, not proof that GitHub Actions ran or maintainers merged the branch. Dependency versions actually exercised are disclosed below; no exact-lock CI claim is made.

## Historical payload: not the final structural candidate

`replay-payload.zip`, `integrated-original-pr.patch` and `new-regressions.patch` are intentionally retained as the earlier behavior-stage evidence supplied by commit `e18e1a0f75c090aed8e3ffd8bd6114e67bc3f5cd`. Their embedded hashes, source and old hook-test path predate the structural follow-up. They do **not** replay or certify the final branch and must not be presented as its final patch. The two historical patches still describe the original six-file integration plus test-only extension, with MIT license and attribution. To inspect historical red/green behavior, use that frozen payload's own README and dependency disclosure; do not substitute its source identity for the final branch.

## Executed scope and limits

The preserved behavior-stage evidence reports the same 13 focused cases on baseline as 10 assertion failures / 3 passes, and the complete candidate as 13 passes. The prior developer cohort was 71 unique passing cases across overlapping 13 focused, 30 hook, 26 neighbor and 6 hydration selections. Independent QA's separate overlapping cohort was 55 unique cases, including 10 independent controls (baseline 7 failures / 3 passes; candidate 10 passes). These counts must not be added together. Final structural follow-up runs are supplied separately, bound to the final source, rather than relabeling these historical receipts.

Actual reused dependencies: Node 26.12.1, React 19.2.7, Vitest 4.1.10, Vite 8.2.0, TypeScript 6.0.3 and jsdom 29.1.1. Both root and Desktop workspace dependencies are required, including the Vite Babel plugin; setup failures are not behavioral red tests. RPC/REST are test doubles; the real hook, reducers, history projection and tool-result consumers run in source tests. Fixture content is synthetic.

This does not prove an actual Windows incident's unique cause, scrolling geometry, sidebar search performance, Electron packaging, upstream CI, merge/release, or installed-client adoption. Historical 190/1,049-case results belong to another baseline and are not claimed as current execution. No private transcripts, actual hidden reasoning, real user session identifiers, screenshots, account data, credentials or machine paths are included. The original MIT license and author attribution remain intact.
