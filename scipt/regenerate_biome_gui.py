from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from gui_common import BaseWindow
import starbound_world_editor as swe


TRANSLATIONS = {
    "en": {
        "window_title": "Starbound Biome Sector Regenerator",
        "page_title": "Convert and regenerate a rectangular biome area",
        "subtitle": "Scan assets and rebuild the selected area from the target biome's complete terrain, liquid, placeable, monster and parallax recipe.",
        "source_label": "Source .world",
        "assets_label": "Starbound assets folder",
        "output_label": "New output .world path",
        "x_start_label": "X start (tile, inclusive)",
        "x_end_label": "X end (tile, inclusive)",
        "y_start_label": "Y start (tile, inclusive)",
        "y_end_label": "Y end (tile, inclusive)",
        "biome_label": "Target biome type",
        "select_source": "Select the Starbound world",
        "select_assets": "Select the Starbound assets folder",
        "save_world": "Save the regenerated Starbound world",
        "world_file": "Starbound world",
        "load_world": "Load world information",
        "scan_assets": "Scan assets and fill biome list",
        "start": "Create reset World",
        "open_folder": "Open folder",
        "initial_status": "Select a .world and an assets folder. The folder may contain packed.pak or unpacked assets.",
        "assets_scanned": "Assets scanned: {asset_mode}, {asset_biomes} biome definitions. Select and load a world; missing biomes can be compiled into it.",
        "world_loaded": "Loaded: {width}×{height} tiles. Assets: {asset_mode}, {asset_biomes} definitions; {compiled} already exist and the rest can be compiled. Valid X range: 0–{max_x}.",
        "existing_biome": "existing indexes: {indexes}",
        "new_biome": "compile from assets",
        "loading": "Reading world metadata…",
        "processing": "Compiling the full biome recipe, removing matching tile/entity sectors, then rebuilding and verifying BTreeDB5…",
        "invalid_source": "Select an existing World4 .world file.",
        "invalid_assets": "Select an assets folder containing packed.pak or unpacked .biome files.",
        "invalid_output": "Select or enter an output .world path. The .world extension is added automatically if omitted.",
        "same_output": "The output cannot overwrite the source world.",
        "invalid_x": "X start and X end must be integers from 0 to {max_x}.",
        "invalid_y": "Y start and Y end must be increasing integers from 0 to {max_y}.",
        "invalid_biome": "Scan/load the selected assets and choose or type a biome that exists in them.",
        "confirm_title": "Confirm full sector reset",
        "confirm": "The whole selected rectangle will be assigned to the target biome and regenerated. Its biome blocks, placeables, monsters, parallax, music and environment settings will change. Every intersecting 32×32 sector is fully reset, including player-built blocks, liquids, placed objects and stored entities.\n\nThe source world is not modified. The actual reset area expands outward to sector boundaries.\n\nX: {x_start} to {x_end}\nY: {y_start} to {y_end}\nTarget biome: {biome}\n\nContinue?",
        "done_title": "Reset world created",
        "done": "Created and verified:\n\n{output}\n\nTarget biome indexes: {target_indexes}\nChanged vertical layout bands: {layers}\nReset sectors: {sectors}\nRemoved tile records: {tiles}\nRemoved entity records: {entities}\nRemoved unique-index entries: {unique_entries}{compile_notes}\n\nWith the game/server fully closed, back up the original, replace it with this output, and rename the output to the original world's exact filename. Missing sectors regenerate as the target biome when approached.",
        "compile_notes": "\nAsset compiler notes: {notes}",
        "ocean_notes": "\nOcean pairing: lower {floor} / upper {surface}; transition Y = {transition}.",
        "done_status": "Created {output}; reset {sectors} sectors. They regenerate when visited.",
    },
    "zh": {
        "window_title": "Starbound Biome 区域重新生成工具",
        "page_title": "转换并重新生成一块矩形 biome 区域",
        "subtitle": "扫描 assets，并按目标 biome 的完整地形、液体、放置物、怪物和 parallax 配方重新生成选区。",
        "source_label": "来源 .world",
        "assets_label": "Starbound assets 文件夹",
        "output_label": "输出新 .world 路径",
        "x_start_label": "X 起点（格，包含）",
        "x_end_label": "X 终点（格，包含）",
        "y_start_label": "Y 起点（格，包含）",
        "y_end_label": "Y 终点（格，包含）",
        "biome_label": "Target biome type（目标 biome）",
        "select_source": "选择 Starbound world",
        "select_assets": "选择 Starbound assets 文件夹",
        "save_world": "保存重新生成的 Starbound world",
        "world_file": "Starbound 世界",
        "load_world": "读取 world 信息",
        "scan_assets": "扫描 Assets 并填充 biome",
        "start": "生成重置后的 World",
        "open_folder": "打开所在文件夹",
        "initial_status": "请选择 .world 和 assets 文件夹；文件夹中可以是 packed.pak，也可以是解包后的 assets。",
        "assets_scanned": "Assets 扫描完成：{asset_mode}，读取到 {asset_biomes} 个 biome 定义。选择并读取 world 后，缺少的 biome 也可以编译加入。",
        "world_loaded": "已读取：{width}×{height} 格。Assets：{asset_mode}，共 {asset_biomes} 个定义；其中 {compiled} 个已存在，其余可从 assets 编译。有效 X 范围：0–{max_x}。",
        "existing_biome": "已有 indexes：{indexes}",
        "new_biome": "从 assets 新编译",
        "loading": "正在读取 world 元数据……",
        "processing": "正在编译完整 biome 配方、删除匹配的 tile/实体 sector，然后重建并验证 BTreeDB5……",
        "invalid_source": "请选择存在的 World4 .world 文件。",
        "invalid_assets": "请选择包含 packed.pak 或解包 .biome 文件的 assets 文件夹。",
        "invalid_output": "请选择或输入输出 .world 的完整路径；未填写 .world 后缀时会自动添加。",
        "same_output": "输出文件不能覆盖来源 world。",
        "invalid_x": "X 起点和终点必须是 0 到 {max_x} 之间的整数。",
        "invalid_y": "Y 起点和终点必须是 0 到 {max_y} 之间递增的整数。",
        "invalid_biome": "请先扫描并读取 assets，然后选择或输入 assets 中存在的 biome。",
        "confirm_title": "确认完整重置 sector",
        "confirm": "所选矩形会整体改成目标 biome 并重新生成，其 biome 物块、placeables、怪物、parallax、音乐和环境设置都会改变。所有相交的 32×32 sector 会完整重置，包括玩家建筑物块、液体、放置的 object 和已保存实体。\n\n来源 world 不会被修改；实际重置范围会向外对齐到 sector 边界。\n\nX：{x_start} 到 {x_end}\nY：{y_start} 到 {y_end}\n目标 biome：{biome}\n\n是否继续？",
        "done_title": "已生成重置后的 world",
        "done": "已经生成并验证：\n\n{output}\n\n目标 biome indexes：{target_indexes}\n修改纵向布局层：{layers}\n重置 sector：{sectors}\n删除 tile 记录：{tiles}\n删除实体记录：{entities}\n删除唯一实体索引：{unique_entries}{compile_notes}\n\n请完全退出游戏和服务器，备份原文件，再用输出替换原文件并改回原 world 完全相同的文件名。玩家靠近缺失 sector 时，它们会按照目标 biome 重新生成。",
        "compile_notes": "\nAssets 编译说明：{notes}",
        "ocean_notes": "\n海洋分层：下层 {floor} / 上层 {surface}；分界 Y = {transition}。",
        "done_status": "已生成 {output}；重置 {sectors} 个 sector，进入附近时会重新生成。",
    },
    "de": {
        "window_title": "Starbound-Biomsektor-Regenerator",
        "page_title": "Ein rechteckiges Biomgebiet umwandeln und neu erzeugen",
        "subtitle": "Assets scannen und das Gebiet aus dem vollständigen Gelände-, Flüssigkeits-, Placeable-, Monster- und Parallaxenrezept des Zielbioms neu aufbauen.",
        "source_label": "Quell-.world",
        "assets_label": "Starbound-Assets-Ordner",
        "output_label": "Pfad der neuen Ausgabe-.world",
        "x_start_label": "X-Anfang (Kachel, inklusiv)",
        "x_end_label": "X-Ende (Kachel, inklusiv)",
        "y_start_label": "Y-Anfang (Kachel, inklusiv)",
        "y_end_label": "Y-Ende (Kachel, inklusiv)",
        "biome_label": "Zielbiomtyp",
        "select_source": "Starbound-Welt auswählen",
        "select_assets": "Starbound-Assets-Ordner auswählen",
        "save_world": "Neu erzeugte Starbound-Welt speichern",
        "world_file": "Starbound-Welt",
        "load_world": "Weltinformationen laden",
        "scan_assets": "Assets scannen und Biomliste füllen",
        "start": "Zurückgesetzte Welt erstellen",
        "open_folder": "Ordner öffnen",
        "initial_status": "Wählen Sie eine .world und einen Assets-Ordner mit packed.pak oder entpackten Assets.",
        "assets_scanned": "Assets gescannt: {asset_mode}, {asset_biomes} Biomdefinitionen. Fehlende Biome können nach dem Laden einer Welt kompiliert werden.",
        "world_loaded": "Geladen: {width}×{height} Kacheln. Assets: {asset_mode}, {asset_biomes} Definitionen; {compiled} sind bereits vorhanden, der Rest kann kompiliert werden. Gültiger X-Bereich: 0–{max_x}.",
        "existing_biome": "vorhandene Indizes: {indexes}",
        "new_biome": "aus Assets kompilieren",
        "loading": "Weltmetadaten werden gelesen…",
        "processing": "Das vollständige Biomrezept wird kompiliert; passende Kachel-/Entitätssektoren werden entfernt und BTreeDB5 wird neu aufgebaut und geprüft…",
        "invalid_source": "Wählen Sie eine vorhandene World4-.world-Datei.",
        "invalid_assets": "Wählen Sie einen Assets-Ordner mit packed.pak oder entpackten .biome-Dateien.",
        "invalid_output": "Wählen Sie einen Ausgabepfad für die .world-Datei. Die Endung .world wird bei Bedarf automatisch ergänzt.",
        "same_output": "Die Ausgabe darf die Quellwelt nicht überschreiben.",
        "invalid_x": "X-Anfang und X-Ende müssen ganze Zahlen von 0 bis {max_x} sein.",
        "invalid_y": "Y-Anfang und Y-Ende müssen aufsteigende ganze Zahlen von 0 bis {max_y} sein.",
        "invalid_biome": "Scannen/laden Sie die Assets und wählen oder schreiben Sie ein darin vorhandenes Biom.",
        "confirm_title": "Vollständiges Zurücksetzen bestätigen",
        "confirm": "Das gesamte gewählte Rechteck wird dem Zielbiom zugewiesen und neu erzeugt. Biomblöcke, Placeables, Monster, Parallaxe, Musik und Umgebungseinstellungen ändern sich. Jeder berührte 32×32-Sektor wird vollständig zurückgesetzt, einschließlich Spielerbauten, Flüssigkeiten, platzierter Objekte und gespeicherter Entitäten.\n\nDie Quellwelt wird nicht verändert; der tatsächliche Bereich wird auf Sektorgrenzen erweitert.\n\nX: {x_start} bis {x_end}\nY: {y_start} bis {y_end}\nZielbiom: {biome}\n\nFortfahren?",
        "done_title": "Zurückgesetzte Welt erstellt",
        "done": "Erstellt und geprüft:\n\n{output}\n\nZielbiomindizes: {target_indexes}\nGeänderte vertikale Layoutbänder: {layers}\nZurückgesetzte Sektoren: {sectors}\nEntfernte Kacheldatensätze: {tiles}\nEntfernte Entitätsdatensätze: {entities}\nEntfernte eindeutige Indexeinträge: {unique_entries}{compile_notes}\n\nBeenden Sie Spiel und Server vollständig, sichern und ersetzen Sie das Original und benennen Sie die Ausgabe exakt wie die ursprüngliche World-Datei. Fehlende Sektoren werden beim Annähern als Zielbiom neu erzeugt.",
        "compile_notes": "\nHinweise des Asset-Compilers: {notes}",
        "ocean_notes": "\nOzeanschichtung: unten {floor} / oben {surface}; Übergang Y = {transition}.",
        "done_status": "{output} erstellt; {sectors} Sektoren zurückgesetzt. Sie werden beim Besuch neu erzeugt.",
    },
}


