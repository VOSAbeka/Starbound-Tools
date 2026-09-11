from __future__ import annotations

from pathlib import Path
import shutil
import tkinter as tk
from tkinter import messagebox, ttk

from gui_common import BaseWindow, read_project_header
import starbound_world_editor as swe


TRANSLATIONS = {
    "en": {
        "window_title": "02 — Starbound Editable JSON → World",
        "page_title": "Apply editable JSON to a Starbound world",
        "subtitle": "Select the edited JSON and its original .world. The original is permanently backed up in the same folder, the edit is built from that backup, and the verified result replaces the original path.",
        "project_label": "Project .json",
        "source_label": "Original source .world",
        "output_label": "New output .world path",
        "planet_name_label": "New planet name (optional)",
        "universe_folder_label": "Universe storage folder",
        "sync_star_map": "Synchronize this name to the star map (backs up and updates universe.chunks)",
        "keep_backup": "Keep the original-world backup (recommended)",
        "backup_keep_note": "The timestamped backup will remain permanently in the original folder.",
        "backup_discard_note": "The backup is still used as the safe build source, but is removed after a verified replacement.",
        "backup_not_retained": "Not retained (option was disabled)",
        "universe_hint": "Required only when star-map synchronization is selected. The folder must contain universe.chunks, and the game/server must be fully closed.",
        "start_import": "Back up and update World",
        "open_folder": "Open folder",
        "initial_status": "Select an exported and edited project JSON. The original Starbound/OpenStarbound world format is preserved.",
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
        "replace_confirm_title": "Back up and replace the original world",
        "replace_confirm": "The original world will first be copied to a timestamped backup in the same folder. The JSON edit will be built from that backup and will replace the original path only after complete verification. {backup_note}\n\nOriginal world:\n{source}\n\nThe game and server must be fully closed. Continue?",
        "importing": "Backing up and rebuilding the world and, if selected, synchronizing universe.chunks…",
        "import_success_status": "Updated and verified: {output}; backup: {backup}",
        "import_done": "World update complete",
        "import_done_message": "The original path was replaced only after structural and record-by-record verification:\n\n{output}\n\nPermanent original-world backup:\n{backup}{sync_details}\n\nKeep the backup and test only while the game is fully closed.",
        "chunks_backup_details": "\n\nThe star-map name was synchronized. Automatic universe.chunks backup:\n{backup}",
    },
    "zh": {
        "window_title": "02 — 可编辑 JSON → Starbound 世界",
        "page_title": "把可编辑 JSON 应用到 Starbound 世界",
        "subtitle": "选择修改后的 JSON 和对应的原始 .world。程序会先在同目录永久备份原文件，再以备份为来源生成修改版；完整校验通过后才替换原路径。",
        "project_label": "项目 .json",
        "source_label": "原始来源 .world",
        "output_label": "输出新 .world 路径",
        "planet_name_label": "新星球名称（可选）",
        "universe_folder_label": "Universe 存档文件夹",
        "sync_star_map": "把新名称同步到星图（自动备份并修改 universe.chunks）",
        "keep_backup": "保留原 world 备份（推荐）",
        "backup_keep_note": "时间戳备份会永久保留在原文件夹中。",
        "backup_discard_note": "程序仍以临时备份作为安全来源，但校验并替换成功后会删除该备份。",
        "backup_not_retained": "未保留（已取消备份选项）",
        "universe_hint": "仅勾选同步星图时需要；请选择包含 universe.chunks 的 storage\\universe 文件夹，并确保游戏和服务器已完全退出。",
        "start_import": "备份并更新 World",
        "open_folder": "打开所在文件夹",
        "initial_status": "请选择导出并编辑过的项目 JSON；导入时保留原文件的 Starbound/OpenStarbound 格式。",
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
        "replace_confirm_title": "备份并替换原 world",
        "replace_confirm": "程序会先把原 world 复制成同目录的时间戳备份，再以该备份为来源应用 JSON。只有完整校验通过后才会替换原路径。{backup_note}\n\n原 world：\n{source}\n\n请确保游戏和服务器已经完全退出。是否继续？",
        "importing": "正在备份并重建 world，并按选择同步 universe.chunks……",
        "import_success_status": "更新并验证成功：{output}；备份：{backup}",
        "import_done": "World 更新完成",
        "import_done_message": "修改版通过结构与逐记录校验后，已经替换原路径：\n\n{output}\n\n永久保留的原 world 备份：\n{backup}{sync_details}\n\n请保留备份，并只在游戏完全退出时测试。",
        "chunks_backup_details": "\n\n星图名称已经同步；universe.chunks 的自动备份位于：\n{backup}",
    },
    "de": {
        "window_title": "02 — Bearbeitbares Starbound-JSON → Welt",
        "page_title": "Bearbeitbares JSON auf eine Starbound-Welt anwenden",
        "subtitle": "Wählen Sie Projekt-JSON und ursprüngliche .world. Das Original wird dauerhaft im selben Ordner gesichert; die geprüfte Änderung wird aus dieser Sicherung gebaut und ersetzt anschließend den Originalpfad.",
        "project_label": "Projekt-.json",
        "source_label": "Ursprüngliche Quell-.world",
        "output_label": "Pfad der neuen Ausgabe-.world",
        "planet_name_label": "Neuer Planetenname (optional)",
        "universe_folder_label": "Universe-Speicherordner",
        "sync_star_map": "Namen mit der Sternenkarte synchronisieren (universe.chunks sichern und ändern)",
        "keep_backup": "Sicherung der Originalwelt behalten (empfohlen)",
        "backup_keep_note": "Die Zeitstempel-Sicherung bleibt dauerhaft im Originalordner erhalten.",
        "backup_discard_note": "Die Sicherung wird weiterhin als sichere Bauquelle verwendet, nach erfolgreicher Prüfung und Ersetzung jedoch entfernt.",
        "backup_not_retained": "Nicht behalten (Option deaktiviert)",
        "universe_hint": "Nur für die Sternenkarten-Synchronisierung erforderlich. Der Ordner muss universe.chunks enthalten; Spiel und Server müssen beendet sein.",
        "start_import": "World sichern und aktualisieren",
        "open_folder": "Ordner öffnen",
        "initial_status": "Wählen Sie ein exportiertes und bearbeitetes Projekt-JSON. Das ursprüngliche Starbound/OpenStarbound-Weltformat bleibt erhalten.",
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
        "replace_confirm_title": "Originalwelt sichern und ersetzen",
        "replace_confirm": "Die Originalwelt wird zuerst als Zeitstempel-Sicherung im selben Ordner kopiert. Die JSON-Änderung wird daraus erstellt und ersetzt den Originalpfad erst nach vollständiger Prüfung. {backup_note}\n\nOriginalwelt:\n{source}\n\nSpiel und Server müssen vollständig beendet sein. Fortfahren?",
        "importing": "Welt wird gesichert und neu erstellt; universe.chunks wird bei Bedarf synchronisiert…",
        "import_success_status": "Aktualisiert und geprüft: {output}; Sicherung: {backup}",
        "import_done": "World-Aktualisierung abgeschlossen",
        "import_done_message": "Der Originalpfad wurde erst nach erfolgreicher Struktur- und Datensatzprüfung ersetzt:\n\n{output}\n\nDauerhafte Sicherung der Originalwelt:\n{backup}{sync_details}\n\nBewahren Sie die Sicherung auf und testen Sie nur bei vollständig beendetem Spiel.",
        "chunks_backup_details": "\n\nDer Sternenkartenname wurde synchronisiert. Automatische Sicherung von universe.chunks:\n{backup}",
    },
}


