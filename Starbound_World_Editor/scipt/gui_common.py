from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable


LANGUAGE_OPTIONS = {
    "English": "en",
    "中文": "zh",
    "Deutsch": "de",
}

COMMON_TRANSLATIONS = {
    "en": {
        "browse": "Browse…",
        "operation_failed": "Operation failed",
        "failed_status": "Failed: {error}",
        "file_exists": "File already exists",
        "replace_prompt": "This output file will be replaced:\n\n{output}\n\nContinue?",
        "no_output_title": "No output yet",
        "no_output": "Complete an operation first.",
        "all_files": "All files",
        "invalid_path": "Invalid path",
        "yes": "Yes",
        "no": "No",
    },
    "zh": {
        "browse": "浏览…",
        "operation_failed": "操作失败",
        "failed_status": "失败：{error}",
        "file_exists": "文件已经存在",
        "replace_prompt": "将替换这个输出文件：\n\n{output}\n\n是否继续？",
        "no_output_title": "尚无输出",
        "no_output": "请先完成一次操作。",
        "all_files": "所有文件",
        "invalid_path": "路径无效",
        "yes": "是",
        "no": "否",
    },
    "de": {
        "browse": "Durchsuchen…",
        "operation_failed": "Vorgang fehlgeschlagen",
        "failed_status": "Fehler: {error}",
        "file_exists": "Datei ist bereits vorhanden",
        "replace_prompt": "Diese Ausgabedatei wird ersetzt:\n\n{output}\n\nFortfahren?",
        "no_output_title": "Noch keine Ausgabe",
        "no_output": "Führen Sie zuerst einen Vorgang aus.",
        "all_files": "Alle Dateien",
        "invalid_path": "Ungültiger Pfad",
        "yes": "Ja",
        "no": "Nein",
    },
}


def enable_windows_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def configure_style(root: tk.Tk) -> None:
    root.option_add("*Font", ("Microsoft YaHei UI", 10))
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
    style.configure("Hint.TLabel", foreground="#555555")
    style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))


class BaseWindow:
    def __init__(
        self, translations: dict[str, dict[str, str]], geometry: str = "820x430"
    ) -> None:
        enable_windows_dpi_awareness()
        self.translations = {
            language: {**COMMON_TRANSLATIONS[language], **translations.get(language, {})}
            for language in ("en", "zh", "de")
        }
        self.language_code = "en"
        self.root = tk.Tk()
        self.root.title(self.tr("window_title"))
        self.root.geometry(geometry)
        self.root.minsize(720, 380)
        configure_style(self.root)
        self.busy = False
        self.last_output: Path | None = None
        self.language_var = tk.StringVar(value="English")
        self.status_translation: tuple[str, dict[str, Any]] | None = None

    def tr(self, key: str, **values: Any) -> str:
        text = self.translations.get(self.language_code, {}).get(key)
        if text is None:
            text = self.translations["en"].get(key, key)
        return text.format(**values) if values else text

    def add_language_selector(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        ttk.Label(frame, text="Language / 语言 / Sprache").pack(anchor="e")
        self.language_combo = ttk.Combobox(
            frame,
            textvariable=self.language_var,
            values=list(LANGUAGE_OPTIONS.keys()),
            state="readonly",
            width=11,
        )
        self.language_combo.pack(anchor="e", pady=(3, 0))
        self.language_combo.bind("<<ComboboxSelected>>", self.change_language)
        return frame

    def change_language(self, _event: Any = None) -> None:
        self.language_code = LANGUAGE_OPTIONS.get(self.language_var.get(), "en")
        self.apply_language()

    def apply_language(self) -> None:
        self.root.title(self.tr("window_title"))
        self.refresh_status_translation()

    def add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse_command: Callable[[], None],
    ) -> tuple[ttk.Label, ttk.Entry, ttk.Button]:
        label_widget = ttk.Label(parent, text=label)
        label_widget.grid(row=row, column=0, sticky="w", pady=6)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=(10, 8), pady=6)
        button = ttk.Button(parent, text=self.tr("browse"), command=browse_command)
        button.grid(row=row, column=2, sticky="ew", pady=6)
        return label_widget, entry, button

    def choose_open_file(
        self, variable: tk.StringVar, title: str, filetypes: list[tuple[str, str]]
    ) -> str:
        path = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if path:
            variable.set(path)
        return path

    def choose_save_file(
        self,
        variable: tk.StringVar,
        title: str,
        extension: str,
        filetypes: list[tuple[str, str]],
    ) -> str:
        path = filedialog.asksaveasfilename(
            title=title,
            defaultextension=extension,
            filetypes=filetypes,
            initialfile=Path(variable.get()).name if variable.get() else None,
            initialdir=str(Path(variable.get()).parent) if variable.get() else None,
        )
        if path:
            variable.set(path)
        return path

    def choose_directory(
        self, variable: tk.StringVar, title: str
    ) -> str:
        initial = variable.get().strip()
        path = filedialog.askdirectory(
            title=title,
            initialdir=initial if initial and Path(initial).is_dir() else None,
        )
        if path:
            variable.set(path)
        return path

    def set_status(self, text: str) -> None:
        self.status_translation = None
        self.status_var.set(text)

    def set_status_key(self, key: str, **values: Any) -> None:
        self.status_translation = (key, values)
        self.status_var.set(self.tr(key, **values))

    def refresh_status_translation(self) -> None:
        if self.status_translation is not None:
            key, values = self.status_translation
            self.status_var.set(self.tr(key, **values))

    def run_worker(
        self,
        task: Callable[[], Any],
        success: Callable[[Any], None],
        busy_key: str,
    ) -> None:
        if self.busy:
            return
        self.busy = True
        self.action_button.state(["disabled"])
        self.progress.start(12)
        self.set_status_key(busy_key)

        def work() -> None:
            try:
                result = task()
            except Exception as exc:
                self.root.after(0, lambda error=exc: self.finish_error(error))
            else:
                self.root.after(0, lambda: self.finish_success(result, success))

        threading.Thread(target=work, daemon=True).start()

    def finish_error(self, error: Exception) -> None:
        self.busy = False
        self.progress.stop()
        self.action_button.state(["!disabled"])
        self.set_status_key("failed_status", error=error)
        messagebox.showerror(self.tr("operation_failed"), str(error), parent=self.root)

    def finish_success(self, result: Any, callback: Callable[[Any], None]) -> None:
        self.busy = False
        self.progress.stop()
        self.action_button.state(["!disabled"])
        callback(result)

    def confirm_replace(self, output: Path) -> bool:
        if not output.exists():
            return True
        return self.ask_yes_no(
            self.tr("file_exists"),
            self.tr("replace_prompt", output=output),
        )

    def ask_yes_no(self, title: str, message: str) -> bool:
        """Show a modal confirmation whose button language follows the UI."""

        result = {"value": False}
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=560, justify="left").pack(
            anchor="w", fill="x"
        )
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(18, 0))

        def finish(value: bool) -> None:
            result["value"] = value
            dialog.destroy()

        yes_button = ttk.Button(buttons, text=self.tr("yes"), command=lambda: finish(True))
        yes_button.pack(side="left", padx=(0, 8))
        no_button = ttk.Button(buttons, text=self.tr("no"), command=lambda: finish(False))
        no_button.pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.bind("<Escape>", lambda _event: finish(False))
        dialog.bind("<Return>", lambda _event: finish(False))
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        no_button.focus_set()
        self.root.wait_window(dialog)
        return result["value"]

    def open_output(self) -> None:
        if self.last_output is None or not self.last_output.exists():
            messagebox.showinfo(
                self.tr("no_output_title"), self.tr("no_output"), parent=self.root
            )
            return
        os.startfile(str(self.last_output))

    def open_output_folder(self) -> None:
        if self.last_output is None or not self.last_output.exists():
            messagebox.showinfo(
                self.tr("no_output_title"), self.tr("no_output"), parent=self.root
            )
            return
        os.startfile(str(self.last_output.parent))

    def run(self) -> None:
        self.root.mainloop()


