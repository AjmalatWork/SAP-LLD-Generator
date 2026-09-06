"""Step 3: retrieval + tiering.

Given a plain-English business requirement (plus an optional developer scoping
note), queries the step-2 graph (`object_calls`, `object_uses_table`) and
vector store (`code_chunks`) and produces a small, ranked, explainable
candidate set of objects — the input to a later LLM-reasoning step (step 4,
not built here).

Does not modify the step-2 schema or embedding model. Does not call an LLM
anywhere, including for reranking — every score here is deterministic and
traceable to its inputs.

======================================================================
TUNABLE CONSTANTS — the only place these should live. See the docstring on
each constant for what moving it does. All of the numbers below come from
`lld_step2/embedding_bench.py`'s investigation into this project's embedding
model quality (see reports/embedding_investigation_report.md), not from a
first-principles ideal weighting — re-run that benchmark against the first
larger, real extraction and revisit these before trusting them long-term.
======================================================================
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import numpy as np
import psycopg
from sentence_transformers import SentenceTransformer

from .config import load_config
from .db import connect
from .graph_loader import (
    CALL_LIKE_EDGE_KINDS,
    get_direct_callees,
    get_direct_callers,
    get_tables_used,
)

# --- Semantic vs. structural blend ------------------------------------------
# The embedding-quality investigation (lld_step2/embedding_bench.py) found the
# current general-purpose embedding model shows only negligible separation
# (Cohen's d ~0.10-0.12) between graph-confirmed related and unrelated chunk
# pairs on ZORDER_MGMT_extract.txt — ~46-49% of unrelated pairs scored as
# close as the median related pair. That benchmark was run against a small
# (15-object), unusually densely-connected package; treat this split as a
# strong evidence-based starting point, not a final answer, and re-run the
# benchmark against the first larger real extraction before trusting it.
SEMANTIC_WEIGHT = 0.30
STRUCTURAL_WEIGHT = 0.70

# Minimum chunk length (characters) to consider for semantic scoring. Filters
# out the chunking artifact found during the investigation: near-identical
# trivial chunks (e.g. a lone "ENDCLASS." line) that embed to near-zero
# distance from each other across unrelated classes, which would otherwise
# falsely inflate semantic_score. Value matches the threshold already
# validated in embedding_bench.py's --min-chunk-length. Fixing the chunker
# itself is step 2's job, out of scope here.
MIN_CHUNK_LENGTH = 15

# --- Structural scoring ------------------------------------------------------
# Number of top-semantic-scoring objects used to seed the structural search.
# Semantic ranking alone isn't trustworthy (see above), but it's still the
# only mechanism connecting free-text requirement language to a starting
# point in the graph at all — structural proximity then re-ranks from there.
TOP_SEED_OBJECTS = 3

# BUG FOUND DURING TESTING, FIXED HERE: being in the top-3 by semantic_score
# used to unconditionally grant that object structural_score = 1.0
# (self-membership), which meant base_score >= STRUCTURAL_WEIGHT (0.70) for
# the top-ranked object on ANY input, however weak its real semantic match --
# confirmed empirically with three nonsense requirements (a keyboard mash,
# random words, and "migrate mainframe COBOL to quantum computing"), each of
# which still produced a top base_score around 0.87-0.89. This made
# likely_new_object mathematically unreachable: no input could ever fall
# below its 0.35 floor.
#
# Fix: an object may only enter the trusted semantic seed set (and so become
# eligible for structural_score = 1.0 via self-membership) if its raw
# semantic_score also clears this separate, absolute floor. This is an
# additional gate on top of the top-3 selection, not a replacement for it.
# If no object clears the floor, the seed set is empty and structural_score
# is 0 for everyone (see compute_structural_scores) -- base_score then
# collapses to a pure semantic signal, which correctly falls below the
# likely_new_object floor for a genuinely unrelated requirement.
#
# Value calibrated against ZORDER_MGMT_extract.txt: raw top-candidate
# semantic_score for 3 nonsense requirements topped out at 0.6175, while 5
# requirements independently derived from known object behavior (credit
# limit check, shipping cost, stock reservation, discount tier, order
# numbering) bottomed out at 0.7286 -- a clean, non-overlapping gap. 0.67
# sits roughly at the midpoint of that gap. Small sample (8 data points) on
# one small package -- re-calibrate the same way (see the nonsense/genuine
# split above) once a larger real extraction exists, the same caveat as the
# 0.30/0.70 weighting itself.
SEED_MIN_SEMANTIC_SCORE = 0.67

STRUCTURAL_SCORE_IN_SEED_SET = 1.0
STRUCTURAL_SCORE_1_HOP = 0.6
STRUCTURAL_SCORE_2_HOP = 0.3
STRUCTURAL_SCORE_NO_PATH = 0.0
# Awarded (as a max, not summed, with the hop-distance score) if an object
# shares a DDIC table with any seed object — table sharing is a strong
# relevance signal in ABAP even absent a direct call.
STRUCTURAL_SCORE_SHARED_TABLE = 0.4

# --- Confidence tier adjustment ---------------------------------------------
# "certain" is a hard filter (no multiplier): the candidate set is restricted
# to the named object(s) plus everything within this many hops, bidirectional.
CERTAIN_HOP_RADIUS = 1

LIKELY_NAMED_MULTIPLIER = 1.5
LIKELY_HOP1_MULTIPLIER = 1.2
UNSURE_NAMED_MULTIPLIER = 1.1

# --- Disagreement detection --------------------------------------------------
# How much higher the top independent (unadjusted) base_score must be than
# the best in-filter object's base_score, under a "certain" note, before it's
# flagged as the hard filter likely hiding the real answer.
CERTAIN_CONFLICT_MARGIN = 0.15

# --- New-object detection ---------------------------------------------------
# Below this blended base_score, the top candidate is considered too weak to
# trust as an anchor (applies to the blended score, not raw semantic score
# alone — see brief: a low blended score means both signals failed to find
# anything, which is meaningful, unlike a low semantic score in isolation).
NEW_OBJECT_SCORE_FLOOR = 0.35
# Below this gap between the top and 5th candidate's base_score, nothing
# stands out and retrieval is effectively returning noise.
NEW_OBJECT_SPREAD_THRESHOLD = 0.05

DEFAULT_TOP_N = 5
UNADJUSTED_RANKING_SIZE = 5

Confidence = Literal["certain", "likely", "unsure"]


# =============================================================================
# Data model
# =============================================================================


@dataclass
class ChunkMatch:
    chunk_index: int
    chunk_text: str
    similarity: float  # normalized 0-1, higher = more similar


@dataclass
class Candidate:
    object_name: str
    type: str
    package: str
    final_score: float
    semantic_score: float
    structural_score: float
    base_score: float
    tier_adjustment: str
    top_chunks: list[ChunkMatch]
    callers: list[str]
    callees: list[str]
    tables_used: list[dict]


@dataclass
class Disagreement:
    kind: str  # "certain_hint_conflict" | "likely_override" | "unsure_override" | "unresolvable_scoping_note"
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class RetrievalResult:
    candidates: list[Candidate]
    disagreements: list[Disagreement]
    likely_new_object: bool
    likely_new_object_reason: str | None
    unadjusted_ranking: list[Candidate]
    query_metadata: dict


# =============================================================================
# Graph helpers (bidirectional adjacency + table-sharing, built once per call)
# =============================================================================


def _fetch_all_objects(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT name, type, package FROM objects ORDER BY name")
        return [{"name": r[0], "type": r[1], "package": r[2]} for r in cur.fetchall()]


def _fetch_bidirectional_adjacency(conn: psycopg.Connection) -> dict[str, set[str]]:
    """Undirected adjacency over `object_calls`, restricted to edges between
    two known objects (external/unresolved call targets have no row in
    `objects` and are excluded here, since they can't be traversed to), and
    to CALL_LIKE_EDGE_KINDS specifically - the real extraction format loads
    every dependency type (including DDIC/message/transaction references)
    into object_calls, and the step 3 brief's "1 hop (calls or is called
    by)" means genuine calls, not any shared reference. Preserves existing
    structural-scoring behavior unchanged rather than silently widening it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT oc.source_object, oc.target_object
            FROM object_calls oc
            JOIN objects o1 ON o1.name = oc.source_object
            JOIN objects o2 ON o2.name = oc.target_object
            WHERE oc.edge_kind = ANY(%(edge_kinds)s)
            """,
            {"edge_kinds": list(CALL_LIKE_EDGE_KINDS)},
        )
        rows = cur.fetchall()
    adjacency: dict[str, set[str]] = {}
    for a, b in rows:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    return adjacency


def _fetch_object_tables(conn: psycopg.Connection) -> dict[str, set[str]]:
    """Object -> set of DDIC *table* names it uses. Scoped to ddic_type =
    'TABL' specifically (not DOMA/DTEL/TTYP, which object_uses_table can
    also hold now) to preserve the step 3 brief's literal "shares a DDIC
    table" bonus criterion unchanged - broadening it to any DDIC reference
    would be a scoring-behavior change, out of scope for this update.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object_name, ddic_object_name FROM object_uses_table WHERE ddic_type = 'TABL'"
        )
        rows = cur.fetchall()
    tables: dict[str, set[str]] = {}
    for object_name, table_name in rows:
        tables.setdefault(object_name, set()).add(table_name)
    return tables


