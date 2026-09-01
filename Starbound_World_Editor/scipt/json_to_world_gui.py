from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from gui_common import BaseWindow, read_project_header
import starbound_world_editor as swe


TRANSLATIONS = {
    "en": {
        "window_title": "Starbound Editable JSON → World",
        "page_title": "Rebuild a Starbound world from editable JSON",
        "subtitle": "Select the edited JSON and its original source .world, then choose the full output path. A new planet name can also be synchronized to the star map through universe.chunks.",
        "project_label": "Project .json",
        "source_label": "Original source .world",
        "output_label": "New output .world path",
        "planet_name_label": "New planet name (optional)",
        "universe_folder_label": "Universe storage folder",
        "sync_star_map": "Synchronize this name to the star map (backs up and updates universe.chunks)",
        "universe_hint": "Required only when star-map synchronization is selected. The folder must contain universe.chunks, and the game/server must be fully closed.",
        "start_import": "Start building World",
        "open_folder": "Open folder",
        "initial_status": "Select an exported and edited project JSON.",
        "select_project": "Select editable project JSON",
        "select_source": "Select the original source world",
        "save_world": "Save the new Starbound world",
        "select_universe_folder": "Select storage\\universe folder",
        "json_file": "JSON",
        "world_file": "Starbound world",
        "invalid_json_title": "Invalid JSON",
        "invalid_project_path": "Select an existing project JSON.",
        "invalid_source_path": "The source .world file was not found.",
        "output_same_title": "Overwrite prohibited",
        "output_same_message": "The output path cannot be the same as the source world.",
        "invalid_output": "Select or enter an output .world path. The .world extension is added automatically if omitted.",
        "invalid_planet_name": "Enter a non-empty new planet name, or turn off star-map synchronization.",
        "invalid_universe_folder": "Select the storage\\universe folder that contains universe.chunks.",
        "chunks_confirm_title": "Update star-map database",
        "chunks_confirm_message": "The selected universe.chunks will be updated in place after the new world is built. An automatic backup will be created first.\n\n{chunks}\n\nThe game and server must be fully closed. Continue?",
        "unknown": "unknown",
        "project_loaded_status": "Project loaded: {name}; world {size}, {biomes} biomes, weather: {weather}",
        "importing": "Rebuilding the world and, if selected, synchronizing universe.chunks…",
        "import_success_status": "Built and verified: {output}",
        "import_done": "World build complete",
        "import_done_message": "The new file was created and passed structural and record-by-record verification:\n\n{output}{sync_details}\n\nBack up your files and test it only while the game is fully closed.",
        "chunks_backup_details": "\n\nThe star-map name was synchronized. Automatic universe.chunks backup:\n{backup}",
    },
    "zh": {
        "window_title": "可编辑 JSON → Starbound 世界",
        "page_title": "从可编辑 JSON 重建新的 Starbound 世界",
        "subtitle": "选择修改后的 JSON、对应的原始 .world 和完整输出路径。还可以输入新星球名，并通过 universe.chunks 同步星图名称。",
        "project_label": "项目 .json",
        "source_label": "原始来源 .world",
        "output_label": "输出新 .world 路径",
        "planet_name_label": "新星球名称（可选）",
        "universe_folder_label": "Universe 存档文件夹",
        "sync_star_map": "把新名称同步到星图（自动备份并修改 universe.chunks）",
        "universe_hint": "仅勾选同步星图时需要；请选择包含 universe.chunks 的 storage\\universe 文件夹，并确保游戏和服务器已完全退出。",
        "start_import": "开始生成 World",
        "open_folder": "打开所在文件夹",
        "initial_status": "请选择导出并编辑过的项目 JSON。",
        "select_project": "选择可编辑项目 JSON",
        "select_source": "选择对应的原始 world",
        "save_world": "保存新的 Starbound world",
        "select_universe_folder": "选择 storage\\universe 文件夹",
        "json_file": "JSON",
        "world_file": "Starbound 世界",
        "invalid_json_title": "JSON 无效",
        "invalid_project_path": "请选择存在的项目 JSON。",
        "invalid_source_path": "找不到来源 .world 文件。",
        "output_same_title": "禁止覆盖",
        "output_same_message": "输出路径不能与来源 world 相同。",
        "invalid_output": "请选择或输入输出 .world 的完整路径；未填写 .world 后缀时会自动添加。",
        "invalid_planet_name": "请输入非空的新星球名称，或取消勾选同步星图。",
        "invalid_universe_folder": "请选择包含 universe.chunks 的 storage\\universe 文件夹。",
        "chunks_confirm_title": "修改星图数据库",
        "chunks_confirm_message": "新 world 生成后，程序会直接更新所选的 universe.chunks，并先自动建立备份。\n\n{chunks}\n\n请确认游戏和服务器已经完全退出。是否继续？",
        "unknown": "未知",
        "project_loaded_status": "已读取项目：{name}；世界 {size}，biome {biomes} 个，天气：{weather}",
        "importing": "正在重建 world，并按选择同步 universe.chunks……",
        "import_success_status": "生成并验证成功：{output}",
        "import_done": "World 生成完成",
        "import_done_message": "新文件已经生成并通过结构与逐记录校验：\n\n{output}{sync_details}\n\n请先备份，再在游戏完全退出时放入 universe 测试。",
        "chunks_backup_details": "\n\n星图名称已经同步；universe.chunks 的自动备份位于：\n{backup}",
    },
    "de": {
        "window_title": "Bearbeitbares Starbound-JSON → Welt",
        "page_title": "Starbound-Welt aus bearbeitbarem JSON neu erstellen",
        "subtitle": "Wählen Sie Projekt-JSON, Quell-.world und Ausgabepfad. Ein neuer Planetenname kann über universe.chunks mit der Sternenkarte synchronisiert werden.",
        "project_label": "Projekt-.json",
        "source_label": "Ursprüngliche Quell-.world",
        "output_label": "Pfad der neuen Ausgabe-.world",
        "planet_name_label": "Neuer Planetenname (optional)",
        "universe_folder_label": "Universe-Speicherordner",
        "sync_star_map": "Namen mit der Sternenkarte synchronisieren (universe.chunks sichern und ändern)",
        "universe_hint": "Nur für die Sternenkarten-Synchronisierung erforderlich. Der Ordner muss universe.chunks enthalten; Spiel und Server müssen beendet sein.",
        "start_import": "World erstellen",
        "open_folder": "Ordner öffnen",
        "initial_status": "Wählen Sie ein exportiertes und bearbeitetes Projekt-JSON.",
        "select_project": "Bearbeitbares Projekt-JSON auswählen",
        "select_source": "Ursprüngliche Quellwelt auswählen",
        "save_world": "Neue Starbound-Welt speichern",
        "select_universe_folder": "Ordner storage\\universe auswählen",
        "json_file": "JSON",
        "world_file": "Starbound-Welt",
        "invalid_json_title": "Ungültiges JSON",
        "invalid_project_path": "Wählen Sie ein vorhandenes Projekt-JSON.",
        "invalid_source_path": "Die Quell-.world-Datei wurde nicht gefunden.",
        "output_same_title": "Überschreiben verboten",
        "output_same_message": "Der Ausgabepfad darf nicht mit der Quellwelt übereinstimmen.",
        "invalid_output": "Wählen Sie einen Ausgabepfad für die .world-Datei. Die Endung .world wird bei Bedarf automatisch ergänzt.",
        "invalid_planet_name": "Geben Sie einen nicht leeren Planetennamen ein oder deaktivieren Sie die Synchronisierung.",
        "invalid_universe_folder": "Wählen Sie den Ordner storage\\universe mit universe.chunks.",
        "chunks_confirm_title": "Sternenkarten-Datenbank ändern",
        "chunks_confirm_message": "Nach dem Erstellen der neuen Welt wird universe.chunks direkt aktualisiert und vorher automatisch gesichert.\n\n{chunks}\n\nSpiel und Server müssen vollständig beendet sein. Fortfahren?",
        "unknown": "unbekannt",
        "project_loaded_status": "Projekt geladen: {name}; Welt {size}, {biomes} Biome, Wetter: {weather}",
        "importing": "Welt wird erstellt und universe.chunks bei Bedarf synchronisiert…",
        "import_success_status": "Erstellt und geprüft: {output}",
        "import_done": "World-Erstellung abgeschlossen",
        "import_done_message": "Die neue Datei wurde erstellt und hat die Struktur- und Datensatzprüfung bestanden:\n\n{output}{sync_details}\n\nErstellen Sie eine Sicherung und testen Sie sie nur bei vollständig beendetem Spiel.",
        "chunks_backup_details": "\n\nDer Sternenkartenname wurde synchronisiert. Automatische Sicherung von universe.chunks:\n{backup}",
    },
}


