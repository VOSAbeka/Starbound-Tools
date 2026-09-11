"""Import native plant entities without importing any native tile changes.

The candidate must be a native Finalize-only pass over the supplied baseline,
not a freshly reset world. This module never writes a file. It keeps every
baseline record byte-for-byte except entity sectors receiving accepted plants.
Even there, each old serialized entity is copied verbatim and in its old order.

Footprints use conservative PNG rectangles (standard library only). Native has
already checked actual alpha masks, roots and other entities. Requiring the
baseline/candidate foreground occupancy to agree throughout each rectangle and
its root row
prevents importing a plant that relied on native's small terrain adjustments,
including adjustments made by a different candidate plant. Transparent pixels
can cause a harmless false rejection; they cannot admit a terrain change.
"""

from __future__ import annotations

from collections import OrderedDict
import io
import json
import math
from pathlib import Path
import struct
from typing import Any, Iterable, Sequence
import zlib

import starbound_world_editor as swe
import ocean_flora_plan


class PlantMergeError(ValueError):
    """The native entity-only merge could not be verified safely."""


def _entity_entries(compressed: bytes, sector: tuple[int, int]):
    try:
        return swe.decode_entity_sector(compressed)
    except Exception as exc:
        raise PlantMergeError(f"Cannot decode entity sector {sector}: {exc}") from exc


def _encode_entries(entries) -> bytes:
    return swe.encode_entity_sector(entries)


def _record_map(records, label):
    result = {}
    for key, value in records:
        if not isinstance(key, bytes) or len(key) != 5 or not isinstance(value, bytes):
            raise PlantMergeError(f"Invalid {label} World4 record")
        if key in result:
            raise PlantMergeError(f"Duplicate {label} World4 record key {key.hex()}")
        result[key] = value
    return result


class _TileReader:
    def __init__(self, records, size, *, cache_limit=128):
        if isinstance(cache_limit, bool) or not isinstance(cache_limit, int) or cache_limit < 1:
            raise ValueError("Tile cache limit must be a positive integer")
        self.records = records
        self.width, self.height = size
        self.cache_limit = cache_limit
        self.cache = OrderedDict()

    def tile(self, x, y):
        if not 0 <= y < self.height:
            return None
        x %= self.width
        sector = x // 32, y // 32
        if sector in self.cache:
            self.cache.move_to_end(sector)
        else:
            key = bytes([swe.STORE_TILE_SECTOR]) + struct.pack(">HH", *sector)
            compressed = self.records.get(key)
            if compressed is None:
                self.cache[sector] = None
            else:
                try:
                    stream = io.BytesIO(zlib.decompress(compressed))
                    stage, version = swe.read_varuint(stream), swe.read_varuint(stream)
                    if stage not in range(6) or version not in (417, 418):
                        raise ValueError(f"unsupported generation {stage} / tile version {version}")
                    tiles = []
                    for _ in range(1024):
                        tile = swe._read_exact(stream, 31)
                        if version == 418:
                            if tile[30] not in (0, 1):
                                raise ValueError("invalid optional rootSource flag")
                            if tile[30]:
                                # Maybe<Vec2I>: boolean followed by two fixed int32s.
                                tile += swe._read_exact(stream, 8)
                        tiles.append(tile)
                    if stream.read():
                        raise ValueError("unexpected trailing tile bytes")
                    self.cache[sector] = tiles
                except Exception as exc:
                    raise PlantMergeError(f"Cannot inspect plant-support sector {sector}: {exc}") from exc
            # A full coast can cover thousands of sectors. Retain only the
            # current neighbourhood instead of two unbounded copies of every
            # individual tile object. Missing-sector None entries count too.
            if len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
        tiles = self.cache[sector]
        return None if tiles is None else tiles[(y % 32) * 32 + x % 32]


def _terrain_signature(tile):
    # Plant survival depends on foreground space/root support, not background,
    # hue, color, grass mods or liquids. Those native changes are discarded, not
    # imported. Null/Boundary cannot stand in for native-validated empty space.
    foreground = struct.unpack_from(">H", tile)[0]
    return (foreground == 65535, foreground not in (65535, 65534, 65526))


