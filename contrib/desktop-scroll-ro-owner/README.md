# Bounded Desktop scroll repair: observer baseline and transcript owner

This is a source/evidence follow-up for the **scroll arm of [#128468](https://github.com/NousResearch/hermes-agent/issues/128468)**. It does not diagnose that issue's NULL/display-order persistence arm, fix every transcript jump, or replace the broader reader-ownership/persisted-landing work in [PR #137886](https://github.com/NousResearch/hermes-agent/pull/137886). It is not a competing PR and has not changed another contributor's branch.

## Two separate source failures

1. A delayed `scroll` from the preceding bottom pin can arrive after the next DOM text growth but before its native ResizeObserver. Updating `previousResizeMetrics` in that scroll callback consumes the growth; the synchronous follow then misses it. The previous-size baseline must advance only in the content observer.
2. Removing that write alone exposes a distinct ownership boundary: the same mounted Thread can switch from a shorter outgoing transcript to a different running transcript with a saved reading offset. A long-lived observer must not compare the incoming content with the old owner's height and pin the reader to the bottom. Recreate/reseed that existing observer at the session/profile-storage/empty-to-loaded commit boundary, before native RO; ordinary scroll continues to update reading state, not resize history.

Production changes are only in `apps/desktop/src/components/assistant-ui/thread/list.tsx`. There is no new scroll owner/store, persistence key, wire/turn protocol, timeout, backend, or styling change.

## Small review path and complete tested recovery

**`scroll-owner-fix.patch`** is the 3-file review delta: that production file and the two required component regressions. Its exact preimage/application and final three blob hashes were separately checked against official main **`016b76da3a8a88e28775e08b6ca2846f471b8eda`**. All executed behavior/check results below belong to the complete tested composition, **not a standalone rerun without its prerequisite**. This small patch is not a promise of clean application to PR #137886's different/extracted implementation.

**`complete-public-base.patch`** is the independent, directly applicable recovery path from that same public main. It composes the previously accepted commentary-reconciliation candidate (13 paths: 11 source/test leaves plus 2 contribution-evidence files, provenance from original PR #132913 / Oidaladn0) with this 7-path scroll unit (one production file, two tests, HTML/fixture and browser replay/docs). It does not count the prerequisite as newly authored scroll work or re-run/add its earlier 86-case suite to this unit's totals.

```sh
git checkout --detach 016b76da3a8a88e28775e08b6ca2846f471b8eda
git apply --check /path/to/complete-public-base.patch
git apply --index /path/to/complete-public-base.patch
git write-tree
```

Expected exact tested tree: **`81ed262369d99d48f2629f8ce1a0eb86e19cd296`**. Complete-patch SHA-256: **`4f7122dbb75a4cd7e42b472790ef2d3fb93ce0940d819015b277b34bfcbc2c26`**. Its 20 paths were cold-applied, reversed to the official base tree, and re-applied to that identical tested tree in a task-owned native Git index/object store. Unrelated promised blobs were not materialized; this was tree/file-byte proof, not a new whole-source restore/build or test run.

For explicit split application, `accepted-reconcile-prerequisite.patch` then `scroll-owner-replay.patch` produce the same tree. The replay patch is relative to local application checkpoint `6912825d9f69b6181dad21ca0c5c1bfe9af6ea51`, but you do not need that unpublished checkpoint: the public main plus the included prerequisite reconstruct it. Do not apply the 3-file patch and then the 7-file replay on top of each other.

Use a disposable developer checkout with the repository-supported workspace dependencies. **Do not apply any patch here to an installed/running Hermes tree.** The artifact carrier's commit is not the tested implementation commit `44a94bcb7b13fe9810b66cec310e149f90a8f236`.

## Reproduction and executed independent QA

- Same final main-growth component test: original inherited base **fails 4520 → expected 4760**; the initial scroll-only candidate fixes that test but fails the newly discovered cross-owner regression (**4400 instead of preserved 3600**). The final source passes both. QA used the original independent test bytes and verified the real running marker/viewport reuse, rather than sampling before asynchronous runtime publication.
- **81 unique component/store tests** in six affected files passed on the final exact source, including existing follow/reading/restore neighbors. They use explicit jsdom geometry/RO doubles; these are not Chromium tests.
- Real headless **Chromium** independently ran the same final production Thread/Markdown/CSS and native layout/RO. The original **17 case IDs** pass on both initial and final candidates. A separate **3-ID owner cohort** changes initial **2 pass / 1 fail (800px reading gap becomes 0)** to final **3/3**, with the restored incoming gap remaining **800px** through native RO. Those cohorts are not added to the 81 as one unique-test total. QA's final run recorded 656 rAF/RO samples; samples are not assertions that each point was painted.
- The browser fixture makes a previously observed race repeatable by injecting one pending scroll from a MutationObserver after actual text/owner commit and before native RO. Layout/resize are not mocked in Chromium. Four-row minimum-height CSS deliberately isolates owner-switch geometry; it is not organic Markdown/backfill timing coverage or product UI whitespace.
- Exact final HEAD passed all **14 official native checks**, health **0 blocking / 0 advisory**, full noEmit and read-only ESLint. Full native source export, all three author's patches/cold index forward/reverse identities and unchanged inherited source were independently checked.

After applying the complete tested composition and preparing the existing workspace/Playwright Chromium dependencies:

```sh
node contrib/scroll-continuity/browser.mjs /absolute/task-owned/evidence
```

The included replay README explains observer/rAF samples, test actors and cleanup. Source/driver/fixture and patch hashes are in `VERIFICATION.json`. Browser/Vite/test resources exited; no R16, user input, private chat/database, live model or backend was used.

Executed base is 016 above. Later inspected `9a76ac8e4f612c081e12008923893077f10f2ec7` only changed two plugin-catalog files; affected source/dependencies were unchanged. That is bounded source equivalence, not a full run of that branch. Original Windows/Electron occurrence attribution and runtime adoption remain unverified. This does not claim #128468/#132776 resolved, PR #137886 tested/compatible, upstream CI/approval/merge, a release, or installed-client acceptance.
