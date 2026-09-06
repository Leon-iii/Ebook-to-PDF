from pathlib import Path

import pytest
from pynput.keyboard import Key, KeyCode

from ebook_to_pdf.app import BORDER_THICKNESS, border_geometries, tk_geometry
from ebook_to_pdf.core import (
    AABB,
    AppSettings,
    load_settings,
    normalize_pdf_filename,
    parse_aabb,
    parse_key,
    save_settings,
    validate_delays,
    validate_pages,
)


def test_aabb_dimensions_and_monitor() -> None:
    region = AABB(-20, 10, 180, 310)
    assert region.width == 200
    assert region.height == 300
    assert region.as_mss_monitor() == {"left": -20, "top": 10, "width": 200, "height": 300}


def test_tk_geometry_keeps_absolute_negative_coordinates() -> None:
    assert tk_geometry(800, 600, -1920, -40) == "800x600+-1920+-40"


def test_red_border_is_strictly_outside_capture_aabb() -> None:
    region = AABB(100, 200, 500, 800)
    geometries = border_geometries(region)

    assert geometries == {
        "top": (408, 4, 96, 196),
        "bottom": (408, 4, 96, 800),
        "left": (4, 600, 96, 200),
        "right": (4, 600, 500, 200),
    }

    for width, height, x, y in geometries.values():
        border_x2 = x + width
        border_y2 = y + height
        overlaps_capture = (
            x < region.x2
            and border_x2 > region.x1
            and y < region.y2
            and border_y2 > region.y1
        )
        assert not overlaps_capture
        assert width >= BORDER_THICKNESS
        assert height >= BORDER_THICKNESS


@pytest.mark.parametrize("values", [("a", "0", "1", "1"), ("2", "0", "1", "1"), ("0", "3", "1", "2")])
def test_parse_aabb_rejects_invalid_values(values: tuple[str, str, str, str]) -> None:
    with pytest.raises(ValueError):
        parse_aabb(values)


def test_page_validation() -> None:
    assert validate_pages("2", "5") == (2, 5)
    with pytest.raises(ValueError):
        validate_pages("0", "5")
    with pytest.raises(ValueError):
        validate_pages("6", "5")


def test_delay_validation() -> None:
    assert validate_delays("3", "0.5") == (3.0, 0.5)
    with pytest.raises(ValueError):
        validate_delays("-1", "1")


def test_filename_normalization() -> None:
    assert normalize_pdf_filename(" my book ") == "my book.pdf"
    assert normalize_pdf_filename("book.PDF") == "book.PDF"
    with pytest.raises(ValueError):
        normalize_pdf_filename("bad:name")
    with pytest.raises(ValueError):
        normalize_pdf_filename("CON.pdf")


def test_key_parser() -> None:
    assert parse_key("→") == Key.right
    assert parse_key("PageDown") == Key.page_down
    assert parse_key("F12") == Key.f12
    parsed = parse_key("n")
    assert isinstance(parsed, KeyCode)
    assert parsed.char == "n"
    with pytest.raises(ValueError):
        parse_key("Ctrl+Right")


def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    expected = AppSettings(x1="42", filename="테스트.pdf", page_delay="1.5")
    save_settings(path, expected)
    assert load_settings(path) == expected


def test_broken_settings_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("not-json", encoding="utf-8")
    assert load_settings(path) == AppSettings()
