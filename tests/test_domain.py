"""Domain tests: IDs, titles, rating types, periods, job transitions."""
import pytest

from domain.models import can_transition
from domain.titles import canonical_title
from domain.validation import (
    normalize_federation, validate_fide_id, validate_period,
    validate_rating_type,
)


class TestFideId:
    def test_required(self):
        with pytest.raises(ValueError):
            validate_fide_id("")
        with pytest.raises(ValueError):
            validate_fide_id("   ")
        with pytest.raises(ValueError):
            validate_fide_id(None)

    def test_unique_business_key_shape(self):
        assert validate_fide_id("  12512294 ") == "12512294"

    def test_too_long_rejected(self):
        with pytest.raises(ValueError):
            validate_fide_id("1" * 21)


class TestTitles:
    def test_priority_order(self):
        assert canonical_title("GM", "WGM", "X", "Y") == "GM"
        assert canonical_title("", "WIM", "X", "Y") == "WIM"
        assert canonical_title(None, "", "OT", "FOA") == "OT"
        assert canonical_title("", "", "", "FOA") == "FOA"

    def test_empty_when_none(self):
        assert canonical_title("", "", "", "") == ""
        assert canonical_title(None, None, None, None) == ""
        assert canonical_title("  ", None, None, None) == ""

    def test_strips_whitespace(self):
        assert canonical_title(" GM ", None, None, None) == "GM"


class TestRatingTypes:
    def test_valid(self):
        for t in ("standard", "rapid", "blitz"):
            assert validate_rating_type(t) == t

    def test_invalid(self):
        with pytest.raises(ValueError):
            validate_rating_type("classical")
        with pytest.raises(ValueError):
            validate_rating_type("")


class TestPeriods:
    def test_valid_normalized(self):
        assert validate_period("2026-9") == "2026-09"
        assert validate_period("2026-09") == "2026-09"

    def test_invalid(self):
        for bad in ("", "2026", "2026-13", "2026-00", "99-09",
                    "1999-12", "sep26", None):
            with pytest.raises(ValueError):
                validate_period(bad)


class TestFederation:
    def test_normalized(self):
        assert normalize_federation("iri") == "IRI"
        assert normalize_federation(None) == ""
        assert normalize_federation("") == ""

    def test_too_long(self):
        with pytest.raises(ValueError):
            normalize_federation("TOOLONG")


class TestJobTransitions:
    def test_happy_path(self):
        assert can_transition("pending", "downloading")
        assert can_transition("downloading", "validating")
        assert can_transition("validating", "importing")
        assert can_transition("importing", "verifying")
        assert can_transition("verifying", "completed")
        assert can_transition("failed", "pending")

    def test_forbidden(self):
        assert not can_transition("pending", "completed")
        assert not can_transition("completed", "pending")
        assert not can_transition("completed", "failed")
        assert not can_transition("failed", "completed")
        assert not can_transition("downloading", "importing")
        assert not can_transition("pending", "failed")  # via fail path only


class TestErrorClassification:
    def test_database_errors(self):
        from sqlalchemy.exc import DataError, IntegrityError
        from domain.errors import FideErrorCategory, classify_exception
        db_err = DataError("INSERT ...", {},
                           Exception('(1406, "Data too long")'))
        assert classify_exception(db_err) == \
            FideErrorCategory.DATABASE_ERROR
        assert classify_exception(IntegrityError(
            "INSERT ...", {}, Exception("duplicate"))) == \
            FideErrorCategory.DATABASE_ERROR

    def test_mysql_error_number_is_not_http(self):
        # Regression: naive "40"/"50" substring matching once labeled
        # MySQL error 1406 as HTTP_ERROR during a real import.
        from domain.errors import FideErrorCategory, classify_exception
        assert classify_exception(Exception(
            '(1406, "Data too long for column")')) != \
            FideErrorCategory.HTTP_ERROR

    def test_http_codes_still_match(self):
        from domain.errors import FideErrorCategory, classify_exception
        assert classify_exception(Exception("HTTP 503 unavailable")) == \
            FideErrorCategory.HTTP_ERROR
        assert classify_exception(Exception("429 too many requests")) == \
            FideErrorCategory.HTTP_RATE_LIMIT
