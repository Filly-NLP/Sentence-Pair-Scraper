# Repository agent routing

Use the configured custom agents for repository work:

- `planner` uses `gpt-5.6-sol` with medium reasoning for planning and
  architecture work. Wait for its result and use it as the implementation plan.
- `reviewer` uses `gpt-5.6-sol` with medium reasoning for review-only requests.
  Wait for its evidence-backed findings and do not edit code during a review.
- `worker` uses `gpt-5.6-luna` with xhigh reasoning for repository exploration,
  discovery, tracing, implementation, fixes, refactors, tests, and validation.

For implementation work, obtain a concrete plan first, then delegate the
approved scope to `worker`. Preserve unrelated working-tree changes, inspect
the resulting diff, and run focused verification before reporting success.
Never commit, push, stash, reset, clean, revert, or expand the approved scope.
