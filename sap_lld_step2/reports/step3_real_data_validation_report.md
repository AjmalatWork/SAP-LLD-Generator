# Step 3 validation against real data: `ZZBA91`

Validates step 3 (retrieval + tiering) against the real `ZZBA91` package (23 objects,
474 dependency entries, 263 DDIC objects) loaded via the updated step 2 pipeline. This
is a validation and investigation task — no scoring weights, thresholds, or
`CALL_LIKE_EDGE_KINDS`/table-sharing logic were changed. All numbers below are measured
directly against the real, loaded data; nothing is estimated.

**One methodology note up front:** the local dev database still had the 15-object
`ZORDER_MGMT` package loaded from prior work when this task started. Testing "nonsense"
requirements with both packages loaded is invalid — `ZORDER_MGMT` genuinely contains
order/notification/shipping objects, so a requirement like "send an email when an order
ships" is a real match against it, not nonsense. `ZORDER_MGMT` was removed from the
database for the isolated runs below (checks 1 and 4), then the requirements were also
run once with both packages present to confirm the contamination (see check 1). No step
2 files or fixtures were touched — this is a `DELETE FROM objects WHERE package = ...`
against the local dev database only, and `ZORDER_MGMT` was reloaded via
`ZORDER_MGMT_extract.txt` after this task's checks were complete so the dev DB is back
to its prior state for anyone else picking up step 3 work next.

## 1. Manual CLI pass with real, domain-grounded requirements

Eight requirements were run through `lld_step2.retrieval_cli`, grounded in method names
and comments read directly from the real `ZZBA91` source (`ZCL_CBA_*`, `ZPCBA91R_*`,
`ZPCBAR_*`). Full CLI output for all runs is preserved in
`step3_real_data_evidence/all_results.txt` and `step3_real_data_evidence/isolated_results.txt` alongside this
report for independent verification.

| # | Requirement (grounded in) | Type | Top candidate | final | Verdict |
|---|---|---|---|---|---|
| R1 | `ZPCBAR_PURGE_SIMU_DATA`'s own header comment + `P_DAYS`/`P_PACK` selection screen | clear | `ZPCBAR_PURGE_SIMU_DATA` | 0.934 | **Correct** — exact match |
| R2 | `ZCL_CBA_AUTHORIZATION::USER_HAS_READ_ACCESS`, called from `ZCL_CBA_GET_AC_DOM::GET_USER_AC_PROGRAM` | clear | `ZCL_CBA_GET_AC_DOM` | 0.938 | **Correct** — `ZCL_CBA_AUTHORIZATION` (the more literal match) ranks 3rd at 0.919, still top 5 |
| R3 | `ZCL_CBA_JSON_UTIL::GET_PROPERTY`'s reflection-based component lookup | clear | `ZCL_CBA_JSON_UTIL` | 0.927 | **Correct** — exact match |
| R4 | `ZCL_CBA_SHORTEST_PATH`, `ZCL_CBA_SIMPLIFIED_GRAPH`, `ZCL_CBA_TRANSLATE_ENGINE::FILL_SHORTEST_PATH`/`FILL_SIMPLIFIED_GRAPH` | ambiguous | `ZCL_CBA_SHORTEST_PATH` | 0.909 | **Correct — genuinely ambiguous**, all 3 real graph/path classes surface in top 3 |
| R5 | `ZCL_CBA_QUALITY_LOG::GET_TRANSLATION_OUTPUT`/`GET_PROB_ALIAS`/`GET_PROB_INV`, dispatched from `ZPCBAR_DISPLAY_OUTPUT` | ambiguous | `Z_CBA_COMPUTE_ALIAS_FORM_FILE` | 0.918 | **Ranking miss — see below** |
| R6 | (nonsense: email notification on order ship) | nonsense | — | 0.173 | Correctly flagged `likely_new_object: True` (isolated) |
| R7 | (nonsense: bank reconciliation) | nonsense | — | 0.189 | Correctly flagged `likely_new_object: True` (isolated) |
| R8 | (nonsense: PDF invoice / POS) | nonsense | — | 0.183 | Correctly flagged `likely_new_object: True` (isolated) |

