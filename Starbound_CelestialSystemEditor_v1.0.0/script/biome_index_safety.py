"""Native byte-width biome index checks and lossless duplicate compaction.

The engine stores both world-region and saved-tile biome references as uint8.
The compiled table is one-based; zero is the native empty-biome sentinel.
This module performs no filesystem I/O and never discards a distinct biome.
"""

from __future__ import annotations

import copy
import json
import math
import zlib
from collections.abc import Callable, Iterable
from typing import Any


MAX_BIOME_INDEX = 255
_REFERENCE_FIELDS = ("blockBiomeIndex", "environmentBiomeIndex")


class BiomeIndexCapacityError(ValueError):
    """The compiled table cannot be represented by native biome index bytes."""


def _region_data(document: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        raise ValueError("World document must be an object")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or "worldTemplate" not in metadata:
        return None
    template = metadata["worldTemplate"]
    if not isinstance(template, dict):
        raise ValueError("worldTemplate must be an object")
    region = template.get("regionData")
    if region is None:
        return None
    if not isinstance(region, dict):
        raise ValueError("worldTemplate.regionData must be an object")
    return region


def _biomes_and_layers(region: dict[str, Any]) -> tuple[list, list]:
    biomes = region.get("biomes")
    layers = region.get("layers")
    if not isinstance(biomes, list) or any(not isinstance(b, dict) for b in biomes):
        raise ValueError("worldTemplate.regionData.biomes must be an array of objects")
    if not isinstance(layers, list):
        raise ValueError("worldTemplate.regionData.layers must be an array")
    return biomes, layers


def _validated_cells(layers: list, maximum: int):
    for layer_number, layer in enumerate(layers, 1):
        if not isinstance(layer, dict) or not isinstance(layer.get("cells"), list):
            raise ValueError(f"Biome layer {layer_number} must contain a cells array")
        for cell_number, cell in enumerate(layer["cells"], 1):
            if not isinstance(cell, dict):
                raise ValueError(f"Biome layer {layer_number} cell {cell_number} must be an object")
            for field in _REFERENCE_FIELDS:
                index = cell.get(field)
                if type(index) is not int or not 0 <= index <= maximum:
                    raise ValueError(
                        f"Biome layer {layer_number} cell {cell_number} has invalid "
                        f"{field} {index!r}; expected an integer in 0..{maximum} "
                        "(0 means no biome)"
                    )
            yield cell


def validate_native_biome_indexes(document: dict[str, Any]) -> None:
    """Reject native-unrepresentable tables/references; metadata fixtures skip.

    A missing world template or absent/null regionData is valid for documents
    without a generated layout. An existing malformed layout does not skip.
    """

    region = _region_data(document)
    if region is None:
        return
    biomes, layers = _biomes_and_layers(region)
    if len(biomes) > MAX_BIOME_INDEX:
        raise BiomeIndexCapacityError(
            f"Compiled biome table has {len(biomes)} entries; native biome indexes "
            f"allow at most {MAX_BIOME_INDEX}. Exact-duplicate compaction is required "
            "before saving; indexes must not be truncated or wrapped."
        )
    for _cell in _validated_cells(layers, min(MAX_BIOME_INDEX, len(biomes))):
        pass


class _NonFiniteBiome(Exception):
    pass


def _check_json_values(value: Any) -> None:
    """Keep identity strict, including boolean/number and int/float distinctions."""
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _NonFiniteBiome()
        return
    if isinstance(value, list):
        for entry in value:
            _check_json_values(entry)
        return
    if isinstance(value, dict):
        for key, entry in value.items():
            if type(key) is not str:
                raise ValueError("Compiled biome JSON object keys must be strings")
            _check_json_values(entry)
        return
    raise ValueError(f"Compiled biome contains unsupported JSON type: {type(value).__name__}")


def _biome_identity(biome: dict[str, Any]) -> str | None:
    try:
        _check_json_values(biome)
    except _NonFiniteBiome:
        # Dormant native NaN/Inf fields are preserved, but never deduplicated.
        return None
    return json.dumps(biome, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def compact_biome_table(
    document: dict[str, Any],
    records: Iterable[tuple[bytes, bytes]],
    *,
    tile_offsets: Callable[[bytes | bytearray, bytes], tuple[int, Iterable[int]]],
    tile_store_type: int = 1,
) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    """Merge exact duplicates and remap *all* saved tile/region references.

    ``records`` values are zlib-compressed World4 records. ``tile_offsets`` is
    the format-validating callback returning ``(version, tile_start_offsets)``.
    Non-tile records and unchanged compressed tile records remain byte-identical.

    All validation, remapping and compression finish before document mutation.
    On failure neither the input document nor its records are modified. On
    success only regionData.biomes/layers are replaced; the caller must update
    its metadata record from the resulting document before writing a world.
    """

    if type(tile_store_type) is not int or not 0 <= tile_store_type <= 255:
        raise ValueError("tile_store_type must be a byte value")
    if not callable(tile_offsets):
        raise ValueError("tile_offsets must be a format-validating callback")
    region = _region_data(document)
    if region is None:
        return list(records), {
            "before": 0, "after": 0, "merged": 0,
            "changedTileRecords": 0, "changedTileReferences": 0,
            "indexMapping": {0: 0},
        }
    biomes, layers = _biomes_and_layers(region)
    unique_biomes: list[dict[str, Any]] = []
    identities: dict[str, int] = {}
    mapping = {0: 0}
    for old_index, biome in enumerate(biomes, 1):
        identity = _biome_identity(biome)
        new_index = identities.get(identity) if identity is not None else None
        if new_index is None:
            unique_biomes.append(biome)
            new_index = len(unique_biomes)
            if identity is not None:
                identities[identity] = new_index
        mapping[old_index] = new_index
    if len(unique_biomes) > MAX_BIOME_INDEX:
        raise BiomeIndexCapacityError(
            f"Compiled biome table has {len(biomes)} entries and still contains "
            f"{len(unique_biomes)} distinct or non-finite entries after exact-duplicate "
            f"compaction; native indexes allow at most {MAX_BIOME_INDEX}. "
            "No world data was changed."
        )

    staged_layers = copy.deepcopy(layers)
    for cell in _validated_cells(staged_layers, len(biomes)):
        for field in _REFERENCE_FIELDS:
            cell[field] = mapping[cell[field]]

    updated_records: list[tuple[bytes, bytes]] = []
    changed_records = 0
    changed_references = 0
    for record in records:
        if not isinstance(record, (tuple, list)) or len(record) != 2:
            raise ValueError("World record must be a (key, compressed value) pair")
        key, compressed = record
        if not isinstance(key, bytes) or len(key) != 5:
            raise ValueError("World4 record key must contain exactly five bytes")
        if not isinstance(compressed, bytes):
            raise ValueError(f"World4 record {key.hex()} value must be bytes")
        if key[0] != tile_store_type:
            updated_records.append((key, compressed))
            continue
        try:
            raw = bytearray(zlib.decompress(compressed))
        except zlib.error as exc:
            raise ValueError(f"Tile sector {key.hex()} cannot be decompressed: {exc}") from exc
        _version, offsets = tile_offsets(raw, key)
        previous_offset = -31
        record_changes = 0
        for offset in offsets:
            if type(offset) is not int or offset < 0 or offset + 31 > len(raw):
                raise ValueError(f"Tile sector {key.hex()} has an invalid tile offset {offset!r}")
            if offset < previous_offset + 31:
                raise ValueError(f"Tile sector {key.hex()} has overlapping or unordered tile offsets")
            previous_offset = offset
            for byte_position in (offset + 27, offset + 28):
                old_index = raw[byte_position]
                if old_index not in mapping:
                    raise ValueError(
                        f"Tile sector {key.hex()} references missing biome {old_index} "
                        f"at byte {byte_position}; table has {len(biomes)} entries"
                    )
                new_index = mapping[old_index]
                if new_index != old_index:
                    raw[byte_position] = new_index
                    record_changes += 1
        if record_changes:
            compressed = zlib.compress(raw, level=9)
            changed_records += 1
            changed_references += record_changes
        updated_records.append((key, compressed))

    staged_biomes = copy.deepcopy(unique_biomes)
    report = {
        "before": len(biomes), "after": len(staged_biomes),
        "merged": len(biomes) - len(staged_biomes),
        "changedTileRecords": changed_records,
        "changedTileReferences": changed_references,
        "indexMapping": mapping,
    }
    # Commit only after every record and both references in every cell validate.
    region["biomes"] = staged_biomes
    region["layers"] = staged_layers
    return updated_records, report
