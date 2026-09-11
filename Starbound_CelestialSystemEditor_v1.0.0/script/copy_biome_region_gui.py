from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from gui_common import BaseWindow
import starbound_world_editor as swe


TRANSLATIONS = {
    "en": {
        "window_title": "Starbound Reference Biome Copier",
        "page_title": "Copy an in-world biome recipe to another region",
        "subtitle": "Choose one reference rectangle containing exactly one compiled biome. Its exact terrain, caves, ores, liquids, placeables, monsters and parallax recipe will be assigned to the target rectangle.",
        "subtitle_hue": "Read each saved reference tile's compiled-biome materialHueShift and color variant, then apply them only to matching material IDs in the target; terrain stays unchanged.",
        "source_label": "Source .world",
        "keep_backup": "Keep original-world backup (recommended)",
        "backup_keep_note": "The timestamped backup will be kept in the same folder.",
        "backup_discard_note": "A temporary backup will still be created for safety, then removed only after the rebuilt world passes verification and replaces the original.",
        "backup_not_retained": "Not retained (removed after successful verified replacement)",
        "select_source": "Select the Starbound world",
        "save_world": "Save the copied-biome Starbound world",
        "world_file": "Starbound world",
        "load_world": "Load world information",
        "analyze": "Analyze reference area",
        "hue_only": "Hue shift only",
        "start": "Update original World",
        "start_hue": "Update original World (hue only)",
        "open_folder": "Open folder",
        "reference_title": "Reference rectangle (must contain one biome)",
        "reference_title_hue": "Reference rectangle (saved tile colors)",
        "target_title": "Target rectangle (will be fully reset)",
        "target_title_hue": "Target rectangle (saved tiles; no reset)",
        "x_start": "X start (inclusive)",
        "x_end": "X end (inclusive)",
        "y_start": "Y start (inclusive)",
        "y_end": "Y end (inclusive)",
        "initial_status": "Select and load a World4 .world, then enter the reference and target rectangles.",
        "loading": "Reading world metadata…",
        "world_loaded": "Loaded: {width}×{height} tiles. Valid X: 0–{max_x}; valid Y: 0–{max_y}. X start greater than X end wraps across the planet seam.",
        "analyzing": "Checking every compiled region cell in the reference rectangle…",
        "analyzing_hue": "Reading saved reference tiles and collecting compiled-biome hue/color styles per material…",
        "reference_ready": "Reference block biome: {name} (index {index}); environment biome: {environment_name} (index {environment_index}); exact region-cell variants in selection: {variants}.{ocean}",
        "hue_ready": "Hue reference: {tiles} saved tiles; compiled-biome materialHueShift values: {biome_hues}; {foreground} foreground materials; {background} background materials; {foreground_mods} foreground material/mod pairs; {background_mods} background material/mod pairs.",
        "ocean_ready": " Ocean pair detected: {floor} below / {surface} above; transition Y = {transition}.",
        "processing": "Copying the exact compiled biome/region recipe, resetting target sectors, then rebuilding and verifying BTreeDB5…",
        "hue_processing": "Copying reference compiled-biome hue and color to matching saved target materials without resetting sectors…",
        "invalid_source": "Select an existing World4 .world file.",
        "invalid_output": "Select or enter an output .world path. The .world extension is added automatically if omitted.",
        "same_output": "The output cannot overwrite the source world.",
        "invalid_x": "{area} X start and end must be integers from 0 to {max_x}. A reversed X range is allowed and wraps around the planet.",
        "invalid_y": "{area} Y start and end must be increasing integers from 0 to {max_y}.",
        "reference_area": "Reference",
        "target_area": "Target",
        "confirm_title": "Confirm target sector reset",
        "confirm": "The target rectangle will inherit the exact compiled biome and region recipe from the reference rectangle: terrain height/shape, caves, ores, liquids, materials, placeables, monsters, parallax, music and environment settings. Every 32×32 sector intersecting the target is fully reset, including player-built blocks, liquids, objects and stored entities.\n\nThe biome recipe keeps the exact requested boundary. Only record deletion expands to complete sector boundaries; cleared tiles outside the requested rectangle regenerate from their original biome. Where the target directly meets another biome, Starbound blends both terrain selectors using this world's regionBlending value. The reference rectangle must contain one compiled biome.\n\nThe original world will first be backed up. The edited world is built from that backup, verified, and then atomically replaces the original path. Close the game and server first. {backup_note}\n\nReference X: {rx0} to {rx1}\nReference Y: {ry0} to {ry1}\nDetected biome: {biome} (index {index})\n\nBiome target X: {tx0} to {tx1}\nBiome target Y: {ty0} to {ty1}\nActual deleted-sector X: {ax0} to {ax1}\nActual deleted-sector Y: {ay0} to {ay1}\n\nContinue?",
        "done_title": "Original world updated",
        "done": "Updated and verified in place:\n\n{output}\n\nBackup: {backup}\n\nReference biome: {biome} (index {index})\nApplied biome indexes: {target_indexes}\nBiome recipe X: {ax0} to {ax1}\nBiome recipe Y: {ay0} to {ay1}\nDeleted-sector X: {dx0} to {dx1}\nDeleted-sector Y: {dy0} to {dy1}\nBoundary blend width: {blend}; direct neighbouring transitions: {transitions}\nChanged vertical layout bands: {layers}\nReset sectors: {sectors}\nRemoved tile records: {tiles}\nRemoved entity records: {entities}\nRemoved unique-index entries: {unique_entries}{ocean}",
        "ocean_done": "\nOcean pair: {floor} below / {surface} above; original transition Y = {transition}.",
        "done_status": "Updated {output}; copied {biome}, reset {sectors} target sectors; backup: {backup}.",
        "hue_confirm_title": "Confirm hue/color-only copy",
        "hue_confirm": "Only saved tiles in the exact target rectangle are edited. For each foreground or background material ID also found in the reference rectangle, the dominant materialHueShift of the reference tile's compiled biome and its color variant are copied. Material-mod hue is copied only when both material and mod IDs match.\n\nTerrain shape, material IDs, target biome metadata, vegetation, liquids, monsters, objects, entities and parallax are not changed. No sector is deleted. Unexplored/missing sectors cannot be recolored by this mode.\n\nThe original world will first be backed up. The edited world is built from that backup, verified, and then atomically replaces the original path. Close the game and server first. {backup_note}\n\nReference X: {rx0} to {rx1}\nReference Y: {ry0} to {ry1}\nCompiled-biome materialHueShift values: {biome_hues}\nReference foreground/background materials: {foreground}/{background}\n\nTarget X: {tx0} to {tx1}\nTarget Y: {ty0} to {ty1}\n\nContinue?",
        "hue_done_title": "Original world updated (hue/color only)",
        "hue_done": "Updated and verified in place:\n\n{output}\n\nBackup: {backup}\n\nReference tiles scanned: {reference_tiles}\nReference foreground/background materials: {reference_foreground}/{reference_background}\nTarget tiles scanned: {target_tiles}\nMatching foreground/background tiles: {foreground_matches}/{background_matches}\nChanged foreground/background tiles: {foreground_changed}/{background_changed}\nChanged foreground/background mod hues: {foreground_mods}/{background_mods}\nTouched tile records: {records}\nReset sectors: 0\nRemoved entity records: 0\n\nNo terrain, biome metadata, vegetation, liquids, monsters, objects, entities or parallax were changed.",
        "hue_done_status": "Updated {output}; recolored {tiles} matching saved tiles without resetting sectors; backup: {backup}.",
        "boundary_warning_title": "Narrow biome boundary detected",
        "boundary_warning": "The target edge leaves a neighbouring compiled region cell narrower than this world's regionBlending width ({blend} tiles). That narrow cell and the biome beyond it can blend simultaneously and create a gap.\n\n{details}",
        "boundary_left": "Left edge X={x}: {width} tiles of {biome} remain before the next region boundary. To absorb that narrow cell, consider X start = {suggested}.",
        "boundary_right": "Right edge X={x}: {width} tiles of {biome} remain before the next region boundary. To absorb that narrow cell, consider X end = {suggested}.",
    },
    "zh": {
        "window_title": "Starbound 参考 Biome 区域复制工具",
        "page_title": "把世界中现有的 biome 完整配方复制到另一区域",
        "subtitle": "选择一个只包含一种 compiled biome 的参考矩形，把它的具体地形、洞穴、矿脉、液体、放置物、怪物和 parallax 配方完整应用到目标矩形。",
        "subtitle_hue": "从参考范围已保存 tile 所属的 compiled biome 读取 materialHueShift 与颜色，只应用到目标中 material ID 相同的物块；地形保持不变。",
        "source_label": "来源 .world",
        "keep_backup": "保留原 world 备份文件（推荐）",
        "backup_keep_note": "带时间戳的备份会保留在原文件所在文件夹。",
        "backup_discard_note": "程序仍会先创建临时备份；只有新 world 验证成功并替换原文件后，才删除该备份。",
        "backup_not_retained": "未保留（验证并成功替换后已删除）",
        "select_source": "选择 Starbound world",
        "save_world": "保存复制 biome 后的 Starbound world",
        "world_file": "Starbound 世界",
        "load_world": "读取 world 信息",
        "analyze": "分析参考范围",
        "hue_only": "Hue shift only（仅复制色相/颜色）",
        "start": "更新原 World",
        "start_hue": "更新原 World（仅色相）",
        "open_folder": "打开所在文件夹",
        "reference_title": "参考矩形（必须只包含一种 biome）",
        "reference_title_hue": "参考矩形（读取已保存 tile 颜色）",
        "target_title": "目标矩形（会完整重置）",
        "target_title_hue": "目标矩形（只改已保存 tile，不重置）",
        "x_start": "X 起点（包含）",
        "x_end": "X 终点（包含）",
        "y_start": "Y 起点（包含）",
        "y_end": "Y 终点（包含）",
        "initial_status": "请选择并读取一个 World4 .world，然后填写参考矩形和目标矩形。",
        "loading": "正在读取 world 元数据……",
        "world_loaded": "已读取：{width}×{height} 格。有效 X：0–{max_x}；有效 Y：0–{max_y}。X 起点大于终点时会跨越星球接缝。",
        "analyzing": "正在检查参考矩形内的所有 compiled region cell……",
        "analyzing_hue": "正在读取参考区已保存 tile，并按 material 统计其 compiled biome hue/color……",
        "reference_ready": "参考 block biome：{name}（index {index}）；environment biome：{environment_name}（index {environment_index}）；选区内具体 region cell 配方变体：{variants}。{ocean}",
        "hue_ready": "Hue 参考：读取 {tiles} 个已保存 tile；compiled biome materialHueShift：{biome_hues}；前景 material {foreground} 种；背景 material {background} 种；前景 material/mod 组合 {foreground_mods} 种；背景组合 {background_mods} 种。",
        "ocean_ready": "检测到海洋配对：下层 {floor} / 上层 {surface}；原世界分界 Y = {transition}。",
        "processing": "正在复制具体 compiled biome/region 配方、重置目标 sector，然后重建并验证 BTreeDB5……",
        "hue_processing": "正在不重置 sector 的情况下，把参考 compiled biome hue/color 复制给目标区相同 material……",
        "invalid_source": "请选择存在的 World4 .world 文件。",
        "invalid_output": "请选择或输入输出 .world 的完整路径；未填写 .world 后缀时会自动添加。",
        "same_output": "输出文件不能覆盖来源 world。",
        "invalid_x": "{area} X 起点和终点必须是 0 到 {max_x} 之间的整数；允许起点大于终点来跨越星球接缝。",
        "invalid_y": "{area} Y 起点和终点必须是 0 到 {max_y} 之间递增的整数。",
        "reference_area": "参考范围",
        "target_area": "目标范围",
        "confirm_title": "确认完整重置目标 sector",
        "confirm": "目标矩形会继承参考矩形的具体 compiled biome 和 region 配方，包括地形高度与起伏、洞穴、矿脉、液体、物块、放置物、怪物、parallax、音乐和环境设置。所有与目标范围相交的 32×32 sector 都会完整重置，其中包括玩家建筑、液体、object 和已保存实体。\n\nbiome 配方会严格保持你输入的边界，只有记录删除范围会扩展到完整 sector；输入矩形外但位于被清除 sector 内的格子会按它们原本的 biome 重生。目标与其他 biome 直接相接时，Starbound 会按该世界的 regionBlending 对两侧 terrain selector 进行模糊过渡。参考矩形必须只包含一种 compiled biome。\n\n程序会先备份原 world，再以备份为源生成并验证新文件，最后原子替换原路径。操作前必须完全退出游戏和服务器。{backup_note}\n\n参考 X：{rx0} 到 {rx1}\n参考 Y：{ry0} 到 {ry1}\n检测到 biome：{biome}（index {index}）\n\nbiome 目标 X：{tx0} 到 {tx1}\nbiome 目标 Y：{ty0} 到 {ty1}\n实际删除 sector X：{ax0} 到 {ax1}\n实际删除 sector Y：{ay0} 到 {ay1}\n\n是否继续？",
        "done_title": "原 world 已更新",
        "done": "已在原路径完成更新并验证：\n\n{output}\n\n备份：{backup}\n\n参考 biome：{biome}（index {index}）\n实际应用 biome indexes：{target_indexes}\nbiome 配方 X：{ax0} 到 {ax1}\nbiome 配方 Y：{ay0} 到 {ay1}\n删除 sector X：{dx0} 到 {dx1}\n删除 sector Y：{dy0} 到 {dy1}\n边界模糊宽度：{blend}；直接相邻过渡数：{transitions}\n修改纵向布局层：{layers}\n重置 sector：{sectors}\n删除 tile 记录：{tiles}\n删除实体记录：{entities}\n删除唯一实体索引：{unique_entries}{ocean}",
        "ocean_done": "\n海洋配对：下层 {floor} / 上层 {surface}；沿用原世界分界 Y = {transition}。",
        "done_status": "已更新 {output}；复制 {biome} 并重置 {sectors} 个目标 sector；备份：{backup}。",
        "hue_confirm_title": "确认仅复制 hue/color",
        "hue_confirm": "只修改精确目标矩形内已保存的 tile。对于参考区也存在的相同前景或背景 material ID，复制参考 tile 所属 compiled biome 中最常见的 materialHueShift，以及该 tile 的 color variant。material mod 只在 material ID 与 mod ID 都相同时复制 hue。\n\n不修改地形、material ID、目标 biome metadata、植被、液体、怪物、object、实体或 parallax，也不删除任何 sector。未探索/未生成的 sector 无法用此模式改色。\n\n程序会先备份原 world，再以备份为源生成并验证新文件，最后原子替换原路径。操作前必须完全退出游戏和服务器。{backup_note}\n\n参考 X：{rx0} 到 {rx1}\n参考 Y：{ry0} 到 {ry1}\nCompiled biome materialHueShift：{biome_hues}\n参考前景/背景 material 种数：{foreground}/{background}\n\n目标 X：{tx0} 到 {tx1}\n目标 Y：{ty0} 到 {ty1}\n\n是否继续？",
        "hue_done_title": "原 world 已更新（仅 hue/color）",
        "hue_done": "已在原路径完成更新并验证：\n\n{output}\n\n备份：{backup}\n\n读取参考 tile：{reference_tiles}\n参考前景/背景 material：{reference_foreground}/{reference_background}\n扫描目标 tile：{target_tiles}\n匹配前景/背景 tile：{foreground_matches}/{background_matches}\n改色前景/背景 tile：{foreground_changed}/{background_changed}\n修改前景/背景 mod hue：{foreground_mods}/{background_mods}\n改动 tile 记录：{records}\n重置 sector：0\n删除实体记录：0\n\n没有修改地形、biome metadata、植被、液体、怪物、object、实体或 parallax。",
        "hue_done_status": "已更新 {output}；不重置 sector 地改色 {tiles} 个匹配 tile；备份：{backup}。",
        "boundary_warning_title": "检测到狭窄 biome 边界",
        "boundary_warning": "目标边缘留下了宽度小于本 world `regionBlending`（{blend} 格）的相邻 compiled region cell。这个狭 cell 和它外侧的 biome 可能同时参与混合，从而产生 gap。\n\n{details}",
        "boundary_left": "左边界 X={x}：距下一个 region 边界还剩 {width} 格 {biome}。如果要吸收这个狭 cell，建议考虑 X start = {suggested}。",
        "boundary_right": "右边界 X={x}：距下一个 region 边界还剩 {width} 格 {biome}。如果要吸收这个狭 cell，建议考虑 X end = {suggested}。",
    },
    "de": {
        "window_title": "Starbound-Referenzbiom-Kopierer",
        "page_title": "Ein vorhandenes Biomrezept in ein anderes Gebiet kopieren",
        "subtitle": "Wählen Sie ein Referenzrechteck mit genau einem kompilierten Biom. Sein exaktes Gelände-, Höhlen-, Erz-, Flüssigkeits-, Placeable-, Monster- und Parallaxenrezept wird dem Zielrechteck zugewiesen.",
        "subtitle_hue": "materialHueShift des kompilierten Bioms und Farbvarianten gespeicherter Referenzkacheln lesen und nur auf gleiche Material-IDs im Ziel anwenden; das Gelände bleibt unverändert.",
        "source_label": "Quell-.world",
        "keep_backup": "Sicherung der ursprünglichen Welt behalten (empfohlen)",
        "backup_keep_note": "Die Sicherung mit Zeitstempel bleibt im selben Ordner erhalten.",
        "backup_discard_note": "Zur Sicherheit wird trotzdem zuerst eine temporäre Sicherung erstellt; sie wird erst gelöscht, nachdem die neue Welt geprüft wurde und das Original erfolgreich ersetzt hat.",
        "backup_not_retained": "Nicht behalten (nach erfolgreichem, geprüftem Ersetzen gelöscht)",
        "select_source": "Starbound-Welt auswählen",
        "save_world": "Starbound-Welt mit kopiertem Biom speichern",
        "world_file": "Starbound-Welt",
        "load_world": "Weltinformationen laden",
        "analyze": "Referenzgebiet analysieren",
        "hue_only": "Nur Hue/Color kopieren",
        "start": "Ursprüngliche Welt aktualisieren",
        "start_hue": "Ursprüngliche Welt aktualisieren (nur Hue)",
        "open_folder": "Ordner öffnen",
        "reference_title": "Referenzrechteck (genau ein Biom)",
        "reference_title_hue": "Referenzrechteck (gespeicherte Kachelfarben)",
        "target_title": "Zielrechteck (wird vollständig zurückgesetzt)",
        "target_title_hue": "Zielrechteck (gespeicherte Kacheln; kein Reset)",
        "x_start": "X-Anfang (inklusive)",
        "x_end": "X-Ende (inklusive)",
        "y_start": "Y-Anfang (inklusive)",
        "y_end": "Y-Ende (inklusive)",
        "initial_status": "Wählen und laden Sie eine World4-.world; geben Sie danach Referenz- und Zielrechteck ein.",
        "loading": "Weltmetadaten werden gelesen…",
        "world_loaded": "Geladen: {width}×{height} Kacheln. Gültiges X: 0–{max_x}; gültiges Y: 0–{max_y}. X-Anfang größer als X-Ende läuft über die Planetengrenze.",
        "analyzing": "Alle kompilierten Regionszellen im Referenzrechteck werden geprüft…",
        "analyzing_hue": "Gespeicherte Referenzkacheln und kompilierte Biom-Hue-/Color-Stile je Material werden gelesen…",
        "reference_ready": "Referenz-Blockbiom: {name} (Index {index}); Umgebungsbiom: {environment_name} (Index {environment_index}); konkrete Regionszellenvarianten in der Auswahl: {variants}.{ocean}",
        "hue_ready": "Hue-Referenz: {tiles} gespeicherte Kacheln; materialHueShift-Werte der kompilierten Biome: {biome_hues}; {foreground} Vordergrundmaterialien; {background} Hintergrundmaterialien; {foreground_mods} Vordergrund-Material/Mod-Paare; {background_mods} Hintergrundpaare.",
        "ocean_ready": " Ozeanpaar erkannt: unten {floor} / oben {surface}; Übergang Y = {transition}.",
        "processing": "Das exakte kompilierte Biom-/Regionsrezept wird kopiert; Zielsektoren werden zurückgesetzt und BTreeDB5 wird neu aufgebaut und geprüft…",
        "hue_processing": "Referenz-Biom-Hue und Farbe werden ohne Sektorrücksetzung auf passende gespeicherte Zielmaterialien kopiert…",
        "invalid_source": "Wählen Sie eine vorhandene World4-.world-Datei.",
        "invalid_output": "Wählen Sie einen Ausgabepfad für die .world-Datei. Die Endung .world wird bei Bedarf ergänzt.",
        "same_output": "Die Ausgabe darf die Quellwelt nicht überschreiben.",
        "invalid_x": "X-Anfang und X-Ende für {area} müssen ganze Zahlen von 0 bis {max_x} sein. Ein umgekehrter X-Bereich darf über die Planetengrenze laufen.",
        "invalid_y": "Y-Anfang und Y-Ende für {area} müssen aufsteigende ganze Zahlen von 0 bis {max_y} sein.",
        "reference_area": "Referenz",
        "target_area": "Ziel",
        "confirm_title": "Zurücksetzen der Zielsektoren bestätigen",
        "confirm": "Das Zielrechteck übernimmt das exakte kompilierte Biom- und Regionsrezept des Referenzrechtecks: Geländehöhe/-form, Höhlen, Erze, Flüssigkeiten, Materialien, Placeables, Monster, Parallaxe, Musik und Umgebung. Jeder berührte 32×32-Zielsektor wird vollständig zurückgesetzt, einschließlich Spielerbauten, Flüssigkeiten, Objekte und gespeicherte Entitäten.\n\nDas Biomrezept behält die exakt eingegebene Grenze. Nur die Datensatzlöschung wird auf vollständige Sektorgrenzen erweitert; gelöschte Kacheln außerhalb des Zielrechtecks regenerieren aus ihrem ursprünglichen Biom. Trifft das Ziel direkt auf ein anderes Biom, mischt Starbound beide Geländeselektoren mit dem regionBlending-Wert dieser Welt. Das Referenzrechteck muss genau ein kompiliertes Biom enthalten.\n\nDie ursprüngliche Welt wird zuerst gesichert. Die bearbeitete Welt wird aus dieser Sicherung erstellt, geprüft und ersetzt danach atomar den ursprünglichen Pfad. Beenden Sie vorher Spiel und Server. {backup_note}\n\nReferenz-X: {rx0} bis {rx1}\nReferenz-Y: {ry0} bis {ry1}\nErkanntes Biom: {biome} (Index {index})\n\nBiomziel-X: {tx0} bis {tx1}\nBiomziel-Y: {ty0} bis {ty1}\nTatsächlich gelöschtes Sektor-X: {ax0} bis {ax1}\nTatsächlich gelöschtes Sektor-Y: {ay0} bis {ay1}\n\nFortfahren?",
        "done_title": "Ursprüngliche Welt aktualisiert",
        "done": "Am ursprünglichen Pfad aktualisiert und geprüft:\n\n{output}\n\nSicherung: {backup}\n\nReferenzbiom: {biome} (Index {index})\nAngewandte Biomindizes: {target_indexes}\nBiomrezept-X: {ax0} bis {ax1}\nBiomrezept-Y: {ay0} bis {ay1}\nGelöschtes Sektor-X: {dx0} bis {dx1}\nGelöschtes Sektor-Y: {dy0} bis {dy1}\nGrenz-Mischbreite: {blend}; direkte Nachbarübergänge: {transitions}\nGeänderte vertikale Layoutbänder: {layers}\nZurückgesetzte Sektoren: {sectors}\nEntfernte Kacheldatensätze: {tiles}\nEntfernte Entitätsdatensätze: {entities}\nEntfernte eindeutige Indexeinträge: {unique_entries}{ocean}",
        "ocean_done": "\nOzeanpaar: unten {floor} / oben {surface}; ursprünglicher Übergang Y = {transition}.",
        "done_status": "{output} aktualisiert; {biome} kopiert, {sectors} Zielsektoren zurückgesetzt; Sicherung: {backup}.",
        "hue_confirm_title": "Nur Hue/Color bestätigen",
        "hue_confirm": "Nur gespeicherte Kacheln im exakten Zielrechteck werden geändert. Für jede Vorder- oder Hintergrund-Material-ID, die auch im Referenzrechteck vorkommt, werden der häufigste materialHueShift des kompilierten Referenzbioms und die Farbvariante der Kachel kopiert. Material-Mod-Hue wird nur kopiert, wenn Material- und Mod-ID übereinstimmen.\n\nGeländeform, Material-IDs, Ziel-Biommetadaten, Vegetation, Flüssigkeiten, Monster, Objekte, Entitäten und Parallaxe bleiben unverändert. Kein Sektor wird gelöscht. Unerforschte/fehlende Sektoren können in diesem Modus nicht umgefärbt werden.\n\nDie ursprüngliche Welt wird zuerst gesichert. Die bearbeitete Welt wird aus dieser Sicherung erstellt, geprüft und ersetzt danach atomar den ursprünglichen Pfad. Beenden Sie vorher Spiel und Server. {backup_note}\n\nReferenz-X: {rx0} bis {rx1}\nReferenz-Y: {ry0} bis {ry1}\nmaterialHueShift-Werte der kompilierten Biome: {biome_hues}\nReferenz-Vorder-/Hintergrundmaterialien: {foreground}/{background}\n\nZiel-X: {tx0} bis {tx1}\nZiel-Y: {ty0} bis {ty1}\n\nFortfahren?",
        "hue_done_title": "Ursprüngliche Welt aktualisiert (nur Hue/Color)",
        "hue_done": "Am ursprünglichen Pfad aktualisiert und geprüft:\n\n{output}\n\nSicherung: {backup}\n\nGescannte Referenzkacheln: {reference_tiles}\nReferenz-Vorder-/Hintergrundmaterialien: {reference_foreground}/{reference_background}\nGescannte Zielkacheln: {target_tiles}\nPassende Vorder-/Hintergrundkacheln: {foreground_matches}/{background_matches}\nGeänderte Vorder-/Hintergrundkacheln: {foreground_changed}/{background_changed}\nGeänderte Vorder-/Hintergrund-Mod-Hues: {foreground_mods}/{background_mods}\nGeänderte Kacheldatensätze: {records}\nZurückgesetzte Sektoren: 0\nEntfernte Entitätsdatensätze: 0\n\nGelände, Biommetadaten, Vegetation, Flüssigkeiten, Monster, Objekte, Entitäten und Parallaxe blieben unverändert.",
        "hue_done_status": "{output} aktualisiert; {tiles} passende gespeicherte Kacheln ohne Sektorrücksetzung umgefärbt; Sicherung: {backup}.",
        "boundary_warning_title": "Schmale Biomgrenze erkannt",
        "boundary_warning": "Am Zielrand bleibt eine benachbarte kompilierte Regionszelle, die schmaler als der `regionBlending`-Wert dieser Welt ({blend} Kacheln) ist. Diese schmale Zelle und das Biom dahinter können gleichzeitig gemischt werden und eine Lücke erzeugen.\n\n{details}",
        "boundary_left": "Linker Rand X={x}: Bis zur nächsten Regionsgrenze bleiben {width} Kacheln von {biome}. Um diese schmale Zelle aufzunehmen, erwägen Sie X-Anfang = {suggested}.",
        "boundary_right": "Rechter Rand X={x}: Bis zur nächsten Regionsgrenze bleiben {width} Kacheln von {biome}. Um diese schmale Zelle aufzunehmen, erwägen Sie X-Ende = {suggested}.",
    },
}


