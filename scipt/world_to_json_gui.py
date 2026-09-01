from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from gui_common import BaseWindow
import starbound_world_editor as swe


TRANSLATIONS = {
    "en": {
        "window_title": "Starbound World → Editable JSON",
        "page_title": "Export a Starbound world as editable JSON",
        "subtitle": "The original .world is never modified. The compact JSON stores each editable world, sky, terrain and biome parameter only once.",
        "source_label": "Source .world",
        "automatic_output_hint": "The JSON is created beside the source world as tmp_<original world filename>.json.",
        "start_export": "Start export",
        "open_json": "Open JSON",
        "open_folder": "Open folder",
        "initial_status": "Select a World4 .world file.",
        "select_world": "Select a Starbound world",
        "world_file": "Starbound world",
        "invalid_world": "Select an existing .world file.",
        "exporting": "Reading BTreeDB5, decompressing records and converting metadata…",
        "export_success_status": "Exported: {output}   World {width}×{height}, {biomes} biomes.",
        "export_done": "Export complete",
        "export_done_message": "The compact editable JSON has been created.\n\nEdit the world, sky, terrain or biomes groups, then run ‘Import JSON to World’.",
    },
    "zh": {
        "window_title": "Starbound 世界 → 可编辑 JSON",
        "page_title": "把 Starbound 世界导出为可编辑 JSON",
        "subtitle": "不会修改原 world。精简 JSON 只保存 world、sky、terrain 和 biomes 中可编辑的参数，每个参数只出现一次。",
        "source_label": "来源 .world",
        "automatic_output_hint": "JSON 会自动生成在来源 world 的同一文件夹，文件名为 tmp_<原 world 完整文件名>.json。",
        "start_export": "开始导出",
        "open_json": "打开 JSON",
        "open_folder": "打开所在文件夹",
        "initial_status": "请选择一个 World4 .world 文件。",
        "select_world": "选择 Starbound 世界",
        "world_file": "Starbound 世界",
        "invalid_world": "请选择存在的 .world 文件。",
        "exporting": "正在读取 BTreeDB5、解压记录并转换元数据……",
        "export_success_status": "导出成功：{output}　世界 {width}×{height}，biome {biomes} 个。",
        "export_done": "导出完成",
        "export_done_message": "已经生成精简的可编辑 JSON。\n\n编辑 world、sky、terrain 或 biomes 参数组，完成后运行“从 JSON 生成 World”。",
    },
    "de": {
        "window_title": "Starbound-Welt → bearbeitbares JSON",
        "page_title": "Starbound-Welt als bearbeitbares JSON exportieren",
        "subtitle": "Die ursprüngliche .world-Datei wird nicht verändert. Das kompakte JSON speichert jeden bearbeitbaren Welt-, Himmel-, Gelände- und Biomparameter nur einmal.",
        "source_label": "Quell-.world",
        "automatic_output_hint": "Das JSON wird neben der Quellwelt als tmp_<vollständiger ursprünglicher World-Dateiname>.json erstellt.",
        "start_export": "Export starten",
        "open_json": "JSON öffnen",
        "open_folder": "Ordner öffnen",
        "initial_status": "Wählen Sie eine World4-.world-Datei.",
        "select_world": "Starbound-Welt auswählen",
        "world_file": "Starbound-Welt",
        "invalid_world": "Wählen Sie eine vorhandene .world-Datei.",
        "exporting": "BTreeDB5 wird gelesen, Datensätze werden entpackt und Metadaten konvertiert…",
        "export_success_status": "Exportiert: {output}   Welt {width}×{height}, {biomes} Biome.",
        "export_done": "Export abgeschlossen",
        "export_done_message": "Das kompakte bearbeitbare JSON wurde erstellt.\n\nBearbeiten Sie world, sky, terrain oder biomes und starten Sie danach ‘JSON in World importieren’.",
    },
}


class ExportWindow(BaseWindow):
    def __init__(self) -> None:
        super().__init__(TRANSLATIONS, "820x380")
        self.world_var = tk.StringVar()
        self.status_var = tk.StringVar()

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        self.title_label = ttk.Label(header, style="Title.TLabel")
        self.title_label.pack(side="left", anchor="nw")
        self.add_language_selector(header).pack(side="right", anchor="ne")
        self.subtitle_label = ttk.Label(outer, style="Hint.TLabel", wraplength=740)
        self.subtitle_label.pack(anchor="w", pady=(4, 14))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.source_label, _, self.source_browse = self.add_path_row(
            form, 0, "", self.world_var, self.browse_world
        )
        self.output_hint_label = ttk.Label(
            outer, style="Hint.TLabel", wraplength=760
        )
        self.output_hint_label.pack(anchor="w", pady=(7, 5))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(6, 8))
        self.action_button = ttk.Button(
            buttons, style="Accent.TButton", command=self.export
        )
        self.action_button.pack(side="left")
        self.open_json_button = ttk.Button(buttons, command=self.open_output)
        self.open_json_button.pack(side="left", padx=8)
        self.open_folder_button = ttk.Button(buttons, command=self.open_output_folder)
        self.open_folder_button.pack(side="left")

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 8))
        ttk.Label(outer, textvariable=self.status_var, wraplength=760).pack(anchor="w")
        self.set_status_key("initial_status")
        self.apply_language()

    def apply_language(self) -> None:
        super().apply_language()
        self.title_label.configure(text=self.tr("page_title"))
        self.subtitle_label.configure(text=self.tr("subtitle"))
        self.source_label.configure(text=self.tr("source_label"))
        self.output_hint_label.configure(text=self.tr("automatic_output_hint"))
        self.source_browse.configure(text=self.tr("browse"))
        self.action_button.configure(text=self.tr("start_export"))
        self.open_json_button.configure(text=self.tr("open_json"))
        self.open_folder_button.configure(text=self.tr("open_folder"))

    def browse_world(self) -> None:
        self.choose_open_file(
            self.world_var,
            self.tr("select_world"),
            [(self.tr("world_file"), "*.world"), (self.tr("all_files"), "*.*")],
        )

    def export(self) -> None:
        world = Path(self.world_var.get().strip())
        if not world.is_file():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_world"), parent=self.root
            )
            return
        output = swe.automatic_project_path(world)
        if not self.confirm_replace(output):
            return

        def task():
            return swe.export_world_project(world, output)

        def success(project):
            self.last_output = output
            size = project["source"]["worldSize"]
            biome_count = project["source"]["biomeCount"]
            self.set_status_key(
                "export_success_status",
                output=output,
                width=size[0],
                height=size[1],
                biomes=biome_count,
            )
            messagebox.showinfo(
                self.tr("export_done"),
                self.tr("export_done_message"),
                parent=self.root,
            )

        self.run_worker(task, success, "exporting")


if __name__ == "__main__":
    ExportWindow().run()
