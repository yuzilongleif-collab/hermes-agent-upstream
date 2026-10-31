# Current-baseline regression supplement for upstream PR #132913

This is an evidence/replay branch for the existing Oidaladn0 contribution, not a competing PR or a release. No new production implementation was written here.

- Fixed official baseline: `f30105a0b4b3d0c1baa6c464189755ccdbc1ac7f`.
- Original six-file contribution: NousResearch/hermes-agent#132913, head `16415106e0a3d9a81e370eaa9d8fdae5dde49cae`.
- `0686fab7ae912c943a925a7f8710f34e3801b73e`: full original-author work integrated on the fixed baseline.
- `46d82aa1312c2a392c4b40262ff5a7a8a3b25268`: three-file test-only extension. It depends on the complete preceding integration, not two selected helpers.

The extension adds four warm/cold hook variants with empty ordinary content, an empty in-flight snapshot, and authorized public commentary in cached/durable projections. It also adds two cross-user-turn controls and reuses seven previously offered synthetic projection/tool-result controls with synthetic two-paragraph text. The tests must retain newer tool results, genuinely repeated public text, and a new suffix, rather than obtaining deduplication by discarding data.

## Consume or replay

For normal code review, inspect the two commits above. To apply the offered patch representation in a separate checkout at the fixed baseline, apply `integrated-original-pr.patch` first and then `new-regressions.patch`. Do not apply both that representation and the same contribution commits.

The supplied `replay-payload.zip` preserves the exact reviewed replay script, both patches, source identity, dependency disclosure and MIT license. Extract it first. The replay creates a temporary export, verifies source/test hashes, runs the same focused selection before and after the production change, and removes its temporary source/state. It reads an existing Git object store and compatible installed dependencies; it installs nothing and does not start Desktop or a service.

From this directory:

```sh
unzip replay-payload.zip -d replay
python3 replay/replay.py \
  --source /path/to/official/hermes-agent \
  --deps /path/to/compatible/monorepo \
  --node /path/to/node-26-or-later \
  --out ./replay-results
```

The dependency root must contain both `node_modules` and `apps/desktop/node_modules`, including the Vite configuration's Babel plugin. A missing dependency is a setup failure, not a red behavioral case. Actual tested versions and deviations from the pinned lock are in `dependencies.json`; this is not exact-lock certification.

## Executed scope and limits

- Same 13 focused cases on baseline: 10 assertion failures / 3 passes. On the complete candidate: 13 passes. The exported replay was run again and reproduced this result.
- Developer's current-source selections: 13 focused, 30 hook, 26 neighbors and 6 hydration/cache cases. These overlap: 71 unique passing cases, not 75. Renderer TypeScript and changed-surface ESLint exit zero; one pre-existing hook dependency warning remains.
- Independent QA reran the same 13-case comparison, added 10 independently designed boundary cases (baseline 7 failures / 3 passes; candidate 10 passes), and exercised the selected 26 neighbors / 6 hydration cases. Its 55 unique executed cases are a separate, overlapping cohort, not additional coverage to add to the developer's 71.
- Complete original contribution plus test extension: clean forward application and exact reverse restoration of all eight final affected files. The branch's eight code/test files match the frozen tested payload.

Actual dependencies were Node 26.12.1, React 19.2.7, Vitest 4.1.10, Vite 8.2.0, TypeScript 6.0.3 and jsdom 29.1.1. RPC/REST are test doubles; the real hook, reducer, history projection and tool-result consumers run in the source tests. All fixture content and non-public-layout placeholders are synthetic.

This does not prove an actual Windows incident's unique cause, scrolling geometry, sidebar search performance, Electron packaging, upstream CI, merge/release, or installed-client adoption. Historical 190/1,049-case evidence belongs to an earlier baseline and is not relabeled as this run. No private transcripts, actual hidden reasoning, real user session identifiers, screenshots, account data, credentials or machine paths are included. The original MIT license and author attribution remain intact.
