from pathlib import Path

import pytest
from PIL import Image
from pynput.keyboard import Key, KeyCode

from ebook_to_pdf.app import BORDER_THICKNESS, border_geometries, tk_geometry
from ebook_to_pdf.core import (
    AABB,
    AppSettings,
    RecoveryManifest,
    format_duration,
    image_signature,
    load_recovery_manifest,
    load_settings,
    normalize_pdf_filename,
    parse_aabb,
    parse_key,
    save_settings,
    save_recovery_manifest,
    signature_difference_percent,
    validate_capture_options,
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


@pytest.mark.parametrize("thickness", [1, 4, 12, 20])
def test_configurable_border_never_overlaps_capture(thickness: int) -> None:
    region = AABB(-300, 10, 700, 810)
    for width, height, x, y in border_geometries(region, thickness).values():
        assert not (
            x < region.x2
            and x + width > region.x1
            and y < region.y2
            and y + height > region.y1
        )


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


def test_capture_option_validation() -> None:
    assert validate_capture_options("10", "4") == (10.0, 4)
    with pytest.raises(ValueError):
        validate_capture_options("0.1", "4")
    with pytest.raises(ValueError):
        validate_capture_options("10", "21")


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


def test_image_change_score_and_duration() -> None:
    black = Image.new("RGB", (100, 80), "black")
    white = Image.new("RGB", (100, 80), "white")
    black_signature = image_signature(black)
    assert signature_difference_percent(black_signature, black_signature) == 0
    assert signature_difference_percent(black_signature, image_signature(white)) == 100
    assert format_duration(65) == "01:05"
    assert format_duration(3665) == "1:01:05"
    black.close()
    white.close()


def test_recovery_manifest_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    expected = RecoveryManifest(
        output_path="C:/output/book.pdf",
        captured_files=["page-00000001.png", "page-00000002.png"],
        next_page=3,
        total_pages=10,
        settings={"x1": "100", "next_key": "→"},
    )
    save_recovery_manifest(path, expected)
    assert load_recovery_manifest(path) == expected


def test_invalid_recovery_manifest_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text('{"version": 99}', encoding="utf-8")
    assert load_recovery_manifest(path) is None
