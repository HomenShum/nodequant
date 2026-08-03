"""Memory invariants, written against the ways memory can produce a wrong number.

Memory is the one component whose purpose is to carry something forward, so it is
the one component that can break the system's central rule by doing its job well:

    The model chooses WHICH questions to ask the API. It never answers them.

Two failure modes are pinned here, and they are the reason this file exists:

  1. A REPLAYED COUNT. Turn 1 measured 4,231 melanoma trials. If that figure
     reaches turn 3's prompt, the planner may restate it for a question with
     different filters — a real number attached to the wrong question, which
     passes every citation and trace check the system has.

  2. A LEAKED FILTER. A genuinely new question silently inherits a stale
     constraint and returns a correct measurement of a question nobody asked.
     Same class of harm, opposite direction, and much easier to ship by accident
     because the number *is* honestly measured.

Everything is offline: no key, no network, no model.
"""

from __future__ import annotations

import re

import pytest

from app.memory import (
    ConversationMemory,
    MemoryStore,
    Turn,
    classify_entity,
    is_followup,
    resolve_followup,
    scrub_numbers,
    summarise,
)
from app.models import (
    Datum,
    Encoding,
    QueryResponse,
    ResponseMeta,
    TrialPhase,
    TrialStatus,
    Visualization,
    VizType,
)
from app.planner import Dimension, PlanFilters, QueryPlan

#: Any run of two or more digits. Nothing `summarise` returns may match this.
MULTI_DIGIT = re.compile(r"\d{2,}")


# ---------------------------------------------------------------- builders


def plan_for(
    dimension: Dimension = Dimension.PHASE,
    viz: VizType = VizType.BAR_CHART,
    title: str = "Melanoma trials by phase",
    **filters,
) -> QueryPlan:
    return QueryPlan(
        dimension=dimension,
        viz_type=viz,
        title=title,
        interpretation="Grouping melanoma trials by phase.",
        filters=PlanFilters(**filters),
    )


def melanoma_by_phase() -> QueryPlan:
    return plan_for(condition="melanoma")


def response_with_big_counts() -> QueryResponse:
    """A realistic turn-1 response whose every number is large.

    Used to prove the recap stays number-free even when the turn it recaps was
    full of five-figure counts.
    """
    return QueryResponse(
        visualization=Visualization(
            type=VizType.BAR_CHART,
            title="Melanoma trials by phase",
            encoding=Encoding(x={"field": "label"}, y={"field": "count"}),
            data=[
                Datum(label="PHASE1", count=4231, citation_total=4231),
                Datum(label="PHASE2", count=38104, citation_total=38104),
                Datum(label="PHASE3", count=12977, citation_total=12977),
            ],
            returned=3,
            total=55312,
        ),
        meta=ResponseMeta(
            interpretation="Melanoma trials grouped by phase.",
            total_matching=55312,
            denominator_note="All melanoma trials in the registry.",
            buckets_are_exclusive=False,
            bucket_sum=55312,
            unbucketed_count=1044,
        ),
    )


# ---------------------------------------------------------------- follow-ups
#
# Scenario: an analyst working a real question over several turns. Each test
# below is one step of that session, not an isolated unit.


def test_followup_inherits_prior_filters_and_applies_the_delta():
    """Turn 1 melanoma/phase -> "now compare that to Opdivo".

    The condition must survive (it was never restated) and the drug must be
    applied (it is the delta). The produced request is a NORMAL QueryRequest —
    it will be planned and executed exactly like a cold one.
    """
    memory = ConversationMemory("s1")
    memory.record(
        "How are melanoma trials distributed across phases?",
        melanoma_by_phase(),
        labels=["PHASE1", "PHASE2", "PHASE3"],
    )

    followup = resolve_followup("now compare that to Opdivo", memory)

    assert followup is not None
    assert followup.condition == "melanoma"      # inherited, never restated
    assert followup.drug_name == "Opdivo"        # the delta
    assert "Opdivo" in followup.query
    assert "melanoma" in followup.query.lower()


def test_followup_drill_into_phase_keeps_condition_and_adds_phase():
    memory = ConversationMemory("s1")
    memory.record("melanoma by phase", melanoma_by_phase(), labels=["PHASE1", "PHASE3"])

    followup = resolve_followup("drill into PHASE3", memory)

    assert followup is not None
    assert followup.condition == "melanoma"
    assert followup.phase == [TrialPhase.PHASE3]


