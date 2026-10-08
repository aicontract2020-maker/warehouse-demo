from __future__ import annotations

from uuid import UUID, uuid4

from app.domain.crossing import ActionType, DirectionalAction
from app.domain.event_machine import (
    EventContext,
    EventFrame,
    EventMachine,
    EventState,
    SourceHealth,
)


def context() -> EventContext:
    return EventContext(
        task_id="task-1",
        zone_profile_id="zone-1",
        zone_version=3,
        sku_id="sku-box",
        unit="case",
        source_location_id="A-03-01",
        destination_location_id="STAGING",
        source_session_id=uuid4(),
        configuration_version="cfg-7",
    )


def action(
    event_id: UUID,
    sequence: int,
    action_type: ActionType = ActionType.PICK,
    confidence: float = 0.99,
) -> DirectionalAction:
    return DirectionalAction(
        action_id=UUID(int=sequence),
        event_id=event_id,
        action_sequence=sequence,
        track_id=sequence,
        transition_index=1,
        action_type=action_type,
        delta=1 if action_type is ActionType.PICK else -1,
        source_timestamp_ms=sequence * 250,
        frame_sequence=sequence,
        confidence=confidence,
        bbox_xyxy=(0, 0, 10, 10),
    )


def frame(
    sequence: int,
    timestamp_ms: int,
    *actions: DirectionalAction,
    motion_stable: bool = True,
    source_health: SourceHealth = SourceHealth.CONNECTED,
    unresolved: frozenset[int] = frozenset(),
    flags: frozenset[str] = frozenset(),
) -> EventFrame:
    return EventFrame(
        frame_sequence=sequence,
        source_timestamp_ms=timestamp_ms,
        motion_stable=motion_stable,
        source_health=source_health,
        unresolved_boundary_track_ids=unresolved,
        actions=tuple(actions),
        integrity_flags=flags,
    )


def test_first_action_starts_event_and_captures_context() -> None:
    machine = EventMachine()
    event_id = uuid4()
    event_context = context()

    snapshot = machine.update(frame(1, 250, action(event_id, 1)), event_context)

    assert snapshot.event_id == event_id
    assert snapshot.state is EventState.ACTIVE
    assert snapshot.started_source_timestamp_ms == 250
    assert snapshot.context == event_context
    assert snapshot.pick_count == 1
    assert snapshot.return_count == 0
    assert snapshot.net_quantity == 1


def test_actions_with_short_pauses_stay_in_one_event_and_recalculate_net() -> None:
    machine = EventMachine()
    event_id = uuid4()
    event_context = context()
    machine.update(frame(1, 0, action(event_id, 1)), event_context)
    machine.update(frame(2, 2_500), event_context)

    snapshot = machine.update(
        frame(3, 9_500, action(event_id, 2, ActionType.RETURN)), event_context
    )

    assert snapshot.state is EventState.ACTIVE
    assert snapshot.event_id == event_id
    assert snapshot.pick_count == 1
    assert snapshot.return_count == 1
    assert snapshot.net_quantity == 0
    assert [item.action_type for item in snapshot.actions] == [
        ActionType.PICK,
        ActionType.RETURN,
    ]


def test_event_completes_after_quiet_then_continuously_stable_interval() -> None:
    machine = EventMachine(quiet_ms=3_000, stable_ms=2_000, hard_idle_ms=10_000)
    event_id = uuid4()
    event_context = context()
    machine.update(frame(1, 0, action(event_id, 1)), event_context)

    settling = machine.update(frame(2, 3_000), event_context)
    completed = machine.update(frame(3, 5_000), event_context)

    assert settling.state is EventState.SETTLING
    assert completed.state is EventState.COMPLETED
    assert completed.ended_source_timestamp_ms == 5_000


def test_unstable_or_unresolved_region_delays_completion_until_hard_timeout() -> None:
    machine = EventMachine()
    event_id = uuid4()
    event_context = context()
    machine.update(frame(1, 0, action(event_id, 1)), event_context)
    machine.update(frame(2, 3_000, motion_stable=False), event_context)
    machine.update(frame(3, 7_000, unresolved=frozenset({7})), event_context)

    timed_out = machine.update(frame(4, 10_000, motion_stable=False), event_context)

    assert timed_out.state is EventState.REVIEW_REQUIRED
    assert timed_out.review_reason == "HARD_IDLE_TIMEOUT"
    assert timed_out.ended_source_timestamp_ms == 10_000


def test_new_action_during_settling_returns_to_active() -> None:
    machine = EventMachine()
    event_id = uuid4()
    event_context = context()
    machine.update(frame(1, 0, action(event_id, 1)), event_context)
    assert machine.update(frame(2, 3_000), event_context).state is EventState.SETTLING

    resumed = machine.update(frame(3, 4_000, action(event_id, 2)), event_context)

    assert resumed.state is EventState.ACTIVE
    assert resumed.pick_count == 2


def test_stream_interruption_freezes_event_for_review() -> None:
    machine = EventMachine()
    event_id = uuid4()
    event_context = context()
    machine.update(frame(1, 0, action(event_id, 1)), event_context)

    interrupted = machine.update(
        frame(2, 500, source_health=SourceHealth.RECONNECTING), event_context
    )
    after_reconnect = machine.update(
        frame(3, 1_000, action(event_id, 2)), event_context
    )

    assert interrupted.state is EventState.REVIEW_REQUIRED
    assert interrupted.review_reason == "SOURCE_INTERRUPTED"
    assert "SOURCE_INTERRUPTED" in interrupted.integrity_flags
    assert after_reconnect == interrupted


def test_event_preserves_integrity_flags_and_minimum_action_confidence() -> None:
    machine = EventMachine()
    event_id = uuid4()
    event_context = context()

    snapshot = machine.update(
        frame(
            2,
            500,
            action(event_id, 1, confidence=0.999),
            action(event_id, 2, confidence=0.91),
            flags=frozenset({"OVERLAPPING_INSEPARABLE"}),
        ),
        event_context,
    )

    assert snapshot.aggregate_confidence == 0.91
    assert snapshot.integrity_flags == frozenset({"OVERLAPPING_INSEPARABLE"})


def test_negative_net_event_requires_review_but_zero_net_can_complete() -> None:
    event_context = context()
    negative_machine = EventMachine()
    negative_id = uuid4()
    negative = negative_machine.update(
        frame(1, 0, action(negative_id, 1, ActionType.RETURN)), event_context
    )

    zero_machine = EventMachine()
    zero_id = uuid4()
    zero_machine.update(frame(1, 0, action(zero_id, 1)), event_context)
    zero_machine.update(
        frame(2, 500, action(zero_id, 2, ActionType.RETURN)), event_context
    )
    zero_machine.update(frame(3, 3_500), event_context)
    zero = zero_machine.update(frame(4, 5_500), event_context)

    assert negative.state is EventState.REVIEW_REQUIRED
    assert negative.review_reason == "NEGATIVE_NET_QUANTITY"
    assert zero.state is EventState.COMPLETED
    assert zero.net_quantity == 0


def test_late_frame_is_discarded_without_reversing_actions() -> None:
    machine = EventMachine()
    event_id = uuid4()
    event_context = context()
    accepted = machine.update(frame(2, 500, action(event_id, 1)), event_context)

    late = machine.update(frame(1, 250, action(event_id, 2)), event_context)

    assert len(late.actions) == 1
    assert late.actions == accepted.actions
    assert "LATE_RESULT_DISCARDED" in late.integrity_flags