def _bfs_hop_distances(
    adjacency: dict[str, set[str]], seeds: set[str], max_depth: int
) -> dict[str, int]:
    """Multi-source BFS. Returns {object: hop_distance} for every object
    reached within `max_depth` hops of any seed, seeds themselves at 0.
    """
    dist: dict[str, int] = {s: 0 for s in seeds}
    frontier = set(seeds)
    for depth in range(1, max_depth + 1):
        next_frontier: set[str] = set()
        for node in frontier:
            for neighbor in adjacency.get(node, ()):
                if neighbor not in dist:
                    dist[neighbor] = depth
                    next_frontier.add(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    return dist


# =============================================================================
# Scoping note resolution
# =============================================================================


def resolve_scoping_note(conn: psycopg.Connection, scoping_note: str | None) -> list[str]:
    """Resolves a free-text scoping note to known object names: exact match
    first, then case-insensitive full match, then substring match (in either
    direction). Returns [] if nothing resolves — callers must not treat that
    as an error (see brief: unresolved notes are useful signal, not failure).
    """
    if not scoping_note:
        return []

    with conn.cursor() as cur:
        cur.execute("SELECT name FROM objects")
        all_names = [r[0] for r in cur.fetchall()]

    note = scoping_note.strip()

    exact = [n for n in all_names if n == note]
    if exact:
        return sorted(set(exact))

    note_lower = note.lower()
    case_insensitive = [n for n in all_names if n.lower() == note_lower]
    if case_insensitive:
        return sorted(set(case_insensitive))

    substring = [n for n in all_names if n.lower() in note_lower or note_lower in n.lower()]
    return sorted(set(substring))


# =============================================================================
# Semantic scoring
# =============================================================================


def embed_requirement(text: str, model_name: str) -> np.ndarray:
    """Embeds requirement text with the SAME model/settings used to embed
    the stored chunks (see lld_step2/embedding.py) — otherwise the cosine
    similarity between the two is meaningless.
    """
    model = SentenceTransformer(model_name)
    vec = model.encode([text], show_progress_bar=False, normalize_embeddings=True)[0]
    return np.asarray(vec, dtype=np.float64)


def _parse_vector_text(text: str) -> np.ndarray:
    return np.array([float(x) for x in text.strip("[]").split(",")], dtype=np.float64)


def compute_semantic_scores(
    conn: psycopg.Connection,
    requirement_vector: np.ndarray,
    all_object_names: list[str],
    min_chunk_length: int = MIN_CHUNK_LENGTH,
) -> dict[str, dict]:
    """Returns {object_name: {"score": float 0-1, "chunks": [ChunkMatch, ...]}}
    for every object, using max cosine similarity across that object's
    non-trivial chunks (see MIN_CHUNK_LENGTH), normalized from [-1,1] to
    [0,1]. Objects with no surviving chunks get score 0.0 and an empty list.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object_name, chunk_index, chunk_text, embedding::text "
            "FROM code_chunks WHERE length(chunk_text) >= %s",
            (min_chunk_length,),
        )
        rows = cur.fetchall()

    per_object: dict[str, list[ChunkMatch]] = {name: [] for name in all_object_names}
    for object_name, chunk_index, chunk_text, embedding_text in rows:
        chunk_vec = _parse_vector_text(embedding_text)
        raw_sim = float(np.dot(chunk_vec, requirement_vector))
        normalized_sim = (raw_sim + 1.0) / 2.0
        per_object.setdefault(object_name, []).append(
            ChunkMatch(chunk_index=chunk_index, chunk_text=chunk_text, similarity=normalized_sim)
        )

    result = {}
    for name in all_object_names:
        chunks = sorted(per_object.get(name, []), key=lambda c: -c.similarity)
        score = chunks[0].similarity if chunks else 0.0
        result[name] = {"score": score, "chunks": chunks}
    return result


# =============================================================================
# Structural scoring
# =============================================================================


def compute_structural_scores(
    adjacency: dict[str, set[str]],
    tables_by_object: dict[str, set[str]],
    all_object_names: list[str],
    seed_objects: list[str],
) -> dict[str, float]:
    seed_set = set(seed_objects)
    hop_distances = _bfs_hop_distances(adjacency, seed_set, max_depth=2)
    seed_tables: set[str] = set()
    for s in seed_objects:
        seed_tables |= tables_by_object.get(s, set())

    scores = {}
    for name in all_object_names:
        if name in seed_set:
            hop_score = STRUCTURAL_SCORE_IN_SEED_SET
        else:
            d = hop_distances.get(name)
            hop_score = {1: STRUCTURAL_SCORE_1_HOP, 2: STRUCTURAL_SCORE_2_HOP}.get(
                d, STRUCTURAL_SCORE_NO_PATH
            )
        shares_table = bool(tables_by_object.get(name, set()) & seed_tables)
        table_score = STRUCTURAL_SCORE_SHARED_TABLE if shares_table else 0.0
        scores[name] = max(hop_score, table_score)
    return scores


# =============================================================================
# retrieve()
# =============================================================================


def retrieve(
    requirement: str,
    scoping_note: str | None = None,
    confidence: Confidence | None = None,
    top_n: int = DEFAULT_TOP_N,
    conn: psycopg.Connection | None = None,
) -> RetrievalResult:
    if scoping_note and confidence is None:
        raise ValueError("confidence is required when scoping_note is given")
    if not scoping_note and confidence is not None:
        raise ValueError("confidence must be omitted when scoping_note is not given")

    config = load_config()
    owns_conn = conn is None
    if conn is None:
        conn = connect(config)

    try:
        objects = _fetch_all_objects(conn)
        object_names = [o["name"] for o in objects]
        objects_by_name = {o["name"]: o for o in objects}

        adjacency = _fetch_bidirectional_adjacency(conn)
        tables_by_object = _fetch_object_tables(conn)

        requirement_vector = embed_requirement(requirement, config.embedding_model)
        semantic = compute_semantic_scores(conn, requirement_vector, object_names)

        top3_by_semantic = sorted(object_names, key=lambda n: (-semantic[n]["score"], n))[
            :TOP_SEED_OBJECTS
        ]
        # Gate: only objects that also clear SEED_MIN_SEMANTIC_SCORE become
        # trusted seeds eligible for structural_score = 1.0. See the comment
        # on SEED_MIN_SEMANTIC_SCORE for why this gate exists.
        seed_objects = [n for n in top3_by_semantic if semantic[n]["score"] >= SEED_MIN_SEMANTIC_SCORE]
        structural = compute_structural_scores(
            adjacency, tables_by_object, object_names, seed_objects
        )

        base_scores = {
            name: SEMANTIC_WEIGHT * semantic[name]["score"] + STRUCTURAL_WEIGHT * structural[name]
            for name in object_names
        }

        # --- scoping note resolution ---
        disagreements: list[Disagreement] = []
        named_objects = resolve_scoping_note(conn, scoping_note) if scoping_note else []
        unresolved_note = bool(scoping_note) and not named_objects
        if unresolved_note:
            disagreements.append(
                Disagreement(
                    kind="unresolvable_scoping_note",
                    message=(
                        f"Scoping note {scoping_note!r} did not match any known object "
                        "(checked exact, case-insensitive, and substring match). This may "
                        "mean the developer has an object in mind that is outside the "
                        "extracted package."
                    ),
                    details={"scoping_note": scoping_note},
                )
            )
        # A note that failed to resolve gets no tier adjustment (nothing to
        # anchor to) but is not treated as an error.
        effective_confidence = confidence if named_objects else None

        # Exactly 1 hop, used by the "likely" tier's hop-1 multiplier (the
        # brief specifies that boost radius literally, independent of the
        # separately-configurable certain-filter radius below).
        named_hop1: set[str] = set()
        for n in named_objects:
            named_hop1 |= adjacency.get(n, set())
        named_hop1 -= set(named_objects)

        # Certain-filter radius is its own configurable constant (may differ
        # from the "likely" tier's fixed 1-hop boost above).
        named_certain_neighbors = set(
            _bfs_hop_distances(adjacency, set(named_objects), CERTAIN_HOP_RADIUS)
        ) - set(named_objects)

        # --- unadjusted ranking (pure base_score, always top 5) ---
        unadjusted_sorted = sorted(object_names, key=lambda n: (-base_scores[n], n))
        unadjusted_top_names = unadjusted_sorted[:UNADJUSTED_RANKING_SIZE]

        # --- tier adjustment ---
        final_scores: dict[str, float] = {}
        tier_labels: dict[str, str] = {}
        allowed: set[str] | None = None

        if effective_confidence == "certain":
            allowed = set(named_objects) | named_certain_neighbors
            for name in object_names:
                final_scores[name] = base_scores[name]
                if name in named_objects:
                    tier_labels[name] = "certain: in filter (named object)"
                elif name in named_certain_neighbors:
                    tier_labels[name] = f"certain: in filter (within {CERTAIN_HOP_RADIUS} hop(s) of named)"
                else:
                    tier_labels[name] = "certain: excluded (outside filter)"
        elif effective_confidence == "likely":
            for name in object_names:
                if name in named_objects:
                    final_scores[name] = base_scores[name] * LIKELY_NAMED_MULTIPLIER
                    tier_labels[name] = f"likely: x{LIKELY_NAMED_MULTIPLIER} (named object)"
                elif name in named_hop1:
                    final_scores[name] = base_scores[name] * LIKELY_HOP1_MULTIPLIER
                    tier_labels[name] = f"likely: x{LIKELY_HOP1_MULTIPLIER} (1-hop of named)"
                else:
                    final_scores[name] = base_scores[name]
                    tier_labels[name] = "likely: no boost"
        elif effective_confidence == "unsure":
            for name in object_names:
                if name in named_objects:
                    final_scores[name] = base_scores[name] * UNSURE_NAMED_MULTIPLIER
                    tier_labels[name] = f"unsure: x{UNSURE_NAMED_MULTIPLIER} (named object)"
                else:
                    final_scores[name] = base_scores[name]
                    tier_labels[name] = "unsure: no boost"
        else:
            for name in object_names:
                final_scores[name] = base_scores[name]
                tier_labels[name] = "none"

        candidate_universe = (
            [n for n in object_names if n in allowed] if allowed is not None else list(object_names)
        )
        adjusted_sorted = sorted(candidate_universe, key=lambda n: (-final_scores[n], n))
        adjusted_top_names = adjusted_sorted[:top_n]

        # --- disagreement: certain-hint conflict ---
        if effective_confidence == "certain" and allowed is not None:
            in_filter_base_scores = [base_scores[n] for n in object_names if n in allowed]
            best_in_filter = max(in_filter_base_scores, default=0.0)
            top_unadjusted = unadjusted_top_names[0] if unadjusted_top_names else None
            if top_unadjusted and top_unadjusted not in allowed:
                gap = base_scores[top_unadjusted] - best_in_filter
                if gap > CERTAIN_CONFLICT_MARGIN:
                    disagreements.append(
                        Disagreement(
                            kind="certain_hint_conflict",
                            message=(
                                f"Certain scoping note restricts results to {sorted(allowed)}, "
                                f"but '{top_unadjusted}' (base_score={base_scores[top_unadjusted]:.3f}) "
                                f"scores {gap:.3f} higher than the best in-filter object "
                                f"(base_score={best_in_filter:.3f}). The hard filter may be hiding "
                                "the real answer."
                            ),
                            details={
                                "top_unadjusted": top_unadjusted,
                                "top_unadjusted_base_score": base_scores[top_unadjusted],
                                "best_in_filter_base_score": best_in_filter,
                                "allowed": sorted(allowed),
                            },
                        )
                    )

        # --- disagreement: likely/unsure override ---
        if effective_confidence in ("likely", "unsure"):
            top_adjusted = adjusted_top_names[0] if adjusted_top_names else None
            top_unadjusted = unadjusted_top_names[0] if unadjusted_top_names else None
            if top_adjusted != top_unadjusted:
                disagreements.append(
                    Disagreement(
                        kind=f"{effective_confidence}_override",
                        message=(
                            f"With the {effective_confidence} scoping hint applied, the top result "
                            f"is '{top_adjusted}' (final_score={final_scores[top_adjusted]:.3f}), but "
                            f"the top independent match without the hint is '{top_unadjusted}' "
                            f"(base_score={base_scores[top_unadjusted]:.3f}). The hint changed the "
                            "top rank."
                        ),
                        details={
                            "top_adjusted": top_adjusted,
                            "top_adjusted_final_score": final_scores[top_adjusted],
                            "top_unadjusted": top_unadjusted,
                            "top_unadjusted_base_score": base_scores[top_unadjusted],
                        },
                    )
                )

        # --- new-object detection (uses base_score, not final_score) ---
        ranking_for_new_check = adjusted_sorted
        likely_new_object = False
        new_object_reason = None
        if ranking_for_new_check:
            top_base = base_scores[ranking_for_new_check[0]]
            fifth_index = min(4, len(ranking_for_new_check) - 1)
            fifth_base = base_scores[ranking_for_new_check[fifth_index]]
            spread = top_base - fifth_base
            if top_base < NEW_OBJECT_SCORE_FLOOR:
                likely_new_object = True
                new_object_reason = (
                    f"Top candidate's base_score ({top_base:.3f}) is below the floor "
                    f"({NEW_OBJECT_SCORE_FLOOR})."
                )
            elif len(ranking_for_new_check) >= 2 and spread < NEW_OBJECT_SPREAD_THRESHOLD:
                likely_new_object = True
                new_object_reason = (
                    f"Spread between top and #{fifth_index + 1} candidate base_score "
                    f"({spread:.3f}) is below threshold ({NEW_OBJECT_SPREAD_THRESHOLD}) - "
                    "nothing stands out."
                )
        else:
            likely_new_object = True
            new_object_reason = "No candidates available at all."

        def _build_candidate(name: str, score: float, tier_label: str) -> Candidate:
            obj = objects_by_name[name]
            top_chunks = semantic[name]["chunks"][:3]
            return Candidate(
                object_name=name,
                type=obj["type"],
                package=obj["package"],
                final_score=score,
                semantic_score=semantic[name]["score"],
                structural_score=structural[name],
                base_score=base_scores[name],
                tier_adjustment=tier_label,
                top_chunks=top_chunks,
                callers=get_direct_callers(conn, name),
                callees=get_direct_callees(conn, name),
                tables_used=get_tables_used(conn, name),
            )

        candidates = [
            _build_candidate(name, final_scores[name], tier_labels[name])
            for name in adjusted_top_names
        ]
        unadjusted_ranking = [
            _build_candidate(name, base_scores[name], "unadjusted (pure base_score)")
            for name in unadjusted_top_names
        ]

        query_metadata = {
            "requirement": requirement,
            "scoping_note": scoping_note,
            "confidence": confidence,
            "resolved_named_objects": named_objects,
            "top_n": top_n,
            "constants": {
                "semantic_weight": SEMANTIC_WEIGHT,
                "structural_weight": STRUCTURAL_WEIGHT,
                "min_chunk_length": MIN_CHUNK_LENGTH,
                "top_seed_objects": TOP_SEED_OBJECTS,
                "seed_min_semantic_score": SEED_MIN_SEMANTIC_SCORE,
                "structural_score_in_seed_set": STRUCTURAL_SCORE_IN_SEED_SET,
                "structural_score_1_hop": STRUCTURAL_SCORE_1_HOP,
                "structural_score_2_hop": STRUCTURAL_SCORE_2_HOP,
                "structural_score_shared_table": STRUCTURAL_SCORE_SHARED_TABLE,
                "certain_hop_radius": CERTAIN_HOP_RADIUS,
                "likely_named_multiplier": LIKELY_NAMED_MULTIPLIER,
                "likely_hop1_multiplier": LIKELY_HOP1_MULTIPLIER,
                "unsure_named_multiplier": UNSURE_NAMED_MULTIPLIER,
                "certain_conflict_margin": CERTAIN_CONFLICT_MARGIN,
                "new_object_score_floor": NEW_OBJECT_SCORE_FLOOR,
                "new_object_spread_threshold": NEW_OBJECT_SPREAD_THRESHOLD,
                "embedding_model": config.embedding_model,
            },
            "top3_semantic_candidates": top3_by_semantic,
            "seed_objects": seed_objects,
        }

        return RetrievalResult(
            candidates=candidates,
            disagreements=disagreements,
            likely_new_object=likely_new_object,
            likely_new_object_reason=new_object_reason,
            unadjusted_ranking=unadjusted_ranking,
            query_metadata=query_metadata,
        )
    finally:
        if owns_conn:
            conn.close()


# =============================================================================
# Export to extraction-file format
# =============================================================================

_METADATA_START = "=== RETRIEVAL METADATA ==="
_METADATA_END = "=== END RETRIEVAL METADATA ==="


def _build_metadata_block(result: RetrievalResult) -> str:
    meta = result.query_metadata
    lines = [_METADATA_START]
    lines.append(f"REQUIREMENT: {meta['requirement']}")
    lines.append(f"SCOPING_NOTE: {meta['scoping_note'] or '(none)'}")
    lines.append(f"CONFIDENCE: {meta['confidence'] or '(none)'}")
    lines.append(f"LIKELY_NEW_OBJECT: {result.likely_new_object}")
    lines.append(f"LIKELY_NEW_OBJECT_REASON: {result.likely_new_object_reason or '(none)'}")
    lines.append(f"DISAGREEMENTS: {len(result.disagreements)}")
    for d in result.disagreements:
        lines.append(f"  - [{d.kind}] {d.message}")
    lines.append(_METADATA_END)
    return "\n".join(lines)


# Inverse of step 1's LLD-level -> dependency-vocabulary mapping
# (ZLLD_PACKAGE_EXTRACTOR's EXTRACT_LLD_OBJECT: PROG->PROGRAM, CLAS->CLASS,
# FUNC->FUNCTION_MODULE) - needed because the real DEPENDENCIES JSON's outer
# entry uses the short dependency-type code for the object itself, not the
# LLD-level type name.
_LLD_TYPE_TO_DEPENDENCY_CODE = {"CLASS": "CLAS", "PROGRAM": "PROG", "FUNCTION_MODULE": "FUNC"}


def _build_real_dependencies_json(conn: psycopg.Connection, candidate: Candidate) -> str:
    """Reconstructs a real-shape DEPENDENCIES blob (matching what step 1's
    extraction actually produces) from this object's stored object_calls
    rows, so the exported file is parseable by the same parser that reads a
    real extraction file - not a separate, simplified shape.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT target_object, dependency_type, signature_json "
            "FROM object_calls WHERE source_object = %s ORDER BY target_object",
            (candidate.object_name,),
        )
        rows = cur.fetchall()

    dependencies = []
    for target_object, dependency_type, signature_json in rows:
        entry = {"TYPE": dependency_type, "NAME": target_object}
        if signature_json:
            entry["SIGNATURE"] = signature_json
        dependencies.append(entry)

    outer = [
        {
            "TYPE": _LLD_TYPE_TO_DEPENDENCY_CODE.get(candidate.type, candidate.type),
            "NAME": candidate.object_name,
            "DEPENDENCIES": dependencies,
        }
    ]
    return json.dumps(outer)


