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
        "subtitle": "Scan assets and rebuild the selected area from the target biome's complete recipe. The original world is backed up first and replaced only after verification.",
        "source_label": "Source .world",
        "assets_label": "Starbound assets folder",
        "output_label": "New output .world path",
        "x_start_label": "X start (tile, inclusive)",
        "x_end_label": "X end (tile, inclusive)",
        "y_start_label": "Y start (tile, inclusive)",
        "y_end_label": "Y end (tile, inclusive)",
        "biome_label": "Target biome type",
        "ocean_floor_height": "Ocean-floor terrain height (optional)",
        "ocean_floor_hint": "For ocean/toxic/arctic/magma pairs. Blank uses the world's compiled surface/subsurface heights.",
        "select_source": "Select the Starbound world",
        "select_assets": "Select the Starbound assets folder",
        "save_world": "Save the regenerated Starbound world",
        "world_file": "Starbound world",
        "load_world": "Load world information",
        "scan_assets": "Scan assets and fill biome list",
        "start": "Create reset World",
        "keep_backup": "Keep the original-world backup (recommended)",
        "backup_keep_note": "The timestamped backup will remain permanently in the original folder.",
        "backup_discard_note": "A temporary backup is still used as the safe source, but is removed after a verified replacement.",
        "backup_not_retained": "Not retained (option was disabled)",
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
        "invalid_ocean_floor": "Ocean-floor terrain height must be a valid number or left blank.",
        "confirm_title": "Confirm full sector reset",
        "confirm": "The exact selected rectangle will be assigned to the target biome and regenerated. Its biome blocks, placeables, monsters, parallax, music and environment settings will change. Every intersecting 32×32 sector is fully reset, including player-built blocks, liquids, placed objects and stored entities.\n\nThe biome recipe keeps the exact entered coordinates; record deletion expands to sector boundaries. Cleared tiles outside the recipe rectangle regenerate from their original biome. A backup is created first, the edit is built from it, and the original path is replaced only after verification. {backup_note}\n\nBiome recipe X: {x_start} to {x_end}\nBiome recipe Y: {y_start} to {y_end}\nDeleted-sector X: {dx0} to {dx1}\nDeleted-sector Y: {dy0} to {dy1}\nTarget biome: {biome}\n\nContinue?",
        "done_title": "World region updated",
        "done": "Updated and verified in place:\n\n{output}\n\nOriginal-world backup: {backup}\n\nTarget biome indexes: {target_indexes}\nBiome recipe X: {ax0} to {ax1}\nBiome recipe Y: {ay0} to {ay1}\nDeleted-sector X: {dx0} to {dx1}\nDeleted-sector Y: {dy0} to {dy1}\nBoundary blend width: {blend}; direct neighbouring transitions: {transitions}\nChanged vertical layout bands: {layers}\nReset sectors: {sectors}\nRemoved tile records: {tiles}\nRemoved entity records: {entities}\nRemoved unique-index entries: {unique_entries}{compile_notes}\n\nMissing sectors regenerate as the target biome when approached.",
        "compile_notes": "\nAsset compiler notes: {notes}",
        "ocean_notes": "\nOcean pairing: lower {floor} / upper {surface}; transition Y = {transition}.",
        "done_status": "Updated {output}; reset {sectors} sectors; backup: {backup}.",
        "boundary_warning_title": "Narrow biome boundary detected",
        "boundary_warning": "The target edge leaves a neighbouring compiled region cell narrower than this world's regionBlending width ({blend} tiles). That narrow cell and the biome beyond it can blend simultaneously and create a gap.\n\n{details}",
        "boundary_left": "Left edge X={x}: {width} tiles of {biome} remain before the next region boundary. Consider X start = {suggested}.",
        "boundary_right": "Right edge X={x}: {width} tiles of {biome} remain before the next region boundary. Consider X end = {suggested}.",
        "boundary_fix_title": "Align biome boundary",
        "boundary_fix": "A narrow compiled region cell at the selected edge can cause a visible gap, especially between an ocean/oceanfloor area and land.\n\nCurrent X: {x_start} to {x_end}\nSuggested X: {suggested_start} to {suggested_end}\nregionBlending: {blend} tiles\n\nAutomatically use the suggested X boundary before regeneration?",
    },
    "zh": {
        "window_title": "Starbound Biome 区域重新生成工具",
        "page_title": "转换并重新生成一块矩形 biome 区域",
        "subtitle": "扫描 assets，并按目标 biome 的完整配方重新生成选区。程序先备份原 world，完整校验后才替换原路径。",
        "source_label": "来源 .world",
        "assets_label": "Starbound assets 文件夹",
        "output_label": "输出新 .world 路径",
        "x_start_label": "X 起点（格，包含）",
        "x_end_label": "X 终点（格，包含）",
        "y_start_label": "Y 起点（格，包含）",
        "y_end_label": "Y 终点（格，包含）",
        "biome_label": "Target biome type（目标 biome）",
        "ocean_floor_height": "海床地形基准高度（可选）",
        "ocean_floor_hint": "适用于 ocean/toxic/arctic/magma 海洋组；留空时使用世界中已编译的 Surface/Subsurface 高度。",
        "select_source": "选择 Starbound world",
        "select_assets": "选择 Starbound assets 文件夹",
        "save_world": "保存重新生成的 Starbound world",
        "world_file": "Starbound 世界",
        "load_world": "读取 world 信息",
        "scan_assets": "扫描 Assets 并填充 biome",
        "start": "生成重置后的 World",
        "keep_backup": "保留原 world 备份（推荐）",
        "backup_keep_note": "时间戳备份会永久保留在原文件夹中。",
        "backup_discard_note": "程序仍以临时备份作为安全来源，但校验并替换成功后会删除该备份。",
        "backup_not_retained": "未保留（已取消备份选项）",
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
        "invalid_ocean_floor": "海床地形基准高度必须是有效数字，或者留空。",
        "confirm_title": "确认完整重置 sector",
        "confirm": "输入的精确矩形会改成目标 biome 并重新生成，其 biome 物块、placeables、怪物、parallax、音乐和环境设置都会改变。所有相交的 32×32 sector 会完整重置，包括玩家建筑物块、液体、放置的 object 和已保存实体。\n\nbiome 配方保持输入的精确坐标，只有记录删除范围扩展到 sector 边界；配方矩形外但位于被清除 sector 内的格子按原 biome 重生。程序会先备份，以备份为来源生成修改版，并在校验后替换原路径。{backup_note}\n\nbiome 配方 X：{x_start} 到 {x_end}\nbiome 配方 Y：{y_start} 到 {y_end}\n删除 sector X：{dx0} 到 {dx1}\n删除 sector Y：{dy0} 到 {dy1}\n目标 biome：{biome}\n\n是否继续？",
        "done_title": "World 区域更新完成",
        "done": "已经原位更新并验证：\n\n{output}\n\n原 world 备份：{backup}\n\n目标 biome indexes：{target_indexes}\nbiome 配方 X：{ax0} 到 {ax1}\nbiome 配方 Y：{ay0} 到 {ay1}\n删除 sector X：{dx0} 到 {dx1}\n删除 sector Y：{dy0} 到 {dy1}\n边界模糊宽度：{blend}；直接相邻过渡数：{transitions}\n修改纵向布局层：{layers}\n重置 sector：{sectors}\n删除 tile 记录：{tiles}\n删除实体记录：{entities}\n删除唯一实体索引：{unique_entries}{compile_notes}\n\n玩家靠近缺失 sector 时，它们会按照目标 biome 重新生成。",
        "compile_notes": "\nAssets 编译说明：{notes}",
        "ocean_notes": "\n海洋分层：下层 {floor} / 上层 {surface}；分界 Y = {transition}。",
        "done_status": "已更新 {output}；重置 {sectors} 个 sector；备份：{backup}。",
        "boundary_warning_title": "检测到狭窄 biome 边界",
        "boundary_warning": "目标边缘留下了宽度小于本 world `regionBlending`（{blend} 格）的相邻 compiled region cell。这个狭 cell 和它外侧的 biome 可能同时参与混合，从而产生 gap。\n\n{details}",
        "boundary_left": "左边界 X={x}：距下一个 region 边界还剩 {width} 格 {biome}，建议考虑 X start = {suggested}。",
        "boundary_right": "右边界 X={x}：距下一个 region 边界还剩 {width} 格 {biome}，建议考虑 X end = {suggested}。",
        "boundary_fix_title": "贴齐 biome 边界",
        "boundary_fix": "所选边缘存在狭窄的 compiled region cell，可能形成可见 gap，尤其容易出现在 ocean/oceanfloor 与陆地的交界。\n\n当前 X：{x_start} 到 {x_end}\n建议 X：{suggested_start} 到 {suggested_end}\nregionBlending：{blend} 格\n\n是否在重新生成前自动使用建议的 X 边界？",
    },
    "de": {
        "window_title": "Starbound-Biomsektor-Regenerator",
        "page_title": "Ein rechteckiges Biomgebiet umwandeln und neu erzeugen",
        "subtitle": "Assets scannen und das Gebiet aus dem vollständigen Zielbiomrezept neu aufbauen. Die Originalwelt wird zuerst gesichert und erst nach Prüfung ersetzt.",
        "source_label": "Quell-.world",
        "assets_label": "Starbound-Assets-Ordner",
        "output_label": "Pfad der neuen Ausgabe-.world",
        "x_start_label": "X-Anfang (Kachel, inklusiv)",
        "x_end_label": "X-Ende (Kachel, inklusiv)",
        "y_start_label": "Y-Anfang (Kachel, inklusiv)",
        "y_end_label": "Y-Ende (Kachel, inklusiv)",
        "biome_label": "Zielbiomtyp",
        "ocean_floor_height": "Geländehöhe des Meeresbodens (optional)",
        "ocean_floor_hint": "Für Ozean-/Gift-/Arktis-/Magma-Paare; leer verwendet die kompilierten Oberflächen-/Untergrundhöhen der Welt.",
        "select_source": "Starbound-Welt auswählen",
        "select_assets": "Starbound-Assets-Ordner auswählen",
        "save_world": "Neu erzeugte Starbound-Welt speichern",
        "world_file": "Starbound-Welt",
        "load_world": "Weltinformationen laden",
        "scan_assets": "Assets scannen und Biomliste füllen",
        "start": "Zurückgesetzte Welt erstellen",
        "keep_backup": "Sicherung der Originalwelt behalten (empfohlen)",
        "backup_keep_note": "Die Zeitstempel-Sicherung bleibt dauerhaft im Originalordner erhalten.",
        "backup_discard_note": "Eine temporäre Sicherung wird weiterhin als sichere Quelle verwendet, nach erfolgreicher Prüfung jedoch entfernt.",
        "backup_not_retained": "Nicht behalten (Option deaktiviert)",
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
        "invalid_ocean_floor": "Die Geländehöhe des Meeresbodens muss eine gültige Zahl sein oder leer bleiben.",
        "confirm_title": "Vollständiges Zurücksetzen bestätigen",
        "confirm": "Das exakt gewählte Rechteck wird dem Zielbiom zugewiesen und neu erzeugt. Biomblöcke, Placeables, Monster, Parallaxe, Musik und Umgebungseinstellungen ändern sich. Jeder berührte 32×32-Sektor wird vollständig zurückgesetzt, einschließlich Spielerbauten, Flüssigkeiten, platzierter Objekte und gespeicherter Entitäten.\n\nDas Biomrezept behält die eingegebenen Koordinaten; nur die Datensatzlöschung wird auf Sektorgrenzen erweitert. Gelöschte Kacheln außerhalb des Rezeptrechtecks regenerieren aus ihrem ursprünglichen Biom. Zuerst wird eine Sicherung erstellt; daraus entsteht die Änderung, die den Originalpfad erst nach Prüfung ersetzt. {backup_note}\n\nBiomrezept-X: {x_start} bis {x_end}\nBiomrezept-Y: {y_start} bis {y_end}\nGelöschtes Sektor-X: {dx0} bis {dx1}\nGelöschtes Sektor-Y: {dy0} bis {dy1}\nZielbiom: {biome}\n\nFortfahren?",
        "done_title": "Weltgebiet aktualisiert",
        "done": "Direkt aktualisiert und geprüft:\n\n{output}\n\nSicherung der Originalwelt: {backup}\n\nZielbiomindizes: {target_indexes}\nBiomrezept-X: {ax0} bis {ax1}\nBiomrezept-Y: {ay0} bis {ay1}\nGelöschtes Sektor-X: {dx0} bis {dx1}\nGelöschtes Sektor-Y: {dy0} bis {dy1}\nGrenz-Mischbreite: {blend}; direkte Nachbarübergänge: {transitions}\nGeänderte vertikale Layoutbänder: {layers}\nZurückgesetzte Sektoren: {sectors}\nEntfernte Kacheldatensätze: {tiles}\nEntfernte Entitätsdatensätze: {entities}\nEntfernte eindeutige Indexeinträge: {unique_entries}{compile_notes}\n\nFehlende Sektoren werden beim Annähern als Zielbiom neu erzeugt.",
        "compile_notes": "\nHinweise des Asset-Compilers: {notes}",
        "ocean_notes": "\nOzeanschichtung: unten {floor} / oben {surface}; Übergang Y = {transition}.",
        "done_status": "{output} aktualisiert; {sectors} Sektoren zurückgesetzt; Sicherung: {backup}.",
        "boundary_warning_title": "Schmale Biomgrenze erkannt",
        "boundary_warning": "Am Zielrand bleibt eine benachbarte kompilierte Regionszelle, die schmaler als der `regionBlending`-Wert dieser Welt ({blend} Kacheln) ist. Diese schmale Zelle und das Biom dahinter können gleichzeitig gemischt werden und eine Lücke erzeugen.\n\n{details}",
        "boundary_left": "Linker Rand X={x}: Bis zur nächsten Regionsgrenze bleiben {width} Kacheln von {biome}. Erwägen Sie X-Anfang = {suggested}.",
        "boundary_right": "Rechter Rand X={x}: Bis zur nächsten Regionsgrenze bleiben {width} Kacheln von {biome}. Erwägen Sie X-Ende = {suggested}.",
        "boundary_fix_title": "Biomgrenze ausrichten",
        "boundary_fix": "Eine schmale kompilierte Regionszelle am gewählten Rand kann eine sichtbare Lücke erzeugen, besonders zwischen Ozean/Ozeanboden und Land.\n\nAktuelles X: {x_start} bis {x_end}\nEmpfohlenes X: {suggested_start} bis {suggested_end}\nregionBlending: {blend} Kacheln\n\nVor der Neuerzeugung automatisch die empfohlene X-Grenze verwenden?",
    },
}


