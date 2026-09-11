from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import starbound_world_editor as swe


OLD_SURFACE = 12000
NEW_SURFACE = 11997
SEABED_RISE_TILES = 30
PIXELS_PER_TILE = 8


def biome_name(biomes: list[dict[str, Any]], index: Any) -> str:
    if isinstance(index, int) and 1 <= index <= len(biomes):
        return str(biomes[index - 1].get("baseName", ""))
    return ""


def cell_has_biome(
    cell: dict[str, Any], biomes: list[dict[str, Any]], names: set[str]
) -> bool:
    return any(
        biome_name(biomes, cell.get(key)) in names
        for key in ("blockBiomeIndex", "environmentBiomeIndex")
    )


def move_parallax_up(parallax: dict[str, Any], tiles: int) -> dict[str, int]:
    pixel_delta = int(tiles) * PIXELS_PER_TILE
    layers = parallax.get("layers")
    if not isinstance(layers, list):
        raise swe.WorldFormatError("oceanfloor parallax has no layer list")
    moved_layers = 0
    moved_limits = 0
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        offset = layer.get("parallaxOffset")
        if not isinstance(offset, list) or len(offset) < 2:
            raise swe.WorldFormatError("oceanfloor parallax layer has no 2D offset")
        offset[1] = float(offset[1]) - pixel_delta
        moved_layers += 1
        for key in ("tileLimitTop", "tileLimitBottom"):
            if isinstance(layer.get(key), (int, float)):
                layer[key] = float(layer[key]) - pixel_delta
                moved_limits += 1
    return {"layers": moved_layers, "limits": moved_limits, "pixels": pixel_delta}


def shift_parallax_vertical_origin(parallax: dict[str, Any], delta: float) -> int:
    changed = 0
    if isinstance(parallax.get("verticalOrigin"), (int, float)):
        parallax["verticalOrigin"] = float(parallax["verticalOrigin"]) + delta
        changed += 1
    layers = parallax.get("layers")
    if isinstance(layers, list):
        for layer in layers:
            if isinstance(layer, dict) and isinstance(
                layer.get("verticalOrigin"), (int, float)
            ):
                layer["verticalOrigin"] = float(layer["verticalOrigin"]) + delta
                changed += 1
    return changed


def segment_at(
    segments: list[tuple[int, int, int, int, int, int, int]], x: int, y: int
) -> tuple[int, int, int, int, int, int, int] | None:
    for segment in segments:
        ys, ye, xs, xe, _liquid, _old_level, _new_level = segment
        if ys <= y <= ye and xs <= x <= xe:
            return segment
    return None


def change_saved_ocean_liquid(
    records: list[tuple[bytes, bytes]],
    width: int,
    height: int,
    segments: list[tuple[int, int, int, int, int, int, int]],
) -> tuple[list[tuple[bytes, bytes]], dict[str, int]]:
    changed_segments = [segment for segment in segments if segment[5] == OLD_SURFACE]
    if not changed_segments:
        return records, {
            "changedTileSectors": 0,
            "clearedSurfaceTiles": 0,
            "correctedSourcePressureTiles": 0,
        }

    min_x = min(segment[2] for segment in segments)
    max_x = max(segment[3] for segment in segments)
    min_y = min(segment[0] for segment in changed_segments)
    max_y = NEW_SURFACE + (OLD_SURFACE - NEW_SURFACE) - 1
    changed_sectors = 0
    cleared = 0
    corrected_pressure = 0
    updated: list[tuple[bytes, bytes]] = []

    for key, compressed in records:
        if key[0] != swe.STORE_TILE_SECTOR:
            updated.append((key, compressed))
            continue
        sector_x, sector_y = swe.world_sector_coordinates(key)
        sx0 = sector_x * swe.WORLD_SECTOR_SIZE
        sy0 = sector_y * swe.WORLD_SECTOR_SIZE
        sx1 = min(width - 1, sx0 + swe.WORLD_SECTOR_SIZE - 1)
        sy1 = min(height - 1, sy0 + swe.WORLD_SECTOR_SIZE - 1)
        if sx0 > max_x or sx1 < min_x or sy0 > max_y or sy1 < min_y:
            updated.append((key, compressed))
            continue

        raw = bytearray(zlib.decompress(compressed))
        expected_size = 3 + swe.WORLD_SECTOR_SIZE * swe.WORLD_SECTOR_SIZE * 31
        if len(raw) != expected_size:
            raise swe.WorldFormatError(
                f"Tile sector {key.hex()} has {len(raw)} bytes, expected {expected_size}"
            )
        sector_changed = False
        for local_y in range(swe.WORLD_SECTOR_SIZE):
            y = sy0 + local_y
            if y < min_y or y > max_y or y >= height:
                continue
            for local_x in range(swe.WORLD_SECTOR_SIZE):
                x = sx0 + local_x
                if x < min_x or x > max_x or x >= width:
                    continue
                segment = segment_at(segments, x, y)
                if segment is None:
                    continue
                _ys, _ye, _xs, _xe, ocean_liquid, old_level, new_level = segment
                offset = 3 + (local_y * swe.WORLD_SECTOR_SIZE + local_x) * 31
                liquid_id = raw[offset + 14]
                if liquid_id != ocean_liquid:
                    continue

                if new_level <= y < OLD_SURFACE and new_level == NEW_SURFACE:
                    raw[offset + 14] = 0
                    struct.pack_into(">f", raw, offset + 15, 0.0)
                    struct.pack_into(">f", raw, offset + 19, 0.0)
                    raw[offset + 23] = 0
                    cleared += 1
                    sector_changed = True
                elif y < new_level and old_level == OLD_SURFACE and raw[offset + 23] == 1:
                    wanted_pressure = float(new_level - y)
                    current_pressure = struct.unpack_from(">f", raw, offset + 19)[0]
                    if abs(current_pressure - wanted_pressure) > 1e-6:
                        struct.pack_into(">f", raw, offset + 19, wanted_pressure)
                        corrected_pressure += 1
                        sector_changed = True

        if sector_changed:
            compressed = zlib.compress(raw, level=9)
            changed_sectors += 1
        updated.append((key, compressed))

    return updated, {
        "changedTileSectors": changed_sectors,
        "clearedSurfaceTiles": cleared,
        "correctedSourcePressureTiles": corrected_pressure,
    }


