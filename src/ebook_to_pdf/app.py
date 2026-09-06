from __future__ import annotations

import ctypes
import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import img2pdf
import mss
from PIL import Image
from platformdirs import user_config_path
from pynput import keyboard

from .core import (
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


APP_NAME = "E-book 화면 캡처 PDF"
BORDER_THICKNESS = 4
HANDLE_SIZE = 18


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
        self.windows: dict[str, tk.Toplevel] = {}
        self._drag_origin: tuple[int, int] | None = None
        self._drag_aabb: AABB | None = None
        self._drag_edges = ""
        for edge in ("top", "bottom", "left", "right"):
            window = tk.Toplevel(root)
            window.withdraw()
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(bg="#e02020", cursor=self._cursor_for(edge))
            window.bind("<ButtonPress-1>", lambda event, e=edge: self._start_resize(event, e))
            window.bind("<B1-Motion>", self._resize)
            window.bind("<ButtonRelease-1>", self._end_resize)
            self.windows[edge] = window

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

    def _position_windows(self) -> None:
        if self.aabb is None:
            return
        a = self.aabb
        geometries = border_geometries(a)
        for edge, (width, height, x, y) in geometries.items():
            self.windows[edge].geometry(tk_geometry(width, height, x, y))

    def show(self) -> None:
        if self.aabb is None:
            return
        for window in self.windows.values():
            window.deiconify()
            window.lift()

    def hide(self) -> None:
        for window in self.windows.values():
            window.withdraw()

    def destroy(self) -> None:
        for window in self.windows.values():
            window.destroy()
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
    ) -> None:
        self.root = root
        self.bounds = bounds
        self.on_selected = on_selected
        self.on_cancel = on_cancel
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
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
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
            outline="#ff3030",
            width=5,
            fill="#ffffff",
        )

    def _drag(self, event: tk.Event) -> None:
        if self.start is not None and self.rectangle is not None:
            self.canvas.coords(self.rectangle, *self.start, event.x, event.y)

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
    def __init__(self, root: tk.Tk, config_path: Path | None = None) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("650x590")
        self.root.minsize(610, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.config_path = config_path or user_config_path("ebook-to-pdf") / "settings.json"
        settings = load_settings(self.config_path)
        if not settings.output_dir:
            settings.output_dir = str(Path.home() / "Documents")
        self.vars = {
            name: tk.StringVar(value=value)
            for name, value in asdict(settings).items()
        }
        self.status_var = tk.StringVar(value="대기 중")
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
        self._active_aabb: AABB | None = None
        self._aabb_lock = threading.Lock()
        self._ui_queue: queue.Queue[tuple[Callable[..., None], tuple[object, ...]]] = queue.Queue()

        self._build_ui()
        self.border = BorderOverlay(root, self._on_border_changed)
        for variable in self.vars.values():
            variable.trace_add("write", self._field_changed)
        self.root.after(150, self._refresh_border_from_fields)

        self._key_listener = keyboard.Listener(on_press=self._global_key_press)
        self._key_listener.daemon = True
        self._key_listener.start()
        self.root.after(40, self._drain_ui_queue)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        title = ttk.Label(outer, text=APP_NAME, font=("Malgun Gothic", 17, "bold"))
        title.grid(row=0, column=0, sticky="w", pady=(0, 12))

        region = ttk.LabelFrame(outer, text="1. 캡처 영역 (화면 절대 좌표)", padding=12)
        region.grid(row=1, column=0, sticky="ew", pady=5)
        for index in range(9):
            region.columnconfigure(index, weight=1 if index in {1, 3, 5, 7} else 0)
        for index, name in enumerate(("x1", "y1", "x2", "y2")):
            ttk.Label(region, text=name).grid(row=0, column=index * 2, padx=(0, 4))
            ttk.Entry(region, textvariable=self.vars[name], width=8).grid(
                row=0, column=index * 2 + 1, sticky="ew", padx=(0, 9)
            )
        ttk.Button(region, text="드래그로 지정", command=self.start_region_selection).grid(
            row=0, column=8, padx=(6, 0)
        )
        ttk.Label(
            region,
            text="빨간 테두리의 변 또는 모서리를 드래그해 영역을 미세 조정할 수 있습니다.",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=9, sticky="w", pady=(9, 0))

        pages = ttk.LabelFrame(outer, text="2. 페이지", padding=12)
        pages.grid(row=2, column=0, sticky="ew", pady=5)
        pages.columnconfigure(1, weight=1)
        pages.columnconfigure(3, weight=1)
        pages.columnconfigure(5, weight=1)
        ttk.Label(pages, text="현재 페이지").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(pages, textvariable=self.vars["current_page"], width=10).grid(row=0, column=1, sticky="ew", padx=(0, 14))
        ttk.Label(pages, text="총 페이지").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(pages, textvariable=self.vars["total_pages"], width=10).grid(row=0, column=3, sticky="ew", padx=(0, 14))
        ttk.Label(pages, text="다음 페이지 키").grid(row=0, column=4, padx=(0, 6))
        ttk.Entry(pages, textvariable=self.vars["next_key"], width=12).grid(row=0, column=5, sticky="ew")

        timing = ttk.LabelFrame(outer, text="3. 타이밍", padding=12)
        timing.grid(row=3, column=0, sticky="ew", pady=5)
        timing.columnconfigure(1, weight=1)
        timing.columnconfigure(3, weight=1)
        ttk.Label(timing, text="시작 대기 (초)").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(timing, textvariable=self.vars["startup_delay"], width=10).grid(row=0, column=1, sticky="ew", padx=(0, 20))
        ttk.Label(timing, text="페이지 전환 대기 (초)").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(timing, textvariable=self.vars["page_delay"], width=10).grid(row=0, column=3, sticky="ew")

        output = ttk.LabelFrame(outer, text="4. 저장", padding=12)
        output.grid(row=4, column=0, sticky="ew", pady=5)
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="파일 이름").grid(row=0, column=0, sticky="w", padx=(0, 7), pady=(0, 8))
        ttk.Entry(output, textvariable=self.vars["filename"]).grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(output, text="저장 폴더").grid(row=1, column=0, sticky="w", padx=(0, 7))
        ttk.Entry(output, textvariable=self.vars["output_dir"]).grid(row=1, column=1, sticky="ew", padx=(0, 7))
        ttk.Button(output, text="찾아보기", command=self.choose_output_dir).grid(row=1, column=2)

        controls = ttk.Frame(outer)
        controls.grid(row=5, column=0, sticky="ew", pady=(14, 8))
        controls.columnconfigure((0, 1, 2), weight=1)
        self.run_button = ttk.Button(controls, text="실행", command=self.start_or_resume)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 5), ipady=6)
        self.pause_button = ttk.Button(controls, text="일시정지", command=self.pause, state="disabled")
        self.pause_button.grid(row=0, column=1, sticky="ew", padx=5, ipady=6)
        self.stop_button = ttk.Button(controls, text="정지", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=(5, 0), ipady=6)

        self.progress = ttk.Progressbar(outer, variable=self.progress_var, maximum=100)
        self.progress.grid(row=6, column=0, sticky="ew", pady=(3, 8))
        ttk.Label(outer, textvariable=self.status_var, wraplength=600).grid(row=7, column=0, sticky="w")
        ttk.Label(
            outer,
            text="실행 중 ESC: 일시정지 · 정지하면 지금까지 캡처한 페이지를 PDF로 저장합니다.",
            foreground="#555555",
        ).grid(row=8, column=0, sticky="w", pady=(9, 0))

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

    def _refresh_border_from_fields(self) -> None:
        self._region_job = None
        if (self._capture_active and not self._pause_event.is_set()) or self._selector is not None:
            return
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

    def _validated_run_values(self) -> tuple[AABB, int, int, object, float, float, Path]:
        aabb = parse_aabb(tuple(self.vars[name].get() for name in ("x1", "y1", "x2", "y2")))
        screen = virtual_screen_bounds()
        if not (
            screen.x1 <= aabb.x1 < aabb.x2 <= screen.x2
            and screen.y1 <= aabb.y1 < aabb.y2 <= screen.y2
        ):
            raise ValueError("캡처 영역은 현재 가상 화면 범위 안에 있어야 합니다.")
        current, total = validate_pages(self.vars["current_page"].get(), self.vars["total_pages"].get())
        next_key = parse_key(self.vars["next_key"].get())
        startup_delay, page_delay = validate_delays(self.vars["startup_delay"].get(), self.vars["page_delay"].get())
        filename = normalize_pdf_filename(self.vars["filename"].get())
        output_dir = Path(self.vars["output_dir"].get()).expanduser()
        if not output_dir.is_dir():
            raise ValueError("저장 폴더가 존재하지 않습니다.")
        return aabb, current, total, next_key, startup_delay, page_delay, output_dir / filename

    def start_or_resume(self) -> None:
        if self._capture_active:
            if not self._pause_event.is_set():
                return
            try:
                startup_delay, _ = validate_delays(self.vars["startup_delay"].get(), self.vars["page_delay"].get())
            except ValueError as exc:
                messagebox.showerror("입력 확인", str(exc), parent=self.root)
                return
            self._prepare_background_capture()
            self.status_var.set(f"{startup_delay:g}초 후 캡처를 재개합니다…")
            self.root.after(int(startup_delay * 1000), self._resume_after_delay)
            return

        try:
            aabb, current, total, next_key, startup_delay, page_delay, output_path = self._validated_run_values()
        except ValueError as exc:
            messagebox.showerror("입력 확인", str(exc), parent=self.root)
            return
        if output_path.exists() and not messagebox.askyesno(
            "파일 덮어쓰기",
            f"이미 존재하는 파일입니다. 덮어쓸까요?\n\n{output_path}",
            parent=self.root,
        ):
            return

        self.vars["filename"].set(output_path.name)
        self._save_settings()
        self._stop_event.clear()
        self._pause_event.clear()
        self._capture_active = True
        with self._aabb_lock:
            self._active_aabb = aabb
        self._captured_files = []
        self._session_dir = Path(tempfile.mkdtemp(prefix="ebook-to-pdf-"))
        self._output_path = output_path
        self.progress_var.set(0)
        self._set_control_state(running=True, paused=False)
        self._prepare_background_capture()
        self.status_var.set(f"{startup_delay:g}초 후 {current}페이지부터 캡처합니다…")
        self._worker = threading.Thread(
            target=self._capture_worker,
            args=(aabb, current, total, next_key, startup_delay, page_delay),
            daemon=True,
        )
        self._worker.start()

    def _prepare_background_capture(self) -> None:
        self.border.hide()
        self.root.iconify()

    def _resume_after_delay(self) -> None:
        if self._capture_active and not self._stop_event.is_set():
            self._pause_event.clear()
            self._set_control_state(running=True, paused=False)

    def pause(self) -> None:
        if not self._capture_active or self._pause_event.is_set():
            return
        self._pause_event.set()
        self.root.after(0, self._show_paused_ui)

    def _show_paused_ui(self) -> None:
        if not self._capture_active:
            return
        self.root.deiconify()
        self.root.lift()
        self._refresh_border_from_fields()
        self.status_var.set("일시정지됨 · 실행 버튼을 누르면 재개합니다.")
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

    def _capture_worker(
        self,
        aabb: AABB,
        current: int,
        total: int,
        next_key: object,
        startup_delay: float,
        page_delay: float,
    ) -> None:
        error: Exception | None = None
        stopped = False
        try:
            if not self._interruptible_wait(startup_delay):
                stopped = True
            controller = keyboard.Controller()
            with mss.mss() as screenshotter:
                for page in range(current, total + 1):
                    if stopped or not self._wait_until_resumed():
                        stopped = True
                        break
                    self._post_status(f"{page}/{total} 페이지 캡처 중…")
                    with self._aabb_lock:
                        capture_aabb = self._active_aabb or aabb
                    shot = screenshotter.grab(capture_aabb.as_mss_monitor())
                    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    page_path = self._session_dir / f"page-{page:08d}.png"  # type: ignore[operator]
                    image.save(page_path, "PNG", compress_level=1)
                    image.close()
                    self._captured_files.append(page_path)
                    progress = len(self._captured_files) / (total - current + 1) * 100
                    self._queue_ui(self.progress_var.set, progress)
                    if page >= total:
                        self._queue_ui(self.vars["current_page"].set, str(total))
                        break
                    controller.press(next_key)  # type: ignore[arg-type]
                    controller.release(next_key)  # type: ignore[arg-type]
                    self._queue_ui(self.vars["current_page"].set, str(page + 1))
                    self._post_status(f"{page + 1}/{total} 페이지 전환 대기 중…")
                    if not self._interruptible_wait(page_delay):
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
        if self._session_dir is not None:
            shutil.rmtree(self._session_dir, ignore_errors=True)
        self._session_dir = None
        self._captured_files = []
        self._save_settings()
        if error is not None:
            self.status_var.set(f"오류: {error}")
            messagebox.showerror("캡처 오류", str(error), parent=self.root)
        elif captured_count == 0:
            self.status_var.set("캡처 없이 정지했습니다.")
        else:
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