def _plant_identity(data, width):
    if not isinstance(data, dict):
        raise ValueError("plant content must be an object")
    position = data.get("tilePosition")
    if (not isinstance(position, list) or len(position) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in position)):
        raise ValueError("plant tilePosition must contain two integers")
    pieces = data.get("pieces")
    if not isinstance(pieces, list) or not pieces or len(pieces) > 10000:
        raise ValueError("plant must have a bounded nonempty pieces list")
    identities = []
    for piece in pieces:
        if not isinstance(piece, dict) or not isinstance(piece.get("image"), str):
            raise ValueError("plant piece has no image")
        # rotationOffset is a native animation phase, not a new plant identity.
        identities.append({k: v for k, v in piece.items() if k != "rotationOffset"})
    return (position[0] % width, position[1], bool(data.get("ceiling", False)),
            json.dumps(identities, sort_keys=True, ensure_ascii=True, allow_nan=False))


class _Footprints:
    def __init__(self, assets_folder):
        self.assets_folder = assets_folder
        self.reader = None
        self.dimensions = {}

    def _size(self, image):
        path, *directives = image.split("?")
        if (not path.startswith("/") or "\\" in path or ".." in path.split("/")
                or ":" in path or not path.lower().endswith(".png")):
            raise ValueError("unverified non-PNG/frame plant image")
        allowed = {"hueshift", "saturation", "brightness", "fade", "scanlines", "setcolor",
                   "replace", "multiply", "blendmult"}
        if any(item.split("=", 1)[0].lower() not in allowed for item in directives if item):
            raise ValueError("unverified plant image sizing directive")
        if path not in self.dimensions:
            if self.reader is None:
                if self.assets_folder is None:
                    raise ValueError("plant footprint requires its asset source")
                self.reader = swe.StarboundAssetReader(Path(self.assets_folder))
            png = self.reader.read_bytes(path)
            if len(png) < 33 or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
                raise ValueError("plant image has no PNG IHDR")
            if struct.unpack_from(">I", png, 8)[0] != 13:
                raise ValueError("invalid PNG IHDR size")
            if zlib.crc32(png[12:29]) & 0xffffffff != struct.unpack_from(">I", png, 29)[0]:
                raise ValueError("invalid PNG IHDR checksum")
            width, height = struct.unpack_from(">II", png, 16)
            if not 0 < width <= 8192 or not 0 < height <= 8192:
                raise ValueError("plant image dimensions exceed safe limits")
            self.dimensions[path] = width, height
        return self.dimensions[path]

    def rectangles(self, data):
        rectangles = [(-1, -1, 1, 1)]
        for piece in data["pieces"]:
            offset = piece.get("offset")
            if (not isinstance(offset, list) or len(offset) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) or abs(v) > 8192 for v in offset)):
                raise ValueError("invalid plant piece offset")
            width, height = self._size(piece["image"])
            x0, y0 = math.floor(offset[0]), math.floor(offset[1])
            x1, y1 = math.ceil(offset[0] + width / 8) - 1, math.ceil(offset[1] + height / 8) - 1
            # All roots lie one tile above/below spaces on anchor row zero.
            if y0 <= 0 <= y1:
                if data.get("ceiling", False):
                    y1 = max(y1, 1)
                else:
                    y0 = min(y0, -1)
            rectangles.append((x0, y0, x1, y1))
        if sum((x1-x0+1)*(y1-y0+1) for x0,y0,x1,y1 in rectangles) > 100000:
            raise ValueError("plant footprint exceeds safe tile-check limit")
        return rectangles