class RegenerateWindow(BaseWindow):
    def __init__(self) -> None:
        super().__init__(TRANSLATIONS, "900x655")
        self.source_var = tk.StringVar()
        self.assets_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.x_start_var = tk.StringVar(value="0")
        self.x_end_var = tk.StringVar()
        self.y_start_var = tk.StringVar(value="0")
        self.y_end_var = tk.StringVar()
        self.biome_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.world_size: tuple[int, int] | None = None
        self.loaded_source: Path | None = None
        self.loaded_assets: Path | None = None
        self.biome_choices: dict[str, dict[str, object]] = {}
        self.biome_choices_by_name: dict[str, dict[str, object]] = {}
        self.asset_biome_names: list[str] = []

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        self.title_label = ttk.Label(header, style="Title.TLabel")
        self.title_label.pack(side="left", anchor="nw")
        self.add_language_selector(header).pack(side="right", anchor="ne")
        self.subtitle_label = ttk.Label(outer, style="Hint.TLabel", wraplength=820)
        self.subtitle_label.pack(anchor="w", pady=(4, 14))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.source_label, _, self.source_browse = self.add_path_row(
            form, 0, "", self.source_var, self.browse_source
        )
        self.assets_label, _, self.assets_browse = self.add_path_row(
            form, 1, "", self.assets_var, self.browse_assets
        )
        self.output_label, _, self.output_browse = self.add_path_row(
            form, 2, "", self.output_var, self.browse_output
        )

        load_buttons = ttk.Frame(outer)
        load_buttons.pack(fill="x", pady=(4, 2))
        self.scan_assets_button = ttk.Button(
            load_buttons, command=self.scan_assets
        )
        self.scan_assets_button.pack(side="left")
        self.load_world_button = ttk.Button(
            load_buttons, command=self.load_selected_world
        )
        self.load_world_button.pack(side="left", padx=8)

        range_frame = ttk.Frame(outer)
        range_frame.pack(fill="x", pady=(8, 4))
        range_frame.columnconfigure(1, weight=1)
        range_frame.columnconfigure(3, weight=1)
        self.x_start_label = ttk.Label(range_frame)
        self.x_start_label.grid(row=0, column=0, sticky="w")
        ttk.Entry(range_frame, textvariable=self.x_start_var, width=14).grid(
            row=0, column=1, sticky="ew", padx=(8, 20)
        )
        self.x_end_label = ttk.Label(range_frame)
        self.x_end_label.grid(row=0, column=2, sticky="w")
        ttk.Entry(range_frame, textvariable=self.x_end_var, width=14).grid(
            row=0, column=3, sticky="ew", padx=(8, 0)
        )
        self.y_start_label = ttk.Label(range_frame)
        self.y_start_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(range_frame, textvariable=self.y_start_var, width=14).grid(
            row=1, column=1, sticky="ew", padx=(8, 20), pady=(8, 0)
        )
        self.y_end_label = ttk.Label(range_frame)
        self.y_end_label.grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(range_frame, textvariable=self.y_end_var, width=14).grid(
            row=1, column=3, sticky="ew", padx=(8, 0), pady=(8, 0)
        )

        biome_frame = ttk.Frame(outer)
        biome_frame.pack(fill="x", pady=(8, 4))
        self.biome_label = ttk.Label(biome_frame)
        self.biome_label.pack(side="left")
        self.biome_combo = ttk.Combobox(
            biome_frame,
            textvariable=self.biome_var,
            state="normal",
            width=48,
        )
        self.biome_combo.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.warning_label = ttk.Label(
            outer, style="Hint.TLabel", wraplength=830
        )
        self.warning_label.pack(anchor="w", pady=(10, 10))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(4, 8))
        self.action_button = ttk.Button(
            buttons, style="Accent.TButton", command=self.regenerate
        )
        self.action_button.pack(side="left")
        self.open_folder_button = ttk.Button(buttons, command=self.open_output_folder)
        self.open_folder_button.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 8))
        ttk.Label(outer, textvariable=self.status_var, wraplength=830).pack(anchor="w")
        self.set_status_key("initial_status")
        self.apply_language()

    def apply_language(self) -> None:
        super().apply_language()
        self.title_label.configure(text=self.tr("page_title"))
        self.subtitle_label.configure(text=self.tr("subtitle"))
        self.source_label.configure(text=self.tr("source_label"))
        self.assets_label.configure(text=self.tr("assets_label"))
        self.output_label.configure(text=self.tr("output_label"))
        self.source_browse.configure(text=self.tr("browse"))
        self.assets_browse.configure(text=self.tr("browse"))
        self.output_browse.configure(text=self.tr("browse"))
        self.scan_assets_button.configure(text=self.tr("scan_assets"))
        self.load_world_button.configure(text=self.tr("load_world"))
        self.x_start_label.configure(text=self.tr("x_start_label"))
        self.x_end_label.configure(text=self.tr("x_end_label"))
        self.y_start_label.configure(text=self.tr("y_start_label"))
        self.y_end_label.configure(text=self.tr("y_end_label"))
        self.biome_label.configure(text=self.tr("biome_label"))
        self.warning_label.configure(text=self.tr("confirm").split("\n\n")[0])
        self.action_button.configure(text=self.tr("start"))
        self.open_folder_button.configure(text=self.tr("open_folder"))

    def browse_source(self) -> None:
        path = self.choose_open_file(
            self.source_var,
            self.tr("select_source"),
            [(self.tr("world_file"), "*.world"), (self.tr("all_files"), "*.*")],
        )
        if not path:
            return
        source = Path(path)
        self.output_var.set(str(source.with_name(source.stem + "_regenerated.world")))

    def browse_assets(self) -> None:
        path = self.choose_directory(self.assets_var, self.tr("select_assets"))
        if path:
            self.loaded_assets = None

    def browse_output(self) -> None:
        self.choose_save_file(
            self.output_var,
            self.tr("save_world"),
            ".world",
            [(self.tr("world_file"), "*.world")],
        )

    def scan_assets(self) -> None:
        raw_assets = self.assets_var.get().strip()
        assets = Path(raw_assets)
        if not raw_assets or not assets.is_dir():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_assets"), parent=self.root
            )
            return
        try:
            catalog = swe.load_asset_biome_catalog(assets)
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        self.asset_biome_names = sorted(catalog["biomes"], key=str.casefold)
        self.biome_choices = {}
        self.biome_choices_by_name = {}
        self.biome_combo.configure(values=self.asset_biome_names)
        if self.asset_biome_names:
            self.biome_var.set(self.asset_biome_names[0])
        self.set_status_key(
            "assets_scanned",
            asset_mode=catalog["mode"],
            asset_biomes=len(self.asset_biome_names),
        )

    def load_selected_world(self) -> None:
        source = Path(self.source_var.get().strip())
        assets = Path(self.assets_var.get().strip())
        if not source.is_file():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_source"), parent=self.root
            )
            return
        if not assets.is_dir():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_assets"), parent=self.root
            )
            return
        self.load_world(source, assets)

    def load_world(
        self, source: Path, assets: Path, preserve_coordinate_entries: bool = False
    ) -> None:
        previous_coordinates = (
            self.x_start_var.get(),
            self.x_end_var.get(),
            self.y_start_var.get(),
            self.y_end_var.get(),
        )
        previous_choice = self.biome_var.get().strip()
        previous_selection = self.biome_choices.get(previous_choice)
        previous_name = (
            str(previous_selection["name"])
            if previous_selection is not None
            else previous_choice
        )
        self.world_size = None
        self.loaded_source = None
        self.loaded_assets = None
        self.biome_choices = {}
        self.biome_choices_by_name = {}
        self.biome_combo.configure(values=[])
        self.biome_var.set("")
        try:
            summary = swe.regeneration_world_summary(source, assets)
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        width, height = summary["size"]
        self.world_size = (width, height)
        self.loaded_source = source.resolve()
        self.loaded_assets = assets.resolve()
        if preserve_coordinate_entries:
            self.x_start_var.set(previous_coordinates[0])
            self.x_end_var.set(previous_coordinates[1] or str(width - 1))
            self.y_start_var.set(previous_coordinates[2])
            self.y_end_var.set(previous_coordinates[3] or str(height - 1))
        else:
            self.x_end_var.set(str(width - 1))
            self.y_end_var.set(str(height - 1))
        self.biome_choices = {}
        for group in summary["biomes"]:
            indexes = list(group["indexes"])
            suffix = (
                self.tr(
                    "existing_biome",
                    indexes=", ".join(str(index) for index in indexes),
                )
                if indexes
                else self.tr("new_biome")
            )
            display = f"{group['name']}  [{suffix}]"
            self.biome_choices[display] = {
                "name": group["name"],
                "indexes": indexes,
            }
            self.biome_choices_by_name[str(group["name"])] = self.biome_choices[display]
        choices = list(self.biome_choices)
        self.biome_combo.configure(values=choices)
        preserved = self.biome_choices_by_name.get(previous_name)
        preserved_display = next(
            (
                display
                for display, value in self.biome_choices.items()
                if value is preserved
            ),
            None,
        )
        self.biome_var.set(preserved_display or (choices[0] if choices else ""))
        self.set_status_key(
            "world_loaded",
            width=width,
            height=height,
            compiled=summary["assets"]["compiledMatchCount"],
            asset_mode=summary["assets"]["mode"],
            asset_biomes=summary["assets"]["biomeCount"],
            max_x=width - 1,
        )

    def regenerate(self) -> None:
        source = Path(self.source_var.get().strip())
        if not source.is_file():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_source"), parent=self.root
            )
            return
        raw_assets = self.assets_var.get().strip()
        assets = Path(raw_assets)
        if not raw_assets or not assets.is_dir():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_assets"), parent=self.root
            )
            return
        if (
            self.world_size is None
            or self.loaded_source != source.resolve()
            or self.loaded_assets != assets.resolve()
        ):
            self.load_world(source, assets, preserve_coordinate_entries=True)
            if self.world_size is None:
                return
        raw_output = self.output_var.get().strip()
        if not raw_output:
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_output"), parent=self.root
            )
            return
        if not raw_output.lower().endswith(".world"):
            raw_output += ".world"
            self.output_var.set(raw_output)
        output = Path(raw_output)
        if source.resolve() == output.resolve():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("same_output"), parent=self.root
            )
            return
        try:
            x_start = int(self.x_start_var.get().strip())
            x_end = int(self.x_end_var.get().strip())
        except ValueError:
            x_start = x_end = -1
        max_x = self.world_size[0] - 1
        if not 0 <= x_start <= max_x or not 0 <= x_end <= max_x:
            messagebox.showerror(
                self.tr("invalid_path"),
                self.tr("invalid_x", max_x=max_x),
                parent=self.root,
            )
            return
        try:
            y_start = int(self.y_start_var.get().strip())
            y_end = int(self.y_end_var.get().strip())
        except ValueError:
            y_start = y_end = -1
        max_y = self.world_size[1] - 1
        if not 0 <= y_start <= y_end <= max_y:
            messagebox.showerror(
                self.tr("invalid_path"),
                self.tr("invalid_y", max_y=max_y),
                parent=self.root,
            )
            return
        choice = self.biome_var.get()
        selection = self.biome_choices.get(choice) or self.biome_choices_by_name.get(
            choice.strip()
        )
        if not selection:
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_biome"), parent=self.root
            )
            return
        indexes = list(selection["indexes"])
        target_biome_name = str(selection["name"])
        if not self.confirm_replace(output):
            return
        if not self.ask_yes_no(
            self.tr("confirm_title"),
            self.tr(
                "confirm",
                x_start=x_start,
                x_end=x_end,
                y_start=y_start,
                y_end=y_end,
                biome=choice,
            ),
        ):
            return

        def task():
            return swe.regenerate_world_biome_x_range(
                source,
                output,
                x_start,
                x_end,
                indexes,
                y_start,
                y_end,
                assets,
                target_biome_name,
            )

        def success(result):
            stats, _document = result
            self.last_output = output
            self.set_status_key(
                "done_status", output=output, sectors=stats["resetSectors"]
            )
            compile_warnings = stats.get("assetCompileWarnings", [])
            compile_notes = (
                self.tr("compile_notes", notes="; ".join(compile_warnings))
                if compile_warnings
                else ""
            )
            if stats.get("oceanCompanionBiome") and stats.get(
                "oceanFloorTransitionY"
            ) is not None:
                compile_notes += self.tr(
                    "ocean_notes",
                    floor=target_biome_name,
                    surface=stats["oceanCompanionBiome"],
                    transition=stats["oceanFloorTransitionY"],
                )
            messagebox.showinfo(
                self.tr("done_title"),
                self.tr(
                    "done",
                    output=output,
                    sectors=stats["resetSectors"],
                    tiles=stats["removedTileRecords"],
                    entities=stats["removedEntityRecords"],
                    unique_entries=stats["removedUniqueIndexEntries"],
                    target_indexes=", ".join(str(index) for index in stats["targetBiomeIndexes"]),
                    layers=stats["changedLayers"],
                    compile_notes=compile_notes,
                ),
                parent=self.root,
            )

        self.run_worker(task, success, "processing")


if __name__ == "__main__":
    RegenerateWindow().run()