class ImportWindow(BaseWindow):
    def __init__(self) -> None:
        super().__init__(TRANSLATIONS, "860x590")
        self.project_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.planet_name_var = tk.StringVar()
        self.universe_folder_var = tk.StringVar()
        self.sync_star_map_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar()
        self._loading_project = False
        self.planet_name_var.trace_add("write", self.planet_name_changed)

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
        self.project_label, _, self.project_browse = self.add_path_row(
            form, 0, "", self.project_var, self.browse_project
        )
        self.source_label, _, self.source_browse = self.add_path_row(
            form, 1, "", self.source_var, self.browse_source
        )
        self.output_label, _, self.output_browse = self.add_path_row(
            form, 2, "", self.output_var, self.browse_output
        )
        self.planet_name_label = ttk.Label(form)
        self.planet_name_label.grid(row=3, column=0, sticky="w", pady=6)
        self.planet_name_entry = ttk.Entry(form, textvariable=self.planet_name_var)
        self.planet_name_entry.grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=6
        )
        self.universe_folder_label, _, self.universe_folder_browse = self.add_path_row(
            form, 4, "", self.universe_folder_var, self.browse_universe_folder
        )
        self.sync_star_map_check = ttk.Checkbutton(
            form, variable=self.sync_star_map_var
        )
        self.sync_star_map_check.grid(
            row=5, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(3, 1)
        )
        self.universe_hint_label = ttk.Label(
            form, style="Hint.TLabel", wraplength=650
        )
        self.universe_hint_label.grid(
            row=6, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(0, 6)
        )

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(6, 8))
        self.action_button = ttk.Button(
            buttons, style="Accent.TButton", command=self.import_project
        )
        self.action_button.pack(side="left")
        self.open_folder_button = ttk.Button(buttons, command=self.open_output_folder)
        self.open_folder_button.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 8))
        ttk.Label(outer, textvariable=self.status_var, wraplength=760).pack(anchor="w")
        self.set_status_key("initial_status")
        self.apply_language()

    def apply_language(self) -> None:
        super().apply_language()
        self.title_label.configure(text=self.tr("page_title"))
        self.subtitle_label.configure(text=self.tr("subtitle"))
        self.project_label.configure(text=self.tr("project_label"))
        self.source_label.configure(text=self.tr("source_label"))
        self.output_label.configure(text=self.tr("output_label"))
        self.planet_name_label.configure(text=self.tr("planet_name_label"))
        self.universe_folder_label.configure(text=self.tr("universe_folder_label"))
        self.sync_star_map_check.configure(text=self.tr("sync_star_map"))
        self.universe_hint_label.configure(text=self.tr("universe_hint"))
        self.project_browse.configure(text=self.tr("browse"))
        self.source_browse.configure(text=self.tr("browse"))
        self.output_browse.configure(text=self.tr("browse"))
        self.universe_folder_browse.configure(text=self.tr("browse"))
        self.action_button.configure(text=self.tr("start_import"))
        self.open_folder_button.configure(text=self.tr("open_folder"))

    def browse_project(self) -> None:
        path = self.choose_open_file(
            self.project_var,
            self.tr("select_project"),
            [(self.tr("json_file"), "*.json"), (self.tr("all_files"), "*.*")],
        )
        if not path:
            return
        project = Path(path)
        try:
            header = read_project_header(project, self.language_code)
        except Exception as exc:
            messagebox.showerror(
                self.tr("invalid_json_title"), str(exc), parent=self.root
            )
            return
        self.source_var.set(str(header["sourceWorld"]))
        self.output_var.set(str(project.parent / swe.suggested_world_output_path(project).name))
        self._loading_project = True
        try:
            self.planet_name_var.set(str(header.get("worldName", "")))
            self.sync_star_map_var.set(False)
        finally:
            self._loading_project = False
        source_path = Path(str(header["sourceWorld"]))
        if (source_path.parent / "universe.chunks").is_file():
            self.universe_folder_var.set(str(source_path.parent))
        size = header["size"]
        size_text = f"{size[0]}×{size[1]}" if len(size) == 2 else self.tr("unknown")
        self.set_status_key(
            "project_loaded_status",
            name=header.get("worldName") or self.tr("unknown"),
            size=size_text,
            biomes=header["biomes"],
            weather=header["weather"],
        )

    def browse_source(self) -> None:
        path = self.choose_open_file(
            self.source_var,
            self.tr("select_source"),
            [(self.tr("world_file"), "*.world"), (self.tr("all_files"), "*.*")],
        )
        if path:
            parent = Path(path).parent
            if (parent / "universe.chunks").is_file():
                self.universe_folder_var.set(str(parent))

    def browse_output(self) -> None:
        self.choose_save_file(
            self.output_var,
            self.tr("save_world"),
            ".world",
            [(self.tr("world_file"), "*.world")],
        )

    def browse_universe_folder(self) -> None:
        self.choose_directory(
            self.universe_folder_var, self.tr("select_universe_folder")
        )

    def planet_name_changed(self, *_args) -> None:
        if not self._loading_project and self.planet_name_var.get().strip():
            self.sync_star_map_var.set(True)

    def import_project(self) -> None:
        project = Path(self.project_var.get().strip())
        if not project.is_file():
            messagebox.showerror(
                self.tr("invalid_path"),
                self.tr("invalid_project_path"),
                parent=self.root,
            )
            return
        source = Path(self.source_var.get().strip())
        if not source.is_file():
            messagebox.showerror(
                self.tr("invalid_path"),
                self.tr("invalid_source_path"),
                parent=self.root,
            )
            return
        raw_output = self.output_var.get().strip()
        if not raw_output:
            messagebox.showerror(
                self.tr("invalid_path"),
                self.tr("invalid_output"),
                parent=self.root,
            )
            return
        if not raw_output.lower().endswith(".world"):
            raw_output += ".world"
            self.output_var.set(raw_output)
        output = Path(raw_output)
        if source.resolve() == output.resolve():
            messagebox.showerror(
                self.tr("output_same_title"),
                self.tr("output_same_message"),
                parent=self.root,
            )
            return
        if not self.confirm_replace(output):
            return

        planet_name = self.planet_name_var.get().strip()
        sync_star_map = self.sync_star_map_var.get()
        chunks_path = None
        if sync_star_map:
            if not planet_name:
                messagebox.showerror(
                    self.tr("invalid_path"),
                    self.tr("invalid_planet_name"),
                    parent=self.root,
                )
                return
            universe_folder = Path(self.universe_folder_var.get().strip())
            chunks_path = universe_folder / "universe.chunks"
            if not universe_folder.is_dir() or not chunks_path.is_file():
                messagebox.showerror(
                    self.tr("invalid_path"),
                    self.tr("invalid_universe_folder"),
                    parent=self.root,
                )
                return
            if not self.ask_yes_no(
                self.tr("chunks_confirm_title"),
                self.tr("chunks_confirm_message", chunks=chunks_path),
            ):
                return

        def task():
            result = swe.import_world_project(
                project,
                output,
                source,
                world_name_override=planet_name or None,
            )
            backup = None
            if chunks_path is not None:
                _source, document = result
                backup = swe.rename_world_in_celestial_database(
                    chunks_path, document, planet_name
                )
            return result, backup

        def success(result):
            (_database, _document), backup = result
            self.last_output = output
            self.set_status_key("import_success_status", output=output)
            sync_details = (
                self.tr("chunks_backup_details", backup=backup) if backup else ""
            )
            messagebox.showinfo(
                self.tr("import_done"),
                self.tr(
                    "import_done_message", output=output, sync_details=sync_details
                ),
                parent=self.root,
            )

        self.run_worker(task, success, "importing")


if __name__ == "__main__":
    ImportWindow().run()
