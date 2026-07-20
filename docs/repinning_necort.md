# Re-pinning `vendor/necort`

`vendor/necort/` is a git submodule tracking a specific commit of
`PhialsBasement/Chain-of-Recursive-Thoughts` PR #7
(`refs/pull/7/head`), currently pinned to
`f4d290ceb086d47bb0f872164344836c47134452`. There is **no auto-update**
(per `docs/build-plan.md` open question, resolved as "manual only" for
v1). Re-pinning is a deliberate, reviewed action.

## Why the fetch needs an explicit ref spec

PR #7 is not a branch on the upstream repo — it's a pull-request head. A
plain `git -C vendor/necort fetch origin` (or `git submodule update
--remote`, which fetches the submodule's configured branch/tag, not an
arbitrary PR ref) will **not** pull new commits pushed to that PR, because
GitHub only exposes PR heads at the `refs/pull/<N>/head` ref, and that ref
is not fetched by a normal `git fetch` unless asked for by name. You must
fetch it explicitly:

```bash
git -C vendor/necort fetch origin refs/pull/7/head
```

This populates `FETCH_HEAD` in the submodule's local repo with the PR's
current head commit, without creating a local branch for it.

## Manual re-pin procedure

1. **Fetch the PR head explicitly** (see above — do not skip the ref spec):
   ```bash
   git -C vendor/necort fetch origin refs/pull/7/head
   ```

2. **Inspect what changed before checking it out.** Compare the currently
   pinned SHA against the new `FETCH_HEAD`:
   ```bash
   git -C vendor/necort log --oneline f4d290ceb086d47bb0f872164344836c47134452..FETCH_HEAD
   git -C vendor/necort diff f4d290ceb086d47bb0f872164344836c47134452..FETCH_HEAD -- recursive_thinking_ai.py nash_recursive_thinking.py
   ```
   Focus the diff on the two files this project actually uses (see
   `docs/necort_deps.md` for why only these two matter). If PR #7 has
   rebased or force-pushed, `FETCH_HEAD` may not be a descendant of the old
   pin at all — read the diff either way before trusting it.

3. **Checkout the new SHA:**
   ```bash
   git -C vendor/necort checkout <new_sha>
   ```
   (Use the literal SHA, not `FETCH_HEAD` — `FETCH_HEAD` is transient and
   won't survive being recorded as the gitlink.)

4. **Re-check the import surface.** If the new commit adds/removes
   top-level imports in `recursive_thinking_ai.py` or
   `nash_recursive_thinking.py`, `docs/necort_deps.md`'s dependency table
   may need updating (new third-party import → new pin; a dropped one is
   safe to leave, but note it). Re-run the empirical import check used to
   build that doc:
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0, 'vendor/necort')
   import recursive_thinking_ai, nash_recursive_thinking
   print('import OK')
   "
   ```

5. **Run the smoke-import test** (`tests/test_necort_vendor.py`) — it will
   fail on the pinned-SHA assertion until you update
   `EXPECTED_PIN_SHA` in that file (and the SHA references in
   `docs/execution-plan.md`'s Global Constraints and in this doc's header)
   to match the new pin:
   ```bash
   uv run pytest tests/test_necort_vendor.py -v
   ```

6. **Run the adapter test suite** (once Task 10 exists — `necort_adapter.py`
   and its tests). The adapter is what actually drives the vendored code at
   runtime (including the `datetime` NameError shim), so it's the real
   regression gate for a re-pin, not just the import smoke test:
   ```bash
   uv run pytest
   ```

7. **Update the LICENSE reference if the license text changed.** Diff
   `vendor/necort/LICENSE` against what `LICENSE-NOTICES` describes; update
   `LICENSE-NOTICES` if copyright holders or terms changed.

8. **Commit the new gitlink** together with any doc/pin updates from steps
   4–7, as one commit:
   ```bash
   git add vendor/necort docs/necort_deps.md docs/repinning_necort.md \
       docs/execution-plan.md LICENSE-NOTICES tests/test_necort_vendor.py
   git commit -m "chore: re-pin vendor/necort to <new_sha>"
   ```
   Do not fold unrelated changes into this commit — a re-pin should be
   independently revertable.

## Verifying the current pin

```bash
git submodule status                       # shows the recorded gitlink SHA
git -C vendor/necort rev-parse HEAD          # shows the submodule's actual checkout
```
Both should print `f4d290ceb086d47bb0f872164344836c47134452` (a leading
`+` on the `git submodule status` line means the submodule's working
checkout doesn't match the SHA committed in the superproject's index —
run `git submodule update` to reconcile, or re-commit the gitlink if the
new checkout is intentional).

## After a fresh clone

The submodule is not populated by a plain `git clone`. Either clone with
`git clone --recurse-submodules <repo>`, or after a normal clone:
```bash
git submodule update --init
```
`tests/test_necort_vendor.py` skips (not fails) if the submodule isn't
initialized, with a message pointing at this command.