def test_followup_same_but_since_year_replaces_the_whole_window():
    """A year delta must REPLACE, not merge.

    Merging "since 2020" into an inherited `end_year=2015` yields an empty
    range that returns zero trials — a wrong answer with no error.
    """
    memory = ConversationMemory("s1")
    memory.record(
        "melanoma trials before 2015 by phase",
        plan_for(condition="melanoma", end_year=2015),
        labels=["PHASE1"],
    )

    followup = resolve_followup("same but since 2020", memory)

    assert followup is not None
    assert followup.start_year == 2020
    assert followup.end_year is None
    assert followup.condition == "melanoma"


def test_followup_what_about_replaces_the_condition_slot_only():
    memory = ConversationMemory("s1")
    memory.record(
        "recruiting lung cancer trials in Germany by phase",
        plan_for(
            condition="lung cancer",
            location="Germany",
            status=[TrialStatus.RECRUITING],
        ),
        labels=["PHASE1"],
    )

    followup = resolve_followup("what about melanoma", memory)

    assert followup is not None
    assert followup.condition == "melanoma"                 # replaced
    assert followup.location == "Germany"                   # inherited
    assert followup.status == [TrialStatus.RECRUITING]      # inherited


def test_followup_regroup_changes_dimension_and_keeps_filters():
    memory = ConversationMemory("s1")
    memory.record("melanoma by phase", melanoma_by_phase(), labels=["PHASE1"])

    followup = resolve_followup("now break it down by country instead", memory)

    assert followup is not None
    assert followup.condition == "melanoma"
    assert "country" in followup.query.lower()


def test_two_named_drugs_do_not_pin_one_arm_of_the_comparison():
    """A vs B must not collapse to B.

    With a drug already in the prior turn, "compare that to Opdivo" is a real
    two-arm comparison. Pinning `drug_name` on the request would force the
    executor to measure one arm and label it a comparison.
    """
    memory = ConversationMemory("s1")
    memory.record(
        "Keytruda melanoma trials by phase",
        plan_for(drug_name="Keytruda", condition="melanoma"),
        labels=["PHASE1"],
    )

    followup = resolve_followup("now compare that to Opdivo", memory)

    assert followup is not None
    assert followup.drug_name is None                 # neither arm pinned
    assert "Keytruda" in followup.query
    assert "Opdivo" in followup.query
    assert followup.condition == "melanoma"           # still inherited


# ------------------------------------------------- the dangerous negative case


@pytest.mark.parametrize(
    "question",
    [
        # A plain new question.
        "How are lung cancer trials distributed across phases?",
        # A new question that merely OPENS with a continuation word. This is the
        # one that ships by accident: "now" looks like a follow-up marker.
        "Now how are lung cancer trials distributed across phases?",
        "And which sponsors run the most diabetes trials?",
        "Show me pancreatic cancer trials by country",
        "What is the enrollment distribution for asthma trials?",
    ],
)
def test_a_fresh_question_never_inherits_stale_filters(question: str):
    """The dangerous failure: a new question silently constrained by an old one.

    If this regresses, the service returns an honestly-measured count for a
    question the user did not ask — and nothing in the response says so, because
    every filter it reports was genuinely applied.
    """
    memory = ConversationMemory("s1")
    memory.record(
        "melanoma trials sponsored by Merck in Germany",
        plan_for(condition="melanoma", sponsor="Merck", location="Germany"),
        labels=["PHASE1"],
    )

    assert resolve_followup(question, memory) is None
    assert is_followup(question, memory) is False


def test_followup_on_empty_memory_is_not_a_followup():
    memory = ConversationMemory("fresh")
    assert resolve_followup("now compare that to Opdivo", memory) is None
    assert resolve_followup("drill into PHASE3", None) is None


def test_anaphor_with_no_identifiable_delta_returns_none():
    """"What about that?" changes nothing, so there is nothing to re-measure."""
    memory = ConversationMemory("s1")
    memory.record("melanoma by phase", melanoma_by_phase(), labels=["PHASE1"])
    assert resolve_followup("what about that?", memory) is None


# ---------------------------------------------------------------- summarise


