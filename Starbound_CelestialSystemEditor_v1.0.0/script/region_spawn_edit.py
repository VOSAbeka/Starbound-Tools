"""Guarded, metadata-only save for a selected rectangle's monster pool."""
from __future__ import annotations

from pathlib import Path

import region_spawn_replacement as replacement
import starbound_world_editor as swe
import world_edit_guard


def inspect_world_rectangle(world, rectangle):
    world = Path(world).resolve()
    before = world_edit_guard.fingerprint(world)
    document = swe.load_metadata_only(world)
    result = replacement.inspect_spawn_rectangle(
        document, *(rectangle[key] for key in ("xStart", "xEnd", "yStart", "yEnd")))
    world_edit_guard.assert_world_unchanged(world, before)
    return result


def replace_in_place(world, rectangle, profile, *, known_spawn_types=None, progress=None):
    """Replace only the selected automatic spawn rule, preserving saved records.

    Empty or identical input is a no-op. No tile, entity, unique-index or other
    non-metadata record is decoded or rewritten. If applying the rule would
    require whole-world biome-index compaction, the operation fails instead of
    changing saved tile references behind a metadata-only promise.
    """
    normalized = replacement.normalize_spawn_profile(
        profile, known_spawn_types=known_spawn_types)
    if normalized is None:
        return None, {
            "spawnOnly": True, "changed": False, "resetSectors": 0,
            "removedMonsterEntities": 0, "preservedSavedMonsterEntities": True,
            "preservedAllNonMetadataRecords": True,
        }
    world = Path(world).resolve()
    world_edit_guard.assert_world_available(world)
    fingerprint = world_edit_guard.fingerprint(world)
    document = swe.load_metadata_only(world)
    result = replacement.set_spawn_profile_rectangle(
        document,
        *(rectangle[key] for key in ("xStart", "xEnd", "yStart", "yEnd")),
        normalized,
    )
    world_edit_guard.assert_world_unchanged(world, fingerprint)
    common = {
        "resetSectors": 0, "removedTileRecords": 0, "removedEntityRecords": 0,
        "removedMonsterEntities": 0, "preservedSavedMonsterEntities": True,
        "preservedAllNonMetadataRecords": True, "biomeIndexCompaction": None,
    }
    if not result["changed"]:
        return None, {**result, **common, "spawnProfileChanged": False, "changed": False}
    if result["requiresBiomeCompaction"]:
        raise replacement.BiomeIndexCapacityError(
            "Spawn replacement would require rewriting saved tile biome indexes; "
            "the metadata-only spawn editor leaves the world unchanged instead."
        )

    def emit(event, **details):
        if progress is not None:
            try:
                progress(event, details)
            except Exception:
                pass

    def builder(backup, staged):
        saved = world_edit_guard.fingerprint(backup)
        if (saved["size"], saved["sha256"]) != (fingerprint["size"], fingerprint["sha256"]):
            raise RuntimeError(
                "Target .world changed while backing up; spawn edit was not installed")
        database, records = swe.load_records(backup)
        try:
            swe.validate_native_biome_indexes(document)
            region = swe.world_template(document)["regionData"]
            swe.validate_project_terrain(
                {"compiledLayers": region["layers"],
                 "terrainSelectors": region.get("terrainSelectors")},
                *document["size"], len(swe.compiled_biomes(document)))
            encoded = swe.encode_world_metadata(document)
            output = [
                (key, encoded if key == swe.WORLD_METADATA_KEY else value)
                for key, value in records
            ]
            swe.atomic_write_world(backup, staged, output, database)
            # Source availability is checked again immediately before commit.
            world_edit_guard.assert_world_unchanged(world, fingerprint)
            return {
                **result, **common, "changed": True, "spawnProfileChanged": True,
                "rectangle": dict(rectangle),
            }
        finally:
            swe.close_database(database)

    backup, summary = swe.rebuild_world_in_place_from_backup(
        world, builder, keep_backup=True)
    emit("spawn_world_replaced_and_verified", world=str(world),
         backup=str(backup), summary=summary)
    return backup, summary
