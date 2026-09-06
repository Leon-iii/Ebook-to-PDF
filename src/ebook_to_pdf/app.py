from __future__ import annotations

import ctypes
import queue
import shutil
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Callable

import img2pdf
import mss
from PIL import Image, ImageTk
from platformdirs import user_config_path, user_data_path
from pynput import keyboard

from .core import (
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


APP_NAME = "E-book 화면 캡처 PDF"
BORDER_THICKNESS = 4
HANDLE_SIZE = 18
CHANGE_THRESHOLD_PERCENT = 0.005
DUPLICATE_THRESHOLD_PERCENT = 0.001
STABLE_THRESHOLD_PERCENT = 0.003
WINDOW_RESTORE = 9
GA_ROOT = 2


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str

    @property
    def display(self) -> str:
        shortened = self.title if len(self.title) <= 72 else self.title[:69] + "…"
        return f"{shortened}  [0x{self.hwnd:X}]"


@dataclass(frozen=True, slots=True)
class RunValues:
    aabb: AABB
    current: int
    total: int
    next_key: object
    startup_delay: float
    page_delay: float
    change_timeout: float
    detect_change: bool
    detect_duplicates: bool
    output_path: Path
    target: WindowInfo


def list_visible_windows(excluded_hwnd: int | None = None) -> list[WindowInfo]:
    user32 = ctypes.windll.user32
    windows: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if excluded_hwnd and hwnd == excluded_hwnd:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title and title != APP_NAME:
            windows.append(WindowInfo(int(hwnd), title))
        return True

    callback_ref = callback_type(callback)
    user32.EnumWindows(callback_ref, 0)
    return sorted(windows, key=lambda item: item.title.casefold())


def window_exists(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsWindow(wintypes.HWND(hwnd)))


def focus_window(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    if not window_exists(hwnd):
        return False
    if user32.IsIconic(wintypes.HWND(hwnd)):
        user32.ShowWindow(wintypes.HWND(hwnd), WINDOW_RESTORE)
    user32.BringWindowToTop(wintypes.HWND(hwnd))
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    return True


def is_foreground_window(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.restype = wintypes.HWND
    foreground = user32.GetForegroundWindow()
    foreground_root = user32.GetAncestor(foreground, GA_ROOT) if foreground else 0
    return bool(foreground_root == hwnd or foreground == hwnd)


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def tk_geometry(width: int, height: int, x: int, y: int) -> str:
    """Use `+-N` so Tk treats negative multi-monitor positions as absolute."""
    return f"{width}x{height}+{x}+{y}"


def border_geometries(aabb: AABB, thickness: int = BORDER_THICKNESS) -> dict[str, tuple[int, int, int, int]]:
    """Return (width, height, x, y) rectangles strictly outside the AABB."""
    return {
        "top": (aabb.width + 2 * thickness, thickness, aabb.x1 - thickness, aabb.y1 - thickness),
        "bottom": (aabb.width + 2 * thickness, thickness, aabb.x1 - thickness, aabb.y2),
        "left": (thickness, aabb.height, aabb.x1 - thickness, aabb.y1),
        "right": (thickness, aabb.height, aabb.x2, aabb.y1),
    }


def configure_dpi_awareness() -> None:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def virtual_screen_bounds() -> AABB:
    user32 = ctypes.windll.user32
    x = user32.GetSystemMetrics(76)
    y = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    return AABB(x, y, x + width, y + height)


class BorderOverlay:
    """Four small windows form a click-through interior and draggable border."""

    def __init__(self, root: tk.Tk, on_change: Callable[[AABB], None]) -> None:
        self.root = root
        self.on_change = on_change
        self.aabb: AABB | None = None
        self.color = "#e02020"
        self.thickness = BORDER_THICKNESS
        self.windows: dict[str, tk.Toplevel] = {}
        self._drag_origin: tuple[int, int] | None = None
        self._drag_aabb: AABB | None = None
        self._drag_edges = ""
        for edge in ("top", "bottom", "left", "right"):
            window = tk.Toplevel(root)
            window.withdraw()
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(bg=self.color, cursor=self._cursor_for(edge))
            window.bind("<ButtonPress-1>", lambda event, e=edge: self._start_resize(event, e))
            window.bind("<B1-Motion>", self._resize)
            window.bind("<ButtonRelease-1>", self._end_resize)
            self.windows[edge] = window
        self.size_label = tk.Toplevel(root)
        self.size_label.withdraw()
        self.size_label.overrideredirect(True)
        self.size_label.attributes("-topmost", True)
        self.size_text = tk.Label(
            self.size_label,
            bg=self.color,
            fg="white",
            padx=7,
            pady=2,
            font=("Malgun Gothic", 9, "bold"),
        )
        self.size_text.pack(fill="both", expand=True)

    @staticmethod
    def _cursor_for(edge: str) -> str:
        return "sb_v_double_arrow" if edge in {"top", "bottom"} else "sb_h_double_arrow"

    def set(self, aabb: AABB | None) -> None:
        self.aabb = aabb
        if aabb is None:
            self.hide()
            return
        self._position_windows()
        self.show()

    def set_style(self, color: str, thickness: int) -> None:
        self.root.winfo_rgb(color)
        self.color = color
        self.thickness = thickness
        for window in self.windows.values():
            window.configure(bg=color)
        self.size_label.configure(bg=color)
        self.size_text.configure(bg=color)
        self._position_windows()

    def _position_windows(self) -> None:
        if self.aabb is None:
            return
        a = self.aabb
        geometries = border_geometries(a, self.thickness)
        for edge, (width, height, x, y) in geometries.items():
            self.windows[edge].geometry(tk_geometry(width, height, x, y))
        self.size_text.configure(text=f"{a.width} × {a.height}px")
        self.size_label.update_idletasks()
        label_width = self.size_label.winfo_reqwidth()
        label_height = self.size_label.winfo_reqheight()
        screen = virtual_screen_bounds()
        label_y = a.y1 - self.thickness - label_height - 3
        if label_y < screen.y1:
            label_y = a.y2 + self.thickness + 3
        label_x = min(max(a.x1, screen.x1), max(screen.x1, screen.x2 - label_width))
        self.size_label.geometry(tk_geometry(label_width, label_height, label_x, label_y))

    def show(self) -> None:
        if self.aabb is None:
            return
        for window in self.windows.values():
            window.deiconify()
            window.lift()
        self.size_label.deiconify()
        self.size_label.lift()

    def hide(self) -> None:
        for window in self.windows.values():
            window.withdraw()
        self.size_label.withdraw()

    def destroy(self) -> None:
        for window in self.windows.values():
            window.destroy()
        self.size_label.destroy()
        self.windows.clear()

    def _start_resize(self, event: tk.Event, edge: str) -> None:
        if self.aabb is None:
            return
        self._drag_origin = (event.x_root, event.y_root)
        self._drag_aabb = self.aabb
        self._drag_edges = edge
        if edge in {"top", "bottom"}:
            if event.x <= HANDLE_SIZE:
                self._drag_edges += "+left"
            elif event.x >= event.widget.winfo_width() - HANDLE_SIZE:
                self._drag_edges += "+right"
        else:
            if event.y <= HANDLE_SIZE:
                self._drag_edges += "+top"
            elif event.y >= event.widget.winfo_height() - HANDLE_SIZE:
                self._drag_edges += "+bottom"

    def _resize(self, event: tk.Event) -> None:
        if self._drag_origin is None or self._drag_aabb is None:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        old = self._drag_aabb
        x1, y1, x2, y2 = old.x1, old.y1, old.x2, old.y2
        if "left" in self._drag_edges:
            x1 = min(x1 + dx, x2 - 10)
        if "right" in self._drag_edges:
            x2 = max(x2 + dx, x1 + 10)
        if "top" in self._drag_edges:
            y1 = min(y1 + dy, y2 - 10)
        if "bottom" in self._drag_edges:
            y2 = max(y2 + dy, y1 + 10)
        self.aabb = AABB(x1, y1, x2, y2)
        self._position_windows()
        self.on_change(self.aabb)

    def _end_resize(self, _event: tk.Event) -> None:
        self._drag_origin = None
        self._drag_aabb = None
        self._drag_edges = ""


class RegionSelector:
    def __init__(
        self,
        root: tk.Tk,
        bounds: AABB,
        on_selected: Callable[[AABB], None],
        on_cancel: Callable[[], None],
        color: str = "#e02020",
    ) -> None:
        self.root = root
        self.bounds = bounds
        self.on_selected = on_selected
        self.on_cancel = on_cancel
        self.color = color
        self.start: tuple[int, int] | None = None
        self.rectangle: int | None = None

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.28)
        self.window.geometry(tk_geometry(bounds.width, bounds.height, bounds.x1, bounds.y1))
        self.canvas = tk.Canvas(
            self.window,
            bg="#18202a",
            cursor="crosshair",
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            bounds.width // 2,
            42,
            text="캡처할 영역을 드래그하세요 · ESC 취소",
            fill="white",
            font=("Malgun Gothic", 16, "bold"),
        )
        self.coordinate_text = self.canvas.create_text(
            14,
            14,
            anchor="nw",
            text="마우스: 0, 0",
            fill="white",
            font=("Consolas", 12, "bold"),
        )
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._motion)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.focus_force()
        self.window.grab_set_global()

    def _press(self, event: tk.Event) -> None:
        self.start = (event.x, event.y)
        if self.rectangle is not None:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline=self.color,
            width=5,
            fill="#ffffff",
        )

    def _drag(self, event: tk.Event) -> None:
        if self.start is not None and self.rectangle is not None:
            self.canvas.coords(self.rectangle, *self.start, event.x, event.y)

    def _motion(self, event: tk.Event) -> None:
        absolute_x = self.bounds.x1 + event.x
        absolute_y = self.bounds.y1 + event.y
        self.canvas.itemconfigure(
            self.coordinate_text,
            text=f"마우스: {absolute_x}, {absolute_y}",
        )

    def _release(self, event: tk.Event) -> None:
        if self.start is None:
            return
        x1, x2 = sorted((self.start[0], event.x))
        y1, y2 = sorted((self.start[1], event.y))
        selected = AABB(
            self.bounds.x1 + x1,
            self.bounds.y1 + y1,
            self.bounds.x1 + x2,
            self.bounds.y1 + y2,
        )
        try:
            selected.validate()
        except ValueError:
            self.cancel()
            return
        self._close()
        self.on_selected(selected)

    def cancel(self) -> None:
        self._close()
        self.on_cancel()

    def _close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class EbookToPdfApp:
    def __init__(
        self,
        root: tk.Tk,
        config_path: Path | None = None,
        recovery_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("760x720")
        self.root.minsize(700, 660)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.config_path = config_path or user_config_path("ebook-to-pdf") / "settings.json"
        self.recovery_dir = recovery_dir or user_data_path("ebook-to-pdf") / "recovery"
        self.recovery_manifest_path = self.recovery_dir / "session.json"
        settings = load_settings(self.config_path)
        if not settings.output_dir:
            documents = Path.home() / "Documents"
            settings.output_dir = str(documents if documents.is_dir() else Path.home())
        self.vars = {
            name: tk.StringVar(value=value)
            for name, value in asdict(settings).items()
        }
        self.status_var = tk.StringVar(value="대기 중")
        self.mouse_position_var = tk.StringVar(value="마우스: 0, 0")
        self.target_display_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)
        self._save_job: str | None = None
        self._region_job: str | None = None
        self._selector: RegionSelector | None = None
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._capture_active = False
        self._closing = False
        self._session_dir: Path | None = None
        self._captured_files: list[Path] = []
        self._output_path: Path | None = None
        self._recovery_pending = False
        self._job_expected_count = 0
        self._job_initial_count = 0
        self._capture_started_at = 0.0
        self._session_settings: dict[str, str] = {}
        self._active_aabb: AABB | None = None
        self._aabb_lock = threading.Lock()
        self._target_hwnd: int | None = None
        self._window_options: dict[str, WindowInfo] = {}
        self._preview_image: ImageTk.PhotoImage | None = None
        self._ui_queue: queue.Queue[tuple[Callable[..., None], tuple[object, ...]]] = queue.Queue()

        self._build_ui()
        self.border = BorderOverlay(root, self._on_border_changed)
        self._apply_border_style()
        for variable in self.vars.values():
            variable.trace_add("write", self._field_changed)
        self.root.after(150, self._refresh_border_from_fields)
        self.root.after(120, self.refresh_target_windows)
        self.root.after(50, self._update_mouse_position)
        self.root.after(350, self._offer_recovery)

        self._key_listener = keyboard.Listener(on_press=self._global_key_press)
        self._key_listener.daemon = True
        self._key_listener.start()
        self.root.after(40, self._drain_ui_queue)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text=APP_NAME, font=("Malgun Gothic", 17, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        region = ttk.LabelFrame(outer, text="1. 캡처 영역 (화면 절대 좌표)", padding=12)
        region.grid(row=1, column=0, sticky="ew", pady=4)
        for index in range(8):
            region.columnconfigure(index, weight=1 if index in {1, 3, 5, 7} else 0)
        for index, name in enumerate(("x1", "y1", "x2", "y2")):
            ttk.Label(region, text=name).grid(row=0, column=index * 2, padx=(0, 4))
            ttk.Entry(region, textvariable=self.vars[name], width=8).grid(
                row=0, column=index * 2 + 1, sticky="ew", padx=(0, 9)
            )
        region_actions = ttk.Frame(region)
        region_actions.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(9, 0))
        ttk.Button(region_actions, text="드래그로 지정", command=self.start_region_selection).pack(side="left")
        ttk.Button(region_actions, text="시험 캡처", command=self.trial_capture).pack(side="left", padx=(6, 14))
        ttk.Label(region_actions, textvariable=self.mouse_position_var, font=("Consolas", 10)).pack(side="left")
        ttk.Label(region_actions, text="테두리").pack(side="left", padx=(18, 4))
        ttk.Entry(region_actions, textvariable=self.vars["border_color"], width=9).pack(side="left")
        ttk.Button(region_actions, text="색상", command=self.choose_border_color, width=5).pack(side="left", padx=(4, 10))
        ttk.Label(region_actions, text="굵기").pack(side="left", padx=(0, 4))
        ttk.Spinbox(
            region_actions,
            from_=1,
            to=20,
            textvariable=self.vars["border_thickness"],
            width=4,
        ).pack(side="left")
        ttk.Label(
            region,
            text="테두리는 캡처 영역 바깥에만 표시됩니다. 변 또는 모서리를 드래그해 미세 조정할 수 있습니다.",
            foreground="#555555",
        ).grid(row=2, column=0, columnspan=8, sticky="w", pady=(8, 0))

        pages = ttk.LabelFrame(outer, text="2. 페이지", padding=12)
        pages.grid(row=2, column=0, sticky="ew", pady=4)
        pages.columnconfigure(1, weight=1)
        pages.columnconfigure(3, weight=1)
        pages.columnconfigure(5, weight=1)
        ttk.Label(pages, text="현재 페이지").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(pages, textvariable=self.vars["current_page"], width=10).grid(row=0, column=1, sticky="ew", padx=(0, 14))
        ttk.Label(pages, text="총 페이지").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(pages, textvariable=self.vars["total_pages"], width=10).grid(row=0, column=3, sticky="ew", padx=(0, 14))
        ttk.Label(pages, text="다음 페이지 키").grid(row=0, column=4, padx=(0, 6))
        ttk.Entry(pages, textvariable=self.vars["next_key"], width=12).grid(row=0, column=5, sticky="ew")

        timing = ttk.LabelFrame(outer, text="3. 타이밍 및 검사", padding=12)
        timing.grid(row=3, column=0, sticky="ew", pady=4)
        timing.columnconfigure(1, weight=1)
        timing.columnconfigure(3, weight=1)
        ttk.Label(timing, text="시작 대기 (초)").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(timing, textvariable=self.vars["startup_delay"], width=10).grid(row=0, column=1, sticky="ew", padx=(0, 20))
        ttk.Label(timing, text="페이지 전환 대기 (초)").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(timing, textvariable=self.vars["page_delay"], width=10).grid(row=0, column=3, sticky="ew")
        checks = ttk.Frame(timing)
        checks.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(9, 0))
        ttk.Checkbutton(
            checks, text="화면 변경 감지", variable=self.vars["change_detection"], onvalue="1", offvalue="0"
        ).pack(side="left")
        ttk.Checkbutton(
            checks, text="중복 페이지 감지", variable=self.vars["duplicate_detection"], onvalue="1", offvalue="0"
        ).pack(side="left", padx=(12, 18))
        ttk.Label(checks, text="변경 최대 대기 (초)").pack(side="left")
        ttk.Entry(checks, textvariable=self.vars["change_timeout"], width=8).pack(side="left", padx=(6, 0))

        target = ttk.LabelFrame(outer, text="4. 대상 e-book 창", padding=12)
        target.grid(row=4, column=0, sticky="ew", pady=4)
        target.columnconfigure(0, weight=1)
        self.target_combo = ttk.Combobox(target, textvariable=self.target_display_var, state="readonly")
        self.target_combo.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.target_combo.bind("<<ComboboxSelected>>", self._target_selected)
        ttk.Button(target, text="창 목록 새로고침", command=self.refresh_target_windows).grid(row=0, column=1)
        ttk.Label(
            target,
            text="실행·재개 시 선택한 창을 앞으로 가져오고, 포커스가 벗어나면 자동 일시정지합니다.",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))

        output = ttk.LabelFrame(outer, text="5. 저장", padding=12)
        output.grid(row=5, column=0, sticky="ew", pady=4)
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="파일 이름").grid(row=0, column=0, sticky="w", padx=(0, 7), pady=(0, 8))
        ttk.Entry(output, textvariable=self.vars["filename"]).grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(output, text="저장 폴더").grid(row=1, column=0, sticky="w", padx=(0, 7))
        ttk.Entry(output, textvariable=self.vars["output_dir"]).grid(row=1, column=1, sticky="ew", padx=(0, 7))
        ttk.Button(output, text="찾아보기", command=self.choose_output_dir).grid(row=1, column=2)

        controls = ttk.Frame(outer)
        controls.grid(row=6, column=0, sticky="ew", pady=(12, 7))
        controls.columnconfigure((0, 1, 2), weight=1)
        self.run_button = ttk.Button(controls, text="실행", command=self.start_or_resume)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 5), ipady=6)
        self.pause_button = ttk.Button(controls, text="일시정지", command=self.pause, state="disabled")
        self.pause_button.grid(row=0, column=1, sticky="ew", padx=5, ipady=6)
        self.stop_button = ttk.Button(controls, text="정지", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=(5, 0), ipady=6)

        self.progress = ttk.Progressbar(outer, variable=self.progress_var, maximum=100)
        self.progress.grid(row=7, column=0, sticky="ew", pady=(3, 7))
        ttk.Label(outer, textvariable=self.status_var, wraplength=700).grid(row=8, column=0, sticky="w")
        ttk.Label(
            outer,
            text="실행 중 ESC: 일시정지 · 정지하면 지금까지 캡처한 페이지를 PDF로 저장합니다.",
            foreground="#555555",
        ).grid(row=9, column=0, sticky="w", pady=(7, 0))

    def _field_changed(self, *_args: object) -> None:
        if self._closing:
            return
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(350, self._save_settings)
        if self._region_job:
            self.root.after_cancel(self._region_job)
        self._region_job = self.root.after(250, self._refresh_border_from_fields)

    def _settings(self) -> AppSettings:
        return AppSettings(**{name: variable.get() for name, variable in self.vars.items()})

    def _save_settings(self) -> None:
        self._save_job = None
        try:
            save_settings(self.config_path, self._settings())
        except OSError as exc:
            self.status_var.set(f"설정 저장 실패: {exc}")

    def _update_mouse_position(self) -> None:
        if self._closing:
            return
        x, y = cursor_position()
        self.mouse_position_var.set(f"마우스: {x}, {y}")
        self.root.after(50, self._update_mouse_position)

    def choose_border_color(self) -> None:
        selected = colorchooser.askcolor(
            color=self.vars["border_color"].get(),
            title="테두리 색상",
            parent=self.root,
        )[1]
        if selected:
            self.vars["border_color"].set(selected)

    def _apply_border_style(self) -> None:
        try:
            _timeout, thickness = validate_capture_options(
                self.vars["change_timeout"].get(),
                self.vars["border_thickness"].get(),
            )
            color = self.vars["border_color"].get().strip()
            self.root.winfo_rgb(color)
            self.border.set_style(color, thickness)
        except (ValueError, tk.TclError):
            return

    def refresh_target_windows(self) -> None:
        if self._closing:
            return
        saved_title = self.vars["target_window_title"].get()
        windows = list_visible_windows(self.root.winfo_id())
        self._window_options = {window.display: window for window in windows}
        displays = list(self._window_options)
        self.target_combo.configure(values=displays)
        selected = next((window for window in windows if window.title == saved_title), None)
        if selected is None and self._target_hwnd is not None:
            selected = next((window for window in windows if window.hwnd == self._target_hwnd), None)
        if selected is not None:
            self._target_hwnd = selected.hwnd
            self.target_display_var.set(selected.display)
            self.vars["target_window_title"].set(selected.title)
        else:
            self._target_hwnd = None
            self.target_display_var.set("")

    def _target_selected(self, _event: tk.Event | None = None) -> None:
        selected = self._window_options.get(self.target_display_var.get())
        if selected is None:
            self._target_hwnd = None
            self.vars["target_window_title"].set("")
            return
        self._target_hwnd = selected.hwnd
        self.vars["target_window_title"].set(selected.title)
        self.status_var.set(f"대상 창 선택: {selected.title}")

    def _selected_target(self) -> WindowInfo:
        selected = self._window_options.get(self.target_display_var.get())
        if selected is None or not window_exists(selected.hwnd):
            raise ValueError("실행할 e-book 창을 선택해 주세요. 창이 보이지 않으면 목록을 새로고침하세요.")
        return selected

    def _refresh_border_from_fields(self) -> None:
        self._region_job = None
        if (self._capture_active and not self._pause_event.is_set()) or self._selector is not None:
            return
        self._apply_border_style()
        try:
            aabb = parse_aabb(tuple(self.vars[name].get() for name in ("x1", "y1", "x2", "y2")))
        except ValueError:
            self.border.set(None)
            return
        if self._capture_active:
            with self._aabb_lock:
                self._active_aabb = aabb
        self.border.set(aabb)

    def _on_border_changed(self, aabb: AABB) -> None:
        if self._capture_active:
            with self._aabb_lock:
                self._active_aabb = aabb
        for name, value in zip(("x1", "y1", "x2", "y2"), (aabb.x1, aabb.y1, aabb.x2, aabb.y2)):
            self.vars[name].set(str(value))

    def start_region_selection(self) -> None:
        if self._capture_active:
            return
        self.border.hide()
        self.root.withdraw()
        self.root.after(120, self._show_selector)

    def _show_selector(self) -> None:
        self._selector = RegionSelector(
            self.root,
            virtual_screen_bounds(),
            self._region_selected,
            self._region_selection_cancelled,
            self.vars["border_color"].get(),
        )

    def _region_selected(self, aabb: AABB) -> None:
        self._selector = None
        self._on_border_changed(aabb)
        self.root.deiconify()
        self.root.lift()
        self.status_var.set(f"영역 지정 완료: {aabb.width} × {aabb.height}px")

    def _region_selection_cancelled(self) -> None:
        self._selector = None
        self.root.deiconify()
        self.root.lift()
        self._refresh_border_from_fields()
        self.status_var.set("영역 지정을 취소했습니다.")

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.vars["output_dir"].get() or None)
        if selected:
            self.vars["output_dir"].set(selected)

    def _offer_recovery(self) -> None:
        if self._closing or self._capture_active:
            return
        manifest = load_recovery_manifest(self.recovery_manifest_path)
        if manifest is None:
            if self.recovery_dir.exists():
                self._clear_recovery()
            return
        files = sorted(self.recovery_dir.glob("page-*.png"))
        if not files or not all(path.is_file() for path in files):
            self._clear_recovery()
            return
        try:
            last_disk_page = int(files[-1].stem.removeprefix("page-"))
        except ValueError:
            last_disk_page = manifest.next_page - 1
        recovered_next_page = max(manifest.next_page, last_disk_page + 1)
        if recovered_next_page > manifest.total_pages:
            recover = messagebox.askyesno(
                "완료 직전 작업 발견",
                f"캡처가 끝난 {len(files)}페이지 작업을 발견했습니다. PDF 생성을 완료할까요?",
                parent=self.root,
            )
            if recover:
                try:
                    output_path = Path(manifest.output_path)
                    self._write_pdf(files, output_path)
                    self._clear_recovery()
                    self.status_var.set(f"복구 PDF 저장 완료: {output_path}")
                    messagebox.showinfo("복구 완료", str(output_path), parent=self.root)
                except Exception as exc:
                    messagebox.showerror("복구 실패", str(exc), parent=self.root)
            else:
                self._clear_recovery()
            return
        recover = messagebox.askyesno(
            "중단된 작업 복구",
            f"이전에 캡처한 {len(files)}페이지가 있습니다.\n"
            f"{recovered_next_page}페이지부터 계속할까요?\n\n"
            "e-book 뷰어에서 표시 중인 페이지를 확인한 뒤 실행하세요.",
            parent=self.root,
        )
        if not recover:
            self._clear_recovery()
            return
        for name, value in manifest.settings.items():
            if name in self.vars:
                self.vars[name].set(value)
        self.vars["current_page"].set(str(recovered_next_page))
        self.vars["total_pages"].set(str(manifest.total_pages))
        output_path = Path(manifest.output_path)
        self.vars["output_dir"].set(str(output_path.parent))
        self.vars["filename"].set(output_path.name)
        self._captured_files = files
        self._session_dir = self.recovery_dir
        self._output_path = output_path
        self._recovery_pending = True
        self.progress_var.set(len(files) / max(len(files) + manifest.total_pages - manifest.next_page + 1, 1) * 100)
        self.status_var.set(
            f"{len(files)}페이지 복구됨 · 뷰어에서 {recovered_next_page}페이지를 연 뒤 실행하세요."
        )
        self.refresh_target_windows()

    def _write_recovery_manifest(self, next_page: int, total_pages: int) -> None:
        if self._session_dir is None or self._output_path is None:
            return
        settings = dict(self._session_settings)
        settings["current_page"] = str(next_page)
        settings["total_pages"] = str(total_pages)
        manifest = RecoveryManifest(
            output_path=str(self._output_path),
            captured_files=[path.name for path in self._captured_files],
            next_page=next_page,
            total_pages=total_pages,
            settings=settings,
        )
        save_recovery_manifest(self.recovery_manifest_path, manifest)

    def _clear_recovery(self) -> None:
        if self.recovery_dir.exists():
            shutil.rmtree(self.recovery_dir, ignore_errors=True)

    def trial_capture(self) -> None:
        if self._capture_active:
            return
        try:
            aabb = self._validated_aabb()
            target = self._selected_target()
        except ValueError as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self.root)
            return
        self.border.hide()
        self.root.iconify()
        self.status_var.set("시험 캡처 준비 중…")
        focus_window(target.hwnd)
        self.root.after(500, self._perform_trial_capture, aabb, target)

    def _perform_trial_capture(self, aabb: AABB, target: WindowInfo) -> None:
        try:
            if not is_foreground_window(target.hwnd):
                raise RuntimeError("선택한 e-book 창에 포커스를 맞추지 못했습니다.")
            with mss.mss() as screenshotter:
                image = self._grab_image(screenshotter, aabb)
        except Exception as exc:
            self.root.deiconify()
            self._refresh_border_from_fields()
            messagebox.showerror("시험 캡처 실패", str(exc), parent=self.root)
            return
        self.root.deiconify()
        self.root.lift()
        self._refresh_border_from_fields()
        self._show_preview(image)
        self.status_var.set(f"시험 캡처 완료: {aabb.width} × {aabb.height}px")

    def _show_preview(self, image: Image.Image) -> None:
        preview = tk.Toplevel(self.root)
        preview.title("시험 캡처 미리보기")
        preview.geometry("860x700")
        preview.minsize(480, 360)
        container = ttk.Frame(preview, padding=12)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text=f"원본 크기: {image.width} × {image.height}px").pack(anchor="w", pady=(0, 8))
        display = image.copy()
        display.thumbnail((820, 620), Image.Resampling.LANCZOS)
        self._preview_image = ImageTk.PhotoImage(display)
        image_label = ttk.Label(container, image=self._preview_image)
        image_label.pack(fill="both", expand=True)
        preview.preview_image = self._preview_image  # type: ignore[attr-defined]
        display.close()
        image.close()

    def _validated_aabb(self) -> AABB:
        aabb = parse_aabb(tuple(self.vars[name].get() for name in ("x1", "y1", "x2", "y2")))
        screen = virtual_screen_bounds()
        if not (
            screen.x1 <= aabb.x1 < aabb.x2 <= screen.x2
            and screen.y1 <= aabb.y1 < aabb.y2 <= screen.y2
        ):
            raise ValueError("캡처 영역은 현재 가상 화면 범위 안에 있어야 합니다.")
        return aabb

    def _validated_run_values(self) -> RunValues:
        aabb = self._validated_aabb()
        current, total = validate_pages(self.vars["current_page"].get(), self.vars["total_pages"].get())
        next_key = parse_key(self.vars["next_key"].get())
        startup_delay, page_delay = validate_delays(self.vars["startup_delay"].get(), self.vars["page_delay"].get())
        change_timeout, _thickness = validate_capture_options(
            self.vars["change_timeout"].get(),
            self.vars["border_thickness"].get(),
        )
        try:
            self.root.winfo_rgb(self.vars["border_color"].get().strip())
        except tk.TclError as exc:
            raise ValueError("올바른 테두리 색상을 입력해 주세요.") from exc
        filename = normalize_pdf_filename(self.vars["filename"].get())
        output_dir = Path(self.vars["output_dir"].get()).expanduser()
        if not output_dir.is_dir():
            raise ValueError("저장 폴더가 존재하지 않습니다.")
        target = self._selected_target()
        return RunValues(
            aabb=aabb,
            current=current,
            total=total,
            next_key=next_key,
            startup_delay=startup_delay,
            page_delay=page_delay,
            change_timeout=change_timeout,
            detect_change=self.vars["change_detection"].get() == "1",
            detect_duplicates=self.vars["duplicate_detection"].get() == "1",
            output_path=output_dir / filename,
            target=target,
        )

    def start_or_resume(self) -> None:
        if self._capture_active:
            if not self._pause_event.is_set():
                return
            try:
                startup_delay, _ = validate_delays(self.vars["startup_delay"].get(), self.vars["page_delay"].get())
                target = self._selected_target()
            except ValueError as exc:
                messagebox.showerror("입력 확인", str(exc), parent=self.root)
                return
            self._target_hwnd = target.hwnd
            self._prepare_background_capture(target.hwnd)
            self.status_var.set(f"{startup_delay:g}초 후 캡처를 재개합니다…")
            self.root.after(int(startup_delay * 1000), self._resume_after_delay, target.hwnd)
            return

        try:
            values = self._validated_run_values()
        except ValueError as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self.root)
            return
        if values.output_path.exists() and not messagebox.askyesno(
            "파일 덮어쓰기",
            f"이미 존재하는 파일입니다. 덮어쓸까요?\n\n{values.output_path}",
            parent=self.root,
        ):
            return

        self.vars["filename"].set(values.output_path.name)
        self._save_settings()
        self._session_settings = {name: variable.get() for name, variable in self.vars.items()}
        self._stop_event.clear()
        self._pause_event.clear()
        self._capture_active = True
        with self._aabb_lock:
            self._active_aabb = values.aabb
        if self._recovery_pending:
            self._session_dir = self.recovery_dir
            self._recovery_pending = False
        else:
            self._clear_recovery()
            self.recovery_dir.mkdir(parents=True, exist_ok=True)
            self._session_dir = self.recovery_dir
            self._captured_files = []
        self._output_path = values.output_path
        self._target_hwnd = values.target.hwnd
        self._job_initial_count = len(self._captured_files)
        self._job_expected_count = self._job_initial_count + (values.total - values.current + 1)
        self._capture_started_at = time.monotonic()
        initial_progress = (
            len(self._captured_files) / self._job_expected_count * 100
            if self._job_expected_count
            else 0
        )
        self.progress_var.set(initial_progress)
        self._set_control_state(running=True, paused=False)
        self._write_recovery_manifest(values.current, values.total)
        self._prepare_background_capture(values.target.hwnd)
        self.status_var.set(f"{values.startup_delay:g}초 후 {values.current}페이지부터 캡처합니다…")
        self._worker = threading.Thread(
            target=self._capture_worker,
            args=(values,),
            daemon=True,
        )
        self._worker.start()

    def _prepare_background_capture(self, target_hwnd: int) -> None:
        self.border.hide()
        self.root.iconify()
        self.root.update_idletasks()
        focus_window(target_hwnd)

    def _resume_after_delay(self, target_hwnd: int) -> None:
        if self._capture_active and not self._stop_event.is_set():
            focus_window(target_hwnd)
            if not is_foreground_window(target_hwnd):
                self._show_paused_ui("선택한 e-book 창에 포커스를 맞추지 못했습니다.")
                return
            self._job_initial_count = len(self._captured_files)
            self._capture_started_at = time.monotonic()
            self._pause_event.clear()
            self._set_control_state(running=True, paused=False)

    def pause(self) -> None:
        if not self._capture_active or self._pause_event.is_set():
            return
        self._pause_event.set()
        self.root.after(0, self._show_paused_ui)

    def _show_paused_ui(self, reason: str | None = None) -> None:
        if not self._capture_active:
            return
        self._pause_event.set()
        self.root.deiconify()
        self.root.lift()
        self._refresh_border_from_fields()
        self.status_var.set(reason or "일시정지됨 · 실행 버튼을 누르면 재개합니다.")
        self._set_control_state(running=True, paused=True)

    def stop(self) -> None:
        if not self._capture_active:
            return
        self._stop_event.set()
        self._pause_event.clear()
        self.status_var.set("정지 중… 지금까지 캡처한 페이지를 PDF로 정리합니다.")
        self.stop_button.configure(state="disabled")

    def _global_key_press(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if key != keyboard.Key.esc:
            return
        self._queue_ui(self._handle_global_escape)

    def _handle_global_escape(self) -> None:
        if self._selector is not None:
            self._selector.cancel()
        elif self._capture_active and not self._pause_event.is_set():
            self._pause_event.set()
            self._show_paused_ui()

    def _queue_ui(self, callback: Callable[..., None], *args: object) -> None:
        self._ui_queue.put((callback, args))

    def _drain_ui_queue(self) -> None:
        if self._closing and not self._capture_active:
            return
        while True:
            try:
                callback, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        self.root.after(40, self._drain_ui_queue)

    def _interruptible_wait(self, seconds: float, honor_pause: bool = True) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            if honor_pause and self._pause_event.is_set():
                if not self._wait_until_resumed():
                    return False
                deadline = time.monotonic() + seconds
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return not self._stop_event.is_set()

    def _wait_until_resumed(self) -> bool:
        while self._pause_event.is_set():
            if self._stop_event.wait(0.05):
                return False
        return not self._stop_event.is_set()

    @staticmethod
    def _grab_image(screenshotter: mss.mss, aabb: AABB) -> Image.Image:
        shot = screenshotter.grab(aabb.as_mss_monitor())
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def _current_aabb(self, fallback: AABB) -> AABB:
        with self._aabb_lock:
            return self._active_aabb or fallback

    def _pause_from_worker(self, reason: str) -> bool:
        self._pause_event.set()
        self._queue_ui(self._show_paused_ui, reason)
        return self._wait_until_resumed()

    def _ensure_target_focus(self) -> bool:
        hwnd = self._target_hwnd
        if hwnd is not None and window_exists(hwnd) and is_foreground_window(hwnd):
            return True
        return self._pause_from_worker(
            "대상 e-book 창의 포커스가 해제되어 자동 일시정지했습니다. 창을 확인하고 재개하세요."
        )

    def _wait_for_page_change(
        self,
        screenshotter: mss.mss,
        previous_signature: bytes,
        fallback_aabb: AABB,
        timeout: float,
    ) -> bool | None:
        deadline = time.monotonic() + timeout
        changed_candidate: bytes | None = None
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return None
            if self._pause_event.is_set() and not self._wait_until_resumed():
                return None
            sample = self._grab_image(screenshotter, self._current_aabb(fallback_aabb))
            signature = image_signature(sample)
            sample.close()
            if signature_difference_percent(previous_signature, signature) >= CHANGE_THRESHOLD_PERCENT:
                if (
                    changed_candidate is not None
                    and signature_difference_percent(changed_candidate, signature) <= STABLE_THRESHOLD_PERCENT
                ):
                    return True
                changed_candidate = signature
            else:
                changed_candidate = None
            if not self._interruptible_wait(0.2):
                return None
        return False

    def _progress_status(self, page: int, total: int, message: str) -> str:
        completed = len(self._captured_files)
        new_completed = completed - self._job_initial_count
        remaining = max(0, self._job_expected_count - completed)
        if new_completed <= 0:
            return f"{page}/{total} {message}"
        elapsed = max(0.01, time.monotonic() - self._capture_started_at)
        eta = elapsed / new_completed * remaining
        return f"{page}/{total} {message} · 예상 남은 시간 {format_duration(eta)}"

    def _capture_worker(self, values: RunValues) -> None:
        error: Exception | None = None
        stopped = False
        previous_signature: bytes | None = None
        try:
            if not self._interruptible_wait(values.startup_delay):
                stopped = True
            else:
                self._capture_started_at = time.monotonic()
            controller = keyboard.Controller()
            if self._captured_files:
                with Image.open(self._captured_files[-1]) as previous_image:
                    previous_signature = image_signature(previous_image)
            with mss.mss() as screenshotter:
                page = values.current
                while page <= values.total:
                    if stopped or not self._wait_until_resumed():
                        stopped = True
                        break
                    if not self._ensure_target_focus():
                        stopped = self._stop_event.is_set()
                        if stopped:
                            break
                        continue
                    self._post_status(self._progress_status(page, values.total, "페이지 캡처 중…"))
                    capture_aabb = self._current_aabb(values.aabb)
                    image = self._grab_image(screenshotter, capture_aabb)
                    signature = image_signature(image)
                    if (
                        values.detect_duplicates
                        and previous_signature is not None
                        and signature_difference_percent(previous_signature, signature) < DUPLICATE_THRESHOLD_PERCENT
                    ):
                        image.close()
                        if not self._pause_from_worker(
                            f"{page}페이지가 직전 페이지와 동일하여 자동 일시정지했습니다. "
                            "뷰어의 현재 페이지를 확인하고 재개하세요."
                        ):
                            stopped = True
                            break
                        continue
                    page_path = self._session_dir / f"page-{page:08d}.png"  # type: ignore[operator]
                    image.save(page_path, "PNG", compress_level=1)
                    image.close()
                    self._captured_files.append(page_path)
                    previous_signature = signature
                    self._write_recovery_manifest(page + 1, values.total)
                    progress = len(self._captured_files) / max(self._job_expected_count, 1) * 100
                    self._queue_ui(self.progress_var.set, progress)
                    self._post_status(self._progress_status(page, values.total, "페이지 저장 완료"))
                    if page >= values.total:
                        self._queue_ui(self.vars["current_page"].set, str(values.total))
                        break
                    controller.press(values.next_key)  # type: ignore[arg-type]
                    controller.release(values.next_key)  # type: ignore[arg-type]
                    page += 1
                    self._queue_ui(self.vars["current_page"].set, str(page))
                    self._post_status(self._progress_status(page, values.total, "페이지 전환 대기 중…"))
                    if not self._interruptible_wait(values.page_delay):
                        stopped = True
                        break
                    if values.detect_change and previous_signature is not None:
                        changed = self._wait_for_page_change(
                            screenshotter,
                            previous_signature,
                            values.aabb,
                            values.change_timeout,
                        )
                        if changed is None:
                            stopped = True
                            break
                        if not changed and not self._pause_from_worker(
                            f"{page}페이지로 화면이 {values.change_timeout:g}초 동안 바뀌지 않아 "
                            "자동 일시정지했습니다. 뷰어를 확인하고 재개하세요."
                        ):
                            stopped = True
                            break
            if self._captured_files:
                self._post_status("PDF 생성 중…")
                self._write_pdf(self._captured_files, self._output_path)  # type: ignore[arg-type]
        except Exception as exc:  # GUI boundary: report unexpected capture/library errors.
            error = exc
        finally:
            self._queue_ui(self._capture_finished, error, stopped)

    @staticmethod
    def _write_pdf(images: list[Path], output_path: Path) -> None:
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            with temporary.open("wb") as pdf_file:
                pdf_file.write(img2pdf.convert([str(path) for path in images]))
            temporary.replace(output_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _post_status(self, message: str) -> None:
        self._queue_ui(self.status_var.set, message)

    def _capture_finished(self, error: Exception | None, stopped: bool) -> None:
        output_path = self._output_path
        captured_count = len(self._captured_files)
        self._capture_active = False
        self._pause_event.clear()
        self._stop_event.clear()
        self._worker = None
        with self._aabb_lock:
            self._active_aabb = None
        self.root.deiconify()
        self.root.lift()
        self._refresh_border_from_fields()
        self._set_control_state(running=False, paused=False)
        self._save_settings()
        if error is not None:
            self._recovery_pending = bool(self._captured_files)
            self.status_var.set(f"오류 · 복구 데이터 보존됨: {error}")
            messagebox.showerror(
                "캡처 오류",
                f"{error}\n\n캡처된 페이지는 다음 실행에서 복구할 수 있도록 보존했습니다.",
                parent=self.root,
            )
        elif captured_count == 0:
            self._clear_recovery()
            self._session_dir = None
            self._captured_files = []
            self.status_var.set("캡처 없이 정지했습니다.")
        else:
            self._clear_recovery()
            self._session_dir = None
            self._captured_files = []
            prefix = "정지됨 · " if stopped else "완료 · "
            self.status_var.set(f"{prefix}{captured_count}페이지 저장: {output_path}")
            messagebox.showinfo(
                "저장 완료",
                f"{captured_count}페이지를 저장했습니다.\n\n{output_path}",
                parent=self.root,
            )
        if self._closing:
            self._finish_close()

    def _set_control_state(self, running: bool, paused: bool) -> None:
        self.run_button.configure(text="재개" if paused else "실행", state="normal" if (not running or paused) else "disabled")
        self.pause_button.configure(state="normal" if running and not paused else "disabled")
        self.stop_button.configure(state="normal" if running else "disabled")

    def close(self) -> None:
        self._closing = True
        if self._selector is not None:
            self._selector.cancel()
        self._save_settings()
        if self._capture_active:
            self._stop_event.set()
            self._pause_event.clear()
            self.root.withdraw()
            return
        self._finish_close()

    def _finish_close(self) -> None:
        try:
            self._key_listener.stop()
        except Exception:
            pass
        self.border.destroy()
        self.root.destroy()


def main() -> None:
    configure_dpi_awareness()
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    EbookToPdfApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
