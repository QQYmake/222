"""Tests for the normalizer module."""

from __future__ import annotations

import pytest

from app.adapters.base import RawObservation
from app.database import ObservationRecord
from app.normalizer import (
    affected_weeks,
    iso_week_id,
    normalize_observations,
    week_utc_range,
)


class TestNormalizeObservations:
    def _make_raw(self, ts=1000, obs_type="heart_rate", value=None):
        return RawObservation(
            source_timestamp_sec=ts,
            type=obs_type,
            value=value or {"bpm": 79},
            source_table="XIAOMI_ACTIVITY_SAMPLE",
            source_identity=f"row:{ts}",
            raw_fields={"HEART_RATE": 79},
        )

    def test_produces_observation_records(self):
        raw = [self._make_raw()]
        results = normalize_observations(
            raw, internal_device_id=1, internal_user_id=1,
        )
        assert len(results) == 1
        assert isinstance(results[0], ObservationRecord)

    def test_utc_timestamp_format(self):
        raw = [self._make_raw(ts=1783746120)]
        results = normalize_observations(
            raw, internal_device_id=1, internal_user_id=1,
        )
        # 1783746120 = 2026-07-11T05:02:00+00:00
        assert results[0].timestamp_utc == "2026-07-11T05:02:00+00:00"

    def test_local_timestamp_shanghai(self):
        raw = [self._make_raw(ts=1783746120)]
        results = normalize_observations(
            raw, internal_device_id=1, internal_user_id=1,
        )
        # UTC+8 → 2026-07-11T13:02:00+08:00
        assert results[0].timestamp_local == "2026-07-11T13:02:00+08:00"

    def test_dedup_key_deterministic(self):
        raw = [self._make_raw()]
        r1 = normalize_observations(raw, internal_device_id=1, internal_user_id=1)
        r2 = normalize_observations(raw, internal_device_id=1, internal_user_id=1)
        assert r1[0].dedup_key == r2[0].dedup_key

    def test_dedup_key_differs_by_device(self):
        raw = [self._make_raw()]
        r1 = normalize_observations(raw, internal_device_id=1, internal_user_id=1)
        r2 = normalize_observations(raw, internal_device_id=2, internal_user_id=1)
        assert r1[0].dedup_key != r2[0].dedup_key

    def test_dedup_key_differs_by_value(self):
        raw1 = [self._make_raw(value={"bpm": 79})]
        raw2 = [self._make_raw(value={"bpm": 80})]
        r1 = normalize_observations(raw1, internal_device_id=1, internal_user_id=1)
        r2 = normalize_observations(raw2, internal_device_id=1, internal_user_id=1)
        assert r1[0].dedup_key != r2[0].dedup_key

    def test_dedup_key_differs_by_type(self):
        raw1 = [self._make_raw(obs_type="heart_rate")]
        raw2 = [self._make_raw(obs_type="steps")]
        r1 = normalize_observations(raw1, internal_device_id=1, internal_user_id=1)
        r2 = normalize_observations(raw2, internal_device_id=1, internal_user_id=1)
        assert r1[0].dedup_key != r2[0].dedup_key

    def test_raw_source_preserved(self):
        raw = [self._make_raw()]
        results = normalize_observations(
            raw, internal_device_id=1, internal_user_id=1,
        )
        assert results[0].raw_source == {"HEART_RATE": 79}

    def test_snapshot_hash_propagated(self):
        raw = [self._make_raw()]
        results = normalize_observations(
            raw, internal_device_id=1, internal_user_id=1,
            snapshot_hash="abc123",
        )
        assert results[0].snapshot_hash == "abc123"


class TestIsoWeekHelpers:
    def test_iso_week_id(self):
        assert iso_week_id("2026-07-09T14:22:00+00:00") == "2026-W28"

    def test_iso_week_id_january(self):
        assert iso_week_id("2026-01-01T00:00:00+00:00") == "2026-W01"

    def test_week_utc_range_monday_to_sunday(self):
        start, end = week_utc_range("2026-W28")
        # Monday 2026-07-06 00:00 Shanghai = 2026-07-05 16:00 UTC
        assert start == "2026-07-05T16:00:00+00:00"
        # Sunday 2026-07-12 23:59:59 Shanghai = 2026-07-12 15:59:59 UTC
        assert end == "2026-07-12T15:59:59+00:00"

    def test_affected_weeks_sorted_unique(self):
        obs = [
            ObservationRecord(
                dedup_key="k1", type="hr",
                timestamp_utc="2026-07-09T10:00:00+00:00",
                timestamp_local="x", value={}, raw_source={},
                source_table="t", source_identity="r",
                internal_device_id=1, internal_user_id=1,
            ),
            ObservationRecord(
                dedup_key="k2", type="hr",
                timestamp_utc="2026-07-08T10:00:00+00:00",
                timestamp_local="x", value={}, raw_source={},
                source_table="t", source_identity="r",
                internal_device_id=1, internal_user_id=1,
            ),
            ObservationRecord(
                dedup_key="k3", type="hr",
                timestamp_utc="2026-07-09T11:00:00+00:00",
                timestamp_local="x", value={}, raw_source={},
                source_table="t", source_identity="r",
                internal_device_id=1, internal_user_id=1,
            ),
        ]
        weeks = affected_weeks(obs)
        assert weeks == ["2026-W28"]


class TestRealTimestamps:
    """Verify normalization against real Gadgetbridge timestamps."""

    @pytest.mark.parametrize("ts,expected_utc", [
        (1783746120, "2026-07-11T05:02:00+00:00"),
        (1783746720, "2026-07-11T05:12:00+00:00"),
        (1783747320, "2026-07-11T05:22:00+00:00"),
    ])
    def test_real_hr_timestamps(self, ts, expected_utc):
        raw = [RawObservation(
            source_timestamp_sec=ts, type="heart_rate",
            value={"bpm": 79}, source_table="t",
            source_identity=f"row:{ts}", raw_fields={},
        )]
        result = normalize_observations(
            raw, internal_device_id=1, internal_user_id=1,
        )
        assert result[0].timestamp_utc == expected_utc

    def test_real_daily_summary_ms_conversion(self):
        """Daily summary timestamp 1783752053000 ms → 1783752053 sec."""
        ts_ms = 1783752053000
        ts_sec = ts_ms // 1000
        raw = [RawObservation(
            source_timestamp_sec=ts_sec, type="steps_daily",
            value={"steps": 0, "source": "daily_summary"},
            source_table="XIAOMI_DAILY_SUMMARY_SAMPLE",
            source_identity=f"row:{ts_ms}", raw_fields={},
        )]
        result = normalize_observations(
            raw, internal_device_id=1, internal_user_id=1,
        )
        # 1783752053 = 2026-07-11T06:40:53+00:00
        assert result[0].timestamp_utc == "2026-07-11T06:40:53+00:00"
