# Origin-linked work: pinned-main composition and current executor follow-up

Support for [NousResearch/hermes-agent#132544](https://github.com/NousResearch/hermes-agent/pull/132544), whose original contribution and authorship belong to **richardhowes**. This is a patch-artifact branch to make that existing PR easier to consume and review, **not a competing PR or an update to its author's branch**.

## Apply the complete candidate

Use **`origin-main-composition.patch`**, starting from official main **`c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`** in a disposable developer checkout. It composes original PR head `c5b236e4b2915b88c9c76009970d63462c64a1e1`, resolves the current-main composer conflict, and includes the role/phase follow-up. No other original-PR patch needs to be applied first.

```sh
git checkout --detach c419cf7c864ac0ee511dbd9f7091a8bba0b567ee
git apply --check /path/to/origin-main-composition.patch
git apply --index /path/to/origin-main-composition.patch
git write-tree
```

Expected tree: **`da0a53e3b5ac7b48dbb81c4f3ed639447c04fea1`**. There are **33 changed source leaves**. Do not apply these instructions to an installed/running Hermes tree.

**`author-followup.patch` is the smaller review delta**, relative to the already-composed merge checkpoint `991393f8f8500f3d8bf71f73d276b36117798b64`. It is NOT directly applicable to raw PR head or stock main. The complete main-based patch above is the independent recovery path. The frozen tested-source identifier is `0bcccbee4f3320d006e4636d7ea050009b1b7001`; it identifies the tested source snapshot, not the Git commit of this artifact branch. Fetching this artifact branch is not a promise that local checkpoint commits are published.

## Follow-up behavior

- Origin-linked durable work stays visible when the foreground stream drawer is collapsed or foreground activity has ended, while that existing drawer remains mounted.
- The optional `current_executor` wire object has `{profile, display_name, phase}` and reports only the verified, unfinished `task.current_run_id` belonging to that task. `phase` is `execution` or `review`; review comes from the native review-request/claim lifecycle, never a profile-name guess.
- Labels are read only from the matching active Hermes home. Missing/unreadable/untrusted profile metadata remains null; symlinks and escaping profile names are rejected. The frontend preserves execution uncertainty and does not fall back to `task.assignee` as the active worker.
- Existing `activity` freshness/status wins: reserved, stale, terminal or unreadable tasks do not become healthy running work merely because an executor field is present. Existing CLI origin/subscription semantics remain unchanged.
- The tests and Chromium fixture use actual captured native API responses over disposable synthetic SQLite/Home state, including the intentional unavailable-store HTTP 500 negative path. It is not a live dispatcher/model run or an installed Electron/Windows validation.

## Executed verification on the frozen source

Independent QA ran **39 unique frontend tests**, **27 native Python tests**, **8 additional API boundary assertions**, and **13 real-source React-component/Chromium assertions**. These are separate cohorts, not an additive unique-test total. The exact frozen source passed **14 official native checks**, repository health **0 blocking / 0 advisory**, full noEmit, and read-only lint on 8 affected leaves. Main-based and review-delta patches were cold-applied/reversed and bound to the same final tree. `VERIFICATION.json` records patch SHA-256 values and scope.

This does **not** claim upstream GitHub Actions success, approval, merge, removal of the original PR's DIRTY status, a release, or runtime adoption. It does not claim the independently tracked message-duplication, scroll or search issues are fixed. No private runtime state, user chat, credentials or private screenshots are included.