def test_summarise_contains_no_multi_digit_numbers_even_after_huge_counts():
    """The core anti-replay test.

    Turn 1 measured tens of thousands of trials across three phases, and turn 2
    added a year filter. Neither the counts nor the years may appear in the text
    handed to the planner: the planner cannot distinguish a remembered
    measurement from an invented one, because both arrive as tokens.
    """
    memory = ConversationMemory("s1")
    memory.record_response(
        "How are melanoma trials distributed across phases?",
        melanoma_by_phase(),
        response_with_big_counts(),
    )
    memory.record(
        "melanoma trials since 2020 per year",
        plan_for(
            dimension=Dimension.YEAR,
            viz=VizType.TIME_SERIES,
            title="Melanoma trials per year",
            condition="melanoma",
            start_year=2020,
        ),
        labels=["2020", "2021", "2022", "2023"],
    )

    recap = summarise(memory)

    assert recap, "a recap with turns recorded must not be empty"
    assert MULTI_DIGIT.search(recap) is None, f"recap leaked a number: {recap!r}"
    for leaked in ("4231", "38104", "12977", "55312", "1044", "2020", "2023"):
        assert leaked not in recap
    # It must still be useful: entities and dimensions survive.
    assert "melanoma" in recap.lower()
    assert "phase" in recap.lower()
    assert "year" in recap.lower()
    # And it must say out loud that no result carried over.
    assert "no counts" in recap.lower()


def test_summarise_scrubs_numbers_smuggled_through_free_text_plan_fields():
    """A plan title is model-authored free text. It reaches the recap; scrub it."""
    memory = ConversationMemory("s1")
    memory.record(
        "melanoma by phase",
        plan_for(
            title="There are 45231 melanoma trials, mostly PHASE2",
            condition="melanoma",
        ),
        labels=["10000-49999", "50000+"],
    )
    recap = summarise(memory)
    assert MULTI_DIGIT.search(recap) is None
    assert "45231" not in recap


def test_summarise_empty_memory_is_empty_string():
    assert summarise(ConversationMemory("s1")) == ""
    assert summarise(None) == ""


def test_scrub_numbers_is_the_standalone_guarantee():
    assert MULTI_DIGIT.search(scrub_numbers("measured 55,312 trials in 2024")) is None
    # Single digits survive — PHASE3 is an enum value, not a quantity.
    assert "PHASE3" in scrub_numbers("grouped by PHASE3")


# ---------------------------------------------------------------- bounds


def test_memory_is_bounded_by_turns_and_evicts_the_oldest():
    memory = ConversationMemory("s1", max_turns=3, max_bytes=10**9)
    for i in range(10):
        memory.record(
            f"question about condition-{i}",
            plan_for(condition=f"condition-{i}"),
            labels=["PHASE1"],
        )

    assert len(memory) == 3
    conditions = [t.filters.condition for t in memory.turns]
    assert conditions == ["condition-7", "condition-8", "condition-9"]
    assert memory.last is not None and memory.last.filters.condition == "condition-9"


def test_memory_is_bounded_by_bytes_and_evicts_the_oldest():
    """A turn-count cap alone does not bound the process.

    Plans carry free-text titles and assumption lists; a handful of verbose
    turns can outweigh a hundred terse ones.
    """
    memory = ConversationMemory("s1", max_turns=1000, max_bytes=2_000)
    for i in range(60):
        memory.record(
            f"a deliberately long question number {i} " + ("padding " * 20),
            plan_for(title=("x" * 110), condition=f"condition-{i}"),
            labels=[f"label-{j}" for j in range(20)],
        )

    assert memory.size_bytes() <= 2_000
    assert len(memory) < 60
    # Oldest went first; newest is always retained.
    assert memory.last is not None
    assert memory.last.filters.condition == "condition-59"
    assert all(t.filters.condition != "condition-0" for t in memory.turns)


def test_one_oversized_turn_is_retained_rather_than_evicting_to_empty():
    """A memory that drops the turn a follow-up refers to is worse than none:
    `resolve_followup` would silently return a fresh query while the user
    believes their context carried."""
    memory = ConversationMemory("s1", max_turns=5, max_bytes=1)
    memory.record("melanoma by phase", melanoma_by_phase(), labels=["PHASE1"])
    assert len(memory) == 1
    assert resolve_followup("drill into PHASE3", memory) is not None


def test_store_bounds_the_number_of_sessions_and_evicts_least_recent():
    """Per-session caps do nothing if session ids can be minted per request."""
    store = MemoryStore(max_sessions=3, max_turns=4)
    for i in range(10):
        store.for_session(f"session-{i}").record(
            "melanoma by phase", melanoma_by_phase(), labels=["PHASE1"]
        )

    assert len(store) == 3
    assert store.session_ids == ("session-7", "session-8", "session-9")


def test_store_lru_keeps_the_recently_touched_session():
    store = MemoryStore(max_sessions=2, max_turns=4)
    store.for_session("a").record("melanoma", melanoma_by_phase(), labels=["PHASE1"])
    store.for_session("b").record("melanoma", melanoma_by_phase(), labels=["PHASE1"])
    store.for_session("a")                       # touch -> "b" is now least recent
    store.for_session("c").record("melanoma", melanoma_by_phase(), labels=["PHASE1"])

    assert set(store.session_ids) == {"a", "c"}


