from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from app.video.buffer import LatestFrameBuffer
from app.video.sources import ReadKind, SourceRead, VideoSource
from app.vision.detector import FrameEnvelope


class CaptureWorker:
    def __init__(
        self,
        source: VideoSource,
        buffer: LatestFrameBuffer,
        session_id: UUID,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._source = source
        self._buffer = buffer
        self._session_id = session_id
        self._monotonic_ns = monotonic_ns
        self._sequence = 0
        self._stopped = False

    def capture_once(self) -> SourceRead:
        if self._stopped:
            raise RuntimeError("capture worker is stopped")
        result = self._source.read()
        if result.kind is not ReadKind.FRAME:
            return result
        assert result.image_bgr is not None and result.source_timestamp_ms is not None
        envelope = FrameEnvelope(
            session_id=self._session_id,
            continuity_segment=self._source.continuity_segment,
            sequence=self._sequence,
            source_timestamp_ms=result.source_timestamp_ms,
            captured_monotonic_ns=self._monotonic_ns(),
            image_bgr=result.image_bgr,
        )
        self._buffer.publish(envelope)
        self._sequence += 1
        return result

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._source.release()
        self._buffer.close()