def merge_native_plants(
    original_records: Sequence[tuple[bytes, bytes]],
    candidate_records: Sequence[tuple[bytes, bytes]],
    selected_sectors: Iterable[Sequence[int]],
    assets_folder: Path | None = None,
    *, ocean_summary: dict[str, Any] | None = None,
) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    """Append safe native PlantEntity v3 entries; retain ALL baseline tile bytes.

    Only anchors and entity records inside ``selected_sectors`` are admitted.
    The complete conservative footprint must also remain inside selected sectors.
    Missing image/unsupported sizing directives skip that plant, not the save.
    When enabled ocean profiles are supplied, new plants must belong to one of
    their exact rectangles AND its floor/ocean block-biome index groups, and
    their whole horizontal footprint must fit the 600-column band inward from
    an actual solid shore. A viewport's sector padding does not extend this band.
    Corrupt selected tile/entity serialization fails before returning any output.
    Caller must generate candidate from this exact baseline with its original
    native seed and assets, and install the returned records transactionally.
    """
    original_records = list(original_records)
    candidate_records = list(candidate_records)
    original = _record_map(original_records, "baseline")
    candidate = _record_map(candidate_records, "candidate")
    try:
        size = swe.decode_world_metadata(original[swe.WORLD_METADATA_KEY])["size"]
        candidate_size = swe.decode_world_metadata(candidate[swe.WORLD_METADATA_KEY])["size"]
        if size != candidate_size or any(v <= 0 for v in size):
            raise ValueError("baseline/candidate dimensions must agree and be positive")
    except Exception as exc:
        raise PlantMergeError(f"Cannot establish plant merge world dimensions: {exc}") from exc
    selected = set()
    for sector in selected_sectors:
        if (not isinstance(sector, (list, tuple)) or len(sector) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) for v in sector)
                or not 0 <= sector[0] < math.ceil(size[0]/32)
                or not 0 <= sector[1] < math.ceil(size[1]/32)):
            raise PlantMergeError(f"Invalid selected plant sector: {sector!r}")
        selected.add(tuple(sector))
    summary = {"candidatePlants": 0, "addedPlants": 0, "duplicatesSkipped": 0,
               "occupiedAnchorsSkipped": 0,
               "nonPlantEntitiesIgnored": 0, "outsideSelectionSkipped": 0,
               "terrainChangeRejected": 0, "missingBaselineTilesRejected": 0,
               "unsupportedPlantRejected": 0, "actualDepthRejected": 0,
               "outsideOceanScopeRejected": 0,
               "outsideShoreRangeRejected": 0,
               "shoreWidthTiles": ocean_flora_plan.DEFAULT_SHORE_FLORA_WIDTH,
               "modifiedEntitySectors": 0,
               "tileRecordsByteIdentical": True, "nonEntityRecordsByteIdentical": True,
               "originalEntityEntriesByteIdentical": True, "rejectionSamples": []}
    existing_entries = {}
    identities = set()
    occupied_anchors = set()
    for key, value in original_records:
        if key[0] != swe.STORE_ENTITY_SECTOR or swe.world_sector_coordinates(key) not in selected:
            continue
        entries = _entity_entries(value, swe.world_sector_coordinates(key))
        existing_entries[key] = entries
        for name, version, data, raw in entries:
            if name == "PlantEntity":
                try:
                    identity = _plant_identity(data, size[0])
                    identities.add(identity)
                    occupied_anchors.add(identity[:2])
                except (TypeError, ValueError) as exc:
                    raise PlantMergeError(f"Malformed existing plant: {exc}") from exc
    original_tiles, candidate_tiles = _TileReader(original, size), _TileReader(candidate, size)
    footprints = _Footprints(assets_folder)
    additions = {}
    safety_profiles = []
    if ocean_summary is not None:
        if not isinstance(ocean_summary, dict):
            raise PlantMergeError("Ocean plant safety summary must be an object")
        rectangles = ocean_summary.get("rectangles", [])
        if not isinstance(rectangles, list):
            raise PlantMergeError("Ocean plant safety rectangles must be a list")
        for rectangle in rectangles:
            if not isinstance(rectangle, dict):
                raise PlantMergeError("Ocean plant safety rectangle must be an object")
            profile = rectangle.get("coastalBasinProfile")
            if not isinstance(profile, dict) or not profile.get("enabled"):
                continue
            try:
                ranges = swe._wrapped_x_ranges(rectangle["xStart"], rectangle["xEnd"], size[0])
                shore_ranges = ocean_flora_plan.shore_flora_ranges(rectangle, size[0])
                y0, y1 = rectangle["yStart"], rectangle["yEnd"]
                if any(isinstance(rectangle[k], bool) or not isinstance(rectangle[k], int)
                       for k in ("xStart", "xEnd", "yStart", "yEnd")):
                    raise ValueError("ocean bounds must be integers")
                level = profile.get("floorSafetyLiquidLevel", rectangle.get("oceanLiquidLevel"))
                if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level):
                    raise ValueError("missing actual ocean liquid level")
                floor_ids = set(profile["floorEcologyBlockBiomeIndexes"])
                ocean_ids = set(profile["surfaceControlBlockBiomeIndexes"])
                if (not floor_ids or not ocean_ids or floor_ids & ocean_ids
                        or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 255
                               for v in floor_ids | ocean_ids)):
                    raise ValueError("invalid floor/ocean block-biome indexes")
                if not 0 <= y0 <= y1 < size[1]:
                    raise ValueError("invalid ocean Y range")
                safety_profiles.append((ranges, y0, y1, float(level), floor_ids, ocean_ids, shore_ranges))
            except (KeyError, TypeError, ValueError) as exc:
                raise PlantMergeError(f"Cannot establish actual-depth plant safety: {exc}") from exc

    def reject(reason, position, detail=None):
        summary[reason] += 1
        if len(summary["rejectionSamples"]) < 20:
            summary["rejectionSamples"].append({"reason": reason, "position": position, "detail": detail})

    for key, value in candidate_records:
        if key[0] != swe.STORE_ENTITY_SECTOR:
            continue
        sector = swe.world_sector_coordinates(key)
        if sector not in selected:
            continue
        for entry in _entity_entries(value, sector):
            name, version, data, raw = entry
            if name != "PlantEntity":
                summary["nonPlantEntitiesIgnored"] += 1
                continue
            summary["candidatePlants"] += 1
            try:
                identity = _plant_identity(data, size[0])
                x, y = identity[:2]
                if version != 3 or data.get("uniqueId") is not None:
                    raise ValueError("unsupported or uniquely indexed PlantEntity")
                if not 0 <= y < size[1] or (x//32, y//32) not in selected:
                    reject("outsideSelectionSkipped", [x, y])
                    continue
                if identity in identities:
                    summary["duplicatesSkipped"] += 1
                    continue
                # An existing tree can serialize different pieces after native
                # growth/breakage or a mod update. It is still the same occupied
                # anchor, not authorization to append a second overlapping plant.
                if identity[:2] in occupied_anchors:
                    summary["occupiedAnchorsSkipped"] += 1
                    continue
                if (x//32, y//32) != sector:
                    raise ValueError("PlantEntity sector does not own its tilePosition")
                rectangles = footprints.rectangles(data)
            except (TypeError, ValueError, OSError) as exc:
                reject("unsupportedPlantRejected", data.get("tilePosition") if isinstance(data, dict) else None, str(exc))
                continue
            reason, detail = None, None
            checked = set()
            # Check actual occupancy first. The synthetic 3x3 neighbourhood is
            # read-only and may use an already saved neighbouring sector; it
            # must not turn a legal edge anchor into an out-of-selection plant.
            for rectangle_index in [*range(1, len(rectangles)), 0]:
                x0, y0, x1, y1 = rectangles[rectangle_index]
                for dy in range(y0, y1+1):
                    for dx in range(x0, x1+1):
                        point = ((x+dx) % size[0], y+dy)
                        if (not 0 <= point[1] < size[1] or
                                (rectangle_index > 0 and (point[0]//32, point[1]//32) not in selected)):
                            reason, detail = "outsideSelectionSkipped", list(point)
                            break
                        if point in checked:
                            continue
                        checked.add(point)
                        before, after = original_tiles.tile(*point), candidate_tiles.tile(*point)
                        if before is None or after is None:
                            reason, detail = "missingBaselineTilesRejected", list(point)
                            break
                        if _terrain_signature(before) != _terrain_signature(after):
                            reason, detail = "terrainChangeRejected", list(point)
                            break
                    if reason:
                        break
                if reason:
                    break
            if reason:
                reject(reason, [x, y], detail)
                continue
            anchor = original_tiles.tile(x, y)
            root = original_tiles.tile(x, y+(1 if data.get("ceiling", False) else -1))
            empty = {65535, 65534, 65526}
            if (struct.unpack_from(">H", anchor)[0] not in empty
                    or struct.unpack_from(">H", root)[0] in empty
                    or anchor[25:27] != b"\xff\xff" or root[25:27] != b"\xff\xff"):
                reject("terrainChangeRejected", [x, y], "invalid original anchor/root support")
                continue
            depth_failure = None
            matched_ocean_scope = False
            matched_shore_scope = False
            # rectangles[0] is an extra 3x3 terrain-read safety neighbourhood,
            # not plant occupancy. Its read may cross the shore-band edge, but
            # every actual image/root column (and the anchor) must remain inside.
            plant_columns = {x}
            for x0, _, x1, _ in rectangles[1:]:
                plant_columns.update((x+dx) % size[0] for dx in range(x0, x1+1))
            for ranges, y0, y1, level, floor_ids, ocean_ids, shore_ranges in safety_profiles:
                if not y0 <= y <= y1 or not any(a <= x <= b for a, b in ranges):
                    continue
                depth = level - y  # Native plant tilePosition, not noisy layout Y.
                block_biome = original_tiles.tile(x, y-1)[27]
                if block_biome not in floor_ids and block_biome not in ocean_ids:
                    continue
                matched_ocean_scope = True
                # The engine can populate neighbouring sectors and the final
                # sector can straddle the exact 600-column boundary. Only this
                # actual shore band's plants are authorized, including roots
                # and the conservative image footprint already checked above.
                if any(not any(a <= px <= b for a, b in shore_ranges) for px in plant_columns):
                    continue
                matched_shore_scope = True
                image_paths = [piece["image"].casefold() for piece in data["pieces"]]
                is_tree = (data.get("fallsWhenDead") is True
                           or data.get("stemDropConfig") is not None
                           or data.get("foliageDropConfig") is not None
                           or any(piece.get("kind") in (1, 2) for piece in data["pieces"])
                           or any("/plants/trees/" in path for path in image_paths))
                tall = is_tree or any(marker in path for path in image_paths
                                      for marker in ("seatree", "kelp", "toxickelp", "seaweed"))
                if block_biome in floor_ids:
                    if depth <= 4:
                        depth_failure = f"floor plant actual depth {depth:g} <= 4"
                    elif tall and depth <= 30:
                        depth_failure = f"tall floor plant actual depth {depth:g} <= 30"
                elif block_biome in ocean_ids and is_tree and depth > 30:
                    depth_failure = f"surface-ocean tree actual depth {depth:g} > 30"
                if depth_failure:
                    break
            if safety_profiles and not matched_ocean_scope:
                reject("outsideOceanScopeRejected", [x, y],
                       "anchor/root biome does not belong to any selected curved ocean profile")
                continue
            if safety_profiles and not matched_shore_scope:
                reject("outsideShoreRangeRejected", [x, y],
                       "anchor or footprint outside actual solid-shore inward 600-column band")
                continue
            if depth_failure:
                reject("actualDepthRejected", [x, y], depth_failure)
                continue
            additions.setdefault(key, []).append(entry)
            identities.add(identity)
            occupied_anchors.add(identity[:2])
            summary["addedPlants"] += 1
    updated = []
    for key, value in original_records:
        if key in additions:
            updated.append((key, _encode_entries(existing_entries.get(key, []) + additions[key])))
        else:
            updated.append((key, value))
    for key in sorted(set(additions)-set(original)):
        updated.append((key, _encode_entries(additions[key])))
    summary["modifiedEntitySectors"] = len(additions)
    result = dict(updated)
    assert {k:v for k,v in result.items() if k[0] != swe.STORE_ENTITY_SECTOR} == {
        k:v for k,v in original.items() if k[0] != swe.STORE_ENTITY_SECTOR}
    assert all(result[key] == value for key, value in original_records if key not in additions)
    for key in additions:
        written = _entity_entries(result[key], swe.world_sector_coordinates(key))
        old = existing_entries.get(key, [])
        assert [entry[3] for entry in written[:len(old)]] == [entry[3] for entry in old]
        assert all(not getattr(before, "subversioning_raw", b"") or
                   before.subversioning_raw == after.subversioning_raw
                   for before, after in zip(old, written))
    return updated, summary