# ---------------------------------------------------------------- isolation


def test_two_sessions_never_see_each_other_state():
    """Cross-session bleed is the multi-tenant form of the stale-filter bug."""
    store = MemoryStore(max_sessions=16)
    alice = store.for_session("alice")
    bob = store.for_session("bob")

    alice.record(
        "melanoma trials sponsored by Merck by phase",
        plan_for(condition="melanoma", sponsor="Merck"),
        labels=["PHASE1"],
    )

    assert len(bob) == 0
    assert bob.last is None
    assert summarise(bob) == ""
    assert resolve_followup("now compare that to Opdivo", bob) is None

    alice_followup = resolve_followup("now compare that to Opdivo", alice)
    assert alice_followup is not None
    assert alice_followup.condition == "melanoma"
    assert alice_followup.sponsor == "Merck"

    bob.record("diabetes trials by country", plan_for(condition="diabetes"), labels=["US"])
    bob_followup = resolve_followup("drill into PHASE3", bob)
    assert bob_followup is not None
    assert bob_followup.condition == "diabetes"   # never "melanoma"
    assert bob_followup.sponsor is None           # never "Merck"

    # Alice is unchanged by Bob's activity.
    assert alice.last is not None
    assert alice.last.filters.condition == "melanoma"
    assert "diabetes" not in summarise(alice).lower()
    assert "merck" not in summarise(bob).lower()


def test_same_session_id_returns_the_same_memory_object():
    store = MemoryStore()
    assert store.for_session("x") is store.for_session("x")
    assert store.for_session("x") is not store.for_session("y")


# ---------------------------------------------------------------- no counts


def test_turn_has_no_field_that_can_hold_a_count():
    """Structural, not behavioural: the invariant is enforced by the type.

    If someone adds `total_matching` to `Turn` in six months, this fails before
    the leak reaches `summarise`.
    """
    allowed = {
        "query", "plan", "filters", "dimension", "viz_type", "labels",
        "max_citations_per_datum",
    }
    assert set(Turn.__dataclass_fields__) == allowed


def test_record_response_stores_shape_and_discards_every_count():
    memory = ConversationMemory("s1")
    turn = memory.record_response(
        "How are melanoma trials distributed across phases?",
        melanoma_by_phase(),
        response_with_big_counts(),
    )

    assert turn.labels == ("PHASE1", "PHASE2", "PHASE3")
    assert turn.viz_type is VizType.BAR_CHART
    blob = repr(turn)
    for count in ("4231", "38104", "12977", "55312", "1044"):
        assert count not in blob


def test_followup_is_a_fresh_request_carrying_no_result():
    """The returned object is a plain QueryRequest — it has nowhere to put a
    remembered number, so re-measurement is not a convention but the only
    option."""
    memory = ConversationMemory("s1")
    memory.record_response(
        "How are melanoma trials distributed across phases?",
        melanoma_by_phase(),
        response_with_big_counts(),
    )
    followup = resolve_followup("now compare that to Opdivo", memory)
    assert followup is not None
    dumped = followup.model_dump_json()
    for count in ("4231", "38104", "12977", "55312", "1044"):
        assert count not in dumped


# ---------------------------------------------------------------- entity slot


@pytest.mark.parametrize(
    "name,slot",
    [
        ("Opdivo", "drug"),
        ("Keytruda", "drug"),
        ("Pembrolizumab", "drug"),
        ("Nivolumab", "drug"),
        ("Osimertinib", "drug"),
        ("melanoma", "condition"),
        ("lung cancer", "condition"),
        ("type 2 diabetes", "condition"),
        ("Alzheimer disease", "condition"),
    ],
)
def test_entity_classification_picks_the_right_filter_slot(name: str, slot: str):
    assert classify_entity(name) == slot


def test_composed_followup_query_routes_identically_without_an_llm():
    """A follow-up must work with no credentials.

    The composed prose is checked against the deterministic keyword planner, so
    the feature is not silently key-only.
    """
    from app.planner import fallback_plan

    memory = ConversationMemory("s1")
    memory.record("melanoma by phase", melanoma_by_phase(), labels=["PHASE1"])

    followup = resolve_followup("now break it down by country", memory)
    assert followup is not None
    plan = fallback_plan(followup)
    assert plan.dimension is Dimension.COUNTRY
    assert plan.filters.condition == "melanoma"   # explicit field won the merge
