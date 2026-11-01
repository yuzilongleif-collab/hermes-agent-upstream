# Guarded receive-side control reads: optional supplement to PR #144701

This supports [NousResearch/hermes-agent#144701](https://github.com/NousResearch/hermes-agent/pull/144701), whose original diagnosis and admission-side proposal belong to **AbdullahPesteli**. It is an optional, more conservative variant offered to that existing effort, **not a competing PR or a claim that its author's deliberately weaker ordering contract is a defect**.

## Primary apply path

Use `control-read-hol.patch` in a disposable developer checkout at official commit **`016b76da3a8a88e28775e08b6ca2846f471b8eda`**. It includes both production changes and all required regression/fixture additions: **7 changed leaves, 2 production**. It is a complete main-based alternative, not a patch to apply on top of PR head `121ce0a87f7fe3dd2cc93aa258035d26fbd7e722`.

```sh
git checkout --detach 016b76da3a8a88e28775e08b6ca2846f471b8eda
git apply --check /path/to/control-read-hol.patch
git apply /path/to/control-read-hol.patch
bash scripts/run_tests.sh tests/gateway/test_control_read_ordering.py
bash scripts/run_tests.sh tests/gateway/test_control_read_unknown_scope.py
```

Prepare the repository-supported dev/test environment first. Do not apply this to an installed/running Hermes tree. The complete patch SHA-256 is **`48b23960b8677be083758ae36b307d2bf3a6d3e29652eab033dad76d7116324f`**; `VERIFICATION.json` includes every changed leaf's before/after SHA-256.

The tested local archive-initialized source snapshot has tree label `090f3294606b56e3f9aed13184879d7c05ba083f`. This is a retained verification label, **not a published implementation commit, the artifact branch's tree, or a promised whole-target tree after applying the patch**. Verification binds the public base, affected source/dependencies and exact final file bytes. The official base commit's actual tree is `ff68247d5204fa6edbd8839b055c541e9906d09a`.

## Contract and trade-off

- Only canonical `session.control.read` enters the existing bounded read lane at receive time. Existing serial handlers and legacy/context-read routing are retained.
- Socket-local completion fences preserve predecessor/successor dispatch ordering when scopes overlap. A scope is either a proven mounted LOCAL session, a known profile with an uncertain session, or an unknown whole-socket scope. Scope classification is not authorization; original dispatch/schema/authority checks still execute.
- Resume and the entire mutate verb remain profile-fenced: short IDs, physical compression segments and multi-target mutations cannot safely be separated by raw `session_id`. Cold/history/messaging sessions are conservative too. Unknown/sessionless methods use a whole-socket fence.
- The existing 2-running/8-waiting admission limit remains. Waiting for a predecessor does not consume a running slot. Disconnect cancels unstarted reads before draining serial successors; started work is allowed to finish. Completion does not mean waiting for the entire model turn/stream.

This retains stricter same-session ordering than the PR author's intentional early-read interleaving. Consequently same-profile resume/mutate or unknown requests can still delay a read; saturated admission can still park the reader. It is not a global scheduler, lineage cache, larger timeout, claim of arbitrary worker cancellation, or universal control/heartbeat fix. It also does not replace the independent storage-lock repair discussed on #140667, nor include the PR author's separate slow-dispatch logging change.

## Executed verification

Independent QA actually ran the repository's native runner over **46 original unique cases** (14 authenticated-peer ordering/lifetime/permission cases, 2 unknown-scope cases and 30 adjacent held/read-lane/transport cases). Capability-negative, multiplex and some adjacent cases use disclosed in-memory socket adapters with real authority/handlers; they are not all TCP tests.

QA added **4 independent real-handler boundary cases** using the in-memory socket adapter. Their corrected final union is **50 unique passing cases**, not a single 50-case run and not author+self-review counts added together. A send-failure oracle was corrected to require handler completion rather than a reply on a closed transport; the original failed oracle was retained, the one affected case rerun, and the other three cases' unchanged dependencies/AST verified.

The unchanged optional `test_qa_hol_edges.py` is provided separately. For those four extra cases, place it at `tests/gateway/test_qa_hol_edges.py` in the already-patched disposable checkout and run:

```sh
bash scripts/run_tests.sh tests/gateway/test_qa_hol_edges.py
```

The primary seven-leaf patch does not include this QA-only file. In the decisive final-fixture comparison, original production left control(B) pending for a roughly 2-second observation while same-socket ping and a second-socket read succeeded; the candidate returned control(B) in about 12 ms before export(A) was released. Export then finished normally. This is a synthetic paused-storage experiment, **not measured production latency**.

The same final seven source/test leaves passed the applicable `ruff,health` selection (2 checks, 0 blocking/0 advisory) and diff whitespace checks; **no full CI or all-native-check claim**. Independent offline source recovery, exact source/import identities, patch forward/reverse/reapply and final-fixture baseline behavioral failure were checked. The full private bundles are intentionally not published.

Executed base: 016 above. Later inspected main `9a76ac8e4f612c081e12008923893077f10f2ec7` differed only in two plugin-catalog files; affected code and tested dependencies were unchanged. This is bounded source equivalence, not a complete run of that later branch. No installed core, running service, model/provider, real chat/database, or Windows/Electron was changed or certified. Publication, original-author adoption, upstream approval/merge and normal runtime adoption remain separate.