class CopyBiomeWindow(BaseWindow):
    def __init__(self) -> None:
        super().__init__(TRANSLATIONS, "960x700")
        self.source_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.hue_only_var = tk.BooleanVar(value=False)
        self.keep_backup_var = tk.BooleanVar(value=True)
        self.world_size: tuple[int, int] | None = None
        self.loaded_source: Path | None = None

        self.reference_vars = [tk.StringVar(value="0") for _ in range(4)]
        self.target_vars = [tk.StringVar(value="0") for _ in range(4)]

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        self.title_label = ttk.Label(header, style="Title.TLabel")
        self.title_label.pack(side="left", anchor="nw")
        self.add_language_selector(header).pack(side="right", anchor="ne")
        self.subtitle_label = ttk.Label(outer, style="Hint.TLabel", wraplength=900)
        self.subtitle_label.pack(anchor="w", pady=(4, 14))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.source_label, _, self.source_browse = self.add_path_row(
            form, 0, "", self.source_var, self.browse_source
        )
        self.keep_backup_check = ttk.Checkbutton(
            form,
            variable=self.keep_backup_var,
        )
        self.keep_backup_check.grid(row=1, column=1, sticky="w", pady=(5, 1))

        info_buttons = ttk.Frame(outer)
        info_buttons.pack(fill="x", pady=(4, 8))
        self.load_button = ttk.Button(info_buttons, command=self.load_selected_world)
        self.load_button.pack(side="left")
        self.analyze_button = ttk.Button(info_buttons, command=self.analyze_reference)
        self.analyze_button.pack(side="left", padx=8)
        self.hue_only_check = ttk.Checkbutton(
            info_buttons,
            variable=self.hue_only_var,
            command=self.mode_changed,
        )
        self.hue_only_check.pack(side="left", padx=(8, 0))

        ranges = ttk.Frame(outer)
        ranges.pack(fill="x", pady=(5, 8))
        ranges.columnconfigure(0, weight=1)
        ranges.columnconfigure(1, weight=1)
        self.reference_frame, self.reference_labels = self.make_range_frame(
            ranges, self.reference_vars
        )
        self.reference_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.target_frame, self.target_labels = self.make_range_frame(
            ranges, self.target_vars
        )
        self.target_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.warning_label = ttk.Label(outer, style="Hint.TLabel", wraplength=900)
        self.warning_label.pack(anchor="w", pady=(5, 10))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(3, 8))
        self.action_button = ttk.Button(
            buttons, style="Accent.TButton", command=self.create_world
        )
        self.action_button.pack(side="left")
        self.open_folder_button = ttk.Button(buttons, command=self.open_output_folder)
        self.open_folder_button.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 8))
        ttk.Label(outer, textvariable=self.status_var, wraplength=900).pack(anchor="w")
        self.set_status_key("initial_status")
        self.apply_language()

    def make_range_frame(self, parent, variables):
        frame = ttk.LabelFrame(parent, padding=10)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        labels = []
        for row, offset in enumerate((0, 2)):
            first = ttk.Label(frame)
            first.grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=variables[offset], width=10).grid(
                row=row, column=1, sticky="ew", padx=(6, 12), pady=5
            )
            second = ttk.Label(frame)
            second.grid(row=row, column=2, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=variables[offset + 1], width=10).grid(
                row=row, column=3, sticky="ew", padx=(6, 0), pady=5
            )
            labels.extend((first, second))
        return frame, labels

    def apply_language(self) -> None:
        super().apply_language()
        self.title_label.configure(text=self.tr("page_title"))
        self.subtitle_label.configure(text=self.tr("subtitle"))
        self.source_label.configure(text=self.tr("source_label"))
        self.source_browse.configure(text=self.tr("browse"))
        self.keep_backup_check.configure(text=self.tr("keep_backup"))
        self.load_button.configure(text=self.tr("load_world"))
        self.analyze_button.configure(text=self.tr("analyze"))
        self.hue_only_check.configure(text=self.tr("hue_only"))
        self.reference_frame.configure(text=self.tr("reference_title"))
        self.target_frame.configure(text=self.tr("target_title"))
        for labels in (self.reference_labels, self.target_labels):
            labels[0].configure(text=self.tr("x_start"))
            labels[1].configure(text=self.tr("x_end"))
            labels[2].configure(text=self.tr("y_start"))
            labels[3].configure(text=self.tr("y_end"))
        self.mode_changed()
        self.open_folder_button.configure(text=self.tr("open_folder"))

    def mode_changed(self) -> None:
        hue_only = self.hue_only_var.get()
        key = "hue_confirm" if hue_only else "confirm"
        self.subtitle_label.configure(
            text=self.tr("subtitle_hue" if hue_only else "subtitle")
        )
        self.reference_frame.configure(
            text=self.tr("reference_title_hue" if hue_only else "reference_title")
        )
        self.target_frame.configure(
            text=self.tr("target_title_hue" if hue_only else "target_title")
        )
        self.action_button.configure(text=self.tr("start_hue" if hue_only else "start"))
        self.warning_label.configure(text=self.tr(key).split("\n\n")[0])

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

    def load_selected_world(self) -> None:
        source = Path(self.source_var.get().strip())
        if not source.is_file():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_source"), parent=self.root
            )
            return
        try:
            document = swe.load_metadata_only(source)
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        width, height = (int(document["size"][0]), int(document["size"][1]))
        self.world_size = (width, height)
        self.loaded_source = source.resolve()
        self.set_status_key(
            "world_loaded",
            width=width,
            height=height,
            max_x=width - 1,
            max_y=height - 1,
        )

    def ensure_loaded(self, source: Path) -> bool:
        if self.world_size is None or self.loaded_source != source.resolve():
            self.load_selected_world()
        return self.world_size is not None and self.loaded_source == source.resolve()

    def parse_range(self, variables, area_key):
        assert self.world_size is not None
        try:
            x_start, x_end, y_start, y_end = (
                int(variable.get().strip()) for variable in variables
            )
        except ValueError:
            x_start = x_end = y_start = y_end = -1
        max_x, max_y = self.world_size[0] - 1, self.world_size[1] - 1
        area = self.tr(area_key)
        if not 0 <= x_start <= max_x or not 0 <= x_end <= max_x:
            messagebox.showerror(
                self.tr("invalid_path"),
                self.tr("invalid_x", area=area, max_x=max_x),
                parent=self.root,
            )
            return None
        if not 0 <= y_start <= y_end <= max_y:
            messagebox.showerror(
                self.tr("invalid_path"),
                self.tr("invalid_y", area=area, max_y=max_y),
                parent=self.root,
            )
            return None
        return x_start, x_end, y_start, y_end

    def reference_summary(self, source: Path):
        reference = self.parse_range(self.reference_vars, "reference_area")
        if reference is None:
            return None
        if self.hue_only_var.get():
            return swe.inspect_world_reference_hues(source, *reference)
        return swe.inspect_world_reference_biome(source, *reference)

    def ocean_text(self, summary, key):
        transition = summary.get("oceanFloorTransitionY")
        if transition is None:
            return ""
        return self.tr(
            key,
            floor=summary["oceanFloorBiome"],
            surface=summary["oceanSurfaceBiome"],
            transition=transition,
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

    def analyze_reference(self) -> None:
        source = Path(self.source_var.get().strip())
        if not source.is_file():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_source"), parent=self.root
            )
            return
        if not self.ensure_loaded(source):
            return
        self.set_status_key(
            "analyzing_hue" if self.hue_only_var.get() else "analyzing"
        )
        try:
            summary = self.reference_summary(source)
            if summary is None:
                return
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        if self.hue_only_var.get():
            status = self.tr(
                "hue_ready",
                tiles=summary["referenceTilesScanned"],
                biome_hues=", ".join(
                    str(value)
                    for value in summary["referenceBiomeMaterialHues"]
                ) or "—",
                foreground=summary["referenceForegroundMaterials"],
                background=summary["referenceBackgroundMaterials"],
                foreground_mods=summary["referenceForegroundModPairs"],
                background_mods=summary["referenceBackgroundModPairs"],
            )
        else:
            status = self.tr(
                "reference_ready",
                name=summary["referenceBiomeName"],
                index=summary["referenceBiomeIndex"],
                environment_name=summary["referenceEnvironmentBiomeName"],
                environment_index=summary["referenceEnvironmentBiomeIndex"],
                variants=summary["referenceCellVariants"],
                ocean=self.ocean_text(summary, "ocean_ready"),
            )
        self.set_status(status)
        messagebox.showinfo(self.tr("analyze"), status, parent=self.root)

    def create_world(self) -> None:
        source = Path(self.source_var.get().strip())
        if not source.is_file():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_source"), parent=self.root
            )
            return
        if not self.ensure_loaded(source):
            return
        reference = self.parse_range(self.reference_vars, "reference_area")
        target = self.parse_range(self.target_vars, "target_area")
        if reference is None or target is None:
            return
        hue_only = self.hue_only_var.get()
        try:
            if hue_only:
                summary = swe.inspect_world_reference_hues(source, *reference)
                aligned = None
            else:
                summary = swe.inspect_world_reference_copy(
                    source, *target, *reference
                )
                aligned = swe.sector_aligned_rectangle(
                    int(summary["size"][0]),
                    int(summary["size"][1]),
                    *target,
                )
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        keep_backup = self.keep_backup_var.get()
        backup_note = self.tr(
            "backup_keep_note" if keep_backup else "backup_discard_note"
        )
        if hue_only:
            if not self.ask_yes_no(
                self.tr("hue_confirm_title"),
                self.tr(
                    "hue_confirm",
                    rx0=reference[0], rx1=reference[1],
                    ry0=reference[2], ry1=reference[3],
                    tx0=target[0], tx1=target[1],
                    ty0=target[2], ty1=target[3],
                    biome_hues=", ".join(
                        str(value)
                        for value in summary["referenceBiomeMaterialHues"]
                    ) or "—",
                    foreground=summary["referenceForegroundMaterials"],
                    background=summary["referenceBackgroundMaterials"],
                    backup_note=backup_note,
                ),
            ):
                return
        else:
            assert aligned is not None
            self.show_boundary_warning(summary)
            if not self.ask_yes_no(
                self.tr("confirm_title"),
                self.tr(
                    "confirm",
                    rx0=reference[0], rx1=reference[1],
                    ry0=reference[2], ry1=reference[3],
                    tx0=target[0], tx1=target[1],
                    ty0=target[2], ty1=target[3],
                    ax0=aligned["xStart"], ax1=aligned["xEnd"],
                    ay0=aligned["yStart"], ay1=aligned["yEnd"],
                    biome=summary["referenceBiomeName"],
                    index=summary["referenceBiomeIndex"],
                    backup_note=backup_note,
                ),
            ):
                return

        def task():
            def build(backup_source: Path, staged_output: Path):
                if hue_only:
                    return swe.copy_world_reference_hues(
                        backup_source, staged_output, *target, *reference
                    )
                return swe.regenerate_world_from_reference_rectangle(
                    backup_source, staged_output, *target, *reference
                )

            return swe.rebuild_world_in_place_from_backup(
                source,
                build,
                keep_backup=keep_backup,
            )

        def success(result):
            backup, (stats, _document) = result
            backup_display = (
                str(backup) if backup is not None else self.tr("backup_not_retained")
            )
            self.last_output = source
            if hue_only:
                changed = (
                    stats["foregroundTilesChanged"]
                    + stats["backgroundTilesChanged"]
                )
                self.set_status_key(
                    "hue_done_status",
                    output=source,
                    tiles=changed,
                    backup=backup_display,
                )
                messagebox.showinfo(
                    self.tr("hue_done_title"),
                    self.tr(
                        "hue_done",
                        output=source,
                        backup=backup_display,
                        reference_tiles=stats["referenceTilesScanned"],
                        reference_foreground=stats[
                            "referenceForegroundMaterials"
                        ],
                        reference_background=stats[
                            "referenceBackgroundMaterials"
                        ],
                        target_tiles=stats["targetTilesScanned"],
                        foreground_matches=stats["foregroundMatches"],
                        background_matches=stats["backgroundMatches"],
                        foreground_changed=stats["foregroundTilesChanged"],
                        background_changed=stats["backgroundTilesChanged"],
                        foreground_mods=stats["foregroundModHueChanged"],
                        background_mods=stats["backgroundModHueChanged"],
                        records=stats["touchedTileRecords"],
                    ),
                    parent=self.root,
                )
                return
            ocean = self.ocean_text(stats, "ocean_done")
            applied = stats["appliedTargetRange"]
            deleted = stats["resetSectorRange"]
            self.set_status_key(
                "done_status",
                output=source,
                biome=stats["referenceBiomeName"],
                sectors=stats["resetSectors"],
                backup=backup_display,
            )
            messagebox.showinfo(
                self.tr("done_title"),
                self.tr(
                    "done",
                    output=source,
                    backup=backup_display,
                    biome=stats["referenceBiomeName"],
                    index=stats["referenceBiomeIndex"],
                    target_indexes=", ".join(
                        str(index) for index in stats["targetBiomeIndexes"]
                    ),
                    ax0=applied["xStart"], ax1=applied["xEnd"],
                    ay0=applied["yStart"], ay1=applied["yEnd"],
                    dx0=deleted["xStart"], dx1=deleted["xEnd"],
                    dy0=deleted["yStart"], dy1=deleted["yEnd"],
                    blend=stats["boundaryBlendWidth"],
                    transitions=len(stats["boundaryTransitions"]),
                    layers=stats["changedLayers"],
                    sectors=stats["resetSectors"],
                    tiles=stats["removedTileRecords"],
                    entities=stats["removedEntityRecords"],
                    unique_entries=stats["removedUniqueIndexEntries"],
                    ocean=ocean,
                ),
                parent=self.root,
            )

        self.run_worker(
            task,
            success,
            "hue_processing" if hue_only else "processing",
        )


if __name__ == "__main__":
    CopyBiomeWindow().run()
