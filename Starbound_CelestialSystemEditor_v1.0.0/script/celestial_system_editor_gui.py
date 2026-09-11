from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import traceback
from types import SimpleNamespace
from typing import Any

from gui_common import BaseWindow
from dungeon_selector import DungeonListSelector
import dungeon_choices
from celestial_resource_validation import ResourcePreflight
import ocean_flora_completion
import region_biome_material_conversion
import region_tile_replacement
import region_spawn_edit
import region_spawn_replacement
import starbound_world_editor as swe
import world_edit_guard
import world_regeneration
from world_parameter_validation import native_float32, native_station_orbit


TOOL_05_LOG_FILENAME = "starbound_world_editor_05.log"
RESET_BODY_FIELDS = frozenset({
    "world_type", "world_size", "world_width", "world_height",
    "terrestrial_type", "primary_biome", "body_seed",
})
RESET_WORLD_PARAMETER_FIELDS = frozenset({"world_size_edit", "world_width", "world_height"})
MPI_VISIBLE_LAYERS = frozenset({"space", "atmosphere", "surface", "subsurface"})
# Keep the legacy filename and identifiers so existing log readers and saved
# diagnostic links still work after the public launcher was renumbered to 03.


def format_global_directives(value: Any) -> str:
    """Display a native directive list without dropping multi-entry values."""
    if value is None:
        return ""
    if isinstance(value, str):
        # Repair files previously written by the legacy scalar GUI on Apply.
        return value
    return json.dumps(value, ensure_ascii=False)


def parse_global_directives(text: str) -> list[str] | None:
    """Native VisitableWorldParameters expects optional List<Directives>."""
    text = text.strip()
    if not text:
        return None
    if text.startswith(("[", "{", '"')) or text in ("null", "true", "false"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Global directives must be one directive or a JSON string array") from exc
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("Global directives JSON must be an array of strings")
        return value
    return [text]


def parse_field_json(text: str, label: str) -> Any:
    """Retain the JSON diagnostic while identifying the editable field."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: {exc}") from exc


def build_scrollable_ocean_hint(parent: tk.Widget, text: str) -> ttk.Frame:
    """Bound long translated help without squeezing the JSON editor rows."""

    frame = ttk.Frame(parent)
    frame.columnconfigure(0, weight=1)
    content = tk.Text(frame, height=5, wrap="word", font="TkDefaultFont",
                      borderwidth=0, highlightthickness=0)
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=content.yview)
    content.configure(yscrollcommand=scrollbar.set)
    content.grid(row=0, column=0, sticky="ew")
    scrollbar.grid(row=0, column=1, sticky="ns")
    content.insert("1.0", text)
    content.configure(state="disabled")
    return frame


def build_reset_parameter_label(parent: tk.Widget, *, wraplength: int = 0, **grid: Any) -> ttk.Label:
    """Use the body page's red caption and raised star without changing inputs."""
    caption = ttk.Frame(parent)
    caption.grid(**grid)
    label = ttk.Label(caption, foreground="#c62828", wraplength=wraplength, justify="left")
    label.pack(side="left")
    ttk.Label(caption, text="*", foreground="#c62828", font=("Segoe UI", 9)).pack(
        side="left", anchor="n", padx=(2, 0))
    return label


def build_reset_legend(parent: tk.Widget) -> ttk.Label:
    """Keep the warning below the scroll viewport and above the global Save."""
    label = ttk.Label(parent, foreground="#c62828", wraplength=1080)
    label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
    parent.bind("<Configure>", lambda event: label.configure(wraplength=max(200, event.width - 12)), add="+")
    return label


def build_vertical_scroll_area(parent: tk.Widget) -> tuple[ttk.Frame, tk.Canvas, ttk.Scrollbar]:
    """Keep a page's natural height inside a resizeable, scrollable viewport."""

    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    canvas = tk.Canvas(parent, borderwidth=0, highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")
    content = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=content, anchor="nw")
    content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=max(1, event.width)))
    return content, canvas, scrollbar


def bind_vertical_page_wheel(content: tk.Widget, canvas: tk.Canvas) -> None:
    """Scope page scrolling to this page; retain native list/text scrolling."""

    def wheel(event: Any) -> str:
        bounds = canvas.bbox("all")
        if bounds is None or bounds[3] - bounds[1] <= canvas.winfo_height():
            return "break"
        delta = getattr(event, "delta", 0)
        if delta:
            units = -int(delta / 120) or (-1 if delta > 0 else 1)
        else:
            units = -1 if getattr(event, "num", 0) == 4 else 1
        canvas.yview_scroll(units, "units")
        return "break"

    widgets = [canvas, content]
    while widgets:
        widget = widgets.pop()
        if widget is not canvas:
            widgets.extend(widget.winfo_children())
        if widget.winfo_class() in ("Treeview", "Listbox", "Text", "Scrollbar", "TScrollbar"):
            continue
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(event, wheel, add="+")


def normalize_ocean_floor_controls(
    coastal_slope: bool, sample_width: Any, ecology_height: Any,
) -> dict[str, Any]:
    """Use the same backend validation for both GUI ocean-floor editors."""

    if ecology_height is None or (isinstance(ecology_height, str) and not ecology_height.strip()):
        ecology_height = swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT
    return swe.normalize_ocean_floor_pair_settings([{
        "surfaceBiome": "ocean",
        "coastalSlopeEnabled": coastal_slope,
        "coastalSlopeStep": sample_width,
        "floorEcologyHeight": ecology_height,
    }])[0]


def tool05_build_identity() -> str:
    # Slim releases keep build identity inside the runtime directory; older
    # releases remain readable without installing or rewriting their manifest.
    try:
        from release_identity import VERSION, BUILD_FAMILY
        if (isinstance(VERSION, str) and re.fullmatch(r"[0-9_]{8,32}", VERSION)
                and isinstance(BUILD_FAMILY, str) and re.fullmatch(r"[a-z0-9-]{1,48}", BUILD_FAMILY)):
            return BUILD_FAMILY + "-" + VERSION
    except ImportError:
        pass
    manifest = Path(__file__).resolve().parent.parent / "release_manifest.json"
    try:
        identity = json.loads(manifest.read_text(encoding="utf-8"))
        version = identity.get("version")
        family = identity.get("buildFamily", "native-ecology-test")
        if (isinstance(version, str) and re.fullmatch(r"[0-9_]{8,32}", version)
                and isinstance(family, str) and re.fullmatch(r"[a-z0-9-]{1,48}", family)):
            return family + "-" + version
    except (OSError, ValueError, AttributeError):
        pass
    return "single-region-development"


TOOL_05_BUILD_ID = tool05_build_identity()


def capture_save_inputs(paths: list[Path]) -> dict[Path, tuple[int, int, str] | None]:
    """Detect concurrent saves while private native generation is running."""
    snapshot = {}
    for path in paths:
        path = path.resolve()
        if path in snapshot:
            continue
        if not path.exists():
            snapshot[path] = None
            continue
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("Save input changed while it was being read: " + str(path))
        snapshot[path] = (after.st_size, after.st_mtime_ns, digest.hexdigest())
    return snapshot


def validate_save_inputs(snapshot: dict[Path, tuple[int, int, str] | None]) -> None:
    current = capture_save_inputs(list(snapshot))
    changed = [str(path) for path in snapshot if current[path] != snapshot[path]]
    if changed:
        raise RuntimeError(
            "The game or another editor changed these files during generation. "
            "No generated replacement was installed. Close the game, reload "
            "the system, and save again:\n" + "\n".join(changed)
        )


def lock_editor_controls(root: Any) -> list[tuple[Any, bool, Any]]:
    """Freeze editing while a worker runs; keep scrolling and progress usable."""
    saved = []
    pending = list(root.winfo_children())
    interactive = {
        "TEntry", "TCombobox", "TSpinbox", "TButton", "TCheckbutton",
        "TRadiobutton", "Treeview", "TNotebook", "Entry", "Spinbox",
        "Button", "Checkbutton", "Radiobutton", "Text", "Listbox",
    }
    while pending:
        widget = pending.pop()
        pending.extend(widget.winfo_children())
        if widget.winfo_class() not in interactive:
            continue
        if callable(getattr(widget, "state", None)):
            saved.append((widget, True, "disabled" in widget.state()))
            widget.state(["disabled"])
        else:
            saved.append((widget, False, widget.cget("state")))
            widget.configure(state="disabled")
    return saved


def unlock_editor_controls(saved: list[tuple[Any, bool, Any]]) -> None:
    for widget, themed, previous in saved:
        try:
            if themed:
                widget.state(["disabled" if previous else "!disabled"])
            else:
                widget.configure(state=previous)
        except tk.TclError:
            pass  # A temporary dialog may have closed while the worker ran.


TRANSLATIONS = {
    "en": {
        "window_title": "03 — Starbound Celestial System Editor",
        "page_title": "Edit a saved celestial system",
        "subtitle": "Select a storage/universe folder, locate a generated system by name or coordinates, then edit its star, planets, moons and ordered biome layers.",
        "chunks_label": "Universe folder",
        "select_chunks": "Select storage/universe folder",
        "chunks_file": "Starbound universe folder",
        "assets_label": "Assets (required for new biomes and celestial bodies)",
        "select_assets": "Select assets folder or packed.pak",
        "load_database": "Load generated systems",
        "search_label": "Filter by name / X:Y:Z",
        "system_label": "Saved system",
        "select_system": "Select a system after loading universe.chunks.",
        "system_tab": "System",
        "bodies_tab": "Planets and moons",
        "biomes_tab": "Ordered biomes",
        "system_objects_tab": "System objects",
        "regions_tab": "Generated biome regions",
        "world_parameters_tab": "World parameters",
        "coordinate": "Coordinate X / Y / Z",
        "system_name": "System name",
        "system_seed": "System seed (read-only)",
        "star_type": "Star typeName",
        "description": "Description",
        "star_image": "System-view star image",
        "star_twinkle_frames": "Universe-map star frames",
        "star_twinkle_background": "Universe-map frame background",
        "image_scale": "Image scale",
        "magnitude": "Magnitude",
        "space_threat": "Space threat level",
        "space_threat_range": "Space threat level must be an integer from 1 to 6.",
        "brightness_min": "Brightness minimum",
        "brightness_max": "Brightness maximum",
        "body_list": "Bodies in this system",
        "kind": "Kind",
        "orbit": "Orbit",
        "name": "Name",
        "world_type": "World type",
        "primary_biome": "Primary biome / biome count",
        "planet": "Planet",
        "moon": "Moon",
        "yes": "Yes",
        "no": "No",
        "planet_orbit": "Planet orbit",
        "moon_orbit": "Moon orbit",
        "body_name": "Body name",
        "body_label": "typeName",
        "small_image": "Small image",
        "small_image_scale": "smallImageScale",
        "large_image": "Direct large image (FloatingDungeon only)",
        "large_image_scale": "imageScale (not system diameter)",
        "world_hue_shift": "World / large-image hue shift (degrees)",
        "global_directives": "Global image directives (advanced)",
        "world_size": "Source size preset",
        "world_width": "World width (tiles)",
        "world_height": "World height (tiles)",
        "system_map_size": "System-map diameter",
        "gravity": "Gravity",
        "terrestrial_type": "Source terrestrial types (read-only)",
        "body_seed": "Body seed (read-only)",
        "world_file": "Generated .world (read-only)",
        "apply_body": "Apply body fields",
        "new_body_type": "New celestial type",
        "new_body_size": "New planet size",
        "planet_body": "Planet",
        "gas_giant": "Gas giant",
        "asteroid_field": "Asteroid field",
        "ancient_gateway": "Ancient gateway",
        "outpost_copy": "Outpost orbital copy (experimental)",
        "add_planet": "Add as planet",
        "add_moon": "Add as selected planet's moon",
        "delete_body": "Remove from star map",
        "rebuild_names": "Rebuild all body names from system name",
        "mpi_ok": "More Planet Info: names are compatible. Use prefix colors such as ^#46D6A0; and keep the final number ^green;I–XX^white;. Do not use lowercase green earlier in the name.",
        "mpi_warning": "More Planet Info: {unsafe} unsafe name(s), {unnumbered} missing number badge(s). Use a prefix color such as ^#46D6A0; and end with ^green;I^white; through ^green;XX^white;. No lowercase green before that suffix, or text after it.",
        "body_hint": "For a terrestrial body, typeName selects the large cockpit-view profile in celestial.config and is also the visible star-map label. The ordinary system-map diameter comes from numeric world width, not imageScale. Direct large image is used only by FloatingDungeon bodies. Image paths must exist in the selected game's assets. Orbit-only changes preserve terrain and buildings.",
        "biome_layer": "Generation layer",
        "biome_group": "Ordered group",
        "main_regions": "Main regions (first is primary)",
        "sub_regions": "Sub-regions (first is primary)",
        "biome_name": "Biome name",
        "biome_order": "Biome order",
        "add_biome": "Add from assets",
        "remove_biome": "Remove",
        "move_up": "Move up",
        "move_down": "Move down",
        "apply_biomes": "Apply biome order",
        "biome_all_applied": "All staged Layer/Main/Sub-region edits for this celestial body were applied.",
        "subregion_pairing_title": "Main/Sub-region pairing adjusted",
        "subregions_filled": "{layer}: Sub-regions were short; added {biomes} from the corresponding Main regions.",
        "subregions_trimmed": "{layer}: Sub-regions were longer than Main regions; removed trailing {biomes}.",
        "uniform_ocean_slots_restored": "{layer}: restored locked uniform-ocean Sub-region slot(s): {biomes}.",
        "ocean_floor_settings": "Ocean-floor pairing…",
        "ocean_floor_title": "Ocean-floor pairing",
        "ocean_surface_biome": "Surface ocean biome",
        "ocean_floor_biome": "Paired floor biome",
        "ocean_floor_bottom": "Floor-biome bottom Y (world default prefilled)",
        "ocean_floor_pair_height": "Floor terrain height (world default prefilled)",
        "ocean_floor_transition": "Ocean/floor boundary Y (world default prefilled)",
        "ocean_disable_subregions": "Keep this ocean uniform (replace its paired sub-region with the same ocean biome)",
        "ocean_coastal_slope": "Build a smooth shore-to-floor basin beside solid land",
        "ocean_coastal_slope_step": "Basin sampling width (tiles)",
        "ocean_floor_ecology_height": "Floor ecology above terrain (tiles)",
        "ocean_floor_biome_overrides": "Floor .biome overrides (JSON object)",
        "ocean_floor_region_overrides": "Floor region-recipe overrides (JSON object)",
        "ocean_floor_pair_hint": "Ocean/floor boundary Y selects the floor environment below it and the surface-ocean parallax above it. The curved bed carries floor ecology; higher water uses Ocean ecology. Floor relief starts below the liquid surface and unsafe relief is flattened. Continuous sloped selectors connect the shores; the shore island guard is retained. Large Hylotl dungeons remain first-creation layer dungeons, not local pairing prefabs.",
        "ocean_ecology_rules": "Curved floor ecology extends at least {minimum} tiles above terrain; larger values are kept. Depth uses the saved liquid level: Floor plants and monster pool are removed through {all_plants} tiles, tall aquatic plants through {tall_plants}, and floor microdungeons through {microdungeons}. Ocean trees are allowed through {ocean_trees} tiles; bush/grass and grass mods remain at all depths. Ordinary Ocean objects and its monster pool stay removed. These are layout-biome rules: native boundary noise can shift effective depths; the shared monster band can extend about one extra tile because its lookup differs from plant anchors.",
        "ocean_floor_saved": "The pairing is staged but has not yet changed the .world. Close this dialog and save the system; when regeneration is needed, Tool 03 builds the native replacement and its final floor in the private save workflow. For an existing world that needs no regeneration, this dialog's Save and apply to generated .world also supports legacy pending-floor recovery.",
        "save_ocean_floor": "Save settings only",
        "apply_ocean_floor_now": "Save and apply to generated .world",
        "ocean_floor_apply_current_message": "Apply this body's complete ocean-floor pairing directly to:\n\n{world}\n\nEvery sector below the detected ocean interval will be reset, including saved blocks, objects and entities. A timestamped backup will be kept. Starbound and its server must be closed.\n\nContinue?",
        "ocean_floor_apply_current_busy": "Compiling and applying the paired ocean floor…",
        "ocean_floor_apply_current_done": "The paired floor was written to the generated world.\n\nOcean intervals: {regions}\nReset sectors: {sectors}\nBackup:\n{backup}",
        "ocean_floor_apply_after_regeneration": "These generation settings require a replacement .world. The pairing remains staged. Close this dialog and save the system so Tool 03 can generate the replacement and apply its floor in one workflow.",
        "ocean_apply_blocked_by_reset": "This body's Ordered Biomes or world size has an unsaved regeneration change. A floor applied to the current world would be discarded. Save the system first; Tool 03's private generation workflow prepares the replacement and its floor together.",
        "direct_world_edit_blocked_by_reset": "This body's Ordered Biomes, world size or other generation recipe still has an unsaved regeneration change. Saving the system would erase a region, parallax or hue edit applied now. Save the system first, let Starbound generate the replacement .world, close the game, then reopen Tool 03 and apply this generated-world edit.",
        "ocean_floor_unavailable": "Select an ocean/toxic/arctic/magma entry in Surface → Main regions.",
        "ocean_apply_title": "Build paired ocean floors",
        "ocean_apply_message": "{count} generated world(s) have pending ocean-floor pairs. Applying them resets the sectors under the detected ocean intervals, including any blocks, objects or entities saved there. A backup of every .world is kept.\n\nContinue?",
        "ocean_apply_done": "Paired ocean floors applied: {worlds} world(s), {regions} ocean interval(s). Pending pairs for worlds not yet generated remain saved.",
        "apply_pending_ocean_floors": "Apply pending ocean floors",
        "native_ordered_title": "Generate ordered biome backgrounds",
        "native_ordered_confirm": "Tool 03 will prepare {count} world(s) in private temporary storage using your game's background server, assign each ordered biome its own parallax, finish any ocean-floor pairs, and verify that the game can reopen them. Keep the game closed. Existing worlds already listed for reset will be fully regenerated; their saved terrain, buildings and entities are kept only in the backup. Generation may take several minutes. If generation fails, the current save is not replaced.\n\nContinue?",
        "open_operation_log": "Open operation log",
        "no_operation_log": "No Tool 03 operation log exists in this universe folder yet.",
        "no_pending_ocean_floors": "No generated .world currently has a pending second-stage ocean-floor build.",
        "ocean_second_stage_required": "{count} ocean world(s) were scheduled for full regeneration. This save prepares their Celestial recipe first; the exact local seabed can only be written after Starbound creates each replacement .world.\n\nKeep Tool 03 open, start Starbound, visit each target planet once, then close the game completely. Tool 03 will detect the stable replacement file and apply the pending floor automatically. The next visit uses the final coastal-basin world. If Tool 03 was closed, load this system again to resume the same automatic step.",
        "ocean_waiting_for_regeneration": "Waiting for Starbound to create and release {count} pending ocean world(s); the floor will be applied automatically.",
        "ocean_starbound_running": "Starbound or its server is still running. The pending ocean floor was not written; Tool 03 will retry after the game closes.",
        "biome_hint": "Asset choices are shown as top-folder-biome, but only the biome name is saved. Apply biome order commits every staged Layer/Main/Sub edit for this body and automatically pairs unequal Main/Sub counts. A uniform surface ocean owns a locked Sub slot at the same index; it follows Main edits and cannot be moved or deleted in Sub. Each mixed surface ocean can have its own floor. Save the system once: required native regeneration, exact ocean-X floor alignment and extra plants within 600 horizontal tiles seaward from actual solid shores finish privately before final delivery; no intermediate game visit or second save is needed.",
        "assets_required": "Select valid assets before adding a biome that is not already in this group.",
        "biome_changed_title": "World regeneration required",
        "biome_changed": "World size or generation content changed for {count} generated world(s). Reset discards their current terrain and buildings; old files remain in automatic backups. Paired ocean worlds are generated and finished in private storage before replacement. Other reset worlds are recreated on the next game visit.\n\nContinue?",
        "native_ocean_confirm": "Tool 03 will create and finish {count} paired ocean world(s) using your game's background server in temporary private storage, then check that the game can reopen them. Keep the game closed. This may take several minutes. Major dungeons intersecting reset sectors are deleted with those sectors; other sectors are not cleared. A failed generation leaves the current save unchanged and retains diagnostic logs.\n\nContinue?",
        "worker_close_blocked": "An operation is still running. Keep Tool 03 open until it finishes or reports an error, so the private server can stop and save files can be committed or restored safely.",
        "native_phase_status": "Private world generation: {phase}. Keep the game closed. {coordinate}",
        "native_phase_prepare": "prepare isolated storage",
        "native_phase_create": "generate native world",
        "native_phase_curve": "write ocean-floor curve",
        "native_phase_flora": "complete nearshore plants (600 tiles)",
        "ocean_flora_completion_hint": "Extra plants are completed only within 600 horizontal tiles seaward from each actual solid shore, not along the whole floor curve. Candidates still come from the native background engine with this world's seed. Plants needing terrain edits are skipped; tile bytes, colors, hue and global noise stay unchanged. Keep the game closed until saving finishes.",
        "native_phase_verify": "reopen and verify final world",
        "native_phase_stage": "stage final file",
        "world_migration_title": "Preserve generated worlds",
        "world_migration": "{count} generated .world file(s) will be copied to their new orbit filenames. Terrain, buildings, objects and entities remain unchanged, and source files are kept.\n\nContinue?",
        "save_system": "Save system to universe.chunks",
        "open_folder": "Open universe folder",
        "initial_status": "Select a storage/universe folder and load its generated systems.",
        "loading_database": "Reading and indexing Celestial2 chunks…",
        "loading_assets": "Loading biome and celestial choices from Assets…",
        "assets_loaded": "Loaded {biomes} biome choices and celestial options from {assets}.",
        "loading_system": "Loading the selected celestial system…",
        "saving_system": "Validating, backing up and rebuilding universe.chunks…",
        "catalog_loaded": "Located {systems} generated systems in {chunks}.",
        "system_loaded": "Loaded {name}: {planets} planets and {moons} moons.",
        "save_confirm_title": "Replace universe.chunks",
        "save_confirm": "This operation will directly replace the selected universe.chunks after creating an automatic backup. Starbound and its server must be fully closed.\n\n{chunks}\n\nContinue?",
        "save_done": "Celestial system saved",
        "save_done_message": "The system was saved and the rebuilt database passed record-by-record verification.\n\nDatabase:\n{chunks}\n\nBackup:\n{backup}\n\nPlanets: {old_count} → {new_count}\nClient contexts updated: {contexts} ({references} references)",
        "save_success": "Saved {name}; backup: {backup}",
        "invalid_chunks": "Select a universe folder containing universe.chunks.",
        "invalid_system": "Select a saved system.",
        "invalid_number": "{field} must be a valid number.",
        "invalid_integer": "{field} must be an integer.",
        "body_not_selected": "Select a planet or moon first.",
        "orbit_used": "Orbit {orbit} is already occupied.",
        "no_planet_orbit": "No free vanilla planet orbit (1–11) remains.",
        "no_moon_orbit": "No free vanilla moon orbit (1–3) remains around this planet.",
        "delete_title": "Remove celestial body",
        "delete_message": "Remove this body from the star map? Existing .world files will be left untouched and become orphaned until restored or migrated.\n\n{name}",
        "names_rebuilt": "Planet and moon names were rebuilt from the current system name. Save the system to keep the change.",
        "names_rebuilt_title": "Body names rebuilt",
        "invalid_coordinate": "Coordinate must contain exactly three integers X, Y, Z in -2147483648..2147483647; they cannot all be 0.",
        "coordinate_used": "Another celestial system already uses coordinate {coordinate}.",
        "coordinate_file_used": "A generated system file already exists at the target coordinate:\n\n{path}",
        "coordinate_move_title": "Move celestial system",
        "coordinate_move": "The system coordinate will change from {source} to {destination}. Its generated .system file and existing planet/moon .world files will be copied to the new coordinate; source files remain as recoverable copies. universe.chunks will be backed up.\n\nContinue?",
        "planet_added": "A fresh {type} was compiled from assets at orbit {orbit}. Edit its fields, then save the system.",
        "moon_added": "A fresh {type} was compiled as a moon at orbit {orbit}. Edit its fields, then save the system.",
        "new_body_parent_required": "Select the planet that will own this moon. Selecting one of its moons also selects that parent planet.",
        "new_body_not_satellite": "This celestial type cannot be added as a satellite. Outpost is an independent FloatingDungeon location.",
        "new_body_assets_required": "Select valid Starbound assets before creating a celestial body.",
        "compiling_body": "Compiling a fresh celestial body from the selected assets…",
        "outpost_note": "The Outpost option creates an independent orbital Floating Dungeon. It is not the unique vanilla InstanceWorld outpost and does not share its quest state.",
        "secondary_region_range": "Secondary-region size range",
        "subregion_range": "Sub-region size range",
        "range_hint": "These two ranges are shared random relative weights, not fixed 0–1 start/end boundaries for individual biomes. Starbound randomizes wrapped X boundaries; use this editor's Generated biome regions page for exact coordinate intervals after generation.",
        "invalid_range": "{field} must contain two numbers in ascending order.",
        "station_list": "Objects saved in this .system",
        "station_type": "New station type",
        "station_target": "Orbit target",
        "system_center": "System center",
        "add_station": "Add space station",
        "remove_station": "Remove selected object",
        "station_hint": "Space stations are .system objects, not planets or moons. Orbit target selects the system center/body; radius is its star-map distance; angle is 0° right, 90° up, 180° left and 270° down; direction is +1 or -1. Apply station orbit stages all fields, then Save system writes the .system file.",
        "station_parameters": "Instance parameters (JSON object)",
        "station_parameters_hint": "displayName is the title shown when this star-map object is selected, and may be changed (for example to LCYY-96); description is its information text; icon is the star-map image path; bookmarkIcon is the bookmark icon key. A temporary anomaly/ship may later despawn or be regenerated, losing its saved override. The object type still supplies warpAction, speed, sky and destination InstanceWorld from assets.",
        "station_added": "Added {type} around {target}. Save the system to keep it.",
        "station_removed": "The selected system object was removed from the staged .system data.",
        "station_assets_required": "Select valid assets before adding a station.",
        "station_type_required": "Select a space-station type.",
        "station_object": "Object type",
        "station_uuid": "UUID",
        "station_radius": "Orbit radius",
        "station_orbit_values": "Orbit radius / initial angle",
        "station_angle": "Initial angle (degrees)",
        "station_direction": "Orbit direction",
        "apply_station": "Apply station orbit",
        "world_size_edit": "Size template preset",
        "day_length": "Day length",
        "weather_pool": "Weather pool",
        "sky_colors": "Sky and light colors (JSON values)",
        "layer_boundary": "Vertical generation layer",
        "layer_min_height": "Minimum height",
        "layer_base_height": "Base height",
        "layer_dungeons": "Real generation dungeons",
        "layer_dungeon_variance": "Dungeon X variance",
        "layer_display_dungeons": "Star-map display dungeons (MPI)",
        "display_dungeons_deferred_title": "Display override deferred",
        "display_dungeons_deferred": "A star-map-only dungeon list was not applied to {count} body/bodies because they do not yet have a generated .world or are being regenerated now. Their Celestial dungeon list must remain the real generation list for the first generation. After Starbound creates the .world, reopen Tool 03 and save the display-only list again.",
        "apply_world_parameters": "Apply world parameters",
        "world_parameters_hint": "Weather: [[weight, \"clear\"], ...]. Layer heights must remain ordered and within the world. Choose real dungeons from loaded assets; changing them or their X variance regenerates the world. MPI choices come from that mod's display catalog, require an existing world, and change only the star map. Icons/text remain mod-defined. Clear selection means none.",
        "edit_species": "Edit generated species…",
        "species_title": "Biome species in generated world",
        "spawn_types": "spawnTypes (one per line)",
        "monster_parameters": "monsterParameters (one shared JSON object)",
        "monster_parameters_hint": "This is one shared parameter object for the whole compiled spawn profile. It applies to every spawnTypes entry and cannot assign different parameters per species. Values may include statusSettings, damage, animationCustom, behavior and behaviorConfig.",
        "save_species": "Save species to .world",
        "species_requires_world": "This body has no generated .world yet. Before first generation, species follow the selected biome in assets; a per-world override can be written after the world exists.",
        "species_saved": "Updated {count} compiled copies of {biome}.\n\nBackup:\n{backup}",
        "client_context_warning": "Orbit changes also update affected *.clientcontext references so the cockpit never points to a removed orbit.",
        "no_editor": "Load a system before editing.",
        "generated_warning_title": "Existing generated worlds",
        "generated_warning": "One or more orbit assignments changed. Existing .world files are not moved by this version, so the new orbit will generate a new world while the old file remains orphaned.\n\nSave anyway?",
        "region_edit_target": "Edit target ({count} queued)",
        "region_new_target": "New rectangle",
        "region_queue_empty": "Add a rectangle before applying the region changes.",
        "region_biome": "Biome",
        "region_x_start": "X start",
        "region_x_end": "X end",
        "region_y_start": "Y start",
        "region_y_end": "Y end",
        "ocean_floor_height": "Ocean-floor terrain height (optional)",
        "add_region": "Add / update rectangle",
        "remove_region": "Remove rectangle",
        "apply_regions": "Apply regions to .world",
        "region_hint": "Each edge is rounded independently to the selected 32-tile sector boundary. Full-biome entries reset exactly those sectors and delete their saved buildings, objects, liquids and entities; Parallax-only entries change metadata without resetting tiles. A custom parallax JSON may accompany either mode. Ocean entries may stay uniform or add an independently bounded flat/curved floor; every floor height must remain inside the rounded rectangle.",
        "region_world_required": "Select a terrestrial body that already has a generated .world.",
        "region_assets_required": "Select valid assets before compiling a biome region.",
        "region_confirm_title": "Reset generated biome sectors",
        "region_confirm": "Apply {count} sector-aligned biome/parallax rectangle(s) directly to:\n\n{world}\n\nFull-biome rectangles delete their sectors, including player buildings, objects, liquids and stored entities. Parallax-only rectangles preserve them. Unlisted sectors remain unchanged and a timestamped backup is created. Starbound/server must be closed.\n\nContinue?",
        "region_done": "Applied {count} biome rectangle(s), reset {sectors} sectors.\n\nBackup:\n{backup}",
        "region_boundary_title": "Align narrow biome boundary",
        "region_boundary": "This rectangle leaves a neighbouring compiled region narrower than regionBlending and may create a visible gap. Use the safe aligned X range {start}–{end} instead?",
        "region_added": "Biome rectangle staged. Apply it separately to the generated .world.",
        "region_added_aligned": "Rectangle staged at sector-aligned range X {x1}–{x2}, Y {y1}–{y2}.",
        "region_definition": "Sector-aligned biome or parallax replacement",
        "region_ocean_options": "Ocean / ocean-floor options",
        "round_lower": "Lower sector edge",
        "round_upper": "Upper sector edge",
        "parallax_only": "Parallax only",
        "full_biome": "Full biome",
        "custom_parallax_json": "Custom parallax JSON (optional)",
        "select_parallax_json": "Select parallax JSON",
        "ocean_surface_height": "Ocean water-surface Y",
        "include_ocean_floor": "Add a paired ocean floor",
        "hue_copy_tab": "Copy material hue/color",
        "hue_copy_title": "Copy saved hue/color to matching materials",
        "hue_reference": "Reference rectangle",
        "hue_target": "Target rectangle",
        "hue_analyze": "Analyze reference",
        "hue_apply": "Apply hue/color to .world",
        "hue_hint": "Only saved target tiles are edited. For every matching foreground/background material ID, the dominant reference hue and color variant are copied; terrain, blocks, liquids, objects, entities, biomes and parallax remain unchanged.",
        "hue_analysis_done": "Reference contains {foreground} foreground and {background} background material styles.",
        "hue_copy_confirm": "Apply reference hue/color directly to matching saved materials in:\n\n{world}\n\nThe world must not be open in Starbound. Continue?",
        "hue_copy_done": "Hue/color copied.\n\nForeground tiles changed: {foreground}\nBackground tiles changed: {background}\nBackup:\n{backup}",
        "generated_structure_locked": "Generated world: changing width/height regenerates the whole world; vertical generation boundaries remain locked until that reset.",
    },
    "zh": {
        "window_title": "03 — Starbound 星系编辑器",
        "page_title": "编辑已保存的 Celestial 星系",
        "subtitle": "选择 storage\\universe 文件夹，再按名称或坐标定位已生成星系，并编辑恒星、行星、卫星和各层有序 Biome。",
        "chunks_label": "Universe 文件夹",
        "select_chunks": "选择 storage\\universe 文件夹",
        "chunks_file": "Starbound Universe 文件夹",
        "assets_label": "Assets（新增 Biome 或天体时需要）",
        "select_assets": "选择 assets 文件夹或 packed.pak",
        "load_database": "读取已生成星系",
        "search_label": "按名称 / X:Y:Z 筛选",
        "system_label": "已保存星系",
        "select_system": "读取 universe.chunks 后请选择星系。",
        "system_tab": "星系",
        "bodies_tab": "行星与卫星",
        "biomes_tab": "有序 Biome",
        "system_objects_tab": "星系对象",
        "regions_tab": "已生成世界 Biome 区域",
        "world_parameters_tab": "世界参数",
        "coordinate": "坐标 X / Y / Z",
        "system_name": "星系名称",
        "system_seed": "星系 Seed（只读）",
        "star_type": "恒星 typeName",
        "description": "描述",
        "star_image": "星系内部视图恒星图片",
        "star_twinkle_frames": "宇宙星图恒星帧图片",
        "star_twinkle_background": "宇宙星图帧背景",
        "image_scale": "图片缩放",
        "magnitude": "星等 Magnitude",
        "space_threat": "太空威胁等级",
        "space_threat_range": "太空威胁等级必须是 1 到 6 的整数。",
        "brightness_min": "亮度最小值",
        "brightness_max": "亮度最大值",
        "body_list": "星系内天体",
        "kind": "类型",
        "orbit": "轨道",
        "name": "名称",
        "world_type": "世界类型",
        "primary_biome": "主要 Biome / Biome 数量",
        "planet": "行星",
        "moon": "卫星",
        "yes": "是",
        "no": "否",
        "planet_orbit": "行星轨道",
        "moon_orbit": "卫星轨道",
        "body_name": "天体名称",
        "body_label": "typeName",
        "small_image": "小型图片",
        "small_image_scale": "smallImageScale",
        "large_image": "直接大图路径（仅 FloatingDungeon）",
        "large_image_scale": "imageScale（不是星系视角直径）",
        "world_hue_shift": "世界 / 行星大图色相偏移（度）",
        "global_directives": "全局图像 directives（高级）",
        "world_size": "来源大小预设",
        "world_width": "世界宽度（格）",
        "world_height": "世界高度（格）",
        "system_map_size": "星系视角直径",
        "gravity": "重力",
        "terrestrial_type": "来源 Terrestrial 类型（只读）",
        "body_seed": "天体 Seed（只读）",
        "world_file": "已生成 .world（只读）",
        "apply_body": "应用当前天体参数",
        "new_body_type": "新天体类型",
        "new_body_size": "新星球大小",
        "planet_body": "星球",
        "gas_giant": "气态巨行星",
        "asteroid_field": "小行星带",
        "ancient_gateway": "古代星门",
        "outpost_copy": "Outpost 轨道副本（实验性）",
        "add_planet": "新增为行星",
        "add_moon": "新增为所选行星的卫星",
        "delete_body": "从星图移除",
        "rebuild_names": "根据星系名重建全部天体名称",
        "mpi_ok": "More Planet Info：名称格式兼容。前缀颜色可用 ^#46D6A0;；末尾保留 ^green;I–XX^white; 编号，前面不要出现小写 green。",
        "mpi_warning": "More Planet Info：{unsafe} 个名称有崩溃风险，另有 {unnumbered} 个缺少编号。前缀颜色可用 ^#46D6A0;，末尾用 ^green;I^white; 至 ^green;XX^white;；此前勿用小写 green，编号后勿加文字。",
        "body_hint": "陆地天体的 typeName 会选择 celestial.config 中的驾驶舱放大图样式，同时也是星图显示的类型 Label。普通星球在星系总览中的直径来自数值世界宽度，不是 imageScale；直接大图路径只供 FloatingDungeon 使用。图片路径必须存在于当前游戏 assets。只改轨道不会改变地形或建筑。",
        "biome_layer": "生成层",
        "biome_group": "有序区域组",
        "main_regions": "主要区域（第一个是 Primary）",
        "sub_regions": "子区域（第一个是 Primary）",
        "biome_name": "Biome 名称",
        "biome_order": "Biome 顺序",
        "add_biome": "从 Assets 新增",
        "remove_biome": "删除",
        "move_up": "上移",
        "move_down": "下移",
        "apply_biomes": "应用 Biome 顺序",
        "biome_all_applied": "已应用当前天体中所有暂存的 Layer、Main regions 和 Sub-regions 改动。",
        "subregion_pairing_title": "已调整 Main/Sub 区域配对",
        "subregions_filled": "{layer}：Sub-regions 数量不足，已按照对应的 Main regions 补入 {biomes}。",
        "subregions_trimmed": "{layer}：Sub-regions 数量多于 Main regions，已从末尾裁剪 {biomes}。",
        "uniform_ocean_slots_restored": "{layer}：已恢复锁定的统一海洋 Sub-region 位置：{biomes}。",
        "ocean_floor_settings": "海洋—海床配对…",
        "ocean_floor_title": "海洋—海床配对",
        "ocean_surface_biome": "上层海洋 Biome",
        "ocean_floor_biome": "配对海床 Biome",
        "ocean_floor_bottom": "海床 Biome 下边界 Y（自动填入世界默认值）",
        "ocean_floor_pair_height": "海床地形高度（自动填入世界默认值）",
        "ocean_floor_transition": "海洋/海床分界 Y（自动填入世界默认值）",
        "ocean_disable_subregions": "保持这片海洋完整（用同一个海洋 Biome 替代其配对 Sub-region）",
        "ocean_coastal_slope": "在实体陆地岸边生成平滑的海岸—盆底曲线",
        "ocean_coastal_slope_step": "盆地曲线采样宽度（格）",
        "ocean_floor_ecology_height": "海床生态高出地形（格）",
        "ocean_floor_biome_overrides": "海床 .biome 覆盖参数（JSON 对象）",
        "ocean_floor_region_overrides": "海床区域生成覆盖参数（JSON 对象）",
        "ocean_floor_pair_hint": "海洋/海床分界 Y 以下使用海床环境，以上保留表层海洋 Parallax。曲线地面及其上方覆盖区使用 Floor 生态，更高水域使用 Ocean 生态。海床起伏从液面以下开始，可能露出水面的起伏会压平；连续倾斜地形连接岸线，并保留近岸岛屿禁区。Hylotl 水下城等大型 Dungeon 仍是首次生成的 Layer Dungeon，不是局部配对预制建筑。",
        "ocean_ecology_rules": "曲线海床生态至少高出地形 {minimum} 格，已有更大值保留。深度以保存的真实液面为基准：Floor 在水下 {all_plants} 格内禁植物和怪物池，{tall_plants} 格内禁高大水生植物，{microdungeons} 格内禁海床 microdungeon。Ocean 树在水下 {ocean_trees} 格内允许；bush、grass 和草皮在所有深度保留，普通 Object 和 Ocean 怪物池继续禁用。这些是布局生态规则：原版边界噪声仍会偏移实际生效深度；怪物与植物的取样位置不同，共用分层时怪物禁区还可能多约 1 格。",
        "ocean_floor_saved": "配对设置已暂存，但尚未修改 .world。请关闭本窗口并保存星系；需要重生成时，03 会在临时存档流程中生成原生新世界并写入最终海床。不需要重生成的已有世界，也可在本窗口使用“保存并立即应用到已生成 .world”，用于修改海床或恢复旧版待处理配对。",
        "save_ocean_floor": "仅保存设置",
        "apply_ocean_floor_now": "保存并立即应用到已生成 .world",
        "ocean_floor_apply_current_message": "将把当前天体的完整海洋—海床配对直接应用到：\n\n{world}\n\n检测到的海洋区间下方所有 sector 都会重置，包括已保存的物块、Object 和实体；程序会保留时间戳备份。请先完全关闭 Starbound 和服务器。\n\n是否继续？",
        "ocean_floor_apply_current_busy": "正在编译并应用配对海床……",
        "ocean_floor_apply_current_done": "配对海床已写入已生成世界。\n\n海洋区间：{regions}\n重置 sector：{sectors}\n备份：\n{backup}",
        "ocean_floor_apply_after_regeneration": "当前生成设置需要替代 .world，配对已暂存。请关闭本窗口并保存星系，由 03 在同一流程中生成新世界并写入海床。",
        "ocean_apply_blocked_by_reset": "当前天体的 Ordered Biomes 或世界大小仍有尚未保存的重生成改动，现在应用到旧世界的海床会被丢弃。请先保存星系，由 03 的临时生成流程一起准备新世界和海床。",
        "direct_world_edit_blocked_by_reset": "当前天体的 Ordered Biomes、世界大小或其他生成配方仍有尚未保存的重生成改动。现在写入的区域、视差或色相修改会在随后保存星系时被删除。请先保存星系，让 Starbound 生成替代 .world，关闭游戏，再重新打开 03 应用这项已生成世界修改。",
        "ocean_floor_unavailable": "请在 Surface → 主区域中选中 ocean、toxic、arctic 或 magma 条目。",
        "ocean_apply_title": "生成配对海床",
        "ocean_apply_message": "有 {count} 个已生成世界存在待应用的海洋—海床配对。应用时会重置检测到的海洋区间下方 sector，其中已保存的物块、Object 和实体也会被清除；每个 .world 都会保留备份。\n\n是否继续？",
        "ocean_apply_done": "已为 {worlds} 个世界应用配对海床，共处理 {regions} 段海洋；尚未生成 .world 的配对会继续保留。",
        "apply_pending_ocean_floors": "应用待处理海床",
        "native_ordered_title": "生成有序群系背景",
        "native_ordered_confirm": "03 将在独立临时存档中，调用游戏后台服务器准备 {count} 个世界，为各有序群系设置自己的视差背景，完成海床配对（如有），并验证游戏能重新读取。请保持游戏关闭。此前列为重置的已有世界会整星球重新生成，原地形、建筑和实体仅保留在备份中。过程可能需要数分钟；生成失败不替换当前存档。\n\n是否继续？",
        "open_operation_log": "打开操作日志",
        "no_operation_log": "这个 universe 文件夹中还没有 03 的操作日志。",
        "no_pending_ocean_floors": "当前没有已生成的 .world 正在等待第二阶段海床构建。",
        "ocean_second_stage_required": "有 {count} 个海洋世界被安排为完整重生成。本次保存会先准备 Celestial 生成配方；必须等 Starbound 创建替代 .world 后，03 才能写入精确的局部海床。\n\n保持 03 开启，启动 Starbound，分别进入目标星球一次，然后完全关闭游戏。03 会检测已经稳定并释放的替代文件，自动应用待处理海床；下一次进入就是最终的海岸盆地世界。如果已经关闭 03，重新打开并载入这个星系也会继续同一个自动步骤。",
        "ocean_waiting_for_regeneration": "正在等待 Starbound 创建并释放 {count} 个待处理海洋世界；完成后会自动应用海床。",
        "ocean_starbound_running": "Starbound 或服务器仍在运行，因此没有写入待处理海床；游戏关闭后 03 会自动重试。",
        "biome_hint": "Assets 选项显示为“顶层目录-Biome 名称”，但只保存真实 Biome 名称。点击“应用 Biome 顺序”会保存当前天体全部暂存的 Layer/Main/Sub 改动，并自动校正 Main/Sub 数量。保持完整的 Surface 海洋会占用同序号的锁定 Sub 槽位：它随 Main 编辑移动，在 Sub 中不能移动或删除。每片混合 Surface 海洋可独立配对海床。保存星系一次即可在临时存档中完成所需原生重生成、按海洋实际 X 区间对齐海床及真实实体岸边向海内横向 600 格的额外植物补全，再交付最终文件；无需中途进入星球或再次保存。",
        "assets_required": "新增当前组里没有的 Biome 前，请选择有效的 assets。",
        "biome_changed_title": "需要重新生成世界",
        "biome_changed": "有 {count} 个已生成世界的尺寸或生成内容发生改变。重置会清除现有地形与建筑，旧文件保留在自动备份中。有海床配对的世界会先在独立临时存档中生成并完成海床，再替换正式文件；其他重置世界在下次进入游戏时生成。\n\n是否继续？",
        "native_ocean_confirm": "03 将在独立临时存档中，调用游戏的后台服务器生成并完成 {count} 个海床配对世界，再检查游戏能否重新读取它们。请保持游戏关闭，过程可能需要数分钟。与重置 sector 相交的大型 dungeon 会随该区域一起删除，其他 sector 不做清理。生成失败时不替换当前存档，并保留诊断日志。\n\n是否继续？",
        "worker_close_blocked": "操作尚未完成。请等待 03 完成或报告错误后再关闭，以便后台服务器安全退出、保存或恢复文件。",
        "native_phase_status": "临时世界生成阶段：{phase}。请保持游戏关闭。{coordinate}",
        "native_phase_prepare": "准备独立存档",
        "native_phase_create": "生成原生世界",
        "native_phase_curve": "写入海床曲线",
        "native_phase_flora": "补全近岸 600 格植物",
        "ocean_flora_completion_hint": "额外植物补全仅限每个真实实体岸边向海内横向 600 格，不覆盖整条海床曲线。候选仍由原版后台按本世界种子随机生成；放不下或需要改地形的植物跳过。补植物不改物块、颜色、hue 和全局噪声，保存完成前请保持游戏关闭。",
        "native_phase_verify": "重新读取并验证最终世界",
        "native_phase_stage": "准备交付最终文件",
        "world_migration_title": "保留已生成世界",
        "world_migration": "将把 {count} 个已生成 .world 复制为新轨道对应的文件名；其中地形、建筑、Object 和实体均不改变，来源文件也会保留。\n\n是否继续？",
        "save_system": "保存星系到 universe.chunks",
        "open_folder": "打开 Universe 文件夹",
        "initial_status": "请选择 storage\\universe 文件夹并读取其中已生成的星系。",
        "loading_database": "正在读取并索引 Celestial2 数据……",
        "loading_assets": "正在从 Assets 读取生态及天体选项……",
        "assets_loaded": "已从 {assets} 读取 {biomes} 个生态选项及天体选项。",
        "loading_system": "正在读取所选星系……",
        "saving_system": "正在校验、备份并重建 universe.chunks……",
        "catalog_loaded": "已在 {chunks} 中定位 {systems} 个已生成星系。",
        "system_loaded": "已读取 {name}：{planets} 颗行星、{moons} 颗卫星。",
        "save_confirm_title": "替换 universe.chunks",
        "save_confirm": "程序会直接替换所选 universe.chunks，并先建立自动备份。请确保 Starbound 和服务器已经完全退出。\n\n{chunks}\n\n是否继续？",
        "save_done": "星系保存完成",
        "save_done_message": "星系已经保存，重建后的数据库通过逐记录校验。\n\n数据库：\n{chunks}\n\n备份：\n{backup}\n\n行星数量：{old_count} → {new_count}\n已更新客户端上下文：{contexts} 个（{references} 处引用）",
        "save_success": "已保存 {name}；备份：{backup}",
        "invalid_chunks": "请选择包含 universe.chunks 的 Universe 文件夹。",
        "invalid_system": "请选择一个已保存的星系。",
        "invalid_number": "{field} 必须是有效数字。",
        "invalid_integer": "{field} 必须是整数。",
        "body_not_selected": "请先选择一颗行星或卫星。",
        "orbit_used": "轨道 {orbit} 已经被占用。",
        "no_planet_orbit": "原版行星轨道 1–11 已经没有空位。",
        "no_moon_orbit": "这颗行星的原版卫星轨道 1–3 已经没有空位。",
        "delete_title": "移除天体",
        "delete_message": "是否从星图中移除这个天体？已有 .world 不会被删除，而会成为可恢复的孤立文件。\n\n{name}",
        "names_rebuilt": "已经根据当前星系名称重建行星和卫星名称；请保存星系以保留改动。",
        "names_rebuilt_title": "天体名称已重建",
        "invalid_coordinate": "坐标必须恰好包含 X、Y、Z 三个整数，每项范围为 -2147483648..2147483647，且不能全部为 0。",
        "coordinate_used": "坐标 {coordinate} 已经被另一个星系占用。",
        "coordinate_file_used": "目标坐标已经存在已生成的星系文件：\n\n{path}",
        "coordinate_move_title": "迁移星系坐标",
        "coordinate_move": "星系坐标将从 {source} 改为 {destination}。程序会把已生成的 .system 以及行星/卫星 .world 文件复制到新坐标，来源文件保留为可恢复副本，并自动备份 universe.chunks。\n\n是否继续？",
        "planet_added": "已从 Assets 编译新的{type}并加入轨道 {orbit}。请编辑参数后保存星系。",
        "moon_added": "已从 Assets 编译新的{type}，作为轨道 {orbit} 的卫星。请编辑参数后保存星系。",
        "new_body_parent_required": "请先选择卫星所属的行星；如果当前选择的是一颗卫星，也会使用它的母星。",
        "new_body_not_satellite": "这种天体不能新增为卫星。Outpost 是独立的 FloatingDungeon 位置。",
        "new_body_assets_required": "创建新天体前请选择有效的 Starbound Assets。",
        "compiling_body": "正在从所选 Assets 编译全新的天体参数……",
        "outpost_note": "Outpost 选项会创建独立的 Floating Dungeon 轨道副本；它不是原版唯一的 InstanceWorld Outpost，也不会共享任务状态。",
        "secondary_region_range": "次要区域相对大小范围",
        "subregion_range": "子区域相对大小范围",
        "range_hint": "这两组范围是所有次要区域共用的随机相对权重，不是各 Biome 固定的 0–1 起止区间。Starbound 会随机计算环绕 X 边界；生成后可在本编辑器的 Generated biome regions 页面按精确坐标处理。",
        "invalid_range": "{field} 必须包含两个递增的数字。",
        "station_list": ".system 中保存的对象",
        "station_type": "新空间站类型",
        "station_target": "环绕目标",
        "system_center": "星系中心",
        "add_station": "新增空间站",
        "remove_station": "移除所选对象",
        "station_hint": "空间站属于 .system 对象，不是行星或卫星。环绕目标决定星系中心或具体天体；半径是星图距离；角度 0°/90°/180°/270° 分别位于右/上/左/下；方向填写 +1 或 -1。先“应用空间站轨道”暂存全部字段，再“保存星系”写入 .system。",
        "station_parameters": "实例参数（JSON 对象）",
        "station_parameters_hint": "displayName 是在星图中选中该对象时显示的标题，可以改成 LCYY-96 等任意文字；description 是介绍；icon 是星图图片路径；bookmarkIcon 是书签图标键。临时 anomaly/飞船以后可能消失或重新生成，因此覆盖值也可能随之消失。对象类型仍从 Assets 决定 warpAction、速度、天空和目标 InstanceWorld。",
        "station_added": "已在{target}附近加入 {type}；保存星系后生效。",
        "station_removed": "已经从待保存的 .system 数据中移除所选对象。",
        "station_assets_required": "新增空间站前请选择有效的 Assets。",
        "station_type_required": "请选择一种空间站类型。",
        "station_object": "对象类型",
        "station_uuid": "UUID",
        "station_radius": "轨道半径",
        "station_orbit_values": "轨道半径 / 初始角度",
        "station_angle": "初始角度（度）",
        "station_direction": "运行方向",
        "apply_station": "应用空间站轨道",
        "world_size_edit": "大小模板预设",
        "day_length": "昼夜长度",
        "weather_pool": "天气池",
        "sky_colors": "天空与光照颜色（JSON 值）",
        "layer_boundary": "纵向生成层",
        "layer_min_height": "最低高度",
        "layer_base_height": "基准高度",
        "layer_dungeons": "实际生成 Dungeon",
        "layer_dungeon_variance": "Dungeon 横向偏移范围",
        "layer_display_dungeons": "星图显示 Dungeon（MPI）",
        "display_dungeons_deferred_title": "显示覆盖暂缓应用",
        "display_dungeons_deferred": "有 {count} 个天体的“仅星图显示 Dungeon”没有应用，因为其 .world 尚未生成，或本次正要重生成。首次生成时 Celestial 的 Dungeon 列表必须保留真实生成列表。请让 Starbound 生成 .world 后，重新打开 03，再保存一次仅显示列表。",
        "apply_world_parameters": "应用世界参数",
        "world_parameters_hint": "天气格式：[[权重, \"clear\"], ...]。层高须有序且在世界高度内。实际生成 Dungeon 从已加载资源选择，改动该列表或 X 偏移范围会重生成世界。MPI 从该 Mod 的显示目录选择，仅适用于已有世界，只改星图；图标和说明仍由 Mod 定义。清空选择表示无。",
        "edit_species": "编辑已生成物种…",
        "species_title": "已生成世界的 Biome 物种",
        "spawn_types": "spawnTypes（每行一个）",
        "monster_parameters": "monsterParameters（全部 spawnTypes 共用）",
        "monster_parameters_hint": "这是整个 compiled spawn profile 共用的一份参数对象，会同时传递给全部 spawnTypes，不能按物种分别赋值。可包含 statusSettings、伤害、animationCustom、behavior 和 behaviorConfig。",
        "save_species": "保存物种到 .world",
        "species_requires_world": "该天体尚无已生成的 .world。首次生成前，物种会跟随 Assets 中的 Biome；世界生成后才能写入单个世界的物种覆盖。",
        "species_saved": "已更新 {biome} 的 {count} 个编译副本。\n\n备份：\n{backup}",
        "client_context_warning": "修改轨道时也会同步更新受影响的 *.clientcontext，避免驾驶舱继续指向已经不存在的轨道。",
        "no_editor": "请先读取一个星系。",
        "generated_warning_title": "发现已生成世界",
        "generated_warning": "一个或多个轨道编号发生了改变。当前版本不会移动已有 .world，因此新轨道会生成新世界，旧文件会保留为孤立文件。\n\n仍然保存吗？",
        "region_edit_target": "编辑目标（已暂存 {count} 项）",
        "region_new_target": "新矩形",
        "region_queue_empty": "请先添加矩形，再应用区域改动。",
        "region_biome": "Biome",
        "region_x_start": "X 起点",
        "region_x_end": "X 终点",
        "region_y_start": "Y 起点",
        "region_y_end": "Y 终点",
        "ocean_floor_height": "海床地形基准高度（可选）",
        "add_region": "新增 / 更新矩形",
        "remove_region": "移除矩形",
        "apply_regions": "应用到 .world",
        "region_hint": "四条边会分别按所选方向取整到 32 格 sector 边界。完整 Biome 会重置这些 sector 并删除其中保存的建筑、Object、液体和实体；“只修改 Parallax”只写元数据，不删除 tile。两种模式都可加载自定义 Parallax JSON。海洋可以保持完整，也可以加入具有独立上下边界的平坦或曲线海床；所有海床高度都必须留在取整后的矩形内。",
        "region_world_required": "请选择一颗已经生成 .world 的 Terrestrial 天体。",
        "region_assets_required": "编译 Biome 区域前请选择有效的 Assets。",
        "region_confirm_title": "重置已生成世界的 Biome sector",
        "region_confirm": "将把 {count} 个按 sector 对齐的 Biome/Parallax 矩形直接应用到：\n\n{world}\n\n完整 Biome 矩形会删除其 sector，包括玩家建筑、Object、液体和实体；只修改 Parallax 的矩形不会删除这些内容。未列出的 sector 保持不变，并建立时间戳备份。请确保游戏和服务器已关闭。\n\n是否继续？",
        "region_done": "已应用 {count} 个 Biome 矩形并重置 {sectors} 个 sector。\n\n备份：\n{backup}",
        "region_boundary_title": "贴齐狭窄的 Biome 边界",
        "region_boundary": "这个矩形会留下比 regionBlending 更窄的相邻 compiled region，可能产生可见 gap。是否改用安全的 X 范围 {start}–{end}？",
        "region_added": "Biome 矩形已经加入待处理列表；请单独应用到已生成的 .world。",
        "region_added_aligned": "矩形已暂存：按 sector 对齐后为 X {x1}–{x2}，Y {y1}–{y2}。",
        "region_definition": "按 Sector 对齐的 Biome 或 Parallax 替换",
        "region_ocean_options": "海洋 / 海床选项",
        "round_lower": "较低坐标的 Sector 边界",
        "round_upper": "较高坐标的 Sector 边界",
        "parallax_only": "只修改 Parallax",
        "full_biome": "完整 Biome",
        "custom_parallax_json": "自定义 Parallax JSON（可选）",
        "select_parallax_json": "选择 Parallax JSON",
        "ocean_surface_height": "海洋水面 Y",
        "include_ocean_floor": "增加配对海床",
        "hue_copy_tab": "复制物块色相/颜色",
        "hue_copy_title": "把参考区域的已保存色相/颜色复制到相同物块",
        "hue_reference": "参考矩形",
        "hue_target": "目标矩形",
        "hue_analyze": "分析参考区域",
        "hue_apply": "应用颜色到 .world",
        "hue_hint": "只编辑目标区域内已经保存的 tile。前景/背景物块 ID 相同时，复制参考区占主导的 Hue 与颜色变体；地形、物块种类、液体、Object、实体、Biome 和 Parallax 都保持不变。",
        "hue_analysis_done": "参考区域包含 {foreground} 种前景物块样式和 {background} 种背景物块样式。",
        "hue_copy_confirm": "将参考区域的色相/颜色直接应用到以下世界中的相同已保存物块：\n\n{world}\n\n请确保 Starbound 没有打开这个世界。是否继续？",
        "hue_copy_done": "色相/颜色复制完成。\n\n修改的前景格：{foreground}\n修改的背景格：{background}\n备份：\n{backup}",
        "generated_structure_locked": "已有世界：修改宽高会重生成整颗星球；纵向生成层边界在重置前仍锁定。",
    },
    "de": {
        "window_title": "03 — Starbound-Sternsystem-Editor",
        "page_title": "Gespeichertes Sternsystem bearbeiten",
        "subtitle": "Wählen Sie den Ordner storage/universe, suchen Sie ein erzeugtes System nach Name oder Koordinaten und bearbeiten Sie Stern, Planeten, Monde und geordnete Biomschichten.",
        "chunks_label": "Universe-Ordner",
        "select_chunks": "Ordner storage/universe auswählen",
        "chunks_file": "Starbound-Universe-Ordner",
        "assets_label": "Assets (für neue Biome und Himmelskörper erforderlich)",
        "select_assets": "Assets-Ordner oder packed.pak auswählen",
        "load_database": "Erzeugte Systeme laden",
        "search_label": "Nach Name / X:Y:Z filtern",
        "system_label": "Gespeichertes System",
        "select_system": "Wählen Sie nach dem Laden ein System aus.",
        "system_tab": "System",
        "bodies_tab": "Planeten und Monde",
        "biomes_tab": "Geordnete Biome",
        "system_objects_tab": "Systemobjekte",
        "regions_tab": "Erzeugte Biomgebiete",
        "world_parameters_tab": "Weltparameter",
        "coordinate": "Koordinaten X / Y / Z",
        "system_name": "Systemname",
        "system_seed": "System-Seed (schreibgeschützt)",
        "star_type": "Stern-typeName",
        "description": "Beschreibung",
        "star_image": "Sternbild in der Systemansicht",
        "star_twinkle_frames": "Sternframes der Universumskarte",
        "star_twinkle_background": "Frame-Hintergrund der Universumskarte",
        "image_scale": "Bildskalierung",
        "magnitude": "Magnitude",
        "space_threat": "Weltraum-Gefahrenstufe",
        "space_threat_range": "Die Weltraum-Gefahrenstufe muss eine ganze Zahl von 1 bis 6 sein.",
        "brightness_min": "Minimale Helligkeit",
        "brightness_max": "Maximale Helligkeit",
        "body_list": "Himmelskörper im System",
        "kind": "Art",
        "orbit": "Umlaufbahn",
        "name": "Name",
        "world_type": "Welttyp",
        "primary_biome": "Primäres Biom / Biomanzahl",
        "planet": "Planet",
        "moon": "Mond",
        "yes": "Ja",
        "no": "Nein",
        "planet_orbit": "Planetenbahn",
        "moon_orbit": "Mondbahn",
        "body_name": "Name des Himmelskörpers",
        "body_label": "typeName",
        "small_image": "Kleines Bild",
        "small_image_scale": "smallImageScale",
        "large_image": "Direktes großes Bild (nur FloatingDungeon)",
        "large_image_scale": "imageScale (nicht Systemdurchmesser)",
        "world_hue_shift": "Welt-/Großbild-Farbtonverschiebung (Grad)",
        "global_directives": "Globale Bild-Directives (erweitert)",
        "world_size": "Quell-Größenvoreinstellung",
        "world_width": "Weltbreite (Blöcke)",
        "world_height": "Welthöhe (Blöcke)",
        "system_map_size": "Durchmesser in der Systemkarte",
        "gravity": "Schwerkraft",
        "terrestrial_type": "Terrestrische Quelltypen (schreibgeschützt)",
        "body_seed": "Seed (schreibgeschützt)",
        "world_file": "Erzeugte .world (schreibgeschützt)",
        "apply_body": "Körperfelder übernehmen",
        "new_body_type": "Neuer Himmelskörpertyp",
        "new_body_size": "Neue Planetengröße",
        "planet_body": "Planet",
        "gas_giant": "Gasriese",
        "asteroid_field": "Asteroidenfeld",
        "ancient_gateway": "Uraltes Tor",
        "outpost_copy": "Outpost-Orbitalkopie (experimentell)",
        "add_planet": "Als Planet hinzufügen",
        "add_moon": "Als Mond des gewählten Planeten hinzufügen",
        "delete_body": "Von Sternkarte entfernen",
        "rebuild_names": "Alle Körpernamen aus Systemnamen aufbauen",
        "mpi_ok": "More Planet Info: Namen kompatibel. Präfixfarben z. B. ^#46D6A0; verwenden; am Ende ^green;I–XX^white; beibehalten. Kein kleingeschriebenes green davor.",
        "mpi_warning": "More Planet Info: {unsafe} unsichere Namen, {unnumbered} fehlende Nummern. Präfixfarbe z. B. ^#46D6A0;; am Ende ^green;I^white; bis ^green;XX^white;. Kein green davor und kein Text danach.",
        "body_hint": "Bei terrestrischen Körpern wählt typeName das Profil der großen Cockpit-Ansicht aus celestial.config und ist zugleich das sichtbare Sternkartenlabel. Der Durchmesser gewöhnlicher Planeten in der Systemkarte stammt aus der numerischen Weltbreite, nicht aus imageScale. Ein direkter Großbildpfad gilt nur für FloatingDungeon; Bildpfade müssen in den Spiel-Assets vorhanden sein. Reine Bahnänderungen bewahren Gelände und Bauten.",
        "biome_layer": "Generierungsschicht",
        "biome_group": "Geordnete Gruppe",
        "main_regions": "Hauptregionen (erste ist primär)",
        "sub_regions": "Unterregionen (erste ist primär)",
        "biome_name": "Biomname",
        "biome_order": "Biomreihenfolge",
        "add_biome": "Aus Assets hinzufügen",
        "remove_biome": "Entfernen",
        "move_up": "Nach oben",
        "move_down": "Nach unten",
        "apply_biomes": "Biomreihenfolge anwenden",
        "biome_all_applied": "Alle vorgemerkten Änderungen an Schichten, Haupt- und Unterregionen dieses Himmelskörpers wurden angewendet.",
        "subregion_pairing_title": "Zuordnung von Haupt-/Unterregionen angepasst",
        "subregions_filled": "{layer}: Es fehlten Unterregionen; {biomes} wurden aus den entsprechenden Hauptregionen ergänzt.",
        "subregions_trimmed": "{layer}: Es gab mehr Unterregionen als Hauptregionen; die letzten Einträge {biomes} wurden entfernt.",
        "uniform_ocean_slots_restored": "{layer}: Gesperrte Unterregionsplätze für einheitliche Ozeane wurden wiederhergestellt: {biomes}.",
        "ocean_floor_settings": "Ozeanboden-Zuordnung…",
        "ocean_floor_title": "Ozeanboden-Zuordnung",
        "ocean_surface_biome": "Oberflächen-Ozeanbiom",
        "ocean_floor_biome": "Zugeordnetes Bodenbiom",
        "ocean_floor_bottom": "Untergrenze des Bodenbioms Y (Weltstandard)",
        "ocean_floor_pair_height": "Bodenhöhe (Weltstandard automatisch eingesetzt)",
        "ocean_floor_transition": "Grenze Ozean/Boden Y (Weltstandard automatisch eingesetzt)",
        "ocean_disable_subregions": "Ozean einheitlich halten (zugeordnete Unterregion durch dasselbe Ozeanbiom ersetzen)",
        "ocean_coastal_slope": "Sanftes Küsten-Becken neben festem Land erzeugen",
        "ocean_coastal_slope_step": "Abtastbreite des Beckens (Blöcke)",
        "ocean_floor_ecology_height": "Bodenökologie über Gelände (Blöcke)",
        "ocean_floor_biome_overrides": "Überschreibungen für Boden-.biome (JSON-Objekt)",
        "ocean_floor_region_overrides": "Überschreibungen des Boden-Rezepts (JSON-Objekt)",
        "ocean_floor_pair_hint": "Unterhalb der Ozean/Boden-Grenze Y gilt die Bodenumgebung; darüber bleibt die Ozean-Parallaxe. Am Kurvenbett gilt Bodenökologie, im höheren Wasser Ozeanökologie. Unsicheres Relief wird unter der Wasseroberfläche abgeflacht. Stetige Neigungen verbinden die Ufer; die Insel-Sperrzone bleibt erhalten. Große Hylotl-Dungeons bleiben Layer-Dungeons der Ersterzeugung, keine lokalen Paarungs-Prefabs.",
        "ocean_ecology_rules": "Die Ökologie über dem Kurvenboden ist mindestens {minimum} Blöcke hoch; größere Werte bleiben erhalten. Gemessen am gespeicherten Flüssigkeitsspiegel: Bodenpflanzen und Monsterpool sind bis {all_plants} Blöcke Tiefe gesperrt, hohe Wasserpflanzen bis {tall_plants}, Boden-Mikrodungeons bis {microdungeons}. Ozeanbäume sind bis {ocean_trees} Blöcke Tiefe erlaubt; Busch/Gras und Grasbeläge bleiben in jeder Tiefe erhalten. Gewöhnliche Ozeanobjekte und der Ozean-Monsterpool bleiben entfernt. Dies sind Layout-Biomregeln: natives Grenzrauschen kann die wirksamen Tiefen verschieben; durch unterschiedliche Abfragepunkte kann die Monstersperre etwa einen weiteren Block umfassen.",
        "ocean_floor_saved": "Die Zuordnung ist vorgemerkt, hat die .world aber noch nicht geändert. Schließen Sie dieses Fenster und speichern Sie das System. Falls eine Neuerzeugung nötig ist, erzeugt Tool 03 die native Ersatzwelt samt endgültigem Boden im privaten Speicherablauf. Für bestehende Welten ohne Neuerzeugung bleibt „Speichern und auf erzeugte .world anwenden“ in diesem Dialog auch zur Wiederherstellung alter vorgemerkter Böden verfügbar.",
        "save_ocean_floor": "Nur Einstellungen speichern",
        "apply_ocean_floor_now": "Speichern und auf erzeugte .world anwenden",
        "ocean_floor_apply_current_message": "Die vollständige Ozeanboden-Zuordnung dieses Körpers wird direkt angewendet auf:\n\n{world}\n\nAlle Sektoren unter dem erkannten Ozeanintervall werden einschließlich gespeicherter Blöcke, Objekte und Entitäten zurückgesetzt. Eine Sicherung mit Zeitstempel bleibt erhalten. Starbound und Server müssen geschlossen sein.\n\nFortfahren?",
        "ocean_floor_apply_current_busy": "Zugeordneter Ozeanboden wird kompiliert und angewendet…",
        "ocean_floor_apply_current_done": "Der zugeordnete Boden wurde in die erzeugte Welt geschrieben.\n\nOzeanintervalle: {regions}\nZurückgesetzte Sektoren: {sectors}\nSicherung:\n{backup}",
        "ocean_floor_apply_after_regeneration": "Diese Einstellungen erfordern eine Ersatz-.world. Die Zuordnung bleibt vorgemerkt. Schließen Sie den Dialog und speichern Sie das System; Tool 03 erzeugt Ersatzwelt und Boden in einem Ablauf.",
        "ocean_apply_blocked_by_reset": "Ordered Biomes oder Weltgröße dieses Körpers enthalten eine ungespeicherte Neuerzeugungsänderung. Ein jetzt angewendeter Boden würde verworfen. Speichern Sie zuerst das System; Tool 03 bereitet Ersatzwelt und Boden gemeinsam im privaten Generierungsablauf vor.",
        "direct_world_edit_blocked_by_reset": "Für die Ordered Biomes, Weltgröße oder ein anderes Erzeugungsrezept dieses Körpers liegt noch eine ungespeicherte Neuerzeugungsänderung vor. Beim späteren Speichern würden jetzt angewandte Regions-, Parallax- oder Farbtonänderungen gelöscht. Speichern Sie zuerst das System, lassen Sie Starbound die Ersatz-.world erzeugen, schließen Sie das Spiel und wenden Sie die Änderung danach in Tool 03 an.",
        "ocean_floor_unavailable": "Unter Surface → Hauptregionen einen Eintrag ocean/toxic/arctic/magma wählen.",
        "ocean_apply_title": "Zugeordnete Ozeanböden erzeugen",
        "ocean_apply_message": "Für {count} erzeugte Welt(en) stehen Ozeanboden-Zuordnungen aus. Dabei werden die Sektoren unter den erkannten Ozeanbereichen einschließlich gespeicherter Blöcke, Objekte und Entitäten zurückgesetzt. Jede .world wird gesichert.\n\nFortfahren?",
        "ocean_apply_done": "Zugeordnete Böden angewendet: {worlds} Welt(en), {regions} Ozeanbereich(e). Zuordnungen für noch nicht erzeugte Welten bleiben gespeichert.",
        "apply_pending_ocean_floors": "Ausstehende Ozeanböden anwenden",
        "native_ordered_title": "Hintergründe geordneter Biome erzeugen",
        "native_ordered_confirm": "Tool 03 bereitet {count} Welt(en) mit dem Spielserver in einem privaten temporären Speicher vor, weist jedem geordneten Biom seinen eigenen Parallax-Hintergrund zu, vervollständigt gegebenenfalls Ozeanböden und prüft das erneute Laden. Lassen Sie das Spiel geschlossen. Bereits zum Zurücksetzen vorgesehene Welten werden vollständig neu erzeugt; gespeichertes Gelände, Gebäude und Entitäten bleiben nur in der Sicherung erhalten. Dies kann mehrere Minuten dauern. Bei einem Erzeugungsfehler wird der aktuelle Spielstand nicht ersetzt.\n\nFortfahren?",
        "open_operation_log": "Vorgangsprotokoll öffnen",
        "no_operation_log": "In diesem Universe-Ordner ist noch kein Vorgangsprotokoll von Werkzeug 03 vorhanden.",
        "no_pending_ocean_floors": "Für keine erzeugte .world steht derzeit die zweite Stufe des Ozeanboden-Aufbaus aus.",
        "ocean_second_stage_required": "Für {count} Ozeanwelt(en) ist eine vollständige Neuerzeugung vorgesehen. Zuerst wird das Celestial-Rezept gespeichert; der exakte lokale Meeresboden kann erst geschrieben werden, nachdem Starbound die neue .world angelegt hat.\n\nWerkzeug 03 geöffnet lassen, Starbound starten, jeden Zielplaneten einmal besuchen und das Spiel vollständig schließen. Werkzeug 03 erkennt die stabile, freigegebene Ersatzdatei und wendet den ausstehenden Boden automatisch an. Der nächste Besuch verwendet die fertige Küstenbecken-Welt. War Werkzeug 03 geschlossen, dieses System erneut laden, um den automatischen Schritt fortzusetzen.",
        "ocean_waiting_for_regeneration": "Warte darauf, dass Starbound {count} ausstehende Ozeanwelt(en) erzeugt und freigibt; der Boden wird danach automatisch angewendet.",
        "ocean_starbound_running": "Starbound oder der Server läuft noch. Der ausstehende Meeresboden wurde nicht geschrieben; Werkzeug 03 versucht es nach dem Beenden des Spiels erneut.",
        "biome_hint": "Asset-Einträge erscheinen als oberster-Ordner-Biom, gespeichert wird nur der Biomname. Biomreihenfolge anwenden übernimmt alle vorgemerkten Layer/Main/Sub-Änderungen dieses Körpers und gleicht ungleiche Anzahlen automatisch an. Ein einheitlicher Surface-Ozean besitzt am selben Index einen gesperrten Sub-Platz; er folgt Main-Änderungen und kann in Sub weder verschoben noch gelöscht werden. Jeder gemischte Surface-Ozean kann einen eigenen Boden erhalten. Das System einmal speichern: notwendige native Neuerzeugung, genaue X-Ausrichtung und zusätzliche Pflanzen bis 600 horizontale Blöcke meerwärts ab festen Ufern erfolgen privat vor der Auslieferung. Kein Zwischenbesuch oder zweites Speichern nötig.",
        "assets_required": "Wählen Sie gültige Assets, bevor Sie ein neues Biom hinzufügen.",
        "biome_changed_title": "Welterneuerzeugung erforderlich",
        "biome_changed": "Weltgröße oder Generierungsinhalt wurde für {count} Welt(en) geändert. Das Zurücksetzen entfernt Gelände und Bauten; alte Dateien bleiben gesichert. Ozeanwelten mit Bodenzuordnung werden vor dem Ersetzen im privaten Speicher fertiggestellt. Andere zurückgesetzte Welten entstehen beim nächsten Spielbesuch.\n\nFortfahren?",
        "native_ocean_confirm": "Tool 03 erzeugt {count} Ozeanwelt(en) mit Bodenzuordnung durch den Spielserver im Hintergrund in einem privaten temporären Speicher und prüft das erneute Laden. Lassen Sie das Spiel geschlossen. Dies kann mehrere Minuten dauern. Große Dungeons in zurückgesetzten Sektoren werden mit diesen Sektoren gelöscht; andere Sektoren werden nicht gelöscht. Bei Fehlern bleibt der aktuelle Spielstand unverändert; Diagnoseprotokolle bleiben erhalten.\n\nFortfahren?",
        "worker_close_blocked": "Der Vorgang läuft noch. Lassen Sie Tool 03 bis zum Abschluss oder einer Fehlermeldung geöffnet, damit der private Server und die Dateisicherung sicher beendet werden können.",
        "native_phase_status": "Private Welterzeugung: {phase}. Spiel geschlossen lassen. {coordinate}",
        "native_phase_prepare": "privaten Speicher vorbereiten",
        "native_phase_create": "native Welt erzeugen",
        "native_phase_curve": "Ozeanboden schreiben",
        "native_phase_flora": "Pflanzen bis 600 Blöcke vom Ufer ergänzen",
        "ocean_flora_completion_hint": "Zusätzliche Pflanzen nur bis 600 horizontale Blöcke meerwärts ab jedem tatsächlichen festen Ufer ergänzen, nicht entlang der ganzen Bodenkurve. Kandidaten stammen weiter aus der nativen Hintergrund-Engine mit dem Seed dieser Welt. Pflanzen mit nötigen Geländeänderungen werden übersprungen; Kachelbytes, Farben, Hue und globales Rauschen bleiben erhalten. Spiel bis zum Speicherabschluss geschlossen lassen.",
        "native_phase_verify": "fertige Welt erneut laden und prüfen",
        "native_phase_stage": "fertige Datei vorbereiten",
        "world_migration_title": "Erzeugte Welten bewahren",
        "world_migration": "{count} erzeugte .world-Datei(en) werden unter die neuen Bahndateinamen kopiert. Gelände, Bauten, Objekte und Entitäten bleiben unverändert; Quelldateien bleiben erhalten.\n\nFortfahren?",
        "save_system": "System in universe.chunks speichern",
        "open_folder": "Universe-Ordner öffnen",
        "initial_status": "Wählen Sie einen storage/universe-Ordner und laden Sie seine erzeugten Systeme.",
        "loading_database": "Celestial2-Datensätze werden indiziert…",
        "loading_assets": "Biom- und Himmelskörperoptionen werden aus Assets geladen…",
        "assets_loaded": "{biomes} Biomoptionen und Himmelskörperoptionen aus {assets} geladen.",
        "loading_system": "Ausgewähltes Sternsystem wird geladen…",
        "saving_system": "universe.chunks wird geprüft, gesichert und neu aufgebaut…",
        "catalog_loaded": "{systems} erzeugte Systeme in {chunks} gefunden.",
        "system_loaded": "{name} geladen: {planets} Planeten und {moons} Monde.",
        "save_confirm_title": "universe.chunks ersetzen",
        "save_confirm": "Die gewählte universe.chunks wird nach einer automatischen Sicherung direkt ersetzt. Starbound und der Server müssen vollständig beendet sein.\n\n{chunks}\n\nFortfahren?",
        "save_done": "Sternsystem gespeichert",
        "save_done_message": "Das System wurde gespeichert; die neue Datenbank hat die Datensatzprüfung bestanden.\n\nDatenbank:\n{chunks}\n\nSicherung:\n{backup}\n\nPlaneten: {old_count} → {new_count}\nAktualisierte Client-Kontexte: {contexts} ({references} Verweise)",
        "save_success": "{name} gespeichert; Sicherung: {backup}",
        "invalid_chunks": "Wählen Sie einen Universe-Ordner mit universe.chunks.",
        "invalid_system": "Wählen Sie ein gespeichertes System.",
        "invalid_number": "{field} muss eine gültige Zahl sein.",
        "invalid_integer": "{field} muss eine Ganzzahl sein.",
        "body_not_selected": "Wählen Sie zuerst einen Planeten oder Mond.",
        "orbit_used": "Umlaufbahn {orbit} ist bereits belegt.",
        "no_planet_orbit": "Keine freie Vanilla-Planetenbahn (1–11) verfügbar.",
        "no_moon_orbit": "Keine freie Vanilla-Mondbahn (1–3) um diesen Planeten verfügbar.",
        "delete_title": "Himmelskörper entfernen",
        "delete_message": "Diesen Körper von der Sternkarte entfernen? Vorhandene .world-Dateien bleiben als verwaiste, wiederherstellbare Dateien erhalten.\n\n{name}",
        "names_rebuilt": "Planeten- und Mondnamen wurden aus dem Systemnamen neu aufgebaut. Speichern Sie das System, um die Änderung zu übernehmen.",
        "names_rebuilt_title": "Körpernamen neu aufgebaut",
        "invalid_coordinate": "Die Koordinate muss genau drei Ganzzahlen X, Y, Z in -2147483648..2147483647 enthalten; nicht alle dürfen 0 sein.",
        "coordinate_used": "Ein anderes Sternsystem verwendet bereits die Koordinate {coordinate}.",
        "coordinate_file_used": "An der Zielkoordinate existiert bereits eine erzeugte Systemdatei:\n\n{path}",
        "coordinate_move_title": "Sternsystem verschieben",
        "coordinate_move": "Die Systemkoordinate wird von {source} nach {destination} geändert. Die erzeugte .system-Datei und vorhandene .world-Dateien der Planeten/Monde werden an die neue Koordinate kopiert; die Quelldateien bleiben als wiederherstellbare Kopien erhalten. universe.chunks wird gesichert.\n\nFortfahren?",
        "planet_added": "Ein neuer Typ {type} wurde aus den Assets für Bahn {orbit} kompiliert. Bearbeiten Sie die Felder und speichern Sie das System.",
        "moon_added": "Ein neuer Typ {type} wurde als Mond in Bahn {orbit} kompiliert. Bearbeiten Sie die Felder und speichern Sie das System.",
        "new_body_parent_required": "Wählen Sie den Planeten, zu dem der neue Mond gehören soll. Ein ausgewählter Mond verwendet ebenfalls seinen Elternplaneten.",
        "new_body_not_satellite": "Dieser Himmelskörpertyp kann nicht als Satellit hinzugefügt werden. Der Außenposten ist ein unabhängiger FloatingDungeon-Ort.",
        "new_body_assets_required": "Wählen Sie gültige Starbound-Assets, bevor Sie einen Himmelskörper erstellen.",
        "compiling_body": "Ein neuer Himmelskörper wird aus den gewählten Assets kompiliert…",
        "outpost_note": "Die Outpost-Option erzeugt ein unabhängiges Floating Dungeon in einer Umlaufbahn. Sie ist nicht die eindeutige Vanilla-InstanceWorld und teilt deren Queststatus nicht.",
        "secondary_region_range": "Größenbereich der Sekundärregionen",
        "subregion_range": "Größenbereich der Unterregionen",
        "range_hint": "Diese Bereiche sind gemeinsame zufällige relative Gewichte und keine festen 0–1-Grenzen einzelner Biome. Starbound würfelt die umlaufenden X-Grenzen; genaue Koordinatenbereiche lassen sich nach der Generierung auf der Seite Generated biome regions dieses Editors bearbeiten.",
        "invalid_range": "{field} muss zwei aufsteigende Zahlen enthalten.",
        "station_list": "In dieser .system gespeicherte Objekte",
        "station_type": "Neuer Stationstyp",
        "station_target": "Umlaufziel",
        "system_center": "Systemzentrum",
        "add_station": "Raumstation hinzufügen",
        "remove_station": "Ausgewähltes Objekt entfernen",
        "station_hint": "Raumstationen sind .system-Objekte, keine Planeten oder Monde. Das Ziel bestimmt Systemzentrum oder Himmelskörper; Radius ist der Abstand auf der Sternkarte; 0°/90°/180°/270° bedeutet rechts/oben/links/unten; Richtung ist +1 oder -1. Zuerst die Stationsbahn anwenden, dann das System speichern.",
        "station_parameters": "Instanzparameter (JSON-Objekt)",
        "station_parameters_hint": "displayName ist der Titel beim Auswählen dieses Sternkartenobjekts und darf z. B. LCYY-96 sein; description ist der Infotext, icon der Bildpfad und bookmarkIcon der Lesezeichen-Schlüssel. Temporäre Anomalien/Schiffe können verschwinden oder neu erzeugt werden und die Überschreibung verlieren. Der Objekttyp liefert weiterhin warpAction, Geschwindigkeit, Himmel und Ziel-InstanceWorld aus den Assets.",
        "station_added": "{type} wurde um {target} hinzugefügt. Speichern Sie das System.",
        "station_removed": "Das ausgewählte Systemobjekt wurde aus den vorgemerkten .system-Daten entfernt.",
        "station_assets_required": "Wählen Sie gültige Assets, bevor Sie eine Station hinzufügen.",
        "station_type_required": "Wählen Sie einen Raumstationstyp.",
        "station_object": "Objekttyp",
        "station_uuid": "UUID",
        "station_radius": "Umlaufradius",
        "station_orbit_values": "Umlaufradius / Anfangswinkel",
        "station_angle": "Startwinkel (Grad)",
        "station_direction": "Umlaufrichtung",
        "apply_station": "Stationsumlauf anwenden",
        "world_size_edit": "Größenvorlage",
        "day_length": "Tageslänge",
        "weather_pool": "Wetterpool",
        "sky_colors": "Himmel- und Lichtfarben (JSON-Werte)",
        "layer_boundary": "Vertikale Generierungsschicht",
        "layer_min_height": "Mindesthöhe",
        "layer_base_height": "Basishöhe",
        "layer_dungeons": "Echte Generierungs-Dungeons",
        "layer_dungeon_variance": "Dungeon-X-Abweichung",
        "layer_display_dungeons": "Sternkarten-Dungeons (MPI)",
        "display_dungeons_deferred_title": "Anzeigeüberschreibung verschoben",
        "display_dungeons_deferred": "Eine reine Sternkarten-Dungeonliste wurde für {count} Körper nicht angewendet, weil noch keine .world existiert oder sie gerade neu erzeugt wird. Für die erste Erzeugung muss die Celestial-Liste die echten Dungeons enthalten. Nach Erzeugung der .world Werkzeug 03 erneut öffnen und die Anzeigeliste nochmals speichern.",
        "apply_world_parameters": "Weltparameter anwenden",
        "world_parameters_hint": "Wetter: [[Gewicht, \"clear\"], ...]. Schichthöhen müssen geordnet und innerhalb der Welt liegen. Echte Dungeons stammen aus geladenen Assets; Listen- oder X-Streuungsänderungen erzeugen die Welt neu. MPI nutzt den Anzeigekatalog des Mods, benötigt eine vorhandene Welt und ändert nur die Sternkarte. Icons/Texte bleiben modabhängig. Leere Auswahl bedeutet keine.",
        "edit_species": "Erzeugte Arten bearbeiten…",
        "species_title": "Biome-Arten in der erzeugten Welt",
        "spawn_types": "spawnTypes (eine Zeile pro Eintrag)",
        "monster_parameters": "monsterParameters (gemeinsames JSON-Objekt)",
        "monster_parameters_hint": "Dies ist ein gemeinsames Parameterobjekt für das gesamte kompilierte Spawnprofil. Es gilt für alle spawnTypes; unterschiedliche Parameter pro Art sind nicht möglich. Möglich sind statusSettings, Schaden, animationCustom, behavior und behaviorConfig.",
        "save_species": "Arten in .world speichern",
        "species_requires_world": "Für diesen Körper gibt es noch keine erzeugte .world. Vor der ersten Generierung folgen die Arten dem Biome in den Assets; eine weltbezogene Überschreibung ist danach möglich.",
        "species_saved": "{count} kompilierte Kopien von {biome} wurden aktualisiert.\n\nSicherung:\n{backup}",
        "client_context_warning": "Bahnänderungen aktualisieren betroffene *.clientcontext-Verweise, damit das Cockpit nicht auf eine entfernte Bahn zeigt.",
        "no_editor": "Laden Sie zuerst ein System.",
        "generated_warning_title": "Erzeugte Welten vorhanden",
        "generated_warning": "Mindestens eine Umlaufbahn wurde geändert. Vorhandene .world-Dateien werden nicht verschoben; die neue Bahn erzeugt eine neue Welt und die alte Datei bleibt verwaist.\n\nTrotzdem speichern?",
        "region_edit_target": "Bearbeitungsziel ({count} vorgemerkt)",
        "region_new_target": "Neues Rechteck",
        "region_queue_empty": "Vor dem Anwenden zuerst ein Rechteck hinzufügen.",
        "region_biome": "Biom",
        "region_x_start": "X-Anfang",
        "region_x_end": "X-Ende",
        "region_y_start": "Y-Anfang",
        "region_y_end": "Y-Ende",
        "ocean_floor_height": "Geländehöhe des Meeresbodens (optional)",
        "add_region": "Rechteck hinzufügen / ändern",
        "remove_region": "Rechteck entfernen",
        "apply_regions": "Gebiete auf .world anwenden",
        "region_hint": "Jede Kante wird unabhängig auf die gewählte 32-Block-Sektorgrenze gerundet. Vollständige Biom-Einträge setzen diese Sektoren zurück und löschen gespeicherte Bauten, Objekte, Flüssigkeiten und Entitäten; Nur-Parallax ändert Metadaten ohne Kachel-Reset. Beide Modi können eigenes Parallax-JSON verwenden. Ozeane können einheitlich bleiben oder einen flachen/kurvigen Boden mit eigenen Grenzen erhalten; alle Bodenhöhen müssen im gerundeten Rechteck liegen.",
        "region_world_required": "Wählen Sie einen terrestrischen Körper mit vorhandener .world.",
        "region_assets_required": "Wählen Sie gültige Assets zum Kompilieren eines Biomgebiets.",
        "region_confirm_title": "Erzeugte Biomsektoren zurücksetzen",
        "region_confirm": "{count} sektorausgerichtete Biom-/Parallax-Rechtecke werden direkt angewandt auf:\n\n{world}\n\nVollständige Biomrechtecke löschen ihre Sektoren einschließlich Bauten, Objekten, Flüssigkeiten und Entitäten; Nur-Parallax erhält sie. Nicht gelistete Sektoren bleiben unverändert; eine Sicherung wird erstellt. Spiel/Server müssen beendet sein.\n\nFortfahren?",
        "region_done": "{count} Biomrechtecke angewandt, {sectors} Sektoren zurückgesetzt.\n\nSicherung:\n{backup}",
        "region_boundary_title": "Schmale Biomgrenze ausrichten",
        "region_boundary": "Dieses Rechteck lässt eine Nachbarregion schmaler als regionBlending zurück und kann eine sichtbare Lücke erzeugen. Stattdessen den sicheren X-Bereich {start}–{end} verwenden?",
        "region_added": "Biomrechteck vorgemerkt; wenden Sie es separat auf die erzeugte .world an.",
        "region_added_aligned": "Rechteck im Sektorraster vorgemerkt: X {x1}–{x2}, Y {y1}–{y2}.",
        "region_definition": "Sektorausgerichteter Biom- oder Parallax-Ersatz",
        "region_ocean_options": "Ozean-/Meeresbodenoptionen",
        "round_lower": "Niedrigere Sektorgrenze",
        "round_upper": "Höhere Sektorgrenze",
        "parallax_only": "Nur Parallax",
        "full_biome": "Vollständiges Biom",
        "custom_parallax_json": "Benutzerdefiniertes Parallax-JSON (optional)",
        "select_parallax_json": "Parallax-JSON auswählen",
        "ocean_surface_height": "Wasseroberfläche Y",
        "include_ocean_floor": "Zugeordneten Meeresboden hinzufügen",
        "hue_copy_tab": "Materialfarbton/-farbe kopieren",
        "hue_copy_title": "Gespeicherten Farbton auf passende Materialien kopieren",
        "hue_reference": "Referenzrechteck",
        "hue_target": "Zielrechteck",
        "hue_analyze": "Referenz analysieren",
        "hue_apply": "Farbton/Farbe auf .world anwenden",
        "hue_hint": "Nur gespeicherte Zielkacheln werden geändert. Bei identischen Vorder-/Hintergrund-Material-IDs werden dominanter Farbton und Farbvariante der Referenz kopiert; Gelände, Blocktypen, Flüssigkeiten, Objekte, Entitäten, Biome und Parallax bleiben unverändert.",
        "hue_analysis_done": "Die Referenz enthält {foreground} Vordergrund- und {background} Hintergrundmaterialstile.",
        "hue_copy_confirm": "Referenzfarbton/-farbe direkt auf passende gespeicherte Materialien anwenden in:\n\n{world}\n\nDie Welt darf nicht in Starbound geöffnet sein. Fortfahren?",
        "hue_copy_done": "Farbton/Farbe kopiert.\n\nGeänderte Vordergrundkacheln: {foreground}\nGeänderte Hintergrundkacheln: {background}\nSicherung:\n{backup}",
        "generated_structure_locked": "Erzeugte Welt: Änderungen an Breite/Höhe erzeugen die ganze Welt neu; vertikale Schichtgrenzen bleiben bis zum Zurücksetzen gesperrt.",
    },
}


ROMAN = {
    1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII",
    8: "VIII", 9: "IX", 10: "X", 11: "XI", 12: "XII", 13: "XIII",
    14: "XIV", 15: "XV", 16: "XVI", 17: "XVII", 18: "XVIII",
    19: "XIX", 20: "XX",
}

VANILLA_STAR_TYPES = (
    "bluestar",
    "orangestar",
    "redstar",
    "whitestar",
    "yellowstar",
)

VANILLA_TERRESTRIAL_GRAPHICS = (
    "alien",
    "arctic",
    "barren",
    "desert",
    "forest",
    "garden",
    "jungle",
    "magma",
    "midnight",
    "moon",
    "ocean",
    "savannah",
    "scorchedcity",
    "snow",
    "toxic",
    "tundra",
    "volcanic",
)

SKY_COLOR_KEYS = (
    "mainColor",
    "morningColors",
    "dayColors",
    "eveningColors",
    "nightColors",
    "morningLightColor",
    "dayLightColor",
    "eveningLightColor",
    "nightLightColor",
)


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


REGION_SINGLE_TRANSLATIONS = {
    "en": {
        "body_reset_legend": "* Red-star parameters reset the planet's generated world when changed and saved.",
        "dungeon_select": "Select dungeons", "dungeon_apply": "Apply selection",
        "dungeon_cancel": "Cancel", "dungeon_clear": "Clear selection",
        "dungeon_unknown": "stored / unavailable",
        "dungeon_loading": "Reading real-generation and MPI dungeon choices from the installed assets…",
        "mpi_layers_hint": "MPI display choices apply to Space, Atmosphere, Surface and Subsurface only.",
        "region_operation": "Operation",
        "region_operation_biome": "Biome / parallax",
        "region_operation_tiles": "Replace tiles only",
        "region_operation_spawns": "Replace monster spawns only",
        "region_spawn_current": "Selected region: biome - monsters",
        "region_spawn_replacement": "Replacement spawnProfile JSON (blank = unchanged)",
        "region_spawn_refresh": "Refresh selection",
        "region_spawn_loading": "Reading the selected region's monster pools…",
        "region_spawn_empty": "Select a generated world and a valid rectangle to view its monster pools.",
        "region_spawn_none": "(no monster spawns)",
        "region_warn_spawns": "Completely replaces only the selected automatic monster spawnProfile. Every saved entity remains untouched, including monsters kept in player buildings; temporary runtime monsters are left for the game to discard when the world unloads. Saved tiles are not reset. New layout boundaries can change native blending near the edges, including environment/liquid sampling and future terrain generation; future monster placement is not a hard rectangle fence.",
        "region_spawn_template": "Input template (keep the outer spawnProfile field):\n\"spawnProfile\": {\n  \"monsterParameters\": {\"aggressive\": false, \"colors\": \"garden\"},\n  \"spawnTypes\": [\"blueback\"]\n}",
        "region_spawns_done": "The automatic monster spawnProfile was replaced. Every saved tile and entity, including saved monsters, was preserved.\n\nBackup:\n{backup}",
        "region_spawns_no_change": "The automatic monster spawnProfile was already identical. No file was written or backup created; all saved entities remain unchanged.",
        "region_definition": "Current rectangle (each edge is rounded to a sector boundary)",
        "region_material_title": "Replace saved tile materials — no biome reset",
        "region_tile_mode": "Tile replacement module",
        "region_tile_mode_material": "One materialName → another",
        "region_tile_mode_biome": "Convert to a biome block arrangement",
        "region_source_item": "From materialName",
        "region_target_item": "To materialName",
        "region_biome_material_target": "Target biome palette",
        "region_biome_material_fuzzy": "Starbound-style fuzzy boundary (affects both sides)",
        "region_tile_layer": "Tile layers",
        "region_layer_both": "Foreground + background",
        "region_layer_foreground": "Foreground only",
        "region_layer_background": "Background only",
        "region_load_items": "Load material names from Assets",
        "region_items_loaded": "Loaded {count} materialName entries. You may also type a name directly.",
        "region_items_required": "Enter both source and replacement materialName values, not numeric material IDs.",
        "region_material_hint": "Use materialName from .material files in the selected Assets/tiles/materials and its subfolders, not material IDs or display names. Only saved matching tiles change; this does not generate missing sectors. Existing hue, paint and other tile data stay unchanged.",
        "region_biome_material_hint": "Converts only saved natural main/sub-block materials, preserving their generated clump roles. It does not change terrain shape, biome indexes, hue/paint/mod bytes, liquids or entities. Fuzzy mode samples this world's saved Starbound block/blend noise on a 4-tile grid, so the changed mask can cross both sides of the nominal rectangle; hard mode never crosses it.",
        "region_biome_material_required": "Choose a target biome palette.",
        "region_inspecting_tiles": "Inspecting saved tiles and material names…",
        "region_writing": "Applying the current rectangle to .world…",
        "region_completed": "Current rectangle applied to .world; reset {sectors} sectors.",
        "region_tiles_completed": "Updated .world: replaced {foreground} foreground and {background} background tiles.",
        "region_no_change": "No tile changes needed; .world unchanged.",
        "region_cancelled": "Cancelled; .world unchanged.",
        "region_unload_hint": "Leave the target planet and wait for its world to unload; do not return until editing finishes. In multiplayer, no other player may remain there. Other worlds may stay open.",
        "region_tiles_no_change": "No material changes are needed: no matching saved tiles in the selected layers, or source and target resolve to the same material. No file was written and no backup was created.",
        "region_confirm_title": "Apply current rectangle",
        "region_single_confirm": "Apply only the current rectangle to:\n{world}\n\nActual rounded bounds (inclusive): X {x1}–{x2}, Y {y1}–{y2}\n\n{warning}\n\n{availability}\n\nA backup will be created. Continue?",
        "region_warn_biome": "Full biome replacement resets the selected sectors and deletes saved buildings, objects, plants, liquids and entities there. Other sectors are not selected for replacement.",
        "region_warn_parallax": "Parallax only changes biome background metadata. It does not reset saved tiles, buildings, plants or entities.",
        "region_warn_tiles": "Only material IDs are replaced. Saved object/plant/entity records, wiring, hue and paint are preserved. The new block may have different falling, collision or support behavior: the game may subsequently move blocks or detach unsupported objects. Replacement textures may interpret existing paint differently.",
        "region_tiles_confirm": "Replace {source} → {target} ({layer}) in:\n{world}\n\nActual rounded bounds (inclusive): X {x1}–{x2}, Y {y1}–{y2}\nMatching tiles to change: foreground {foreground}, background {background}.\n\n{warning}\n\n{availability}\n\nA backup will be created. Continue?",
        "region_tiles_done": "Replaced foreground {foreground} and background {background} tiles. No biome reset.\n\nBackup:\n{backup}",
        "region_biome_material_no_change": "No eligible saved natural terrain material needs conversion to this biome palette. No file was written or backup created.",
        "region_biome_material_confirm": "Convert saved natural terrain to {biome} ({layer}, {boundary}) in:\n{world}\n\nNominal rounded bounds (inclusive): X {x1}–{x2}, Y {y1}–{y2}\nPalette: {palette}\nMaterial layers to change: foreground {foreground}, background {background}.\nFuzzy mask: {outside} saved tile(s) outside included; {inside} saved tile(s) inside excluded.\n\n{warning}\n\n{availability}\n\nA backup will be created. Continue?",
        "region_biome_material_done": "Converted foreground {foreground} and background {background} saved material layers to {biome}. Terrain, biome indexes and entities were not reset.\n\nBackup:\n{backup}",
        "region_biome_material_completed": "Updated .world: converted {foreground} foreground and {background} background material layers to {biome}.",
        "region_boundary_hard": "hard boundary",
        "region_boundary_fuzzy": "Starbound-style fuzzy boundary",
        "region_hint": "Apply reads the current form each time; there is no queued target. All four edges use the selected 32-tile sector rounding. Full biome resets the selected sectors; Parallax only changes backgrounds; Replace tiles only changes matching saved materials. Biome colors prefer this world's existing same-biome hue, otherwise a stable planet-seed selection. Ocean/floor heights must stay inside the rounded rectangle.",
    },
    "zh": {
        "body_reset_legend": "* 红色星号标记的参数在修改并保存后会重置星球。",
        "dungeon_select": "选择 Dungeon", "dungeon_apply": "应用选择",
        "dungeon_cancel": "取消", "dungeon_clear": "清空选择",
        "dungeon_unknown": "存档已有／资源不可用",
        "dungeon_loading": "正在从当前资源读取实际生成与 MPI 显示的 Dungeon 选项…",
        "mpi_layers_hint": "MPI 仅显示 Space、Atmosphere、Surface 和 Subsurface 四层的列表。",
        "region_operation": "编辑操作",
        "region_operation_biome": "Biome / Parallax",
        "region_operation_tiles": "仅替换物块",
        "region_operation_spawns": "仅替换区域怪物生成",
        "region_spawn_current": "所选区域：biome - 怪物",
        "region_spawn_replacement": "替换用 spawnProfile JSON（留空不修改）",
        "region_spawn_refresh": "刷新选区",
        "region_spawn_loading": "正在读取所选区域的怪物生成池…",
        "region_spawn_empty": "选择已生成的世界和有效矩形后，将显示该区域的怪物生成池。",
        "region_spawn_none": "（不生成怪物）",
        "region_warn_spawns": "只完整替换选区内星球今后自动生成怪物所使用的 spawnProfile。不会删除或修改任何已保存的实体，包括玩家建筑中的怪物；临时生成的怪物仍由游戏在世界卸载时自行清理。已保存物块也不会重置。新增布局边界可能改变边缘的原生群系混合，包括环境／液体采样及后续地形生成；以后刷怪的落点不是严格矩形围栏。",
        "region_spawn_template": "输入模板（必须保留外层 spawnProfile 字段）：\n\"spawnProfile\": {\n  \"monsterParameters\": {\"aggressive\": false, \"colors\": \"garden\"},\n  \"spawnTypes\": [\"blueback\"]\n}",
        "region_spawns_done": "已完整替换星球自动生成怪物所使用的 spawnProfile。所有已保存物块和实体均保持不变，包括已保存怪物。\n\n备份：\n{backup}",
        "region_spawns_no_change": "星球自动生成怪物所使用的 spawnProfile 已完全相同；没有写入文件或创建备份，所有已保存实体保持不变。",
        "region_definition": "当前矩形（四条边分别取整到 sector 边界）",
        "region_material_title": "替换已保存物块的材质，不重置 Biome",
        "region_tile_mode": "物块替换模块",
        "region_tile_mode_material": "指定 materialName 一对一替换",
        "region_tile_mode_biome": "转换成 Biome 物块排列",
        "region_source_item": "原物块 materialName",
        "region_target_item": "目标物块 materialName",
        "region_biome_material_target": "目标 Biome 材质排列",
        "region_biome_material_fuzzy": "按 Starbound 风格模糊边界（影响边界两侧）",
        "region_tile_layer": "替换图层",
        "region_layer_both": "前景 + 背景",
        "region_layer_foreground": "仅前景",
        "region_layer_background": "仅背景",
        "region_load_items": "从 Assets 加载材质名称",
        "region_items_loaded": "已加载 {count} 个 materialName，也可以直接输入名称。",
        "region_items_required": "请输入原物块和目标物块的 materialName，不要输入数字材质 ID。",
        "region_material_hint": "使用所选 Assets/tiles/materials 及其所有子目录中 .material 文件的 materialName，不是数字 ID 或显示名称。仅替换已保存的匹配物块，不生成尚未保存的 sector；保留原有 hue、喷漆及其它 tile 数据。",
        "region_biome_material_hint": "只把已保存的天然主块／副块换成目标 Biome 的材质，并沿用现有已生成的团簇角色；不改地形形状、Biome 编号、hue／喷漆／mod 字节、液体或实体。模糊模式按该世界已保存的 Starbound block/blend noise 以 4 格网格采样，范围可越过名义矩形的两侧；硬边界绝不越界。",
        "region_biome_material_required": "请选择目标 Biome 材质排列。",
        "region_inspecting_tiles": "正在检查已保存的物块和材质名称…",
        "region_writing": "正在将当前矩形的修改写入 .world…",
        "region_completed": "当前矩形已应用到 .world，重置了 {sectors} 个 sector。",
        "region_tiles_completed": "已更新 .world：替换前景 {foreground} 格、背景 {background} 格。",
        "region_no_change": "无需替换物块，.world 未修改。",
        "region_cancelled": "已取消，.world 未修改。",
        "region_unload_hint": "请离开目标星球并等待该世界卸载；编辑完成前不要返回。多人游戏中，也不能有其他玩家留在该星球。其它世界可以保持运行。",
        "region_tiles_no_change": "无需替换：所选图层中没有匹配的已保存物块，或者原材质与目标材质相同。没有写入文件，也没有创建备份。",
        "region_confirm_title": "应用当前矩形",
        "region_single_confirm": "仅将当前矩形应用到：\n{world}\n\n取整后的实际范围（包含两端）：X {x1}–{x2}，Y {y1}–{y2}\n\n{warning}\n\n{availability}\n\n将创建备份。是否继续？",
        "region_warn_biome": "完整 Biome 替换会重置选中的 sector，并删除其中已保存的建筑、Object、植物、液体和实体。其它 sector 不会被选中进行替换。",
        "region_warn_parallax": "只修改 Parallax 仅改变生态的背景元数据，不重置已保存的物块、建筑、植物和实体。",
        "region_warn_tiles": "仅修改材质编号，保留已保存的 Object／植物／实体记录、电路、色相和喷漆。但新物块可能具有不同的重力、碰撞或支撑属性；进入游戏后，原版物理仍可能使物块下落或使失去支撑的物件脱落。新材质对原有喷漆的显示也可能不同。",
        "region_tiles_confirm": "在以下文件中将 {source} 替换成 {target}（{layer}）：\n{world}\n\n取整后的实际范围（包含两端）：X {x1}–{x2}，Y {y1}–{y2}\n需要替换的匹配物块：前景 {foreground}，背景 {background}。\n\n{warning}\n\n{availability}\n\n将创建备份。是否继续？",
        "region_tiles_done": "已替换前景 {foreground} 格、背景 {background} 格，没有重置 Biome。\n\n备份：\n{backup}",
        "region_biome_material_no_change": "所选范围没有需要转换成该 Biome 材质排列的已保存天然物块；没有写入文件或创建备份。",
        "region_biome_material_confirm": "把以下世界中的已保存天然地形转换成 {biome}（{layer}，{boundary}）：\n{world}\n\n取整后的名义范围（包含两端）：X {x1}–{x2}，Y {y1}–{y2}\n目标材质：{palette}\n将修改的材质层：前景 {foreground}，背景 {background}。\n模糊范围：纳入矩形外 {outside} 个已保存格；排除矩形内 {inside} 个已保存格。\n\n{warning}\n\n{availability}\n\n将创建备份。是否继续？",
        "region_biome_material_done": "已把前景 {foreground} 个、背景 {background} 个已保存材质层转换成 {biome}；没有重置地形、Biome 编号或实体。\n\n备份：\n{backup}",
        "region_biome_material_completed": "已更新 .world：前景 {foreground} 个、背景 {background} 个材质层已转换成 {biome}。",
        "region_boundary_hard": "硬边界",
        "region_boundary_fuzzy": "Starbound 风格模糊边界",
        "region_hint": "每次点击应用都直接读取当前输入，不储存任务队列。四条边分别按所选方向取整到 32 格 sector 边界。完整 Biome 会重置选中 sector；只修改 Parallax 仅改背景；仅替换物块只改已保存的匹配材质。Biome 颜色优先沿用本星球已有同类生态的色相，否则按星球种子稳定选色。所有海洋／海床高度必须在取整后的矩形内。",
    },
    "de": {
        "body_reset_legend": "* Rot markierte Parameter setzen beim Ändern und Speichern die erzeugte Planetenwelt zurück.",
        "dungeon_select": "Dungeons auswählen", "dungeon_apply": "Auswahl anwenden",
        "dungeon_cancel": "Abbrechen", "dungeon_clear": "Auswahl leeren",
        "dungeon_unknown": "gespeichert / nicht verfügbar",
        "dungeon_loading": "Dungeon-Auswahl für Erzeugung und MPI wird aus den installierten Assets gelesen…",
        "mpi_layers_hint": "MPI zeigt nur Listen aus Space, Atmosphere, Surface und Subsurface an.",
        "region_operation": "Bearbeitung",
        "region_operation_biome": "Biom / Parallax",
        "region_operation_tiles": "Nur Blöcke ersetzen",
        "region_operation_spawns": "Nur Monster-Spawns ersetzen",
        "region_spawn_current": "Auswahl: Biom - Monster",
        "region_spawn_replacement": "Ersatz-spawnProfile-JSON (leer = unverändert)",
        "region_spawn_refresh": "Auswahl aktualisieren",
        "region_spawn_loading": "Monsterpools der Auswahl werden gelesen…",
        "region_spawn_empty": "Eine erzeugte Welt und ein gültiges Rechteck auswählen, um Monsterpools zu sehen.",
        "region_spawn_none": "(keine Monster-Spawns)",
        "region_warn_spawns": "Ersetzt ausschließlich das vollständige spawnProfile für künftig automatisch erzeugte Monster in der Auswahl. Alle gespeicherten Entitäten bleiben unverändert, einschließlich Monster in Spielerbauten; vorübergehende Laufzeitmonster werden beim Entladen der Welt weiterhin vom Spiel verworfen. Gespeicherte Blöcke werden nicht zurückgesetzt. Neue Layoutgrenzen können natives Blending am Rand verändern, einschließlich Umgebung/Flüssigkeit und künftiger Geländeerzeugung; künftige Spawnpositionen sind nicht streng auf das Rechteck begrenzt.",
        "region_spawn_template": "Eingabevorlage (äußeres Feld spawnProfile beibehalten):\n\"spawnProfile\": {\n  \"monsterParameters\": {\"aggressive\": false, \"colors\": \"garden\"},\n  \"spawnTypes\": [\"blueback\"]\n}",
        "region_spawns_done": "Das spawnProfile für automatisch erzeugte Monster wurde ersetzt. Alle gespeicherten Blöcke und Entitäten, einschließlich Monster, blieben erhalten.\n\nSicherung:\n{backup}",
        "region_spawns_no_change": "Das automatische Monster-spawnProfile war bereits identisch. Keine Datei oder Sicherung wurde erstellt; alle gespeicherten Entitäten bleiben unverändert.",
        "region_definition": "Aktuelles Rechteck (jede Kante wird auf Sektorgrenzen gerundet)",
        "region_material_title": "Gespeicherte Blockmaterialien ersetzen — kein Biom-Reset",
        "region_tile_mode": "Blockersatz-Modul",
        "region_tile_mode_material": "Ein materialName → ein anderer",
        "region_tile_mode_biome": "In eine Biom-Blockanordnung umwandeln",
        "region_source_item": "Quellmaterial materialName",
        "region_target_item": "Zielmaterial materialName",
        "region_biome_material_target": "Ziel-Biompalette",
        "region_biome_material_fuzzy": "Starbound-artige weiche Grenze (beide Seiten)",
        "region_tile_layer": "Blockebenen",
        "region_layer_both": "Vordergrund + Hintergrund",
        "region_layer_foreground": "Nur Vordergrund",
        "region_layer_background": "Nur Hintergrund",
        "region_load_items": "Materialnamen aus Assets laden",
        "region_items_loaded": "{count} materialName-Einträge geladen. Namen können auch direkt eingegeben werden.",
        "region_items_required": "Quell- und Ziel-materialName eingeben, keine numerischen Material-IDs.",
        "region_material_hint": "Verwenden Sie materialName aus .material-Dateien unter Assets/tiles/materials einschließlich aller Unterordner, nicht Material-ID oder Anzeigename. Nur passende gespeicherte Blöcke werden ersetzt; fehlende Sektoren werden nicht erzeugt. Farbton, Bemalung und andere Blockdaten bleiben erhalten.",
        "region_biome_material_hint": "Nur gespeicherte natürliche Haupt-/Nebenblöcke werden in die Zielpalette umgesetzt; ihre erzeugten Clusterrollen bleiben erhalten. Geländeform, Biomindizes, Farb-/Mod-Daten, Flüssigkeiten und Entitäten bleiben unverändert. Die weiche Grenze tastet gespeichertes Starbound block/blend noise im 4-Block-Raster ab und kann beide Seiten des Nennrechtecks betreffen; die harte Grenze überschreitet es nie.",
        "region_biome_material_required": "Ein Ziel-Biom auswählen.",
        "region_inspecting_tiles": "Gespeicherte Blöcke und Materialnamen werden geprüft…",
        "region_writing": "Das aktuelle Rechteck wird auf .world angewandt…",
        "region_completed": "Aktuelles Rechteck auf .world angewandt; {sectors} Sektoren zurückgesetzt.",
        "region_tiles_completed": ".world aktualisiert: {foreground} Vordergrund- und {background} Hintergrundblöcke ersetzt.",
        "region_no_change": "Keine Blockänderung nötig; .world unverändert.",
        "region_cancelled": "Abgebrochen; .world unverändert.",
        "region_unload_hint": "Den Zielplaneten verlassen und warten, bis seine Welt entladen ist. Bis zum Abschluss nicht zurückkehren. Im Mehrspielermodus darf dort kein anderer Spieler bleiben. Andere Welten dürfen geöffnet bleiben.",
        "region_tiles_no_change": "Keine Änderung nötig: keine passenden gespeicherten Blöcke oder identisches Quell- und Zielmaterial. Keine Datei geschrieben und keine Sicherung erstellt.",
        "region_confirm_title": "Aktuelles Rechteck anwenden",
        "region_single_confirm": "Nur das aktuelle Rechteck anwenden auf:\n{world}\n\nGerundete Grenzen (einschließlich): X {x1}–{x2}, Y {y1}–{y2}\n\n{warning}\n\n{availability}\n\nEine Sicherung wird erstellt. Fortfahren?",
        "region_warn_biome": "Vollständiger Biom-Ersatz setzt die ausgewählten Sektoren zurück und löscht dort Bauten, Objekte, Pflanzen, Flüssigkeiten und Entitäten. Andere Sektoren werden nicht zum Ersatz ausgewählt.",
        "region_warn_parallax": "Nur Parallax ändert Hintergrundmetadaten, ohne gespeicherte Blöcke, Bauten, Pflanzen oder Entitäten zurückzusetzen.",
        "region_warn_tiles": "Nur Material-IDs ändern sich. Objekt-/Pflanzen-/Entitätsdaten, Verkabelung, Farbton und Bemalung bleiben erhalten. Andere Schwerkraft-, Kollisions- oder Stützeigenschaften des Zielblocks können im Spiel jedoch Blöcke bewegen oder Objekte ablösen. Bemalung kann auf dem neuen Material anders aussehen.",
        "region_tiles_confirm": "{source} → {target} ersetzen ({layer}) in:\n{world}\n\nGerundete Grenzen (einschließlich): X {x1}–{x2}, Y {y1}–{y2}\nZu ersetzen: Vordergrund {foreground}, Hintergrund {background}.\n\n{warning}\n\n{availability}\n\nEine Sicherung wird erstellt. Fortfahren?",
        "region_tiles_done": "Vordergrund {foreground}, Hintergrund {background} Blöcke ersetzt. Kein Biom-Reset.\n\nSicherung:\n{backup}",
        "region_biome_material_no_change": "Kein geeignetes gespeichertes natürliches Gelände muss in diese Biompalette umgewandelt werden. Keine Datei geschrieben oder Sicherung erstellt.",
        "region_biome_material_confirm": "Gespeichertes natürliches Gelände in {biome} umwandeln ({layer}, {boundary}) in:\n{world}\n\nGerundeter Nennbereich (einschließlich): X {x1}–{x2}, Y {y1}–{y2}\nPalette: {palette}\nZu ändern: Vordergrund {foreground}, Hintergrund {background}.\nWeiche Maske: {outside} gespeicherte Blöcke außen einbezogen; {inside} innen ausgeschlossen.\n\n{warning}\n\n{availability}\n\nEine Sicherung wird erstellt. Fortfahren?",
        "region_biome_material_done": "Vordergrund {foreground} und Hintergrund {background} gespeicherte Materialebenen in {biome} umgewandelt. Gelände, Biomindizes und Entitäten wurden nicht zurückgesetzt.\n\nSicherung:\n{backup}",
        "region_biome_material_completed": ".world aktualisiert: {foreground} Vordergrund- und {background} Hintergrundebenen in {biome} umgewandelt.",
        "region_boundary_hard": "harte Grenze",
        "region_boundary_fuzzy": "Starbound-artige weiche Grenze",
        "region_hint": "Anwenden liest jedes Mal das aktuelle Formular, ohne Warteschlange. Jede Kante nutzt die gewählte 32-Block-Sektorrundung. Vollständiges Biom setzt Sektoren zurück; Nur Parallax ändert Hintergründe; Nur Blöcke ersetzt gespeicherte Materialien. Biomfarben verwenden bevorzugt vorhandene Farben desselben Bioms, andernfalls eine stabile Planetenseed-Auswahl. Ozean-/Bodenhöhen müssen im gerundeten Rechteck liegen.",
    },
}
for _region_language, _region_messages in REGION_SINGLE_TRANSLATIONS.items():
    TRANSLATIONS[_region_language].update(_region_messages)

FORM_PREFLIGHT_TRANSLATIONS = {
    "en": {
        "required_field": "{field} must not be blank.",
        "integer_range": "{field} must be an integer in {minimum}..{maximum}.",
        "resource_warning_title": "Image resource warnings",
        "resource_warning_confirm": "{details}\n\nThe game may show a fallback image. Keep these values and continue?",
        "resource_warning_cancelled": "Image changes were not applied.",
        "body_applied_unsaved": "Body fields applied to the editor; not saved to disk.",
    },
    "zh": {
        "required_field": "{field} 不能为空。",
        "integer_range": "{field} 必须是 {minimum}..{maximum} 范围内的整数。",
        "resource_warning_title": "图像资源警告",
        "resource_warning_confirm": "{details}\n\n游戏可能显示回退图像。保留这些值并继续吗？",
        "resource_warning_cancelled": "未应用这些图像修改。",
        "body_applied_unsaved": "当前天体参数已应用到编辑器；尚未保存到文件。",
    },
    "de": {
        "required_field": "{field} darf nicht leer sein.",
        "integer_range": "{field} muss eine Ganzzahl in {minimum}..{maximum} sein.",
        "resource_warning_title": "Bildressourcen-Warnungen",
        "resource_warning_confirm": "{details}\n\nDas Spiel zeigt möglicherweise ein Ersatzbild. Werte behalten und fortfahren?",
        "resource_warning_cancelled": "Die Bildänderungen wurden nicht übernommen.",
        "body_applied_unsaved": "Körperfelder im Editor übernommen; noch nicht auf Datenträger gespeichert.",
    },
}
for _form_language, _form_messages in FORM_PREFLIGHT_TRANSLATIONS.items():
    TRANSLATIONS[_form_language].update(_form_messages)


class CelestialSystemEditorWindow(BaseWindow):
    def __init__(self) -> None:
        super().__init__(TRANSLATIONS, "1120x760")
        self._worker_control_states: list[tuple[Any, bool, Any]] = []
        self.root.protocol("WM_DELETE_WINDOW", self.close_window)
        self.root.minsize(960, 650 + self.caption_height)
        self.chunks_var = tk.StringVar()
        self.assets_var = tk.StringVar()
        # Only explicit Assets selection (or opening an asset-backed menu)
        # loads these choices. Universe selection must remain assets-independent.
        self._asset_choices_path: str | None = None
        self.search_var = tk.StringVar()
        self.system_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.catalog: list[dict[str, Any]] = []
        self.catalog_by_label: dict[str, list[int]] = {}
        self.editor: dict[str, Any] | None = None
        self.original_editor: dict[str, Any] | None = None
        self.system_file_state: dict[str, Any] | None = None
        self.original_system_file_state: dict[str, Any] | None = None
        self.body_refs: dict[str, tuple[str, list[Any], list[Any] | None]] = {}
        self.body_origins: dict[int, tuple[int, int] | None] = {}
        self.current_body_iid: str | None = None
        self._selecting_body = False

        self.system_name_var = tk.StringVar()
        self.system_seed_var = tk.StringVar()
        self.coordinate_var = tk.StringVar()
        self.star_type_var = tk.StringVar()
        self.system_description_var = tk.StringVar()
        self.star_image_var = tk.StringVar()
        self.star_twinkle_frames_var = tk.StringVar()
        self.star_twinkle_background_var = tk.StringVar()
        self.image_scale_var = tk.StringVar()
        self.magnitude_var = tk.StringVar()
        self.space_threat_var = tk.StringVar()
        self.brightness_min_var = tk.StringVar()
        self.brightness_max_var = tk.StringVar()

        self.body_kind_var = tk.StringVar()
        self.planet_orbit_var = tk.StringVar()
        self.moon_orbit_var = tk.StringVar()
        self.body_name_var = tk.StringVar()
        self.body_label_var = tk.StringVar()
        self.body_description_var = tk.StringVar()
        self.small_image_var = tk.StringVar()
        self.small_image_scale_var = tk.StringVar()
        self.large_image_var = tk.StringVar()
        self.large_image_scale_var = tk.StringVar()
        self.world_hue_shift_var = tk.StringVar()
        self.global_directives_var = tk.StringVar()
        self.world_type_var = tk.StringVar()
        self.world_size_var = tk.StringVar()
        self.world_width_var = tk.StringVar()
        self.world_height_var = tk.StringVar()
        self.system_map_size_var = tk.StringVar()
        self.loaded_world_dimensions: tuple[int, int] | None = None
        self.gravity_var = tk.StringVar()
        self.terrestrial_type_var = tk.StringVar()
        self.primary_biome_var = tk.StringVar()
        self.body_seed_var = tk.StringVar()
        self.world_file_var = tk.StringVar()
        self.day_length_var = tk.StringVar()
        self.world_layer_var = tk.StringVar()
        self.layer_min_height_var = tk.StringVar()
        self.layer_base_height_var = tk.StringVar()
        self.layer_dungeons_var = tk.StringVar()
        self.layer_dungeon_variance_var = tk.StringVar()
        self.layer_display_dungeons_var = tk.StringVar()
        self.sky_color_vars = {key: tk.StringVar() for key in SKY_COLOR_KEYS}
        self.world_layer_map: dict[str, str] = {}
        self.mpi_var = tk.StringVar()
        self.biome_layer_var = tk.StringVar()
        self.biome_group_var = tk.StringVar(value="regions")
        self.biome_name_var = tk.StringVar()
        self.secondary_region_min_var = tk.StringVar()
        self.secondary_region_max_var = tk.StringVar()
        self.subregion_min_var = tk.StringVar()
        self.subregion_max_var = tk.StringVar()
        self.new_body_type_var = tk.StringVar()
        self.new_body_size_var = tk.StringVar(value="medium")
        self.new_body_type_map: dict[str, str] = {}
        self.available_planet_sizes: list[str] = [
            "verysmall", "small", "medium", "large"
        ]
        self.station_type_var = tk.StringVar()
        self.station_target_var = tk.StringVar()
        self.station_radius_var = tk.StringVar(value="100")
        self.station_angle_var = tk.StringVar(value="0")
        self.station_direction_var = tk.StringVar(value="1")
        self.region_biome_var = tk.StringVar()
        self.region_x_start_var = tk.StringVar(value="0")
        self.region_x_end_var = tk.StringVar()
        self.region_y_start_var = tk.StringVar(value="0")
        self.region_y_end_var = tk.StringVar()
        self.region_ocean_floor_var = tk.StringVar()
        self.region_round_x_start_var = tk.StringVar(value="lower")
        self.region_round_x_end_var = tk.StringVar(value="upper")
        self.region_round_y_start_var = tk.StringVar(value="lower")
        self.region_round_y_end_var = tk.StringVar(value="upper")
        self.region_parallax_only_var = tk.BooleanVar(value=False)
        self.region_parallax_path_var = tk.StringVar()
        self.region_ocean_liquid_var = tk.StringVar()
        self.region_include_floor_var = tk.BooleanVar(value=False)
        self.region_floor_biome_var = tk.StringVar()
        self.region_floor_bottom_var = tk.StringVar()
        self.region_floor_transition_var = tk.StringVar()
        self.region_ocean_uniform_var = tk.BooleanVar(value=True)
        self.region_coastal_slope_var = tk.BooleanVar(value=False)
        self.region_coastal_step_var = tk.StringVar(value="32")
        self.region_floor_ecology_var = tk.StringVar(value=str(swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT))
        self.hue_reference_vars = {
            key: tk.StringVar(value="" if key.endswith("End") else "0")
            for key in ("xStart", "xEnd", "yStart", "yEnd")
        }
        self.hue_target_vars = {
            key: tk.StringVar(value="" if key.endswith("End") else "0")
            for key in ("xStart", "xEnd", "yStart", "yEnd")
        }
        self.region_operation_var = tk.StringVar(value="biome")
        self._spawn_preview_after = None
        self._spawn_preview_poll_after = None
        self._window_closing = False
        self._spawn_preview_serial = 0
        self._spawn_preview_running = False
        self._spawn_preview_queue = queue.Queue()
        self.region_source_item_var = tk.StringVar()
        self.region_target_item_var = tk.StringVar()
        self.region_tile_mode_var = tk.StringVar(value="material")
        self.region_target_biome_var = tk.StringVar()
        self.region_fuzzy_boundary_var = tk.BooleanVar(value=False)
        self.region_tile_layer_var = tk.StringVar(value="both")
        self.region_plan_world: Path | None = None
        self.station_target_map: dict[str, tuple[int, int]] = {}
        self.station_row_map: dict[str, dict[str, Any]] = {}
        self.biome_group_map: dict[str, tuple[str, str]] = {}
        self.biome_pending: dict[tuple[int, str, str], list[str]] = {}
        self.ordered_parallax_applied: dict[int, set[str]] = {}
        self.world_layer_pending: dict[
            tuple[int, str], tuple[str, str, str, str]
        ] = {}
        self.star_map_dungeon_text: dict[tuple[int, str], str] = {}
        self.ocean_pair_settings: dict[int, list[dict[str, Any] | None]] = {}
        self.ocean_pair_pending: dict[int, bool] = {}
        self.ocean_pair_store: dict[str, Any] = {"version": 1, "bodies": {}}
        self._pending_ocean_monitor_after: str | None = None
        self._pending_ocean_observations: dict[str, tuple[int, int]] = {}
        self._audit_lock = threading.Lock()
        self.loaded_biome_key: tuple[int, str, str] | None = None
        self.loaded_world_layer_key: tuple[int, str] | None = None
        self.available_biomes: list[str] = []
        self.biome_display_to_name: dict[str, str] = {}
        self.biome_name_to_display: dict[str, str] = {}
        # Keep the vanilla values available before an assets scan finishes (or
        # when the selected assets path is temporarily unavailable).  Modded
        # values discovered later are merged into this list.
        self.available_star_types: list[str] = list(VANILLA_STAR_TYPES)
        self.available_terrestrial_graphics: list[str] = list(
            VANILLA_TERRESTRIAL_GRAPHICS
        )

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        self.title_label = ttk.Label(header, style="Title.TLabel")
        self.title_label.pack(side="left", anchor="nw")
        self.add_language_selector(header).pack(side="right", anchor="ne")
        self.subtitle_label = ttk.Label(outer, style="Hint.TLabel", wraplength=930)
        self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(3, 10))

        source = ttk.Frame(outer)
        source.grid(row=2, column=0, sticky="ew")
        source.columnconfigure(1, weight=1)
        self.chunks_label, _, self.chunks_browse = self.add_path_row(
            source, 0, "", self.chunks_var, self.browse_chunks
        )
        self.assets_label, self.assets_entry, self.assets_browse = self.add_path_row(
            source, 1, "", self.assets_var, self.browse_assets
        )
        self.assets_entry.bind("<Return>", self.asset_path_entered)
        self.load_button = ttk.Button(source, command=self.load_catalog)
        self.load_button.grid(row=2, column=2, sticky="ew", pady=5)
        self.system_label = ttk.Label(source)
        self.system_label.grid(row=3, column=0, sticky="w", pady=5)
        self.system_combo = ttk.Combobox(
            source, textvariable=self.system_var, state="readonly"
        )
        self.system_combo.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=5)
        self.system_combo.bind("<<ComboboxSelected>>", self.system_selected)
        self.search_label = ttk.Label(source)
        self.search_label.grid(row=2, column=0, sticky="w", pady=5)
        self.search_entry = ttk.Entry(source, textvariable=self.search_var)
        self.search_entry.grid(row=2, column=1, sticky="ew", padx=(10, 8), pady=5)
        self.search_var.trace_add("write", lambda *_args: self.filter_catalog())

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=3, column=0, sticky="nsew", pady=(8, 8))
        self.system_tab = ttk.Frame(self.notebook, padding=12)
        self.bodies_tab = ttk.Frame(self.notebook, padding=10)
        self.world_parameters_tab = ttk.Frame(self.notebook, padding=10)
        self.biomes_tab = ttk.Frame(self.notebook, padding=10)
        self.system_objects_tab = ttk.Frame(self.notebook, padding=10)
        self.regions_tab = ttk.Frame(self.notebook, padding=10)
        self.hue_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.system_tab)
        self.notebook.add(self.bodies_tab)
        self.notebook.add(self.world_parameters_tab)
        self.notebook.add(self.biomes_tab)
        self.notebook.add(self.system_objects_tab)
        self.notebook.add(self.regions_tab)
        self.notebook.add(self.hue_tab)
        self.build_system_tab()
        self.build_bodies_tab()
        self.build_world_parameters_tab()
        self.build_biomes_tab()
        self.build_system_objects_tab()
        self.build_regions_tab()
        self.build_hue_tab()
        for widget in (
            self.new_body_size_combo, self.world_size_edit_combo,
            self.station_type_combo, self.biome_name_combo,
            self.region_biome_combo, self.region_floor_biome_combo,
            self.system_field_widgets.get("star_type"),
            self.body_entries.get("body_label"),
        ):
            if isinstance(widget, ttk.Combobox):
                widget.configure(postcommand=self.refresh_asset_sizes)

        self.build_action_bar(outer)
        self.status_label = ttk.Label(outer, textvariable=self.status_var, wraplength=1100)
        self.status_label.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        self.set_status_key("initial_status")
        self.apply_language()

    def build_action_bar(self, outer: tk.Widget) -> None:
        """Keep only Save and progress in the persistent bottom action row.

        Legacy pending-floor recovery remains in the pairing dialog and the
        existing monitor; removing its old button must not remove that logic.
        """

        bottom = ttk.Frame(outer)
        bottom.grid(row=4, column=0, sticky="ew")
        self.action_button = ttk.Button(
            bottom, style="Accent.TButton", command=self.save_system
        )
        self.action_button.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.pack(side="right", fill="x", expand=True, padx=(16, 0))

    def ocean_floor_editor_hint(self, context_key: str) -> str:
        """Both floor editors display one backend-derived ecology ruleset."""

        rules = self.tr(
            "ocean_ecology_rules",
            minimum=f"{swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT:g}",
            all_plants=f"{swe.OCEAN_FLOOR_ALL_PLANT_CLEARANCE:g}",
            tall_plants=f"{swe.OCEAN_FLOOR_TALL_PLANT_CLEARANCE:g}",
            microdungeons=f"{swe.OCEAN_FLOOR_MICRODUNGEON_CLEARANCE:g}",
            ocean_trees=f"{swe.OCEAN_SURFACE_TREE_MAXIMUM_DEPTH:g}",
        )
        return self.tr(context_key) + "\n\n" + rules + "\n\n" + self.tr("ocean_flora_completion_hint")

    def report_ocean_generation_progress(
        self, event: str, details: dict[str, Any], *, log_prefix: str = "", **context: Any,
    ) -> None:
        """Keep all floor-edit paths on the same thread-safe phase display."""

        self.audit_log(log_prefix + event, **{**context, **details})
        phase = None
        if event == "native_generation_preparing":
            phase = "prepare"
        elif event == "native_server_started":
            phase = details.get("phase", "create")
            if phase == "flora-terrain":
                phase = "flora"
        elif event in ("apply_started", "rectangle_compiled"):
            phase = "curve"
        elif event.startswith(("native_coastal_", "native_viewport_")):
            phase = "flora"
        elif event == "native_final_world_staged":
            phase = "stage"
        root = getattr(self, "root", None)
        if phase not in ("prepare", "create", "curve", "flora", "verify", "stage") or root is None:
            return
        coordinate = ":".join(map(str, details.get("coordinate") or []))
        try:
            root.after(0, lambda current=phase, body=coordinate: self.set_status_key(
                "native_phase_status", phase=self.tr("native_phase_" + current), coordinate=body
            ))
        except tk.TclError:
            pass

    def run_worker(self, task: Any, success: Any, busy_key: str) -> None:
        if self.busy:
            return
        self._worker_control_states = lock_editor_controls(self.root)
        try:
            super().run_worker(task, success, busy_key)
        except Exception:
            unlock_editor_controls(self._worker_control_states)
            self._worker_control_states = []
            raise

    def finish_error(self, error: Exception) -> None:
        unlock_editor_controls(self._worker_control_states)
        self._worker_control_states = []
        super().finish_error(error)

    def finish_success(self, result: Any, callback: Any) -> None:
        unlock_editor_controls(self._worker_control_states)
        self._worker_control_states = []
        super().finish_success(result, callback)

    def close_window(self) -> None:
        if self.busy:
            messagebox.showinfo(
                self.tr("window_title"), self.tr("worker_close_blocked"), parent=self.root
            )
            return
        self._window_closing = True
        for name in ("_spawn_preview_after", "_spawn_preview_poll_after"):
            pending = getattr(self, name, None)
            if pending is not None:
                self.root.after_cancel(pending)
                setattr(self, name, None)
        self.root.destroy()

    def build_system_tab(self) -> None:
        content, self.system_scroll_canvas, self.system_page_scrollbar = build_vertical_scroll_area(self.system_tab)
        self.system_scroll_content = content
        content.columnconfigure(1, weight=1, uniform="system_inputs")
        content.columnconfigure(3, weight=1, uniform="system_inputs")
        fields = [
            ("coordinate", self.coordinate_var, False),
            ("system_name", self.system_name_var, False),
            ("system_seed", self.system_seed_var, True),
            ("star_type", self.star_type_var, False),
            ("description", self.system_description_var, False),
            ("star_image", self.star_image_var, False),
            ("star_twinkle_frames", self.star_twinkle_frames_var, False),
            ("star_twinkle_background", self.star_twinkle_background_var, False),
            ("image_scale", self.image_scale_var, False),
            ("magnitude", self.magnitude_var, False),
            ("space_threat", self.space_threat_var, False),
            ("brightness_min", self.brightness_min_var, False),
            ("brightness_max", self.brightness_max_var, False),
        ]
        self.system_field_labels: dict[str, ttk.Label] = {}
        self.system_field_widgets: dict[str, tk.Widget] = {}
        for index, (key, variable, readonly) in enumerate(fields):
            row = index // 2
            column = (index % 2) * 2
            label = ttk.Label(content, wraplength=180)
            label.grid(row=row, column=column, sticky="w", pady=6, padx=(0, 8))
            if key == "star_type":
                entry = ttk.Combobox(
                    content,
                    textvariable=variable,
                    values=self.available_star_types,
                    state="readonly",
                    width=1,
                )
            elif key == "space_threat":
                entry = ttk.Combobox(
                    content,
                    textvariable=variable,
                    values=[str(value) for value in range(1, 7)],
                    state="readonly",
                    width=1,
                )
            else:
                entry = ttk.Entry(
                    content,
                    textvariable=variable,
                    state="readonly" if readonly else "normal",
                    width=1,
                )
            entry.grid(row=row, column=column + 1, sticky="ew", pady=6, padx=(0, 20))
            self.system_field_labels[key] = label
            self.system_field_widgets[key] = entry

        self.rebuild_names_button = ttk.Button(
            content, command=self.rebuild_body_names
        )
        self.rebuild_names_button.grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(18, 5)
        )
        self.mpi_label = ttk.Label(
            content, textvariable=self.mpi_var, style="Hint.TLabel", wraplength=620
        )
        self.mpi_label.grid(row=8, column=0, columnspan=4, sticky="w", pady=(5, 5))

        def wrap_system_labels(event: Any) -> None:
            for label in self.system_field_labels.values():
                label.configure(wraplength=max(96, min(180, event.width // 5)))
            self.mpi_label.configure(wraplength=max(200, event.width - 20))

        self.system_scroll_canvas.bind("<Configure>", wrap_system_labels, add="+")
        bind_vertical_page_wheel(content, self.system_scroll_canvas)

    def build_bodies_tab(self) -> None:
        self.bodies_tab.columnconfigure(0, weight=3)
        self.bodies_tab.columnconfigure(1, weight=2)
        self.bodies_tab.rowconfigure(2, weight=1)

        creator = ttk.Frame(self.bodies_tab)
        creator.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        creator.columnconfigure(1, weight=1)
        creator.columnconfigure(3, weight=1)
        self.new_body_type_label = ttk.Label(creator)
        self.new_body_type_label.grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.new_body_type_combo = ttk.Combobox(
            creator, textvariable=self.new_body_type_var, state="readonly"
        )
        self.new_body_type_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.new_body_type_combo.bind(
            "<<ComboboxSelected>>", self.new_body_type_changed
        )
        self.new_body_size_label = ttk.Label(creator)
        self.new_body_size_label.grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.new_body_size_combo = ttk.Combobox(
            creator,
            textvariable=self.new_body_size_var,
            values=self.available_planet_sizes,
            state="readonly",
            width=12,
        )
        self.new_body_size_combo.grid(row=0, column=3, sticky="ew", padx=(0, 12))
        self.add_planet_button = ttk.Button(
            creator, command=lambda: self.add_new_body(False)
        )
        self.add_planet_button.grid(row=0, column=4, padx=(0, 5))
        self.add_moon_button = ttk.Button(
            creator, command=lambda: self.add_new_body(True)
        )
        self.add_moon_button.grid(row=0, column=5)

        body_header = ttk.Frame(self.bodies_tab)
        body_header.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.body_list_label = ttk.Label(body_header)
        self.body_list_label.pack(side="left")
        controls = ttk.Frame(body_header)
        controls.pack(side="right")
        self.apply_body_button = ttk.Button(controls, command=self.apply_body_fields)
        self.apply_body_button.pack(side="left", padx=(0, 5))
        self.delete_body_button = ttk.Button(controls, command=self.delete_current_body)
        self.delete_body_button.pack(side="left", padx=(5, 0))
        columns = ("kind", "orbit", "name", "worldType", "biome")
        self.body_tree = ttk.Treeview(
            self.bodies_tab, columns=columns, show="headings", selectmode="browse"
        )
        widths = {"kind": 70, "orbit": 65, "name": 250, "worldType": 100,
                  "biome": 120}
        for column in columns:
            self.body_tree.heading(column, text=column)
            self.body_tree.column(column, width=widths[column], stretch=column == "name")
        self.body_tree.grid(row=2, column=0, sticky="nsew", padx=(0, 10))
        tree_scroll = ttk.Scrollbar(
            self.bodies_tab, orient="vertical", command=self.body_tree.yview
        )
        tree_scroll.grid(row=2, column=0, sticky="nse", padx=(0, 10))
        self.body_tree.configure(yscrollcommand=tree_scroll.set)
        self.body_tree.bind("<<TreeviewSelect>>", self.body_selected)

        details_host = ttk.Frame(self.bodies_tab)
        details_host.grid(row=2, column=1, sticky="nsew")
        details_host.columnconfigure(0, weight=1)
        details_host.rowconfigure(0, weight=1)
        self.body_details_canvas = tk.Canvas(
            details_host, highlightthickness=0, borderwidth=0, height=330
        )
        self.body_details_canvas.grid(row=0, column=0, sticky="nsew")
        details_scroll = ttk.Scrollbar(
            details_host, orient="vertical", command=self.body_details_canvas.yview
        )
        details_scroll.grid(row=0, column=1, sticky="ns")
        self.body_details_canvas.configure(yscrollcommand=details_scroll.set)
        details = ttk.Frame(self.body_details_canvas)
        details_window = self.body_details_canvas.create_window(
            (0, 0), window=details, anchor="nw"
        )
        details.bind(
            "<Configure>",
            lambda _event: self.body_details_canvas.configure(
                scrollregion=self.body_details_canvas.bbox("all")
            ),
        )
        self.body_details_canvas.bind(
            "<Configure>",
            lambda event: self.body_details_canvas.itemconfigure(
                details_window, width=event.width
            ),
        )
        self.body_details_canvas.bind(
            "<Configure>",
            lambda event: self.body_hint_label.configure(wraplength=max(240, event.width - 16)),
            add="+",
        )
        # Keep the descriptive column compact so that long translated labels do
        # not consume the space needed by paths and other editable values.
        details.columnconfigure(0, weight=0, minsize=135)
        details.columnconfigure(1, weight=1, minsize=190)
        body_fields = [
            ("kind", self.body_kind_var, True),
            ("planet_orbit", self.planet_orbit_var, False),
            ("moon_orbit", self.moon_orbit_var, False),
            ("body_name", self.body_name_var, False),
            ("body_label", self.body_label_var, False),
            ("description", self.body_description_var, False),
            ("small_image", self.small_image_var, False),
            ("small_image_scale", self.small_image_scale_var, False),
            ("large_image", self.large_image_var, False),
            ("large_image_scale", self.large_image_scale_var, False),
            ("world_hue_shift", self.world_hue_shift_var, False),
            ("global_directives", self.global_directives_var, False),
            ("world_type", self.world_type_var, True),
            ("world_size", self.world_size_var, True),
            ("world_width", self.world_width_var, True),
            ("world_height", self.world_height_var, True),
            ("system_map_size", self.system_map_size_var, True),
            ("gravity", self.gravity_var, False),
            ("terrestrial_type", self.terrestrial_type_var, True),
            ("primary_biome", self.primary_biome_var, True),
            ("body_seed", self.body_seed_var, True),
            ("world_file", self.world_file_var, True),
        ]
        self.body_field_labels: dict[str, ttk.Label] = {}
        self.body_entries: dict[str, tk.Widget] = {}
        for row, (key, variable, readonly) in enumerate(body_fields):
            if key in RESET_BODY_FIELDS:
                caption = ttk.Frame(details)
                caption.grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
                label = ttk.Label(caption, wraplength=122, justify="left", foreground="#c62828")
                label.pack(side="left")
                ttk.Label(caption, text="*", foreground="#c62828", font=("Segoe UI", 9)).pack(
                    side="left", anchor="n", padx=(2, 0))
            else:
                label = ttk.Label(details, wraplength=135, justify="left")
                label.grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            if key == "planet_orbit":
                entry = ttk.Combobox(
                    details,
                    textvariable=variable,
                    values=[str(value) for value in range(1, 12)],
                    state="readonly",
                )
            elif key == "moon_orbit":
                entry = ttk.Combobox(
                    details,
                    textvariable=variable,
                    values=[str(value) for value in range(1, 4)],
                    state="disabled",
                )
            elif key == "body_label":
                entry = ttk.Combobox(
                    details,
                    textvariable=variable,
                    values=self.available_terrestrial_graphics,
                    state="readonly",
                )
            else:
                entry = ttk.Entry(
                    details,
                    textvariable=variable,
                    state="readonly" if readonly else "normal",
                )
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            self.body_field_labels[key] = label
            self.body_entries[key] = entry

        self.body_hint_label = ttk.Label(
            details, style="Hint.TLabel", wraplength=360
        )
        self.body_hint_label.grid(row=len(body_fields), column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.body_reset_legend = ttk.Label(self.bodies_tab, foreground="#c62828", wraplength=1080)
        self.body_reset_legend.grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))
        bind_vertical_page_wheel(details, self.body_details_canvas)

    def build_world_parameters_tab(self) -> None:
        content, self.world_parameters_canvas, self.world_parameters_scrollbar = build_vertical_scroll_area(
            self.world_parameters_tab
        )
        self.world_parameters_content = content
        # The three JSON value columns resize with the page.  A shared uniform
        # group keeps the middle field exactly twice the width of either side.
        content.columnconfigure(
            1, weight=1, uniform="skyValue"
        )
        content.columnconfigure(
            3, weight=2, uniform="skyValue"
        )
        content.columnconfigure(
            5, weight=1, uniform="skyValue"
        )
        self.world_parameter_labels: dict[str, ttk.Label] = {}

        for row, (key, variable) in enumerate(
            (
                ("world_size_edit", self.world_size_var),
                ("world_width", self.world_width_var),
                ("world_height", self.world_height_var),
                ("gravity", self.gravity_var),
                ("day_length", self.day_length_var),
            )
        ):
            if key in RESET_WORLD_PARAMETER_FIELDS:
                label = build_reset_parameter_label(content, row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            else:
                label = ttk.Label(content)
                label.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            if key == "world_size_edit":
                widget = ttk.Combobox(
                    content,
                    textvariable=variable,
                    values=self.available_planet_sizes,
                    state="readonly",
                )
                self.world_size_edit_combo = widget
            else:
                widget = ttk.Entry(content, textvariable=variable)
                if key == "world_width":
                    self.world_width_entry = widget
                elif key == "world_height":
                    self.world_height_entry = widget
            widget.grid(row=row, column=1, sticky="ew", padx=(0, 18), pady=4)
            self.world_parameter_labels[key] = label

        self.weather_pool_label = ttk.Label(content)
        self.weather_pool_label.grid(row=0, column=2, sticky="nw", padx=(0, 8), pady=4)
        self.weather_pool_text = tk.Text(content, height=5, width=16, wrap="word")
        self.weather_pool_text.grid(row=0, column=3, rowspan=5, sticky="nsew", pady=4)

        separator = ttk.Separator(content, orient="horizontal")
        separator.grid(row=5, column=0, columnspan=6, sticky="ew", pady=8)
        self.sky_colors_label = ttk.Label(content)
        self.sky_colors_label.grid(row=6, column=0, columnspan=6, sticky="w", pady=(0, 4))
        self.sky_color_labels: dict[str, ttk.Label] = {}
        for index, key in enumerate(SKY_COLOR_KEYS):
            row = 7 + index // 3
            column = (index % 3) * 2
            label = ttk.Label(content, text=key)
            label.grid(row=row, column=column, sticky="w", padx=(0, 5), pady=3)
            ttk.Entry(
                content, textvariable=self.sky_color_vars[key], width=8
            ).grid(row=row, column=column + 1, sticky="ew", padx=(0, 14), pady=3)
            self.sky_color_labels[key] = label

        layer_row = 10
        self.layer_boundary_label = ttk.Label(content)
        self.layer_boundary_label.grid(row=layer_row, column=0, sticky="w", pady=(12, 4))
        self.world_layer_combo = ttk.Combobox(
            content,
            textvariable=self.world_layer_var,
            state="readonly",
        )
        self.world_layer_combo.grid(row=layer_row, column=1, sticky="ew", padx=(0, 18), pady=(12, 4))
        self.world_layer_combo.bind("<<ComboboxSelected>>", self.world_layer_changed)
        self.layer_min_height_label = build_reset_parameter_label(
            content, row=layer_row, column=2, sticky="w", padx=(0, 8), pady=(12, 4))
        self.layer_min_height_entry = ttk.Entry(
            content, textvariable=self.layer_min_height_var
        )
        self.layer_min_height_entry.grid(
            row=layer_row, column=3, sticky="ew", pady=(12, 4)
        )
        self.layer_base_height_label = build_reset_parameter_label(
            content, row=layer_row + 1, column=2, sticky="w", padx=(0, 8), pady=4)
        self.layer_base_height_entry = ttk.Entry(
            content, textvariable=self.layer_base_height_var
        )
        self.layer_base_height_entry.grid(
            row=layer_row + 1, column=3, sticky="ew", pady=4
        )
        self.layer_dungeons_label = build_reset_parameter_label(
            content, wraplength=157, row=0, column=4, sticky="w", padx=(14, 8), pady=4
        )
        self.layer_dungeons_entry = DungeonListSelector(
            content, textvariable=self.layer_dungeons_var
        )
        self.layer_dungeons_entry.grid(
            row=0, column=5, sticky="ew", pady=4
        )
        self.layer_dungeon_variance_label = build_reset_parameter_label(
            content, wraplength=157, row=1, column=4, sticky="w", padx=(14, 8), pady=4
        )
        self.layer_dungeon_variance_entry = ttk.Entry(
            content,
            textvariable=self.layer_dungeon_variance_var,
        )
        self.layer_dungeon_variance_entry.grid(
            row=1, column=5, sticky="ew", pady=4
        )
        self.layer_display_dungeons_label = ttk.Label(content, wraplength=170)
        self.layer_display_dungeons_label.grid(
            row=2, column=4, sticky="w", padx=(14, 8), pady=4
        )
        self.layer_display_dungeons_entry = DungeonListSelector(
            content,
            textvariable=self.layer_display_dungeons_var,
            state="disabled",
        )
        self.layer_display_dungeons_entry.grid(
            row=2, column=5, sticky="ew", pady=4
        )
        self.layer_dungeons_entry.before_open = self._prepare_dungeon_dropdown
        self.layer_display_dungeons_entry.before_open = self._prepare_dungeon_dropdown
        self.apply_world_parameters_button = ttk.Button(
            content, command=self.apply_world_parameters
        )
        self.apply_world_parameters_button.grid(
            row=layer_row + 2, column=0, columnspan=6, sticky="w", pady=(8, 4)
        )
        self.world_parameters_hint_label = ttk.Label(
            content, style="Hint.TLabel", wraplength=1020
        )
        self.world_parameters_hint_label.grid(
            row=layer_row + 3, column=0, columnspan=6, sticky="w", pady=(12, 0)
        )
        self.world_parameters_canvas.bind(
            "<Configure>",
            lambda event: self.world_parameters_hint_label.configure(wraplength=max(200, event.width - 12)),
            add="+",
        )
        bind_vertical_page_wheel(content, self.world_parameters_canvas)
        self.world_parameters_reset_legend = build_reset_legend(self.world_parameters_tab)

    def build_biomes_tab(self) -> None:
        content, self.biomes_scroll_canvas, self.biomes_page_scrollbar = build_vertical_scroll_area(self.biomes_tab)
        self.biomes_scroll_content = content
        content.columnconfigure(1, weight=1)
        self.biome_layer_label = ttk.Label(content)
        self.biome_layer_label.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
        self.biome_layer_combo = ttk.Combobox(
            content, textvariable=self.biome_layer_var, state="readonly"
        )
        self.biome_layer_combo.grid(row=0, column=1, sticky="ew", pady=5)
        self.biome_layer_combo.bind("<<ComboboxSelected>>", self.biome_context_changed)
        self.biome_group_label = ttk.Label(content)
        self.biome_group_label.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=5)
        self.biome_group_combo = ttk.Combobox(content, state="readonly")
        self.biome_group_combo.grid(row=1, column=1, sticky="ew", pady=5)
        self.biome_group_combo.bind("<<ComboboxSelected>>", self.biome_context_changed)

        ranges = ttk.Frame(content)
        ranges.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(3, 5))
        ranges.columnconfigure(1, weight=1)
        ranges.columnconfigure(3, weight=1)
        self.secondary_region_range_label = build_reset_parameter_label(
            ranges, row=0, column=0, sticky="w", padx=(0, 6))
        secondary_pair = ttk.Frame(ranges)
        secondary_pair.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        secondary_pair.columnconfigure(0, weight=1)
        secondary_pair.columnconfigure(2, weight=1)
        ttk.Entry(secondary_pair, textvariable=self.secondary_region_min_var, width=8).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(secondary_pair, text="–").grid(row=0, column=1, padx=4)
        ttk.Entry(secondary_pair, textvariable=self.secondary_region_max_var, width=8).grid(
            row=0, column=2, sticky="ew"
        )
        self.subregion_range_label = build_reset_parameter_label(
            ranges, row=0, column=2, sticky="w", padx=(0, 6))
        sub_pair = ttk.Frame(ranges)
        sub_pair.grid(row=0, column=3, sticky="ew")
        sub_pair.columnconfigure(0, weight=1)
        sub_pair.columnconfigure(2, weight=1)
        ttk.Entry(sub_pair, textvariable=self.subregion_min_var, width=8).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(sub_pair, text="–").grid(row=0, column=1, padx=4)
        ttk.Entry(sub_pair, textvariable=self.subregion_max_var, width=8).grid(
            row=0, column=2, sticky="ew"
        )

        list_frame = ttk.Frame(content)
        list_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(6, 8))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(1, weight=1)
        self.biome_order_label = build_reset_parameter_label(
            list_frame, row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.biome_list = tk.Listbox(list_frame, exportselection=False, height=4)
        self.biome_list.grid(row=1, column=0, sticky="nsew")
        self.biome_list.bind("<<ListboxSelect>>", self.biome_list_selected)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.biome_list.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.biome_list.configure(yscrollcommand=scroll.set)

        edit = ttk.Frame(content)
        edit.grid(row=4, column=0, columnspan=2, sticky="ew")
        edit.columnconfigure(1, weight=1)
        self.biome_name_label = build_reset_parameter_label(
            edit, row=0, column=0, sticky="w", padx=(0, 8))
        self.biome_name_combo = ttk.Combobox(
            edit, textvariable=self.biome_name_var, state="readonly"
        )
        self.biome_name_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.add_biome_button = ttk.Button(edit, command=self.add_biome)
        self.add_biome_button.grid(row=0, column=2, padx=3)
        self.remove_biome_button = ttk.Button(edit, command=self.remove_biome)
        self.remove_biome_button.grid(row=0, column=3, padx=3)
        self.move_up_button = ttk.Button(edit, command=lambda: self.move_biome(-1))
        self.move_up_button.grid(row=1, column=2, padx=3, pady=(6, 0))
        self.move_down_button = ttk.Button(edit, command=lambda: self.move_biome(1))
        self.move_down_button.grid(row=1, column=3, padx=3, pady=(6, 0))
        self.apply_biomes_button = ttk.Button(edit, command=self.apply_biome_order)
        self.apply_biomes_button.grid(row=1, column=1, sticky="e", padx=(0, 8), pady=(6, 0))
        self.edit_species_button = ttk.Button(edit, command=self.edit_biome_species)
        self.edit_species_button.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.ocean_floor_settings_button = ttk.Button(
            edit, command=self.edit_ocean_floor_pair
        )
        self.ocean_floor_settings_button.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self.biome_hint_label = ttk.Label(
            content, style="Hint.TLabel", wraplength=1120
        )
        self.biome_hint_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.range_hint_label = ttk.Label(
            content, style="Hint.TLabel", wraplength=1120
        )
        self.range_hint_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        def resize_hints(event: Any) -> None:
            for label in (self.biome_hint_label, self.range_hint_label):
                label.configure(wraplength=max(240, event.width - 12))
        self.biomes_scroll_canvas.bind("<Configure>", resize_hints, add="+")
        bind_vertical_page_wheel(content, self.biomes_scroll_canvas)
        self.biomes_reset_legend = build_reset_legend(self.biomes_tab)

    def build_system_objects_tab(self) -> None:
        content, self.system_objects_scroll_canvas, self.system_objects_page_scrollbar = build_vertical_scroll_area(self.system_objects_tab)
        self.system_objects_scroll_content = content
        content.columnconfigure(0, weight=1)
        self.station_list_label = ttk.Label(content)
        self.station_list_label.grid(row=0, column=0, sticky="w", pady=(0, 6))
        columns = ("type", "target", "uuid")
        self.station_tree = ttk.Treeview(
            content,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        for column, width in (("type", 260), ("target", 240), ("uuid", 360)):
            self.station_tree.heading(column, text=column)
            self.station_tree.column(column, width=width, stretch=column == "uuid")
        self.station_tree.grid(row=1, column=0, sticky="nsew")
        self.station_tree.bind("<<TreeviewSelect>>", self.station_selected)
        station_scroll = ttk.Scrollbar(
            content,
            orient="vertical",
            command=self.station_tree.yview,
        )
        station_scroll.grid(row=1, column=0, sticky="nse")
        self.station_tree.configure(yscrollcommand=station_scroll.set)

        controls = ttk.Frame(content)
        controls.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)
        self.station_type_label = ttk.Label(controls)
        self.station_type_label.grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.station_type_combo = ttk.Combobox(
            controls, textvariable=self.station_type_var, state="readonly"
        )
        self.station_type_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.station_target_label = ttk.Label(controls)
        self.station_target_label.grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.station_target_combo = ttk.Combobox(
            controls, textvariable=self.station_target_var, state="readonly"
        )
        self.station_target_combo.grid(row=0, column=3, sticky="ew", padx=(0, 12))
        self.add_station_button = ttk.Button(controls, command=self.add_station)
        self.add_station_button.grid(row=0, column=4, padx=(0, 5))
        self.remove_station_button = ttk.Button(
            controls, command=self.remove_selected_system_object
        )
        self.remove_station_button.grid(row=0, column=5)
        self.station_radius_label = ttk.Label(controls)
        self.station_radius_label.grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(7, 0))
        ttk.Entry(controls, textvariable=self.station_radius_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 12), pady=(7, 0)
        )
        self.station_angle_label = ttk.Label(controls)
        self.station_angle_label.grid(row=1, column=2, sticky="w", padx=(0, 6), pady=(7, 0))
        ttk.Entry(controls, textvariable=self.station_angle_var).grid(
            row=1, column=3, sticky="ew", padx=(0, 12), pady=(7, 0)
        )
        self.station_direction_label = ttk.Label(controls)
        self.station_direction_label.grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(7, 0))
        self.station_direction_combo = ttk.Combobox(
            controls,
            textvariable=self.station_direction_var,
            values=("1", "-1"),
            state="readonly",
        )
        self.station_direction_combo.grid(
            row=2, column=1, sticky="ew", padx=(0, 12), pady=(7, 0)
        )
        self.apply_station_button = ttk.Button(
            controls, command=self.apply_station_fields
        )
        self.apply_station_button.grid(row=2, column=4, columnspan=2, sticky="ew", pady=(7, 0))
        self.station_parameters_label = ttk.Label(controls)
        self.station_parameters_label.grid(
            row=3, column=0, sticky="nw", padx=(0, 6), pady=(8, 0)
        )
        self.station_parameters_text = tk.Text(controls, height=5, wrap="word")
        self.station_parameters_text.grid(
            row=3, column=1, columnspan=5, sticky="nsew", pady=(8, 0)
        )
        self.station_parameters_hint_label = ttk.Label(
            controls, style="Hint.TLabel", wraplength=960
        )
        self.station_parameters_hint_label.grid(
            row=4, column=0, columnspan=6, sticky="w", pady=(5, 0)
        )
        self.station_hint_label = ttk.Label(
            content, style="Hint.TLabel", wraplength=1120
        )
        self.station_hint_label.grid(row=3, column=0, sticky="w", pady=(10, 0))
        def resize_hints(event: Any) -> None:
            for label in (self.station_hint_label, self.station_parameters_hint_label):
                label.configure(wraplength=max(240, event.width - 12))
        self.system_objects_scroll_canvas.bind("<Configure>", resize_hints, add="+")
        bind_vertical_page_wheel(content, self.system_objects_scroll_canvas)

    def build_regions_tab(self) -> None:
        content, self.region_scroll_canvas, self.region_page_scrollbar = build_vertical_scroll_area(
            self.regions_tab
        )
        self.region_scroll_content = content
        content.columnconfigure(0, weight=1)
        operation = ttk.Frame(content)
        operation.grid(row=0, column=0, sticky="ew")
        operation.columnconfigure(1, weight=1)
        self.region_operation_label = ttk.Label(operation)
        self.region_operation_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.region_operation_combo = ttk.Combobox(
            operation, textvariable=self.region_operation_var, state="readonly")
        self.region_operation_combo.grid(row=0, column=1, sticky="ew")
        self.region_operation_combo.bind("<<ComboboxSelected>>", lambda _event: self.region_mode_changed())

        form = ttk.LabelFrame(content)
        self.region_definition_frame = form
        form.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for column in (1, 4):
            form.columnconfigure(column, weight=1)
        self.region_field_labels: dict[str, ttk.Label] = {}
        coordinate_fields = (
            ("region_x_start", self.region_x_start_var, self.region_round_x_start_var),
            ("region_x_end", self.region_x_end_var, self.region_round_x_end_var),
            ("region_y_start", self.region_y_start_var, self.region_round_y_start_var),
            ("region_y_end", self.region_y_end_var, self.region_round_y_end_var),
        )
        self.region_biome_label = ttk.Label(form)
        self.region_biome_label.grid(row=0, column=0, sticky="w", padx=(8, 5), pady=4)
        self.region_field_labels["region_biome"] = self.region_biome_label
        self.region_biome_combo = ttk.Combobox(
            form, textvariable=self.region_biome_var, state="normal"
        )
        self.region_biome_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self.region_biome_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.region_mode_changed()
        )
        self.region_biome_combo.bind("<FocusOut>", lambda _event: self.region_mode_changed())
        self.region_biome_combo.bind("<Return>", lambda _event: self.region_mode_changed())
        self.region_parallax_only_check = ttk.Checkbutton(
            form,
            variable=self.region_parallax_only_var,
            command=self.region_mode_changed,
        )
        self.region_parallax_only_check.grid(
            row=0, column=3, columnspan=2, sticky="w", padx=(12, 8), pady=4
        )
        self.region_round_combos: dict[str, ttk.Combobox] = {}
        for index, (key, variable, round_variable) in enumerate(coordinate_fields):
            row, side = divmod(index, 2)
            row += 1
            column = side * 3
            label = ttk.Label(form)
            label.grid(row=row, column=column, sticky="w", padx=(8, 5), pady=4)
            widget = ttk.Entry(form, textvariable=variable, width=13)
            widget.grid(row=row, column=column + 1, sticky="ew", pady=4)
            combo = ttk.Combobox(
                form,
                textvariable=round_variable,
                state="readonly",
                width=18,
            )
            combo.grid(row=row, column=column + 2, sticky="ew", padx=(5, 8), pady=4)
            variable.trace_add("write", self.schedule_region_spawn_preview)
            round_variable.trace_add("write", self.schedule_region_spawn_preview)
            self.region_round_combos[key] = combo
            self.region_field_labels[key] = label

        self.region_parallax_label = ttk.Label(form)
        self.region_parallax_label.grid(
            row=3, column=0, sticky="w", padx=(8, 5), pady=4
        )
        self.region_parallax_entry = ttk.Entry(
            form, textvariable=self.region_parallax_path_var
        )
        self.region_parallax_entry.grid(
            row=3, column=1, columnspan=4, sticky="ew", pady=4
        )
        self.region_parallax_browse = ttk.Button(
            form, command=self.browse_region_parallax
        )
        self.region_parallax_browse.grid(row=3, column=5, padx=(5, 8), pady=4)

        material = ttk.LabelFrame(content)
        self.region_material_frame = material
        material.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        material.columnconfigure(1, weight=1)
        material.columnconfigure(3, weight=1)
        self.region_material_labels: dict[str, ttk.Label] = {}
        self.region_material_labels["region_tile_mode"] = ttk.Label(material)
        self.region_material_labels["region_tile_mode"].grid(
            row=0, column=0, sticky="w", padx=(8, 5), pady=4
        )
        self.region_tile_mode_combo = ttk.Combobox(
            material, textvariable=self.region_tile_mode_var, state="readonly"
        )
        self.region_tile_mode_combo.grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=(0, 8), pady=4
        )
        self.region_tile_mode_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.region_tile_mode_changed()
        )
        self.region_manual_tile_widgets: list[tk.Widget] = []
        for column, key, variable in (
            (0, "region_source_item", self.region_source_item_var),
            (2, "region_target_item", self.region_target_item_var),
        ):
            label = ttk.Label(material)
            label.grid(row=1, column=column, sticky="w", padx=(8, 5), pady=4)
            self.region_material_labels[key] = label
            combo = ttk.Combobox(material, textvariable=variable, state="normal")
            combo.grid(row=1, column=column + 1, sticky="ew", padx=(0, 8), pady=4)
            self.region_manual_tile_widgets.extend((label, combo))
            if column == 0:
                self.region_source_item_combo = combo
            else:
                self.region_target_item_combo = combo
        self.region_material_labels["region_tile_layer"] = ttk.Label(material)
        self.region_material_labels["region_tile_layer"].grid(row=2, column=0, sticky="w", padx=(8, 5), pady=4)
        self.region_tile_layer_combo = ttk.Combobox(
            material, textvariable=self.region_tile_layer_var, state="readonly")
        self.region_tile_layer_combo.grid(row=2, column=1, sticky="ew", pady=4)
        self.region_load_items_button = ttk.Button(material, command=self.load_region_block_items)
        self.region_load_items_button.grid(row=2, column=2, columnspan=2, sticky="e", padx=8, pady=4)
        self.region_manual_tile_widgets.append(self.region_load_items_button)

        self.region_biome_material_widgets: list[tk.Widget] = []
        self.region_material_labels["region_biome_material_target"] = ttk.Label(material)
        self.region_material_labels["region_biome_material_target"].grid(
            row=1, column=0, sticky="w", padx=(8, 5), pady=4
        )
        self.region_target_biome_combo = ttk.Combobox(
            material, textvariable=self.region_target_biome_var, state="readonly"
        )
        self.region_target_biome_combo.grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=4
        )
        self.region_fuzzy_boundary_check = ttk.Checkbutton(
            material, variable=self.region_fuzzy_boundary_var,
            command=self.region_tile_mode_changed,
        )
        self.region_fuzzy_boundary_check.grid(
            row=1, column=2, columnspan=2, sticky="w", padx=(8, 8), pady=4
        )
        self.region_biome_material_widgets.extend((
            self.region_material_labels["region_biome_material_target"],
            self.region_target_biome_combo,
            self.region_fuzzy_boundary_check,
        ))
        self.region_material_hint = ttk.Label(material, style="Hint.TLabel", wraplength=1020)
        self.region_material_hint.grid(row=3, column=0, columnspan=4, sticky="ew", padx=8, pady=4)

        self.region_spawn_frame = ttk.Frame(content)
        self.region_spawn_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.region_spawn_frame.columnconfigure(0, weight=1, uniform="spawnPane")
        self.region_spawn_frame.columnconfigure(1, weight=1, uniform="spawnPane")
        left = ttk.Frame(self.region_spawn_frame)
        right = ttk.Frame(self.region_spawn_frame)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        for pane in (left, right):
            pane.columnconfigure(0, weight=1)
            pane.rowconfigure(1, weight=1)
        self.region_spawn_current_label = ttk.Label(left)
        self.region_spawn_current_label.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.region_spawn_replacement_label = ttk.Label(right)
        self.region_spawn_replacement_label.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.region_spawn_current_text = tk.Text(left, height=11, width=24, wrap="word", state="disabled")
        self.region_spawn_json_text = tk.Text(right, height=11, width=24, wrap="word", undo=True)
        for pane, editor in ((left, self.region_spawn_current_text), (right, self.region_spawn_json_text)):
            editor.grid(row=1, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(pane, orient="vertical", command=editor.yview)
            scrollbar.grid(row=1, column=1, sticky="ns")
            editor.configure(yscrollcommand=scrollbar.set)
        self.region_spawn_refresh_button = ttk.Button(left, command=self.schedule_region_spawn_preview)
        self.region_spawn_refresh_button.grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.region_spawn_hint = ttk.Label(self.region_spawn_frame, style="Hint.TLabel", wraplength=1000)
        self.region_spawn_hint.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        ocean = ttk.LabelFrame(content)
        ocean.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        for column in (1, 3):
            ocean.columnconfigure(column, weight=1)
        self.region_ocean_frame = ocean
        self.region_ocean_labels: dict[str, ttk.Label] = {}
        ocean_fields = (
            ("ocean_surface_height", self.region_ocean_liquid_var),
            ("ocean_floor_biome", self.region_floor_biome_var),
            ("ocean_floor_bottom", self.region_floor_bottom_var),
            ("ocean_floor_height", self.region_ocean_floor_var),
            ("ocean_floor_transition", self.region_floor_transition_var),
            ("ocean_coastal_slope_step", self.region_coastal_step_var),
            ("ocean_floor_ecology_height", self.region_floor_ecology_var),
        )
        self.region_ocean_widgets: list[tk.Widget] = []
        for index, (key, variable) in enumerate(ocean_fields):
            row, pair = divmod(index, 2)
            column = pair * 2
            label = ttk.Label(ocean)
            label.grid(row=row, column=column, sticky="w", padx=(8, 5), pady=4)
            if key == "ocean_floor_biome":
                widget = ttk.Combobox(ocean, textvariable=variable, state="normal")
                self.region_floor_biome_combo = widget
            else:
                widget = ttk.Entry(ocean, textvariable=variable)
            widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 8), pady=4)
            self.region_ocean_labels[key] = label
            self.region_ocean_widgets.extend((label, widget))
        checks = ttk.Frame(ocean)
        checks.grid(row=4, column=0, columnspan=4, sticky="ew", padx=8, pady=4)
        self.region_include_floor_check = ttk.Checkbutton(
            checks,
            variable=self.region_include_floor_var,
            command=self.region_mode_changed,
        )
        self.region_include_floor_check.pack(anchor="w")
        self.region_ocean_uniform_check = ttk.Checkbutton(
            checks, variable=self.region_ocean_uniform_var
        )
        self.region_ocean_uniform_check.pack(anchor="w", pady=(3, 0))
        self.region_coastal_slope_check = ttk.Checkbutton(
            checks, variable=self.region_coastal_slope_var
        )
        self.region_coastal_slope_check.pack(anchor="w", pady=(3, 0))

        buttons = ttk.Frame(content)
        buttons.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.apply_regions_button = ttk.Button(
            buttons, style="Accent.TButton", command=self.apply_region_plans
        )
        self.apply_regions_button.pack(side="right")
        self.region_hint_label = ttk.Label(
            content, style="Hint.TLabel", wraplength=1090
        )
        self.region_hint_label.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.region_scroll_canvas.bind(
            "<Configure>",
            lambda event: self.region_hint_label.configure(wraplength=max(200, event.width - 12)),
            add="+",
        )
        bind_vertical_page_wheel(content, self.region_scroll_canvas)
        self.region_mode_changed()

    def build_hue_tab(self) -> None:
        content, self.hue_scroll_canvas, self.hue_page_scrollbar = build_vertical_scroll_area(self.hue_tab)
        self.hue_scroll_content = content
        content.columnconfigure(0, weight=1)
        self.hue_title_label = ttk.Label(content, style="Title.TLabel", wraplength=1050)
        self.hue_title_label.grid(row=0, column=0, sticky="w", pady=(0, 8))
        areas = ttk.Frame(content)
        areas.grid(row=1, column=0, sticky="ew")
        areas.columnconfigure(0, weight=1)
        areas.columnconfigure(1, weight=1)
        self.hue_area_frames: dict[str, ttk.LabelFrame] = {}
        self.hue_labels: dict[tuple[str, str], ttk.Label] = {}
        for column, (kind, variables) in enumerate(
            (("reference", self.hue_reference_vars), ("target", self.hue_target_vars))
        ):
            frame = ttk.LabelFrame(areas)
            frame.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
            frame.columnconfigure(1, weight=1)
            self.hue_area_frames[kind] = frame
            for row, key in enumerate(("xStart", "xEnd", "yStart", "yEnd")):
                label = ttk.Label(frame, wraplength=150)
                label.grid(row=row, column=0, sticky="w", padx=8, pady=6)
                ttk.Entry(frame, textvariable=variables[key], width=1).grid(
                    row=row, column=1, sticky="ew", padx=(5, 8), pady=6
                )
                self.hue_labels[(kind, key)] = label
        buttons = ttk.Frame(content)
        buttons.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.hue_analyze_button = ttk.Button(
            buttons, command=self.analyze_hue_reference
        )
        self.hue_analyze_button.pack(side="left")
        self.hue_apply_button = ttk.Button(
            buttons, style="Accent.TButton", command=self.apply_hue_copy
        )
        self.hue_apply_button.pack(side="right")
        self.hue_hint_label = ttk.Label(
            content, style="Hint.TLabel", wraplength=1050
        )
        self.hue_hint_label.grid(row=3, column=0, sticky="w", pady=(12, 0))

        def wrap_hue_labels(event: Any) -> None:
            width = max(200, event.width - 12)
            self.hue_title_label.configure(wraplength=width)
            self.hue_hint_label.configure(wraplength=width)
            for label in self.hue_labels.values():
                label.configure(wraplength=max(80, min(150, event.width // 6)))

        self.hue_scroll_canvas.bind("<Configure>", wrap_hue_labels, add="+")
        bind_vertical_page_wheel(content, self.hue_scroll_canvas)

    def apply_language(self) -> None:
        super().apply_language()
        self.root.title(self.tr("window_title"))
        self.title_label.configure(text=self.tr("page_title"))
        self.subtitle_label.configure(text=self.tr("subtitle"))
        self.chunks_label.configure(text=self.tr("chunks_label"))
        self.chunks_browse.configure(text=self.tr("browse"))
        self.assets_label.configure(text=self.tr("assets_label"))
        self.assets_browse.configure(text=self.tr("browse"))
        self.load_button.configure(text=self.tr("load_database"))
        self.search_label.configure(text=self.tr("search_label"))
        self.system_label.configure(text=self.tr("system_label"))
        self.notebook.tab(self.system_tab, text=self.tr("system_tab"))
        self.notebook.tab(self.bodies_tab, text=self.tr("bodies_tab"))
        self.notebook.tab(
            self.world_parameters_tab, text=self.tr("world_parameters_tab")
        )
        self.notebook.tab(self.biomes_tab, text=self.tr("biomes_tab"))
        self.notebook.tab(
            self.system_objects_tab, text=self.tr("system_objects_tab")
        )
        self.notebook.tab(self.regions_tab, text=self.tr("regions_tab"))
        self.notebook.tab(self.hue_tab, text=self.tr("hue_copy_tab"))
        for key, label in self.system_field_labels.items():
            label.configure(text=self.tr(key))
        for key, label in self.body_field_labels.items():
            label.configure(text=self.tr(key),
                            foreground="#c62828" if key in RESET_BODY_FIELDS else "")
        self.body_list_label.configure(text=self.tr("body_list"))
        headings = {
            "kind": "kind", "orbit": "orbit", "name": "name",
            "worldType": "world_type", "biome": "primary_biome",
        }
        for column, key in headings.items():
            self.body_tree.heading(column, text=self.tr(key))
        self.apply_body_button.configure(text=self.tr("apply_body"))
        current_preset = self.new_body_type_map.get(
            self.new_body_type_var.get(), "planet"
        )
        type_pairs = [
            ("planet", self.tr("planet_body")),
            ("gasgiant", self.tr("gas_giant")),
            ("asteroidfield", self.tr("asteroid_field")),
            ("ancientgateway", self.tr("ancient_gateway")),
            ("outpost", self.tr("outpost_copy")),
        ]
        self.new_body_type_map = {label: key for key, label in type_pairs}
        self.new_body_type_combo.configure(values=[label for _key, label in type_pairs])
        self.new_body_type_var.set(
            next(label for key, label in type_pairs if key == current_preset)
        )
        self.new_body_type_label.configure(text=self.tr("new_body_type"))
        self.new_body_size_label.configure(text=self.tr("new_body_size"))
        self.add_planet_button.configure(text=self.tr("add_planet"))
        self.add_moon_button.configure(text=self.tr("add_moon"))
        self.new_body_type_changed()
        self.delete_body_button.configure(text=self.tr("delete_body"))
        self.rebuild_names_button.configure(text=self.tr("rebuild_names"))
        self.body_hint_label.configure(text=self.tr("body_hint"))
        self.body_reset_legend.configure(text=self.tr("body_reset_legend"))
        self.world_parameters_reset_legend.configure(text=self.tr("body_reset_legend"))
        self.biomes_reset_legend.configure(text=self.tr("body_reset_legend"))
        self.biome_layer_label.configure(text=self.tr("biome_layer"))
        self.biome_group_label.configure(text=self.tr("biome_group"))
        current_group = self.current_biome_group()
        group_values = [self.tr("main_regions"), self.tr("sub_regions")]
        self.biome_group_combo.configure(values=group_values)
        self.biome_group_combo.set(
            group_values[0] if current_group == "regions" else group_values[1]
        )
        self.biome_name_label.configure(text=self.tr("biome_name"))
        self.biome_order_label.configure(text=self.tr("biome_order"))
        self.add_biome_button.configure(text=self.tr("add_biome"))
        self.remove_biome_button.configure(text=self.tr("remove_biome"))
        self.move_up_button.configure(text=self.tr("move_up"))
        self.move_down_button.configure(text=self.tr("move_down"))
        self.apply_biomes_button.configure(text=self.tr("apply_biomes"))
        self.ocean_floor_settings_button.configure(
            text=self.tr("ocean_floor_settings")
        )
        self.biome_hint_label.configure(text=self.tr("biome_hint"))
        self.secondary_region_range_label.configure(
            text=self.tr("secondary_region_range")
        )
        self.subregion_range_label.configure(text=self.tr("subregion_range"))
        self.range_hint_label.configure(text=self.tr("range_hint"))
        self.edit_species_button.configure(text=self.tr("edit_species"))
        for key, label in self.world_parameter_labels.items():
            label.configure(text=self.tr(key))
        self.weather_pool_label.configure(text=self.tr("weather_pool"))
        self.sky_colors_label.configure(text=self.tr("sky_colors"))
        self.layer_boundary_label.configure(text=self.tr("layer_boundary"))
        self.layer_min_height_label.configure(text=self.tr("layer_min_height"))
        self.layer_base_height_label.configure(text=self.tr("layer_base_height"))
        self.layer_dungeons_label.configure(text=self.tr("layer_dungeons"))
        self.layer_dungeon_variance_label.configure(
            text=self.tr("layer_dungeon_variance")
        )
        self.layer_display_dungeons_label.configure(
            text=self.tr("layer_display_dungeons")
        )
        self.apply_world_parameters_button.configure(
            text=self.tr("apply_world_parameters")
        )
        self.world_parameters_hint_label.configure(
            text=self.tr("world_parameters_hint") + " " + self.tr("mpi_layers_hint")
        )
        for selector in (self.layer_dungeons_entry, self.layer_display_dungeons_entry):
            selector.labels.update(title=self.tr("dungeon_select"), apply=self.tr("dungeon_apply"),
                                   cancel=self.tr("dungeon_cancel"), clear=self.tr("dungeon_clear"),
                                   unknown=self.tr("dungeon_unknown"))
        self.station_list_label.configure(text=self.tr("station_list"))
        self.station_type_label.configure(text=self.tr("station_type"))
        self.station_target_label.configure(text=self.tr("station_target"))
        self.station_tree.heading("type", text=self.tr("station_object"))
        self.station_tree.heading("target", text=self.tr("station_target"))
        self.station_tree.heading("uuid", text=self.tr("station_uuid"))
        self.add_station_button.configure(text=self.tr("add_station"))
        self.remove_station_button.configure(text=self.tr("remove_station"))
        self.station_radius_label.configure(text=self.tr("station_radius"))
        self.station_angle_label.configure(text=self.tr("station_angle"))
        self.station_direction_label.configure(text=self.tr("station_direction"))
        self.apply_station_button.configure(text=self.tr("apply_station"))
        self.station_hint_label.configure(text=self.tr("station_hint"))
        self.station_parameters_label.configure(text=self.tr("station_parameters"))
        self.station_parameters_hint_label.configure(
            text=self.tr("station_parameters_hint")
        )
        self._refresh_region_operation_choices()
        self.region_definition_frame.configure(text=self.tr("region_definition"))
        for key, label in self.region_field_labels.items():
            label.configure(text=self.tr(key))
        self.region_parallax_only_check.configure(text=self.tr("parallax_only"))
        self.region_parallax_label.configure(text=self.tr("custom_parallax_json"))
        self.region_parallax_browse.configure(text=self.tr("browse"))
        self.region_ocean_frame.configure(text=self.tr("region_ocean_options"))
        for key, label in self.region_ocean_labels.items():
            label.configure(text=self.tr(key))
        self.region_include_floor_check.configure(text=self.tr("include_ocean_floor"))
        self.region_ocean_uniform_check.configure(text=self.tr("ocean_disable_subregions"))
        self.region_coastal_slope_check.configure(text=self.tr("ocean_coastal_slope"))
        self._refresh_rounding_choices()
        self.apply_regions_button.configure(text=self.tr("apply_regions"))
        self.region_hint_label.configure(text=self.ocean_floor_editor_hint("region_hint"))
        self.hue_title_label.configure(text=self.tr("hue_copy_title"))
        self.hue_area_frames["reference"].configure(text=self.tr("hue_reference"))
        self.hue_area_frames["target"].configure(text=self.tr("hue_target"))
        coordinate_labels = {
            "xStart": "region_x_start", "xEnd": "region_x_end",
            "yStart": "region_y_start", "yEnd": "region_y_end",
        }
        for (_kind, key), label in self.hue_labels.items():
            label.configure(text=self.tr(coordinate_labels[key]))
        self.hue_analyze_button.configure(text=self.tr("hue_analyze"))
        self.hue_apply_button.configure(text=self.tr("hue_apply"))
        self.hue_hint_label.configure(text=self.tr("hue_hint"))
        self.action_button.configure(text=self.tr("save_system"))
        if self.editor is not None:
            self.rebuild_body_tree(self.current_body_iid)
            self.refresh_mpi_warning()
            self.refresh_biome_context()
            self.rebuild_station_tree()
        self.refresh_ocean_floor_button()
        self.region_mode_changed()

    def browse_chunks(self) -> None:
        path = self.choose_directory(self.chunks_var, self.tr("select_chunks"))
        if path:
            self.load_catalog()

    def browse_assets(self) -> None:
        if self.choose_directory(self.assets_var, self.tr("select_assets")):
            # Re-selecting the same folder is also an explicit cache refresh
            # after the user changes installed/unpacked assets.
            self.refresh_asset_sizes(force=True)

    def asset_path_entered(self, _event: Any = None) -> str:
        self.refresh_asset_sizes(force=True)
        return "break"

    def refresh_asset_sizes(self, *, force: bool = False) -> None:
        if force:
            self._dungeon_choices_context = None
        raw_assets = self.assets_var.get().strip()
        if not raw_assets or self.busy:
            # Path("") is the editor's working directory, NOT a selected Assets
            # folder. Never recursively search it as a side effect of navigation.
            return
        assets = Path(raw_assets)
        asset_key = str(assets.resolve()).casefold()
        if not force and getattr(self, "_asset_choices_path", None) == asset_key:
            return
        self._asset_choices_path = None

        def task():
            return (
                swe.celestial_planet_sizes(assets),
                swe.system_station_types(assets),
                swe.celestial_asset_biome_choices(assets),
                swe.celestial_star_types(assets),
                swe.celestial_terrestrial_graphics_types(assets),
            )

        def success(result):
            # A delayed scan must not install another folder's choices.
            current = self.assets_var.get().strip()
            if not current or str(Path(current).resolve()).casefold() != asset_key:
                return
            sizes, station_types, biome_choices, star_types, graphics_types = result
            biomes = [choice["name"] for choice in biome_choices]
            if sizes:
                self.available_planet_sizes = list(sizes)
                self.new_body_size_combo.configure(values=sizes)
                if self.new_body_size_var.get() not in sizes:
                    self.new_body_size_var.set(
                        "medium" if "medium" in sizes else sizes[0]
                    )
                self.world_size_edit_combo.configure(values=sizes)
            self.station_type_combo.configure(values=station_types)
            if station_types and self.station_type_var.get() not in station_types:
                self.station_type_var.set(station_types[0])
            self.available_biomes = list(biomes)
            self.biome_display_to_name = {
                str(choice["display"]): str(choice["name"])
                for choice in biome_choices
            }
            self.biome_name_to_display = {
                str(choice["name"]): str(choice["display"])
                for choice in biome_choices
            }
            self.biome_name_combo.configure(
                values=[choice["display"] for choice in biome_choices]
            )
            current_biome_choice = self.biome_name_var.get().strip()
            if current_biome_choice in self.biome_name_to_display:
                self.biome_name_var.set(
                    self.biome_name_to_display[current_biome_choice]
                )
            elif biome_choices and current_biome_choice not in self.biome_display_to_name:
                self.biome_name_var.set(str(biome_choices[0]["display"]))
            self.region_biome_combo.configure(values=biomes)
            self.region_target_biome_combo.configure(values=biomes)
            current_target = self.region_target_biome_var.get().strip()
            if biomes and current_target not in biomes:
                preferred = self.region_biome_var.get().strip()
                if preferred not in biomes:
                    preferred = "garden" if "garden" in biomes else biomes[0]
                self.region_target_biome_var.set(preferred)
            if biomes and self.region_biome_var.get() not in biomes:
                self.region_biome_var.set(biomes[0])
            floor_biomes = [
                name
                for name in biomes
                if (swe.ocean_biome_pair(name) or (None, None))[0]
                == name.casefold()
            ]
            self.region_floor_biome_combo.configure(values=floor_biomes)
            if floor_biomes and self.region_floor_biome_var.get() not in floor_biomes:
                self.region_floor_biome_var.set(floor_biomes[0])
            self.region_mode_changed()
            self.available_star_types = list(
                dict.fromkeys([*VANILLA_STAR_TYPES, *star_types])
            )
            star_widget = self.system_field_widgets.get("star_type")
            if isinstance(star_widget, ttk.Combobox):
                current = self.star_type_var.get().strip()
                values = list(self.available_star_types)
                if current and current not in values:
                    values.append(current)
                star_widget.configure(values=values)
            self.available_terrestrial_graphics = list(
                dict.fromkeys([*VANILLA_TERRESTRIAL_GRAPHICS, *graphics_types])
            )
            appearance_widget = self.body_entries.get("body_label")
            if isinstance(appearance_widget, ttk.Combobox):
                current = self.body_label_var.get().strip()
                values = list(self.available_terrestrial_graphics)
                if current and current not in values:
                    values.append(current)
                appearance_widget.configure(values=values)
            self._asset_choices_path = asset_key
            self.set_status_key("assets_loaded", assets=assets, biomes=len(biomes))

        self.run_worker(task, success, "loading_assets")

    def new_body_type_changed(self, _event: Any = None) -> None:
        preset = self.new_body_type_map.get(self.new_body_type_var.get(), "planet")
        self.new_body_size_combo.configure(
            state="readonly" if preset == "planet" else "disabled"
        )
        self.add_moon_button.configure(
            state=(
                "normal"
                if swe.celestial_body_preset_supports_satellite(preset)
                else "disabled"
            )
        )
        if preset == "outpost":
            self.set_status_key("outpost_note")

    def chunks_path(self) -> Path:
        return Path(self.chunks_var.get().strip()) / "universe.chunks"

    @staticmethod
    def _audit_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {
                str(key): CelestialSystemEditorWindow._audit_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [CelestialSystemEditorWindow._audit_safe(item) for item in value]
        return str(value)

    def operation_log_path(self, universe_folder: Path | None = None) -> Path:
        if universe_folder is None:
            universe_folder = Path(self.chunks_var.get().strip())
        return Path(universe_folder) / TOOL_05_LOG_FILENAME

    def audit_log(
        self,
        event: str,
        *,
        universe_folder: Path | None = None,
        **details: Any,
    ) -> Path | None:
        """Append one durable, machine-readable Tool 05 diagnostic event."""

        try:
            path = self.operation_log_path(universe_folder)
            if not path.parent.is_dir():
                return None
            record = {
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "pid": os.getpid(),
                "build": TOOL_05_BUILD_ID,
                "event": str(event),
                **self._audit_safe(details),
            }
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            with self._audit_lock:
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            return path
        except Exception:
            # Logging is diagnostic only.  It must never block a world edit.
            return None

    def audit_exception(
        self,
        event: str,
        exc: BaseException,
        *,
        universe_folder: Path | None = None,
        **details: Any,
    ) -> None:
        self.audit_log(
            event,
            universe_folder=universe_folder,
            errorType=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
            **details,
        )

    def open_operation_log(self) -> None:
        path = self.operation_log_path()
        if not path.is_file():
            messagebox.showinfo(
                self.tr("open_operation_log"),
                self.tr("no_operation_log"),
                parent=self.root,
            )
            return
        os.startfile(path)

    def load_catalog(self) -> None:
        universe = Path(self.chunks_var.get().strip())
        chunks = universe / "universe.chunks"
        if not universe.is_dir() or not chunks.is_file():
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_chunks"), parent=self.root
            )
            return
        self.cancel_pending_ocean_monitor()

        def task():
            return swe.list_generated_celestial_systems(universe)

        def success(catalog):
            self.catalog = catalog
            self.filter_catalog()
            self.system_var.set("")
            self.editor = None
            self.original_editor = None
            self.system_file_state = None
            self.original_system_file_state = None
            self.body_origins = {}
            self.biome_pending = {}
            self.ordered_parallax_applied = {}
            self.world_layer_pending = {}
            self.ocean_pair_settings = {}
            self.ocean_pair_pending = {}
            self.ocean_pair_store = {"version": 1, "bodies": {}}
            self.loaded_biome_key = None
            self.loaded_world_layer_key = None
            self.region_plan_world = None
            self.rebuild_body_tree()
            self.rebuild_station_tree()
            self.set_status_key("catalog_loaded", systems=len(catalog), chunks=universe)

        self.run_worker(task, success, "loading_database")

    def filter_catalog(self) -> None:
        query = self.search_var.get().strip().casefold()
        labels: list[str] = []
        self.catalog_by_label = {}
        for item in self.catalog:
            coordinate = ":".join(str(value) for value in item["location"])
            haystack = f'{item["name"]} {coordinate} {item["typeName"]}'.casefold()
            if query and query not in haystack:
                continue
            label = (
                f'{item["name"]}  [{coordinate}]  '
                f'({item["planetCount"]}/{item["satelliteCount"]})'
            )
            suffix = 2
            original = label
            while label in self.catalog_by_label:
                label = f"{original} #{suffix}"
                suffix += 1
            labels.append(label)
            self.catalog_by_label[label] = item["location"]
        self.system_combo.configure(values=labels)
        if self.system_var.get() not in self.catalog_by_label:
            self.system_var.set("")

    @staticmethod
    def capture_editor_dungeon_pools(
        editor: dict[str, Any]
    ) -> dict[tuple[int, int, str], list[str]]:
        """Capture the Celestial lists used by star-map information mods."""

        result: dict[tuple[int, int, str], list[str]] = {}
        for planet_entry in editor.get("planets", []):
            planet_orbit = int(planet_entry[0])
            bodies = [(0, planet_entry[1]["parameters"])] + [
                (int(moon[0]), moon[1])
                for moon in (planet_entry[1].get("satellites") or [])
            ]
            for moon_orbit, parameters in bodies:
                for layer in swe.celestial_layer_settings(parameters):
                    result[(planet_orbit, moon_orbit, str(layer["layerId"]))] = list(
                        layer.get("dungeons", [])
                    )
        return result

    def system_selected(self, _event: Any = None) -> None:
        location = self.catalog_by_label.get(self.system_var.get())
        chunks = self.chunks_path()
        if location is None:
            return
        self.cancel_pending_ocean_monitor()
        universe_folder = chunks.parent
        system_path = Path(self.chunks_var.get().strip()) / (
            "_".join(str(value) for value in location) + ".system"
        )
        self.audit_log(
            "system_load_requested",
            universe_folder=universe_folder,
            location=location,
            systemFile=system_path,
        )

        def task():
            try:
                editor = swe.load_celestial_system(chunks, location)
                display_dungeons = self.capture_editor_dungeon_pools(editor)
                swe.hydrate_celestial_system_from_world_files(
                    editor, universe_folder
                )
                ocean_store = swe.load_ocean_pair_sidecar(universe_folder)
                self.audit_log(
                    "system_files_loaded",
                    universe_folder=universe_folder,
                    location=location,
                    oceanPairStore=ocean_store,
                )
                return (
                    editor,
                    swe.load_generated_system_file(system_path),
                    ocean_store,
                    display_dungeons,
                )
            except Exception as exc:
                self.audit_exception(
                    "system_load_failed",
                    exc,
                    universe_folder=universe_folder,
                    location=location,
                )
                raise

        def success(result):
            editor, system_state, ocean_pair_store, display_dungeons = result
            # Older Tool 05 builds could leave secondaryRegions longer than
            # secondarySubRegions.  Vanilla indexes those arrays in lockstep
            # and crashes instead of reporting a data error, so repair legacy
            # records as soon as they enter the editor.
            for planet_entry in editor.get("planets", []):
                if not (
                    isinstance(planet_entry, list)
                    and len(planet_entry) == 2
                    and isinstance(planet_entry[1], dict)
                ):
                    continue
                planet_data = planet_entry[1]
                planet_parameters = planet_data.get("parameters")
                if isinstance(planet_parameters, dict):
                    swe.repair_celestial_region_pairings(planet_parameters)
                for moon_entry in planet_data.get("satellites") or []:
                    if (
                        isinstance(moon_entry, list)
                        and len(moon_entry) == 2
                        and isinstance(moon_entry[1], dict)
                    ):
                        swe.repair_celestial_region_pairings(moon_entry[1])
            self.editor = editor
            self.original_editor = json_clone(editor)
            self.system_file_state = system_state
            self.original_system_file_state = {
                "name": system_state["name"],
                "version": system_state["version"],
                "body": json_clone(system_state["body"]),
                "trailing": system_state["trailing"],
            }
            self.biome_pending = {}
            self.ordered_parallax_applied = {}
            self.world_layer_pending = {}
            self.star_map_dungeon_text = {}
            for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
                for layer in swe.celestial_layer_settings(parameters):
                    layer_id = str(layer["layerId"])
                    shown = display_dungeons.get(
                        (planet_orbit, moon_orbit, layer_id),
                        list(layer.get("dungeons", [])),
                    )
                    self.star_map_dungeon_text[(id(parameters), layer_id)] = (
                        json.dumps(shown, ensure_ascii=False)
                    )
            self.ocean_pair_settings = {}
            self.ocean_pair_pending = {}
            self.ocean_pair_store = ocean_pair_store
            self.loaded_biome_key = None
            self.loaded_world_layer_key = None
            self.region_plan_world = None
            self.capture_body_origins()
            self.load_ocean_pair_settings()
            ocean_bodies: list[dict[str, Any]] = []
            for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
                pairs = self.serialized_ocean_pairs(parameters)
                if not pairs:
                    continue
                world = self.body_world_path(planet_orbit, moon_orbit)
                ocean_bodies.append(
                    {
                        "bodyKey": swe.ocean_pair_body_key(
                            editor["location"], planet_orbit, moon_orbit
                        ),
                        "world": str(world),
                        "worldExists": world.is_file(),
                        "worldSizeBytes": world.stat().st_size if world.is_file() else None,
                        "worldModifiedNs": world.stat().st_mtime_ns if world.is_file() else None,
                        "pending": bool(
                            self.ocean_pair_pending.get(id(parameters), False)
                        ),
                        "pairs": pairs,
                    }
                )
            self.audit_log(
                "system_loaded_into_editor",
                universe_folder=universe_folder,
                location=editor["location"],
                oceanBodies=ocean_bodies,
            )
            self.load_system_fields()
            self.rebuild_body_tree()
            self.rebuild_station_tree()
            planets = len(editor["planets"])
            moons = sum(len(entry[1].get("satellites") or []) for entry in editor["planets"])
            self.set_status_key(
                "system_loaded",
                name=editor["systemParameters"]["name"],
                planets=planets,
                moons=moons,
            )
            # Loading a saved system is read-only. Keep legacy pending pairs
            # visible for the explicit Apply pending command, but do not rewrite
            # worlds merely because a system was selected.

        self.run_worker(task, success, "loading_system")

    def capture_body_origins(self) -> None:
        self.body_origins = {}
        if self.editor is None:
            return
        for planet_entry in self.editor["planets"]:
            planet_orbit = int(planet_entry[0])
            parameters = planet_entry[1]["parameters"]
            self.body_origins[id(parameters)] = (planet_orbit, 0)
            for moon_entry in planet_entry[1].get("satellites") or []:
                self.body_origins[id(moon_entry[1])] = (
                    planet_orbit,
                    int(moon_entry[0]),
                )

    def iter_body_parameters(self):
        if self.editor is None:
            return
        for planet_entry in self.editor["planets"]:
            planet_orbit = int(planet_entry[0])
            yield planet_orbit, 0, planet_entry[1]["parameters"]
            for moon_entry in planet_entry[1].get("satellites") or []:
                yield planet_orbit, int(moon_entry[0]), moon_entry[1]

    @staticmethod
    def default_ocean_floor_setting(surface_biome: str) -> dict[str, Any] | None:
        pair = swe.ocean_biome_pair(surface_biome)
        if pair is None or pair[1] != str(surface_biome).strip().casefold():
            return None
        return {
            "surfaceBiome": pair[1],
            "floorBiome": pair[0],
            "floorBottomY": None,
            "floorHeight": None,
            "transitionY": None,
            "disableSubRegions": True,
            "coastalSlopeEnabled": True,
            "coastalSlopeStep": 32,
            "floorEcologyHeight": swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT,
            "biomeOverrides": {},
            "regionOverrides": {},
        }

    @staticmethod
    def surface_biome_names(parameters: dict[str, Any]) -> list[str]:
        try:
            return [
                str(region.get("biome", ""))
                for region in swe.celestial_biome_region_objects(
                    parameters, "surface", "regions"
                )
            ]
        except Exception:
            return []

    def ensure_ocean_pair_settings(
        self, parameters: dict[str, Any], names: list[str]
    ) -> list[dict[str, Any] | None]:
        parameter_id = id(parameters)
        current = self.ocean_pair_settings.get(parameter_id, [])
        aligned: list[dict[str, Any] | None] = []
        unused = set(range(len(current)))
        for index, name in enumerate(names):
            default = self.default_ocean_floor_setting(name)
            if default is None:
                aligned.append(None)
                continue
            chosen: dict[str, Any] | None = None
            if index < len(current):
                candidate = current[index]
                if (
                    isinstance(candidate, dict)
                    and str(candidate.get("surfaceBiome", "")).casefold()
                    == default["surfaceBiome"]
                ):
                    chosen = candidate
                    unused.discard(index)
            if chosen is None:
                for candidate_index in list(unused):
                    candidate = current[candidate_index]
                    if (
                        isinstance(candidate, dict)
                        and str(candidate.get("surfaceBiome", "")).casefold()
                        == default["surfaceBiome"]
                    ):
                        chosen = candidate
                        unused.discard(candidate_index)
                        break
            # Persist the effective values shown by both ocean editors. Older
            # releases stored e.g. ecology=1 but silently generated the minimum
            # four-tile curve band; reload/save must not keep that stale value.
            aligned.append(swe.normalize_ocean_floor_pair_settings([chosen or default])[0])
        self.ocean_pair_settings[parameter_id] = aligned
        return aligned

    def load_ocean_pair_settings(self) -> None:
        self.ocean_pair_settings = {}
        self.ocean_pair_pending = {}
        if self.editor is None:
            return
        bodies = self.ocean_pair_store.get("bodies", {})
        location = self.editor["location"]
        for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
            names = self.surface_biome_names(parameters)
            key = swe.ocean_pair_body_key(location, planet_orbit, moon_orbit)
            record = bodies.get(key, {}) if isinstance(bodies, dict) else {}
            stored = record.get("pairs", []) if isinstance(record, dict) else []
            by_index = {
                int(item.get("surfaceIndex", -1)): item
                for item in stored
                if isinstance(item, dict)
            }
            values: list[dict[str, Any] | None] = []
            for index, name in enumerate(names):
                default = self.default_ocean_floor_setting(name)
                item = by_index.get(index)
                if default is None:
                    values.append(None)
                elif isinstance(item, dict):
                    merged = {**default, **json_clone(item)}
                    merged.pop("surfaceIndex", None)
                    values.append(merged)
                else:
                    values.append(default)
            if any(value is not None for value in values):
                self.ocean_pair_settings[id(parameters)] = values
                if isinstance(record, dict) and stored:
                    self.ocean_pair_pending[id(parameters)] = bool(
                        record.get("pending", False)
                    )
                else:
                    # A sole vanilla ocean already has a paired floor. Mixed
                    # surface lists need the second-stage exact-X alignment.
                    self.ocean_pair_pending[id(parameters)] = len(names) > 1

    def serialized_ocean_pairs(
        self, parameters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        names = self.surface_biome_names(parameters)
        settings = self.ensure_ocean_pair_settings(parameters, names)
        result: list[dict[str, Any]] = []
        for index, value in enumerate(settings):
            if value is None:
                continue
            result.append({"surfaceIndex": index, **json_clone(value)})
        return result

    @staticmethod
    def display_number(value: Any) -> str:
        return "" if value is None else str(value)

    def set_world_dimension_values(self, visitable: dict[str, Any]) -> None:
        size = visitable.get("worldSize")
        if not (
            isinstance(size, list)
            and len(size) == 2
            and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in size
            )
        ):
            self.world_width_var.set("")
            self.world_height_var.set("")
            self.system_map_size_var.set("")
            self.loaded_world_dimensions = None
            return
        width, height = int(size[0]), int(size[1])
        self.world_width_var.set(str(width))
        self.world_height_var.set(str(height))
        self.loaded_world_dimensions = (width, height)
        assets_text = self.assets_var.get().strip()
        assets = Path(assets_text) if assets_text else None
        diameter = swe.celestial_system_map_size_for_width(width, assets)
        self.system_map_size_var.set(self.display_number(diameter))

    def ocean_pair_default_heights(
        self, parameters: dict[str, Any]
    ) -> tuple[int | None, float | None, int | None]:
        """Prefer generated World4 heights, then the Celestial template."""

        fallback = (
            swe.ocean_floor_pair_default_bottom(parameters),
            *swe.ocean_floor_pair_default_heights(parameters),
        )
        reference = self.selected_reference()
        if reference is None:
            return fallback
        kind, planet_entry, moon_entry = reference
        planet_orbit = int(planet_entry[0])
        moon_orbit = int(moon_entry[0]) if kind == "moon" and moon_entry else 0
        world = self.body_world_path(planet_orbit, moon_orbit)
        if not world.is_file():
            return fallback
        try:
            document = swe.load_metadata_only(world)
            world_parameters = swe.world_template(document).get("worldParameters", {})
            generated = swe.ocean_floor_pair_default_heights(world_parameters)
        except Exception:
            return fallback
        generated_bottom = swe.ocean_floor_pair_default_bottom(world_parameters)
        return (
            generated_bottom if generated_bottom is not None else fallback[0],
            generated[0] if generated[0] is not None else fallback[1],
            generated[1] if generated[1] is not None else fallback[2],
        )

    def load_system_fields(self) -> None:
        if self.editor is None:
            return
        parameters = self.editor["systemParameters"]
        raw = parameters.get("parameters") or {}
        brightness = raw.get("brightness")
        brightness = brightness if isinstance(brightness, list) else [None, None]
        self.coordinate_var.set(": ".join(str(v) for v in self.editor["location"]))
        self.system_name_var.set(str(parameters.get("name", "")))
        self.system_seed_var.set(str(parameters.get("seed", "")))
        current_star_type = str(raw.get("typeName", ""))
        self.star_type_var.set(current_star_type)
        star_widget = self.system_field_widgets.get("star_type")
        if isinstance(star_widget, ttk.Combobox):
            values = list(self.available_star_types)
            if current_star_type and current_star_type not in values:
                values.append(current_star_type)
            star_widget.configure(values=values)
        self.system_description_var.set(str(raw.get("description", "")))
        self.star_image_var.set(str(raw.get("image", "")))
        self.star_twinkle_frames_var.set(str(raw.get("twinkleFrames", "")))
        self.star_twinkle_background_var.set(str(raw.get("twinkleBackground", "")))
        self.image_scale_var.set(self.display_number(raw.get("imageScale")))
        self.magnitude_var.set(self.display_number(raw.get("magnitude")))
        self.space_threat_var.set(self.display_number(raw.get("spaceThreatLevel")))
        self.brightness_min_var.set(self.display_number(brightness[0] if brightness else None))
        self.brightness_max_var.set(self.display_number(brightness[1] if len(brightness) > 1 else None))
        self.refresh_mpi_warning()

    def apply_system_fields(self) -> None:
        from world_parameter_validation import native_float32, native_integer
        if self.editor is None:
            raise ValueError(self.tr("no_editor"))
        coordinate_parts = [
            part
            for part in re.split(r"[\s,:;/]+", self.coordinate_var.get().strip())
            if part
        ]
        if len(coordinate_parts) != 3:
            raise ValueError(self.tr("invalid_coordinate"))
        try:
            location = [int(part) for part in coordinate_parts]
            for component in location:
                native_integer(component, label=self.tr("invalid_coordinate"),
                               minimum=-0x80000000, maximum=0x7fffffff)
        except ValueError as exc:
            raise ValueError(self.tr("invalid_coordinate")) from exc
        # Native CelestialCoordinate treats the all-zero system as null and
        # rejects it before database lookup/update (StarCelestialCoordinate.cpp).
        if not any(location):
            raise ValueError(self.tr("invalid_coordinate"))
        name = self.system_name_var.get().strip()
        if not name:
            raise ValueError(self.tr("required_field", field=self.tr("system_name")))
        parameters = self.editor["systemParameters"]
        old_raw = parameters.get("parameters")
        # Stage only the editable owner. Neither unrelated body records nor
        # potentially large custom branches need a full editor deepcopy.
        raw = dict(old_raw) if isinstance(old_raw, dict) else {}
        for key, variable in (
            ("typeName", self.star_type_var),
            ("description", self.system_description_var),
            ("image", self.star_image_var),
            ("twinkleFrames", self.star_twinkle_frames_var),
            ("twinkleBackground", self.star_twinkle_background_var),
        ):
            value = variable.get().strip()
            if value:
                raw[key] = value
            elif key in {"typeName", "image", "twinkleFrames", "twinkleBackground"}:
                # Sky sun-type, central-body and twinkle readers use toString()
                # without a fallback. Blank retains, never deletes, these keys.
                if not isinstance(raw.get(key), str) or not raw[key].strip():
                    raise ValueError(f"System field {key} is required; enter a value when no valid existing value is available")
            else:
                raw.pop(key, None)
        for key, variable, label_key in (
            ("imageScale", self.image_scale_var, "image_scale"),
            ("magnitude", self.magnitude_var, "magnitude"),
            ("spaceThreatLevel", self.space_threat_var, "space_threat"),
        ):
            text = variable.get().strip()
            keep_existing = not text
            if keep_existing:
                if key not in raw:
                    raise ValueError(f"System field {key} is required; blank can only preserve an existing value")
                text = raw[key]
            try:
                if key == "spaceThreatLevel":
                    number = native_integer(text, label=self.tr(label_key), minimum=1, maximum=6)
                else:
                    number = native_float32(text, label=self.tr(label_key), positive=key == "imageScale")
            except ValueError as exc:
                if key == "spaceThreatLevel":
                    raise ValueError(self.tr("space_threat_range")) from exc
                raise ValueError(str(exc)) from exc
            if not keep_existing:
                raw[key] = int(number) if float(number).is_integer() else number
        brightness_text = [
            self.brightness_min_var.get().strip(),
            self.brightness_max_var.get().strip(),
        ]
        if any(brightness_text):
            brightness = [native_float32(value, label=self.tr(key), nonnegative=True)
                          for value, key in zip(brightness_text, ("brightness_min", "brightness_max"))]
            if brightness[0] > brightness[1]:
                raise ValueError(f"{self.tr('brightness_min')} <= {self.tr('brightness_max')}")
            raw["brightness"] = brightness
        else:
            raw.pop("brightness", None)
        self._check_image_edits(raw, old_raw or {}, kind="system")
        # No attached model or normalized field text changes before every
        # coordinate, string, number and brightness endpoint is valid.
        self.editor["location"] = location
        parameters["name"] = name
        parameters["parameters"] = raw
        self.coordinate_var.set(": ".join(str(value) for value in location))

    def body_world_path(
        self,
        planet_orbit: int,
        moon_orbit: int,
        location: list[int] | None = None,
    ) -> Path:
        if location is None:
            location = self.editor["location"] if self.editor else [0, 0, 0]
        base = "_".join(str(value) for value in location) + f"_{planet_orbit}"
        if moon_orbit:
            base += f"_{moon_orbit}"
        return Path(self.chunks_var.get().strip()) / f"{base}.world"

    def system_file_path(self, location: list[int]) -> Path:
        filename = "_".join(str(value) for value in location) + ".system"
        return Path(self.chunks_var.get().strip()) / filename

    @staticmethod
    def body_parameters(reference: tuple[str, list[Any], list[Any] | None]) -> dict[str, Any]:
        kind, planet_entry, moon_entry = reference
        if kind == "planet":
            return planet_entry[1]["parameters"]
        return moon_entry[1]

    def body_summary(self, reference: tuple[str, list[Any], list[Any] | None]) -> tuple[Any, ...]:
        kind, planet_entry, moon_entry = reference
        planet_orbit = int(planet_entry[0])
        moon_orbit = int(moon_entry[0]) if moon_entry is not None else 0
        parameters = self.body_parameters(reference)
        raw_value = parameters.get("parameters")
        raw = raw_value if isinstance(raw_value, dict) else {}
        visitable_value = parameters.get("visitableParameters")
        visitable = visitable_value if isinstance(visitable_value, dict) else {}
        biome_count = sum(
            len(group["biomes"]) for group in swe.celestial_layer_groups(parameters)
        )
        primary_biome = visitable.get("primaryBiome", visitable.get("typeName", ""))
        return (
            self.tr(kind),
            f"{planet_orbit}" + (f".{moon_orbit}" if moon_orbit else ""),
            parameters.get("name", ""),
            raw.get("worldType", ""),
            f"{primary_biome} / {biome_count}",
        )

    def rebuild_body_tree(self, select_iid: str | None = None) -> None:
        self._selecting_body = True
        try:
            for iid in self.body_tree.get_children():
                self.body_tree.delete(iid)
            self.body_refs = {}
            self.current_body_iid = None
            if self.editor is None:
                return
            for planet_entry in sorted(self.editor["planets"], key=lambda value: value[0]):
                planet_iid = f"p_{id(planet_entry)}"
                reference = ("planet", planet_entry, None)
                self.body_refs[planet_iid] = reference
                self.body_tree.insert("", "end", iid=planet_iid, values=self.body_summary(reference))
                for moon_entry in sorted(
                    planet_entry[1].get("satellites") or [], key=lambda value: value[0]
                ):
                    moon_iid = f"m_{id(moon_entry)}"
                    reference = ("moon", planet_entry, moon_entry)
                    self.body_refs[moon_iid] = reference
                    self.body_tree.insert("", "end", iid=moon_iid, values=self.body_summary(reference))
            if select_iid in self.body_refs:
                self.body_tree.selection_set(select_iid)
                self.current_body_iid = select_iid
                self.load_body_fields(self.body_refs[select_iid])
        finally:
            self._selecting_body = False

    def body_selected(self, _event: Any = None) -> None:
        if self._selecting_body:
            return
        selection = self.body_tree.selection()
        if not selection:
            return
        new_iid = selection[0]
        if self.current_body_iid and self.current_body_iid != new_iid:
            try:
                self.apply_current_body(rebuild=False, silent=True)
            except Exception as exc:
                messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
                self._selecting_body = True
                self.body_tree.selection_set(self.current_body_iid)
                self._selecting_body = False
                return
        self.current_body_iid = new_iid
        reference = self.body_refs.get(new_iid)
        if reference:
            self.load_body_fields(reference)
            self.refresh_biome_context()

    def load_body_fields(self, reference: tuple[str, list[Any], list[Any] | None]) -> None:
        kind, planet_entry, moon_entry = reference
        planet_orbit = int(planet_entry[0])
        moon_orbit = int(moon_entry[0]) if moon_entry is not None else 0
        parameters = self.body_parameters(reference)
        raw_value = parameters.get("parameters")
        raw = raw_value if isinstance(raw_value, dict) else {}
        visitable_value = parameters.get("visitableParameters")
        visitable = visitable_value if isinstance(visitable_value, dict) else {}
        terrestrial = raw.get("terrestrialType")
        if isinstance(terrestrial, list):
            terrestrial_text = ", ".join(str(value) for value in terrestrial)
        else:
            terrestrial_text = self.display_number(terrestrial)
        self.body_kind_var.set(self.tr(kind))
        self.planet_orbit_var.set(str(planet_orbit))
        self.moon_orbit_var.set(str(moon_orbit) if moon_orbit else "")
        self.body_name_var.set(str(parameters.get("name", "")))
        self.body_label_var.set(str(visitable.get("typeName", "")))
        self.body_description_var.set(str(raw.get("description", "")))
        self.small_image_var.set(str(raw.get("smallImage", "")))
        self.small_image_scale_var.set(self.display_number(raw.get("smallImageScale")))
        self.large_image_var.set(str(raw.get("image", "")))
        self.large_image_scale_var.set(self.display_number(raw.get("imageScale")))
        self.world_hue_shift_var.set(self.display_number(visitable.get("hueShift")))
        directives = visitable.get("globalDirectives")
        self.global_directives_var.set(format_global_directives(directives))
        self.world_type_var.set(str(raw.get("worldType", "")))
        self.world_size_var.set(str(raw.get("worldSize", "")))
        self.set_world_dimension_values(visitable)
        self.gravity_var.set(self.display_number(visitable.get("gravity")))
        self.terrestrial_type_var.set(terrestrial_text)
        biome_count = sum(
            len(group["biomes"]) for group in swe.celestial_layer_groups(parameters)
        )
        primary = str(visitable.get("primaryBiome", visitable.get("typeName", "")))
        self.primary_biome_var.set(f"{primary} / {biome_count}")
        self.body_seed_var.set(str(parameters.get("seed", "")))
        path = self.body_world_path(planet_orbit, moon_orbit)
        self.world_file_var.set(str(path) if path.is_file() else "")
        self.body_entries["moon_orbit"].configure(
            state="readonly" if kind == "moon" else "disabled"
        )
        self.body_entries["planet_orbit"].configure(
            state="readonly"
        )
        self.body_entries["gravity"].configure(
            state="normal" if isinstance(visitable_value, dict) else "readonly"
        )
        world_type = str(raw.get("worldType", ""))
        self.body_entries["large_image"].configure(
            state="normal" if world_type == "FloatingDungeon" else "readonly"
        )
        self.body_entries["large_image_scale"].configure(
            state="normal" if isinstance(raw_value, dict) else "readonly"
        )
        has_visitable_parameters = isinstance(visitable_value, dict)
        appearance_state = "normal" if has_visitable_parameters else "readonly"
        self.body_entries["body_label"].configure(
            state="readonly" if has_visitable_parameters else "disabled"
        )
        self.body_entries["world_hue_shift"].configure(state=appearance_state)
        self.body_entries["global_directives"].configure(state=appearance_state)
        appearance_widget = self.body_entries["body_label"]
        if isinstance(appearance_widget, ttk.Combobox):
            current = self.body_label_var.get().strip()
            values = list(self.available_terrestrial_graphics)
            if current and current not in values:
                values.append(current)
            appearance_widget.configure(values=values)
        self.load_world_parameter_fields()
        self.load_region_context()

    def load_world_parameter_fields(self) -> None:
        self.store_loaded_world_layer_fields()
        self.loaded_world_layer_key = None
        reference = self.selected_reference()
        self.world_layer_map = {}
        self.world_layer_combo.configure(values=[])
        self.world_layer_var.set("")
        self.layer_min_height_var.set("")
        self.layer_base_height_var.set("")
        self.layer_dungeons_var.set("")
        self.layer_dungeon_variance_var.set("")
        self.layer_display_dungeons_var.set("")
        self.weather_pool_text.delete("1.0", "end")
        for variable in self.sky_color_vars.values():
            variable.set("")
        if reference is None:
            return
        parameters = self.body_parameters(reference)
        raw = parameters.get("parameters") or {}
        visitable = parameters.get("visitableParameters")
        if not isinstance(visitable, dict):
            self.day_length_var.set("")
            self.world_size_edit_combo.configure(state="disabled")
            self.world_width_entry.configure(state="disabled")
            self.world_height_entry.configure(state="disabled")
            self.layer_min_height_entry.configure(state="disabled")
            self.layer_base_height_entry.configure(state="disabled")
            self.layer_dungeons_entry.configure(state="disabled")
            self.layer_dungeon_variance_entry.configure(state="disabled")
            self.layer_display_dungeons_entry.configure(state="disabled")
            self.set_world_dimension_values({})
            return
        self.world_size_var.set(str(raw.get("worldSize", visitable.get("sizeName", ""))))
        self.set_world_dimension_values(visitable)
        self.day_length_var.set(self.display_number(visitable.get("dayLength")))
        self.weather_pool_text.insert(
            "1.0",
            json.dumps(
                swe.weather_pool_to_editor_pairs(visitable.get("weatherPool", [])),
                ensure_ascii=False,
                indent=2,
            ),
        )
        sky = visitable.get("skyColoring")
        sky = sky if isinstance(sky, dict) else {}
        for key, variable in self.sky_color_vars.items():
            value = sky.get(key)
            variable.set(
                "" if value is None else json.dumps(value, ensure_ascii=False)
            )
        layers = swe.celestial_layer_settings(parameters)
        values: list[str] = []
        for layer in layers:
            label = f'{layer["layerLabel"]} [{layer["layerId"]}]'
            values.append(label)
            self.world_layer_map[label] = str(layer["layerId"])
        self.world_layer_combo.configure(values=values)
        if values:
            self.world_layer_var.set(values[0])
            self.world_layer_changed()
        terrestrial = raw.get("worldType") == "Terrestrial"
        structural_state = "readonly" if terrestrial else "disabled"
        self.world_size_edit_combo.configure(state=structural_state)
        dimension_state = "normal" if terrestrial else "disabled"
        self.world_width_entry.configure(state=dimension_state)
        self.world_height_entry.configure(state=dimension_state)
        layer_state = "normal" if terrestrial else "disabled"
        self.layer_min_height_entry.configure(state=layer_state)
        self.layer_base_height_entry.configure(state=layer_state)
        self.layer_dungeons_entry.configure(state=layer_state)
        self.layer_dungeon_variance_entry.configure(state=layer_state)
        self.layer_display_dungeons_entry.configure(
            state=(
                "normal"
                if terrestrial and bool(self.world_file_var.get().strip())
                and self.world_layer_map.get(self.world_layer_var.get()) in MPI_VISIBLE_LAYERS
                else "disabled"
            )
        )

    def world_layer_changed(self, _event: Any = None) -> None:
        self.store_loaded_world_layer_fields()
        reference = self.selected_reference()
        layer_id = self.world_layer_map.get(self.world_layer_var.get())
        if reference is None or layer_id is None:
            self.loaded_world_layer_key = None
            return
        parameters = self.body_parameters(reference)
        key = (id(parameters), layer_id)
        pending = self.world_layer_pending.get(key)
        layer = swe._celestial_layer(parameters, layer_id)
        minimum = pending[0] if pending is not None else self.display_number(
            layer.get("layerMinHeight")
        )
        base = pending[1] if pending is not None else self.display_number(
            layer.get("layerBaseHeight")
        )
        dungeons = pending[2] if pending is not None else json.dumps(
            layer.get("dungeons", []), ensure_ascii=False
        )
        dungeon_variance = pending[3] if pending is not None else self.display_number(
            layer.get("dungeonXVariance")
        )
        self.layer_min_height_var.set(
            minimum
        )
        self.layer_base_height_var.set(
            base
        )
        self.layer_dungeons_var.set(dungeons)
        self.layer_dungeon_variance_var.set(dungeon_variance)
        self.layer_display_dungeons_var.set(
            self.star_map_dungeon_text.get(key, dungeons)
        )
        self.loaded_world_layer_key = key
        if hasattr(self, "layer_display_dungeons_entry"):
            self.layer_display_dungeons_entry.configure(
                state="normal" if layer_id in MPI_VISIBLE_LAYERS
                and bool(self.world_file_var.get().strip())
                and (parameters.get("parameters") or {}).get("worldType") == "Terrestrial"
                else "disabled"
            )

    def store_loaded_world_layer_fields(self) -> None:
        """Keep the visible boundary fields when another layer is selected."""

        if self.loaded_world_layer_key is None:
            return
        self.star_map_dungeon_text[self.loaded_world_layer_key] = (
            self.layer_display_dungeons_var.get().strip() or "[]"
        )
        visible = (
            self.layer_min_height_var.get().strip(),
            self.layer_base_height_var.get().strip(),
            self.layer_dungeons_var.get().strip(),
            self.layer_dungeon_variance_var.get().strip(),
        )
        parameters = self.parameters_by_identity().get(
            self.loaded_world_layer_key[0]
        )
        if parameters is not None:
            layer = swe._celestial_layer(
                parameters, self.loaded_world_layer_key[1]
            )
            original = (
                self.display_number(layer.get("layerMinHeight")),
                self.display_number(layer.get("layerBaseHeight")),
                json.dumps(layer.get("dungeons", []), ensure_ascii=False),
                self.display_number(layer.get("dungeonXVariance")),
            )
            if visible == original:
                self.world_layer_pending.pop(self.loaded_world_layer_key, None)
                return
        self.world_layer_pending[self.loaded_world_layer_key] = visible

    @staticmethod
    def parse_dungeon_names(text: str) -> list[str]:
        """Parse and normalize one dungeon-ID JSON array."""

        try:
            value = json.loads(text or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Dungeon lists must be valid JSON arrays") from exc
        if not isinstance(value, list):
            raise ValueError("Dungeon lists must be JSON arrays")
        result: list[str] = []
        for name in value:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Every dungeon ID must be a non-empty string")
            result.append(name.strip())
        return result

    def display_dungeon_lists_by_coordinate(
        self,
    ) -> dict[tuple[int, int, str], list[str]]:
        """Return every GUI display-only list, keyed by the current orbit."""

        result: dict[tuple[int, int, str], list[str]] = {}
        for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
            for layer in swe.celestial_layer_settings(parameters):
                layer_id = str(layer["layerId"])
                text = self.star_map_dungeon_text.get(
                    (id(parameters), layer_id),
                    json.dumps(layer.get("dungeons", []), ensure_ascii=False),
                )
                result[(planet_orbit, moon_orbit, layer_id)] = (
                    self.parse_dungeon_names(text)
                )
        return result

    def apply_display_dungeon_overrides(
        self,
        editor: dict[str, Any],
        desired: dict[tuple[int, int, str], list[str]],
        source_location: list[int],
        destination_location: list[int],
    ) -> tuple[int, int]:
        """Overlay safe star-map-only lists onto a cloned Celestial editor.

        Starbound must see the real list while a body is generated.  A display
        override is therefore safe only when a matching World4 already exists
        and this save is not changing that body's generation recipe.
        """

        if self.editor is None or self.original_editor is None:
            return 0, 0
        originals = self.parameters_by_coordinate(self.original_editor)
        cloned = self.parameters_by_coordinate(editor)
        applied_layers = 0
        deferred_bodies: set[tuple[int, int]] = set()
        for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
            coordinate = (planet_orbit, moon_orbit)
            origin = self.body_origins.get(id(parameters))
            original_parameters = originals.get(origin) if origin is not None else None
            structural_change = (
                original_parameters is None
                or self.biome_signature(parameters)
                != self.biome_signature(original_parameters)
            )
            source_world = (
                self.body_world_path(*origin, location=source_location)
                if origin is not None
                else None
            )
            destination_world = self.body_world_path(
                *coordinate, location=destination_location
            )
            has_generated_world = bool(
                (source_world is not None and source_world.is_file())
                or destination_world.is_file()
            )
            target_parameters = cloned.get(coordinate)
            if target_parameters is None:
                continue
            for layer in swe.celestial_layer_settings(parameters):
                layer_id = str(layer["layerId"])
                actual = list(layer.get("dungeons", []))
                wanted = desired.get((planet_orbit, moon_orbit, layer_id), actual)
                if wanted == actual:
                    continue
                if structural_change or not has_generated_world:
                    deferred_bodies.add(coordinate)
                    continue
                target_layer = swe._celestial_layer(target_parameters, layer_id)
                swe.set_celestial_layer_dungeons(
                    target_parameters,
                    layer_id,
                    wanted,
                    target_layer.get("dungeonXVariance"),
                )
                applied_layers += 1
        return applied_layers, len(deferred_bodies)

    def apply_pending_world_layers(self, parameter_id: int) -> None:
        """Validate all pending layers before one branch-local commit."""
        from world_parameter_validation import stage_layer_edits, validate_native_layer_numbers
        parameters = self.parameters_by_identity().get(parameter_id)
        if parameters is None:
            return
        keys = [key for key in self.world_layer_pending if key[0] == parameter_id]
        if not keys:
            return
        candidate = stage_layer_edits(parameters, {key[1]: self.world_layer_pending[key] for key in keys})
        swe.validate_celestial_layer_layout(candidate)
        validate_native_layer_numbers(candidate)
        parameters.clear()
        parameters.update(candidate)
        for key in keys:
            self.world_layer_pending.pop(key, None)

    def _store_world_parameter_fields(self) -> None:
        from world_parameter_validation import (
            native_float32, preset_dimensions, sky_updates, stage_layer_edits,
            validate_active_ocean_levels, validate_native_layer_numbers, world_dimensions,
        )
        reference = self.selected_reference()
        if reference is None:
            return
        parameters = self.body_parameters(reference)
        raw = parameters.get("parameters") or {}
        visitable = parameters.get("visitableParameters")
        if not isinstance(visitable, dict):
            return
        terrestrial = raw.get("worldType") == "Terrestrial"
        # Parse every visible field before modifying the attached model. Native
        # float32/color/integer validation is stronger than JSON finiteness.
        gravity = None
        gravity_text = self.gravity_var.get().strip()
        if gravity_text:
            gravity = native_float32(gravity_text, label=self.tr("gravity"),
                                     positive=terrestrial, nonnegative=not terrestrial)
        day_length = None
        day_text = self.day_length_var.get().strip()
        if day_text:
            day_length = native_float32(day_text, label=self.tr("day_length"), positive=True)
        weather_pool = None
        weather_text = self.weather_pool_text.get("1.0", "end").strip()
        if weather_text:
            weather_value = parse_field_json(weather_text, self.tr("weather_pool"))
            weather_pool = ([] if weather_value == [] and not terrestrial
                            else swe.weather_pool_from_editor(weather_value))
            if weather_pool != visitable.get("weatherPool"):
                self._resource_preflight().validate_weather_pool(
                    weather_pool, label=self.tr("weather_pool"), allow_empty=not terrestrial,
                )
        candidate_sky = sky_updates(visitable.get("skyColoring"),
                                    {key: variable.get() for key, variable in self.sky_color_vars.items()},
                                    terrestrial=terrestrial)
        current_size = str(raw.get("worldSize", ""))
        selected_size = self.world_size_var.get().strip() or current_size
        desired_dimensions = (world_dimensions(self.world_width_var.get(), self.world_height_var.get())
                              if terrestrial else None)
        dimensions_were_edited = (
            desired_dimensions is not None
            and desired_dimensions != self.loaded_world_dimensions
        )
        preset_was_edited = terrestrial and selected_size != current_size
        self.store_loaded_world_layer_fields()
        pending_keys = [key for key in self.world_layer_pending if key[0] == id(parameters)]
        # Shallow owner copies plus only edited layer dicts; no full world or
        # giant arbitrary parameter branch is deep-copied for an ordinary edit.
        candidate = stage_layer_edits(parameters, {key[1]: self.world_layer_pending[key] for key in pending_keys})
        swe.validate_celestial_layer_layout(candidate)
        validate_native_layer_numbers(candidate)
        target_dimensions = desired_dimensions
        if preset_was_edited:
            assets_text = self.assets_var.get().strip()
            assets = Path(assets_text)
            if not assets_text or not assets.exists():
                raise ValueError(self.tr("new_body_assets_required"))
            preset_size = preset_dimensions(candidate, assets, selected_size)
            if not dimensions_were_edited:
                target_dimensions = preset_size
        current_dimensions = candidate["visitableParameters"].get("worldSize")
        resized = target_dimensions is not None and current_dimensions != list(target_dimensions)
        if resized:
            # Scale the CURRENT recipe once, including active ocean levels and
            # all absolute Y coordinates. A preset only supplies target size;
            # it must not transplant unscaled sea levels into a shorter world.
            swe.resize_celestial_terrestrial_body_dimensions(candidate, *target_dimensions)
            validate_active_ocean_levels(candidate)
        visitable = candidate["visitableParameters"]
        if preset_was_edited:
            candidate["parameters"]["worldSize"] = selected_size
            visitable["sizeName"] = selected_size
        if gravity is not None:
            visitable["gravity"] = gravity
        if day_length is not None:
            visitable["dayLength"] = day_length
        if weather_pool is not None:
            visitable["weatherPool"] = weather_pool
        if candidate_sky is not None:
            visitable["skyColoring"] = candidate_sky
        swe.validate_celestial_layer_layout(candidate)
        validate_native_layer_numbers(candidate)
        current_dimensions = visitable.get("worldSize")
        map_size = None
        if isinstance(current_dimensions, list) and len(current_dimensions) == 2:
            assets_text = self.assets_var.get().strip()
            map_size = self.display_number(swe.celestial_system_map_size_for_width(
                current_dimensions[0], Path(assets_text) if assets_text else None))
        # Only this point changes the attached model. Failed input keeps all
        # original body/layer references and the user's pending draft text.
        parameters.clear()
        parameters.update(candidate)
        for key in pending_keys:
            self.world_layer_pending.pop(key, None)
        if isinstance(current_dimensions, list) and len(current_dimensions) == 2:
            self.world_width_var.set(str(int(current_dimensions[0])))
            self.world_height_var.set(str(int(current_dimensions[1])))
            self.loaded_world_dimensions = (
                int(current_dimensions[0]), int(current_dimensions[1])
            )
            self.system_map_size_var.set(map_size)
        self.loaded_world_layer_key = None

    def apply_world_parameters(self) -> None:
        try:
            self._store_world_parameter_fields()
        except Exception as exc:
            messagebox.showerror(
                self.tr("operation_failed"), str(exc), parent=self.root
            )
            return
        iid = self.current_body_iid
        self.rebuild_body_tree(iid)
        self.refresh_biome_context()
        self.load_world_parameter_fields()

    def load_region_context(self) -> None:
        reference = self.selected_reference()
        world: Path | None = None
        if reference is not None:
            kind, planet_entry, moon_entry = reference
            planet = int(planet_entry[0])
            moon = int(moon_entry[0]) if kind == "moon" and moon_entry else 0
            candidate = self.body_world_path(planet, moon)
            if candidate.is_file():
                world = candidate
        world_changed = world != self.region_plan_world
        if world_changed:
            self.region_plan_world = world
            # Invalidate old-world replies before any synchronous metadata
            # probe can fail; failed loading must not display the old profile.
            self.schedule_region_spawn_preview()
        if world is None:
            self.region_x_end_var.set("")
            self.region_y_end_var.set("")
            return
        try:
            document = swe.load_metadata_only(world)
            width, height = int(document["size"][0]), int(document["size"][1])
        except Exception:
            return
        if not self.region_x_end_var.get().strip():
            self.region_x_end_var.set(str(width - 1))
        if not self.region_y_end_var.get().strip():
            self.region_y_end_var.set(str(height - 1))
        for variables in (self.hue_reference_vars, self.hue_target_vars):
            if not variables["xEnd"].get().strip():
                variables["xEnd"].set(str(width - 1))
            if not variables["yEnd"].get().strip():
                variables["yEnd"].set(str(height - 1))

    def browse_region_parallax(self) -> None:
        self.choose_open_file(
            self.region_parallax_path_var,
            self.tr("select_parallax_json"),
            [("JSON", "*.json"), (self.tr("all_files"), "*.*")],
        )

    def _round_direction(self, variable: tk.StringVar) -> str:
        value = variable.get().strip()
        lower_values = {"lower"}
        upper_values = {"upper"}
        for messages in TRANSLATIONS.values():
            lower_values.add(str(messages.get("round_lower", "")))
            upper_values.add(str(messages.get("round_upper", "")))
        if value in lower_values:
            return "lower"
        if value in upper_values:
            return "upper"
        raise ValueError(f"Unknown sector rounding direction: {value!r}")

    def _refresh_rounding_choices(self) -> None:
        values = [self.tr("round_lower"), self.tr("round_upper")]
        for key, combo in self.region_round_combos.items():
            variable = {
                "region_x_start": self.region_round_x_start_var,
                "region_x_end": self.region_round_x_end_var,
                "region_y_start": self.region_round_y_start_var,
                "region_y_end": self.region_round_y_end_var,
            }[key]
            direction = self._round_direction(variable)
            combo.configure(values=values)
            variable.set(values[0] if direction == "lower" else values[1])

    def region_mode_changed(self) -> None:
        operation = self._region_operation()
        tile_only = operation == "tiles"
        spawn_only = operation == "spawns"
        for widget in (self.region_biome_combo, self.region_parallax_only_check,
                       self.region_parallax_entry, self.region_parallax_browse):
            widget.configure(state="disabled" if tile_only or spawn_only else "normal")
        for name in ("region_biome_label", "region_biome_combo", "region_parallax_only_check",
                     "region_parallax_label", "region_parallax_entry", "region_parallax_browse"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.grid_remove() if tile_only or spawn_only else widget.grid()
        if hasattr(self, "region_spawn_frame"):
            if spawn_only:
                self.region_spawn_frame.grid()
            else:
                self.region_spawn_frame.grid_remove()
        if tile_only or spawn_only:
            if tile_only:
                self.region_material_frame.grid()
            else:
                self.region_material_frame.grid_remove()
            self.region_ocean_frame.grid_remove()
        else:
            self.region_material_frame.grid_remove()
            self.region_ocean_frame.grid()
        pair = swe.ocean_biome_pair(self.region_biome_var.get().strip())
        is_ocean = pair is not None
        is_floor = bool(
            pair and self.region_biome_var.get().strip().casefold() == pair[0]
        )
        if is_floor:
            self.region_floor_biome_var.set(pair[0])
        elif pair and not self.region_floor_biome_var.get().strip():
            self.region_floor_biome_var.set(pair[0])
        if is_floor:
            self.region_include_floor_var.set(True)
        state = "normal" if is_ocean and not tile_only and not spawn_only and not self.region_parallax_only_var.get() else "disabled"
        for widget in self.region_ocean_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.region_include_floor_check.configure(state=state)
            self.region_ocean_uniform_check.configure(state=state)
            self.region_coastal_slope_check.configure(state=state)
        except AttributeError:
            pass
        if is_floor:
            self.region_include_floor_check.configure(state="disabled")
        if spawn_only and hasattr(self, "region_spawn_frame"):
            self.schedule_region_spawn_preview()
        if hasattr(self, "region_tile_mode_combo"):
            self.region_tile_mode_changed()

    def region_tile_mode_changed(self) -> None:
        """Show exactly one tile-only module while keeping layer selection shared."""
        try:
            mode = self._region_tile_mode()
        except ValueError:
            return
        for widget in self.region_manual_tile_widgets:
            widget.grid() if mode == "material" else widget.grid_remove()
        for widget in self.region_biome_material_widgets:
            widget.grid() if mode == "biome" else widget.grid_remove()
        self.region_material_hint.configure(
            text=self.tr(
                "region_material_hint" if mode == "material"
                else "region_biome_material_hint"
            )
        )

    def _region_choice(self, variable: tk.StringVar, choices: dict[str, str]) -> str:
        value = variable.get().strip()
        for canonical, key in choices.items():
            if value == canonical or any(value == language.get(key) for language in TRANSLATIONS.values()):
                return canonical
        raise ValueError(f"Unknown region option: {value!r}")

    def _region_operation(self) -> str:
        return self._region_choice(self.region_operation_var, {
            "biome": "region_operation_biome", "tiles": "region_operation_tiles",
            "spawns": "region_operation_spawns",
        })

    def _region_tile_layer(self) -> str:
        return self._region_choice(self.region_tile_layer_var, {
            "both": "region_layer_both", "foreground": "region_layer_foreground",
            "background": "region_layer_background",
        })

    def _region_tile_mode(self) -> str:
        return self._region_choice(self.region_tile_mode_var, {
            "material": "region_tile_mode_material",
            "biome": "region_tile_mode_biome",
        })

    def _refresh_region_operation_choices(self) -> None:
        for variable, combo, choices in (
            (self.region_operation_var, self.region_operation_combo, {
                "biome": "region_operation_biome", "tiles": "region_operation_tiles",
                "spawns": "region_operation_spawns"}),
            (self.region_tile_mode_var, self.region_tile_mode_combo, {
                "material": "region_tile_mode_material",
                "biome": "region_tile_mode_biome"}),
            (self.region_tile_layer_var, self.region_tile_layer_combo, {
                "both": "region_layer_both", "foreground": "region_layer_foreground",
                "background": "region_layer_background"}),
        ):
            canonical = self._region_choice(variable, choices)
            combo.configure(values=[self.tr(key) for key in choices.values()])
            variable.set(self.tr(choices[canonical]))
        self.region_operation_label.configure(text=self.tr("region_operation"))
        self.region_material_frame.configure(text=self.tr("region_material_title"))
        for key, label in self.region_material_labels.items():
            label.configure(text=self.tr(key))
        self.region_load_items_button.configure(text=self.tr("region_load_items"))
        self.region_fuzzy_boundary_check.configure(
            text=self.tr("region_biome_material_fuzzy")
        )
        self.region_spawn_current_label.configure(text=self.tr("region_spawn_current"))
        self.region_spawn_replacement_label.configure(text=self.tr("region_spawn_replacement"))
        self.region_spawn_refresh_button.configure(text=self.tr("region_spawn_refresh"))
        self.region_spawn_hint.configure(
            text=self.tr("region_warn_spawns") + "\n\n" + self.tr("region_spawn_template")
        )
        self.region_tile_mode_changed()

    def _set_region_spawn_preview(self, text: str) -> None:
        self.region_spawn_current_text.configure(state="normal")
        self.region_spawn_current_text.delete("1.0", "end")
        self.region_spawn_current_text.insert("1.0", text)
        self.region_spawn_current_text.configure(state="disabled")

    def schedule_region_spawn_preview(self, *_args: Any) -> None:
        """Debounce coordinate changes; never decode huge world JSON on Tk."""
        if not hasattr(self, "region_spawn_frame") or getattr(self, "_window_closing", False):
            return
        self._spawn_preview_serial += 1
        if self._spawn_preview_after is not None:
            self.root.after_cancel(self._spawn_preview_after)
            self._spawn_preview_after = None
        if self._region_operation() != "spawns":
            return
        self._set_region_spawn_preview(self.tr("region_spawn_loading"))
        self._spawn_preview_after = self.root.after(400, self._start_region_spawn_preview)

    def _start_region_spawn_preview(self) -> None:
        self._spawn_preview_after = None
        if (getattr(self, "_window_closing", False)
                or self._region_operation() != "spawns" or self._spawn_preview_running):
            return
        if self.busy:
            self._spawn_preview_after = self.root.after(400, self._start_region_spawn_preview)
            return
        world = self.region_plan_world
        if world is None or not world.is_file():
            self._set_region_spawn_preview(self.tr("region_spawn_empty"))
            return
        try:
            raw, directions = self._region_coordinate_snapshot()
        except Exception as exc:
            self._set_region_spawn_preview(str(exc))
            return
        serial = self._spawn_preview_serial
        self._spawn_preview_running = True

        def inspect():
            # The worker talks only to a Queue; every Tk call stays on main.
            try:
                before = world_edit_guard.fingerprint(world)
                document = swe.load_metadata_only(world)
                bounds = swe.sector_round_rectangle(
                    *document["size"], *(raw[key] for key in ("xStart", "xEnd", "yStart", "yEnd")),
                    **directions)
                result = region_spawn_replacement.inspect_spawn_rectangle(
                    document, *(bounds[key] for key in ("xStart", "xEnd", "yStart", "yEnd")))
                world_edit_guard.assert_world_unchanged(world, before)
                self._spawn_preview_queue.put((serial, bounds, result, None))
            except Exception as exc:
                self._spawn_preview_queue.put((serial, None, None, str(exc)))

        threading.Thread(target=inspect, daemon=True).start()
        self._spawn_preview_poll_after = self.root.after(75, self._poll_region_spawn_preview)

    def _poll_region_spawn_preview(self) -> None:
        self._spawn_preview_poll_after = None
        if getattr(self, "_window_closing", False):
            return
        try:
            serial, bounds, result, error = self._spawn_preview_queue.get_nowait()
        except queue.Empty:
            self._spawn_preview_poll_after = self.root.after(75, self._poll_region_spawn_preview)
            return
        self._spawn_preview_running = False
        if self._region_operation() != "spawns":
            return
        if serial != self._spawn_preview_serial:
            if self._spawn_preview_after is None:
                self._spawn_preview_after = self.root.after(10, self._start_region_spawn_preview)
            return
        if error is not None:
            self._set_region_spawn_preview(error)
            return
        lines = [f"X {bounds['xStart']}–{bounds['xEnd']}, Y {bounds['yStart']}–{bounds['yEnd']}", ""]
        for group in result["groups"]:
            name = group["biomeName"] or "Null biome (0)"
            monsters = ", ".join(group["spawnTypes"]) or self.tr("region_spawn_none")
            lines.append(f"{name} - {monsters}")
            lines.append("")
        self._set_region_spawn_preview("\n".join(lines))

    def _region_coordinate_snapshot(self) -> tuple[dict[str, Any], dict[str, str]]:
        try:
            values = {key: float(variable.get().strip()) for key, variable in (
                ("xStart", self.region_x_start_var), ("xEnd", self.region_x_end_var),
                ("yStart", self.region_y_start_var), ("yEnd", self.region_y_end_var))}
            if not all(math.isfinite(value) for value in values.values()):
                raise ValueError("Coordinates must be finite")
            raw = {key: int(value) if value.is_integer() else value for key, value in values.items()}
        except ValueError as exc:
            raise ValueError(self.tr("invalid_number", field="X / Y")) from exc
        directions = {
            "x_start_direction": self._round_direction(self.region_round_x_start_var),
            "x_end_direction": self._round_direction(self.region_round_x_end_var),
            "y_start_direction": self._round_direction(self.region_round_y_start_var),
            "y_end_direction": self._round_direction(self.region_round_y_end_var),
        }
        return raw, directions

    def _region_assets(self) -> Path:
        raw = self.assets_var.get().strip()
        if not raw or not Path(raw).exists():
            raise ValueError(self.tr("region_assets_required"))
        return Path(raw)

    def _resource_preflight(self) -> ResourcePreflight:
        """Capture the explicitly selected stack, with no implicit live install."""
        assets = self.assets_var.get().strip()
        universe_variable = getattr(self, "chunks_var", None)
        universe = universe_variable.get().strip() if universe_variable is not None else ""
        return ResourcePreflight(Path(assets) if assets else None,
                                 universe_folder=Path(universe) if universe else None)

    def _check_image_edits(self, current: dict[str, Any], original: dict[str, Any], *,
                           kind: str, preflight: ResourcePreflight | None = None) -> None:
        """Validate only edited image fields; preserve unrelated legacy values."""
        keys = {"system": ("image", "twinkleFrames", "twinkleBackground"),
                "body": ("smallImage", "image"), "station": ("icon",)}[kind]
        original = original if isinstance(original, dict) else {}
        changed = [key for key in keys if key in current and current[key] != original.get(key)]
        if not changed:
            return
        checker = preflight if preflight is not None else self._resource_preflight()
        if kind == "system":
            diagnostics = checker.check_system_images(current, fields=changed)
        else:
            diagnostics = [diagnostic for key in changed
                           for diagnostic in checker.check_image(current[key], label=f"{kind}.{key}")]
        # A known missing central-star image can stop the installed Navigation
        # UI, whereas secondary images have observed fallback behavior.
        fatal = [str(item) for item in diagnostics
                 if kind == "system" and item.field == "System.image" and item.is_error]
        if fatal:
            raise ValueError("\n".join(fatal))
        if diagnostics and not self.ask_yes_no(
                self.tr("resource_warning_title"),
                self.tr("resource_warning_confirm", details="\n".join(map(str, diagnostics)))):
            raise ValueError(self.tr("resource_warning_cancelled"))

    def _validate_changed_resources_for_save(self) -> None:
        """Recheck staged edits against the load snapshot using current assets.

        This covers earlier Apply operations, other bodies and a changed asset
        selection. It does not silently rewrite or reject untouched legacy data.
        """
        if self.editor is None or self.original_editor is None:
            return
        checker = self._resource_preflight()
        self._check_image_edits(self.editor["systemParameters"].get("parameters") or {},
                                self.original_editor["systemParameters"].get("parameters") or {},
                                kind="system", preflight=checker)
        originals = self.parameters_by_coordinate(self.original_editor)
        for planet, moon, parameters in self.iter_body_parameters():
            origin = self.body_origins.get(id(parameters), (planet, moon))
            original = originals.get(origin) or {}
            raw = parameters.get("parameters") or {}
            visitable = parameters.get("visitableParameters") or {}
            old_visitable = original.get("visitableParameters") or {}
            pool = visitable.get("weatherPool")
            if "weatherPool" in visitable and pool != old_visitable.get("weatherPool"):
                checker.validate_weather_pool(pool, label=f"{parameters.get('name', planet)} / {self.tr('weather_pool')}",
                                              allow_empty=raw.get("worldType") != "Terrestrial")
            self._check_image_edits(raw, original.get("parameters") or {}, kind="body", preflight=checker)
        old_objects = {obj.get("uuid"): obj for obj in (self.original_system_file_state or {}).get("body", {}).get("objects", [])}
        for obj in (self.system_file_state or {}).get("body", {}).get("objects", []):
            original = old_objects.get(obj.get("uuid")) or {}
            self._check_image_edits(obj.get("parameters") or {}, original.get("parameters") or {},
                                    kind="station", preflight=checker)

    def _prepare_dungeon_dropdown(self, selector: DungeonListSelector) -> bool:
        """Resolve both catalogs once; unknown/unsupported assets never look valid."""
        if self.busy:
            return False
        try:
            assets = self._region_assets()
            universe = Path(self.chunks_var.get().strip()) if self.chunks_var.get().strip() else None
            context = (str(assets.resolve()).casefold(), str(universe.resolve()).casefold() if universe else "")
            if getattr(self, "_dungeon_choices_context", None) == context:
                return True
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return False

        def task():
            sources = dungeon_choices.discover_asset_sources(assets, universe_folder=universe)
            stack = dungeon_choices.AssetSourceStack(sources)
            return dungeon_choices.real_dungeon_names(stack), dungeon_choices.mpi_dungeon_names(stack)

        def success(result):
            real, display = result
            self.layer_dungeons_entry.configure(values=real)
            self.layer_display_dungeons_entry.configure(values=display)
            self._dungeon_choices_context = context
            self.set_status(f"Dungeon choices: {len(real)} generation / {len(display)} MPI")
            selector.open_dropdown()

        self.run_worker(task, success, "dungeon_loading")
        return False

    def load_region_block_items(self) -> None:
        if self.busy:
            return
        try:
            assets = self._region_assets()
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return

        def task():
            return region_tile_replacement.load_block_item_catalog(assets)

        def success(catalog):
            names = sorted(catalog, key=str.casefold)
            self.region_source_item_combo.configure(values=names)
            self.region_target_item_combo.configure(values=names)
            self.set_status(self.tr("region_items_loaded", count=len(names)))

        self.run_worker(task, success, "region_inspecting_tiles")

    def _region_rectangle_value(self) -> dict[str, Any]:
        if self.region_plan_world is None or not self.region_plan_world.is_file():
            raise ValueError(self.tr("region_world_required"))
        try:
            numbers = {
                "xStart": float(self.region_x_start_var.get().strip()),
                "xEnd": float(self.region_x_end_var.get().strip()),
                "yStart": float(self.region_y_start_var.get().strip()),
                "yEnd": float(self.region_y_end_var.get().strip()),
            }
            if not all(math.isfinite(value) for value in numbers.values()):
                raise ValueError("Coordinates must be finite")
            # Keep integer input compatible, but never truncate a fractional
            # coordinate before its selected sector-edge rounding is applied.
            raw = {key: int(value) if value.is_integer() else value
                   for key, value in numbers.items()}
        except ValueError as exc:
            raise ValueError(self.tr("invalid_number", field="X / Y")) from exc
        document = swe.load_metadata_only(self.region_plan_world)
        width, height = int(document["size"][0]), int(document["size"][1])
        rounded = swe.sector_round_rectangle(
            width, height, raw["xStart"], raw["xEnd"], raw["yStart"], raw["yEnd"],
            x_start_direction=self._round_direction(self.region_round_x_start_var),
            x_end_direction=self._round_direction(self.region_round_x_end_var),
            y_start_direction=self._round_direction(self.region_round_y_start_var),
            y_end_direction=self._round_direction(self.region_round_y_end_var),
        )
        return {**rounded, "requestedRange": raw}

    def _region_form_value(self) -> dict[str, Any]:
        self._region_assets()
        rectangle = self._region_rectangle_value()
        biome = self.region_biome_var.get().strip()
        if not biome:
            raise ValueError(self.tr("region_biome"))
        parallax_only = bool(self.region_parallax_only_var.get())
        parallax_path = self.region_parallax_path_var.get().strip()
        parallax = swe.load_parallax_json_file(Path(parallax_path)) if parallax_path else None
        pair = swe.ocean_biome_pair(biome)
        is_floor = bool(pair and biome.casefold() == pair[0] and not parallax_only)
        include_floor = bool(pair and not parallax_only and (
            self.region_include_floor_var.get() or is_floor))
        floor = floor_bottom = transition = liquid = None
        coastal_step = 32
        ecology_height = swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT
        coastal_slope = bool(include_floor and self.region_coastal_slope_var.get())

        def optional_number(variable, label, integer=False):
            raw = variable.get().strip()
            if not raw:
                return None
            try:
                value = float(raw)
                if not math.isfinite(value) or (integer and not value.is_integer()):
                    raise ValueError()
                return int(value) if integer else value
            except ValueError as exc:
                raise ValueError(f"{self.tr(label)}: {raw!r} is not a valid "
                                 f"{'integer' if integer else 'finite number'}") from exc

        if pair and not parallax_only:
            liquid = optional_number(self.region_ocean_liquid_var, "ocean_surface_height", True)
        if include_floor:
            floor = optional_number(self.region_ocean_floor_var, "ocean_floor_height")
            floor_bottom = optional_number(self.region_floor_bottom_var, "ocean_floor_bottom", True)
            transition = optional_number(self.region_floor_transition_var, "ocean_floor_transition", True)
            step = optional_number(self.region_coastal_step_var, "ocean_coastal_slope_step", True)
            ecology = optional_number(self.region_floor_ecology_var, "ocean_floor_ecology_height", True)
            controls = normalize_ocean_floor_controls(
                coastal_slope, 32 if step is None else step,
                swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT if ecology is None else ecology,
            )
            coastal_step = controls["coastalSlopeStep"]
            ecology_height = controls["floorEcologyHeight"]
            self.region_floor_ecology_var.set(str(ecology_height))
        return {
            "biome": biome, **rectangle,
            "oceanFloorHeight": floor, "oceanFloorBottom": floor_bottom,
            "oceanFloorTransition": transition, "oceanLiquidLevel": liquid,
            "oceanFloorBiome": pair[0] if is_floor else (
                self.region_floor_biome_var.get().strip() or None),
            "includeOceanFloor": include_floor, "oceanFloorOnly": is_floor,
            "oceanUniform": bool(self.region_ocean_uniform_var.get()),
            "coastalSlopeEnabled": coastal_slope, "coastalSlopeStep": coastal_step,
            "floorEcologyHeight": ecology_height,
            "parallaxOnly": parallax_only, "parallaxPath": parallax_path, "parallax": parallax,
        }

    @staticmethod
    def _safe_boundary_range(
        preview: dict[str, Any], x_start: int, x_end: int
    ) -> tuple[int, int]:
        narrow = [
            item
            for item in preview.get("boundaryTransitions", [])
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
        return (
            next(iter(left)) if len(left) == 1 else x_start,
            next(iter(right)) if len(right) == 1 else x_end,
        )

    def apply_region_plans(self) -> None:
        """Apply one immutable snapshot of the current form; never stage a queue."""
        if self.busy:
            return
        try:
            operation = self._region_operation()
            if operation == "spawns" and not self.region_spawn_json_text.get("1.0", "end").strip():
                self.set_status_key("region_spawns_no_change")
                return
            world = self.region_plan_world
            assets = self._region_assets()
            if operation == "tiles":
                tile_mode = self._region_tile_mode()
                item = {**self._region_rectangle_value(), "mode": tile_mode,
                        "layer": self._region_tile_layer()}
                if tile_mode == "material":
                    item.update({
                        "sourceItem": self.region_source_item_var.get().strip(),
                        "targetItem": self.region_target_item_var.get().strip(),
                    })
                    if not item["sourceItem"] or not item["targetItem"]:
                        raise ValueError(self.tr("region_items_required"))
                else:
                    item.update({
                        "targetBiome": self.region_target_biome_var.get().strip(),
                        "fuzzyBoundary": bool(self.region_fuzzy_boundary_var.get()),
                    })
                    if not item["targetBiome"]:
                        raise ValueError(self.tr("region_biome_material_required"))
            elif operation == "spawns":
                item = {**self._region_rectangle_value(), "spawnProfile":
                        region_spawn_replacement.normalize_spawn_profile_template(
                            self.region_spawn_json_text.get("1.0", "end"))}
            else:
                item = json_clone(self._region_form_value())
            self.assert_direct_world_edit_safe(world)
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        if operation == "tiles":
            self._apply_region_tile_request(world, assets, item)
            return
        warning = self.tr("region_warn_spawns" if operation == "spawns" else
                          "region_warn_parallax" if item.get("parallaxOnly") else "region_warn_biome")
        if not self.ask_yes_no(
            self.tr("region_confirm_title"),
            self.tr("region_single_confirm", world=world, warning=warning,
                    x1=item["xStart"], x2=item["xEnd"], y1=item["yStart"], y2=item["yEnd"],
                    availability=self.tr("region_unload_hint")),
        ):
            self.set_status_key("region_cancelled")
            return
        if operation == "spawns":
            self._apply_region_spawn_request(world, assets, item)
            return
        plan = [item]
        self.audit_log("region_apply_requested", world=world, assets=assets,
                       rectangleCount=1, rectangles=plan)

        def task():
            try:
                self._assert_region_world_available(world)
                return ocean_flora_completion.regenerate_rectangles_in_place(
                    world, assets, plan,
                    progress=lambda event, details: self.report_ocean_generation_progress(
                        event, details, world=world),
                )
            except Exception as exc:
                self.audit_exception("region_apply_failed", exc, world=world,
                                     assets=assets, rectangleCount=1)
                raise

        def success(result):
            backup, summary = result
            self.last_output = world
            self.audit_log("region_apply_completed", world=world, backup=backup, summary=summary)
            self.set_status_key("region_completed", sectors=summary["resetSectors"])
            messagebox.showinfo(
                self.tr("region_confirm_title"),
                self.tr("region_done", count=1, sectors=summary["resetSectors"], backup=backup),
                parent=self.root,
            )

        self.run_worker(task, success, "region_writing")

    def _assert_region_world_available(self, world: Path) -> None:
        # Check only the captured target file. Other running worlds are not a
        # reason to reject this edit, and no Tk form values are read here.
        world_edit_guard.assert_world_available(Path(world))

    def _apply_region_spawn_request(self, world: Path, assets: Path, item: dict[str, Any]) -> None:
        request = json_clone(item)
        self.audit_log("region_spawn_apply_requested", world=world, request=request)

        def task():
            try:
                self._assert_region_world_available(world)
                sources = dungeon_choices.discover_asset_sources(assets, universe_folder=world.parent)
                known = region_spawn_replacement.load_spawn_type_names(assets, asset_sources=sources)
                return region_spawn_edit.replace_in_place(
                    world, request, request["spawnProfile"], known_spawn_types=known,
                    progress=lambda event, details: self.audit_log(event, **details))
            except Exception as exc:
                self.audit_exception("region_spawn_apply_failed", exc, world=world, request=request)
                raise

        def success(result):
            backup, summary = result
            self.last_output = world
            self.audit_log("region_spawn_apply_completed", world=world, backup=backup, summary=summary)
            if summary["changed"]:
                self.set_status_key("region_spawns_done", backup=backup)
                messagebox.showinfo(
                    self.tr("region_confirm_title"),
                    self.tr("region_spawns_done", backup=backup),
                    parent=self.root,
                )
            else:
                self.set_status_key("region_spawns_no_change")
                messagebox.showinfo(
                    self.tr("region_confirm_title"),
                    self.tr("region_spawns_no_change"), parent=self.root,
                )
            self.schedule_region_spawn_preview()

        self.run_worker(task, success, "region_writing")

    def _apply_region_tile_request(self, world: Path, assets: Path, item: dict[str, Any]) -> None:
        """Count saved matches first; commit only the exact previewed source."""
        if item.get("mode", "material") == "biome":
            self._apply_region_biome_material_request(world, assets, item)
            return
        request = json_clone(item)
        rectangle = {key: request[key] for key in ("xStart", "xEnd", "yStart", "yEnd")}
        args = (world, assets, request["sourceItem"], request["targetItem"],
                rectangle, request["layer"])
        self.audit_log("region_tile_preview_requested", world=world, assets=assets, request=request)

        def preview_task():
            try:
                self._assert_region_world_available(world)
                return region_tile_replacement.inspect_world_material_rectangle(*args)
            except Exception as exc:
                self.audit_exception("region_tile_preview_failed", exc, world=world, request=request)
                raise

        def preview_done(preview):
            preview = json_clone(preview)
            if not preview["changedTileLayers"]:
                self.set_status_key("region_no_change")
                messagebox.showinfo(self.tr("region_confirm_title"),
                                    self.tr("region_tiles_no_change"), parent=self.root)
                self.audit_log("region_tile_no_change", world=world, summary=preview)
                return
            layer_label = self.tr({
                "both": "region_layer_both", "foreground": "region_layer_foreground",
                "background": "region_layer_background",
            }[request["layer"]])
            warning = self.tr("region_warn_tiles") + "\n\n" + "\n".join(preview.get("warnings", []))
            if not self.ask_yes_no(
                self.tr("region_confirm_title"),
                self.tr("region_tiles_confirm", world=world,
                        source=preview["sourceItem"], target=preview["targetItem"], layer=layer_label,
                        foreground=preview["foregroundTiles"], background=preview["backgroundTiles"],
                        x1=rectangle["xStart"], x2=rectangle["xEnd"],
                        y1=rectangle["yStart"], y2=rectangle["yEnd"], warning=warning,
                        availability=self.tr("region_unload_hint")),
            ):
                self.set_status_key("region_cancelled")
                return

            try:
                self.assert_direct_world_edit_safe(world)
            except Exception as exc:
                messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
                return

            def write_task():
                try:
                    self._assert_region_world_available(world)
                    return region_tile_replacement.replace_world_material_rectangle_in_place(
                        *args, expected_fingerprint=preview["sourceFingerprint"],
                        expected_asset_signature=preview["assetSignature"])
                except Exception as exc:
                    self.audit_exception("region_tile_apply_failed", exc, world=world, request=request)
                    raise

            def write_done(result):
                backup, summary = result
                self.last_output = world
                self.audit_log("region_tile_apply_completed", world=world, backup=backup, summary=summary)
                self.set_status_key("region_tiles_completed", foreground=summary["foregroundTiles"],
                                    background=summary["backgroundTiles"])
                messagebox.showinfo(
                    self.tr("region_confirm_title"),
                    self.tr("region_tiles_done", foreground=summary["foregroundTiles"],
                            background=summary["backgroundTiles"], backup=backup),
                    parent=self.root,
                )

            self.run_worker(write_task, write_done, "region_writing")

        self.run_worker(preview_task, preview_done, "region_inspecting_tiles")

    def _apply_region_biome_material_request(
        self, world: Path, assets: Path, item: dict[str, Any]
    ) -> None:
        """Preview and atomically commit saved natural-material conversion."""
        request = json_clone(item)
        rectangle = {
            key: request[key] for key in ("xStart", "xEnd", "yStart", "yEnd")
        }
        args = (
            world, assets, request["targetBiome"], rectangle,
            request["layer"], request["fuzzyBoundary"],
        )
        self.audit_log(
            "region_biome_material_preview_requested",
            world=world, assets=assets, request=request,
        )

        def preview_task():
            try:
                self._assert_region_world_available(world)
                return region_biome_material_conversion.inspect_world_biome_material_rectangle(
                    *args
                )
            except Exception as exc:
                self.audit_exception(
                    "region_biome_material_preview_failed", exc,
                    world=world, request=request,
                )
                raise

        def preview_done(preview):
            preview = json_clone(preview)
            if not preview["changedTileLayers"]:
                self.set_status_key("region_no_change")
                messagebox.showinfo(
                    self.tr("region_confirm_title"),
                    self.tr("region_biome_material_no_change"), parent=self.root,
                )
                self.audit_log(
                    "region_biome_material_no_change", world=world, summary=preview
                )
                return
            layer_label = self.tr({
                "both": "region_layer_both",
                "foreground": "region_layer_foreground",
                "background": "region_layer_background",
            }[request["layer"]])
            boundary_label = self.tr(
                "region_boundary_fuzzy" if request["fuzzyBoundary"]
                else "region_boundary_hard"
            )
            warnings = [self.tr("region_biome_material_hint")]
            warnings.extend(preview.get("warnings", []))
            if not self.ask_yes_no(
                self.tr("region_confirm_title"),
                self.tr(
                    "region_biome_material_confirm",
                    world=world, biome=preview["targetBiome"], layer=layer_label,
                    boundary=boundary_label,
                    palette=", ".join(preview["targetMaterialNames"]),
                    foreground=preview["foregroundTiles"],
                    background=preview["backgroundTiles"],
                    outside=preview.get("fuzzyIncludedOutsideTiles", 0),
                    inside=preview.get("fuzzyExcludedInsideTiles", 0),
                    x1=rectangle["xStart"], x2=rectangle["xEnd"],
                    y1=rectangle["yStart"], y2=rectangle["yEnd"],
                    warning="\n\n".join(warnings),
                    availability=self.tr("region_unload_hint"),
                ),
            ):
                self.set_status_key("region_cancelled")
                return
            try:
                self.assert_direct_world_edit_safe(world)
            except Exception as exc:
                messagebox.showerror(
                    self.tr("operation_failed"), str(exc), parent=self.root
                )
                return

            def write_task():
                try:
                    self._assert_region_world_available(world)
                    return region_biome_material_conversion.replace_world_biome_material_rectangle_in_place(
                        *args,
                        expected_fingerprint=preview["sourceFingerprint"],
                        expected_asset_signature=preview["assetSignature"],
                    )
                except Exception as exc:
                    self.audit_exception(
                        "region_biome_material_apply_failed", exc,
                        world=world, request=request,
                    )
                    raise

            def write_done(result):
                backup, summary = result
                self.last_output = world
                self.audit_log(
                    "region_biome_material_apply_completed",
                    world=world, backup=backup, summary=summary,
                )
                self.set_status_key(
                    "region_biome_material_completed",
                    foreground=summary["foregroundTiles"],
                    background=summary["backgroundTiles"],
                    biome=summary["targetBiome"],
                )
                messagebox.showinfo(
                    self.tr("region_confirm_title"),
                    self.tr(
                        "region_biome_material_done",
                        foreground=summary["foregroundTiles"],
                        background=summary["backgroundTiles"],
                        biome=summary["targetBiome"], backup=backup,
                    ),
                    parent=self.root,
                )

            self.run_worker(write_task, write_done, "region_writing")

        self.run_worker(preview_task, preview_done, "region_inspecting_tiles")

    def _hue_coordinates(self) -> tuple[dict[str, int], dict[str, int]]:
        if self.region_plan_world is None or not self.region_plan_world.is_file():
            raise ValueError(self.tr("region_world_required"))
        try:
            reference = {
                key: int(variable.get().strip())
                for key, variable in self.hue_reference_vars.items()
            }
            target = {
                key: int(variable.get().strip())
                for key, variable in self.hue_target_vars.items()
            }
        except ValueError as exc:
            raise ValueError(self.tr("invalid_integer", field="X / Y")) from exc
        document = swe.load_metadata_only(self.region_plan_world)
        width, height = int(document["size"][0]), int(document["size"][1])
        for title, value in (("Reference", reference), ("Target", target)):
            if not 0 <= value["xStart"] < width or not 0 <= value["xEnd"] < width:
                raise ValueError(f"{title} X must be from 0 to {width - 1}")
            if not 0 <= value["yStart"] <= value["yEnd"] < height:
                raise ValueError(f"{title} Y must increase from 0 to {height - 1}")
        return reference, target

    def analyze_hue_reference(self) -> None:
        try:
            reference, _target = self._hue_coordinates()
            summary = swe.inspect_world_reference_hues(
                self.region_plan_world,
                reference["xStart"], reference["xEnd"],
                reference["yStart"], reference["yEnd"],
            )
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        messagebox.showinfo(
            self.tr("hue_copy_tab"),
            self.tr(
                "hue_analysis_done",
                foreground=summary.get("referenceForegroundMaterials", 0),
                background=summary.get("referenceBackgroundMaterials", 0),
            ),
            parent=self.root,
        )

    def apply_hue_copy(self) -> None:
        try:
            reference, target = self._hue_coordinates()
            world = Path(self.region_plan_world)
            self.assert_direct_world_edit_safe(world)
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        if not self.ask_yes_no(
            self.tr("hue_copy_tab"), self.tr("hue_copy_confirm", world=world)
        ):
            return

        def task():
            def build(backup_source: Path, staged_output: Path):
                return swe.copy_world_reference_hues(
                    backup_source,
                    staged_output,
                    target["xStart"], target["xEnd"],
                    target["yStart"], target["yEnd"],
                    reference["xStart"], reference["xEnd"],
                    reference["yStart"], reference["yEnd"],
                )[0]
            return swe.rebuild_world_in_place_from_backup(world, build)

        def success(result):
            backup, summary = result
            self.last_output = world
            messagebox.showinfo(
                self.tr("hue_copy_tab"),
                self.tr(
                    "hue_copy_done",
                    foreground=summary.get("foregroundTilesChanged", 0),
                    background=summary.get("backgroundTilesChanged", 0),
                    backup=backup,
                ),
                parent=self.root,
            )

        self.run_worker(task, success, "saving_system")

    def current_biome_group(self) -> str:
        value = self.biome_group_combo.get() if hasattr(self, "biome_group_combo") else ""
        return "subregions" if value == self.tr("sub_regions") else "regions"

    def refresh_biome_context(self) -> None:
        self.biome_group_map = {}
        reference = self.selected_reference()
        if reference is None:
            self.store_loaded_biome_list()
            self.biome_list.delete(0, "end")
            self.loaded_biome_key = None
            self.biome_layer_combo.configure(values=[])
            self.biome_layer_var.set("")
            for variable in (
                self.secondary_region_min_var,
                self.secondary_region_max_var,
                self.subregion_min_var,
                self.subregion_max_var,
            ):
                variable.set("")
            return
        parameters = self.body_parameters(reference)
        groups = swe.celestial_layer_groups(parameters)
        layer_values: list[str] = []
        for group in groups:
            label = group["layerLabel"]
            if label not in layer_values:
                layer_values.append(label)
            self.biome_group_map[f'{label}|{group["group"]}'] = (
                group["layerId"], group["group"]
            )
        current = self.biome_layer_var.get()
        self.biome_layer_combo.configure(values=layer_values)
        self.biome_layer_var.set(current if current in layer_values else (layer_values[0] if layer_values else ""))
        groups_display = [self.tr("main_regions"), self.tr("sub_regions")]
        self.biome_group_combo.configure(values=groups_display)
        if self.biome_group_combo.get() not in groups_display:
            self.biome_group_combo.set(groups_display[0])
        self.load_biome_list()

    def biome_context_changed(self, _event: Any = None) -> None:
        self.load_biome_list()

    def current_biome_context(self) -> tuple[dict[str, Any], str, str] | None:
        reference = self.selected_reference()
        if reference is None:
            return None
        label = self.biome_layer_var.get()
        group = self.current_biome_group()
        context = self.biome_group_map.get(f"{label}|{group}")
        if context is None:
            return None
        return self.body_parameters(reference), context[0], context[1]

    def load_biome_list(self) -> None:
        self.store_loaded_biome_list()
        self.biome_list.delete(0, "end")
        context = self.current_biome_context()
        if context is None:
            self.loaded_biome_key = None
            for variable in (
                self.secondary_region_min_var,
                self.secondary_region_max_var,
                self.subregion_min_var,
                self.subregion_max_var,
            ):
                variable.set("")
            return
        parameters, layer_id, group = context
        key = (id(parameters), layer_id, group)
        self.loaded_biome_key = key
        names = self.biome_pending.get(key)
        if names is None:
            names = [
                str(region.get("biome", ""))
                for region in swe.celestial_biome_region_objects(parameters, layer_id, group)
            ]
        if layer_id == "surface" and group == "regions":
            self.ensure_ocean_pair_settings(parameters, list(names))
        elif layer_id == "surface" and group == "subregions":
            main_key = (id(parameters), "surface", "regions")
            main_names = self.biome_pending.get(main_key)
            if main_names is None:
                main_names = self.surface_biome_names(parameters)
            settings = self.ensure_ocean_pair_settings(
                parameters, list(main_names)
            )
            normalized, _added, _removed, _forced = (
                swe.normalize_uniform_ocean_subregion_names(
                    main_names, names, settings
                )
            )
            if list(names) != normalized:
                names = normalized
                self.biome_pending[key] = list(normalized)
        for name in names:
            self.biome_list.insert("end", name)
        layer = swe._celestial_layer(parameters, layer_id)
        secondary = layer.get("secondaryRegionSizeRange", [0.5, 0.8])
        subregion = layer.get("subRegionSizeRange", [0.25, 0.4])
        self.secondary_region_min_var.set(self.display_number(secondary[0]))
        self.secondary_region_max_var.set(self.display_number(secondary[1]))
        self.subregion_min_var.set(self.display_number(subregion[0]))
        self.subregion_max_var.set(self.display_number(subregion[1]))
        self.refresh_ocean_floor_button()

    def store_loaded_biome_list(self) -> None:
        if self.loaded_biome_key is None or not hasattr(self, "biome_list"):
            return
        names = [
            str(self.biome_list.get(index)) for index in range(self.biome_list.size())
        ]
        # A valid compiled region group always has a primary biome.  An empty
        # list here can only be a transient UI state while the selection is
        # being rebuilt; it must not overwrite the stored recipe.
        if not names:
            self.biome_pending.pop(self.loaded_biome_key, None)
            return
        self.biome_pending[self.loaded_biome_key] = names
        self.store_loaded_range_fields(strict=False)

    def _range_from_variables(
        self,
        minimum: tk.StringVar,
        maximum: tk.StringVar,
        field_key: str,
        *,
        strict: bool,
    ) -> list[float] | None:
        values = [minimum.get().strip(), maximum.get().strip()]
        if not all(values):
            if strict:
                raise ValueError(
                    self.tr("invalid_range", field=self.tr(field_key))
                )
            return None
        try:
            pair = [float(value) for value in values]
        except ValueError as exc:
            if strict:
                raise ValueError(
                    self.tr("invalid_range", field=self.tr(field_key))
                ) from exc
            return None
        if not all(math.isfinite(value) for value in pair):
            if strict:
                raise ValueError(
                    self.tr("invalid_range", field=self.tr(field_key))
                )
            return None
        if pair[0] < 0 or pair[1] < pair[0]:
            if strict:
                raise ValueError(
                    self.tr("invalid_range", field=self.tr(field_key))
                )
            return None
        if field_key == "subregion_range" and pair[1] >= 1:
            if strict:
                raise ValueError(
                    self.tr("invalid_range", field=self.tr(field_key))
                )
            return None
        return pair

    def store_loaded_range_fields(self, *, strict: bool) -> None:
        if self.loaded_biome_key is None:
            return
        parameter_id, layer_id, _group = self.loaded_biome_key
        parameters = self.parameters_by_identity().get(parameter_id)
        if parameters is None:
            return
        secondary = self._range_from_variables(
            self.secondary_region_min_var,
            self.secondary_region_max_var,
            "secondary_region_range",
            strict=strict,
        )
        subregion = self._range_from_variables(
            self.subregion_min_var,
            self.subregion_max_var,
            "subregion_range",
            strict=strict,
        )
        layer = swe._celestial_layer(parameters, layer_id)
        if secondary is not None:
            layer["secondaryRegionSizeRange"] = secondary
        if subregion is not None:
            layer["subRegionSizeRange"] = subregion

    def biome_list_selected(self, _event: Any = None) -> None:
        selection = self.biome_list.curselection()
        if selection:
            name = str(self.biome_list.get(selection[0]))
            self.biome_name_var.set(
                self.biome_name_to_display.get(name, name)
            )
        self.refresh_ocean_floor_button()

    def current_surface_pair_state(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any] | None]] | None:
        if self.loaded_biome_key is None:
            return None
        parameter_id, layer_id, group = self.loaded_biome_key
        if layer_id != "surface" or group != "regions":
            return None
        parameters = self.parameters_by_identity().get(parameter_id)
        if parameters is None:
            return None
        names = [str(self.biome_list.get(i)) for i in range(self.biome_list.size())]
        return parameters, self.ensure_ocean_pair_settings(parameters, names)

    def current_uniform_ocean_subregion_locks(self) -> dict[int, str]:
        """Return locked ocean slots for the displayed Surface Sub-regions."""

        if self.loaded_biome_key is None:
            return {}
        parameter_id, layer_id, group = self.loaded_biome_key
        if layer_id != "surface" or group != "subregions":
            return {}
        parameters = self.parameters_by_identity().get(parameter_id)
        if parameters is None:
            return {}
        main_key = (parameter_id, "surface", "regions")
        main_names = self.biome_pending.get(main_key)
        if main_names is None:
            main_names = self.surface_biome_names(parameters)
        settings = self.ensure_ocean_pair_settings(parameters, list(main_names))
        return swe.uniform_ocean_subregion_locks(main_names, settings)

    def normalize_loaded_surface_subregions(self) -> None:
        """Immediately restore locked slots after a Sub-region list edit."""

        if self.loaded_biome_key is None:
            return
        parameter_id, layer_id, group = self.loaded_biome_key
        if layer_id != "surface" or group != "subregions":
            return
        parameters = self.parameters_by_identity().get(parameter_id)
        if parameters is None:
            return
        main_key = (parameter_id, "surface", "regions")
        main_names = self.biome_pending.get(main_key)
        if main_names is None:
            main_names = self.surface_biome_names(parameters)
        settings = self.ensure_ocean_pair_settings(parameters, list(main_names))
        names = [
            str(self.biome_list.get(index))
            for index in range(self.biome_list.size())
        ]
        normalized, _added, _removed, _forced = (
            swe.normalize_uniform_ocean_subregion_names(
                main_names, names, settings
            )
        )
        selection = self.biome_list.curselection()
        selected_index = selection[0] if selection else None
        if names != normalized:
            self.biome_list.delete(0, "end")
            for name in normalized:
                self.biome_list.insert("end", name)
            if selected_index is not None and normalized:
                self.biome_list.selection_set(
                    min(selected_index, len(normalized) - 1)
                )
        self.biome_pending[self.loaded_biome_key] = list(normalized)

    def sync_surface_subregions_for_main_edit(
        self,
        parameters: dict[str, Any],
        old_main_names: list[str],
        new_main_names: list[str],
        old_settings: list[dict[str, Any] | None],
        new_settings: list[dict[str, Any] | None],
        action: str,
        index: int,
        target: int | None = None,
    ) -> None:
        """Move the paired Sub slot together with an edited Surface Main row."""

        key = (id(parameters), "surface", "subregions")
        names = self.biome_pending.get(key)
        if names is None:
            names = [
                str(region.get("biome", ""))
                for region in swe.celestial_biome_region_objects(
                    parameters, "surface", "subregions"
                )
            ]
        names, _added, _removed, _forced = (
            swe.normalize_uniform_ocean_subregion_names(
                old_main_names, names, old_settings
            )
        )
        if action == "insert":
            names.insert(index, new_main_names[index])
        elif action == "delete":
            if index < len(names):
                names.pop(index)
        elif action == "move":
            if target is None:
                raise ValueError("A paired Main-region move needs a target index")
            names.insert(target, names.pop(index))
        else:
            raise ValueError(f"Unknown Main-region edit action: {action}")
        normalized, _added, _removed, _forced = (
            swe.normalize_uniform_ocean_subregion_names(
                new_main_names, names, new_settings
            )
        )
        self.biome_pending[key] = normalized

    def refresh_biome_edit_buttons(self) -> None:
        if not hasattr(self, "remove_biome_button"):
            return
        selection = self.biome_list.curselection()
        index = selection[0] if selection else None
        locks = self.current_uniform_ocean_subregion_locks()
        locked = index in locks if index is not None else False
        size = self.biome_list.size()
        can_remove = index is not None and size > 1 and not locked
        can_up = (
            index is not None
            and index > 0
            and not locked
            and index - 1 not in locks
        )
        can_down = (
            index is not None
            and index + 1 < size
            and not locked
            and index + 1 not in locks
        )
        self.remove_biome_button.configure(
            state="normal" if can_remove else "disabled"
        )
        self.move_up_button.configure(state="normal" if can_up else "disabled")
        self.move_down_button.configure(
            state="normal" if can_down else "disabled"
        )

    def refresh_ocean_floor_button(self) -> None:
        self.refresh_biome_edit_buttons()
        if not hasattr(self, "ocean_floor_settings_button"):
            return
        state = self.current_surface_pair_state()
        selection = self.biome_list.curselection() if hasattr(self, "biome_list") else ()
        enabled = False
        if state is not None and selection:
            name = str(self.biome_list.get(selection[0]))
            pair = swe.ocean_biome_pair(name)
            enabled = pair is not None and pair[1] == name.strip().casefold()
        if enabled:
            self.ocean_floor_settings_button.grid()
            self.ocean_floor_settings_button.configure(state="normal")
        else:
            self.ocean_floor_settings_button.grid_remove()

    def ocean_pair_can_apply_to_current_world(
        self,
        parameters: dict[str, Any],
        planet_orbit: int,
        moon_orbit: int,
        world: Path,
    ) -> bool:
        """Return whether a pending floor can safely modify the current .world.

        A coordinate/orbit move or a changed Celestial generation signature
        means that the existing file is about to be replaced.  In that case we
        keep the pair pending and apply it only after Starbound creates the new
        world, rather than rebuilding a stale file that will immediately be
        discarded.
        """

        if (
            self.editor is None
            or self.original_editor is None
            or not Path(world).is_file()
            or list(self.editor.get("location", []))
            != list(self.original_editor.get("location", []))
        ):
            return False
        origin = self.body_origins.get(id(parameters))
        if origin != (int(planet_orbit), int(moon_orbit)):
            return False
        originals = self.parameters_by_coordinate(self.original_editor)
        original = originals.get(origin)
        if original is None:
            return False
        if self.biome_signature(parameters) != self.biome_signature(original):
            return False
        return swe.generated_world_matches_celestial_type(parameters, Path(world))

    def edit_ocean_floor_pair(self) -> None:
        state = self.current_surface_pair_state()
        selection = self.biome_list.curselection()
        if state is None or not selection:
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("ocean_floor_unavailable"),
                parent=self.root,
            )
            return
        parameters, settings = state
        index = int(selection[0])
        name = str(self.biome_list.get(index)).strip()
        pair = swe.ocean_biome_pair(name)
        if pair is None or pair[1] != name.casefold():
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("ocean_floor_unavailable"),
                parent=self.root,
            )
            return
        current = settings[index] or self.default_ocean_floor_setting(name)
        assert current is not None
        (
            default_floor_bottom,
            default_floor_height,
            default_transition,
        ) = self.ocean_pair_default_heights(parameters)
        displayed_floor_bottom = current.get("floorBottomY")
        if displayed_floor_bottom is None:
            displayed_floor_bottom = default_floor_bottom
            # Sidecars from earlier builds used an explicitly lowered terrain
            # height as the implicit floor-biome bottom.  Show that effective
            # legacy value so saving this dialog migrates it without changing
            # the generated vertical band.
            legacy_floor_height = current.get("floorHeight")
            if legacy_floor_height is not None:
                displayed_floor_bottom = (
                    float(legacy_floor_height)
                    if displayed_floor_bottom is None
                    else min(
                        float(displayed_floor_bottom),
                        float(legacy_floor_height),
                    )
                )
        displayed_floor_height = current.get("floorHeight")
        if displayed_floor_height is None:
            displayed_floor_height = default_floor_height
        displayed_transition = current.get("transitionY")
        if displayed_transition is None:
            displayed_transition = default_transition

        window = tk.Toplevel(self.root)
        window.title(self.tr("ocean_floor_title"))
        window.geometry("800x720")
        window.minsize(660, 620)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(10, weight=1)
        frame.rowconfigure(12, weight=1)

        floor_var = tk.StringVar(value=str(current.get("floorBiome", pair[0])))
        bottom_var = tk.StringVar(
            value=self.display_number(displayed_floor_bottom)
        )
        height_var = tk.StringVar(
            value=self.display_number(displayed_floor_height)
        )
        transition_var = tk.StringVar(
            value=self.display_number(displayed_transition)
        )
        ttk.Label(frame, text=self.tr("ocean_surface_biome")).grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4
        )
        # This display-only value is not captured by the save callbacks. A local
        # StringVar would be collected after opening the dialog and clear it.
        surface_entry = ttk.Entry(frame)
        surface_entry.insert(0, pair[1])
        surface_entry.configure(state="readonly")
        surface_entry.grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Label(frame, text=self.tr("ocean_floor_biome")).grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4
        )
        floor_values = self.available_biomes or [pair[0]]
        ttk.Combobox(
            frame, textvariable=floor_var, values=floor_values, state="readonly"
        ).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text=self.tr("ocean_floor_bottom")).grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=bottom_var).grid(
            row=2, column=1, sticky="ew", pady=4
        )
        ttk.Label(frame, text=self.tr("ocean_floor_pair_height")).grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=height_var).grid(
            row=3, column=1, sticky="ew", pady=4
        )
        ttk.Label(frame, text=self.tr("ocean_floor_transition")).grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=transition_var).grid(
            row=4, column=1, sticky="ew", pady=4
        )
        uniform_var = tk.BooleanVar(
            value=bool(current.get("disableSubRegions", True))
        )
        coastal_slope_var = tk.BooleanVar(
            value=bool(current.get("coastalSlopeEnabled", True))
        )
        controls = normalize_ocean_floor_controls(
            bool(coastal_slope_var.get()), current.get("coastalSlopeStep", 32),
            current.get("floorEcologyHeight", swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT),
        )
        coastal_step_var = tk.StringVar(value=str(controls["coastalSlopeStep"]))
        ecology_height_var = tk.StringVar(value=str(controls["floorEcologyHeight"]))
        ttk.Checkbutton(
            frame,
            text=self.tr("ocean_disable_subregions"),
            variable=uniform_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 8))
        ttk.Checkbutton(
            frame,
            text=self.tr("ocean_coastal_slope"),
            variable=coastal_slope_var,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(frame, text=self.tr("ocean_coastal_slope_step")).grid(
            row=7, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=coastal_step_var).grid(
            row=7, column=1, sticky="ew", pady=4
        )
        ttk.Label(frame, text=self.tr("ocean_floor_ecology_height")).grid(
            row=8, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=ecology_height_var).grid(
            row=8, column=1, sticky="ew", pady=4
        )
        ttk.Label(frame, text=self.tr("ocean_floor_biome_overrides")).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(10, 4)
        )
        biome_text = tk.Text(frame, height=4, wrap="none")
        biome_text.grid(row=10, column=0, columnspan=2, sticky="nsew")
        biome_text.insert(
            "1.0",
            json.dumps(
                current.get("biomeOverrides", {}), ensure_ascii=False, indent=2
            ),
        )
        ttk.Label(frame, text=self.tr("ocean_floor_region_overrides")).grid(
            row=11, column=0, columnspan=2, sticky="w", pady=(10, 4)
        )
        region_text = tk.Text(frame, height=4, wrap="none")
        region_text.grid(row=12, column=0, columnspan=2, sticky="nsew")
        region_text.insert(
            "1.0",
            json.dumps(
                current.get("regionOverrides", {}), ensure_ascii=False, indent=2
            ),
        )
        build_scrollable_ocean_hint(
            frame, self.ocean_floor_editor_hint("ocean_floor_pair_hint"),
        ).grid(row=13, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        def stage_pair() -> bool:
            try:
                floor_name = floor_var.get().strip()
                if not floor_name:
                    raise ValueError(self.tr("ocean_floor_biome"))
                floor_bottom = (
                    int(round(float(bottom_var.get().strip())))
                    if bottom_var.get().strip()
                    else None
                )
                floor_height = (
                    float(height_var.get().strip())
                    if height_var.get().strip()
                    else None
                )
                transition = (
                    int(round(float(transition_var.get().strip())))
                    if transition_var.get().strip()
                    else None
                )
                coastal_step = int(coastal_step_var.get().strip())
                ecology_height = int(ecology_height_var.get().strip() or swe.OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT)
                controls = normalize_ocean_floor_controls(
                    bool(coastal_slope_var.get()), coastal_step, ecology_height,
                )
                coastal_step = controls["coastalSlopeStep"]
                ecology_height = controls["floorEcologyHeight"]
                biome_overrides = parse_field_json(
                    biome_text.get("1.0", "end").strip() or "{}",
                    self.tr("ocean_floor_biome_overrides"),
                )
                region_overrides = parse_field_json(
                    region_text.get("1.0", "end").strip() or "{}",
                    self.tr("ocean_floor_region_overrides"),
                )
                if not isinstance(biome_overrides, dict):
                    raise ValueError(self.tr("ocean_floor_biome_overrides"))
                if not isinstance(region_overrides, dict):
                    raise ValueError(self.tr("ocean_floor_region_overrides"))
                swe.validate_finite_json_numbers(
                    biome_overrides, self.tr("ocean_floor_biome_overrides")
                )
                swe.validate_finite_json_numbers(
                    region_overrides, self.tr("ocean_floor_region_overrides")
                )
                value = {
                    "surfaceBiome": pair[1],
                    "floorBiome": floor_name,
                    "floorBottomY": floor_bottom,
                    "floorHeight": floor_height,
                    "transitionY": transition,
                    "disableSubRegions": bool(uniform_var.get()),
                    "coastalSlopeEnabled": bool(coastal_slope_var.get()),
                    "coastalSlopeStep": coastal_step,
                    "floorEcologyHeight": ecology_height,
                    "biomeOverrides": biome_overrides,
                    "regionOverrides": region_overrides,
                }
                value = swe.normalize_ocean_floor_pair_settings([value])[0]
                ecology_height_var.set(str(value["floorEcologyHeight"]))
            except Exception as exc:
                messagebox.showerror(
                    self.tr("operation_failed"), str(exc), parent=window
                )
                return False
            settings[index] = value
            self.ocean_pair_settings[id(parameters)] = settings
            swe.align_surface_ocean_subregions(parameters, settings)
            self.ocean_pair_pending[id(parameters)] = True
            return True

        def save_pair() -> None:
            if not stage_pair():
                return
            messagebox.showinfo(
                self.tr("ocean_floor_title"),
                self.tr("ocean_floor_saved"),
                parent=window,
            )
            window.destroy()

        def apply_pair_now() -> None:
            if not stage_pair():
                return
            reference = self.selected_reference()
            if reference is None or self.editor is None:
                messagebox.showerror(
                    self.tr("operation_failed"),
                    self.tr("body_not_selected"),
                    parent=window,
                )
                return
            kind, planet_entry, moon_entry = reference
            planet_orbit = int(planet_entry[0])
            moon_orbit = (
                int(moon_entry[0]) if kind == "moon" and moon_entry else 0
            )
            world = self.body_world_path(planet_orbit, moon_orbit)
            if not self.ocean_pair_can_apply_to_current_world(
                parameters, planet_orbit, moon_orbit, world
            ):
                messagebox.showinfo(
                    self.tr("ocean_floor_title"),
                    self.tr("ocean_floor_apply_after_regeneration"),
                    parent=window,
                )
                window.destroy()
                return
            assets = Path(self.assets_var.get().strip())
            if not assets.exists():
                messagebox.showerror(
                    self.tr("operation_failed"),
                    self.tr("region_assets_required"),
                    parent=window,
                )
                return
            pairs = self.serialized_ocean_pairs(parameters)
            location = list(self.editor["location"])
            key = swe.ocean_pair_body_key(location, planet_orbit, moon_orbit)
            store = json_clone(self.ocean_pair_store)
            bodies = store.setdefault("bodies", {})
            if not isinstance(bodies, dict):
                bodies = {}
                store["bodies"] = bodies
            bodies[key] = {"pairs": pairs, "pending": False}
            universe_folder = Path(self.chunks_var.get().strip())
            if not self.ask_yes_no(
                self.tr("ocean_apply_title"),
                self.tr("ocean_floor_apply_current_message", world=world),
            ):
                self.audit_log(
                    "single_floor_apply_cancelled",
                    universe_folder=universe_folder,
                    world=world,
                    bodyKey=key,
                    pairs=pairs,
                )
                return
            self.audit_log(
                "single_floor_apply_confirmed",
                universe_folder=universe_folder,
                world=world,
                bodyKey=key,
                pairs=pairs,
            )
            window.destroy()

            def task():
                try:
                    backup, summary = ocean_flora_completion.apply_pairs_in_place(
                        world,
                        assets,
                        pairs,
                        progress=lambda event, details: self.report_ocean_generation_progress(
                            event, details, log_prefix="ocean_core_",
                            universe_folder=universe_folder,
                            targetWorld=world,
                            bodyKey=key,
                        ),
                    )
                    swe.save_ocean_pair_sidecar(
                        universe_folder, store
                    )
                    self.audit_log(
                        "single_floor_apply_world_completed",
                        universe_folder=universe_folder,
                        world=world,
                        bodyKey=key,
                        backup=backup,
                        summary=summary,
                    )
                except Exception as exc:
                    self.audit_exception(
                        "single_floor_apply_failed",
                        exc,
                        universe_folder=universe_folder,
                        world=world,
                        bodyKey=key,
                    )
                    if "backup" in locals() and backup is not None and backup.exists():
                        shutil.copy2(backup, world)
                    raise
                return backup, summary, store

            def success(result):
                backup, summary, saved_store = result
                self.ocean_pair_store = saved_store
                self.ocean_pair_pending[id(parameters)] = False
                self.audit_log(
                    "single_floor_apply_completed",
                    universe_folder=universe_folder,
                    world=world,
                    bodyKey=key,
                    backup=backup,
                    summary=summary,
                )
                self.set_status(
                    self.tr(
                        "ocean_floor_apply_current_done",
                        regions=len(summary.get("rectangles", [])),
                        sectors=int(summary.get("resetSectors", 0)),
                        backup=backup,
                    )
                )
                messagebox.showinfo(
                    self.tr("ocean_floor_title"),
                    self.tr(
                        "ocean_floor_apply_current_done",
                        regions=len(summary.get("rectangles", [])),
                        sectors=int(summary.get("resetSectors", 0)),
                        backup=backup,
                    ),
                    parent=self.root,
                )

            self.run_worker(
                task, success, "ocean_floor_apply_current_busy"
            )

        button_frame = ttk.Frame(frame)
        button_frame.grid(
            row=14, column=0, columnspan=2, sticky="e", pady=(10, 0)
        )
        ttk.Button(
            button_frame, text=self.tr("save_ocean_floor"), command=save_pair
        ).pack(side="left")
        ttk.Button(
            button_frame,
            text=self.tr("apply_ocean_floor_now"),
            command=apply_pair_now,
        ).pack(side="left", padx=(8, 0))

    def add_biome(self) -> None:
        selected_label = self.biome_name_var.get().strip()
        name = self.biome_display_to_name.get(selected_label, selected_label)
        if name:
            selection = self.biome_list.curselection()
            index = selection[0] + 1 if selection else self.biome_list.size()
            state = self.current_surface_pair_state()
            locks = self.current_uniform_ocean_subregion_locks()
            if index in locks:
                index += 1
            old_main_names = [
                str(self.biome_list.get(i))
                for i in range(self.biome_list.size())
            ]
            old_settings = json_clone(state[1]) if state is not None else []
            self.biome_list.insert(index, name)
            if state is not None:
                parameters, settings = state
                settings.insert(index, self.default_ocean_floor_setting(name))
                self.ocean_pair_settings[id(parameters)] = settings
                self.ocean_pair_pending[id(parameters)] = True
                new_main_names = [
                    str(self.biome_list.get(i))
                    for i in range(self.biome_list.size())
                ]
                self.sync_surface_subregions_for_main_edit(
                    parameters,
                    old_main_names,
                    new_main_names,
                    old_settings,
                    settings,
                    "insert",
                    index,
                )
            else:
                self.normalize_loaded_surface_subregions()
            self.biome_list.selection_clear(0, "end")
            if self.biome_list.size():
                self.biome_list.selection_set(
                    min(index, self.biome_list.size() - 1)
                )
            self.refresh_ocean_floor_button()

    def edit_biome_species(self) -> None:
        reference = self.selected_reference()
        selection = self.biome_list.curselection()
        if reference is None or not selection:
            messagebox.showerror(
                self.tr("operation_failed"), self.tr("body_not_selected"), parent=self.root
            )
            return
        kind, planet_entry, moon_entry = reference
        planet_orbit = int(planet_entry[0])
        moon_orbit = int(moon_entry[0]) if kind == "moon" and moon_entry else 0
        world = self.body_world_path(planet_orbit, moon_orbit)
        if not world.is_file():
            messagebox.showinfo(
                self.tr("species_title"), self.tr("species_requires_world"), parent=self.root
            )
            return
        biome_name = str(self.biome_list.get(selection[0]))
        try:
            self.assert_direct_world_edit_safe(world)
            world_edit_guard.assert_world_available(world)
            opened_fingerprint = world_edit_guard.fingerprint(world)
            profile = swe.world_biome_spawn_profile(world, biome_name)
            world_edit_guard.assert_world_unchanged(world, opened_fingerprint)
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return

        window = tk.Toplevel(self.root)
        window.title(self.tr("species_title"))
        window.geometry("700x500")
        window.minsize(560, 400)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)
        ttk.Label(frame, text=f"{biome_name} — {self.tr('spawn_types')}").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        spawn_text = tk.Text(frame, height=9, wrap="none")
        spawn_text.grid(row=1, column=0, sticky="nsew")
        spawn_text.insert("1.0", "\n".join(str(v) for v in profile.get("spawnTypes", [])))
        ttk.Label(frame, text=self.tr("monster_parameters")).grid(
            row=2, column=0, sticky="w", pady=(10, 4)
        )
        parameter_text = tk.Text(frame, height=9, wrap="none")
        parameter_text.grid(row=3, column=0, sticky="nsew")
        parameter_text.insert(
            "1.0",
            json.dumps(
                profile.get("monsterParameters")
                if isinstance(profile.get("monsterParameters"), dict)
                else {},
                ensure_ascii=False,
                indent=2,
            ),
        )
        ttk.Label(
            frame,
            text=self.tr("monster_parameters_hint"),
            style="Hint.TLabel",
            wraplength=650,
        ).grid(row=4, column=0, sticky="w", pady=(6, 0))

        def save_species() -> None:
            try:
                spawn_input = spawn_text.get("1.0", "end")
                parameter_input = parameter_text.get("1.0", "end")
                if not spawn_input.strip() and not parameter_input.strip():
                    messagebox.showinfo(
                        self.tr("species_title"), self.tr("region_spawns_no_change"),
                        parent=window,
                    )
                    window.destroy()
                    return
                spawn_types = [
                    line.strip()
                    for line in spawn_input.splitlines()
                    if line.strip()
                ]
                parameters = parse_field_json(parameter_input.strip() or "{}", self.tr("monster_parameters"))
                if not isinstance(parameters, dict):
                    raise ValueError(self.tr("monster_parameters"))
                self._resource_preflight().validate_spawn_profile(
                    {"spawnTypes": spawn_types, "monsterParameters": parameters},
                    label=self.tr("species_title"),
                )
                self.assert_direct_world_edit_safe(world)
                backup, count = swe.write_world_biome_spawn_profile_in_place(
                    world, biome_name, spawn_types, parameters,
                    expected_fingerprint=opened_fingerprint,
                )
            except Exception as exc:
                messagebox.showerror(self.tr("operation_failed"), str(exc), parent=window)
                return
            messagebox.showinfo(
                self.tr("species_title"),
                (self.tr("species_saved", count=count, biome=biome_name, backup=backup)
                 if count else self.tr("region_spawns_no_change")),
                parent=window,
            )
            window.destroy()

        ttk.Button(frame, text=self.tr("save_species"), command=save_species).grid(
            row=5, column=0, sticky="e", pady=(10, 0)
        )

    def remove_biome(self) -> None:
        selection = self.biome_list.curselection()
        if selection and self.biome_list.size() > 1:
            locks = self.current_uniform_ocean_subregion_locks()
            if selection[0] in locks:
                self.refresh_ocean_floor_button()
                return
            state = self.current_surface_pair_state()
            old_main_names = [
                str(self.biome_list.get(i))
                for i in range(self.biome_list.size())
            ]
            old_settings = json_clone(state[1]) if state is not None else []
            if state is not None:
                parameters, settings = state
                if selection[0] < len(settings):
                    settings.pop(selection[0])
                self.ocean_pair_settings[id(parameters)] = settings
                self.ocean_pair_pending[id(parameters)] = True
            self.biome_list.delete(selection[0])
            if state is not None:
                new_main_names = [
                    str(self.biome_list.get(i))
                    for i in range(self.biome_list.size())
                ]
                self.sync_surface_subregions_for_main_edit(
                    parameters,
                    old_main_names,
                    new_main_names,
                    old_settings,
                    settings,
                    "delete",
                    selection[0],
                )
            else:
                self.normalize_loaded_surface_subregions()
            self.refresh_ocean_floor_button()

    def move_biome(self, delta: int) -> None:
        selection = self.biome_list.curselection()
        if not selection:
            return
        index = selection[0]
        target = index + delta
        if target < 0 or target >= self.biome_list.size():
            return
        locks = self.current_uniform_ocean_subregion_locks()
        if index in locks or target in locks:
            self.refresh_ocean_floor_button()
            return
        state = self.current_surface_pair_state()
        old_main_names = [
            str(self.biome_list.get(i)) for i in range(self.biome_list.size())
        ]
        old_settings = json_clone(state[1]) if state is not None else []
        value = self.biome_list.get(index)
        self.biome_list.delete(index)
        self.biome_list.insert(target, value)
        self.biome_list.selection_set(target)
        if state is not None:
            parameters, settings = state
            moved = settings.pop(index)
            settings.insert(target, moved)
            self.ocean_pair_settings[id(parameters)] = settings
            self.ocean_pair_pending[id(parameters)] = True
            new_main_names = [
                str(self.biome_list.get(i))
                for i in range(self.biome_list.size())
            ]
            self.sync_surface_subregions_for_main_edit(
                parameters,
                old_main_names,
                new_main_names,
                old_settings,
                settings,
                "move",
                index,
                target,
            )
        else:
            self.normalize_loaded_surface_subregions()
        self.refresh_ocean_floor_button()

    def apply_biome_order(self) -> None:
        try:
            self.store_loaded_range_fields(strict=True)
            self.store_loaded_biome_list()
        except Exception as exc:
            messagebox.showerror(
                self.tr("operation_failed"), str(exc), parent=self.root
            )
            return
        if self.loaded_biome_key is None:
            messagebox.showerror(
                self.tr("operation_failed"), self.tr("body_not_selected"), parent=self.root
            )
            return
        parameter_id = self.loaded_biome_key[0]
        selected = {
            key for key in self.biome_pending if key[0] == parameter_id
        }
        try:
            adjustments = self.apply_pending_biomes(selected)
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        # An explicit same-order Apply also repairs already-generated worlds;
        # merely visiting this page or saving display metadata does not opt in.
        applied = getattr(self, "ordered_parallax_applied", {})
        applied.setdefault(parameter_id, set()).update(key[1] for key in selected)
        self.ordered_parallax_applied = applied
        iid = self.current_body_iid
        self.rebuild_body_tree(iid)
        self.refresh_biome_context()
        self.status_var.set(self.tr("biome_all_applied"))
        if adjustments:
            lines = []
            for adjustment in adjustments:
                if adjustment["action"] == "ocean_locked":
                    message_key = "uniform_ocean_slots_restored"
                elif adjustment["action"] == "filled":
                    message_key = "subregions_filled"
                else:
                    message_key = "subregions_trimmed"
                lines.append(
                    self.tr(
                        message_key,
                        layer=adjustment["layer"],
                        biomes=", ".join(adjustment["biomes"]),
                    )
                )
            messagebox.showinfo(
                self.tr("subregion_pairing_title"),
                "\n".join(lines),
                parent=self.root,
            )

    def parameters_by_identity(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        if self.editor is None:
            return result
        for planet_entry in self.editor["planets"]:
            parameters = planet_entry[1]["parameters"]
            result[id(parameters)] = parameters
            for moon_entry in planet_entry[1].get("satellites") or []:
                result[id(moon_entry[1])] = moon_entry[1]
        return result

    def apply_pending_biomes(
        self, selected: set[tuple[int, str, str]] | None = None
    ) -> list[dict[str, Any]]:
        parameters_map = self.parameters_by_identity()
        assets_text = self.assets_var.get().strip()
        assets = Path(assets_text) if assets_text else None
        keys = [
            key
            for key in self.biome_pending
            if selected is None or key in selected
        ]
        adjustments: list[dict[str, Any]] = []
        # Prepare the complete selected batch before consuming any pending key
        # or mutating a body. A later invalid biome must not commit earlier
        # planets, including their ocean settings/dirty flags.
        staged_bodies: list[tuple[dict[str, Any], dict[str, Any]]] = []
        pair_state = SimpleNamespace(
            ocean_pair_settings=dict(self.ocean_pair_settings),
            default_ocean_floor_setting=self.default_ocean_floor_setting,
        )
        staged_pending = dict(self.ocean_pair_pending)
        parameter_ids = list(dict.fromkeys(key[0] for key in keys))
        for parameter_id in parameter_ids:
            parameters = parameters_map.get(parameter_id)
            if parameters is None:
                continue
            # The ordering helpers replace layer fields/arrays, never mutate
            # existing region recipes. Copy those writable containers only;
            # do not clone a generated world's potentially huge metadata or
            # unrelated biome payloads. Pairing repair can touch every layer.
            scratch = dict(parameters)
            raw = parameters.get("parameters")
            if isinstance(raw, dict):
                scratch["parameters"] = dict(raw)
            visitable = parameters.get("visitableParameters")
            if isinstance(visitable, dict):
                staged_visitable = dict(visitable)
                for layer_key in (
                    "spaceLayer", "atmosphereLayer", "surfaceLayer",
                    "subsurfaceLayer", "coreLayer",
                ):
                    layer = visitable.get(layer_key)
                    if isinstance(layer, dict):
                        staged_visitable[layer_key] = dict(layer)
                underground = visitable.get("undergroundLayers")
                if isinstance(underground, list):
                    staged_visitable["undergroundLayers"] = [
                        dict(layer) if isinstance(layer, dict) else layer
                        for layer in underground
                    ]
                scratch["visitableParameters"] = staged_visitable
            layer_labels = {
                str(group["layerId"]): str(group["layerLabel"])
                for group in swe.celestial_layer_groups(scratch)
            }

            def layer_rank(layer_id: str) -> tuple[int, int]:
                if layer_id == "surface":
                    return (0, 0)
                if layer_id == "subsurface":
                    return (1, 0)
                if layer_id.startswith("underground:"):
                    try:
                        return (2, int(layer_id.split(":", 1)[1]))
                    except ValueError:
                        return (2, 999)
                return ({"core": 3, "atmosphere": 4, "space": 5}.get(layer_id, 6), 0)

            body_keys = sorted(
                (key for key in keys if key[0] == parameter_id),
                key=lambda key: (
                    layer_rank(key[1]),
                    0 if key[2] == "regions" else 1,
                ),
            )
            ocean_adjustment_recorded = False
            for key in body_keys:
                _parameter_id, layer_id, group = key
                names = list(self.biome_pending[key])
                if group == "subregions":
                    main_names = [
                        str(region.get("biome", ""))
                        for region in swe.celestial_biome_region_objects(
                            scratch, layer_id, "regions"
                        )
                    ]
                    forced: list[dict[str, Any]] = []
                    if layer_id == "surface":
                        pair_settings = CelestialSystemEditorWindow.ensure_ocean_pair_settings(
                            pair_state, parameters, main_names
                        )
                        names, added, removed, forced = (
                            swe.normalize_uniform_ocean_subregion_names(
                                main_names, names, pair_settings
                            )
                        )
                    else:
                        names, added, removed = (
                            swe.normalize_celestial_subregion_names(
                                main_names, names
                            )
                        )
                    if added:
                        adjustments.append(
                            {
                                "layer": layer_labels.get(layer_id, layer_id),
                                "action": "filled",
                                "biomes": added,
                            }
                        )
                    if removed:
                        adjustments.append(
                            {
                                "layer": layer_labels.get(layer_id, layer_id),
                                "action": "trimmed",
                                "biomes": removed,
                            }
                        )
                    if forced:
                        if layer_id == "surface":
                            ocean_adjustment_recorded = True
                        adjustments.append(
                            {
                                "layer": layer_labels.get(layer_id, layer_id),
                                "action": "ocean_locked",
                                "biomes": [
                                    f'{int(item["index"]) + 1}: '
                                    f'{item["biome"]}'
                                    for item in forced
                                ],
                            }
                        )
                swe.set_celestial_biome_order(
                    scratch,
                    layer_id,
                    group,
                    names,
                    assets,
                )
            swe.validate_celestial_region_pairings(scratch)
            current_surface_names = self.surface_biome_names(scratch)
            if current_surface_names:
                pair_settings = CelestialSystemEditorWindow.ensure_ocean_pair_settings(
                    pair_state, parameters, current_surface_names
                )
                if any(isinstance(item, dict) for item in pair_settings):
                    changed_ocean_slots = swe.align_surface_ocean_subregions(
                        scratch, pair_settings
                    )
                    if changed_ocean_slots and not ocean_adjustment_recorded:
                        adjustments.append(
                            {
                                "layer": layer_labels.get("surface", "surface"),
                                "action": "ocean_locked",
                                "biomes": [
                                    f"{index + 1}: {current_surface_names[index]}"
                                    for index in changed_ocean_slots
                                ],
                            }
                        )
                    # Reapplying an identical order may request only the
                    # parallax repair. Do not re-arm an already finished ocean
                    # unless the recipe changed or a pairing edit is pending.
                    staged_pending[parameter_id] = bool(
                        staged_pending.get(parameter_id, False) or scratch != parameters
                    )
                else:
                    staged_pending[parameter_id] = False
            staged_bodies.append((parameters, scratch))
        # No validation or asset lookup remains below this commit boundary.
        # Keep parameter and pending-map identities used by other open pages.
        for parameters, scratch in staged_bodies:
            parameters.clear()
            parameters.update(scratch)
        self.ocean_pair_settings.clear()
        self.ocean_pair_settings.update(pair_state.ocean_pair_settings)
        self.ocean_pair_pending.clear()
        self.ocean_pair_pending.update(staged_pending)
        for key in keys:
            self.biome_pending.pop(key, None)
        return adjustments

    def enforce_all_uniform_ocean_subregions(self) -> dict[int, list[int]]:
        """Reapply every hard ocean lock before serializing a system save."""

        changed: dict[int, list[int]] = {}
        for _planet_orbit, _moon_orbit, parameters in self.iter_body_parameters():
            names = self.surface_biome_names(parameters)
            if not names:
                continue
            parameter_id = id(parameters)
            settings = self.ensure_ocean_pair_settings(parameters, names)
            if not any(isinstance(item, dict) for item in settings):
                self.ocean_pair_pending[parameter_id] = False
                continue
            indexes = swe.align_surface_ocean_subregions(parameters, settings)
            if indexes:
                changed[parameter_id] = indexes
                self.ocean_pair_pending[parameter_id] = True
        return changed

    def apply_body_fields(self) -> None:
        """The button owns UI errors; internal Save/switch callers still raise."""
        try:
            self.apply_current_body()
        except Exception as exc:
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            self.set_status_key("failed_status", error=str(exc))
        else:
            # The no-selection path already shows its own error and returns.
            # A valid Apply only updates the editor model, never the save files.
            if self.editor is not None and self.current_body_iid:
                self.set_status_key("body_applied_unsaved")

    def apply_current_body(self, rebuild: bool = True, silent: bool = False) -> None:
        if self.editor is None or not self.current_body_iid:
            if not silent:
                messagebox.showerror(
                    self.tr("operation_failed"), self.tr("body_not_selected"), parent=self.root
                )
            return
        reference = self.body_refs[self.current_body_iid]
        kind, planet_entry, moon_entry = reference
        try:
            planet_orbit = int(self.planet_orbit_var.get().strip())
        except ValueError as exc:
            raise ValueError(
                self.tr("invalid_integer", field=self.tr("planet_orbit"))
            ) from exc
        if not 1 <= planet_orbit <= 11:
            raise ValueError(self.tr("integer_range", field=self.tr("planet_orbit"), minimum=1, maximum=11))
        moon_orbit = 0
        if kind == "moon":
            try:
                moon_orbit = int(self.moon_orbit_var.get().strip())
            except ValueError as exc:
                raise ValueError(
                    self.tr("invalid_integer", field=self.tr("moon_orbit"))
                ) from exc
            if not 1 <= moon_orbit <= 3:
                raise ValueError(self.tr("integer_range", field=self.tr("moon_orbit"), minimum=1, maximum=3))
        for other in self.editor["planets"]:
            if other is not planet_entry and int(other[0]) == planet_orbit:
                raise ValueError(self.tr("orbit_used", orbit=planet_orbit))
        if kind == "moon":
            for other in planet_entry[1].get("satellites", []):
                if other is not moon_entry and int(other[0]) == moon_orbit:
                    raise ValueError(self.tr("orbit_used", orbit=f"{planet_orbit}.{moon_orbit}"))
        name = self.body_name_var.get().strip()
        if not name:
            raise ValueError(self.tr("required_field", field=self.tr("body_name")))
        old_planet_orbit = int(planet_entry[0])
        old_moon_orbit = int(moon_entry[0]) if moon_entry is not None else 0
        # Parse the entire body form before committing either this page or the
        # pending World parameters page.  A late invalid scale/name/hue must
        # not leave an orbit move, station edit, or weather change behind.
        station_updates = []
        if self.system_file_state is not None:
            for obj in self.system_file_state.get("body", {}).get("objects", []):
                target = (obj.get("orbit") or {}).get("target")
                if not isinstance(target, dict):
                    continue
                target_planet = int(target.get("planet", 0))
                target_moon = int(target.get("satellite", 0))
                if target_planet == old_planet_orbit:
                    # The planet-orbit field moves the parent entry even when
                    # its moon is selected.  Every sibling and parent station
                    # follows that move; only the selected moon is renumbered.
                    new_moon = (moon_orbit if kind == "moon"
                                and target_moon == old_moon_orbit else target_moon)
                    station_updates.append((target, planet_orbit, new_moon))
        parameters = self.body_parameters(reference)
        raw = dict(parameters.get("parameters") or {})
        original_visitable = parameters.get("visitableParameters")
        visitable = (dict(original_visitable)
                     if isinstance(original_visitable, dict) else None)

        label_text = self.body_label_var.get().strip()
        if isinstance(visitable, dict):
            if label_text:
                visitable["typeName"] = label_text
            else:
                visitable.pop("typeName", None)
        for key, variable in (
            ("description", self.body_description_var),
            ("smallImage", self.small_image_var),
        ):
            value = variable.get().strip()
            if value:
                raw[key] = value
            else:
                raw.pop(key, None)
        scale_text = self.small_image_scale_var.get().strip()
        if scale_text:
            try:
                scale = native_float32(scale_text, positive=True)
                raw["smallImageScale"] = scale
            except ValueError as exc:
                raise ValueError(
                    self.tr("invalid_number", field=self.tr("small_image_scale"))
                ) from exc
        else:
            raw.pop("smallImageScale", None)
        if str(raw.get("worldType", "")) == "FloatingDungeon":
            image_text = self.large_image_var.get().strip()
            if image_text:
                raw["image"] = image_text
            else:
                raw.pop("image", None)
        image_scale_text = self.large_image_scale_var.get().strip()
        if image_scale_text:
            try:
                image_scale = native_float32(image_scale_text, positive=True)
                raw["imageScale"] = image_scale
            except ValueError as exc:
                raise ValueError(
                    self.tr("invalid_number", field=self.tr("large_image_scale"))
                ) from exc
        else:
            raw.pop("imageScale", None)
        gravity_text = self.gravity_var.get().strip()
        if isinstance(visitable, dict):
            if not gravity_text:
                raise ValueError(
                    self.tr("invalid_number", field=self.tr("gravity"))
                )
            try:
                gravity = native_float32(
                    gravity_text, positive=raw.get("worldType") == "Terrestrial",
                    nonnegative=True,
                )
                visitable["gravity"] = gravity
            except ValueError as exc:
                raise ValueError(
                    self.tr("invalid_number", field=self.tr("gravity"))
                ) from exc
            hue_text = self.world_hue_shift_var.get().strip()
            if hue_text:
                try:
                    hue_shift = native_float32(hue_text)
                    visitable["hueShift"] = hue_shift
                except ValueError as exc:
                    raise ValueError(
                        self.tr("invalid_number", field=self.tr("world_hue_shift"))
                    ) from exc
            else:
                visitable.pop("hueShift", None)
            visitable["globalDirectives"] = parse_global_directives(self.global_directives_var.get())
        self._check_image_edits(raw, parameters.get("parameters") or {}, kind="body")
        self._store_world_parameter_fields()
        # Keep the parameter identity and any World parameters changes just
        # committed above.  Copy only this page's editable scalar keys, not a
        # stale shallow snapshot of the large world-generation structure.
        live_raw = parameters.setdefault("parameters", {})
        for key in ("description", "smallImage", "smallImageScale", "imageScale"):
            if key in raw:
                live_raw[key] = raw[key]
            else:
                live_raw.pop(key, None)
        if str(raw.get("worldType", "")) == "FloatingDungeon":
            if "image" in raw:
                live_raw["image"] = raw["image"]
            else:
                live_raw.pop("image", None)
        live_visitable = parameters.get("visitableParameters")
        if isinstance(visitable, dict) and isinstance(live_visitable, dict):
            for key in ("typeName", "gravity", "hueShift", "globalDirectives"):
                if key in visitable:
                    live_visitable[key] = visitable[key]
                else:
                    live_visitable.pop(key, None)
        parameters["name"] = name
        planet_entry[0] = planet_orbit
        if moon_entry is not None:
            moon_entry[0] = moon_orbit
        for target, new_planet, new_moon in station_updates:
            target["planet"] = new_planet
            target["satellite"] = new_moon
        if rebuild:
            iid = self.current_body_iid
            self.rebuild_body_tree(iid)
            self.rebuild_station_tree()
            self.refresh_biome_context()
            self.refresh_mpi_warning()

    def selected_reference(self) -> tuple[str, list[Any], list[Any] | None] | None:
        if self.current_body_iid:
            return self.body_refs.get(self.current_body_iid)
        return None

    def add_new_body(self, as_moon: bool) -> None:
        if self.editor is None:
            messagebox.showerror(self.tr("operation_failed"), self.tr("no_editor"), parent=self.root)
            return
        assets = Path(self.assets_var.get().strip())
        if not assets.exists():
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("new_body_assets_required"),
                parent=self.root,
            )
            return
        preset = self.new_body_type_map.get(self.new_body_type_var.get(), "planet")
        if as_moon and not swe.celestial_body_preset_supports_satellite(preset):
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("new_body_not_satellite"),
                parent=self.root,
            )
            return
        size_name = self.new_body_size_var.get().strip() or "medium"
        type_label = self.new_body_type_var.get()
        planet_entry: list[Any] | None = None
        if as_moon:
            reference = self.selected_reference()
            if reference is None:
                messagebox.showerror(
                    self.tr("operation_failed"),
                    self.tr("new_body_parent_required"),
                    parent=self.root,
                )
                return
            _kind, planet_entry, _moon_entry = reference
            satellites = planet_entry[1].setdefault("satellites", [])
            used = {int(entry[0]) for entry in satellites}
            satellite_orbit = next(
                (value for value in range(1, 4) if value not in used), None
            )
            if satellite_orbit is None:
                messagebox.showerror(
                    self.tr("operation_failed"),
                    self.tr("no_moon_orbit"),
                    parent=self.root,
                )
                return
            planet_orbit = int(planet_entry[0])
            parent_name = str(planet_entry[1]["parameters"].get("name", ""))
            suffix = chr(ord("a") + satellite_orbit - 1)
            body_name = f"{parent_name} ^white;- ^yellow;{suffix}^white;"
            display_orbit = f"{planet_orbit}.{satellite_orbit}"
        else:
            used = {int(entry[0]) for entry in self.editor["planets"]}
            planet_orbit = next(
                (value for value in range(1, 12) if value not in used), None
            )
            if planet_orbit is None:
                messagebox.showerror(
                    self.tr("operation_failed"),
                    self.tr("no_planet_orbit"),
                    parent=self.root,
                )
                return
            satellite_orbit = 0
            index = len(self.editor["planets"]) + 1
            roman = ROMAN.get(index, str(index))
            base_name = str(self.editor["systemParameters"].get("name", "New System"))
            body_name = f"{base_name} ^green;{roman}^white;"
            display_orbit = str(planet_orbit)

        location = list(self.editor["location"])

        def task():
            return swe.build_new_celestial_body(
                assets,
                preset,
                size_name,
                location,
                planet_orbit,
                satellite_orbit,
                name=body_name,
            )

        def success(parameters):
            if self.editor is None:
                return
            if as_moon:
                assert planet_entry is not None
                entry = [satellite_orbit, parameters]
                planet_entry[1].setdefault("satellites", []).append(entry)
                iid = f"m_{id(entry)}"
                status_key = "moon_added"
            else:
                entry = [planet_orbit, {"parameters": parameters, "satellites": []}]
                self.editor["planets"].append(entry)
                iid = f"p_{id(entry)}"
                status_key = "planet_added"
            self.body_origins[id(parameters)] = None
            self.rebuild_body_tree(iid)
            self.refresh_biome_context()
            self.refresh_mpi_warning()
            self.set_status_key(
                status_key, orbit=display_orbit, type=type_label
            )

        self.run_worker(task, success, "compiling_body")

    def clone_as_moon(self) -> None:
        """Compatibility alias retained for older launcher integrations."""
        self.add_new_body(True)

    def clone_as_planet(self) -> None:
        """Compatibility alias retained for older launcher integrations."""
        self.add_new_body(False)

    def rebuild_station_tree(self) -> None:
        if not hasattr(self, "station_tree"):
            return
        for iid in self.station_tree.get_children():
            self.station_tree.delete(iid)
        self.station_row_map = {}
        self.station_target_map = {}
        targets: list[str] = []
        center = self.tr("system_center")
        self.station_target_map[center] = (0, 0)
        targets.append(center)
        if self.editor is not None:
            for planet_entry in sorted(
                self.editor["planets"], key=lambda value: value[0]
            ):
                planet_orbit = int(planet_entry[0])
                planet_name = str(
                    planet_entry[1]["parameters"].get("name", "")
                )
                label = f"{planet_orbit} — {planet_name}"
                self.station_target_map[label] = (planet_orbit, 0)
                targets.append(label)
                for moon_entry in sorted(
                    planet_entry[1].get("satellites") or [],
                    key=lambda value: value[0],
                ):
                    moon_orbit = int(moon_entry[0])
                    moon_name = str(moon_entry[1].get("name", ""))
                    label = f"{planet_orbit}.{moon_orbit} — {moon_name}"
                    self.station_target_map[label] = (planet_orbit, moon_orbit)
                    targets.append(label)
        self.station_target_combo.configure(values=targets)
        if self.station_target_var.get() not in self.station_target_map:
            self.station_target_var.set(center)

        if self.system_file_state is None:
            return
        objects = self.system_file_state.get("body", {}).get("objects", [])
        if not isinstance(objects, list):
            return
        reverse_targets = {
            coordinate: label for label, coordinate in self.station_target_map.items()
        }
        for index, obj in enumerate(objects):
            if not isinstance(obj, dict):
                continue
            target_label = ""
            orbit = obj.get("orbit")
            if isinstance(orbit, dict):
                target = orbit.get("target")
                if isinstance(target, dict):
                    coordinate = (
                        int(target.get("planet", 0)),
                        int(target.get("satellite", 0)),
                    )
                    target_label = reverse_targets.get(
                        coordinate, f"{coordinate[0]}.{coordinate[1]}"
                    )
            iid = f"o_{index}_{id(obj)}"
            self.station_row_map[iid] = obj
            self.station_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    str(obj.get("name", "")),
                    target_label,
                    str(obj.get("uuid", "")),
                ),
            )

    def add_station(self) -> None:
        if self.editor is None or self.system_file_state is None:
            messagebox.showerror(
                self.tr("operation_failed"), self.tr("no_editor"), parent=self.root
            )
            return
        assets = Path(self.assets_var.get().strip())
        if not assets.exists():
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("station_assets_required"),
                parent=self.root,
            )
            return
        station_type = self.station_type_var.get().strip()
        if not station_type:
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("station_type_required"),
                parent=self.root,
            )
            return
        target_label = self.station_target_var.get()
        target = self.station_target_map.get(target_label)
        if target is None:
            messagebox.showerror(
                self.tr("operation_failed"),
                self.tr("station_target"),
                parent=self.root,
            )
            return
        location = list(self.editor["location"])
        try:
            radius, angle, direction = native_station_orbit(
                self.station_radius_var.get().strip(), self.station_angle_var.get().strip(),
                self.station_direction_var.get().strip(), radius_label=self.tr("station_radius"),
                angle_label=self.tr("station_angle"), direction_label=self.tr("station_direction"),
            )
        except ValueError as exc:
            messagebox.showerror(
                self.tr("operation_failed"), str(exc), parent=self.root
            )
            return

        def task():
            return swe.build_system_station_object(
                assets,
                location,
                station_type,
                target[0],
                target[1],
                orbit_radius=radius,
                orbit_angle=angle,
                orbit_direction=direction,
            )

        def success(obj):
            if self.system_file_state is None:
                return
            body = self.system_file_state["body"]
            obj["spawnTime"] = float(body.get("lastSpawn", 0.0))
            obj["orbit"]["enterTime"] = float(body.get("lastSpawn", 0.0))
            body.setdefault("objects", []).append(obj)
            self.rebuild_station_tree()
            self.set_status_key(
                "station_added", type=station_type, target=target_label
            )

        self.run_worker(task, success, "compiling_body")

    def station_selected(self, _event: Any = None) -> None:
        selection = self.station_tree.selection()
        if not selection:
            return
        obj = self.station_row_map.get(selection[0])
        if not isinstance(obj, dict):
            return
        self.station_type_var.set(str(obj.get("name", "")))
        self.station_parameters_text.delete("1.0", "end")
        self.station_parameters_text.insert(
            "1.0",
            json.dumps(
                obj.get("parameters")
                if isinstance(obj.get("parameters"), dict)
                else {},
                ensure_ascii=False,
                indent=2,
            ),
        )
        orbit = obj.get("orbit")
        if not isinstance(orbit, dict):
            return
        target = orbit.get("target")
        if isinstance(target, dict):
            coordinate = (
                int(target.get("planet", 0)),
                int(target.get("satellite", 0)),
            )
            reverse = {
                value: label for label, value in self.station_target_map.items()
            }
            if coordinate in reverse:
                self.station_target_var.set(reverse[coordinate])
        position = orbit.get("enterPosition")
        if isinstance(position, list) and len(position) == 2:
            x, y = -float(position[0]), -float(position[1])
            self.station_radius_var.set(f"{math.hypot(x, y):.3f}".rstrip("0").rstrip("."))
            self.station_angle_var.set(f"{math.degrees(math.atan2(y, x)):.3f}".rstrip("0").rstrip("."))
        self.station_direction_var.set(
            "-1" if int(orbit.get("direction", 1)) < 0 else "1"
        )

    def apply_station_fields(self) -> None:
        selection = self.station_tree.selection()
        if not selection or self.editor is None:
            return
        obj = self.station_row_map.get(selection[0])
        target = self.station_target_map.get(self.station_target_var.get())
        if not isinstance(obj, dict) or target is None:
            return
        try:
            radius, angle_degrees, direction = native_station_orbit(
                self.station_radius_var.get().strip(), self.station_angle_var.get().strip(),
                self.station_direction_var.get().strip(), radius_label=self.tr("station_radius"),
                angle_label=self.tr("station_angle"), direction_label=self.tr("station_direction"),
            )
            angle = math.radians(angle_degrees)
        except ValueError as exc:
            messagebox.showerror(
                self.tr("operation_failed"),
                str(exc),
                parent=self.root,
            )
            return
        try:
            parameters = parse_field_json(
                self.station_parameters_text.get("1.0", "end").strip() or "{}", self.tr("station_parameters"),
            )
            if not isinstance(parameters, dict):
                raise ValueError(self.tr("station_parameters"))
            swe.validate_finite_json_numbers(parameters, self.tr("station_parameters"))
            self._check_image_edits(parameters, obj.get("parameters") or {}, kind="station")
        except (ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror(
                self.tr("operation_failed"), str(exc), parent=self.root
            )
            return
        x, y = radius * math.cos(angle), radius * math.sin(angle)
        station_type = self.station_type_var.get().strip()
        if station_type:
            obj["name"] = station_type
        obj["parameters"] = parameters
        orbit = obj.setdefault("orbit", {})
        orbit["target"] = {
            "location": list(self.editor["location"]),
            "planet": target[0],
            "satellite": target[1],
        }
        orbit["direction"] = -1 if direction < 0 else 1
        orbit["enterPosition"] = [-x, -y]
        if self.system_file_state is not None:
            orbit["enterTime"] = float(
                self.system_file_state.get("body", {}).get("lastSpawn", 0.0)
            )
        obj["position"] = [x, y]
        self.rebuild_station_tree()

    def remove_selected_system_object(self) -> None:
        if self.system_file_state is None:
            return
        selection = self.station_tree.selection()
        if not selection:
            return
        obj = self.station_row_map.get(selection[0])
        objects = self.system_file_state.get("body", {}).get("objects", [])
        if obj is not None and obj in objects:
            objects.remove(obj)
            self.rebuild_station_tree()
            self.set_status_key("station_removed")

    def delete_current_body(self) -> None:
        reference = self.selected_reference()
        if self.editor is None or reference is None:
            messagebox.showerror(self.tr("operation_failed"), self.tr("body_not_selected"), parent=self.root)
            return
        name = self.body_parameters(reference).get("name", "")
        if not self.ask_yes_no(
            self.tr("delete_title"), self.tr("delete_message", name=name)
        ):
            return
        kind, planet_entry, moon_entry = reference
        old_planet_orbit = int(planet_entry[0])
        old_moon_orbit = int(moon_entry[0]) if moon_entry is not None else 0
        if self.system_file_state is not None:
            for obj in self.system_file_state.get("body", {}).get("objects", []):
                target = (obj.get("orbit") or {}).get("target")
                if not isinstance(target, dict):
                    continue
                target_planet = int(target.get("planet", 0))
                target_moon = int(target.get("satellite", 0))
                affected = (
                    target_planet == old_planet_orbit
                    and (kind == "planet" or target_moon == old_moon_orbit)
                )
                if affected:
                    target["planet"] = 0
                    target["satellite"] = 0
        if kind == "planet":
            self.editor["planets"].remove(planet_entry)
        else:
            planet_entry[1]["satellites"].remove(moon_entry)
        self.current_body_iid = None
        self.rebuild_body_tree()
        self.rebuild_station_tree()
        self.refresh_biome_context()
        self.refresh_mpi_warning()

    def rebuild_body_names(self) -> None:
        if self.editor is None:
            messagebox.showerror(self.tr("operation_failed"), self.tr("no_editor"), parent=self.root)
            return
        try:
            self.apply_system_fields()
        except Exception as exc:
            messagebox.showerror(
                self.tr("operation_failed"), str(exc), parent=self.root
            )
            return
        base = self.editor["systemParameters"]["name"]
        planets = sorted(self.editor["planets"], key=lambda value: value[0])
        for index, planet_entry in enumerate(planets, start=1):
            roman = ROMAN.get(index, str(index))
            planet_name = f"{base} ^green;{roman}^white;"
            planet_entry[1]["parameters"]["name"] = planet_name
            for moon_index, moon_entry in enumerate(
                sorted(planet_entry[1].get("satellites", []), key=lambda value: value[0])
            ):
                letter = chr(ord("a") + moon_index) if moon_index < 26 else str(moon_index + 1)
                moon_entry[1]["name"] = (
                    f"{planet_name} ^white;- ^yellow;{letter}^white;"
                )
        self.rebuild_body_tree()
        self.rebuild_station_tree()
        self.refresh_mpi_warning()
        self.set_status_key("names_rebuilt")
        messagebox.showinfo(
            self.tr("names_rebuilt_title"), self.tr("names_rebuilt"), parent=self.root
        )

    def refresh_mpi_warning(self) -> None:
        if self.editor is None:
            self.mpi_var.set("")
            return
        names = []
        for planet_entry in self.editor["planets"]:
            names.append(str(planet_entry[1]["parameters"].get("name", "")))
            names.extend(
                str(entry[1].get("name", ""))
                for entry in planet_entry[1].get("satellites", [])
            )
        recognized = {f"^green;{value}^white;" for value in ROMAN.values()}
        unsafe = 0
        unnumbered = 0
        for name in names:
            position = name.find("green")
            if position < 0:
                unsafe += 1
                continue
            suffix_start = max(0, position - 1)
            if name[suffix_start:] not in recognized:
                unnumbered += 1
        self.mpi_var.set(
            self.tr("mpi_warning", unsafe=unsafe, unnumbered=unnumbered)
            if unsafe or unnumbered
            else self.tr("mpi_ok")
        )

    @staticmethod
    def biome_signature(parameters: dict[str, Any]) -> str:
        raw = parameters.get("parameters") or {}
        visitable = json_clone(parameters.get("visitableParameters"))
        if isinstance(visitable, dict):
            # These values can be synchronized into WorldMetadata without
            # deleting any tile/entity sectors.  The remaining values are the
            # generation recipe and do require regeneration when changed.
            for key in (
                "typeName",
                "threatLevel",
                "gravity",
                "airless",
                "weatherPool",
                "environmentStatusEffects",
                "overrideTech",
                "globalDirectives",
                "beamUpRule",
                "disableDeathDrops",
                "terraformed",
                "worldEdgeForceRegions",
                "hueShift",
                "skyColoring",
                "dayLength",
            ):
                visitable.pop(key, None)
        generation = {
            "seed": parameters.get("seed"),
            "raw": {
                key: raw.get(key)
                for key in (
                    "worldType",
                    "worldSize",
                    "terrestrialType",
                    "dungeonWorld",
                )
            },
            "visitable": visitable,
        }
        return json.dumps(generation, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def parameters_by_coordinate(editor: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
        result: dict[tuple[int, int], dict[str, Any]] = {}
        for planet_entry in editor["planets"]:
            planet_orbit = int(planet_entry[0])
            result[(planet_orbit, 0)] = planet_entry[1]["parameters"]
            for moon_entry in planet_entry[1].get("satellites") or []:
                result[(planet_orbit, int(moon_entry[0]))] = moon_entry[1]
        return result

    def client_context_orbit_map(self) -> dict[tuple[int, int], tuple[int, int]]:
        result: dict[tuple[int, int], tuple[int, int]] = {}
        if self.editor is None:
            return result
        for planet_entry in self.editor["planets"]:
            planet_orbit = int(planet_entry[0])
            parameters = planet_entry[1]["parameters"]
            origin = self.body_origins.get(id(parameters))
            if origin is not None and origin != (planet_orbit, 0):
                result[origin] = (planet_orbit, 0)
            for moon_entry in planet_entry[1].get("satellites") or []:
                moon_orbit = int(moon_entry[0])
                origin = self.body_origins.get(id(moon_entry[1]))
                if origin is not None and origin != (planet_orbit, moon_orbit):
                    result[origin] = (planet_orbit, moon_orbit)
        return result

    def world_file_plans(
        self,
    ) -> tuple[
        list[tuple[Path, Path, int, int]],
        list[Path],
        list[tuple[Path, dict[str, Any], int, int]],
        int,
    ]:
        if self.editor is None or self.original_editor is None:
            return [], [], [], 0
        originals = self.parameters_by_coordinate(self.original_editor)
        source_location = list(self.original_editor["location"])
        destination_location = list(self.editor["location"])
        location_changed = source_location != destination_location
        migrations: list[tuple[Path, Path, int, int]] = []
        resets: list[Path] = []
        metadata_updates: list[tuple[Path, dict[str, Any], int, int]] = []
        biome_changed_generated = 0
        for planet_entry in self.editor["planets"]:
            planet_orbit = int(planet_entry[0])
            bodies = [(0, planet_entry[1]["parameters"])] + [
                (int(moon[0]), moon[1])
                for moon in (planet_entry[1].get("satellites") or [])
            ]
            for moon_orbit, parameters in bodies:
                origin = self.body_origins.get(id(parameters))
                if origin is None:
                    continue
                original_parameters = originals.get(origin)
                if original_parameters is None:
                    continue
                current = (planet_orbit, moon_orbit)
                biome_changed = self.biome_signature(parameters) != self.biome_signature(
                    original_parameters
                )
                source = self.body_world_path(*origin, location=source_location)
                destination = self.body_world_path(
                    *current, location=destination_location
                )
                generated_world = destination if destination.is_file() else source
                if (
                    generated_world.is_file()
                    and not swe.generated_world_matches_celestial_type(
                        parameters, generated_world
                    )
                ):
                    biome_changed = True
                if biome_changed:
                    if (current != origin or location_changed) and destination.exists():
                        raise FileExistsError(
                            "Cannot move and regenerate this body because the destination "
                            f"world already exists: {destination}"
                        )
                    if source.is_file() or destination.is_file():
                        biome_changed_generated += 1
                    if destination.is_file():
                        resets.append(destination)
                elif (current != origin or location_changed) and source.is_file():
                    migrations.append((source, destination, planet_orbit, moon_orbit))
                if not biome_changed and (source.is_file() or destination.is_file()):
                    current_compare = json_clone(parameters)
                    original_compare = json_clone(original_parameters)
                    current_compare.pop("coordinate", None)
                    original_compare.pop("coordinate", None)
                    if (
                        current != origin
                        or location_changed
                        or current_compare != original_compare
                    ):
                        metadata_updates.append(
                            (destination, parameters, planet_orbit, moon_orbit)
                        )
        return (
            migrations,
            list(dict.fromkeys(resets)),
            metadata_updates,
            biome_changed_generated,
        )

    def assert_direct_world_edit_safe(self, world: Path) -> None:
        """Reject a World4-only edit that a later Save System would erase."""

        target = str(Path(world).resolve()).casefold()
        _migrations, resets, _metadata_updates, _changed = self.world_file_plans()
        if any(str(Path(path).resolve()).casefold() == target for path in resets):
            raise RuntimeError(self.tr("direct_world_edit_blocked_by_reset"))

    @staticmethod
    def starbound_process_running() -> bool | None:
        """Return Starbound's process state, or ``None`` when it is unknown."""

        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # Failure to enumerate processes is not proof that the game is
            # closed.  The automatic monitor treats this as unsafe, while a
            # manually confirmed apply may still proceed with its backup path.
            return None
        output = result.stdout.casefold()
        return any(
            f'"{name}"' in output
            for name in (
                "starbound.exe",
                "starbound_opengl.exe",
                "starbound_server.exe",
                "openstarbound.exe",
            )
        )

    def pending_ocean_worlds(self) -> list[Path]:
        result: list[Path] = []
        if self.editor is None:
            return result
        for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
            if not self.ocean_pair_pending.get(id(parameters), False):
                continue
            if not self.serialized_ocean_pairs(parameters):
                continue
            result.append(self.body_world_path(planet_orbit, moon_orbit))
        return result

    def schedule_pending_ocean_monitor(self, delay_ms: int = 3000) -> None:
        """Watch for regenerated worlds and run the World4 second stage."""

        if self._pending_ocean_monitor_after is not None:
            try:
                self.root.after_cancel(self._pending_ocean_monitor_after)
            except tk.TclError:
                pass
        self._pending_ocean_monitor_after = self.root.after(
            max(100, int(delay_ms)), self.poll_pending_ocean_monitor
        )

    def cancel_pending_ocean_monitor(self) -> None:
        scheduled = getattr(self, "_pending_ocean_monitor_after", None)
        if scheduled is not None:
            try:
                self.root.after_cancel(scheduled)
            except tk.TclError:
                pass
        self._pending_ocean_monitor_after = None
        self._pending_ocean_observations = {}

    def poll_pending_ocean_monitor(self) -> None:
        self._pending_ocean_monitor_after = None
        pending = self.pending_ocean_worlds()
        if not pending:
            self._pending_ocean_observations = {}
            return
        self.set_status_key("ocean_waiting_for_regeneration", count=len(pending))
        process_state = self.starbound_process_running()
        if self.busy or process_state is not False:
            self._pending_ocean_observations = {}
            self.schedule_pending_ocean_monitor()
            return

        present: list[Path] = []
        observations: dict[str, tuple[int, int]] = {}
        for path in pending:
            try:
                if not path.is_file():
                    continue
                file_stat = path.stat()
            except OSError:
                # Starbound replaces the world file while regenerating it.  A
                # path may disappear between the existence check and stat;
                # retry on the next monitor tick instead of losing the watch.
                continue
            present.append(path)
            observations[str(path.resolve()).casefold()] = (
                int(file_stat.st_size),
                int(file_stat.st_mtime_ns),
            )
        stable = bool(observations) and all(
            self._pending_ocean_observations.get(key) == value
            for key, value in observations.items()
        )
        self._pending_ocean_observations = observations
        if not stable:
            self.schedule_pending_ocean_monitor()
            return

        self.audit_log(
            "pending_floor_monitor_stable_world_detected",
            universe_folder=Path(self.chunks_var.get().strip()),
            worlds=present,
            observations=observations,
        )
        self._pending_ocean_observations = {}
        self.apply_pending_ocean_floors(
            auto_offer=True,
            automatic_monitor=True,
        )

    def ocean_pair_save_plan(
        self,
        source_location: list[int],
        destination_location: list[int],
        resets: list[Path],
    ) -> tuple[dict[str, Any], list[tuple[Path, str, list[dict[str, Any]]]]]:
        store = json_clone(self.ocean_pair_store)
        bodies = store.setdefault("bodies", {})
        if not isinstance(bodies, dict):
            bodies = {}
            store["bodies"] = bodies
        source_prefix = ":".join(str(int(value)) for value in source_location) + "|"
        for stored_key in list(bodies):
            if str(stored_key).startswith(source_prefix):
                bodies.pop(stored_key, None)
        reset_paths = {str(path.resolve()).casefold() for path in resets}
        jobs: list[tuple[Path, str, list[dict[str, Any]]]] = []
        for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
            key = swe.ocean_pair_body_key(
                destination_location, planet_orbit, moon_orbit
            )
            pairs = self.serialized_ocean_pairs(parameters)
            if not pairs:
                bodies.pop(key, None)
                continue
            world = self.body_world_path(
                planet_orbit, moon_orbit, location=destination_location
            )
            world_is_being_reset = (
                str(world.resolve()).casefold() in reset_paths
            )
            # Any full regeneration discards the World4-only local basin,
            # including one that was successfully applied before.  Re-arm the
            # second stage even when the sidecar previously said pending=false.
            pending = bool(
                self.ocean_pair_pending.get(id(parameters), False)
                or world_is_being_reset
            )
            record = {"pairs": pairs, "pending": pending}
            bodies[key] = record
            if (
                pending
                and world.is_file()
                and not world_is_being_reset
            ):
                jobs.append((world, key, pairs))
            audit = getattr(self, "audit_log", None)
            if callable(audit):
                audit(
                    "ocean_pair_plan_body",
                    universe_folder=self.chunks_path().parent,
                    bodyKey=key,
                    world=world,
                    worldExists=world.is_file(),
                    worldSizeBytes=world.stat().st_size if world.is_file() else None,
                    worldModifiedNs=(
                        world.stat().st_mtime_ns if world.is_file() else None
                    ),
                    worldIsBeingReset=world_is_being_reset,
                    pendingBefore=bool(
                        self.ocean_pair_pending.get(id(parameters), False)
                    ),
                    pendingAfter=pending,
                    scheduledForImmediateApply=bool(
                        pending and world.is_file() and not world_is_being_reset
                    ),
                    pairs=pairs,
                )
        return store, jobs

    def missing_ocean_world_generation_reason(
        self,
        planet: int,
        moon: int,
        parameters: dict[str, Any],
        pairs: list[dict[str, Any]],
    ) -> str | None:
        """Distinguish this save's edits from older ungenerated pending bodies."""

        original_editor = self.original_editor
        if not isinstance(original_editor, dict):
            raise ValueError(
                "The loaded system snapshot is unavailable; reload the system "
                "before generating a missing ocean world."
            )
        originals = self.parameters_by_coordinate(original_editor)
        current = self.parameters_by_coordinate(
            self.editor or {"planets": []}
        ).get((planet, moon))
        # The generation snapshot is a deep copy, so recover its body's identity
        # from the live editor. This also preserves origins across orbit moves.
        origin = self.body_origins.get(id(current), (planet, moon))
        original = originals.get(origin) if origin is not None else None
        if original is None:
            return "new_paired_body"
        if self.biome_signature(parameters) != self.biome_signature(original):
            return "generation_recipe_changed"

        stored_key = swe.ocean_pair_body_key(original_editor["location"], *origin)
        stored_bodies = self.ocean_pair_store.get("bodies", {})
        stored_record = stored_bodies.get(stored_key, {}) if isinstance(stored_bodies, dict) else {}
        stored_pairs = stored_record.get("pairs", []) if isinstance(stored_record, dict) else []
        by_index = {
            int(item.get("surfaceIndex", -1)): item
            for item in stored_pairs
            if isinstance(item, dict)
        }
        # Mirror load_ocean_pair_settings: oceans without a sidecar already had
        # default pairings when loaded. Merely serializing those defaults is not
        # a user edit and must not eagerly create every missing ocean planet.
        original_pairs: list[dict[str, Any]] = []
        for index, name in enumerate(self.surface_biome_names(original)):
            default = self.default_ocean_floor_setting(name)
            if default is not None:
                original_pairs.append({
                    **default, **json_clone(by_index.get(index, {})),
                    "surfaceIndex": index,
                })

        def pair_signature(values: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
            normalized = swe.normalize_ocean_floor_pair_settings(values)
            # Normalization intentionally drops surfaceIndex, but which ocean
            # an override belongs to is significant even for duplicate biomes.
            return sorted(
                ((int(value.get("surfaceIndex", -1)), setting)
                 for value, setting in zip(values, normalized)),
                key=lambda item: item[0],
            )

        if pair_signature(pairs) != pair_signature(original_pairs):
            return "ocean_pairing_changed"
        return None

    def native_ocean_generation_plan(
        self,
        generation_editor: dict[str, Any],
        ocean_store: dict[str, Any],
        resets: list[Path],
        migrations: list[tuple[Path, Path, int, int]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build reset or newly edited missing worlds, not older pending worlds.

        Read parameters from the normalized snapshot before MPI display-only
        dungeon overrides.  An old world's already-applied basin never acts as
        the source recipe for its replacement.
        """

        reset_keys = {str(path.resolve()).casefold() for path in resets}
        migration_targets = {
            str(destination.resolve()).casefold()
            for _source, destination, _planet, _moon in migrations or []
        }
        location = generation_editor["location"]
        jobs: list[dict[str, Any]] = []
        for (planet, moon), parameters in self.parameters_by_coordinate(
            generation_editor
        ).items():
            key = swe.ocean_pair_body_key(location, planet, moon)
            record = ocean_store.get("bodies", {}).get(key) or {}
            pairs = record.get("pairs") or []
            # The native default shares the primary environment (including
            # parallax) across secondary regions. Stage land-only ordered
            # worlds too, so their backgrounds can be assigned before install.
            layer_names: dict[str, set[str]] = {}
            for group in swe.celestial_layer_groups(parameters):
                layer_names.setdefault(group["layerId"], set()).update(group["biomes"])
            parallax_layers = [layer for layer, names in layer_names.items() if len(names) > 1]
            if not pairs and not parallax_layers:
                continue
            world = self.body_world_path(planet, moon, location=location)
            if str(world.resolve()).casefold() in migration_targets:
                continue
            if str(world.resolve()).casefold() in reset_keys:
                reason = "world_reset"
            else:
                if world.is_file():
                    continue
                reason = self.missing_ocean_world_generation_reason(
                    planet, moon, parameters, pairs
                )
                if reason is None and parallax_layers:
                    live = self.parameters_by_coordinate(self.editor or {"planets": []}).get((planet, moon))
                    if getattr(self, "ordered_parallax_applied", {}).get(id(live)):
                        reason = "ordered_parallax_apply"
                if reason is None:
                    continue
            jobs.append(
                {
                    "world": world,
                    "bodyKey": key,
                    "parameters": json_clone(parameters),
                    "oceanPairs": json_clone(pairs),
                    "orderedParallaxLayers": parallax_layers,
                    "generationReason": reason,
                }
            )
        return jobs

    def ordered_parallax_repair_plan(
        self, native_jobs: list[dict[str, Any]],
        migrations: list[tuple[Path, Path, int, int]], resets: list[Path],
        ocean_store: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Explicit same-order Apply: metadata-only, never a forced reset."""
        applied = getattr(self, "ordered_parallax_applied", {})
        if not applied:
            return []
        excluded_paths = {str(Path(path).resolve()).casefold() for path in resets}
        excluded_paths.update(str(job["world"].resolve()).casefold() for job in native_jobs)
        migration_sources = {str(dst.resolve()).casefold(): src for src, dst, _p, _m in migrations}
        jobs = []
        for planet, moon, parameters in self.iter_body_parameters():
            layer_ids = sorted(applied.get(id(parameters), ()))
            if not layer_ids:
                continue
            world = self.body_world_path(planet, moon)
            path_key = str(world.resolve()).casefold()
            if path_key in excluded_paths:
                continue
            source = migration_sources.get(path_key, world)
            if not source.is_file():
                continue
            key = swe.ocean_pair_body_key(self.editor["location"], planet, moon)
            pairs = (ocean_store.get("bodies", {}).get(key) or {}).get("pairs") or []
            excluded = sorted({str(pair.get(field, "")) for pair in pairs
                               for field in ("surfaceBiome", "floorBiome") if pair.get(field)})
            jobs.append({"world": world, "layerIds": layer_ids, "excludedBiomeNames": excluded})
        return jobs

    def stage_native_ocean_worlds(
        self,
        chunks: Path,
        source_location: list[int],
        generation_editor: dict[str, Any],
        assets: Path,
        jobs: list[dict[str, Any]],
        server_executable: Path,
    ) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
        """Prepare verified final files before any live save is replaced."""

        staging = tempfile.TemporaryDirectory(
            prefix=".swe05-final-worlds-", dir=chunks.parent
        )
        staged_jobs = [
            dict(job, output=Path(staging.name) / job["world"].name)
            for job in jobs
        ]
        def progress(event: str, details: dict[str, Any]) -> None:
            self.report_ocean_generation_progress(
                event, details, log_prefix="native_regeneration_", universe_folder=chunks.parent
            )
        try:
            result = world_regeneration.regenerate_celestial_worlds(
                chunks,
                source_location,
                generation_editor,
                assets,
                staged_jobs,
                server_executable=server_executable,
                progress=progress,
            )
            for job in staged_jobs:
                if not job["output"].is_file():
                    raise RuntimeError(
                        "Native regeneration did not produce the final world: "
                        + str(job["world"])
                    )
            return staging, staged_jobs, result
        except Exception:
            staging.cleanup()
            raise

    def apply_pending_ocean_floors(
        self,
        auto_offer: bool = False,
        automatic_monitor: bool = False,
    ) -> None:
        """Recover legacy pending floors without resaving Celestial2.

        Normal saves now create replacement worlds and apply their floors in
        the private native workflow. Keep this operation for old sidecars and
        the existing completion monitor, without a permanent bottom button.
        """

        universe_folder = Path(self.chunks_var.get().strip())
        self.audit_log(
            "pending_floor_apply_requested",
            universe_folder=universe_folder,
            automaticOffer=auto_offer,
            automaticMonitor=automatic_monitor,
        )
        if self.editor is None or self.original_editor is None:
            self.audit_log(
                "pending_floor_apply_rejected",
                universe_folder=universe_folder,
                reason="system_not_loaded",
            )
            if not auto_offer:
                messagebox.showerror(
                    self.tr("operation_failed"),
                    self.tr("invalid_system"),
                    parent=self.root,
                )
            return
        chunks = self.chunks_path()
        if not chunks.is_file():
            self.audit_log(
                "pending_floor_apply_rejected",
                universe_folder=universe_folder,
                reason="universe_chunks_missing",
                chunks=chunks,
            )
            if not auto_offer:
                messagebox.showerror(
                    self.tr("invalid_path"),
                    self.tr("invalid_chunks"),
                    parent=self.root,
                )
            return
        try:
            source_location = list(self.original_editor["location"])
            destination_location = list(self.editor["location"])
            store, jobs = self.ocean_pair_save_plan(
                source_location, destination_location, []
            )
            _migrations, pending_resets, _metadata, _changed = self.world_file_plans()
            reset_keys = {
                str(path.resolve()).casefold() for path in pending_resets
            }
            blocked_jobs = [
                world for world, _key, _pairs in jobs
                if str(world.resolve()).casefold() in reset_keys
            ]
            if blocked_jobs:
                raise RuntimeError(self.tr("ocean_apply_blocked_by_reset"))
        except Exception as exc:
            self.audit_exception(
                "pending_floor_plan_failed",
                exc,
                universe_folder=universe_folder,
            )
            if not auto_offer:
                messagebox.showerror(
                    self.tr("operation_failed"), str(exc), parent=self.root
                )
            return
        if not jobs:
            self.audit_log(
                "pending_floor_apply_no_jobs",
                universe_folder=universe_folder,
                automaticOffer=auto_offer,
                store=store,
            )
            if not auto_offer:
                messagebox.showinfo(
                    self.tr("ocean_apply_title"),
                    self.tr("no_pending_ocean_floors"),
                    parent=self.root,
                )
            if automatic_monitor and self.pending_ocean_worlds():
                self.schedule_pending_ocean_monitor()
            return
        process_state = self.starbound_process_running()
        if process_state is True or (automatic_monitor and process_state is None):
            self.audit_log(
                "pending_floor_apply_deferred_game_running",
                universe_folder=universe_folder,
                automaticMonitor=automatic_monitor,
                jobs=[str(world) for world, _key, _pairs in jobs],
            )
            self.set_status_key("ocean_starbound_running")
            if not automatic_monitor:
                messagebox.showinfo(
                    self.tr("ocean_apply_title"),
                    self.tr("ocean_starbound_running"),
                    parent=self.root,
                )
            self.schedule_pending_ocean_monitor()
            return
        assets = Path(self.assets_var.get().strip())
        if not assets.exists():
            self.audit_log(
                "pending_floor_apply_rejected",
                universe_folder=universe_folder,
                reason="assets_missing",
                assets=assets,
            )
            if not auto_offer:
                messagebox.showerror(
                    self.tr("operation_failed"),
                    self.tr("region_assets_required"),
                    parent=self.root,
                )
            return
        if not automatic_monitor and not self.ask_yes_no(
            self.tr("ocean_apply_title"),
            self.tr("ocean_apply_message", count=len(jobs)),
        ):
            self.audit_log(
                "pending_floor_apply_cancelled",
                universe_folder=universe_folder,
                jobs=[{"world": world, "bodyKey": key, "pairs": pairs} for world, key, pairs in jobs],
            )
            return
        self.audit_log(
            "pending_floor_apply_confirmed",
            universe_folder=universe_folder,
            assets=assets,
            jobs=[{"world": world, "bodyKey": key, "pairs": pairs} for world, key, pairs in jobs],
        )

        def task():
            backups: list[tuple[Path, Path]] = []
            summaries: list[dict[str, Any]] = []
            try:
                for world, key, pairs in jobs:
                    self.audit_log(
                        "pending_floor_world_started",
                        universe_folder=universe_folder,
                        world=world,
                        bodyKey=key,
                        pairs=pairs,
                    )
                    backup, summary = ocean_flora_completion.apply_pairs_in_place(
                        world,
                        assets,
                        pairs,
                        progress=lambda event, details, target=world, body_key=key: self.report_ocean_generation_progress(
                            event, details, log_prefix="ocean_core_",
                            universe_folder=universe_folder,
                            targetWorld=target,
                            bodyKey=body_key,
                        ),
                    )
                    if backup is not None:
                        backups.append((backup, world))
                    summaries.append(summary)
                    store["bodies"][key]["pending"] = False
                    self.audit_log(
                        "pending_floor_world_completed",
                        universe_folder=universe_folder,
                        world=world,
                        bodyKey=key,
                        backup=backup,
                        summary=summary,
                    )
                sidecar = swe.save_ocean_pair_sidecar(chunks.parent, store)
                self.audit_log(
                    "pending_floor_sidecar_saved",
                    universe_folder=universe_folder,
                    sidecar=sidecar,
                    store=store,
                )
            except Exception as exc:
                self.audit_exception(
                    "pending_floor_apply_failed",
                    exc,
                    universe_folder=universe_folder,
                )
                for backup, world in reversed(backups):
                    if backup.exists():
                        shutil.copy2(backup, world)
                        self.audit_log(
                            "pending_floor_world_restored",
                            universe_folder=universe_folder,
                            world=world,
                            backup=backup,
                        )
                raise
            return store, summaries

        def success(result):
            saved_store, summaries = result
            self.ocean_pair_store = saved_store
            self.load_ocean_pair_settings()
            regions = sum(
                len(summary.get("rectangles", [])) for summary in summaries
            )
            message = self.tr(
                "ocean_apply_done", worlds=len(summaries), regions=regions
            )
            self.set_status(message)
            self.audit_log(
                "pending_floor_apply_completed",
                universe_folder=universe_folder,
                worldCount=len(summaries),
                regionCount=regions,
                summaries=summaries,
            )
            messagebox.showinfo(
                self.tr("ocean_apply_title"), message, parent=self.root
            )
            if self.pending_ocean_worlds():
                self.schedule_pending_ocean_monitor()

        self.run_worker(task, success, "ocean_floor_apply_current_busy")

    def perform_world_file_changes(
        self,
        migrations: list[tuple[Path, Path, int, int]],
        resets: list[Path],
        metadata_updates: list[tuple[Path, dict[str, Any], int, int]],
        system_change: tuple[Path, Path, list[int], dict[str, Any]] | None = None,
        *,
        destination_location: list[int] | None = None,
    ) -> tuple[
        list[Path],
        list[tuple[Path, Path]],
        list[tuple[Path, Path]],
        list[tuple[Path, Path]],
    ]:
        for source, destination, _planet, _moon in migrations:
            if destination.exists() and source.resolve() != destination.resolve():
                raise FileExistsError(
                    f"Cannot preserve orbit change because destination already exists: {destination}"
                )
        created: list[Path] = []
        reset_backups: list[tuple[Path, Path]] = []
        system_backups: list[tuple[Path, Path]] = []
        metadata_backups: list[tuple[Path, Path]] = []
        try:
            location = (
                list(destination_location) if destination_location is not None
                else self.editor["location"] if self.editor else [0, 0, 0]
            )
            if system_change is not None:
                (
                    system_source,
                    system_destination,
                    system_location,
                    system_state,
                ) = system_change
                backup = swe.write_generated_system_file(
                    system_source,
                    system_destination,
                    system_state,
                    system_location,
                )
                if system_source.resolve() != system_destination.resolve():
                    created.append(system_destination)
                elif backup is not None:
                    system_backups.append((backup, system_source))
            for source, destination, planet, moon in migrations:
                if source.resolve() == destination.resolve() or destination.exists():
                    continue
                swe.copy_world_to_celestial_coordinate(
                    source, destination, location, planet, moon
                )
                created.append(destination)
            for path, parameters, planet, moon in metadata_updates:
                if not path.is_file():
                    continue
                backup = swe.sync_world_celestial_parameters_in_place(
                    path, parameters, location, planet, moon
                )
                metadata_backups.append((backup, path))
            for path in resets:
                if not path.is_file():
                    continue
                backup = swe._unique_backup_path(path)
                path.replace(backup)
                reset_backups.append((backup, path))
        except Exception:
            for backup, path in reversed(metadata_backups):
                if backup.exists():
                    shutil.copy2(backup, path)
            for backup, path in reversed(reset_backups):
                if backup.exists() and not path.exists():
                    backup.replace(path)
            for path in reversed(created):
                if path.exists():
                    path.unlink()
            for backup, path in reversed(system_backups):
                if backup.exists():
                    shutil.copy2(backup, path)
            raise
        return created, reset_backups, system_backups, metadata_backups

    def save_system(self) -> None:
        chunks = self.chunks_path()
        universe_folder = chunks.parent
        self.audit_log(
            "save_system_requested",
            universe_folder=universe_folder,
            chunks=chunks,
        )
        if not chunks.is_file():
            self.audit_log(
                "save_system_rejected",
                universe_folder=universe_folder,
                reason="universe_chunks_missing",
                chunks=chunks,
            )
            messagebox.showerror(self.tr("invalid_path"), self.tr("invalid_chunks"), parent=self.root)
            return
        if self.editor is None:
            self.audit_log(
                "save_system_rejected",
                universe_folder=universe_folder,
                reason="system_not_loaded",
            )
            messagebox.showerror(self.tr("operation_failed"), self.tr("invalid_system"), parent=self.root)
            return
        try:
            if self.current_body_iid:
                self.apply_current_body(rebuild=False, silent=True)
            self.apply_system_fields()
            self.store_loaded_range_fields(strict=True)
            self.store_loaded_biome_list()
            self.apply_pending_biomes()
            self.enforce_all_uniform_ocean_subregions()
            self._validate_changed_resources_for_save()
            orbit_map = self.client_context_orbit_map()
            display_dungeon_lists = self.display_dungeon_lists_by_coordinate()
            editor = json_clone(self.editor)
            source_location = list(self.original_editor["location"])
            destination_location = list(editor["location"])
            swe.normalize_celestial_system_editor(editor, destination_location)
            generation_editor = json_clone(editor)
            if (
                destination_location != source_location
                and swe.celestial_system_coordinate_exists(
                    chunks,
                    destination_location,
                    exclude_location=source_location,
                )
            ):
                raise ValueError(
                    self.tr(
                        "coordinate_used",
                        coordinate=": ".join(map(str, destination_location)),
                    )
                )
        except Exception as exc:
            self.audit_exception(
                "save_system_input_validation_failed",
                exc,
                universe_folder=universe_folder,
            )
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        try:
            (
                migrations,
                resets,
                metadata_updates,
                biome_changed_generated,
            ) = self.world_file_plans()
            _display_layers, display_deferred = (
                self.apply_display_dungeon_overrides(
                    editor,
                    display_dungeon_lists,
                    source_location,
                    destination_location,
                )
            )
            ocean_store, ocean_jobs = self.ocean_pair_save_plan(
                source_location, destination_location, resets
            )
            native_jobs = self.native_ocean_generation_plan(
                generation_editor, ocean_store, resets, migrations
            )
            parallax_repair_jobs = CelestialSystemEditorWindow.ordered_parallax_repair_plan(
                self, native_jobs, migrations, resets, ocean_store,
            )
            if parallax_repair_jobs and self.starbound_process_running() is not False:
                raise RuntimeError(self.tr("ocean_starbound_running"))
            pending_after_regeneration = 0
            assets_path = Path(self.assets_var.get().strip())
            server_executable = None
            if ocean_jobs or native_jobs:
                if not assets_path.exists():
                    raise ValueError(self.tr("region_assets_required"))
            if native_jobs:
                if self.starbound_process_running():
                    raise RuntimeError(self.tr("ocean_starbound_running"))
                server_executable = world_regeneration.resolve_server_executable(
                    chunks, assets_path
                )
            system_source = self.system_file_path(source_location)
            system_destination = self.system_file_path(destination_location)
            if not system_source.is_file():
                raise FileNotFoundError(system_source)
            if self.system_file_state is None or self.original_system_file_state is None:
                raise ValueError("The generated .system state was not loaded")
            system_file_changed = (
                self.system_file_state.get("body")
                != self.original_system_file_state.get("body")
            )
            system_change = None
            if destination_location != source_location or system_file_changed:
                if (
                    destination_location != source_location
                    and system_destination.exists()
                ):
                    raise FileExistsError(
                        self.tr("coordinate_file_used", path=system_destination)
                    )
                system_change = (
                    system_source,
                    system_destination,
                    destination_location,
                    self.system_file_state,
                )
            self.audit_log(
                "save_system_plan_created",
                universe_folder=universe_folder,
                sourceLocation=source_location,
                destinationLocation=destination_location,
                migrations=migrations,
                resetWorlds=resets,
                metadataUpdateWorlds=[item[0] for item in metadata_updates],
                biomeChangedGeneratedCount=biome_changed_generated,
                oceanJobs=[
                    {"world": world, "bodyKey": key, "pairs": pairs}
                    for world, key, pairs in ocean_jobs
                ],
                nativeRegenerationJobs=native_jobs,
                orderedParallaxRepairJobs=parallax_repair_jobs,
                nativeServerExecutable=server_executable,
                pendingAfterRegeneration=pending_after_regeneration,
                oceanStore=ocean_store,
            )
        except Exception as exc:
            self.audit_exception(
                "save_system_plan_failed",
                exc,
                universe_folder=universe_folder,
            )
            messagebox.showerror(self.tr("operation_failed"), str(exc), parent=self.root)
            return
        if destination_location != source_location and not self.ask_yes_no(
            self.tr("coordinate_move_title"),
            self.tr(
                "coordinate_move",
                source=": ".join(map(str, source_location)),
                destination=": ".join(map(str, destination_location)),
            ),
        ):
            return
        if migrations and not self.ask_yes_no(
            self.tr("world_migration_title"),
            self.tr("world_migration", count=len(migrations)),
        ):
            return
        if biome_changed_generated and not self.ask_yes_no(
            self.tr("biome_changed_title"),
            self.tr("biome_changed", count=biome_changed_generated),
        ):
            return
        ordered_native = any(job.get("orderedParallaxLayers") for job in native_jobs)
        if native_jobs and not self.ask_yes_no(
            self.tr("native_ordered_title" if ordered_native else "ocean_apply_title"),
            self.tr("native_ordered_confirm" if ordered_native else "native_ocean_confirm", count=len(native_jobs)),
        ):
            return
        if ocean_jobs and not self.ask_yes_no(
            self.tr("ocean_apply_title"),
            self.tr("ocean_apply_message", count=len(ocean_jobs)),
        ):
            return
        if display_deferred:
            messagebox.showinfo(
                self.tr("display_dungeons_deferred_title"),
                self.tr("display_dungeons_deferred", count=display_deferred),
                parent=self.root,
            )
        if not self.ask_yes_no(
            self.tr("save_confirm_title"), self.tr("save_confirm", chunks=chunks)
        ):
            self.audit_log(
                "save_system_cancelled",
                universe_folder=universe_folder,
                reason="final_confirmation_declined",
            )
            return
        self.audit_log(
            "save_system_confirmed",
            universe_folder=universe_folder,
            sourceLocation=source_location,
            destinationLocation=destination_location,
            resetWorlds=resets,
            pendingAfterRegeneration=pending_after_regeneration,
        )

        def task():
            staging = None
            staged_jobs: list[dict[str, Any]] = []
            native_result: dict[str, Any] = {}
            try:
                if native_jobs:
                    save_inputs = capture_save_inputs(
                        [chunks, system_source, system_destination]
                        + list(resets)
                        + [path for item in migrations for path in item[:2]]
                        + [item[0] for item in metadata_updates]
                        + [job["world"] for job in native_jobs]
                        + [world for world, _key, _pairs in ocean_jobs]
                        + swe.ocean_pair_storage_paths(chunks.parent)
                        + list(chunks.parent.glob("*.clientcontext"))
                    )
                    staging, staged_jobs, native_result = self.stage_native_ocean_worlds(
                        chunks,
                        source_location,
                        generation_editor,
                        assets_path,
                        native_jobs,
                        server_executable,
                    )
                    validate_save_inputs(save_inputs)
                    if self.starbound_process_running():
                        raise RuntimeError(self.tr("ocean_starbound_running"))
                    self.audit_log(
                        "save_system_native_inputs_unchanged",
                        universe_folder=universe_folder,
                        paths=list(save_inputs),
                    )
                (
                    created,
                    reset_backups,
                    system_backups,
                    metadata_backups,
                ) = self.perform_world_file_changes(
                    migrations, resets, metadata_updates, system_change,
                    destination_location=destination_location,
                )
            except Exception:
                if staging is not None:
                    staging.cleanup()
                raise
            self.audit_log(
                "save_system_world_file_changes_completed",
                universe_folder=universe_folder,
                created=created,
                resetBackups=reset_backups,
                systemBackups=system_backups,
                metadataBackups=metadata_backups,
            )
            ocean_world_backups: list[tuple[Path, Path]] = []
            parallax_backups: list[tuple[Path, Path]] = []
            ocean_results: list[dict[str, Any]] = []
            try:
                reset_targets = {
                    str(path.resolve()).casefold() for _backup, path in reset_backups
                }
                for job in staged_jobs:
                    world = job["world"]
                    # Existing reset targets were backed up by the transaction;
                    # new bodies are removed again if any later save step fails.
                    if world.exists():
                        raise FileExistsError(
                            "World appeared during regeneration; it was not replaced: "
                            + str(world)
                        )
                    os.replace(job["output"], world)
                    if str(world.resolve()).casefold() not in reset_targets:
                        created.append(world)
                    if job.get("oceanPairs"):
                        ocean_store["bodies"][job["bodyKey"]]["pending"] = False
                    self.audit_log(
                        "save_system_native_final_world_installed",
                        universe_folder=universe_folder,
                        world=world,
                        bodyKey=job["bodyKey"],
                    )
                for result in native_result.get("worlds", []):
                    ocean_summary = result.get("oceanSummary", result)
                    if ocean_summary.get("pairsApplied", 0):
                        ocean_results.append(ocean_summary)
                for job in parallax_repair_jobs:
                    if self.starbound_process_running() is not False:
                        raise RuntimeError(self.tr("ocean_starbound_running"))
                    repair_backup, repair_summary = world_regeneration.apply_ordered_parallaxes_in_place(
                        job["world"], job["layerIds"], keep_backup=True,
                        excluded_biome_names=job["excludedBiomeNames"],
                    )
                    if repair_backup is not None:
                        parallax_backups.append((repair_backup, job["world"]))
                    self.audit_log("save_system_ordered_parallaxes_repaired",
                                   universe_folder=universe_folder, world=job["world"],
                                   backup=repair_backup, summary=repair_summary)
                database_result = swe.save_celestial_system(
                    chunks, source_location, editor
                )
                self.audit_log(
                    "celestial_database_saved",
                    universe_folder=universe_folder,
                    databaseBackup=database_result[0],
                    summary=database_result[1],
                )
                context_results = swe.migrate_client_contexts(
                    chunks.parent,
                    source_location,
                    destination_location,
                    orbit_map,
                )
                for world, key, pairs in ocean_jobs:
                    world_backup, ocean_summary = (
                        ocean_flora_completion.apply_pairs_in_place(
                            world,
                            assets_path,
                            pairs,
                            progress=lambda event, details, target=world, body_key=key: self.report_ocean_generation_progress(
                                event, details, log_prefix="ocean_core_",
                                universe_folder=universe_folder,
                                targetWorld=target,
                                bodyKey=body_key,
                            ),
                        )
                    )
                    if world_backup is not None:
                        ocean_world_backups.append((world_backup, world))
                    ocean_results.append(ocean_summary)
                    ocean_store["bodies"][key]["pending"] = False
                    self.audit_log(
                        "save_system_immediate_floor_completed",
                        universe_folder=universe_folder,
                        world=world,
                        bodyKey=key,
                        backup=world_backup,
                        summary=ocean_summary,
                    )
                sidecar_path = swe.save_ocean_pair_sidecar(
                    chunks.parent, ocean_store
                )
                self.audit_log(
                    "save_system_sidecar_saved",
                    universe_folder=universe_folder,
                    sidecar=sidecar_path,
                    oceanStore=ocean_store,
                )
            except Exception as exc:
                self.audit_exception(
                    "save_system_failed_and_rolling_back",
                    exc,
                    universe_folder=universe_folder,
                )
                if "database_result" in locals():
                    shutil.copy2(database_result[0], chunks)
                for ocean_backup, ocean_world in reversed(ocean_world_backups):
                    if ocean_backup.exists():
                        shutil.copy2(ocean_backup, ocean_world)
                for parallax_backup, parallax_world in reversed(parallax_backups):
                    if parallax_backup.exists():
                        shutil.copy2(parallax_backup, parallax_world)
                for metadata_backup, metadata_path in reversed(metadata_backups):
                    if metadata_backup.exists():
                        shutil.copy2(metadata_backup, metadata_path)
                for backup, path in reversed(reset_backups):
                    if backup.exists():
                        shutil.copy2(backup, path)
                for path in reversed(created):
                    if path.exists():
                        path.unlink()
                for system_backup, system_path in reversed(system_backups):
                    if system_backup.exists():
                        shutil.copy2(system_backup, system_path)
                if "context_results" in locals():
                    for context_path, context_backup, _count in context_results:
                        if context_backup is not None and context_backup.exists():
                            shutil.copy2(context_backup, context_path)
                raise
            finally:
                if staging is not None:
                    staging.cleanup()
            return (
                database_result,
                created,
                reset_backups,
                system_backups,
                metadata_backups,
                context_results,
                ocean_results,
                ocean_store,
            )

        def success(result):
            (
                (backup, summary),
                _created,
                _reset_backups,
                _system_backups,
                _metadata_backups,
                context_results,
                ocean_results,
                saved_ocean_store,
            ) = result
            loaded_editor = swe.load_celestial_system(chunks, destination_location)
            saved_display_dungeons = self.capture_editor_dungeon_pools(loaded_editor)
            swe.hydrate_celestial_system_from_world_files(
                loaded_editor, Path(self.chunks_var.get().strip())
            )
            self.editor = loaded_editor
            self.original_editor = json_clone(loaded_editor)
            self.system_file_state = swe.load_generated_system_file(system_destination)
            self.original_system_file_state = {
                "name": self.system_file_state["name"],
                "version": self.system_file_state["version"],
                "body": json_clone(self.system_file_state["body"]),
                "trailing": self.system_file_state["trailing"],
            }
            self.biome_pending = {}
            self.ordered_parallax_applied = {}
            self.world_layer_pending = {}
            self.loaded_biome_key = None
            self.loaded_world_layer_key = None
            self.capture_body_origins()
            self.star_map_dungeon_text = {}
            for planet_orbit, moon_orbit, parameters in self.iter_body_parameters():
                for layer in swe.celestial_layer_settings(parameters):
                    layer_id = str(layer["layerId"])
                    shown = display_dungeon_lists.get(
                        (planet_orbit, moon_orbit, layer_id),
                        saved_display_dungeons.get(
                            (planet_orbit, moon_orbit, layer_id),
                            list(layer.get("dungeons", [])),
                        ),
                    )
                    self.star_map_dungeon_text[(id(parameters), layer_id)] = (
                        json.dumps(shown, ensure_ascii=False)
                    )
            self.ocean_pair_store = saved_ocean_store
            self.load_ocean_pair_settings()
            self.load_system_fields()
            self.rebuild_body_tree()
            self.rebuild_station_tree()
            self.last_output = chunks
            self.audit_log(
                "save_system_completed",
                universe_folder=universe_folder,
                destinationLocation=destination_location,
                databaseBackup=backup,
                summary=summary,
                oceanResults=ocean_results,
                pendingAfterRegeneration=pending_after_regeneration,
            )
            self.set_status_key(
                "save_success", name=summary["newName"], backup=backup
            )
            messagebox.showinfo(
                self.tr("save_done"),
                self.tr(
                    "save_done_message",
                    chunks=chunks,
                    backup=backup,
                    old_count=summary["oldPlanetCount"],
                    new_count=summary["newPlanetCount"],
                    contexts=len(context_results),
                    references=sum(item[2] for item in context_results),
                ),
                parent=self.root,
            )
            if pending_after_regeneration:
                messagebox.showinfo(
                    self.tr("ocean_apply_title"),
                    self.tr(
                        "ocean_second_stage_required",
                        count=pending_after_regeneration,
                    ),
                    parent=self.root,
                )
                self.schedule_pending_ocean_monitor()
            if ocean_results:
                messagebox.showinfo(
                    self.tr("ocean_apply_title"),
                    self.tr(
                        "ocean_apply_done",
                        worlds=len(ocean_results),
                        regions=sum(
                            int(item.get("pairsApplied", 0))
                            for item in ocean_results
                        ),
                    ),
                    parent=self.root,
                )

        self.run_worker(task, success, "saving_system")

    def open_universe_folder(self) -> None:
        path = Path(self.chunks_var.get().strip())
        if path.is_dir():
            self.last_output = path / "universe.chunks"
            self.open_output_folder()
        else:
            messagebox.showerror(
                self.tr("invalid_path"), self.tr("invalid_chunks"), parent=self.root
            )


if __name__ == "__main__":
    CelestialSystemEditorWindow().run()
