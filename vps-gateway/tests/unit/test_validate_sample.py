"""validate_sample 和错误类型测试。"""
import pytest
from datetime import datetime, timezone
from app.domain.models.sample import (
    SampleType,
    SampleEnvelope,
    SampleValidationError,
    SampleReadError,
    validate_sample,
    is_valid_iso8601,
)


_SENTINEL = object()


def _make_envelope(
    sample_type="identity",
    version=1,
    updated_at="2025-01-01T00:00:00+08:00",
    source="sample",
    data=_SENTINEL,
):
    """构造测试用 Envelope。"""
    if data is _SENTINEL:
        data = {"name": "test"}
    return SampleEnvelope(
        sample_type=sample_type,
        version=version,
        updated_at=updated_at,
        source=source,
        data=data,
    )


class TestIsValidIso8601:
    def test_valid_iso8601(self):
        assert is_valid_iso8601("2025-01-01T00:00:00+08:00") is True

    def test_valid_utc(self):
        assert is_valid_iso8601("2025-01-01T00:00:00Z") is True

    def test_invalid_string(self):
        assert is_valid_iso8601("not-a-date") is False

    def test_empty_string(self):
        assert is_valid_iso8601("") is False

    def test_none(self):
        assert is_valid_iso8601(None) is False


class TestValidateSample:
    def test_valid_sample_passes(self):
        env = _make_envelope()
        result = validate_sample(env, expected_type="identity")
        assert result is env

    def test_type_mismatch_raises(self):
        env = _make_envelope(sample_type="identity")
        with pytest.raises(SampleValidationError, match="sample_type"):
            validate_sample(env, expected_type="preferences")

    def test_version_below_1_raises(self):
        env = _make_envelope(version=0, source="sample")
        with pytest.raises(SampleValidationError, match="version"):
            validate_sample(env, expected_type="identity")

    def test_fallback_empty_version_0_allowed(self):
        env = _make_envelope(version=0, source="fallback_empty")
        result = validate_sample(env, expected_type="identity")
        assert result is env

    def test_invalid_updated_at_raises(self):
        env = _make_envelope(updated_at="not-a-date")
        with pytest.raises(SampleValidationError, match="updated_at"):
            validate_sample(env, expected_type="identity")

    def test_missing_data_raises(self):
        env = _make_envelope(data=None)
        with pytest.raises(SampleValidationError, match="data"):
            validate_sample(env, expected_type="identity")

    def test_negative_version_raises(self):
        env = _make_envelope(version=-1, source="sample")
        with pytest.raises(SampleValidationError, match="version"):
            validate_sample(env, expected_type="identity")


class TestSampleReadError:
    def test_public_message_contains_type_and_reason(self):
        err = SampleReadError(
            sample_type="identity",
            reason="missing",
            cause=FileNotFoundError("no such file"),
        )
        msg = err.public_message
        assert "identity" in msg
        assert "missing" in msg

    def test_stores_cause(self):
        original = IOError("disk error")
        err = SampleReadError(
            sample_type="preferences",
            reason="io_error",
            cause=original,
        )
        assert err.cause is original

    def test_stores_sample_type(self):
        err = SampleReadError(
            sample_type="memories",
            reason="invalid_json",
            cause=ValueError("bad json"),
        )
        assert err.sample_type == "memories"


class TestSampleEnvelopeImmutable:
    def test_envelope_is_immutable(self):
        env = _make_envelope()
        with pytest.raises((AttributeError, TypeError)):
            env.version = 99