def verify_output(
    output: Path,
    expected_oceanfloor_index: int,
    expected_ocean_indexes: set[int],
) -> dict[str, Any]:
    database, records, document = swe.load_metadata_document(output)
    try:
        biomes = swe.compiled_biomes(document)
        floor = biomes[expected_oceanfloor_index - 1]
        floor_layers = floor["parallax"]["layers"]
        remaining_old_surface_cells = sum(
            1
            for cell in swe.iter_compiled_cells(document)
            if cell_has_biome(cell, biomes, {"ocean"})
            and cell.get("oceanLiquid")
            and float(cell.get("oceanLiquidLevel", -1)) == OLD_SURFACE
        )
        ocean_origins = {
            str(index): biomes[index - 1].get("parallax", {}).get("verticalOrigin")
            for index in sorted(expected_ocean_indexes)
        }

        bad_saved_surface_tiles = 0
        width, height = map(int, document["size"])
        target_segments: list[tuple[int, int, int, int, int, int, int]] = []
        for ys, ye, layer in swe._layer_vertical_segments(document):
            for xs, xe, cell in swe._layer_horizontal_segments(layer, width):
                if (
                    cell_has_biome(cell, biomes, {"ocean"})
                    and int(cell.get("oceanLiquid", 0)) != 0
                    and int(cell.get("oceanLiquidLevel", -1)) == NEW_SURFACE
                ):
                    target_segments.append(
                        (ys, ye, xs, xe, int(cell["oceanLiquid"]), NEW_SURFACE, NEW_SURFACE)
                    )
        for key, compressed in records:
            if key[0] != swe.STORE_TILE_SECTOR:
                continue
            sector_x, sector_y = swe.world_sector_coordinates(key)
            sy0 = sector_y * swe.WORLD_SECTOR_SIZE
            sy1 = sy0 + swe.WORLD_SECTOR_SIZE - 1
            if sy0 > OLD_SURFACE - 1 or sy1 < NEW_SURFACE:
                continue
            sx0 = sector_x * swe.WORLD_SECTOR_SIZE
            raw = zlib.decompress(compressed)
            for local_y in range(swe.WORLD_SECTOR_SIZE):
                y = sy0 + local_y
                if not NEW_SURFACE <= y < OLD_SURFACE or y >= height:
                    continue
                for local_x in range(swe.WORLD_SECTOR_SIZE):
                    x = sx0 + local_x
                    if x >= width:
                        continue
                    segment = segment_at(target_segments, x, y)
                    if segment is None:
                        continue
                    offset = 3 + (local_y * swe.WORLD_SECTOR_SIZE + local_x) * 31
                    if raw[offset + 14] == segment[4]:
                        bad_saved_surface_tiles += 1

        return {
            "remainingOceanCellsAt12000": remaining_old_surface_cells,
            "savedOceanTilesRemainingAt11997To11999": bad_saved_surface_tiles,
            "oceanfloorLayerCount": len(floor_layers),
            "oceanfloorOffsetY": [layer["parallaxOffset"][1] for layer in floor_layers],
            "oceanfloorTileLimits": [
                [layer.get("tileLimitTop"), layer.get("tileLimitBottom")]
                for layer in floor_layers
            ],
            "oceanParallaxVerticalOrigins": ocean_origins,
        }
    finally:
        swe.close_database(database)


