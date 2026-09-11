"""Replace saved material IDs in one rectangle, without regenerating tiles.

The UI accepts materialName values from /tiles/materials/**/*.material, not
numeric IDs. Inventory .matitem definitions are deliberately not consulted.
Only the two material-ID bytes in each selected tile layer may change. This
preserves serialized entities and all other tile fields, but cannot override
the new material's in-game physics (falling blocks, support/collision, etc.).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Any, Mapping, Sequence
import zlib

import starbound_world_editor as swe
import world_edit_guard


# Native MaterialId is uint16, but 65000..65535 are engine/meta materials and
# must not be supplied as ordinary placeable block items.
FIRST_META_MATERIAL_ID = 65000
LAYERS = {"both": (0, 7), "foreground": (0,), "background": (7,)}


def _semantic_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _material_id(value: Any, path: str) -> int:
    if type(value) is not int or not 0 <= value < FIRST_META_MATERIAL_ID:
        raise ValueError(
            f"{path}: materialId must be an integer from 0 to 64999; "
            "engine/meta material IDs are not placeable block items"
        )
    return value


def _name(value: Any, path: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{path}: {field} must be a nonempty name without surrounding spaces")
    return value


def _bool_field(document: dict[str, Any], name: str, default: bool, path: str) -> bool:
    value = document.get(name, default)
    if type(value) is not bool:
        raise ValueError(f"{path}: {name} must be true or false")
    return value


def load_block_item_catalog(assets: Path) -> dict[str, dict[str, Any]]:
    """Map materialName under /tiles/materials to native IDs and physics.

    The historical function name is retained for GUI compatibility. Materials
    do not need an inventory item. Duplicate material IDs/names are rejected;
    case-insensitive name lookup is accepted only when unambiguous. Loose
    traversal is restricted to tiles/materials and its subdirectories.
    """
    assets = Path(assets)
    roots = (assets, assets / "packed", assets / "assets" / "packed", assets / "assets")
    loose_root = next((root for root in roots if (root / "tiles" / "materials").is_dir()), None)
    documents = []
    if loose_root is not None:
        for directory, _subdirectories, files in os.walk(loose_root / "tiles" / "materials"):
            for filename in files:
                if not filename.lower().endswith(".material"):
                    continue
                local_path = Path(directory) / filename
                path = "/" + local_path.relative_to(loose_root).as_posix()
                documents.append((path, swe.parse_starbound_json(local_path.read_bytes(), path)))
    else:
        # Prefer an explicit package path so invalid loose directories do not
        # trigger an unrelated full-tree biome search in AssetReader.
        packages = (assets, assets / "packed.pak", assets / "assets" / "packed.pak")
        package = next((path for path in packages if path.is_file() and path.name.lower() == "packed.pak"), None)
        if package is None:
            raise ValueError("No tiles/materials folder or packed.pak was found in the selected Assets")
        reader = swe.StarboundAssetReader(package)
        for path in reader.paths(".material"):
            if path.replace("\\", "/").lower().startswith("/tiles/materials/"):
                documents.append((path, reader.json(path)))
    materials: dict[int, dict[str, Any]] = {}
    material_names: dict[str, str] = {}
    catalog: dict[str, dict[str, Any]] = {}
    for path, document in sorted(documents):
        if not isinstance(document, dict):
            raise ValueError(f"{path}: expected a material JSON object")
        material_id = _material_id(document.get("materialId"), path)
        material_name = _name(document.get("materialName"), path, "materialName")
        if material_id in materials:
            raise ValueError(
                f"Ambiguous materialId {material_id}: "
                f"{materials[material_id]['materialPath']} and {path}"
            )
        if material_name in material_names:
            raise ValueError(
                f"Duplicate materialName {material_name!r}: "
                f"{material_names[material_name]} and {path}"
            )
        collision = document.get("collisionKind", "block")
        if not isinstance(collision, str) or not collision:
            raise ValueError(f"{path}: collisionKind must be a nonempty string")
        falling = _bool_field(document, "falling", False, path)
        cascading = _bool_field(document, "cascading", False, path)
        materials[material_id] = {
            "materialId": material_id,
            "materialName": material_name,
            "materialPath": path,
            "collisionKind": collision,
            "falling": falling,
            "cascading": cascading,
            "foregroundOnly": _bool_field(document, "foregroundOnly", False, path),
            "supportsMods": _bool_field(
                document, "supportsMods", not (falling or cascading or collision != "block"), path
            ),
            "liquidInteractions": bool(document.get("liquidInteractions")),
            "materialDefinitionSignature": _semantic_digest(document),
            # Compatibility alias: all user-facing names now mean materialName.
            "itemName": material_name,
            "shortDescription": document.get("shortdescription", material_name),
        }
        material_names[material_name] = path
        catalog[material_name] = materials[material_id]
    if not catalog:
        raise ValueError("No .material definitions were found under Assets /tiles/materials")
    return dict(sorted(catalog.items(), key=lambda entry: (entry[0].casefold(), entry[0])))


def resolve_block_item(catalog: Mapping[str, dict[str, Any]], value: str) -> dict[str, Any]:
    """Trim input, prefer exact materialName, then accept one unique casefold match."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter a materialName, for example dirt; not an itemName or numeric ID")
    item_name = value.strip()
    if item_name in catalog:
        return dict(catalog[item_name])
    matches = [item for name, item in catalog.items() if name.casefold() == item_name.casefold()]
    if len(matches) == 1:
        return dict(matches[0])
    if matches:
        raise ValueError(
            f"Ambiguous materialName {item_name!r}; use exact spelling: "
            + ", ".join(item["itemName"] for item in matches)
        )
    raise ValueError(
        f"Unknown materialName {item_name!r}. Use materialName from a .material "
        "file under Assets /tiles/materials, not an inventory itemName, numeric "
        "material ID, or display name."
    )