def export_candidates_to_file(
    result: RetrievalResult, path: str, conn: psycopg.Connection | None = None
) -> None:
    """Serializes the retrieval result to a text file in the same real
    format lld_step2.parser expects (file header + shared DDIC section +
    real per-object DEPENDENCIES shape), with a clearly-delimited metadata
    header (disagreements + likely_new_object) in front of everything else.
    Only the objects in `result.candidates` are included — this is a small,
    pre-filtered file for a later LLM step, not a re-export of the whole
    package.

    The shared --- DDIC --- section here is intentionally minimal
    (`{"PACKAGES": []}`) - full DDIC field-level detail for any TABL/DTEL/
    etc. references is already resolvable from the database by name if
    step 4 needs it later; duplicating it into every export is unnecessary.
    RUN_TYPE is set to INCREMENTAL since this is inherently a subset of a
    package, never meant to be re-loaded into step 2's own database (this
    file is for step 4's consumption, not a real sync).

    Note: `code_chunks` only stores per-chunk text, not the object's whole
    original source, so the SOURCE section here is reconstructed by joining
    that object's chunks back together in chunk_index order (this is why a
    connection is needed even though `result` already carries top-matching
    chunks per candidate — those are a filtered subset, not the full object).
    """
    config = load_config()
    owns_conn = conn is None
    if conn is None:
        conn = connect(config)

    try:
        package = result.candidates[0].package if result.candidates else "UNKNOWN"

        header_lines = [
            "RUN_TYPE: INCREMENTAL",
            f"PACKAGE: {package}",
            f"EXTRACTED_AT: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
            "REMOVED_OBJECTS: NONE",
            "",
            "--- DDIC ---",
            json.dumps({"PACKAGES": []}),
        ]

        blocks = [_build_metadata_block(result), "\n".join(header_lines)]

        for candidate in result.candidates:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT chunk_text FROM code_chunks WHERE object_name = %s ORDER BY chunk_index",
                    (candidate.object_name,),
                )
                chunk_texts = [r[0] for r in cur.fetchall()]
            source = "\n\n".join(chunk_texts)

            dep_json = _build_real_dependencies_json(conn, candidate)

            block = (
                f"=== OBJECT: {candidate.object_name} ===\n"
                f"TYPE: {candidate.type}\n"
                f"PACKAGE: {candidate.package}\n"
                f"\n"
                f"--- SOURCE ---\n"
                f"{source}\n"
                f"\n"
                f"--- DEPENDENCIES ---\n"
                f"{dep_json}\n"
                f"=== END OBJECT ==="
            )
            blocks.append(block)

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(blocks) + "\n")
    finally:
        if owns_conn:
            conn.close()


def extract_object_blocks(text: str) -> str:
    """Returns the substring of an exported file starting after the
    `=== RETRIEVAL METADATA ===` wrapper - i.e. the real RUN_TYPE/PACKAGE/
    .../--- DDIC ---/OBJECT-blocks content, which is what
    `lld_step2.parser.parse_extraction_file` actually expects (it requires
    the file header, not just bare OBJECT blocks). If no metadata wrapper
    is found, returns the text unchanged (already a bare extraction file).
    """
    idx = text.find(_METADATA_END)
    if idx == -1:
        return text
    return text[idx + len(_METADATA_END):].lstrip("\n")