def adjust_world(source: Path, output: Path) -> dict[str, Any]:
    database, records, document = swe.load_metadata_document(source)
    try:
        biomes = swe.compiled_biomes(document)
        width, height = map(int, document["size"])
        referenced_indexes: set[int] = set()
        layout_segments: list[tuple[int, int, int, int, dict[str, Any]]] = []
        for ys, ye, layer in swe._layer_vertical_segments(document):
            for xs, xe, cell in swe._layer_horizontal_segments(layer, width):
                layout_segments.append((ys, ye, xs, xe, cell))
                for key in ("blockBiomeIndex", "environmentBiomeIndex"):
                    if isinstance(cell.get(key), int):
                        referenced_indexes.add(int(cell[key]))

        live_oceanfloor_indexes = {
            index
            for index in referenced_indexes
            if biome_name(biomes, index) == "oceanfloor"
        }
        if len(live_oceanfloor_indexes) != 1:
            raise ValueError(
                "Expected exactly one referenced oceanfloor, got "
                + repr(sorted(live_oceanfloor_indexes))
            )
        oceanfloor_index = next(iter(live_oceanfloor_indexes))
        floor_move = move_parallax_up(
            biomes[oceanfloor_index - 1]["parallax"], SEABED_RISE_TILES
        )

        liquid_segments: list[tuple[int, int, int, int, int, int, int]] = []
        changed_cells = 0
        changed_ocean_indexes: set[int] = set()
        seen_cells: set[int] = set()
        for ys, ye, xs, xe, cell in layout_segments:
            if not cell_has_biome(cell, biomes, {"ocean"}):
                continue
            liquid = int(cell.get("oceanLiquid", 0))
            level_value = cell.get("oceanLiquidLevel")
            if not liquid or not isinstance(level_value, (int, float)):
                continue
            old_level = int(level_value)
            new_level = NEW_SURFACE if old_level == OLD_SURFACE else old_level
            if old_level in (OLD_SURFACE, NEW_SURFACE):
                liquid_segments.append(
                    (ys, ye, xs, xe, liquid, old_level, new_level)
                )
            if old_level != OLD_SURFACE:
                continue
            if id(cell) not in seen_cells:
                cell["oceanLiquidLevel"] = NEW_SURFACE
                seen_cells.add(id(cell))
                changed_cells += 1
            for key in ("blockBiomeIndex", "environmentBiomeIndex"):
                index = cell.get(key)
                if biome_name(biomes, index) == "ocean":
                    changed_ocean_indexes.add(int(index))

        if not changed_cells:
            raise ValueError("No referenced ocean cells with oceanLiquidLevel 12000 were found")

        shifted_origin_fields = 0
        for index in sorted(changed_ocean_indexes):
            parallax = biomes[index - 1].get("parallax")
            if isinstance(parallax, dict):
                shifted_origin_fields += shift_parallax_vertical_origin(parallax, -3.0)

        updated_records, liquid_stats = change_saved_ocean_liquid(
            records, width, height, liquid_segments
        )
        metadata_value = swe.encode_world_metadata(document)
        replacement_records = [
            (key, metadata_value if key == swe.WORLD_METADATA_KEY else value)
            for key, value in updated_records
        ]
        swe.atomic_write_world(source, output, replacement_records, database)
    finally:
        swe.close_database(database)

    verification = verify_output(output, oceanfloor_index, changed_ocean_indexes)
    if verification["remainingOceanCellsAt12000"] != 0:
        raise RuntimeError("Verification failed: referenced ocean cells still use level 12000")
    if verification["savedOceanTilesRemainingAt11997To11999"] != 0:
        raise RuntimeError("Verification failed: saved ocean water remains above level 11997")
    return {
        "source": str(source),
        "output": str(output),
        "sourcePreserved": True,
        "oceanfloorBiomeIndex": oceanfloor_index,
        "oceanfloorParallax": floor_move,
        "oceanCellsChangedFrom12000To11997": changed_cells,
        "oceanBiomeIndexesAdjusted": sorted(changed_ocean_indexes),
        "oceanParallaxVerticalOriginFieldsAdjusted": shifted_origin_fields,
        "savedLiquid": liquid_stats,
        "verification": verification,
        "outputBytes": output.stat().st_size,
        "outputSha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error(f"Source does not exist: {args.source}")
    try:
        result = adjust_world(args.source, args.output)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