def _rectangle(rectangle: Mapping[str, Any], width: int, height: int) -> dict[str, Any]:
    if not isinstance(rectangle, Mapping):
        raise ValueError("Rectangle must contain xStart, xEnd, yStart and yEnd")
    coordinates: dict[str, int] = {}
    for field in ("xStart", "xEnd", "yStart", "yEnd"):
        value = rectangle.get(field)
        if type(value) is not int:
            raise ValueError(f"Rectangle {field} must be a whole tile coordinate")
        coordinates[field] = value
    if width <= 0 or height <= 0:
        raise swe.WorldFormatError("World width and height must be positive")
    ranges = swe._wrapped_x_ranges(coordinates["xStart"], coordinates["xEnd"], width)
    if not 0 <= coordinates["yStart"] <= coordinates["yEnd"] < height:
        raise ValueError(f"Rectangle Y coordinates must increase and remain within 0..{height - 1}")
    return {
        **coordinates,
        "xRanges": ranges,
        "wrapped": coordinates["xStart"] > coordinates["xEnd"],
    }


def _warnings(source: dict[str, Any], target: dict[str, Any], layer: str) -> list[str]:
    if layer not in LAYERS:
        raise ValueError("Layer must be 'both', 'foreground', or 'background'")
    if source["materialId"] == target["materialId"]:
        return []
    if target["foregroundOnly"] and layer in ("both", "background"):
        raise ValueError(
            f"Target block {target['itemName']!r} only supports foreground placement; "
            "select foreground only"
        )
    if layer != "background" and source["collisionKind"] != target["collisionKind"]:
        raise ValueError(
            f"Cannot replace foreground collision kind {source['collisionKind']!r} "
            f"with {target['collisionKind']!r}. Tile-only replacement preserves the "
            "saved collision byte and cannot safely convert these collision kinds. "
            "Choose a target with the same collision kind, or background only."
        )
    warnings = []
    if layer != "background" and (target["falling"] or target["cascading"]):
        warnings.append(
            "The target is a falling/cascading material. Although saved entities are "
            "not edited, gravity can move these tiles and detach supported objects or "
            "plants when the game runs."
        )
    if layer != "background" and target["collisionKind"] != "block":
        warnings.append(
            f"The target uses {target['collisionKind']} collision, not a solid block. "
            "The saved collision byte and object/plant records are preserved, but "
            "support requirements and other target-material behavior still apply."
        )
    if not target["supportsMods"]:
        warnings.append(
            "The target material does not support tile mods. Existing grass/ore/mod "
            "bytes are preserved, but the game may hide or remove unsupported mods."
        )
    if target["liquidInteractions"]:
        warnings.append(
            "The target material has liquid interactions and may transform after "
            "contact with liquids in the game."
        )
    return warnings


