# Step 3 scoring fix — seed self-membership circularity

## The bug

`base_score = 0.30 * semantic_score + 0.70 * structural_score`, and the top-3 objects by
`semantic_score` were used, unconditionally, as the seed set for computing
`structural_score` — with any object *in* that set automatically scoring
`structural_score = 1.0` via self-membership. That guarantees
`base_score >= 0.70 * 1.0 = 0.70` for the top-ranked object regardless of how weak its
actual semantic match is, since being "in the top 3" says nothing about whether any of
those 3 are actually relevant — only that they were the least-bad of 15 candidates.

Confirmed empirically before any fix, using three deliberately nonsensical requirements:

| Requirement | Top candidate | base_score |
|---|---|---|
| "asdkj alksjd laksjd laksjdlk ajsdlkaj sdlkaj sdlkj" | `ZCL_PRICING_ENGINE` | 0.871 |
| "xyzzy plugh frobnicate qux wibble wobble" | `ZCL_NOTIFICATION_SENDER` | 0.874 |
| "Migrate the mainframe COBOL payroll batch job to a quantum computing cluster" | `ZCL_NOTIFICATION_SENDER` | 0.885 |

All three sit far above the `likely_new_object` floor of 0.35 — the flag was
mathematically unreachable on this dataset, for any input, because the top-ranked
object always lands in the guaranteed-≥0.70 band.

## Phase 1 — calibration

Raw, pre-structural `semantic_score` (max cosine similarity across an object's
non-trivial chunks, normalized 0–1) for the top-ranked candidate, on the same three
nonsense requirements plus 5 requirements independently derived from known object
behavior in `ZORDER_MGMT_extract.txt` (not guessed — each maps to a specific method or
table already read from the file: credit-limit checking, shipping cost, stock
reservation, order numbering, discount tiering):

| Requirement | Top candidate | raw semantic_score |
|---|---|---|
| *(nonsense)* keyboard mash | `ZCL_PRICING_ENGINE` | 0.5701 |
| *(nonsense)* random words | `ZCL_NOTIFICATION_SENDER` | 0.5786 |
| *(nonsense)* COBOL/quantum | `ZCL_NOTIFICATION_SENDER` | 0.6175 |
| Credit limit check → `ZCL_ORDER_VALIDATOR` | `ZCL_ORDER_VALIDATOR` ✓ | 0.7407 |
| Shipping cost → `ZCL_SHIPPING_CALCULATOR` | `ZCL_SHIPPING_CALCULATOR` ✓ | 0.7941 |
| Stock reservation → `ZCL_STOCK_MANAGER` | `ZCL_STOCK_MANAGER` ✓ | 0.7286 |
| Order numbering → `ZFM_ORDER_NUMBER_RANGE` | `ZCL_ORDER_PROCESSOR` ✗ (see note) | 0.7335 |
| Discount tiering → `ZCL_DISCOUNT_CALCULATOR` | `ZCL_DISCOUNT_CALCULATOR` ✓ | 0.7755 |

**Nonsense scores: 0.5701 – 0.6175. Genuine scores: 0.7286 – 0.7941.** A clean gap of
~0.11 with **zero overlap** — meaningfully better separation than the chunk-to-chunk
benchmark found (which showed 46–49% overlap). This is enough of a gap to support a
fix with an explicit floor rather than papering over the bug.

One honest caveat surfaced in this table: the "order numbering" requirement's top pick
was `ZCL_ORDER_PROCESSOR`, not the expected `ZFM_ORDER_NUMBER_RANGE` — a top-1 identity
miss, likely the same weak-semantic-signal pattern from the original investigation
showing up again, just not severe enough here to break score-level separation. Its
score (0.7335) still lands cleanly in the genuine cluster, so it doesn't affect this
calibration, but it's a reminder that a good *score* doesn't guarantee the *correct*
top pick — structural signal (which still dominates the final blend at 0.70 weight)
is doing real work to compensate for that, not this floor.

## Phase 2 — fix

Added `SEED_MIN_SEMANTIC_SCORE = 0.67` (midpoint of the calibration gap) as an
additional gate on seed eligibility in [`lld_step2/retrieval.py`](../lld_step2/retrieval.py):
an object must be in the top 3 by `semantic_score` **and** clear this floor to become a
trusted seed. If none clear it, the seed set is empty, `structural_score` is 0 for
every object (already the natural behavior of `compute_structural_scores` given an
empty seed set — no code branch needed there), and `base_score` collapses to
`0.30 * semantic_score`, correctly falling below the `likely_new_object` floor for a
genuinely unrelated requirement.

Regression check, same 8 requirements, after the fix:

| Requirement | likely_new_object | top base_score |
|---|---|---|
| *(nonsense)* keyboard mash | **True** | 0.171 |
| *(nonsense)* random words | **True** | 0.174 |
| *(nonsense)* COBOL/quantum | **True** | 0.185 |
| Credit limit check | **False** | 0.922 (top: `ZCL_ORDER_VALIDATOR`, correct) |
| Shipping cost | **False** | 0.938 (top: `ZCL_SHIPPING_CALCULATOR`, correct) |
| Stock reservation | **False** | 0.919 (top: `ZCL_STOCK_MANAGER`, correct) |
| Order numbering | **False** | 0.920 (top: `ZCL_ORDER_PROCESSOR`, same identity miss as phase 1, unrelated to this fix) |
| Discount tiering | **False** | 0.933 (top: `ZCL_DISCOUNT_CALCULATOR`, correct) |

All 3 nonsense cases now correctly flag `likely_new_object = True`; all 5 genuine cases
correctly stay `False`. No regression in the opposite direction.

As a side effect, the fix also improved general scoring quality beyond just the
new-object flag: previously, any object landing in the raw top-3 by semantic score got
an unconditional 1.0 structural credit even when its match was weak, artificially
inflating objects that only vaguely resembled the requirement. Post-fix, e.g. for the
"purge old log entries" requirement, only `ZCL_ORDER_LOGGER` (semantic 0.806) clears the
floor and becomes the sole trusted seed; `ZRP_ORDER_BATCH_RUN` and `ZCL_STOCK_MANAGER`
(previously auto-boosted to structural=1.0 just for being top-3) now correctly show
structural=0.6, reflecting their real 1-hop relationship to the actual seed rather than
a coincidence of being the least-bad of the remaining semantic scores.

## Verdict

Phase 1's numbers supported a clean fix — the two groups didn't overlap, so this isn't
the same problem the embedding-chunk benchmark found (that was pairwise chunk-to-chunk
similarity with no aggregation; this is max-similarity aggregated to object level, with
a full sentence-length requirement compared against a full sentence-length chunk,
which is a more favorable comparison for the model). **Step 3 is not blocked by this
finding** — the fix is in place and regression-tested in both directions.

Caveat carried forward: this is 8 data points on one small, densely-connected package.
Re-run this same nonsense-vs-genuine calibration (not just the chunk-pair benchmark)
once a larger real extraction exists, the same way `SEMANTIC_WEIGHT`/`STRUCTURAL_WEIGHT`
need re-validation — `SEED_MIN_SEMANTIC_SCORE`'s comment in the code points back here.
