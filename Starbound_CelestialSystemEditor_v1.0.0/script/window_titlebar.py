"""Larger app-owned caption, with genuine Windows caption hit testing.

Only this Tk root and caption HWNDs are touched. No global settings change.
The small C helper handles hit testing without a Python window-procedure callback.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import tkinter as tk


class WindowsFrame:
    def __init__(self, root):
        self.root = root
        architecture = "x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "x86"
        self.caption_api = ctypes.CDLL(str(Path(__file__).with_name(
            "native_titlebar_" + architecture + ".dll")), use_last_error=True)
        self.caption_api.install_caption.argtypes = [wintypes.HWND] * 5 + [ctypes.c_int] * 2
        self.caption_api.install_caption.restype = ctypes.c_int
        self.caption_api.update_caption.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int]
        self.caption_api.update_caption.restype = ctypes.c_int
        self._caption_handle = None
        self._caption_bounds = None
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "GetParent": ([wintypes.HWND], wintypes.HWND),
            "GetWindowLongW": ([wintypes.HWND, ctypes.c_int], wintypes.LONG),
            "SetWindowLongW": ([wintypes.HWND, ctypes.c_int, wintypes.LONG], wintypes.LONG),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            "ShowWindowAsync": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    @property
    def handle(self):
        # Tk's client HWND is a child of its top-level wrapper.
        return self.api.GetParent(self.root.winfo_id())

    def configure(self):
        if not self.root.winfo_exists():
            return
        hwnd = self.handle
        if not hwnd:
            # Mapping/restoring and teardown can briefly remove the wrapper.
            return
        style = self.api.GetWindowLongW(hwnd, -16)
        # No system caption; retain native sizing, minimize/maximize and menu.
        style = (style & ~0x00C00000) | 0x00040000 | 0x00020000 | 0x00010000 | 0x00080000
        self.api.SetWindowLongW(hwnd, -16, style)
        extended = self.api.GetWindowLongW(hwnd, -20)
        self.api.SetWindowLongW(hwnd, -20, (extended & ~0x00000080) | 0x00040000)
        # NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED
        self.api.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037)

    def minimize(self):
        if not self.root.winfo_exists():
            return False
        hwnd = self.handle
        return bool(hwnd and self.api.ShowWindowAsync(hwnd, 6))

    def toggle_maximize(self):
        # Unlike wm iconify, normal/zoomed are supported for override-redirect
        # windows. Route through Tk so its state and Python callbacks stay valid.
        self.root.state("normal" if self.is_maximized() else "zoomed")

    def is_maximized(self):
        return bool(self.api.IsZoomed(self.handle))

    def is_minimized(self):
        return bool(self.api.IsIconic(self.handle))

    def install_caption(self, bar):
        hwnd = self.handle
        right = max(0, bar.frame.winfo_width() - bar.minimize_button.winfo_x())
        if not hwnd or not self.caption_api.install_caption(
                hwnd, self.root.winfo_id(), bar.frame.winfo_id(),
                bar.title_label.winfo_id(), bar.icon_label.winfo_id(), bar.height, right):
            raise RuntimeError("Could not initialize native titlebar hit testing")
        self._caption_handle = hwnd
        self._caption_bounds = (bar.height, right)

    def update_caption(self, bar):
        if not self._caption_handle:
            return
        bounds = (bar.height, max(0, bar.frame.winfo_width() - bar.minimize_button.winfo_x()))
        if bounds != self._caption_bounds:
            self.caption_api.update_caption(self._caption_handle, *bounds)
            self._caption_bounds = bounds

    def disable_caption(self):
        hwnd = self._caption_handle
        self._caption_handle = None
        self._caption_bounds = None
        if hwnd:
            # Keep any existing subclass inert while Tk restores its wrapper.
            # This exclusion is wider than any valid client rectangle.
            self.caption_api.update_caption(hwnd, 1, 0x7fffffff)


class WindowTitlebar:
    def __init__(self, root, source_icon):
        self.root = root
        self.native = WindowsFrame(root)
        self._native_enabled = True
        self.native_error = None
        self._maximized = None
        self.icon_edge = 2 * max(16, self.native.api.GetSystemMetrics(49))
        self.height = self.icon_edge + 16
        self.icon = source_icon.zoom(self.icon_edge).subsample(
            max(source_icon.width(), source_icon.height()))
        self.background = "#f3f3f3"
        self.frame = tk.Frame(root, background=self.background, height=self.height)
        self.frame.pack(side="top", fill="x")
        self.frame.pack_propagate(False)
        self.icon_label = tk.Label(self.frame, image=self.icon, background=self.background)
        self.icon_label.pack(side="left", padx=(10, 8), pady=8)
        self.title_label = tk.Label(self.frame, text=root.title(), anchor="w",
                                   background=self.background, foreground="#333333")
        self.close_button = self._button("×", self.close, close=True)
        self.maximize_button = self._button("□", self.toggle_maximize)
        self.minimize_button = self._button("−", self.minimize)
        self.title_label.pack(side="left", fill="x", expand=True)
        # No Python mouse-press/motion callbacks: Windows receives real
        # HTCAPTION hits and owns dragging, release, double-click and Snap.
        self._original_title = root.title
        self._original_wm_title = root.wm_title
        root.title = self.title
        root.wm_title = self.title
        root.overrideredirect(True)
        self._pending_configure = None
        root.bind("<Map>", self._mapped, add="+")
        root.bind("<Configure>", self._configured, add="+")
        root.bind("<Destroy>", self._destroyed, add="+")
        root.bind("<Alt-F4>", self.close, add="+")
        self._schedule_configure()

    def _button(self, text, command, close=False):
        button = tk.Button(self.frame, text=text, command=command, relief="flat",
                           borderwidth=0, highlightthickness=0, takefocus=True,
                           background=self.background, activebackground="#c42b1c" if close else "#dddddd",
                           activeforeground="white" if close else "black", font=("Segoe UI", 14))
        button.pack(side="right", fill="y", ipadx=12)
        return button

    def title(self, value=None):
        if value is None:
            return self._original_title()
        self.title_label.configure(text=value)
        return self._original_title(value)

    def _mapped(self, event):
        if event.widget is self.root:
            self._schedule_configure()

    def _schedule_configure(self):
        if self._native_enabled and self._pending_configure is None:
            self._pending_configure = self.root.after_idle(self._apply_native_frame)

    def _apply_native_frame(self):
        self._pending_configure = None
        if self._native_enabled and self.root.winfo_exists() and self.root.winfo_ismapped():
            try:
                self.native.configure()
                self.native.install_caption(self)
            except (OSError, RuntimeError, tk.TclError) as error:
                self._fallback_to_system_caption(error)

    def _fallback_to_system_caption(self, error):
        if not self._native_enabled:
            return
        # Set this before any Tk calls: restoring the wrapper can synchronously
        # generate Map/Configure events, which must not reinstall the helper.
        self._native_enabled = False
        self.native_error = str(error)
        if self._pending_configure is not None:
            try:
                self.root.after_cancel(self._pending_configure)
            except tk.TclError:
                pass
            self._pending_configure = None
        try:
            self.native.disable_caption()
        except (OSError, RuntimeError, tk.TclError):
            # Still restore the system caption if a stale native handle cannot
            # be disabled; wrapper destruction also removes its C subclasses.
            pass
        self.root.title = self._original_title
        self.root.wm_title = self._original_wm_title
        if self.root.winfo_exists():
            self.root.overrideredirect(False)
            self.frame.pack_forget()

    def _destroyed(self, event):
        if event.widget is self.root:
            if self._pending_configure is not None:
                self.root.after_cancel(self._pending_configure)
                self._pending_configure = None

    def _configured(self, event):
        if self._native_enabled and event.widget is self.root:
            maximized = self.native.is_maximized()
            if maximized != self._maximized:
                self.maximize_button.configure(text="❐" if maximized else "□")
                self._maximized = maximized
            self.native.update_caption(self)

    def minimize(self):
        if self._native_enabled:
            self.native.minimize()

    def toggle_maximize(self, _event=None):
        if self._native_enabled:
            self.native.toggle_maximize()
        return "break"

    def close(self, _event=None):
        # Preserve the editor's existing unsaved-work / busy close guard.
        callback = self.root.protocol("WM_DELETE_WINDOW")
        if callback:
            self.root.tk.call(callback)
        else:
            self.root.destroy()
        return "break"
