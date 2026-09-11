"""Report major-dungeon tiles removed by the user's requested ocean reset.

Known dungeon overlaps are allowed. Unsupported/corrupt selected sector data and
unexpected writes outside the reset remain errors. The optional legacy assertion
is retained for diagnostic callers, but production uses the removal report.
"""

from __future__ import annotations

from pathlib import Path
import struct
from typing import Any, Sequence
import zlib

import starbound_world_editor as swe


class NativeDungeonInspectionError(ValueError):
    """Safety could not be established for the sectors about to be reset."""


class NativeDungeonConflictError(ValueError):
    """A basin reset would delete native major-dungeon tiles and entities."""


def _selected_rectangles(
    rectangles: Sequence[dict[str, Any]], width: int, height: int,
) -> list[tuple[list[tuple[int, int]], int, int]]:
    selected = []
    for index, rectangle in enumerate(rectangles):
        try:
            coordinates = [rectangle[name] for name in ("xStart", "xEnd", "yStart", "yEnd")]
            if any(isinstance(value, bool) or not isinstance(value, int) for value in coordinates):
                raise ValueError("rectangle coordinates must be integers")
            x_start, x_end, y_start, y_end = coordinates
            ranges = swe._wrapped_x_ranges(x_start, x_end, width)
            if not 0 <= y_start <= y_end < height:
                raise ValueError(f"Y bounds must be ordered inside 0..{height - 1}")
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeDungeonInspectionError(
                f"Cannot verify native dungeon safety: invalid ocean reset rectangle {index}: {exc}"
            ) from exc
        selected.append((ranges, y_start, y_end))
    return selected


def _major_dungeon_tile_counts(compressed: bytes, sector: tuple[int, int]) -> dict[int, int]:
    """Inspect known 417/418 layouts, including optional rootSource coordinates."""
    try:
        raw = zlib.decompress(compressed)
        _version, tile_offsets = swe.tile_sector_offsets(raw)
        # StarGameTypes.hpp: real DungeonId values are uint16 < 65520.
        # 65533 denotes biome microdungeons; 65535 denotes natural terrain.
        counts: dict[int, int] = {}
        for offset in tile_offsets:
            dungeon_id = struct.unpack_from(">H", raw, offset + 25)[0]
            if dungeon_id < 65520:
                counts[dungeon_id] = counts.get(dungeon_id, 0) + 1
        return counts
    except Exception as exc:
        raise NativeDungeonInspectionError(
            f"Cannot inspect native dungeon removals in sector {sector}: {exc}. "
            "The generated world cannot be verified for installation."
        ) from exc