class RegenerateWindow(BaseWindow):
    def __init__(self) -> None:
        super().__init__(TRANSLATIONS, "900x655")
        self.source_var = tk.StringVar()
        self.assets_var = tk.StringVar()
        self.keep_backup_var = tk.BooleanVar(value=True)
        self.x_start_var = tk.StringVar(value="0")
        self.x_end_var = tk.StringVar()
        self.y_start_var = tk.StringVar(value="0")
        self.y_end_var = tk.StringVar()
        self.biome_var = tk.StringVar()
        self.ocean_floor_height_var = tk.StringVar()
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
        self.keep_backup_check = ttk.Checkbutton(
            form, variable=self.keep_backup_var
        )
        self.keep_backup_check.grid(
            row=2, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=6
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

        ocean_frame = ttk.Frame(outer)
        ocean_frame.pack(fill="x", pady=(5, 2))
        self.ocean_floor_height_label = ttk.Label(ocean_frame)
        self.ocean_floor_height_label.pack(side="left")
        ttk.Entry(
            ocean_frame, textvariable=self.ocean_floor_height_var, width=16
        ).pack(side="left", padx=(10, 10))
        self.ocean_floor_hint_label = ttk.Label(
            ocean_frame, style="Hint.TLabel", wraplength=590
        )
        self.ocean_floor_hint_label.pack(side="left", fill="x", expand=True)

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
        self.source_browse.configure(text=self.tr("browse"))
        self.assets_browse.configure(text=self.tr("browse"))
        self.keep_backup_check.configure(text=self.tr("keep_backup"))
        self.scan_assets_button.configure(text=self.tr("scan_assets"))
        self.load_world_button.configure(text=self.tr("load_world"))
        self.x_start_label.configure(text=self.tr("x_start_label"))
        self.x_end_label.configure(text=self.tr("x_end_label"))
        self.y_start_label.configure(text=self.tr("y_start_label"))
        self.y_end_label.configure(text=self.tr("y_end_label"))
        self.biome_label.configure(text=self.tr("biome_label"))
        self.ocean_floor_height_label.configure(text=self.tr("ocean_floor_height"))
        self.ocean_floor_hint_label.configure(text=self.tr("ocean_floor_hint"))
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
        self.world_size = None
        self.loaded_source = None

    def browse_assets(self) -> None:
        path = self.choose_directory(self.assets_var, self.tr("select_assets"))
        if path:
            self.loaded_assets = None

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

    def show_boundary_warning(self, summary) -> None:
        narrow = [
            item
            for item in summary.get("boundaryTransitions", [])
            if item.get("narrowNeighborCell")
        ]
        unique = {}
        for item in narrow:
            key = (
                item["side"],
                item["x"],
                item["neighborBiome"],
                item["neighborCellWidthFromBoundary"],
                item["suggestedTargetBoundary"],
            )
            unique[key] = item
        if not unique:
            return
        details = []
        for item in unique.values():
            details.append(
                self.tr(
                    "boundary_left"
                    if item["side"] == "left"
                    else "boundary_right",
                    x=item["x"],
                    width=item["neighborCellWidthFromBoundary"],
                    biome=item["neighborBiome"],
                    suggested=item["suggestedTargetBoundary"],
                )
            )
        messagebox.showwarning(
            self.tr("boundary_warning_title"),
            self.tr(
                "boundary_warning",
                blend=summary.get("boundaryBlendWidth", 0),
                details="\n".join(details),
            ),
            parent=self.root,
        )

    def offer_boundary_alignment(
        self, summary, x_start: int, x_end: int
    ) -> tuple[int, int]:
        """Offer an exact rectangular fix when all Y bands agree on it."""

        narrow = [
            item
            for item in summary.get("boundaryTransitions", [])
            if item.get("narrowNeighborCell")
        ]
        left = {
            int(item["suggestedTargetBoundary"])
            for item in narrow
            if item.get("side") == "left"
        }
        right = {
            int(item["suggestedTargetBoundary"])
            for item in narrow
            if item.get("side") == "right"
        }
        suggested_start = next(iter(left)) if len(left) == 1 else x_start
        suggested_end = next(iter(right)) if len(right) == 1 else x_end
        if (suggested_start, suggested_end) == (x_start, x_end):
            self.show_boundary_warning(summary)
            return x_start, x_end
        if self.ask_yes_no(
            self.tr("boundary_fix_title"),
            self.tr(
                "boundary_fix",
                x_start=x_start,
                x_end=x_end,
                suggested_start=suggested_start,
                suggested_end=suggested_end,
                blend=summary.get("boundaryBlendWidth", 0),
            ),
        ):
            self.x_start_var.set(str(suggested_start))
            self.x_end_var.set(str(suggested_end))
            return suggested_start, suggested_end
        self.show_boundary_warning(summary)
        return x_start, x_end

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
        floor_text = self.ocean_floor_height_var.get().strip()
        try:
            ocean_floor_height = float(floor_text) if floor_text else None
        except ValueError:
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("invalid_ocean_floor"),
                parent=self.root,
            )
            return
        try:
            preview = swe.inspect_world_asset_biome_rectangle(
                source,
                assets,
                target_biome_name,
                x_start,
                x_end,
                y_start,
                y_end,
                ocean_floor_height,
            )
        except Exception as exc:
            messagebox.showerror(
                self.tr("operation_failed"), str(exc), parent=self.root
            )
            return
        adjusted_x_start, adjusted_x_end = self.offer_boundary_alignment(
            preview, x_start, x_end
        )
        if (adjusted_x_start, adjusted_x_end) != (x_start, x_end):
            x_start, x_end = adjusted_x_start, adjusted_x_end
            try:
                preview = swe.inspect_world_asset_biome_rectangle(
                    source,
                    assets,
                    target_biome_name,
                    x_start,
                    x_end,
                    y_start,
                    y_end,
                    ocean_floor_height,
                )
            except Exception as exc:
                messagebox.showerror(
                    self.tr("operation_failed"), str(exc), parent=self.root
                )
                return
            self.show_boundary_warning(preview)
        deleted_preview = preview["resetSectorRange"]
        keep_backup = self.keep_backup_var.get()
        if not self.ask_yes_no(
            self.tr("confirm_title"),
            self.tr(
                "confirm",
                x_start=x_start,
                x_end=x_end,
                y_start=y_start,
                y_end=y_end,
                dx0=deleted_preview["xStart"],
                dx1=deleted_preview["xEnd"],
                dy0=deleted_preview["yStart"],
                dy1=deleted_preview["yEnd"],
                biome=choice,
                backup_note=self.tr(
                    "backup_keep_note" if keep_backup else "backup_discard_note"
                ),
            ),
        ):
            return

        def task():
            return swe.rebuild_world_in_place_from_backup(
                source,
                lambda backup_source, staged_output: swe.regenerate_world_biome_x_range(
                    backup_source,
                    staged_output,
                    x_start,
                    x_end,
                    indexes,
                    y_start,
                    y_end,
                    assets,
                    target_biome_name,
                    ocean_floor_height,
                ),
                keep_backup=keep_backup,
            )

        def success(result):
            backup, (stats, _document) = result
            backup_display = (
                str(backup) if backup is not None else self.tr("backup_not_retained")
            )
            self.last_output = source
            applied = stats["appliedTargetRange"]
            deleted = stats["resetSectorRange"]
            self.set_status_key(
                "done_status",
                output=source,
                sectors=stats["resetSectors"],
                backup=backup_display,
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
                    output=source,
                    backup=backup_display,
                    sectors=stats["resetSectors"],
                    tiles=stats["removedTileRecords"],
                    entities=stats["removedEntityRecords"],
                    unique_entries=stats["removedUniqueIndexEntries"],
                    target_indexes=", ".join(str(index) for index in stats["targetBiomeIndexes"]),
                    ax0=applied["xStart"], ax1=applied["xEnd"],
                    ay0=applied["yStart"], ay1=applied["yEnd"],
                    dx0=deleted["xStart"], dx1=deleted["xEnd"],
                    dy0=deleted["yStart"], dy1=deleted["yEnd"],
                    blend=stats.get("boundaryBlendWidth", 0),
                    transitions=len(stats.get("boundaryTransitions", [])),
                    layers=stats["changedLayers"],
                    compile_notes=compile_notes,
                ),
                parent=self.root,
            )

        self.run_worker(task, success, "processing")


if __name__ == "__main__":
    RegenerateWindow().run()
