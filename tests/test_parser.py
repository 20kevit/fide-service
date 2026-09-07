"""Parser + validation tests on small fixtures (no network, no big files)."""
import io
import zipfile

import pytest

from domain import xml_validation
from domain.errors import FideError, FideErrorCategory
from domain.parser import parse_fide_xml


class TestParser:
    def test_standard_file_all_federations(self, fixture_path):
        rows = list(parse_fide_xml(
            fixture_path("small_standard.xml"), rating_type="standard"))
        ids = [r.fide_id for r in rows]
        # Inactive + foreign rows included; blank IDs skipped (see below).
        assert ids == ["12512294", "12512295", "99900001", "12512296"]

    def test_federation_filter(self, fixture_path):
        rows = list(parse_fide_xml(
            fixture_path("small_standard.xml"),
            allowed_federations=["IRI"], rating_type="standard"))
        assert {r.fide_id for r in rows} == {
            "12512294", "12512295", "12512296"}

    def test_rating_type_isolation(self, fixture_path):
        rows = {r.fide_id: r for r in parse_fide_xml(
            fixture_path("small_standard.xml"),
            rating_type="rapid")}
        assert rows["12512294"].rating_rapid == 2550  # explicit rapid tags
        assert rows["12512294"].rating_standard == 0  # never leaks across
        generic = {r.fide_id: r for r in parse_fide_xml(
            fixture_path("small_rapid.xml"), rating_type="rapid")}
        assert generic["12512294"].rating_rapid == 2560  # generic tags used
        assert generic["12512294"].rating_standard == 0

    def test_titles_and_inactive(self, fixture_path):
        rows = {r.fide_id: r for r in parse_fide_xml(
            fixture_path("small_standard.xml"), rating_type="standard")}
        assert rows["12512294"].title == "GM"
        assert rows["12512295"].wtitle == "WIM"
        assert rows["12512296"].inactive is True
        assert rows["12512294"].birth_year == "1999"

    def test_bad_int_tolerated(self, fixture_path):
        rows = list(parse_fide_xml(
            fixture_path("small_standard.xml"), rating_type="standard"))
        assert all(isinstance(r.rating_standard, int) for r in rows)

    def test_unknown_rating_type_rejected(self, fixture_path):
        with pytest.raises(ValueError):
            list(parse_fide_xml(
                fixture_path("small_standard.xml"),
                rating_type="classical"))

    def test_blank_fideid_skipped(self):
        blob = (b"<playerslist>"
                b"<player><fideid></fideid><name>No Id</name>"
                b"<country>IRI</country></player>"
                b"<player><fideid>7</fideid><name>Has Id</name>"
                b"<country>IRI</country></player></playerslist>")
        rows = list(parse_fide_xml(io.BytesIO(blob), rating_type="standard"))
        assert [r.fide_id for r in rows] == ["7"]

    def test_stream_input(self, fixture_path):
        with open(fixture_path("small_rapid.xml"), "rb") as fh:
            rows = list(parse_fide_xml(fh, rating_type="rapid"))
        assert len(rows) == 3


class TestXmlValidation:
    def test_valid_file(self, fixture_path):
        result = xml_validation.validate_xml_path(
            fixture_path("small_standard.xml"))
        assert result.sample_ok and result.players_found == 4

    def test_malformed(self, fixture_path):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(fixture_path("malformed.xml"))
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_wrong_root(self, fixture_path):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(fixture_path("wrong_root.xml"))
        assert exc.value.category == FideErrorCategory.INVALID_FIDE_FORMAT

    def test_html_disguised(self, fixture_path):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(fixture_path("html_page.xml"))
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_xxe_rejected(self, fixture_path):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path(fixture_path("xxe.xml"))
        assert exc.value.category == FideErrorCategory.INVALID_XML

    def test_missing_file(self):
        with pytest.raises(FideError) as exc:
            xml_validation.validate_xml_path("/no/such/file.xml")
        assert exc.value.category == FideErrorCategory.FILE_NOT_FOUND

    def test_stream_factory_two_passes(self, fixture_path):
        path = fixture_path("small_rapid.xml")
        result = xml_validation.validate_stream(
            lambda: open(path, "rb"), "small_rapid.xml")
        assert result.sample_ok and result.players_found == 3

    def test_zip_member_stream(self, fixture_path, tmp_path):
        zpath = str(tmp_path / "m.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.write(fixture_path("small_rapid.xml"), "players_list.xml")
        from infrastructure import storage
        member, _ = storage.inspect_zip(zpath)
        result = xml_validation.validate_stream(
            lambda: storage.open_zip_member(zpath, member), "m.zip#xml")
        assert result.sample_ok