def inspect_ocean_reset_major_dungeon_removals(
    world: Path, rectangles: Sequence[dict[str, Any]], *, rewritten_world: Path | None = None,
) -> dict[str, Any]:
    """Inspect the pre-basin native backup using actual 32x32 reset semantics.

    Whole intersecting sectors are deleted, even when a dungeon tile itself lies
    just outside the input rectangle. Overlapping rectangles inspect each sector
    once. Known major-dungeon overlaps are reported and allowed. When a rewritten
    file is supplied, its non-metadata records must exactly equal the core reset
    result, which also verifies untouched sectors and unique-entity index cleanup.
    This function performs no file writes.
    """
    database, records, document = swe.load_metadata_document(Path(world))
    try:
        size = document.get("size")
        if (not isinstance(size, list) or len(size) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in size)):
            raise NativeDungeonInspectionError("Cannot verify native dungeon safety: invalid world dimensions")
        width, height = size
        selections = _selected_rectangles(rectangles, width, height)
        checked: set[tuple[int, int]] = set()
        removed: dict[tuple[int, int], dict[int, int]] = {}
        for key, compressed in records:
            if not key or key[0] != swe.STORE_TILE_SECTOR:
                continue
            sector = swe.world_sector_coordinates(key)
            y_min = sector[1] * swe.WORLD_SECTOR_SIZE
            y_max = min(height - 1, y_min + swe.WORLD_SECTOR_SIZE - 1)
            if not any(
                swe._sector_intersects_x_ranges(sector[0], ranges, width)
                and y_min <= y_end and y_max >= y_start
                for ranges, y_start, y_end in selections
            ):
                continue
            checked.add(sector)
            tile_counts = _major_dungeon_tile_counts(compressed, sector)
            if tile_counts:
                removed[sector] = tile_counts
        verified = False
        if rewritten_world is not None:
            expected = list(records)
            for rectangle in rectangles:
                expected, _summary = swe.reset_generated_rectangle(
                    expected, width, height,
                    rectangle["xStart"], rectangle["xEnd"], rectangle["yStart"], rectangle["yEnd"],
                    allow_empty=True,
                )
            actual_database, actual_records = swe.load_records(Path(rewritten_world))
            try:
                expected_nonmetadata = {key: value for key, value in expected if key != swe.WORLD_METADATA_KEY}
                actual_nonmetadata = {key: value for key, value in actual_records if key != swe.WORLD_METADATA_KEY}
                if expected_nonmetadata != actual_nonmetadata:
                    changed_keys = sorted(set(expected_nonmetadata) ^ set(actual_nonmetadata))
                    changed_keys += [key for key in expected_nonmetadata.keys() & actual_nonmetadata.keys()
                                     if expected_nonmetadata[key] != actual_nonmetadata[key]]
                    raise NativeDungeonInspectionError(
                        "Ocean reset records differ from the requested sector reset; "
                        "untouched sectors or entity indexes may have changed. "
                        f"Sample record keys: {[key.hex() for key in changed_keys[:8]]}"
                    )
                verified = True
            finally:
                swe.close_database(actual_database)
        return {
            "checkedTileSectors": len(checked),
            "selectedRectangles": len(selections),
            "removedMajorDungeonSectorCount": len(removed),
            "removedMajorDungeonTileCount": sum(sum(counts.values()) for counts in removed.values()),
            "removedMajorDungeonIds": sorted({dungeon_id for counts in removed.values() for dungeon_id in counts}),
            "removedMajorDungeonSamples": [
                {"sector": [x, y],
                 "tileBounds": [x * 32, y * 32, min(width - 1, x * 32 + 31), min(height - 1, y * 32 + 31)],
                 "dungeonIds": sorted(removed[(x, y)]),
                 "tileCount": sum(removed[(x, y)].values())}
                for x, y in sorted(removed)[:8]
            ],
            "outsideResetRecordsVerified": verified,
            "overlapPolicy": "delete-with-reset",
        }
    finally:
        swe.close_database(database)


def assert_ocean_reset_preserves_major_dungeons(
    world: Path, rectangles: Sequence[dict[str, Any]],
) -> dict[str, int]:
    """Legacy diagnostic assertion; production intentionally permits deletion."""
    report = inspect_ocean_reset_major_dungeon_removals(world, rectangles)
    if report["removedMajorDungeonSectorCount"]:
        examples = "; ".join(
            f"({sample['sector'][0]}, {sample['sector'][1]}) "
            f"[tiles X={sample['tileBounds'][0]}..{sample['tileBounds'][2]}, "
            f"Y={sample['tileBounds'][1]}..{sample['tileBounds'][3]}]"
            for sample in report["removedMajorDungeonSamples"]
        )
        raise NativeDungeonConflictError(
            "Ocean basin generation would erase native major dungeons: "
            f"dungeon IDs {report['removedMajorDungeonIds']}; "
            f"{report['removedMajorDungeonSectorCount']} intersecting 32x32 sectors; {examples}. "
            "This diagnostic assertion performs no writes; the original universe is unchanged."
        )
    return {"checkedTileSectors": report["checkedTileSectors"],
            "selectedRectangles": report["selectedRectangles"], "majorDungeonConflicts": 0}
