"""A constrained, searchable dropdown for an ordered dungeon-ID JSON list."""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox, ttk


INVALID_SELECTION_MESSAGES = {
    "en": (
        "The stored dungeon list must be a valid JSON array of strings.\n"
        "Nothing has been changed. Correct the source value and try again.\n"
        'Format: ["id1", "id2"]'
    ),
    "zh": (
        "当前 Dungeon 列表必须是合法的 JSON 字符串数组。\n"
        "原内容未改动。请先修正来源值，再重新打开选择器。\n"
        '格式：["id1", "id2"]'
    ),
    "de": (
        "Die gespeicherte Dungeon-Liste muss ein gültiges JSON-Array aus Zeichenketten sein.\n"
        "Der ursprüngliche Wert bleibt unverändert. Bitte den Quellwert korrigieren und erneut öffnen.\n"
        'Format: ["id1", "id2"]'
    ),
}


def parse_dungeon_selection(value):
    """Validate without normalizing away damaged data, unknown IDs or repeats.

    An unset/whitespace-only field remains the existing empty-selection case.
    Nonempty content must actually be a JSON string array; a malformed value
    must never become an empty selection merely by opening and applying.
    """
    if not isinstance(value, str):
        raise ValueError("Dungeon selection must be JSON text")
    if not value.strip():
        return []
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Dungeon selection must be a valid JSON array of strings") from exc
    if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
        raise ValueError("Dungeon selection must be a valid JSON array of strings")
    return result


class DungeonListSelector(ttk.Frame):
    """Keep the existing StringVar contract without permitting arbitrary IDs.

    Existing unknown IDs remain visible and are preserved unless removed. New
    choices must come from the asset catalog. Opening/cancelling is a no-op.
    """

    def __init__(self, parent, *, textvariable, state="normal", **kwargs):
        super().__init__(parent, **kwargs)
        self.variable = textvariable
        self.choices = []
        self.labels = {
            "title": "Select dungeons", "filter": "Filter", "apply": "Apply",
            "cancel": "Cancel", "clear": "Clear selection", "unknown": "stored / unavailable",
        }
        self.columnconfigure(0, weight=1)
        self.display = ttk.Entry(self, textvariable=textvariable, state="readonly", width=16)
        self.display.grid(row=0, column=0, sticky="ew")
        self.button = ttk.Button(self, text="▾", command=self.open_dropdown, width=2)
        self.button.grid(row=0, column=1, sticky="ns")
        self.display.bind("<Button-1>", lambda _event: self.open_dropdown())
        self.display.bind("<Alt-Down>", lambda _event: self.open_dropdown())
        self.display.bind("<Return>", lambda _event: self.open_dropdown())
        self.popup = None
        self.before_open = None
        self.configure(state=state)

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {})
        options.update(kwargs)
        if "state" in options:
            state = options.pop("state")
            self.button.configure(state="disabled" if state == "disabled" else "normal")
            self.display.configure(state="disabled" if state == "disabled" else "readonly")
        if "values" in options:
            self.choices = sorted(set(str(value) for value in options.pop("values")))
        return super().configure(**options)

    config = configure

    def open_dropdown(self):
        if self.button.instate(["disabled"]):
            return
        if self.before_open is not None and not self.before_open(self):
            return
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.lift()
            return
        try:
            original = parse_dungeon_selection(self.variable.get())
        except ValueError:
            # The integrated editor supplies a bound before_open callback;
            # read its current language without coupling to another GUI module.
            # Standalone callers can explicitly supply labels.language/message.
            owner = getattr(self.before_open, "__self__", None)
            language = self.labels.get("language", getattr(owner, "language_code", "en"))
            message = self.labels.get("invalid_value", INVALID_SELECTION_MESSAGES.get(language, INVALID_SELECTION_MESSAGES["en"]))
            messagebox.showerror(self.labels["title"], message, parent=self.winfo_toplevel())
            return
        chosen = set(original)
        available = list(dict.fromkeys([*self.choices, *original]))
        popup = tk.Toplevel(self)
        self.popup = popup
        popup.title(self.labels["title"])
        popup.transient(self.winfo_toplevel())
        popup.minsize(340, 240)
        x = max(0, min(self.winfo_rootx(), self.winfo_screenwidth() - 460))
        y = max(0, min(self.winfo_rooty() + self.winfo_height(), self.winfo_screenheight() - 460))
        popup.geometry(f"460x420+{x}+{y}")
        pane = ttk.Frame(popup, padding=10)
        pane.pack(fill="both", expand=True)
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(1, weight=1)
        search = tk.StringVar(popup)
        entry = ttk.Entry(pane, textvariable=search)
        entry.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        listing = tk.Listbox(pane, selectmode="multiple", exportselection=False)
        listing.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(pane, orient="vertical", command=listing.yview)
        listing.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")
        visible = []
        updating = [False]

        def remember(_event=None):
            if updating[0]:
                return
            indexes = set(listing.curselection())
            for index, name in enumerate(visible):
                if index in indexes:
                    chosen.add(name)
                else:
                    chosen.discard(name)

        def render(*_args):
            updating[0] = True
            try:
                visible[:] = [name for name in available if search.get().casefold() in name.casefold()]
                listing.delete(0, "end")
                for index, name in enumerate(visible):
                    suffix = "" if name in self.choices else f" ({self.labels['unknown']})"
                    listing.insert("end", name + suffix)
                    if name in chosen:
                        listing.selection_set(index)
            finally:
                updating[0] = False

        def apply():
            remember()
            # Preserve saved order and duplicates when not explicitly removed.
            result = [name for name in original if name in chosen]
            result.extend(name for name in self.choices if name in chosen and name not in original)
            self.variable.set(json.dumps(result, ensure_ascii=False))
            close()

        def close():
            self.popup = None
            popup.destroy()

        def clear():
            chosen.clear()
            render()

        listing.bind("<<ListboxSelect>>", remember)
        search.trace_add("write", render)
        controls = ttk.Frame(pane)
        controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(controls, text=self.labels["clear"], command=clear).pack(side="left")
        ttk.Button(controls, text=self.labels["apply"], command=apply).pack(side="right")
        ttk.Button(controls, text=self.labels["cancel"], command=close).pack(side="right", padx=5)
        popup.protocol("WM_DELETE_WINDOW", close)
        popup.bind("<Escape>", lambda _event: close())
        render()
        popup.grab_set()
        entry.focus_set()