class ImportWindow(BaseWindow):
    def __init__(self) -> None:
        super().__init__(TRANSLATIONS, "860x545")
        self.project_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.planet_name_var = tk.StringVar()
        self.universe_folder_var = tk.StringVar()
        self.sync_star_map_var = tk.BooleanVar(value=False)
        self.keep_backup_var = tk.BooleanVar(value=True)
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
        self.planet_name_label = ttk.Label(form)
        self.planet_name_label.grid(row=2, column=0, sticky="w", pady=6)
        self.planet_name_entry = ttk.Entry(form, textvariable=self.planet_name_var)
        self.planet_name_entry.grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=6
        )
        self.universe_folder_label, _, self.universe_folder_browse = self.add_path_row(
            form, 3, "", self.universe_folder_var, self.browse_universe_folder
        )
        self.sync_star_map_check = ttk.Checkbutton(
            form, variable=self.sync_star_map_var
        )
        self.sync_star_map_check.grid(
            row=4, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(3, 1)
        )
        self.universe_hint_label = ttk.Label(
            form, style="Hint.TLabel", wraplength=650
        )
        self.universe_hint_label.grid(
            row=5, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(0, 6)
        )
        self.keep_backup_check = ttk.Checkbutton(
            form, variable=self.keep_backup_var
        )
        self.keep_backup_check.grid(
            row=6, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(2, 6)
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
        self.planet_name_label.configure(text=self.tr("planet_name_label"))
        self.universe_folder_label.configure(text=self.tr("universe_folder_label"))
        self.sync_star_map_check.configure(text=self.tr("sync_star_map"))
        self.keep_backup_check.configure(text=self.tr("keep_backup"))
        self.universe_hint_label.configure(text=self.tr("universe_hint"))
        self.project_browse.configure(text=self.tr("browse"))
        self.source_browse.configure(text=self.tr("browse"))
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
        if not self.ask_yes_no(
            self.tr("replace_confirm_title"),
            self.tr(
                "replace_confirm",
                source=source,
                backup_note=self.tr(
                    "backup_keep_note"
                    if self.keep_backup_var.get()
                    else "backup_discard_note"
                ),
            ),
        ):
            return
        keep_backup = self.keep_backup_var.get()

        def task():
            chunks_backup = None

            def builder(backup_source, staged_output):
                nonlocal chunks_backup
                result = swe.import_world_project(
                    project,
                    staged_output,
                    backup_source,
                    world_name_override=planet_name or None,
                )
                if chunks_path is not None:
                    _source, document = result
                    chunks_backup = swe.rename_world_in_celestial_database(
                        chunks_path, document, planet_name
                    )
                return result

            try:
                backup, result = swe.rebuild_world_in_place_from_backup(
                    source, builder, keep_backup=keep_backup
                )
            except Exception:
                if chunks_backup is not None and chunks_backup.is_file():
                    shutil.copy2(chunks_backup, chunks_path)
                raise
            return backup, result, chunks_backup

        def success(result):
            backup, (_database, _document), chunks_backup = result
            backup_display = (
                str(backup) if backup is not None else self.tr("backup_not_retained")
            )
            self.last_output = source
            self.set_status_key(
                "import_success_status", output=source, backup=backup_display
            )
            sync_details = (
                self.tr("chunks_backup_details", backup=chunks_backup)
                if chunks_backup
                else ""
            )
            messagebox.showinfo(
                self.tr("import_done"),
                self.tr(
                    "import_done_message",
                    output=source,
                    backup=backup_display,
                    sync_details=sync_details,
                ),
                parent=self.root,
            )

        self.run_worker(task, success, "importing")


if __name__ == "__main__":
    ImportWindow().run()