**R5 ranking miss, read against the actual code:** `ZPCBAR_DISPLAY_OUTPUT` is the
report literally named for this requirement — its `START-OF-SELECTION` is a `CASE
P_DATAOB` dispatch that calls exactly `GET_TRANSLATION_OUTPUT`, `GET_PROB_ALIAS`, and
(by the same pattern) an invariant-data getter on `ZCL_CBA_QUALITY_LOG`. But
`ZPCBAR_DISPLAY_OUTPUT` ranks **7th of 23** (`final=0.603`), one place below the top-5
cutoff, because its own chunk text is mostly `INCLUDE` statements and a thin dispatch
`CASE` (`semantic=0.609`) and it isn't itself a top-3 semantic seed, so it only gets the
1-hop structural score (0.600) rather than the seed bonus (1.000). `ZCL_CBA_QUALITY_LOG`
— which does hold the exact-name methods — does rank correctly, at 3rd (`final=0.912`).
This is the same category of miss already tracked in
[ISSUES.md #1](../ISSUES.md) (the `ZFM_ORDER_NUMBER_RANGE` case): a legitimately correct
object is pushed down the ranking, not out of the results, by thin/boilerplate source
text scoring low on raw semantic similarity. It is not being fixed here, per the same
reasoning already recorded in ISSUES.md — this is a second observed instance of a known,
already-tracked pattern rather than a new one. **Recommendation: append this as a second
occurrence under ISSUES.md #1** rather than opening a new item, since it's the same root
cause on a second real object.

**Contamination check:** running R6 against the database *with `ZORDER_MGMT` still
loaded* (`step3_real_data_evidence/all_results.txt`, R6) returns `ZCL_NOTIFICATION_SENDER`
(`ZORDER_MGMT`) at `final=0.904` — a **correct**, non-nonsense match, because
`ZORDER_MGMT` really does have `notify_order_confirmed`/`notify_order_failed` methods.
This isn't a step 3 bug; it's confirmation that retrieval has no per-requirement package
scoping and pools every loaded package as candidates. Worth knowing for step 4: if
multiple packages are ever loaded at once, "which package is this LLD for" needs to come
from the scoping note, not be assumed from context.

## 2. Structural signal sparsity — table-sharing bonus firing frequency

**Confirmed as the brief anticipated, and worse in practice than the raw edge_kind
counts alone suggest.**

Raw counts in the loaded `ZZBA91` data:

| ddic_type | `object_uses_table` rows | distinct objects |
|---|---|---|
| `TABL` | 6 | 3 |
| `TTYP` | 3 | — |
| `DTEL` | 19 | — |
| `STRU` (no `object_uses_table` row — not wired per the original brief) | 35 | — |

Only **3 of the 23 objects** (`ZPCBAR_PURGE_SIMU_DATA`, `ZPCBA91R_IMPORT_ALIAS_MDL`,
`ZPCBA91R_COPY_TABLE`) reference a real `TABL`-type table at all, and their `TABL`
references don't overlap with each other or with any of the 8 requirements' seed
objects tested above.

Directly measured firing frequency: across all 5 real (grounded + ambiguous)
requirements in check 1, the table-sharing bonus (`STRUCTURAL_SCORE_SHARED_TABLE = 0.4`)
**fired zero times** — i.e. for every requirement, either no seed object had any `TABL`
reference at all (4 of 5 requirements: R2–R5), or the one requirement that did have a
seed with a `TABL` reference (R1: `ZPCBAR_PURGE_SIMU_DATA` → `ZDCBAT_UC2_UID`) had no
other object in the package sharing that specific table. The bonus is not merely rare on
this package — it never once changed a candidate's structural score for any requirement
tried.

**Verdict: (c) a real gap that should block moving to step 4 until addressed**, or at
minimum be explicitly acknowledged as a known limitation before step 4 relies on it. The
brief's hypothesis is confirmed directly, not just suggested: real ABAP code in this
package overwhelmingly expresses "these objects touch the same data" through `STRU`
(structure references, 35 occurrences) and `DTEL` (data-element references, 19
occurrences) rather than raw `TABL` references. A bonus scoped only to `TABL` is
calibrated against a signal that is common in the hand-crafted `ZORDER_MGMT` fixture
(deliberately dense table-sharing, per the brief's own description) but essentially
absent in real code. This doesn't mean step 3 is unusable today — call-graph proximity
(`CALL_LIKE_EDGE_KINDS`, see check 5) is still doing real, correct work, as check 1
shows — but the table-sharing half of the structural score is currently decorative on
real data. Recommend widening the bonus to include `STRU` (and possibly `DTEL`) as its
own explicit, evidence-backed decision — not implemented here per the brief's scope.

## 3. Embedding-quality benchmark re-run against real `ZZBA91` chunks

Ran `lld_step2.embedding_bench` (unmodified) against the real, loaded `ZZBA91` chunks.
Full output: `step3_real_data_evidence/embedding_bench_real.txt`.

| | related (n) | unrelated (n) | mean related | mean unrelated | Cohen's d |
|---|---|---|---|---|---|
| **`ZZBA91` (real, this task)** | 3,625 | 103,112 | 0.7136 | 0.7117 | **−0.011** |
| `ZORDER_MGMT` (original investigation) | — | — | — | — | ≈ 0.10–0.12 |

Object-pair ground truth: 12 related object pairs, 241 unrelated, out of 253 total pairs
(`CALL_LIKE_EDGE_KINDS` call edges ∪ shared-`TABL` pairs — same definition as the
original benchmark, unmodified).

**Separation did not improve on real code — it got worse, and flipped sign.** Cohen's d
of −0.011 is not just "still negligible," it means related-pair chunk distances are
(negligibly) *larger* than unrelated-pair distances on average — the opposite of the
intended direction. This is a smaller, noisier effect than even the already-negligible
0.10–0.12 found on the small, hand-crafted `ZORDER_MGMT` sample.

**Broadened "related" definition, tested per the brief's specific question:** re-ran the
same benchmark with "related" widened to also include any pair of objects that share a
`STRU` or `MESS` reference (not just `CALL_LIKE_EDGE_KINDS` + shared `TABL`). This adds
5 more related object pairs (12 → 17) and ~356 more related chunk pairs (3,625 → 3,981).
Result: **d gets worse, not better** (−0.011 → −0.047). Widening "related" to include
structure/message-class sharing does not recover a real separation signal on this
package — if anything it dilutes it further, since two objects that merely reference the
same message class are evidently not more semantically similar in embedding space than
two arbitrary objects.

**Verdict: (c) — this should block treating semantic similarity as a strong standalone
signal, but it does not block step 4, because step 3 was already designed around this
finding.** The original investigation's 0.30/0.70 semantic/structural weighting was
chosen precisely because semantic similarity alone showed weak separation even on the
synthetic sample — this real-data result is a confirmation of that original caveat, not
a new problem the current design is unprepared for. It does mean the weighting split
should not be revisited toward *more* semantic weight based on any future "it might
generalize better on real data" hope — this result says the opposite. No weighting
change is recommended or implemented here.

## 4. Seed-circularity calibration re-run on real requirements

`SEED_MIN_SEMANTIC_SCORE = 0.67` re-checked against the 5 grounded/ambiguous and 3
nonsense requirements from check 1 (isolated to `ZZBA91` only, so nonsense results
aren't contaminated by `ZORDER_MGMT` — see the methodology note above). Top-1 semantic
score per requirement:

| Requirement | Type | Top-1 semantic score | Clears 0.67 floor? |
|---|---|---|---|
| R1 (purge job) | grounded | 0.7785 | Yes |
| R2 (read access check) | grounded | 0.7938 | Yes |
| R3 (property reflection) | grounded | 0.7560 | Yes |
| R4 (shortest path) | ambiguous | 0.6974 | Yes |
| R5 (display output) | ambiguous | 0.7253 | Yes |
| R6 (email notification) | nonsense | 0.5783 | No |
| R7 (bank reconciliation) | nonsense | 0.6307 | No |
| R8 (PDF invoice) | nonsense | 0.6112 | No |

**The gap still holds cleanly — every grounded/ambiguous requirement's best candidate
clears 0.67, every nonsense requirement's best candidate does not — but it is
considerably narrower on real data than the calibration's own numbers suggested.** The
lowest grounded/ambiguous score (R4, 0.6974) sits only **0.027 above** the floor, and
the highest nonsense score (R7, 0.6307) sits only **0.039 below** it. The floor is not
centered in the gap; it sits close to the grounded side. The gap itself is only ~0.067
wide on this data. This is a materially thinner margin than a floor calibrated on a
small, densely-connected synthetic package would suggest is safe, and it's a sample of
only 8 requirements on one real package — not enough to conclude the floor is exactly
right, only that it isn't currently wrong.

**Verdict: (b) — a real gap worth watching, not urgent, not blocking.** The floor still
does its job on every case tried. But given how thin the real-data margin is (0.027 on
one side, 0.039 on the other, from a single ambiguous ABAP requirement each), a single
future requirement with slightly more generic wording than R4 or slightly more specific
wording than R7 could plausibly land on the wrong side of 0.67. Recommend re-checking
this gap again once a second real package is available (out of scope to source one
here, per the brief), rather than adjusting the constant now on an 8-point sample.

## 5. `CALL_LIKE_EDGE_KINDS` scope check

Current `CALL_LIKE_EDGE_KINDS` (from `graph_loader.py`, unmodified) and real counts:

| Raw `dependency_type` | Mapped `edge_kind` | Count | In `CALL_LIKE_EDGE_KINDS`? |
|---|---|---|---|
| `METH` | `calls_method` | 197 | **Yes** |
| `OM` | `calls_method` | 57 | **Yes** |
| `CLAS` | `class_ref` | 43 | **Yes** |
| `INCL` | `includes` | 12 | **Yes** |
| `INTF` | `implements` | 7 | **Yes** |
| `FUNC` | `calls_function` | 7 | **Yes** |
| `PROG` | `program_ref` | 6 | **Yes** |
| `FUGR` | `function_group_ref` | 6 | **Yes** |
| **Included subtotal** | | **335 / 474 (70.7%)** | |
| `MESS` | `message_ref` | 36 | No |
| `STRU` | `structure_ref` | 35 | No |
| `OA` | `reference` (unmapped, falls to default) | 26 | No |
| `DTEL` | `data_element_ref` | 19 | No |
| `DGT` | `generic_type_ref` | 7 | No |
| `TABL` | `table_ref` | 6 | No (handled separately, see check 2) |
| `MSAG` | `message_class_ref` | 3 | No |
| `TTYP` | `table_type_ref` | 3 | No |
| `TYPE` | `type_ref` | 3 | No |
| `TRAN` | `transaction_ref` | 1 | No |
| **Excluded subtotal** | | **139 / 474 (29.3%)** | |

**`implements` (from `INTF`) is already included in `CALL_LIKE_EDGE_KINDS`.** The
brief's specific concern — that a class/interface relationship might currently be
excluded from graph-proximity scoring — does not apply; this was already handled
correctly by the real-format update (`graph_loader.py:63-71`, `EDGE_KIND_MAP["INTF"] =
"implements"`, and `"implements"` is in the `CALL_LIKE_EDGE_KINDS` tuple). No gap here.

**Verdict: (a) — fine as-is.** The two largest excluded categories, `MESS` (36,
message-class references) and `STRU` (35, structure references), are legitimately
different kinds of relationship from a call/include/interface edge — an object raising
the same message class as another, or using the same local structure type, is a weaker
and noisier signal of "these objects belong together" than an actual call or
inheritance relationship. `STRU` in particular is already addressed as its own
candidate gap under check 2 (as a table-sharing-bonus widening candidate, not a
call-graph-proximity one) — the two checks point at the same underlying data but
recommend different, non-overlapping fixes if either is pursued.

## Go/no-go recommendation

**Go — step 3 is ready to feed step 4 with two flagged, non-blocking findings and one
already-tracked pattern reinforced.**

- Check 1: correct on all 3 clear-mapping requirements, correctly ambiguous on the
  graph/path requirement, correctly flagged all 3 nonsense requirements once tested in
  isolation. One ranking miss (R5) is the same already-tracked pattern as
  [ISSUES.md #1](../ISSUES.md) — recommend logging it there as a second occurrence.
- Check 2 (table-sharing bonus): **(c)** — confirmed dead on real data, but its absence
  doesn't currently produce a wrong answer in any of the 8 requirements tested, since
  call-graph proximity alone was sufficient in every case. Recommend deciding on
  widening it to `STRU` before a real requirement is found where table-sharing is the
  *only* available structural signal — but that hasn't happened yet.
- Check 3 (embedding benchmark): **(c)**, but for step 3's own architecture, not a
  blocker — the 0.30/0.70 weighting was already chosen assuming semantic similarity
  alone is weak, and this confirms rather than contradicts that assumption.
- Check 4 (seed-circularity floor): **(b)** — holds on every case tried, margin is
  thinner than desirable, worth re-checking on a second real package when one exists.
  Not urgent.
- Check 5 (`CALL_LIKE_EDGE_KINDS`): **(a)** — no gap; `implements` was already handled
  correctly by the real-format update.

Nothing found here requires reopening step 3's scoring before step 4 starts. The two
"(c)" items are real design limitations worth carrying forward as known context into
step 4's design (e.g., step 4 should not assume the table-sharing bonus is a reliable
signal on real packages), not defects requiring a step 3 rework first.