def replace_material_rectangle_records(
    records: Sequence[tuple[bytes, bytes]],
    width: int,
    height: int,
    source_id: int,
    target_id: int,
    rectangle: Mapping[str, Any],
    layer: str = "both",
) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    """Pure record transformation; exact inclusive bounds, horizontal wrap.

    No implicit rounding or regeneration occurs here. Absent sectors remain
    absent. Untouched compressed records, including all entity records, are
    returned byte for byte; modified sectors differ only at matching IDs.
    """
    source_id = _material_id(source_id, "Source")
    target_id = _material_id(target_id, "Target")
    if layer not in LAYERS:
        raise ValueError("Layer must be 'both', 'foreground', or 'background'")
    area = _rectangle(rectangle, width, height)
    x_ranges = area["xRanges"]
    y_start, y_end = area["yStart"], area["yEnd"]
    requested_tiles = sum(end - start + 1 for start, end in x_ranges) * (y_end - y_start + 1)
    selected_sector_x = {
        x for start, end in x_ranges for x in range(start // 32, end // 32 + 1)
    }
    expected_sectors = len(selected_sector_x) * (y_end // 32 - y_start // 32 + 1)
    stats: dict[str, Any] = {
        "rectangle": area,
        "requestedTiles": requested_tiles,
        "savedTilesScanned": 0,
        "savedSectorsScanned": 0,
        "missingSectors": 0,
        "changedSectors": 0,
        "foregroundMatches": 0,
        "backgroundMatches": 0,
        "foregroundTiles": 0,
        "backgroundTiles": 0,
        "changedTileLayers": 0,
        "generatedSectors": 0,
        "preservedNonMaterialBytes": True,
    }
    output = []
    seen = set()
    for key, compressed in records:
        if key in seen:
            raise swe.WorldFormatError("World contains duplicate record keys")
        seen.add(key)
        if len(key) != 5:
            raise swe.WorldFormatError("World4 record key must be five bytes")
        if key[0] != swe.STORE_TILE_SECTOR:
            output.append((key, compressed))
            continue
        sector_x, sector_y = swe.world_sector_coordinates(key)
        if sector_x not in selected_sector_x or not y_start // 32 <= sector_y <= y_end // 32:
            output.append((key, compressed))
            continue
        try:
            raw = bytearray(zlib.decompress(compressed))
        except zlib.error as exc:
            raise swe.WorldFormatError(f"Tile sector {key.hex()} cannot be decompressed: {exc}") from exc
        _version, offsets = swe.tile_sector_offsets(raw, key)
        stats["savedSectorsScanned"] += 1
        changed = False
        for local_y in range(32):
            world_y = sector_y * 32 + local_y
            if not y_start <= world_y <= y_end:
                continue
            for local_x in range(32):
                world_x = sector_x * 32 + local_x
                if not any(start <= world_x <= end for start, end in x_ranges):
                    continue
                stats["savedTilesScanned"] += 1
                tile_offset = offsets[local_y * 32 + local_x]
                for relative in LAYERS[layer]:
                    offset = tile_offset + relative
                    if struct.unpack_from(">H", raw, offset)[0] != source_id:
                        continue
                    prefix = "foreground" if relative == 0 else "background"
                    stats[prefix + "Matches"] += 1
                    if source_id != target_id:
                        struct.pack_into(">H", raw, offset, target_id)
                        stats[prefix + "Tiles"] += 1
                        changed = True
        if changed:
            stats["changedSectors"] += 1
            output.append((key, zlib.compress(raw, level=9)))
        else:
            output.append((key, compressed))
    stats["missingSectors"] = expected_sectors - stats["savedSectorsScanned"]
    stats["changedTileLayers"] = stats["foregroundTiles"] + stats["backgroundTiles"]
    if source_id == target_id:
        stats["unchangedReason"] = "Source and target items resolve to the same material"
    elif not stats["changedTileLayers"]:
        stats["unchangedReason"] = "No matching saved tiles were found in this rectangle and layer"
    else:
        stats["unchangedReason"] = None
    return output, stats


def world_file_fingerprint(path: Path) -> dict[str, Any]:
    """Hash a stable source; mtime alone cannot protect against a running game."""
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino"):
        if getattr(before, field, None) != getattr(after, field, None):
            raise RuntimeError("The world changed while being read. Close the game and try again.")
    return {"size": after.st_size, "mtimeNs": after.st_mtime_ns, "sha256": digest.hexdigest()}


def _assert_fingerprint(world: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    actual = world_file_fingerprint(world)
    if any(actual.get(field) != expected.get(field) for field in ("size", "mtimeNs", "sha256")):
        raise RuntimeError(
            "The world changed after the replacement was inspected. Nothing was written. "
            "Close the game, reload the world, and inspect/apply again."
        )
    return actual


def _prepare(
    world: Path, assets: Path, source_item: str, target_item: str,
    rectangle: Mapping[str, Any], layer: str,
    expected_fingerprint: Mapping[str, Any] | None,
    expected_asset_signature: str | None = None,
) -> tuple[Any, list[tuple[bytes, bytes]], dict[str, Any]]:
    catalog = load_block_item_catalog(assets)
    source = resolve_block_item(catalog, source_item)
    target = resolve_block_item(catalog, target_item)
    asset_signature = _semantic_digest({"source": source, "target": target})
    if expected_asset_signature is not None and asset_signature != expected_asset_signature:
        raise RuntimeError(
            "The selected block item/material assets changed after the replacement was "
            "inspected. Nothing was written. Inspect/apply again to confirm the current "
            "material mapping and physics."
        )
    warnings = _warnings(source, target, layer)
    fingerprint = world_file_fingerprint(world)
    if expected_fingerprint is not None:
        _assert_fingerprint(world, expected_fingerprint)
    database, records = swe.load_records(world)
    try:
        metadata_records = [value for key, value in records if key == swe.WORLD_METADATA_KEY]
        if len(metadata_records) != 1:
            raise swe.WorldFormatError("Expected exactly one WorldMetadata record")
        document = swe.decode_world_metadata(metadata_records[0])
        width, height = document["size"]
        updated, summary = replace_material_rectangle_records(
            records, width, height, source["materialId"], target["materialId"], rectangle, layer
        )
        _assert_fingerprint(world, fingerprint)
        if summary["missingSectors"]:
            warnings.append(
                f"{summary['missingSectors']} selected sector(s) are not saved/generated. "
                "Only existing saved tiles are replaced; missing sectors are left untouched."
            )
        summary.update({
            "world": str(world), "worldSize": [width, height], "layer": layer,
            "sourceItem": source["itemName"], "targetItem": target["itemName"],
            "sourceMaterialId": source["materialId"], "targetMaterialId": target["materialId"],
            "sourceFingerprint": fingerprint, "warnings": warnings,
            "assetSignature": asset_signature,
        })
        return database, updated, summary
    except Exception:
        swe.close_database(database)
        raise


def inspect_world_material_rectangle(
    world: Path, assets: Path, source_item: str, target_item: str,
    rectangle: Mapping[str, Any], layer: str = "both",
) -> dict[str, Any]:
    """Read-only preview including exact saved match counts and safety warnings."""
    database, _records, summary = _prepare(
        Path(world), Path(assets), source_item, target_item, rectangle, layer, None
    )
    swe.close_database(database)
    return summary


def replace_world_material_rectangle_in_place(
    world: Path, assets: Path, source_item: str, target_item: str,
    rectangle: Mapping[str, Any], layer: str = "both", *,
    keep_backup: bool = True,
    expected_fingerprint: Mapping[str, Any] | None = None,
    expected_asset_signature: str | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Back up, verify, and atomically commit one tile-only replacement.

    No-match/same-material operations do not write, rebuild, or create backups.
    Pass a preview's sourceFingerprint and assetSignature to refuse stale
    confirmation if either the world or the selected block definitions change.
    A failed build leaves the source unchanged and retains a recovery backup.
    """
    world = Path(world)
    database, records, summary = _prepare(
        world, Path(assets), source_item, target_item, rectangle, layer,
        expected_fingerprint, expected_asset_signature
    )
    backup = None
    staged = None
    try:
        if not summary["changedTileLayers"]:
            return None, summary
        fingerprint = summary["sourceFingerprint"]
        _assert_fingerprint(world, fingerprint)
        backup = swe._unique_world_edit_backup_path(world)
        shutil.copy2(world, backup)
        if world_file_fingerprint(backup)["sha256"] != fingerprint["sha256"]:
            raise RuntimeError("The world changed during backup. Nothing was written to the source.")
        with tempfile.NamedTemporaryFile(
            prefix=world.name + ".", suffix=".tile-replacement", dir=world.parent, delete=False
        ) as stream:
            staged = Path(stream.name)
        swe.atomic_write_world(backup, staged, records, database)
        # Release the original read handle before Windows' atomic replacement.
        swe.close_database(database)
        database = None
        _assert_fingerprint(world, fingerprint)
        world_edit_guard.assert_world_available(world)
        os.replace(staged, world)
        staged = None
        if not keep_backup:
            try:
                backup.unlink()
                backup = None
            except OSError:
                summary["warnings"].append("The edit succeeded, but its optional backup could not be removed.")
        summary["outputFingerprint"] = world_file_fingerprint(world)
        return backup, summary
    finally:
        if database is not None:
            swe.close_database(database)
        if staged is not None and staged.exists():
            staged.unlink()
