# Known issues / open items

Running list of things found during development or manual verification that are real
enough to act on eventually, but not blocking. Each should have a tracked test (xfail
or otherwise) or a reference to one, so it doesn't only live in a report.

## 1. Weak semantic ranking can put the correct object below rank 1 (step 3)

**Found:** manual verification pass on `lld_step2.retrieval_cli`, 2026-09-05 (see also
`reports/step3_scoring_fix_report.md`, phase 1 calibration table).

For the requirement "Generate the next order number using a number range object", the
genuinely correct object (`ZFM_ORDER_NUMBER_RANGE`, whose only chunk literally calls
`CALL FUNCTION 'NUMBER_GET_NEXT'`) scores `semantic_score=0.675`, while an unrelated
chunk (`ZCL_ORDER_PROCESSOR.build_item_list`, a plain `SELECT * FROM zorder_item`) scores
higher at `0.734` — on raw embedding similarity alone, the wrong chunk looks like the
better match. `ZFM_ORDER_NUMBER_RANGE` still surfaces at rank 4 of 5 (a human skimming
the candidate list would still catch it), but it does not rank first, unlike every other
manually-checked requirement in this package.

**Status:** tracked via `test_order_numbering_semantic_ranking_miss` in
`tests/test_retrieval.py`, marked `xfail` with this explanation, so it stays visible in
test output rather than only in this file or a report. Not fixed — this is exactly the
kind of single-observed-case result the project has already agreed not to chase with an
ad hoc weighting tweak (see the embedding investigation report and the step 3 brief's
"Notes on judgment"). If this pattern recurs on a larger real extraction, that's when a
weighting change is warranted, backed by another proper benchmark — not this one
instance.

## 2. `NEW_OBJECT_SPREAD_THRESHOLD` is currently dead code in practice

**Found:** same manual verification pass, testing ~10 requirements spanning genuine,
nonsense, and borderline wording.

`likely_new_object` is guarded by two conditions — the `base_score` floor and the
top-vs-5th `base_score` spread — designed as independent checks so either one alone
could catch a case the other missed. On `ZORDER_MGMT_extract.txt`, they never once
disagreed: spread was ≥0.32 in every case where the floor passed, and <0.02 in every
case where the floor failed. Root cause: the structural score's step function (0 / 0.3
/ 0.4 / 0.6 / 1.0) is coarse relative to the 0.05 spread threshold — any difference in
hop-tier between the top and 5th candidate already produces a gap far larger than 0.05.
So on this dataset, the spread condition is never the deciding factor; the floor check
is doing 100% of the real work, and the spread check is redundant logic that happens to
never be exercised on its own.

**Status:** correctly implemented per the brief, not a bug — and deliberately **not
being fixed or removed now**. Reworking or dropping the spread check would be scope
creep into a step that's already been validated and closed out; it stays as-is.
Worth revisiting only if either of these happens:
- **The structural score becomes more granular** (e.g. a continuous graph-distance
  measure instead of the current 5-value step function) — finer steps could let spread
  vary independently of the floor instead of moving in lockstep with it.
- **A real case turns up where spread fires independently of the floor** — i.e. the top
  candidate clears 0.35 but the top-vs-5th gap is still under 0.05. None of the ~10
  requirements tried here produced one; a larger, more architecturally varied real
  extraction might.

Until one of those happens, treat `NEW_OBJECT_SPREAD_THRESHOLD` as unvalidated and not
worth tuning — there's currently no observed scenario where changing its value would
change any actual outcome.
