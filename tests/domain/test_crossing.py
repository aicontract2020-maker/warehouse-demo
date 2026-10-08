from __future__ import annotations

from uuid import UUID, uuid4

from app.domain.crossing import ActionType, CrossingEngine
from app.domain.zones import ZoneProfile
from app.vision.tracker import TrackingBatch, TrackObservation, TrackState


def zone() -> ZoneProfile:
    return ZoneProfile(
        zone_profile_id="zone-1",
        source_profile_id="source-1",
        name="Pick zone",
        version=1,
        source_width=1000,
        source_height=600,
        shelf_polygon=((0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)),
        interaction_polygon=((0.2, 0.0), (0.8, 0.0), (0.8, 1.0), (0.2, 1.0)),
        exit_polygon=((0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 1.0)),
        counting_line=((0.5, 0.0), (0.5, 1.0)),
        shelf_side_sign=1,
        uncertainty_band_norm=0.02,
        crossing_confirm_frames=2,
        quiet_seconds=3,
        stable_seconds=2,
        hard_idle_seconds=10,
        merge_window_seconds=10,
        active=True,
    )


def observation(
    track_id: int,
    x: float,
    *,
    state: TrackState = TrackState.CONFIRMED,
    ambiguous: bool = False,
    confidence: float = 0.98,
) -> TrackObservation:
    pixel_x = x * 1000
    return TrackObservation(
        track_id=track_id,
        class_name="box",
        confidence=confidence,
        bbox_xyxy=(pixel_x - 20, 100, pixel_x + 20, 200),
        anchor_xy=(pixel_x, 200),
        velocity_xy_per_second=(0, 0),
        state=state,
        age_observations=3,
        missed_duration_ms=0,
        ambiguous=ambiguous,
    )


def tracking_batch(
    session_id: UUID,
    sequence: int,
    *observations: TrackObservation,
) -> TrackingBatch:
    return TrackingBatch(
        session_id=session_id,
        continuity_segment=0,
        frame_sequence=sequence,
        source_timestamp_ms=sequence * 250,
        observations=tuple(observations),
    )


def feed(
    engine: CrossingEngine,
    event_id: UUID,
    session_id: UUID,
    positions: list[float],
    track_id: int = 1,
) -> list:
    actions = []
    for sequence, x in enumerate(positions, start=1):
        result = engine.update(
            event_id,
            tracking_batch(session_id, sequence, observation(track_id, x)),
            zone(),
        )
        actions.extend(result.actions)
    return actions


def test_pick_requires_two_stable_exit_observations() -> None:
    engine = CrossingEngine(uuid_factory=iter((UUID(int=1),)).__next__)
    event_id = uuid4()

    actions = feed(engine, event_id, uuid4(), [0.30, 0.60, 0.62])

    assert len(actions) == 1
    assert actions[0].action_id == UUID(int=1)
    assert actions[0].event_id == event_id
    assert actions[0].action_sequence == 1
    assert actions[0].track_id == 1
    assert actions[0].transition_index == 1
    assert actions[0].action_type is ActionType.PICK
    assert actions[0].delta == 1
    assert actions[0].frame_sequence == 3


def test_jitter_and_uncertainty_band_do_not_emit_an_action() -> None:
    engine = CrossingEngine()

    actions = feed(engine, uuid4(), uuid4(), [0.30, 0.49, 0.51, 0.49, 0.60, 0.49])

    assert actions == []


def test_staying_on_exit_side_does_not_duplicate_pick() -> None:
    engine = CrossingEngine()

    actions = feed(engine, uuid4(), uuid4(), [0.30, 0.60, 0.62, 0.65, 0.70])

    assert [action.action_type for action in actions] == [ActionType.PICK]


def test_return_requires_prior_pick_and_rearms_next_pick() -> None:
    engine = CrossingEngine()

    actions = feed(
        engine,
        uuid4(),
        uuid4(),
        [0.30, 0.60, 0.62, 0.40, 0.38, 0.60, 0.62],
    )

    assert [action.action_type for action in actions] == [
        ActionType.PICK,
        ActionType.RETURN,
        ActionType.PICK,
    ]
    assert [action.delta for action in actions] == [1, -1, 1]
    assert [action.transition_index for action in actions] == [1, 2, 3]
    assert [action.action_sequence for action in actions] == [1, 2, 3]


def test_track_starting_at_exit_cannot_create_unpaired_return() -> None:
    engine = CrossingEngine()

    actions = feed(engine, uuid4(), uuid4(), [0.70, 0.40, 0.38])

    assert actions == []


def test_tentative_ambiguous_or_lost_tracks_do_not_count() -> None:
    engine = CrossingEngine()
    event_id = uuid4()
    session_id = uuid4()
    engine.update(event_id, tracking_batch(session_id, 1, observation(1, 0.30)), zone())

    tentative = engine.update(
        event_id,
        tracking_batch(
            session_id, 2, observation(1, 0.60, state=TrackState.TENTATIVE)
        ),
        zone(),
    )
    ambiguous = engine.update(
        event_id,
        tracking_batch(session_id, 3, observation(1, 0.60, ambiguous=True)),
        zone(),
    )
    lost = engine.update(
        event_id,
        tracking_batch(session_id, 4, observation(1, 0.50, state=TrackState.LOST)),
        zone(),
    )

    assert tentative.actions == ambiguous.actions == lost.actions == ()
    assert "TRACK_LOST_NEAR_BOUNDARY" in lost.integrity_flags


def test_distinct_tracks_emit_distinct_actions_in_track_order() -> None:
    engine = CrossingEngine()
    event_id = uuid4()
    session_id = uuid4()
    engine.update(
        event_id,
        tracking_batch(session_id, 1, observation(2, 0.30), observation(1, 0.30)),
        zone(),
    )
    engine.update(
        event_id,
        tracking_batch(session_id, 2, observation(2, 0.60), observation(1, 0.60)),
        zone(),
    )

    result = engine.update(
        event_id,
        tracking_batch(session_id, 3, observation(2, 0.62), observation(1, 0.62)),
        zone(),
    )

    assert [action.track_id for action in result.actions] == [1, 2]
    assert [action.action_sequence for action in result.actions] == [1, 2]


def test_new_event_resets_action_sequence_and_track_transition_state() -> None:
    engine = CrossingEngine()
    first_event = uuid4()
    second_event = uuid4()
    session_id = uuid4()
    first_actions = feed(engine, first_event, session_id, [0.30, 0.60, 0.62])

    second_actions = feed(engine, second_event, session_id, [0.30, 0.60, 0.62])

    assert first_actions[0].action_sequence == 1
    assert second_actions[0].action_sequence == 1
    assert second_actions[0].event_id == second_event

