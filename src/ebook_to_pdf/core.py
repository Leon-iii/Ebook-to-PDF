from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pynput.keyboard import Key, KeyCode


@dataclass(frozen=True, slots=True)
class AABB:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def validate(self) -> None:
        if self.x2 <= self.x1:
            raise ValueError("x2는 x1보다 커야 합니다.")
        if self.y2 <= self.y1:
            raise ValueError("y2는 y1보다 커야 합니다.")
        if self.width < 10 or self.height < 10:
            raise ValueError("캡처 영역의 폭과 높이는 각각 10px 이상이어야 합니다.")

    def as_mss_monitor(self) -> dict[str, int]:
        self.validate()
        return {
            "left": self.x1,
            "top": self.y1,
            "width": self.width,
            "height": self.height,
        }


@dataclass(slots=True)
class AppSettings:
    x1: str = "100"
    y1: str = "100"
    x2: str = "1100"
    y2: str = "1500"
    total_pages: str = "1"
    current_page: str = "1"
    next_key: str = "→"
    filename: str = "ebook.pdf"
    output_dir: str = ""
    startup_delay: str = "3.0"
    page_delay: str = "1.0"

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "AppSettings":
        defaults = cls()
        values = {
            name: str(raw.get(name, getattr(defaults, name)))
            for name in asdict(defaults)
        }
        return cls(**values)


def load_settings(path: Path) -> AppSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("settings root is not an object")
        return AppSettings.from_mapping(raw)
    except (OSError, ValueError, TypeError):
        return AppSettings()


def save_settings(path: Path, settings: AppSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_aabb(values: tuple[str, str, str, str]) -> AABB:
    try:
        aabb = AABB(*(int(value.strip()) for value in values))
    except ValueError as exc:
        raise ValueError("AABB 좌표는 정수로 입력해 주세요.") from exc
    aabb.validate()
    return aabb


def validate_pages(current_text: str, total_text: str) -> tuple[int, int]:
    try:
        current = int(current_text.strip())
        total = int(total_text.strip())
    except ValueError as exc:
        raise ValueError("현재 페이지와 총 페이지는 정수여야 합니다.") from exc
    if total < 1:
        raise ValueError("총 페이지는 1 이상이어야 합니다.")
    if not 1 <= current <= total:
        raise ValueError("현재 페이지는 1 이상, 총 페이지 이하여야 합니다.")
    return current, total


def validate_delays(startup_text: str, page_text: str) -> tuple[float, float]:
    try:
        startup = float(startup_text.strip())
        page = float(page_text.strip())
    except ValueError as exc:
        raise ValueError("대기 시간은 숫자로 입력해 주세요.") from exc
    if not 0 <= startup <= 60:
        raise ValueError("시작 대기 시간은 0~60초여야 합니다.")
    if not 0.05 <= page <= 60:
        raise ValueError("페이지 전환 대기 시간은 0.05~60초여야 합니다.")
    return startup, page


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def normalize_pdf_filename(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("저장할 파일 이름을 입력해 주세요.")
    if _INVALID_FILENAME.search(name):
        raise ValueError('파일 이름에는 < > : " / \\ | ? * 문자를 사용할 수 없습니다.')
    name = name.rstrip(". ")
    if not name:
        raise ValueError("유효한 파일 이름을 입력해 주세요.")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if Path(name).stem.upper() in _RESERVED_NAMES:
        raise ValueError("Windows 예약 이름은 파일 이름으로 사용할 수 없습니다.")
    return name


_KEY_ALIASES: dict[str, Key] = {
    "→": Key.right,
    "right": Key.right,
    "오른쪽": Key.right,
    "←": Key.left,
    "left": Key.left,
    "왼쪽": Key.left,
    "↑": Key.up,
    "up": Key.up,
    "위": Key.up,
    "↓": Key.down,
    "down": Key.down,
    "아래": Key.down,
    "space": Key.space,
    "스페이스": Key.space,
    "enter": Key.enter,
    "엔터": Key.enter,
    "return": Key.enter,
    "pagedown": Key.page_down,
    "page down": Key.page_down,
    "pgdn": Key.page_down,
    "pageup": Key.page_up,
    "page up": Key.page_up,
    "pgup": Key.page_up,
    "home": Key.home,
    "end": Key.end,
}


def parse_key(value: str) -> Key | KeyCode:
    text = value.strip()
    if not text:
        raise ValueError("다음 페이지 키를 입력해 주세요.")
    alias = _KEY_ALIASES.get(text.casefold())
    if alias is not None:
        return alias
    function_match = re.fullmatch(r"f(\d{1,2})", text.casefold())
    if function_match:
        number = int(function_match.group(1))
        if 1 <= number <= 20:
            return getattr(Key, f"f{number}")
    if len(text) == 1:
        return KeyCode.from_char(text)
    raise ValueError("지원 키: 화살표, Space, Enter, PageDown, PageUp, Home, End, F1~F20, 단일 문자")

