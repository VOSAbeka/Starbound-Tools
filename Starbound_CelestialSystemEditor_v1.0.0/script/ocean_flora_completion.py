"""Complete only nearshore sectors with the world's own native flora and colours.

The temporary engine may bypass its biome-transition placement veto, but the
delivered world's layout/noise and all records outside the planned sectors stay
unchanged. Only 600 columns inward from each actual solid shore receive extra
plant completion, not the whole basin. No foreign plant/tile templates are used.
"""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import zlib

import starbound_world_editor as swe
import world_regeneration as native
import ocean_flora_plan
import ocean_flora_entities
import world_edit_guard


def sector_set(windows):
    return {(sx, sy) for x, y, width, height in windows
            for sx in range(x // 32, (x + width - 1) // 32 + 1)
            for sy in range(y // 32, (y + height - 1) // 32 + 1)}


def merge_coastal_records(original, generated, selected):
    """Import selected native sectors, including only their unique-index entries."""
    sector_types = {swe.STORE_TILE_SECTOR, swe.STORE_ENTITY_SECTOR, swe.STORE_SECTOR_UNIQUES}
    result = dict(original)
    for key in list(result):
        if key[0] in sector_types and swe.world_sector_coordinates(key) in selected:
            del result[key]
    for key, value in generated:
        if key[0] in sector_types and swe.world_sector_coordinates(key) in selected:
            result[key] = value
    old_buckets = {key: value for key, value in original if key[0] == swe.STORE_UNIQUE_INDEX}
    new_buckets = {key: value for key, value in generated if key[0] == swe.STORE_UNIQUE_INDEX}
    for key in old_buckets.keys() | new_buckets.keys():
        old = swe.read_unique_index_store(old_buckets[key]) if key in old_buckets else []
        new = swe.read_unique_index_store(new_buckets[key]) if key in new_buckets else []
        kept = [item for item in old if item[1] not in selected]
        imported = [item for item in new if item[1] in selected]
        if not imported and kept == old:
            continue  # Preserve the original bytes outside the target.
        names = {item[0] for item in kept}
        if any(item[0] in names for item in imported):
            raise swe.WorldFormatError("Coastal generation collided with an outside unique entity")
        if kept or imported:
            result[key] = swe.write_unique_index_store(kept + imported)
        else:
            result.pop(key, None)
    completed = set()
    for key, value in generated:
        if key[0] == swe.STORE_TILE_SECTOR and swe.world_sector_coordinates(key) in selected:
            stream = io.BytesIO(zlib.decompress(value))
            generation = swe.read_varuint(stream)
            if generation < 4:
                raise swe.WorldFormatError("Native coastal sector did not finish generation")
            completed.add(swe.world_sector_coordinates(key))
    if completed != selected:
        raise swe.WorldFormatError(f"Native coastal generation is missing {len(selected - completed)} sectors")
    return sorted(result.items())


def incomplete_sectors(records, selected):
    complete = set()
    for key, value in records:
        if key[0] == swe.STORE_TILE_SECTOR:
            sector = swe.world_sector_coordinates(key)
            if sector in selected and swe.read_varuint(io.BytesIO(zlib.decompress(value))) >= 4:
                complete.add(sector)
    return selected - complete


def prepare_plant_candidates(records, selected):
    """Re-run only Finalize in a disposable copy; never regenerate its tiles."""
    result = []
    for key, value in records:
        if key[0] == swe.STORE_TILE_SECTOR and swe.world_sector_coordinates(key) in selected:
            stream = io.BytesIO(zlib.decompress(value))
            if swe.read_varuint(stream) < 4:
                raise swe.WorldFormatError("Plant completion requires already generated terrain")
            output = io.BytesIO()
            swe.write_varuint(output, 3)
            output.write(stream.read())  # Exact tile bytes/version after the level.
            value = zlib.compress(output.getvalue(), level=9)
        result.append((key, value))
    return result


def verify_generation_identity(document, expected):
    """Native flora must come from these exact seeds/material appearances."""
    if ocean_flora_plan.generation_seed_snapshot(document) != expected:
        raise native.NativeGenerationError("Native flora generation changed the world seed or biome material colours")


def complete_in_place(world, assets, summary, *, universe_folder=None,
                      server_executable=None, progress=None, timeout_seconds=3600):
    """Finish a staged world; the caller must not expose it until this succeeds."""
    if not any(isinstance(rectangle, dict) and rectangle.get("coastalBasinProfile")
               for rectangle in summary.get("rectangles", [])):
        return {"performed": False, "reason": "no_curved_ocean_floor"}
    world, assets = Path(world).resolve(), Path(assets).resolve()
    document = swe.load_metadata_only(world)
    plan = ocean_flora_plan.plan_ocean_flora_windows(document, summary)
    if not plan["windows"]:
        return {"performed": False, "reason": "no_solid_shore_for_plant_completion", "plan": plan}
    universe_folder = Path(universe_folder or world.parent).resolve()
    server = Path(server_executable).resolve() if server_executable else native.resolve_server_executable(
        universe_folder / "universe.chunks", assets)
    if not server.is_file():
        raise native.NativeGenerationError(f"Native coastal generator is missing: {server}")
    diagnostic = Path(tempfile.mkdtemp(prefix="swe05-coastal-flora-"))
    storage = diagnostic / "storage"
    universe = storage / "universe"
    universe.mkdir(parents=True)
    template = swe.world_template(document)
    parameters = copy.deepcopy(template["celestialParameters"])
    parameters["visitableParameters"] = copy.deepcopy(template["worldParameters"])
    coordinate = native._coordinate(parameters)
    private_world = universe / native._world_filename(coordinate)
    selected = sector_set(plan["windows"])
    events = []

    def emit(event, **details):
        item = {"event": event, **details}
        events.append(item)
        with (diagnostic / "flora.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        if progress:
            try:
                progress(event, details)
            except Exception:
                pass

    emit("native_coastal_flora_preparing", coordinate=list(coordinate), plan=plan,
         diagnosticDirectory=str(diagnostic), worldSeed=template.get("seed"),
         sourceColours="same-world compiled materialHueShift / native colour variants")
    database, original, document = swe.load_metadata_document(world)
    try:
        # First obtain terrain by the UNMODIFIED native rules. Existing complete
        # sectors are kept byte-for-byte, including the user's colours/buildings.
        # Only missing terrain is generated; this is not a foreign coast prefab.
        missing = incomplete_sectors(original, selected)
        swe.atomic_write_world(world, private_world, original, database)
        for filename in ("universe.chunks", "universe.dat"):
            source = universe_folder / filename
            if source.is_file():
                shutil.copy2(source, universe / filename)
        expected_layout = copy.deepcopy(swe.world_template(document)["regionData"])
        expected_identity = ocean_flora_plan.generation_seed_snapshot(document)
        baseline = list(original)
        if missing:
            terrain_budget = native.native_viewport_timeout_budget(
                plan["windows"], phase="terrain", minimum_seconds=min(timeout_seconds, 14400))
            emit("native_coastal_terrain_required", sectorCount=len(missing), timeoutBudget=terrain_budget)
            native._run_native_pass(server, diagnostic, storage,
                [(parameters, coordinate, private_world)], time.monotonic() + terrain_budget["timeoutSeconds"], emit, "flora-terrain",
                viewport_windows_by_coordinate={tuple(coordinate): plan["windows"]})
            base_db, base_records, base_document = swe.load_metadata_document(private_world)
            try:
                verify_generation_identity(base_document, expected_identity)
                comparison = native._compare_native_layout(swe.world_template(base_document)["regionData"], expected_layout)
                if not comparison["matches"]:
                    emit("native_coastal_terrain_layout_mismatch", comparison=comparison)
                    raise native.NativeGenerationError("Native terrain generation changed the edited layout: "
                                                       + str(comparison.get("difference")))
                baseline = merge_coastal_records(original, base_records, missing)
            finally:
                swe.close_database(base_db)
        candidates = prepare_plant_candidates(baseline, selected)
        candidate_input = diagnostic / "candidate-input.world"
        swe.atomic_write_world(world, candidate_input, candidates, database)
        shutil.copyfile(candidate_input, private_world)
        plant_budget = native.native_viewport_timeout_budget(
            plan["windows"], phase="plants", minimum_seconds=min(timeout_seconds, 14400))
        emit("native_coastal_plant_candidates_started", terrainAlreadyComplete=True,
             existingCompleteSectorsPreserved=len(selected) - len(missing), timeoutBudget=plant_budget)
        native._run_native_pass(server, diagnostic, storage,
            [(parameters, coordinate, private_world)], time.monotonic() + plant_budget["timeoutSeconds"],
            emit, "flora", viewport_windows_by_coordinate={tuple(coordinate): plan["windows"]},
            biome_transition_threshold=0.0)
        generated_db, generated, actual = swe.load_metadata_document(private_world)
        try:
            unfinished = incomplete_sectors(generated, selected)
            if unfinished:
                raise native.NativeGenerationError(
                    f"Native plant finalization did not complete {len(unfinished)} coastal sectors")
            native._verify_parameters(actual, parameters)
            verify_generation_identity(actual, expected_identity)
            comparison = native._compare_native_layout(swe.world_template(actual)["regionData"], expected_layout)
            if not comparison["matches"]:
                raise native.NativeGenerationError("Native coastal generation changed layout/colour/seed metadata: "
                                                   + str(comparison.get("difference")))
            merged, plant_summary = ocean_flora_entities.merge_native_plants(
                baseline, generated, selected, assets, ocean_summary=summary)
            # Keep all original metadata, not the diagnostic universe/server state.
            staged = diagnostic / "merged.world"
            swe.atomic_write_world(world, staged, merged, database)
        finally:
            swe.close_database(generated_db)
    except Exception as exc:
        emit("native_coastal_flora_failed", error=str(exc), diagnosticDirectory=str(diagnostic))
        raise native.NativeGenerationError(f"{exc}\nCoastal flora diagnostics: {diagnostic}") from exc
    finally:
        swe.close_database(database)
    # No live data is changed until every native sector and layout check passed.
    shutil.copyfile(staged, world)
    result = {"performed": True, "targetSectorCount": len(selected),
              "windowCount": len(plan["windows"]), "plan": plan,
              "shoreWidthTiles": ocean_flora_plan.DEFAULT_SHORE_FLORA_WIDTH,
              "worldSeed": template.get("seed"), "layoutAndColourParametersPreserved": True,
              "outsideSectorRecordsPreserved": True, "diagnosticDirectory": str(diagnostic),
              "normalNativeTerrainSectorsCreated": len(missing),
              "plantOnlyCompletion": plant_summary}
    emit("native_coastal_flora_completed", **result)
    swe.atomic_json_dump({"plan": plan, "events": events, "result": result}, diagnostic / "flora.json")
    return result


def _edit_and_complete(world, assets, operation, values, *, progress=None, guard_source=False):
    world, assets = Path(world).resolve(), Path(assets).resolve()
    snapshot = None
    if guard_source:
        world_edit_guard.assert_world_available(world)
        snapshot = world_edit_guard.fingerprint(world)

    def builder(backup, staged):
        if snapshot is not None:
            backup_state = world_edit_guard.fingerprint(backup)
            if (backup_state["size"], backup_state["sha256"]) != (snapshot["size"], snapshot["sha256"]):
                raise RuntimeError("Target .world changed while backing up; no result was installed")
        with tempfile.TemporaryDirectory(prefix=".swe05-coastal-edit-", dir=world.parent) as folder:
            edited = Path(folder) / world.name
            shutil.copy2(backup, edited)
            _, summary = operation(edited, assets, values, progress=progress)
            summary["nativeCoastalFlora"] = complete_in_place(edited, assets, summary,
                universe_folder=world.parent, progress=progress)
            shutil.copyfile(edited, staged)
            # A curve/plant build may take time. Recheck the actual world just
            # before the outer atomic commit, not the private generation copy.
            if snapshot is not None:
                world_edit_guard.assert_world_unchanged(world, snapshot)
            return summary

    return swe.rebuild_world_in_place_from_backup(world, builder, keep_backup=True)


def apply_pairs_in_place(world, assets, settings, *, progress=None):
    return _edit_and_complete(world, assets, swe.apply_generated_ocean_floor_pairs_in_place,
                              settings, progress=progress)


def regenerate_rectangles_in_place(world, assets, plan, *, progress=None):
    return _edit_and_complete(world, assets, swe.regenerate_world_biome_rectangles_in_place,
                              plan, progress=progress, guard_source=True)