def read_project_header(path: Path, language: str = "en") -> dict[str, Any]:
    errors = {
        "en": ("This is not a Starbound World Editor project JSON.", "The project is missing its editable parameter groups."),
        "zh": ("这不是 Starbound World Editor 生成的项目 JSON。", "项目缺少可编辑参数组。"),
        "de": ("Dies ist keine Projekt-JSON des Starbound World Editors.", "Dem Projekt fehlen die bearbeitbaren Parametergruppen."),
    }
    invalid_project, missing_parameters = errors.get(language, errors["en"])
    project = json.loads(path.read_text(encoding="utf-8"))
    source = project.get("source") if isinstance(project, dict) else None
    if isinstance(source, dict) and source.get("schema") == "StarboundWorldEditorProject2":
        world = project.get("world")
        biomes = project.get("biomes")
        if not isinstance(world, dict) or not isinstance(biomes, list):
            raise ValueError(missing_parameters)
        weather = world.get("weatherPool", [])
        return {
            "sourceWorld": source.get("worldFile", ""),
            "sourceSha256": source.get("sha256", ""),
            "size": source.get("worldSize", []),
            "biomes": source.get("biomeCount", 0),
            "biomeProfiles": len(biomes),
            "worldName": world.get("worldName", ""),
            "weather": ", ".join(
                f"{entry.get('item')}={entry.get('weight')}"
                for entry in weather
                if isinstance(entry, dict)
            ),
        }
    editor = project.get("editor") if isinstance(project, dict) else None
    editable = project.get("editable") if isinstance(project, dict) else None
    if not isinstance(editor, dict) or editor.get("schema") != "StarboundWorldEditorProject1":
        raise ValueError(invalid_project)
    if not isinstance(editable, dict):
        raise ValueError(missing_parameters)
    advanced = project.get("advancedWorldDocument", {})
    weather = editable.get("weatherPool", [])
    return {
        "sourceWorld": editor.get("sourceWorld", ""),
        "sourceSha256": editor.get("sourceSha256", ""),
        "size": advanced.get("size", []),
        "biomes": len(editable.get("biomes", [])),
        "biomeProfiles": len(editable.get("biomes", [])),
        "worldName": "",
        "weather": ", ".join(
            f"{entry.get('item')}={entry.get('weight')}"
            for entry in weather
            if isinstance(entry, dict)
        ),
    }
