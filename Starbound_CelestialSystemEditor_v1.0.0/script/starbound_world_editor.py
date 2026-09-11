#!/usr/bin/env python3
"""Read and safely rewrite Starbound World4 (.world) files.

This module intentionally uses only Python's standard library.  It understands
the BTreeDB5 container, zlib-compressed World4 records and Starbound's binary
object notation (SBON).  The command line interface is defined near the end of
the file.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import posixpath
import random
import re
import shutil
import struct
import sys
import tempfile
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Sequence, TypeVar
import uuid
import zlib

if __package__:
    from .biome_index_safety import (
        MAX_BIOME_INDEX, BiomeIndexCapacityError, compact_biome_table,
        validate_native_biome_indexes,
    )
else:
    from biome_index_safety import (
        MAX_BIOME_INDEX, BiomeIndexCapacityError, compact_biome_table,
        validate_native_biome_indexes,
    )


HEADER_SIZE = 512
MAGIC = b"BTreeDB5"
WORLD_IDENTIFIER = "World4"
CELESTIAL_IDENTIFIER = "Celestial2"
VERSIONED_JSON_FILE_MAGIC = b"SBVJ01"
CELESTIAL_KEY_SIZE = 32
CELESTIAL_CHUNK_SIZE = 50
MAX_U32 = 0xFFFFFFFF
WORLD_METADATA_KEY = b"\x00\x00\x00\x00\x00"
WORLD_SECTOR_SIZE = 32
OCEAN_FLOOR_RELIEF_START_DEPTH = 100.0
OCEAN_FLOOR_MICRODUNGEON_CLEARANCE = 150.0
OCEAN_FLOOR_TALL_PLANT_CLEARANCE = 30.0
OCEAN_FLOOR_ALL_PLANT_CLEARANCE = 4.0
OCEAN_SURFACE_TREE_MAXIMUM_DEPTH = 30.0
OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT = 4
OCEAN_FLOOR_VEGETATION_DENSITY_MULTIPLIER = 1.4
OCEAN_SHORE_ISLAND_EXCLUSION_WIDTH = 750
STORE_TILE_SECTOR = 1
STORE_ENTITY_SECTOR = 2
STORE_UNIQUE_INDEX = 3
STORE_SECTOR_UNIQUES = 4
PAK_MAGIC = b"SBAsset6"
PAK_INDEX_MAGIC = b"INDEX"

_T = TypeVar("_T")


def automatic_project_path(world_path: Path) -> Path:
    """Return the fixed editable-JSON path used by the export window."""
    return world_path.with_name(f"tmp_{world_path.name}.json")


def suggested_world_output_path(project_path: Path) -> Path:
    """Return a safe default output name for an imported project JSON."""
    name = project_path.name
    lower_name = name.lower()
    if lower_name.startswith("tmp_") and lower_name.endswith(".world.json"):
        base_name = name[4 : -len(".world.json")]
    else:
        base_name = name
        for suffix in (".world.editable.json", ".editable.json", ".json"):
            if base_name.lower().endswith(suffix):
                base_name = base_name[: -len(suffix)]
                break
    return project_path.with_name(base_name + "_edited.world")


class WorldFormatError(ValueError):
    """Raised when a file is not a supported or structurally valid world."""


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise WorldFormatError(
            f"Unexpected end of file: wanted {size} bytes, got {len(value)}"
        )
    return value


def read_varuint(stream: BinaryIO) -> int:
    value = 0
    while True:
        byte = _read_exact(stream, 1)[0]
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value


def write_varuint(stream: BinaryIO, value: int) -> None:
    if value < 0:
        raise ValueError("A Starbound unsigned varint cannot be negative")
    encoded = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        encoded.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    stream.write(encoded)


def read_varint(stream: BinaryIO) -> int:
    value = read_varuint(stream)
    return -(value >> 1) - 1 if value & 1 else value >> 1


def write_varint(stream: BinaryIO, value: int) -> None:
    encoded = (-(value + 1) << 1 | 1) if value < 0 else value << 1
    write_varuint(stream, encoded)


def read_bytes(stream: BinaryIO) -> bytes:
    return _read_exact(stream, read_varuint(stream))


def write_bytes(stream: BinaryIO, value: bytes) -> None:
    write_varuint(stream, len(value))
    stream.write(value)


def read_string(stream: BinaryIO) -> str:
    return read_bytes(stream).decode("utf-8")


def write_string(stream: BinaryIO, value: str) -> None:
    write_bytes(stream, value.encode("utf-8"))


def read_dynamic(stream: BinaryIO) -> Any:
    type_id = _read_exact(stream, 1)[0]
    if type_id == 1:
        return None
    if type_id == 2:
        return struct.unpack(">d", _read_exact(stream, 8))[0]
    if type_id == 3:
        return _read_exact(stream, 1) != b"\x00"
    if type_id == 4:
        return read_varint(stream)
    if type_id == 5:
        return read_string(stream)
    if type_id == 6:
        return [read_dynamic(stream) for _ in range(read_varuint(stream))]
    if type_id == 7:
        return {
            read_string(stream): read_dynamic(stream)
            for _ in range(read_varuint(stream))
        }
    raise WorldFormatError(f"Unknown SBON dynamic type 0x{type_id:02x}")


def strip_starbound_json_comments(text: str) -> str:
    """Remove // and /* */ comments while preserving quoted strings."""

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            if index + 1 >= len(text):
                raise ValueError("Unterminated block comment in Starbound JSON")
            index += 2
            continue
        output.append(char)
        index += 1
    if in_string:
        raise ValueError("Unterminated string in Starbound JSON")
    return "".join(output)


def parse_starbound_json(data: bytes, source_name: str) -> Any:
    try:
        text = data.decode("utf-8-sig")
        return json.loads(strip_starbound_json_comments(text))
    except Exception as exc:
        raise ValueError(f"Cannot parse Starbound asset {source_name}: {exc}") from exc


def validate_finite_json_numbers(value: Any, label: str = "JSON value") -> None:
    """Reject NaN/Infinity anywhere in a JSON-like value.

    Python accepts the non-standard JSON tokens ``NaN`` and ``Infinity``.
    Starbound can physically receive those floats, but world generation and
    celestial rendering do not safely support them as user parameters.
    """

    def walk(item: Any, path: str) -> None:
        if isinstance(item, bool) or item is None or isinstance(item, (str, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{label} contains a non-finite number at {path}")
            return
        if isinstance(item, dict):
            for key, child in item.items():
                walk(child, f"{path}.{key}")
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")

    walk(value, "$")


def _normalize_asset_path(path: str) -> str:
    normalized = posixpath.normpath("/" + path.replace("\\", "/").lstrip("/"))
    return normalized.lower()


def _relative_asset_path(base_path: str, target: str) -> str:
    target_path = target.split(":", 1)[0]
    if target_path.startswith("/"):
        return posixpath.normpath(target_path)
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), target_path))


def _asset_json_query(value: Any, query: str) -> Any:
    if not query:
        return value
    current = value
    for match in re.finditer(r"([^.\[\]]+)|\[(-?\d+)\]", query):
        token = match.group(1) if match.group(1) is not None else match.group(2)
        assert token is not None
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise KeyError(query)
    return current


class StarboundAssetReader:
    """Read arbitrary assets from an unpacked tree or an SBAsset6 packed.pak."""

    @staticmethod
    def _unpacked_root(source: Path) -> Path | None:
        """Find the virtual ``/`` of an unpacked Starbound asset tree.

        Users commonly select either the actual unpacked directory (``packed``)
        or its parent (``assets``).  Looking for ``*.biome`` below the selected
        directory is not enough: treating that parent as virtual ``/`` makes an
        absolute asset such as ``/parallax/surface/oceanfloor.parallax`` resolve
        one directory too high.
        """

        conventional = (source, source / "packed", source / "assets" / "packed")
        for candidate in conventional:
            biome_directory = candidate / "biomes"
            if biome_directory.is_dir() and next(
                biome_directory.rglob("*.biome"), None
            ) is not None:
                return candidate

        biome = next(source.rglob("*.biome"), None)
        if biome is None:
            return None
        for parent in biome.parents:
            if parent.name.lower() == "biomes":
                return parent.parent
            if parent == source:
                break
        return source

    def __init__(self, source: Path):
        source = Path(source)
        self.root: Path | None = None
        self.pak: Path | None = None
        self.entries: dict[str, tuple[str, int, int]] = {}
        self._unpacked_paths: list[tuple[str, str]] | None = None
        if source.is_file():
            if source.name.lower() != "packed.pak":
                raise ValueError("The assets input must be a folder or packed.pak")
            self.pak = source
        elif source.is_dir():
            self.root = self._unpacked_root(source)
            if self.root is None:
                candidates = (source / "packed.pak", source / "assets" / "packed.pak")
                self.pak = next((path for path in candidates if path.is_file()), None)
                if self.pak is None:
                    raise ValueError(
                        "No unpacked .biome files or packed.pak were found in the selected assets folder"
                    )
        else:
            raise ValueError(f"Assets folder does not exist: {source}")

        self.mode = "unpacked" if self.root is not None else "packed.pak"
        self.source = self.root or self.pak
        if self.pak is not None:
            self._read_pak_index()

    def _read_pak_index(self) -> None:
        assert self.pak is not None
        with self.pak.open("rb") as stream:
            if _read_exact(stream, len(PAK_MAGIC)) != PAK_MAGIC:
                raise ValueError(f"Not a supported Starbound packed.pak: {self.pak}")
            index_offset = struct.unpack(">Q", _read_exact(stream, 8))[0]
            if index_offset < 16 or index_offset >= self.pak.stat().st_size:
                raise ValueError(f"Invalid packed.pak index offset: {self.pak}")
            stream.seek(index_offset)
            if _read_exact(stream, len(PAK_INDEX_MAGIC)) != PAK_INDEX_MAGIC:
                raise ValueError(f"packed.pak has no SBAsset6 INDEX: {self.pak}")
            for _ in range(read_varuint(stream)):
                read_string(stream)
                read_dynamic(stream)
            for _ in range(read_varuint(stream)):
                asset_path = read_string(stream)
                offset, size = struct.unpack(">QQ", _read_exact(stream, 16))
                self.entries[_normalize_asset_path(asset_path)] = (asset_path, offset, size)

    def paths(self, suffix: str) -> Iterator[str]:
        suffix = suffix.lower()
        if self.root is not None:
            if self._unpacked_paths is None:
                self._unpacked_paths = []
                for path in sorted(self.root.rglob("*")):
                    if path.is_file():
                        virtual = "/" + path.relative_to(self.root).as_posix()
                        self._unpacked_paths.append((virtual.lower(), virtual))
            for lowered, virtual in self._unpacked_paths:
                if lowered.endswith(suffix):
                    yield virtual
        else:
            for asset_path, _offset, _size in sorted(self.entries.values()):
                if asset_path.lower().endswith(suffix):
                    yield asset_path

    def read_bytes(self, asset_path: str) -> bytes:
        plain_path = asset_path.split(":", 1)[0]
        normalized = _normalize_asset_path(plain_path)
        if self.root is not None:
            path = self.root / normalized.lstrip("/")
            if not path.is_file():
                raise FileNotFoundError(f"Asset not found: {plain_path}")
            return path.read_bytes()
        assert self.pak is not None
        try:
            _original, offset, size = self.entries[normalized]
        except KeyError as exc:
            raise FileNotFoundError(f"Asset not found in packed.pak: {plain_path}") from exc
        with self.pak.open("rb") as stream:
            stream.seek(offset)
            return _read_exact(stream, size)

    def json(self, reference: str, relative_to: str | None = None) -> Any:
        path_part, separator, query = reference.partition(":")
        if relative_to is not None:
            path_part = _relative_asset_path(relative_to, path_part)
        document = parse_starbound_json(self.read_bytes(path_part), path_part)
        return _asset_json_query(document, query) if separator else document


def _pak_biome_documents(pak: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield resolved .biome assets directly from an SBAsset6 packed.pak."""

    with pak.open("rb") as stream:
        if _read_exact(stream, len(PAK_MAGIC)) != PAK_MAGIC:
            raise ValueError(f"Not a supported Starbound packed.pak: {pak}")
        index_offset = struct.unpack(">Q", _read_exact(stream, 8))[0]
        if index_offset < 16 or index_offset >= pak.stat().st_size:
            raise ValueError(f"Invalid packed.pak index offset: {pak}")
        stream.seek(index_offset)
        if _read_exact(stream, len(PAK_INDEX_MAGIC)) != PAK_INDEX_MAGIC:
            raise ValueError(f"packed.pak has no SBAsset6 INDEX: {pak}")

        # The package metadata is an untagged SBON map.
        for _ in range(read_varuint(stream)):
            read_string(stream)
            read_dynamic(stream)

        entries: list[tuple[str, int, int]] = []
        for _ in range(read_varuint(stream)):
            asset_path = read_string(stream)
            offset, size = struct.unpack(">QQ", _read_exact(stream, 16))
            if asset_path.lower().endswith(".biome"):
                entries.append((asset_path, offset, size))

        file_size = pak.stat().st_size
        for asset_path, offset, size in entries:
            if offset < 16 or size < 0 or offset + size > index_offset or offset + size > file_size:
                raise ValueError(f"Invalid packed asset range for {asset_path}")
            stream.seek(offset)
            document = parse_starbound_json(_read_exact(stream, size), asset_path)
            if isinstance(document, dict):
                yield asset_path, document


def _directory_biome_documents(root: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted(root.rglob("*.biome")):
        if not path.is_file():
            continue
        relative = "/" + path.relative_to(root).as_posix()
        document = parse_starbound_json(path.read_bytes(), relative)
        if isinstance(document, dict):
            yield relative, document


def load_asset_biome_catalog(assets_folder: Path) -> dict[str, Any]:
    """Discover biome definitions in an unpacked assets tree or packed.pak.

    The GUI asks for a directory.  For convenience the core also accepts a
    direct packed.pak path.  The unpacked virtual root is detected even if the
    user selects its parent ``assets`` directory; otherwise packed.pak is
    detected in the selected directory or in its conventional assets child.
    """

    reader = StarboundAssetReader(Path(assets_folder))
    biomes: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    for asset_path in reader.paths(".biome"):
        document = reader.json(asset_path)
        name = document.get("name")
        if not isinstance(name, str) or not name:
            continue
        biomes[name] = document
        paths[name] = asset_path
    if not biomes:
        raise ValueError(f"No readable .biome definitions were found in {reader.source}")
    return {
        "mode": reader.mode,
        "source": str(reader.source),
        "biomes": biomes,
        "paths": paths,
    }


def write_dynamic(stream: BinaryIO, value: Any) -> None:
    if value is None:
        stream.write(b"\x01")
    elif isinstance(value, bool):
        stream.write(b"\x03\x01" if value else b"\x03\x00")
    elif isinstance(value, int):
        stream.write(b"\x04")
        write_varint(stream, value)
    elif isinstance(value, float):
        stream.write(b"\x02")
        stream.write(struct.pack(">d", value))
    elif isinstance(value, str):
        stream.write(b"\x05")
        write_string(stream, value)
    elif isinstance(value, list):
        stream.write(b"\x06")
        write_varuint(stream, len(value))
        for item in value:
            write_dynamic(stream, item)
    elif isinstance(value, dict):
        stream.write(b"\x07")
        write_varuint(stream, len(value))
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"SBON map key must be a string, got {type(key)!r}")
            write_string(stream, key)
            write_dynamic(stream, item)
    else:
        raise TypeError(f"Unsupported SBON value {value!r}")


def read_versioned_json(stream: BinaryIO) -> tuple[str, int | None, Any]:
    name = read_string(stream)
    is_versioned = _read_exact(stream, 1) != b"\x00"
    version = struct.unpack(">i", _read_exact(stream, 4))[0] if is_versioned else None
    return name, version, read_dynamic(stream)


def write_versioned_json(
    stream: BinaryIO, name: str, version: int | None, data: Any
) -> None:
    write_string(stream, name)
    if version is None:
        stream.write(b"\x00")
    else:
        stream.write(b"\x01")
        stream.write(struct.pack(">i", version))
    write_dynamic(stream, data)


def read_subversioning(stream: BinaryIO, context: str = "VersionedJson") -> dict[str, Any] | None:
    """Read OpenStarbound's optional *separate* VersionedJson extension.

    The outer payload is a JsonObject (no SBON type byte), not a Json value.
    Do not consume this in read_versioned_json: entity sectors store all base
    entities first, followed by one extension for each entity in the same order.
    """
    first = stream.read(1)
    if not first:
        return None
    version = struct.unpack(">I", first + _read_exact(stream, 3))[0]
    if version != 1:
        raise WorldFormatError(
            f"Unsupported OpenStarbound subversioning version {version} in {context}; "
            "the file has not been modified"
        )
    count = read_varuint(stream)
    if count > 100000:
        raise WorldFormatError(f"Too many extension fields in {context}")
    data = {}
    for _ in range(count):
        key = read_string(stream)
        if key in data:
            raise WorldFormatError(f"Duplicate extension key {key!r} in {context}")
        data[key] = read_dynamic(stream)
    extension = {"version": version, "data": data}
    _validate_subversioning(extension, context)
    return extension


def _validate_subversioning(extension: Any, context: str) -> None:
    if not isinstance(extension, dict) or type(extension.get("version")) is not int or extension["version"] != 1:
        raise WorldFormatError(f"Unsupported OpenStarbound subversioning in {context}")
    data = extension.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("subVersions"), dict):
        raise WorldFormatError(f"Missing subVersions object in {context}")
    for name, value in data["subVersions"].items():
        if not isinstance(name, str) or type(value) is not int or not 0 <= value <= 0xffffffff:
            raise WorldFormatError(f"Invalid subversion {name!r} in {context}")


def write_subversioning(stream: BinaryIO, extension: dict[str, Any] | None,
                       context: str = "VersionedJson") -> None:
    if extension is None:
        return
    _validate_subversioning(extension, context)
    stream.write(struct.pack(">I", extension["version"]))
    data = extension["data"]
    write_varuint(stream, len(data))
    for key, value in data.items():
        write_string(stream, key)
        write_dynamic(stream, value)


class SerializedEntityEntry(tuple):
    """Legacy four-tuple with an independently preserved native trailer."""

    def __new__(cls, name, version, data, raw, subversioning_raw=b""):
        item = super().__new__(cls, (name, version, data, raw))
        item.subversioning_raw = subversioning_raw
        return item

    def __getnewargs__(self):
        return (*self, self.subversioning_raw)


def decode_entity_sector(compressed: bytes) -> list[SerializedEntityEntry]:
    raw = zlib.decompress(compressed)
    if len(raw) > 128 * 1024 * 1024:
        raise WorldFormatError("Entity sector is too large")
    stream = io.BytesIO(raw)
    count = read_varuint(stream)
    if count > 100000:
        raise WorldFormatError("Too many entities in one sector")
    base = []
    for _ in range(count):
        start = stream.tell()
        name, version, data = read_versioned_json(stream)
        base.append((name, version, data, raw[start:stream.tell()]))
    entries = []
    has_extensions = stream.tell() != len(raw)
    for index, entry in enumerate(base):
        start = stream.tell()
        if has_extensions and read_subversioning(stream, f"entity {index}") is None:
            raise WorldFormatError("Missing OpenStarbound per-entity subversioning")
        entries.append(SerializedEntityEntry(*entry, raw[start:stream.tell()]))
    if stream.read():
        raise WorldFormatError("Unexpected trailing data in entity sector")
    return entries


def encode_entity_sector(entries: Sequence[tuple]) -> bytes:
    stream = io.BytesIO()
    write_varuint(stream, len(entries))
    for entry in entries:
        stream.write(entry[3])
    if any(getattr(entry, "subversioning_raw", b"") for entry in entries):
        # Mixed native/vanilla plants require an empty extension for each
        # vanilla entry, otherwise the engine attaches trailers to wrong IDs.
        for entry in entries:
            raw = getattr(entry, "subversioning_raw", b"")
            if raw:
                check = io.BytesIO(raw)
                read_subversioning(check, "entity")
                if check.read():
                    raise WorldFormatError("Unexpected data after entity subversioning")
                stream.write(raw)
            else:
                write_subversioning(stream, {"version": 1, "data": {"subVersions": {}}})
    return zlib.compress(stream.getvalue(), 9)


class LeafStream:
    """Read the logical byte stream stored across chained LL blocks."""

    def __init__(self, db: "BTreeDB5", first_block: int):
        self.db = db
        self.block = first_block
        self.offset = 2
        self._load_block(first_block)

    def _load_block(self, block: int) -> None:
        self.block = block
        self.db.stream.seek(self.db.block_offset(block))
        if _read_exact(self.db.stream, 2) != b"LL":
            raise WorldFormatError(f"Block {block} is not an LL leaf block")
        self.offset = 2

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("LeafStream only supports reads with an explicit size")
        result = bytearray()
        remaining = size
        payload_end = self.db.block_size - 4
        while remaining:
            available = payload_end - self.offset
            take = min(available, remaining)
            result.extend(_read_exact(self.db.stream, take))
            self.offset += take
            remaining -= take
            if remaining:
                next_block = struct.unpack(">I", _read_exact(self.db.stream, 4))[0]
                if next_block == MAX_U32:
                    raise WorldFormatError("Leaf chain ended before its record data")
                self._load_block(next_block)
        return bytes(result)

    def skip(self, size: int) -> None:
        remaining = size
        payload_end = self.db.block_size - 4
        while remaining:
            available = payload_end - self.offset
            take = min(available, remaining)
            self.db.stream.seek(take, io.SEEK_CUR)
            self.offset += take
            remaining -= take
            if remaining:
                next_block = struct.unpack(">I", _read_exact(self.db.stream, 4))[0]
                if next_block == MAX_U32:
                    raise WorldFormatError("Leaf chain ended while skipping a record")
                self._load_block(next_block)


class BTreeDB5:
    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self._read_header()

    def _read_header(self) -> None:
        self.stream.seek(0)
        header = _read_exact(self.stream, HEADER_SIZE)
        if header[:8] != MAGIC:
            raise WorldFormatError("Not a BTreeDB5 file")
        self.block_size = struct.unpack_from(">i", header, 8)[0]
        self.identifier = header[12:28].rstrip(b"\x00").decode("utf-8")
        self.key_size = struct.unpack_from(">i", header, 28)[0]
        self.use_alt_root = bool(header[32])
        slot = 50 if self.use_alt_root else 33
        self.free_block = struct.unpack_from(">I", header, slot)[0]
        self.stored_size = struct.unpack_from(">q", header, slot + 4)[0]
        self.root_block = struct.unpack_from(">I", header, slot + 12)[0]
        self.root_is_leaf = bool(header[slot + 16])
        if self.block_size < 32 or self.key_size < 1:
            raise WorldFormatError("Invalid BTreeDB5 block/key size")

    def block_offset(self, block: int) -> int:
        return HEADER_SIZE + block * self.block_size

    def _read_index(self, block: int) -> tuple[int, list[bytes], list[int]]:
        self.stream.seek(self.block_offset(block))
        if _read_exact(self.stream, 2) != b"II":
            raise WorldFormatError(f"Block {block} is not an II index block")
        height = _read_exact(self.stream, 1)[0]
        count, first_child = struct.unpack(">II", _read_exact(self.stream, 8))
        keys: list[bytes] = []
        children = [first_child]
        for _ in range(count):
            keys.append(_read_exact(self.stream, self.key_size))
            children.append(struct.unpack(">I", _read_exact(self.stream, 4))[0])
        return height, keys, children

    def _read_leaf(self, block: int) -> list[tuple[bytes, bytes]]:
        reader = LeafStream(self, block)
        count = struct.unpack(">I", reader.read(4))[0]
        records: list[tuple[bytes, bytes]] = []
        for _ in range(count):
            key = reader.read(self.key_size)
            length = read_varuint(reader)  # type: ignore[arg-type]
            records.append((key, reader.read(length)))
        return records

    def records(self) -> Iterator[tuple[bytes, bytes]]:
        """Yield all records from the active root in binary key order."""

        seen: set[int] = set()

        def walk(block: int, is_leaf: bool) -> Iterator[tuple[bytes, bytes]]:
            if block in seen:
                raise WorldFormatError(f"B-tree cycle or duplicate child at block {block}")
            seen.add(block)
            if is_leaf:
                yield from self._read_leaf(block)
                return
            height, _, children = self._read_index(block)
            children_are_leaves = height == 0
            for child in children:
                yield from walk(child, children_are_leaves)

        yield from walk(self.root_block, self.root_is_leaf)

    def get(self, key: bytes) -> bytes:
        if len(key) != self.key_size:
            raise ValueError(f"Key must be exactly {self.key_size} bytes")
        block = self.root_block
        is_leaf = self.root_is_leaf
        while not is_leaf:
            height, keys, children = self._read_index(block)
            child_index = 0
            while child_index < len(keys) and key >= keys[child_index]:
                child_index += 1
            block = children[child_index]
            is_leaf = height == 0
        for current_key, value in self._read_leaf(block):
            if current_key == key:
                return value
        raise KeyError(key.hex())


class BTreeDB5Writer:
    """Build a compact BTreeDB5 database without mutating the source file."""

    def __init__(self, block_size: int, identifier: str, key_size: int):
        self.block_size = block_size
        self.identifier = identifier
        self.key_size = key_size
        self.blocks: list[bytearray] = []

    def _new_block(self) -> tuple[int, bytearray]:
        block = bytearray(self.block_size)
        self.blocks.append(block)
        return len(self.blocks) - 1, block

    def _write_leaf(self, records: Sequence[tuple[bytes, bytes]]) -> int:
        logical = io.BytesIO()
        logical.write(struct.pack(">I", len(records)))
        for key, value in records:
            if len(key) != self.key_size:
                raise ValueError(f"Invalid key length for {key.hex()}")
            logical.write(key)
            write_varuint(logical, len(value))
            logical.write(value)
        payload = logical.getvalue()
        capacity = self.block_size - 6
        first_block = -1
        previous: bytearray | None = None
        for offset in range(0, max(1, len(payload)), capacity):
            index, block = self._new_block()
            if first_block < 0:
                first_block = index
            block[:2] = b"LL"
            chunk = payload[offset : offset + capacity]
            block[2 : 2 + len(chunk)] = chunk
            struct.pack_into(">I", block, self.block_size - 4, MAX_U32)
            if previous is not None:
                struct.pack_into(">I", previous, self.block_size - 4, index)
            previous = block
        return first_block

    def _write_index(
        self, height: int, children: Sequence[tuple[bytes, int]]
    ) -> int:
        if len(children) < 2:
            raise ValueError("An index node must have at least two children")
        index, block = self._new_block()
        block[:2] = b"II"
        block[2] = height
        struct.pack_into(">I", block, 3, len(children) - 1)
        struct.pack_into(">I", block, 7, children[0][1])
        offset = 11
        for first_key, child_block in children[1:]:
            block[offset : offset + self.key_size] = first_key
            offset += self.key_size
            struct.pack_into(">I", block, offset, child_block)
            offset += 4
        if offset > self.block_size:
            raise ValueError("Too many children for an index block")
        return index

    def build(self, records: Iterable[tuple[bytes, bytes]]) -> tuple[int, bool]:
        ordered = sorted(records, key=lambda item: item[0])
        if not ordered:
            return self._write_leaf([]), True
        if len({key for key, _ in ordered}) != len(ordered):
            raise ValueError("Duplicate BTreeDB5 keys")

        # Starbound's own implementation splits leaves at two records.  Using
        # the same small logical leaf size also bounds the cost of record edits.
        level: list[tuple[bytes, int]] = []
        for offset in range(0, len(ordered), 2):
            group = ordered[offset : offset + 2]
            level.append((group[0][0], self._write_leaf(group)))
        if len(level) == 1:
            return level[0][1], True

        max_children = (self.block_size - 11) // (self.key_size + 4) + 1
        height = 0
        while len(level) > 1:
            next_level: list[tuple[bytes, int]] = []
            group_count = (len(level) + max_children - 1) // max_children
            base_size, larger_groups = divmod(len(level), group_count)
            offset = 0
            for group_number in range(group_count):
                group_size = base_size + (1 if group_number < larger_groups else 0)
                group = level[offset : offset + group_size]
                offset += group_size
                next_level.append((group[0][0], self._write_index(height, group)))
            level = next_level
            height += 1
        return level[0][1], False

    def write(self, destination: BinaryIO, records: Iterable[tuple[bytes, bytes]]) -> None:
        root_block, root_is_leaf = self.build(records)
        file_size = HEADER_SIZE + len(self.blocks) * self.block_size
        header = bytearray(HEADER_SIZE)
        header[:8] = MAGIC
        struct.pack_into(">i", header, 8, self.block_size)
        encoded_identifier = self.identifier.encode("utf-8")
        if len(encoded_identifier) > 16:
            raise ValueError("BTreeDB5 identifier is longer than 16 bytes")
        header[12 : 12 + len(encoded_identifier)] = encoded_identifier
        struct.pack_into(">i", header, 28, self.key_size)
        header[32] = 0
        struct.pack_into(">I", header, 33, MAX_U32)
        struct.pack_into(">q", header, 37, file_size)
        struct.pack_into(">I", header, 45, root_block)
        header[49] = int(root_is_leaf)
        struct.pack_into(">I", header, 50, MAX_U32)
        struct.pack_into(">q", header, 54, file_size)
        struct.pack_into(">I", header, 62, MAX_U32)
        header[66] = 1
        destination.write(header)
        for block in self.blocks:
            destination.write(block)


def decode_world_metadata(compressed: bytes) -> dict[str, Any]:
    stream = io.BytesIO(zlib.decompress(compressed))
    width, height = struct.unpack(">ii", _read_exact(stream, 8))
    name, version, body = read_versioned_json(stream)
    if name != "WorldMetadata" or not isinstance(body, dict):
        raise WorldFormatError(f"Expected WorldMetadata, got {name!r}")
    extension = read_subversioning(stream, "WorldMetadata")
    if stream.read(1):
        raise WorldFormatError("Unexpected trailing data in WorldMetadata record")
    format_info = {"name": name, "version": version}
    if extension is not None:
        format_info["subVersioning"] = extension
    return {
        "format": format_info,
        "size": [width, height],
        "metadata": body,
    }


def encode_world_metadata(document: dict[str, Any]) -> bytes:
    try:
        width, height = document["size"]
        format_info = document["format"]
        body = document["metadata"]
    except (KeyError, TypeError, ValueError) as exc:
        raise WorldFormatError("Invalid exported metadata JSON shape") from exc
    # BiomeIndex is uint8_t in the game, not an arbitrary SBON integer.
    # Temporary editing indexes must be compacted before serialization.
    validate_native_biome_indexes(document)
    raw = io.BytesIO()
    raw.write(struct.pack(">ii", int(width), int(height)))
    write_versioned_json(
        raw,
        str(format_info.get("name", "WorldMetadata")),
        format_info.get("version"),
        body,
    )
    write_subversioning(raw, format_info.get("subVersioning"), "WorldMetadata")
    return zlib.compress(raw.getvalue(), level=9)


def load_records(path: Path) -> tuple[BTreeDB5, list[tuple[bytes, bytes]]]:
    stream = path.open("rb")
    try:
        database = BTreeDB5(stream)
        if database.identifier != WORLD_IDENTIFIER or database.key_size != 5:
            raise WorldFormatError(
                f"Expected World4 with 5-byte keys, got "
                f"{database.identifier!r}/{database.key_size}"
            )
        records = list(database.records())
    except Exception:
        stream.close()
        raise
    database._owned_stream = stream  # type: ignore[attr-defined]
    return database, records


def close_database(database: BTreeDB5) -> None:
    stream = getattr(database, "_owned_stream", None)
    if stream is not None:
        stream.close()


def atomic_write_world(
    source: Path,
    destination: Path,
    replacement_records: Iterable[tuple[bytes, bytes]],
    database: BTreeDB5,
) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("Refusing to overwrite the source .world; choose a new output path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    replacement_records = list(replacement_records)
    if len({key for key, _value in replacement_records}) != len(replacement_records):
        raise WorldFormatError("Refusing to write a World4 database with duplicate record keys")
    metadata_record = dict(replacement_records).get(WORLD_METADATA_KEY)
    if metadata_record is None:
        raise WorldFormatError("Refusing to write World4 without WorldMetadata")
    # Also protect callers supplying already-encoded replacement records.
    validate_native_biome_indexes(decode_world_metadata(metadata_record))
    temp_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix=destination.name + ".", suffix=".tmp",
            dir=destination.parent, delete=False
        ) as stream:
            temp_file = Path(stream.name)
            writer = BTreeDB5Writer(
                database.block_size, database.identifier, database.key_size
            )
            writer.write(stream, replacement_records)
            stream.flush()
            os.fsync(stream.fileno())
        # Do a complete structural and content verification before the atomic
        # rename.  A bug in the writer must never replace a usable destination.
        expected = dict(replacement_records)
        verification, actual_records = load_records(temp_file)
        try:
            actual = dict(actual_records)
            if len(actual_records) != len(replacement_records) or actual != expected:
                raise WorldFormatError("Rebuilt database failed record verification")
            decode_world_metadata(actual[WORLD_METADATA_KEY])
        finally:
            close_database(verification)
        os.replace(temp_file, destination)
        shutil.copystat(source, destination)
    finally:
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def celestial_coordinate(document: dict[str, Any]) -> tuple[list[int], int, int]:
    """Return the celestial location, planet orbit and satellite orbit."""

    parameters = world_template(document).get("celestialParameters")
    if not isinstance(parameters, dict):
        raise WorldFormatError("这个 world 没有 celestialParameters")
    coordinate = parameters.get("coordinate")
    if isinstance(coordinate, dict):
        location = coordinate.get("location")
        planet = coordinate.get("planet", 0)
        satellite = coordinate.get("satellite", 0)
    elif isinstance(coordinate, str):
        pieces = [piece for piece in re.split(r"[ _:]+", coordinate) if piece]
        if len(pieces) not in (4, 5):
            raise WorldFormatError("celestialParameters.coordinate 格式无效")
        location = [int(value) for value in pieces[:3]]
        planet = int(pieces[3])
        satellite = int(pieces[4]) if len(pieces) == 5 else 0
    else:
        raise WorldFormatError("celestialParameters.coordinate 格式无效")
    if (
        not isinstance(location, list)
        or len(location) != 3
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in location)
        or not isinstance(planet, int)
        or isinstance(planet, bool)
        or planet <= 0
        or not isinstance(satellite, int)
        or isinstance(satellite, bool)
        or satellite < 0
    ):
        raise WorldFormatError("celestialParameters.coordinate 不是有效星球坐标")
    return list(location), planet, satellite


def copy_world_to_celestial_coordinate(
    source: Path,
    destination: Path,
    location: Sequence[int],
    planet: int,
    satellite: int,
) -> None:
    """Copy a generated world to a new orbit without regenerating its contents.

    Only ``WorldMetadata.worldTemplate.celestialParameters.coordinate`` is
    synchronized.  Tile sectors, entities, liquids, objects, player buildings
    and every other database record are retained byte-for-byte.
    """

    source = Path(source)
    destination = Path(destination)
    database, records = load_records(source)
    try:
        record_map = dict(records)
        document = decode_world_metadata(record_map[WORLD_METADATA_KEY])
        celestial = world_template(document).get("celestialParameters")
        if not isinstance(celestial, dict):
            raise WorldFormatError("这个 world 没有 celestialParameters")
        celestial["coordinate"] = {
            "location": [int(value) for value in location],
            "planet": int(planet),
            "satellite": int(satellite),
        }
        replacement = encode_world_metadata(document)
        updated = [
            (key, replacement if key == WORLD_METADATA_KEY else value)
            for key, value in records
        ]
        atomic_write_world(source, destination, updated, database)
    finally:
        close_database(database)


def copy_system_to_celestial_location(
    source: Path,
    destination: Path,
    location: Sequence[int],
) -> None:
    """Copy a generated ``.system`` file to a new celestial location.

    Starbound stores the system location both in the filename and inside the
    VersionedJson document.  Object orbit targets may also contain the old
    location, so every exact embedded reference is synchronized while all
    unrelated system state is preserved.
    """

    source = Path(source)
    destination = Path(destination)
    new_location = [int(value) for value in location]
    if len(new_location) != 3:
        raise ValueError("Celestial location requires X, Y and Z")
    if source.resolve() == destination.resolve():
        return
    if destination.exists():
        raise FileExistsError(f"Destination system file already exists: {destination}")

    source_bytes = source.read_bytes()
    if not source_bytes.startswith(VERSIONED_JSON_FILE_MAGIC):
        raise WorldFormatError("The .system file is missing its SBVJ01 header")
    stream = io.BytesIO(source_bytes[len(VERSIONED_JSON_FILE_MAGIC) :])
    name, version, body = read_versioned_json(stream)
    trailing = stream.read()
    if name != "System" or not isinstance(body, dict):
        raise WorldFormatError(f"Expected System VersionedJson, got {name!r}")
    old_location = body.get("location")
    if not (
        isinstance(old_location, list)
        and len(old_location) == 3
        and all(isinstance(value, int) and not isinstance(value, bool) for value in old_location)
    ):
        raise WorldFormatError("The .system file has no valid location")

    def rewrite(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("location") == old_location:
                value["location"] = list(new_location)
            for child in value.values():
                rewrite(child)
        elif isinstance(value, list):
            for child in value:
                rewrite(child)

    rewrite(body)
    body["location"] = list(new_location)
    output = io.BytesIO()
    output.write(VERSIONED_JSON_FILE_MAGIC)
    write_versioned_json(output, name, version, body)
    output.write(trailing)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=destination.name + ".",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as target:
            temporary = Path(target.name)
            target.write(output.getvalue())
            target.flush()
            os.fsync(target.fileno())
        verify_bytes = temporary.read_bytes()
        if not verify_bytes.startswith(VERSIONED_JSON_FILE_MAGIC):
            raise WorldFormatError("Copied .system file lost its SBVJ01 header")
        verify = io.BytesIO(verify_bytes[len(VERSIONED_JSON_FILE_MAGIC) :])
        verify_name, _verify_version, verify_body = read_versioned_json(verify)
        if verify_name != "System" or verify_body.get("location") != new_location:
            raise WorldFormatError("Copied .system file failed coordinate verification")
        os.replace(temporary, destination)
        shutil.copystat(source, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_generated_system_file(path: Path) -> dict[str, Any]:
    """Read a generated ``.system`` VersionedJson file for safe editing."""

    path = Path(path)
    payload = path.read_bytes()
    if not payload.startswith(VERSIONED_JSON_FILE_MAGIC):
        raise WorldFormatError("The .system file is missing its SBVJ01 header")
    stream = io.BytesIO(payload[len(VERSIONED_JSON_FILE_MAGIC) :])
    name, version, body = read_versioned_json(stream)
    if name != "System" or not isinstance(body, dict):
        raise WorldFormatError(f"Expected System VersionedJson, got {name!r}")
    location = body.get("location")
    if not (
        isinstance(location, list)
        and len(location) == 3
        and all(isinstance(value, int) and not isinstance(value, bool) for value in location)
    ):
        raise WorldFormatError("The .system file has no valid location")
    if not isinstance(body.get("objects", []), list):
        raise WorldFormatError("The .system file has an invalid objects list")
    return {
        "name": name,
        "version": version,
        "body": deep_copy_json(body),
        "trailing": stream.read(),
    }


def _rewrite_embedded_system_location(
    body: dict[str, Any], old_location: Sequence[int], new_location: Sequence[int]
) -> None:
    old_values = [int(value) for value in old_location]
    new_values = [int(value) for value in new_location]

    def rewrite(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("location") == old_values:
                value["location"] = list(new_values)
            for child in value.values():
                rewrite(child)
        elif isinstance(value, list):
            for child in value:
                rewrite(child)

    rewrite(body)
    body["location"] = list(new_values)


def write_generated_system_file(
    source: Path,
    destination: Path,
    state: dict[str, Any],
    location: Sequence[int],
) -> Path | None:
    """Write an edited ``.system`` atomically and return its backup, if any."""

    source = Path(source)
    destination = Path(destination)
    body = deep_copy_json(state.get("body"))
    if not isinstance(body, dict):
        raise ValueError("System state has no body")
    old_location = body.get("location")
    new_location = [int(value) for value in location]
    if not isinstance(old_location, list) or len(old_location) != 3:
        raise ValueError("System state has no valid source location")
    _rewrite_embedded_system_location(body, old_location, new_location)
    validate_finite_json_numbers(body, "Generated .system data")
    output = io.BytesIO()
    output.write(VERSIONED_JSON_FILE_MAGIC)
    write_versioned_json(
        output,
        str(state.get("name", "System")),
        state.get("version"),
        body,
    )
    trailing = state.get("trailing", b"")
    if isinstance(trailing, bytes):
        output.write(trailing)

    same_path = source.resolve() == destination.resolve()
    if not same_path and destination.exists():
        raise FileExistsError(f"Destination system file already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=destination.name + ".",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as target:
            temporary = Path(target.name)
            target.write(output.getvalue())
            target.flush()
            os.fsync(target.fileno())
        verify = load_generated_system_file(temporary)
        if verify["body"].get("location") != new_location:
            raise WorldFormatError("Edited .system file failed coordinate verification")
        if same_path:
            backup = _unique_backup_path(source)
            shutil.copy2(source, backup)
        os.replace(temporary, destination)
        if source.exists() and not same_path:
            shutil.copystat(source, destination)
        return backup
    except Exception:
        if backup is not None and backup.exists() and same_path:
            shutil.copy2(backup, source)
        raise
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _rewrite_celestial_world_id(
    value: str,
    source_location: Sequence[int],
    destination_location: Sequence[int],
    orbit_map: dict[tuple[int, int], tuple[int, int]],
) -> str:
    """Rewrite an exact CelestialWorld id used by a client context."""

    match = re.fullmatch(
        r"CelestialWorld:([+-]?\d+):([+-]?\d+):([+-]?\d+):"
        r"([+-]?\d+)(?::([+-]?\d+))?",
        value,
    )
    if match is None:
        return value
    location = [int(match.group(index)) for index in range(1, 4)]
    if location != [int(item) for item in source_location]:
        return value
    old_orbit = (int(match.group(4)), int(match.group(5) or 0))
    new_orbit = orbit_map.get(old_orbit, old_orbit)
    pieces = [
        "CelestialWorld",
        *(str(int(item)) for item in destination_location),
        str(new_orbit[0]),
    ]
    if new_orbit[1]:
        pieces.append(str(new_orbit[1]))
    return ":".join(pieces)


def rewrite_client_context_celestial_references(
    body: dict[str, Any],
    source_location: Sequence[int],
    destination_location: Sequence[int],
    orbit_map: dict[tuple[int, int], tuple[int, int]],
) -> int:
    """Synchronize ship locations after moving a Celestial body or system.

    A generated system can be edited while a player ship is parked at one of
    its bodies.  Starbound stores that location in ``*.clientcontext``.  If a
    body is moved but this reference is left behind, SystemWorld asks the
    Celestial database for a coordinate that no longer exists and the cockpit
    interface cannot open.
    """

    source = [int(item) for item in source_location]
    destination = [int(item) for item in destination_location]
    changes = 0

    def rewrite(value: Any) -> Any:
        nonlocal changes
        if isinstance(value, dict):
            location = value.get("location")
            planet = value.get("planet")
            satellite = value.get("satellite", 0)
            if (
                location == source
                and isinstance(planet, int)
                and not isinstance(planet, bool)
                and isinstance(satellite, int)
                and not isinstance(satellite, bool)
            ):
                old_orbit = (int(planet), int(satellite))
                new_orbit = orbit_map.get(old_orbit, old_orbit)
                if location != destination:
                    value["location"] = list(destination)
                    changes += 1
                if old_orbit != new_orbit:
                    value["planet"] = new_orbit[0]
                    value["satellite"] = new_orbit[1]
                    changes += 1
            for key, child in list(value.items()):
                value[key] = rewrite(child)
            return value
        if isinstance(value, list):
            for index, child in enumerate(value):
                value[index] = rewrite(child)
            return value
        if isinstance(value, str):
            edited = _rewrite_celestial_world_id(
                value, source, destination, orbit_map
            )
            if edited != value:
                changes += 1
            return edited
        return value

    rewrite(body)
    return changes


def migrate_client_contexts(
    universe_folder: Path,
    source_location: Sequence[int],
    destination_location: Sequence[int],
    orbit_map: dict[tuple[int, int], tuple[int, int]],
) -> list[tuple[Path, Path, int]]:
    """Atomically update affected ``*.clientcontext`` files with backups."""

    universe_folder = Path(universe_folder)
    results: list[tuple[Path, Path, int]] = []
    try:
        for path in sorted(universe_folder.glob("*.clientcontext")):
            payload = path.read_bytes()
            if not payload.startswith(VERSIONED_JSON_FILE_MAGIC):
                continue
            stream = io.BytesIO(payload[len(VERSIONED_JSON_FILE_MAGIC) :])
            name, version, body = read_versioned_json(stream)
            trailing = stream.read()
            if name != "ClientContext" or not isinstance(body, dict):
                continue
            edited = deep_copy_json(body)
            change_count = rewrite_client_context_celestial_references(
                edited, source_location, destination_location, orbit_map
            )
            if not change_count:
                continue
            output = io.BytesIO()
            output.write(VERSIONED_JSON_FILE_MAGIC)
            write_versioned_json(output, name, version, edited)
            output.write(trailing)
            backup = _unique_backup_path(path)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w+b",
                    prefix=path.name + ".",
                    suffix=".tmp",
                    dir=path.parent,
                    delete=False,
                ) as target:
                    temporary = Path(target.name)
                    target.write(output.getvalue())
                    target.flush()
                    os.fsync(target.fileno())
                verification = temporary.read_bytes()
                verify_stream = io.BytesIO(
                    verification[len(VERSIONED_JSON_FILE_MAGIC) :]
                )
                verify_name, _verify_version, verify_body = read_versioned_json(
                    verify_stream
                )
                if verify_name != "ClientContext" or verify_body != edited:
                    raise WorldFormatError("Edited client context failed verification")
                shutil.copy2(path, backup)
                os.replace(temporary, path)
                results.append((path, backup, change_count))
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
    except Exception:
        for path, backup, _count in reversed(results):
            if backup.exists():
                shutil.copy2(backup, path)
        raise
    return results


def system_station_types(assets_folder: Path) -> list[str]:
    reader = StarboundAssetReader(Path(assets_folder))
    config = reader.json("/system_objects.config")
    if not isinstance(config, dict):
        return []
    return sorted(
        name
        for name, value in config.items()
        if isinstance(value, dict)
        and bool(value.get("permanent", False))
        and (
            name.startswith("spacestation_")
            or name == "playerstation"
        )
    )


def system_station_config(
    assets_folder: Path, station_type: str
) -> dict[str, Any]:
    """Return one station's resolved asset configuration."""

    reader = StarboundAssetReader(Path(assets_folder))
    config = reader.json(f"/system_objects.config:{station_type}")
    if not isinstance(config, dict):
        raise ValueError(f"System object type not found: {station_type}")
    return deep_copy_json(config)


def build_system_station_object(
    assets_folder: Path,
    location: Sequence[int],
    station_type: str,
    planet_orbit: int = 0,
    satellite_orbit: int = 0,
    *,
    orbit_radius: float | None = None,
    orbit_angle: float = 0.0,
    orbit_direction: int = 1,
) -> dict[str, Any]:
    """Create a permanent SystemObject station orbiting a celestial target."""

    reader = StarboundAssetReader(Path(assets_folder))
    config = reader.json(f"/system_objects.config:{station_type}")
    if not isinstance(config, dict) or not bool(config.get("permanent", False)):
        raise ValueError(f"Not a permanent system station type: {station_type}")
    location_values = [int(value) for value in location]
    if len(location_values) != 3:
        raise ValueError("Celestial location requires X, Y and Z")
    planet_orbit = int(planet_orbit)
    satellite_orbit = int(satellite_orbit)
    if planet_orbit < 0 or satellite_orbit < 0:
        raise ValueError("Station orbit target is invalid")
    radius_values = config.get("orbitRange", [10.0, 10.0])
    radius_low, radius_high = _range_pair(radius_values, (10.0, 10.0))
    # Planetary orbit distance also includes the target body's visual radius.
    # A conservative extra radius keeps a manually inserted station clear of
    # the body until SystemWorld performs its first normal orbit update.
    default_radius = (
        (radius_low + radius_high) / 2.0
        + (25.0 if planet_orbit else 100.0)
    )
    if __package__:
        from .world_parameter_validation import native_station_orbit
    else:
        from world_parameter_validation import native_station_orbit
    radius, angle_degrees, direction = native_station_orbit(
        default_radius if orbit_radius is None else orbit_radius, orbit_angle, orbit_direction,
    )
    angle = math.radians(angle_degrees)
    relative_x = math.cos(angle) * radius
    relative_y = math.sin(angle) * radius
    return {
        "uuid": uuid.uuid4().hex,
        "name": str(station_type),
        "orbit": {
            "target": {
                "location": location_values,
                "planet": planet_orbit,
                "satellite": satellite_orbit,
            },
            "direction": direction,
            "enterTime": 0.0,
            "enterPosition": [-relative_x, -relative_y],
        },
        "spawnTime": 0.0,
        "position": [relative_x, relative_y],
        # Native .system records keep only per-instance overrides here.
        # displayName, description and icon are resolved from
        # /system_objects.config at runtime; starting empty also matches
        # naturally spawned stations exactly.  The GUI may add explicit
        # overrides after creation.
        "parameters": {},
    }


def celestial_chunk_key(location: Sequence[int]) -> bytes:
    if len(location) < 2:
        raise ValueError("Celestial location requires X and Y")
    chunk_x = int(location[0]) // CELESTIAL_CHUNK_SIZE
    chunk_y = int(location[1]) // CELESTIAL_CHUNK_SIZE
    return hashlib.sha256(struct.pack(">ii", chunk_x, chunk_y)).digest()


def decode_celestial_chunk(compressed: bytes) -> tuple[str, int | None, dict[str, Any], bytes]:
    stream = io.BytesIO(zlib.decompress(compressed))
    name, version, body = read_versioned_json(stream)
    if name != "CelestialChunk" or not isinstance(body, dict):
        raise WorldFormatError(f"Expected CelestialChunk, got {name!r}")
    return name, version, body, stream.read()


def encode_celestial_chunk(
    name: str, version: int | None, body: dict[str, Any], trailing: bytes = b""
) -> bytes:
    stream = io.BytesIO()
    write_versioned_json(stream, name, version, body)
    stream.write(trailing)
    return zlib.compress(stream.getvalue(), level=9)


def set_celestial_chunk_world_name(
    body: dict[str, Any], coordinate: tuple[list[int], int, int], new_name: str
) -> str:
    location, planet_orbit, satellite_orbit = coordinate
    system_objects = body.get("systemObjects")
    if not isinstance(system_objects, list):
        raise WorldFormatError("CelestialChunk 缺少 systemObjects")
    for system_entry in system_objects:
        if (
            not isinstance(system_entry, list)
            or len(system_entry) != 2
            or system_entry[0] != location
            or not isinstance(system_entry[1], list)
        ):
            continue
        for planet_entry in system_entry[1]:
            if (
                not isinstance(planet_entry, list)
                or len(planet_entry) != 2
                or planet_entry[0] != planet_orbit
                or not isinstance(planet_entry[1], dict)
            ):
                continue
            planet = planet_entry[1]
            if satellite_orbit == 0:
                parameters = planet.get("parameters")
            else:
                parameters = None
                satellites = planet.get("satellites")
                if isinstance(satellites, list):
                    for satellite_entry in satellites:
                        if (
                            isinstance(satellite_entry, list)
                            and len(satellite_entry) == 2
                            and satellite_entry[0] == satellite_orbit
                        ):
                            parameters = satellite_entry[1]
                            break
            if not isinstance(parameters, dict):
                break
            old_name = parameters.get("name")
            if not isinstance(old_name, str):
                raise WorldFormatError("目标 CelestialParameters 缺少有效 name")
            parameters["name"] = new_name
            return old_name
    raise WorldFormatError("universe.chunks 中找不到这个 world 的星球坐标")


def _unique_backup_path(source: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = source.with_name(f"{source.name}.bak_before_rename_{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = source.with_name(
            f"{source.name}.bak_before_rename_{stamp}_{suffix}"
        )
        suffix += 1
    return candidate


def _unique_world_edit_backup_path(source: Path) -> Path:
    """Return a permanent same-folder backup name for an in-place world edit."""

    source = Path(source)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = source.with_name(f"{source.name}.bak_before_edit_{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = source.with_name(
            f"{source.name}.bak_before_edit_{stamp}_{suffix}"
        )
        suffix += 1
    return candidate


def rebuild_world_in_place_from_backup(
    source: Path,
    builder: Callable[[Path, Path], _T],
    *,
    keep_backup: bool = True,
) -> tuple[Path | None, _T]:
    """Back up a world, build from that backup, then atomically replace it.

    The permanent backup is created in the source folder before ``builder`` is
    called.  ``builder`` receives the backup as its read-only source and a
    temporary output path.  The original world is replaced only after the
    builder returns successfully and the staged output exists.  When
    ``keep_backup`` is false the same safe backup-first flow is used, but the
    backup is removed after success.  A failed build never replaces the source.
    """

    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"World file was not found: {source}")
    backup = _unique_world_edit_backup_path(source)
    shutil.copy2(source, backup)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=source.name + ".",
            suffix=".rebuilt",
            dir=source.parent,
            delete=False,
        ) as stream:
            staged = Path(stream.name)
        staged.unlink()
        result = builder(backup, staged)
        if not staged.is_file():
            raise RuntimeError("The world builder did not create its staged output")
        os.replace(staged, source)
        staged = None
        retained_backup: Path | None = backup
        if not keep_backup:
            try:
                backup.unlink()
                retained_backup = None
            except OSError:
                # Failure to remove an optional backup is safer than treating
                # a fully verified world replacement as failed.
                retained_backup = backup
        return retained_backup, result
    finally:
        if staged is not None and staged.exists():
            staged.unlink()


def rename_world_in_celestial_database(
    chunks_path: Path, document: dict[str, Any], new_name: str
) -> Path:
    """Synchronize a world name into universe.chunks, with an automatic backup."""

    chunks_path = Path(chunks_path)
    if not chunks_path.is_file():
        raise FileNotFoundError(f"找不到 universe.chunks：{chunks_path}")
    if not isinstance(new_name, str) or not new_name.strip():
        raise ValueError("新星球名称不能为空")
    coordinate = celestial_coordinate(document)
    stream = chunks_path.open("rb")
    try:
        database = BTreeDB5(stream)
        if (
            database.identifier != CELESTIAL_IDENTIFIER
            or database.key_size != CELESTIAL_KEY_SIZE
        ):
            raise WorldFormatError(
                f"Expected Celestial2/32, got "
                f"{database.identifier!r}/{database.key_size}"
            )
        records = list(database.records())
        block_size = database.block_size
    finally:
        stream.close()

    target_key = celestial_chunk_key(coordinate[0])
    record_map = dict(records)
    if target_key not in record_map:
        # A nonstandard celestial chunk size is rare, but scanning provides a
        # safe fallback without asking for assets or assuming another value.
        target_key = b""
        for key, compressed in records:
            try:
                _name, _version, body, _trailing = decode_celestial_chunk(compressed)
                locations = [
                    entry[0]
                    for entry in body.get("systemObjects", [])
                    if isinstance(entry, list) and len(entry) == 2
                ]
            except Exception:
                continue
            if coordinate[0] in locations:
                target_key = key
                break
        if not target_key:
            raise WorldFormatError("universe.chunks 中没有包含目标星系的 CelestialChunk")

    name, version, body, trailing = decode_celestial_chunk(record_map[target_key])
    set_celestial_chunk_world_name(body, coordinate, new_name)
    replacement = encode_celestial_chunk(name, version, body, trailing)
    updated_records = [
        (key, replacement if key == target_key else value)
        for key, value in records
    ]

    temp_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=chunks_path.name + ".",
            suffix=".tmp",
            dir=chunks_path.parent,
            delete=False,
        ) as output:
            temp_file = Path(output.name)
            writer = BTreeDB5Writer(
                block_size, CELESTIAL_IDENTIFIER, CELESTIAL_KEY_SIZE
            )
            writer.write(output, updated_records)
            output.flush()
            os.fsync(output.fileno())

        verify_stream = temp_file.open("rb")
        try:
            verification = BTreeDB5(verify_stream)
            actual = dict(verification.records())
            if actual != dict(updated_records):
                raise WorldFormatError("重建的 universe.chunks 未通过逐记录校验")
            _n, _v, verified_body, _t = decode_celestial_chunk(actual[target_key])
            verified_name = set_celestial_chunk_world_name(
                verified_body, coordinate, new_name
            )
            if verified_name != new_name:
                raise WorldFormatError("重建的 universe.chunks 名称校验失败")
        finally:
            verify_stream.close()

        backup = _unique_backup_path(chunks_path)
        shutil.copy2(chunks_path, backup)
        shutil.copystat(chunks_path, temp_file)
        os.replace(temp_file, chunks_path)
        return backup
    finally:
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def _load_celestial_database_records(
    chunks_path: Path,
) -> tuple[int, list[tuple[bytes, bytes]]]:
    """Load every Celestial2 record and return the original block size."""

    chunks_path = Path(chunks_path)
    if not chunks_path.is_file():
        raise FileNotFoundError(f"找不到 universe.chunks：{chunks_path}")
    stream = chunks_path.open("rb")
    try:
        database = BTreeDB5(stream)
        if (
            database.identifier != CELESTIAL_IDENTIFIER
            or database.key_size != CELESTIAL_KEY_SIZE
        ):
            raise WorldFormatError(
                f"Expected Celestial2/32, got "
                f"{database.identifier!r}/{database.key_size}"
            )
        return database.block_size, list(database.records())
    finally:
        stream.close()


def _celestial_system_entry(
    body: dict[str, Any], location: Sequence[int]
) -> tuple[dict[str, Any], list[Any]]:
    location_list = [int(value) for value in location]
    system_parameters = body.get("systemParameters")
    system_objects = body.get("systemObjects")
    if not isinstance(system_parameters, list) or not isinstance(system_objects, list):
        raise WorldFormatError("CelestialChunk 缺少 systemParameters/systemObjects")
    parameters = next(
        (
            entry[1]
            for entry in system_parameters
            if isinstance(entry, list)
            and len(entry) == 2
            and entry[0] == location_list
            and isinstance(entry[1], dict)
        ),
        None,
    )
    objects = next(
        (
            entry[1]
            for entry in system_objects
            if isinstance(entry, list)
            and len(entry) == 2
            and entry[0] == location_list
            and isinstance(entry[1], list)
        ),
        None,
    )
    if parameters is None or objects is None:
        raise WorldFormatError(
            "CelestialChunk 中找不到目标星系：" + ":".join(map(str, location_list))
        )
    return parameters, objects


def _find_celestial_system_chunk(
    records: Sequence[tuple[bytes, bytes]], location: Sequence[int]
) -> tuple[bytes, str, int | None, dict[str, Any], bytes]:
    location_list = [int(value) for value in location]
    record_map = dict(records)
    preferred_key = celestial_chunk_key(location_list)
    candidates: list[tuple[bytes, bytes]] = []
    if preferred_key in record_map:
        candidates.append((preferred_key, record_map[preferred_key]))
    candidates.extend(
        (key, value) for key, value in records if key != preferred_key
    )
    for key, compressed in candidates:
        try:
            name, version, body, trailing = decode_celestial_chunk(compressed)
            _celestial_system_entry(body, location_list)
        except Exception:
            continue
        return key, name, version, body, trailing
    raise WorldFormatError(
        "universe.chunks 中找不到星系：" + ":".join(map(str, location_list))
    )


def list_celestial_systems(chunks_path: Path) -> list[dict[str, Any]]:
    """Return a compact catalog of every saved celestial system."""

    _block_size, records = _load_celestial_database_records(Path(chunks_path))
    result: list[dict[str, Any]] = []
    for _key, compressed in records:
        try:
            name, _version, body, _trailing = decode_celestial_chunk(compressed)
        except Exception:
            continue
        if name != "CelestialChunk":
            continue
        object_counts: dict[tuple[int, int, int], tuple[int, int]] = {}
        for entry in body.get("systemObjects", []):
            if not (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], list)
                and len(entry[0]) == 3
                and isinstance(entry[1], list)
            ):
                continue
            planet_count = len(entry[1])
            satellite_count = 0
            for planet_entry in entry[1]:
                if (
                    isinstance(planet_entry, list)
                    and len(planet_entry) == 2
                    and isinstance(planet_entry[1], dict)
                    and isinstance(planet_entry[1].get("satellites"), list)
                ):
                    satellite_count += len(planet_entry[1]["satellites"])
            object_counts[tuple(int(value) for value in entry[0])] = (
                planet_count,
                satellite_count,
            )
        for entry in body.get("systemParameters", []):
            if not (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], list)
                and len(entry[0]) == 3
                and isinstance(entry[1], dict)
            ):
                continue
            location = [int(value) for value in entry[0]]
            parameters = entry[1]
            raw_parameters = parameters.get("parameters")
            raw_parameters = raw_parameters if isinstance(raw_parameters, dict) else {}
            planet_count, satellite_count = object_counts.get(tuple(location), (0, 0))
            result.append(
                {
                    "location": location,
                    "name": str(parameters.get("name", "")),
                    "typeName": str(raw_parameters.get("typeName", "")),
                    "planetCount": planet_count,
                    "satelliteCount": satellite_count,
                }
            )
    result.sort(key=lambda item: (item["name"].casefold(), item["location"]))
    return result


_SYSTEM_FILENAME_RE = re.compile(r"^(-?\d+)_(-?\d+)_(-?\d+)\.system$", re.IGNORECASE)


def generated_system_locations(universe_folder: Path) -> list[list[int]]:
    """Return coordinates represented by generated ``*.system`` files."""

    universe_folder = Path(universe_folder)
    if not universe_folder.is_dir():
        raise ValueError(f"Universe folder does not exist: {universe_folder}")
    locations: list[list[int]] = []
    for path in universe_folder.glob("*.system"):
        match = _SYSTEM_FILENAME_RE.fullmatch(path.name)
        if match:
            locations.append([int(value) for value in match.groups()])
    locations.sort()
    return locations


def list_generated_celestial_systems(universe_folder: Path) -> list[dict[str, Any]]:
    """List generated systems in a universe folder without scanning every chunk.

    A normal universe may contain thousands of cached CelestialChunk entries but
    only a relatively small number of generated ``*.system`` files.  Resolving
    those coordinates through the B-tree index keeps the GUI selector useful and
    much faster.  A coordinate that is absent from the database is skipped.
    """

    universe_folder = Path(universe_folder)
    chunks_path = universe_folder / "universe.chunks"
    if not chunks_path.is_file():
        raise ValueError(f"universe.chunks was not found in: {universe_folder}")
    result: list[dict[str, Any]] = []
    with chunks_path.open("rb") as stream:
        database = BTreeDB5(stream)
        if database.identifier != CELESTIAL_IDENTIFIER or database.key_size != 32:
            raise WorldFormatError(
                f"Expected {CELESTIAL_IDENTIFIER}/32, got "
                f"{database.identifier}/{database.key_size}"
            )
        for location in generated_system_locations(universe_folder):
            try:
                compressed = database.get(celestial_chunk_key(location))
                name, _version, body, _trailing = decode_celestial_chunk(compressed)
                if name != "CelestialChunk":
                    continue
                parameters, planets = _celestial_system_entry(body, location)
            except Exception:
                continue
            raw = parameters.get("parameters")
            raw = raw if isinstance(raw, dict) else {}
            satellite_count = sum(
                len(entry[1].get("satellites") or [])
                for entry in planets
                if isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[1], dict)
            )
            result.append(
                {
                    "location": list(location),
                    "name": str(parameters.get("name", "")),
                    "typeName": str(raw.get("typeName", "")),
                    "planetCount": len(planets),
                    "satelliteCount": satellite_count,
                }
            )
    result.sort(key=lambda item: (item["name"].casefold(), item["location"]))
    return result


def celestial_layer_groups(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe every ordered biome group stored for one celestial body."""

    visitable = parameters.get("visitableParameters")
    if not isinstance(visitable, dict):
        return []
    layers: list[tuple[str, str, Any]] = [
        ("surface", "Surface", visitable.get("surfaceLayer")),
        ("subsurface", "Subsurface", visitable.get("subsurfaceLayer")),
    ]
    underground = visitable.get("undergroundLayers")
    if isinstance(underground, list):
        layers.extend(
            (f"underground:{index}", f"Underground {index + 1}", layer)
            for index, layer in enumerate(underground)
        )
    layers.extend(
        [
            ("core", "Core", visitable.get("coreLayer")),
            ("atmosphere", "Atmosphere", visitable.get("atmosphereLayer")),
            ("space", "Space", visitable.get("spaceLayer")),
        ]
    )
    result: list[dict[str, Any]] = []
    for layer_id, label, layer in layers:
        if not isinstance(layer, dict):
            continue
        for group, primary_key, secondary_key in (
            ("regions", "primaryRegion", "secondaryRegions"),
            ("subregions", "primarySubRegion", "secondarySubRegions"),
        ):
            primary = layer.get(primary_key)
            secondary = layer.get(secondary_key)
            entries: list[dict[str, Any]] = []
            if isinstance(primary, dict):
                entries.append(primary)
            if isinstance(secondary, list):
                entries.extend(entry for entry in secondary if isinstance(entry, dict))
            if entries:
                result.append(
                    {
                        "layerId": layer_id,
                        "layerLabel": label,
                        "group": group,
                        "biomes": [str(entry.get("biome", "")) for entry in entries],
                    }
                )
    return result


def _celestial_layer(parameters: dict[str, Any], layer_id: str) -> dict[str, Any]:
    visitable = parameters.get("visitableParameters")
    if not isinstance(visitable, dict):
        raise ValueError("This celestial body has no visitableParameters")
    if layer_id.startswith("underground:"):
        try:
            index = int(layer_id.split(":", 1)[1])
            layer = visitable["undergroundLayers"][index]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"Unknown celestial biome layer: {layer_id}") from exc
    else:
        key = {
            "surface": "surfaceLayer",
            "subsurface": "subsurfaceLayer",
            "core": "coreLayer",
            "atmosphere": "atmosphereLayer",
            "space": "spaceLayer",
        }.get(layer_id)
        if key is None:
            raise ValueError(f"Unknown celestial biome layer: {layer_id}")
        layer = visitable.get(key)
    if not isinstance(layer, dict):
        raise ValueError(f"Celestial biome layer is missing: {layer_id}")
    return layer


def celestial_biome_region_objects(
    parameters: dict[str, Any], layer_id: str, group: str
) -> list[dict[str, Any]]:
    """Return the live ordered region objects for a layer/group."""

    layer = _celestial_layer(parameters, layer_id)
    if group == "regions":
        primary_key, secondary_key = "primaryRegion", "secondaryRegions"
    elif group == "subregions":
        primary_key, secondary_key = "primarySubRegion", "secondarySubRegions"
    else:
        raise ValueError(f"Unknown celestial biome group: {group}")
    result: list[dict[str, Any]] = []
    primary = layer.get(primary_key)
    if isinstance(primary, dict):
        result.append(primary)
    secondary = layer.get(secondary_key)
    if isinstance(secondary, list):
        result.extend(entry for entry in secondary if isinstance(entry, dict))
    return result


def _new_celestial_region(
    compiler: "AssetBiomeCompiler",
    biome_name: str,
    layer: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(_stable_asset_seed(seed, biome_name, "celestial-region"))
    recipe = compiler.region_recipe(biome_name, rng)
    base_height = int(layer.get("layerBaseHeight", layer.get("layerMinHeight", 0)))
    return {
        "bgOreSelector": recipe.get("bgOreSelector") or "normalBackgroundVeins",
        "bgCaveSelector": recipe.get("bgCaveSelector") or "empty",
        "biome": biome_name,
        "fgOreSelector": recipe.get("fgOreSelector") or "normalVeins",
        "fillMicrodungeons": bool(recipe.get("fillMicrodungeons", False)),
        "subBlockSelector": recipe.get("subBlockSelector") or "largeClumps",
        "blockSelector": recipe.get("blockSelector") or "solid",
        "encloseLiquids": bool(recipe.get("encloseLiquids", False)),
        "fgCaveSelector": recipe.get("fgCaveSelector") or "normalCaves",
        "oceanLiquid": int(recipe.get("oceanLiquid", 0)),
        "oceanLiquidLevel": (
            base_height + int(recipe.get("oceanLevelOffset", 0))
            if recipe.get("oceanLiquid")
            else 0
        ),
        "caveLiquid": int(recipe.get("caveLiquid", 0)),
        "caveLiquidSeedDensity": float(recipe.get("caveLiquidSeedDensity", 0.0)),
    }


def repair_celestial_region_pairings(parameters: dict[str, Any]) -> int:
    """Repair the one-to-one main/sub-region invariant used by Starbound.

    Vanilla ``WorldLayout::addLayer`` indexes ``secondarySubRegions[i]`` for
    every ``secondaryRegions[i]`` without checking the second list's length.
    A malformed celestial record therefore crashes the game with an access
    violation instead of producing a useful configuration error.  Missing
    partners are safely initialized from the corresponding main region; extra
    orphaned sub-regions are discarded.
    """

    visitable = parameters.get("visitableParameters")
    if not isinstance(visitable, dict):
        return 0
    layers: list[Any] = [
        visitable.get("spaceLayer"),
        visitable.get("atmosphereLayer"),
        visitable.get("surfaceLayer"),
        visitable.get("subsurfaceLayer"),
        visitable.get("coreLayer"),
    ]
    underground = visitable.get("undergroundLayers")
    if isinstance(underground, list):
        layers.extend(underground)

    repairs = 0
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        primary = layer.get("primaryRegion")
        primary_sub = layer.get("primarySubRegion")
        if isinstance(primary, dict) and not isinstance(primary_sub, dict):
            layer["primarySubRegion"] = deep_copy_json(primary)
            repairs += 1

        secondary = layer.get("secondaryRegions")
        secondary = (
            [entry for entry in secondary if isinstance(entry, dict)]
            if isinstance(secondary, list)
            else []
        )
        secondary_sub = layer.get("secondarySubRegions")
        secondary_sub = (
            [entry for entry in secondary_sub if isinstance(entry, dict)]
            if isinstance(secondary_sub, list)
            else []
        )
        repaired_sub = secondary_sub[: len(secondary)]
        for index in range(len(repaired_sub), len(secondary)):
            repaired_sub.append(deep_copy_json(secondary[index]))
        if layer.get("secondaryRegions") != secondary:
            layer["secondaryRegions"] = secondary
            repairs += 1
        if layer.get("secondarySubRegions") != repaired_sub:
            layer["secondarySubRegions"] = repaired_sub
            repairs += 1
    return repairs


def validate_celestial_region_pairings(parameters: dict[str, Any]) -> None:
    """Reject terrestrial region arrays that vanilla would index unsafely."""

    visitable = parameters.get("visitableParameters")
    if not isinstance(visitable, dict):
        return
    named_layers: list[tuple[str, Any]] = [
        ("Space", visitable.get("spaceLayer")),
        ("Atmosphere", visitable.get("atmosphereLayer")),
        ("Surface", visitable.get("surfaceLayer")),
        ("Subsurface", visitable.get("subsurfaceLayer")),
        ("Core", visitable.get("coreLayer")),
    ]
    underground = visitable.get("undergroundLayers")
    if isinstance(underground, list):
        named_layers.extend(
            (f"Underground {index + 1}", layer)
            for index, layer in enumerate(underground)
        )
    for label, layer in named_layers:
        if not isinstance(layer, dict):
            continue
        if not isinstance(layer.get("primaryRegion"), dict):
            raise ValueError(f"{label} is missing primaryRegion")
        if not isinstance(layer.get("primarySubRegion"), dict):
            raise ValueError(f"{label} is missing primarySubRegion")
        secondary = layer.get("secondaryRegions")
        secondary_sub = layer.get("secondarySubRegions")
        secondary_count = len(secondary) if isinstance(secondary, list) else 0
        sub_count = len(secondary_sub) if isinstance(secondary_sub, list) else 0
        if secondary_count != sub_count:
            raise ValueError(
                f"{label} has {secondary_count} secondary region(s) but "
                f"{sub_count} secondary sub-region(s); Starbound requires "
                "a one-to-one pair"
            )


def normalize_celestial_subregion_names(
    main_biomes: Sequence[str], sub_biomes: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """Match a user-edited sub-region list to its main-region list.

    Starbound indexes main and sub regions in lockstep.  Missing trailing sub
    entries therefore inherit the corresponding main biome; extra trailing
    entries cannot be addressed by the engine and are removed.  The two
    returned change lists are suitable for an explicit GUI notification.
    """

    main = [str(name).strip() for name in main_biomes]
    sub = [str(name).strip() for name in sub_biomes]
    if not main or any(not name for name in main):
        raise ValueError("A celestial main-region group must contain valid biomes")
    if any(not name for name in sub):
        raise ValueError("A celestial sub-region group contains an empty biome")
    target_count = len(main)
    normalized = list(sub[:target_count])
    added = list(main[len(normalized) :])
    normalized.extend(added)
    removed = list(sub[target_count:])
    return normalized, added, removed


def uniform_ocean_subregion_locks(
    main_biomes: Sequence[str],
    settings: Sequence[dict[str, Any] | None],
) -> dict[int, str]:
    """Return hard Sub-region slots for uniform surface oceans.

    The slot is derived from the ocean's current Main-region index.  It is not
    a permanent numeric position: inserting, deleting or moving a Main region
    moves the lock with that ocean entry.  A setting is accepted only when its
    declared surface biome matches the ocean currently occupying that slot.
    """

    locks: dict[int, str] = {}
    for index, raw_name in enumerate(main_biomes):
        if index >= len(settings):
            break
        configuration = settings[index]
        if not isinstance(configuration, dict) or not bool(
            configuration.get("disableSubRegions", True)
        ):
            continue
        name = str(raw_name).strip()
        pair = ocean_biome_pair(name)
        if pair is None or pair[1] != name.casefold():
            continue
        configured_name = str(
            configuration.get("surfaceBiome", name)
        ).strip().casefold()
        if configured_name != name.casefold():
            continue
        locks[index] = name
    return locks


def normalize_uniform_ocean_subregion_names(
    main_biomes: Sequence[str],
    sub_biomes: Sequence[str],
    settings: Sequence[dict[str, Any] | None],
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    """Normalize Sub-regions and enforce uniform-ocean positional locks.

    In addition to Starbound's one-to-one Main/Sub array length invariant,
    every ocean configured with ``disableSubRegions`` owns its corresponding
    Sub-region slot.  A misplaced automatic copy of that ocean under a land
    Main region is replaced with the land Main biome, so reordering or deleting
    Sub entries cannot leave a second, stale ocean behind.

    The fourth return value describes forced replacements as dictionaries with
    zero-based ``index``, ``biome`` and ``replaced`` fields.
    """

    main = [str(name).strip() for name in main_biomes]
    normalized, added, removed = normalize_celestial_subregion_names(
        main, sub_biomes
    )
    locks = uniform_ocean_subregion_locks(main, settings)
    locked_biomes = {name.casefold() for name in locks.values()}
    forced: list[dict[str, Any]] = []

    # Remove copies that used to be a locked placeholder but have been shifted
    # under a different Main region.  A separate Main ocean of the same type
    # remains valid and is not removed.
    for index, current in enumerate(list(normalized)):
        if index in locks:
            continue
        if (
            current.casefold() in locked_biomes
            and main[index].casefold() != current.casefold()
        ):
            replacement = main[index]
            normalized[index] = replacement
            forced.append(
                {
                    "index": index,
                    "biome": replacement,
                    "replaced": current,
                    "reason": "misplacedUniformOcean",
                }
            )

    # Restore every lock from its corresponding Main recipe.  This makes the
    # result deterministic after arbitrary Sub-region insert/delete/move edits.
    for index, expected in locks.items():
        current = normalized[index]
        if current.casefold() == expected.casefold():
            normalized[index] = expected
            continue
        normalized[index] = expected
        forced.append(
            {
                "index": index,
                "biome": expected,
                "replaced": current,
                "reason": "uniformOceanLock",
            }
        )
    return normalized, added, removed, forced


def set_celestial_biome_order(
    parameters: dict[str, Any],
    layer_id: str,
    group: str,
    biome_names: Sequence[str],
    assets_folder: Path | None = None,
) -> None:
    """Set one ordered celestial biome group, compiling any new names.

    Existing region objects are moved intact, preserving their exact selectors,
    liquids and other parameters.  A name that is new to the group requires
    assets so that a complete region recipe can be constructed.
    """

    names = [str(name).strip() for name in biome_names]
    if not names or any(not name for name in names):
        raise ValueError("A celestial biome group must contain at least one biome")
    first_ocean_pair = ocean_biome_pair(names[0])
    requested_ocean_surface = (
        first_ocean_pair[1]
        if layer_id == "surface"
        and group == "regions"
        and first_ocean_pair is not None
        and names[0].casefold() == first_ocean_pair[1]
        and len(names) == 1
        else None
    )
    if requested_ocean_surface is not None and assets_folder is None:
        raise ValueError(
            "Configuring the matching ocean-floor layer requires assets"
        )
    repair_celestial_region_pairings(parameters)
    layer = _celestial_layer(parameters, layer_id)
    main_regions = celestial_biome_region_objects(parameters, layer_id, "regions")
    sub_regions = celestial_biome_region_objects(parameters, layer_id, "subregions")
    if group == "regions":
        existing_pairs = list(zip(main_regions, sub_regions))
        by_name: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for region, subregion in existing_pairs:
            by_name.setdefault(
                str(region.get("biome", "")).casefold(), []
            ).append((region, subregion))
    else:
        if len(names) != len(main_regions):
            raise ValueError(
                "The sub-region list must contain exactly one entry for every "
                f"main region ({len(main_regions)} required)"
            )
        by_name = {}
        for region in sub_regions:
            by_name.setdefault(
                str(region.get("biome", "")).casefold(), []
            ).append(region)

    remaining = {key: len(pool) for key, pool in by_name.items()}
    missing: list[str] = []
    for index, name in enumerate(names):
        key = name.casefold()
        if remaining.get(key, 0):
            remaining[key] -= 1
        elif (
            group == "subregions"
            and index < len(main_regions)
            and str(main_regions[index].get("biome", "")).casefold() == key
        ):
            # The GUI uses the corresponding main biome when a trailing
            # sub-region is omitted.  Its complete region recipe is already
            # present, so no external assets are required for this fallback.
            continue
        else:
            missing.append(name)
    compiler: AssetBiomeCompiler | None = None
    if missing:
        if assets_folder is None:
            raise ValueError(
                "Adding a new biome requires an assets folder or packed.pak"
            )
        compiler = AssetBiomeCompiler(Path(assets_folder))
        unknown = [name for name in missing if name not in compiler.catalog["biomes"]]
        if unknown:
            raise ValueError("Biome not found in assets: " + ", ".join(unknown))
    seed = int(parameters.get("seed", 0))
    ordered: list[dict[str, Any]] = []
    ordered_subregions: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        pool = by_name.get(name.casefold()) or []
        if pool:
            selected = pool.pop(0)
        elif (
            group == "subregions"
            and index < len(main_regions)
            and str(main_regions[index].get("biome", "")).casefold()
            == name.casefold()
        ):
            selected = deep_copy_json(main_regions[index])
        else:
            assert compiler is not None
            selected = _new_celestial_region(compiler, name, layer, seed)
        if group == "regions":
            if isinstance(selected, tuple):
                region, subregion = selected
            else:
                region = selected
                # Using the same complete region recipe is a valid sub-region
                # fallback and, critically, maintains the engine's required
                # one-to-one array shape for newly added biomes.
                subregion = deep_copy_json(region)
            ordered.append(region)
            ordered_subregions.append(subregion)
        else:
            assert isinstance(selected, dict)
            ordered.append(selected)
    if group == "regions":
        primary_key, secondary_key = "primaryRegion", "secondaryRegions"
    else:
        primary_key, secondary_key = "primarySubRegion", "secondarySubRegions"
    layer[primary_key] = ordered[0]
    layer[secondary_key] = ordered[1:]
    if group == "regions":
        layer["primarySubRegion"] = ordered_subregions[0]
        layer["secondarySubRegions"] = ordered_subregions[1:]
    validate_celestial_region_pairings(parameters)
    if layer_id == "surface" and group == "regions":
        visitable = parameters["visitableParameters"]
        visitable["primaryBiome"] = names[0]
        if requested_ocean_surface is not None:
            raw = parameters.get("parameters")
            size_name = (
                str(raw.get("worldSize", "medium"))
                if isinstance(raw, dict)
                else "medium"
            )
            resources = _celestial_biome_resources(Path(assets_folder))
            compiled = _build_terrestrial_world_parameters(
                resources,
                requested_ocean_surface,
                size_name,
                int(parameters.get("seed", 0)),
                surface_only_primary=True,
            )
            # Use the vanilla/mod planet type's complete paired layers.  This
            # is what supplies oceanfloor/toxicoceanfloor/arcticoceanfloor/
            # magmaoceanfloor, the seafloor selector, ocean liquid and ocean
            # dungeons instead of producing an empty column of liquid.
            visitable["surfaceLayer"] = compiled["surfaceLayer"]
            visitable["subsurfaceLayer"] = compiled["subsurfaceLayer"]
            visitable["primaryBiome"] = requested_ocean_surface
            visitable["surfaceLiquid"] = compiled.get("surfaceLiquid", 0)
            if isinstance(raw, dict):
                raw["terrestrialType"] = [requested_ocean_surface]


CELESTIAL_BODY_PRESETS = (
    "planet",
    "gasgiant",
    "asteroidfield",
    "ancientgateway",
    "outpost",
)

# Starbound's saved celestial tree accepts these world-bearing body records in
# a planet's ``satellites`` list.  Outpost remains excluded because this tool
# represents it as an independent FloatingDungeon location rather than an
# ordinary orbiting celestial body.
CELESTIAL_SATELLITE_BODY_PRESETS = frozenset(
    {"planet", "gasgiant", "asteroidfield", "ancientgateway"}
)


def celestial_body_preset_supports_satellite(preset: str) -> bool:
    """Return whether Tool 05 may safely create this preset as a moon."""

    normalized = str(preset).strip().lower()
    if normalized == "garden":
        normalized = "planet"
    return normalized in CELESTIAL_SATELLITE_BODY_PRESETS


def compact_object_array_json(value: Any) -> str:
    """Pretty-print an object array with its outer brackets kept compact."""

    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if rendered.startswith("[\n  {") and rendered.endswith("\n  }\n]"):
        rendered = "[{" + rendered[len("[\n  {") : -len("\n  }\n]")] + "\n  }]"
    return rendered


def _json_merge_objects(*values: Any) -> dict[str, Any]:
    """Recursively merge Starbound configuration objects.

    Starbound's ``jsonMerge`` recursively combines object members while a
    scalar or array in a later stage replaces the earlier value.  Celestial
    and terrestrial world generation both rely on this three-stage merge.
    """

    result: dict[str, Any] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        for key, item in value.items():
            if isinstance(item, dict) and isinstance(result.get(key), dict):
                result[key] = _json_merge_objects(result[key], item)
            else:
                result[key] = deep_copy_json(item)
    return result


def _binned_asset_choice(value: Any, level: float) -> Any:
    """Return the highest Starbound binned-choice entry not above ``level``."""

    if not isinstance(value, list) or not value:
        return value
    candidates: list[tuple[float, Any]] = []
    for entry in value:
        if (
            isinstance(entry, list)
            and len(entry) == 2
            and isinstance(entry[0], (int, float))
        ):
            candidates.append((float(entry[0]), entry[1]))
    if not candidates:
        return value
    candidates.sort(key=lambda entry: entry[0])
    eligible = [entry for entry in candidates if entry[0] <= float(level)]
    return deep_copy_json((eligible[-1] if eligible else candidates[0])[1])


def _range_pair(value: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, list) and len(value) == 2:
        return float(value[0]), float(value[1])
    return default


def _random_range(value: Any, rng: random.Random, default: float) -> float:
    low, high = _range_pair(value, (default, default))
    return rng.uniform(low, high)


def _random_list_item(value: Any, rng: random.Random, default: Any = None) -> Any:
    if isinstance(value, list) and value:
        return deep_copy_json(rng.choice(value))
    return deep_copy_json(default)


def _weather_pool_from_biome(
    reader: StarboundAssetReader,
    biome: dict[str, Any],
    threat_level: float,
    rng: random.Random,
) -> list[dict[str, Any]]:
    references = _binned_asset_choice(biome.get("weather", []), threat_level)
    if not isinstance(references, list) or not references:
        return []
    reference = rng.choice(references)
    if not isinstance(reference, str):
        return []
    pool = reader.json(reference)
    result: list[dict[str, Any]] = []
    if isinstance(pool, list):
        for entry in pool:
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], (int, float))
                and isinstance(entry[1], str)
            ):
                result.append({"weight": float(entry[0]), "item": entry[1]})
    return result


class _CelestialBiomeResources:
    """Small asset subset needed to compile celestial world parameters."""

    def __init__(self, assets_folder: Path):
        self.reader = StarboundAssetReader(Path(assets_folder))
        self.catalog = load_asset_biome_catalog(Path(assets_folder))
        terrestrial = self.reader.json("/terrestrial_worlds.config")
        self.region_defaults = terrestrial.get("regionDefaults", {})
        self.region_types = terrestrial.get("regionTypes", {})
        self.liquid_ids: dict[str, int] = {}
        for asset_path in self.reader.paths(".liquid"):
            try:
                document = self.reader.json(asset_path)
            except Exception:
                continue
            if not isinstance(document, dict):
                continue
            name = document.get("name")
            identifier = document.get("liquidId")
            if isinstance(name, str) and isinstance(identifier, int):
                self.liquid_ids[name.casefold()] = identifier

    def _liquid(self, name: str) -> int:
        try:
            return self.liquid_ids[name.casefold()]
        except KeyError as exc:
            raise ValueError(
                f"Biome region references unknown liquid {name!r}"
            ) from exc


_CELESTIAL_RESOURCE_CACHE: dict[str, _CelestialBiomeResources] = {}


def _celestial_biome_resources(assets_folder: Path) -> _CelestialBiomeResources:
    source = str(Path(assets_folder).resolve()).casefold()
    resources = _CELESTIAL_RESOURCE_CACHE.get(source)
    if resources is None:
        resources = _CelestialBiomeResources(Path(assets_folder))
        _CELESTIAL_RESOURCE_CACHE[source] = resources
    return resources


def _terrestrial_region_from_config(
    compiler: "AssetBiomeCompiler | _CelestialBiomeResources",
    config: dict[str, Any],
    threat_level: float,
    layer_name: str,
    base_height: int,
    rng: random.Random,
) -> dict[str, Any]:
    biome_choices = _binned_asset_choice(config.get("biome", []), threat_level)
    biome = _random_list_item(biome_choices, rng, "")
    if not isinstance(biome, str) or not biome:
        raise ValueError(f"Layer {layer_name!r} has no usable biome choice")

    def required_choice(key: str, fallback: str) -> str:
        value = _random_list_item(config.get(key), rng, fallback)
        return str(value if value is not None else fallback)

    cave_name = _random_list_item(config.get("caveLiquid"), rng)
    ocean_name = _random_list_item(config.get("oceanLiquid"), rng)
    if cave_name:
        density_low, density_high = _range_pair(
            config.get("caveLiquidSeedDensityRange"), (1.0, 1.5)
        )
        cave_liquid = compiler._liquid(str(cave_name))
        cave_density = rng.uniform(density_low, density_high)
    else:
        cave_liquid = 0
        cave_density = 0.0
    if ocean_name:
        ocean_liquid = compiler._liquid(str(ocean_name))
        ocean_level = int(base_height + int(config.get("oceanLevelOffset", 0)))
    else:
        ocean_liquid = 0
        ocean_level = 0
    return {
        "biome": biome,
        "blockSelector": required_choice("blockSelector", "solid"),
        "fgCaveSelector": required_choice("fgCaveSelector", "normalCaves"),
        "bgCaveSelector": required_choice("bgCaveSelector", "backgroundCaves"),
        "fgOreSelector": required_choice("fgOreSelector", "normalVeins"),
        "bgOreSelector": required_choice(
            "bgOreSelector", "normalBackgroundVeins"
        ),
        "subBlockSelector": required_choice("subBlockSelector", "largeClumps"),
        "caveLiquid": cave_liquid,
        "caveLiquidSeedDensity": float(cave_density),
        "oceanLiquid": ocean_liquid,
        "oceanLiquidLevel": ocean_level,
        "encloseLiquids": bool(config.get("encloseLiquids", False)),
        "fillMicrodungeons": bool(config.get("fillMicrodungeons", False)),
    }


def _terrestrial_layer_from_config(
    compiler: "AssetBiomeCompiler | _CelestialBiomeResources",
    config: dict[str, Any],
    layer_name: str,
    threat_level: float,
    seed: int,
) -> dict[str, Any] | None:
    layers = config.get("layers", {})
    if not isinstance(layers, dict) or layer_name not in layers:
        return None
    layer_config = _json_merge_objects(config.get("layerDefaults"), layers[layer_name])
    if not layer_config or not bool(layer_config.get("enabled", True)):
        return None
    rng = random.Random(_stable_asset_seed(seed, layer_name, "celestial-layer"))
    minimum = int(float(layer_config.get("layerLevel", 0)))
    base_height = int(float(layer_config.get("baseHeight", 0)))

    primary_names = layer_config.get("primaryRegion", [])
    primary_name = _random_list_item(primary_names, rng)
    if not isinstance(primary_name, str) or not primary_name:
        raise ValueError(f"Layer {layer_name!r} has no primaryRegion")
    primary_config = _json_merge_objects(
        compiler.region_defaults, compiler.region_types.get(primary_name)
    )
    primary = _terrestrial_region_from_config(
        compiler, primary_config, threat_level, layer_name, base_height, rng
    )

    primary_sub_names = primary_config.get("subRegion", [])
    if isinstance(primary_sub_names, list) and primary_sub_names:
        primary_sub_name = str(rng.choice(primary_sub_names))
        primary_sub_config = _json_merge_objects(
            compiler.region_defaults, compiler.region_types.get(primary_sub_name)
        )
    else:
        primary_sub_config = primary_config
    primary_sub = _terrestrial_region_from_config(
        compiler, primary_sub_config, threat_level, layer_name, base_height, rng
    )

    count_low, count_high = _range_pair(
        layer_config.get("secondaryRegionCount"), (0, 0)
    )
    count = rng.randint(int(count_low), int(count_high))
    secondary_names = [
        str(name)
        for name in layer_config.get("secondaryRegions", [])
        if isinstance(name, str)
    ]
    rng.shuffle(secondary_names)
    secondary_regions: list[dict[str, Any]] = []
    secondary_subregions: list[dict[str, Any]] = []
    for secondary_name in secondary_names[:count]:
        region_config = _json_merge_objects(
            compiler.region_defaults, compiler.region_types.get(secondary_name)
        )
        secondary_regions.append(
            _terrestrial_region_from_config(
                compiler,
                region_config,
                threat_level,
                layer_name,
                base_height,
                rng,
            )
        )
        sub_names = region_config.get("subRegion", [])
        if isinstance(sub_names, list) and sub_names:
            sub_name = str(rng.choice(sub_names))
            sub_config = _json_merge_objects(
                compiler.region_defaults, compiler.region_types.get(sub_name)
            )
        else:
            sub_config = region_config
        secondary_subregions.append(
            _terrestrial_region_from_config(
                compiler,
                sub_config,
                threat_level,
                layer_name,
                base_height,
                rng,
            )
        )

    dungeon_low, dungeon_high = _range_pair(
        layer_config.get("dungeonCountRange"), (0, 0)
    )
    dungeon_count = rng.randint(int(dungeon_low), int(dungeon_high))
    dungeons = _weighted_unique_choices(
        layer_config.get("dungeons", []), dungeon_count, rng
    )
    return {
        "layerMinHeight": minimum,
        "layerBaseHeight": base_height,
        "dungeons": dungeons,
        "dungeonXVariance": int(layer_config.get("dungeonXVariance", 0)),
        "primaryRegion": primary,
        "primarySubRegion": primary_sub,
        "secondaryRegions": secondary_regions,
        "secondarySubRegions": secondary_subregions,
        "secondaryRegionSizeRange": list(
            _range_pair(layer_config.get("secondaryRegionSize"), (0.5, 0.8))
        ),
        "subRegionSizeRange": list(
            _range_pair(layer_config.get("subRegionSize"), (0.25, 0.4))
        ),
    }


def _build_terrestrial_world_parameters(
    compiler: "AssetBiomeCompiler | _CelestialBiomeResources",
    type_name: str,
    size_name: str,
    seed: int,
    *,
    surface_only_primary: bool = False,
) -> dict[str, Any]:
    terrestrial = compiler.reader.json("/terrestrial_worlds.config")
    sizes = terrestrial.get("planetSizes", {}) if isinstance(terrestrial, dict) else {}
    types = terrestrial.get("planetTypes", {}) if isinstance(terrestrial, dict) else {}
    if size_name not in sizes:
        raise ValueError(f"Planet size not found in assets: {size_name}")
    if type_name not in types:
        raise ValueError(f"Terrestrial planet type not found in assets: {type_name}")
    config = _json_merge_objects(
        terrestrial.get("planetDefaults"), sizes[size_name], types[type_name]
    )
    if surface_only_primary:
        layers = config.setdefault("layers", {})
        surface = layers.setdefault("surface", {})
        surface["primaryRegion"] = [type_name]
        surface["secondaryRegions"] = []
        surface["secondaryRegionCount"] = [0, 0]

    rng = random.Random(_stable_asset_seed(seed, type_name, size_name, "planet"))
    threat_level = _random_range(config.get("threatRange"), rng, 1.0)
    layer_names = ["space", "atmosphere", "surface", "subsurface"]
    layers: dict[str, dict[str, Any]] = {}
    for layer_name in layer_names:
        layer = _terrestrial_layer_from_config(
            compiler, config, layer_name, threat_level, seed
        )
        if layer is None:
            raise ValueError(f"Required terrestrial layer is disabled: {layer_name}")
        layers[layer_name] = layer
    underground: list[dict[str, Any]] = []
    index = 1
    while True:
        layer = _terrestrial_layer_from_config(
            compiler, config, f"underground{index}", threat_level, seed
        )
        if layer is None:
            break
        underground.append(layer)
        index += 1
    core = _terrestrial_layer_from_config(
        compiler, config, "core", threat_level, seed
    )
    if core is None:
        raise ValueError("Required terrestrial layer is disabled: core")

    primary_biome = str(layers["surface"]["primaryRegion"]["biome"])
    try:
        biome = compiler.catalog["biomes"][primary_biome]
    except KeyError as exc:
        raise ValueError(
            f"Primary biome definition is missing from assets: {primary_biome}"
        ) from exc
    sky_options = biome.get("skyOptions", []) if isinstance(biome, dict) else []
    sky = _random_list_item(sky_options, rng, {})
    if not isinstance(sky, dict):
        sky = {}
    hue = _random_list_item(biome.get("hueShiftOptions", []), rng, 0.0)
    surface_region = layers["surface"]["primaryRegion"]
    surface_liquid = int(
        surface_region.get("oceanLiquid") or surface_region.get("caveLiquid") or 0
    )
    return {
        "typeName": type_name,
        "threatLevel": float(threat_level),
        "worldSize": [int(value) for value in config.get("size", [4000, 3000])],
        "gravity": float(_random_range(config.get("gravityRange"), rng, 80.0)),
        "airless": bool(biome.get("airless", False)),
        "weatherPool": _weather_pool_from_biome(
            compiler.reader, biome, threat_level, rng
        ),
        "environmentStatusEffects": [
            str(value) for value in biome.get("statusEffects", [])
        ],
        "overrideTech": deep_copy_json(config.get("overrideTech")),
        "globalDirectives": deep_copy_json(config.get("globalDirectives")),
        "beamUpRule": str(config.get("beamUpRule", "Surface")),
        "disableDeathDrops": bool(config.get("disableDeathDrops", False)),
        "terraformed": False,
        "worldEdgeForceRegions": str(
            config.get("worldEdgeForceRegions", "Top")
        ),
        "primaryBiome": primary_biome,
        "sizeName": size_name,
        "hueShift": float(hue or 0.0),
        "surfaceLiquid": surface_liquid,
        "skyColoring": deep_copy_json(sky),
        "dayLength": float(_random_range(config.get("dayLengthRange"), rng, 900.0)),
        "blockNoise": deep_copy_json(config.get("blockNoise", {})),
        "blendNoise": deep_copy_json(config.get("blendNoise", {})),
        "blendSize": float(config.get("blendSize", 80.0)),
        "spaceLayer": layers["space"],
        "atmosphereLayer": layers["atmosphere"],
        "surfaceLayer": layers["surface"],
        "subsurfaceLayer": layers["subsurface"],
        "undergroundLayers": underground,
        "coreLayer": core,
        "type": "TerrestrialWorldParameters",
    }


def _build_asteroids_world_parameters(reader: StarboundAssetReader, seed: int) -> dict[str, Any]:
    config = reader.json("/asteroids_worlds.config")
    rng = random.Random(_stable_asset_seed(seed, "asteroids"))
    return {
        "typeName": "asteroids",
        "threatLevel": float(_random_range(config.get("threatRange"), rng, 1.0)),
        "worldSize": [int(value) for value in config.get("worldSize", [16000, 7000])],
        "gravity": float(_random_range(config.get("gravityRange"), rng, 0.0)),
        "airless": True,
        "weatherPool": [],
        "environmentStatusEffects": [
            str(value) for value in config.get("environmentStatusEffects", [])
        ],
        "overrideTech": deep_copy_json(config.get("overrideTech")),
        "globalDirectives": deep_copy_json(config.get("globalDirectives")),
        "beamUpRule": str(config.get("beamUpRule", "Surface")),
        "disableDeathDrops": bool(config.get("disableDeathDrops", False)),
        "terraformed": False,
        "worldEdgeForceRegions": str(
            config.get("worldEdgeForceRegions", "TopAndBottom")
        ),
        "asteroidTopLevel": int(config.get("asteroidsTop", 5000)),
        "asteroidBottomLevel": int(config.get("asteroidsBottom", 2000)),
        "blendSize": float(config.get("blendSize", 50.0)),
        "asteroidBiome": str(config.get("biome", "asteroids")),
        "ambientLightLevel": deep_copy_json(
            config.get("ambientLightLevel", [128, 128, 128])
        ),
        "type": "AsteroidsWorldParameters",
    }


def _build_floating_dungeon_world_parameters(
    reader: StarboundAssetReader, dungeon_world: str
) -> dict[str, Any]:
    config = reader.json(f"/dungeon_worlds.config:{dungeon_world}")
    music = config.get("musicTrack")
    ambient = config.get("ambientNoises")
    return {
        "typeName": dungeon_world,
        "threatLevel": float(config.get("threatLevel", 0.0)),
        "worldSize": [int(value) for value in config["worldSize"]],
        "gravity": float(config.get("gravity", 80.0)),
        "airless": bool(config.get("airless", False)),
        "weatherPool": [
            {"weight": float(entry[0]), "item": str(entry[1])}
            for entry in config.get("weatherPool", [])
            if isinstance(entry, list) and len(entry) == 2
        ],
        "environmentStatusEffects": [
            str(value) for value in config.get("environmentStatusEffects", [])
        ],
        "overrideTech": deep_copy_json(config.get("overrideTech")),
        "globalDirectives": deep_copy_json(config.get("globalDirectives")),
        "beamUpRule": str(config.get("beamUpRule", "Surface")),
        "disableDeathDrops": bool(config.get("disableDeathDrops", False)),
        "terraformed": False,
        "worldEdgeForceRegions": str(
            config.get("worldEdgeForceRegions", "Top")
        ),
        "dungeonBaseHeight": int(config["dungeonBaseHeight"]),
        "dungeonSurfaceHeight": int(
            config.get("dungeonSurfaceHeight", config["dungeonBaseHeight"])
        ),
        "dungeonUndergroundLevel": int(config.get("dungeonUndergroundLevel", 0)),
        "primaryDungeon": str(config["primaryDungeon"]),
        "biome": deep_copy_json(config.get("biome")),
        "ambientLightLevel": deep_copy_json(
            config.get("ambientLightLevel", [128, 128, 128])
        ),
        "dayMusicTrack": deep_copy_json(config.get("dayMusicTrack", music)),
        "nightMusicTrack": deep_copy_json(config.get("nightMusicTrack", music)),
        "dayAmbientNoises": deep_copy_json(
            config.get("dayAmbientNoises", ambient)
        ),
        "nightAmbientNoises": deep_copy_json(
            config.get("nightAmbientNoises", ambient)
        ),
        "type": "FloatingDungeonWorldParameters",
    }


def celestial_planet_sizes(assets_folder: Path) -> list[str]:
    reader = StarboundAssetReader(Path(assets_folder))
    config = reader.json("/terrestrial_worlds.config")
    sizes = config.get("planetSizes", {}) if isinstance(config, dict) else {}
    return [str(name) for name in sizes]


def celestial_asset_biomes(assets_folder: Path) -> list[str]:
    """Return every biome name available to the celestial biome editor."""

    catalog = load_asset_biome_catalog(Path(assets_folder))
    return sorted((str(name) for name in catalog["biomes"]), key=str.casefold)


def celestial_asset_biome_choices(
    assets_folder: Path,
) -> list[dict[str, str]]:
    """Return biome names with compact top-level asset-folder labels.

    A path such as ``/biomes/surface_detached/old/charred/charred.biome`` is
    displayed as ``surface_detached-charred``.  Only the first directory below
    ``biomes`` is used, so implementation folders such as ``old`` do not make
    the GUI labels unnecessarily long.  The underlying biome name remains
    unchanged and is still the value written to Starbound data.
    """

    catalog = load_asset_biome_catalog(Path(assets_folder))
    choices: list[dict[str, str]] = []
    for raw_name, raw_path in catalog["paths"].items():
        name = str(raw_name)
        parts = [part for part in str(raw_path).replace("\\", "/").split("/") if part]
        category = "biomes"
        try:
            biome_index = next(
                index for index, part in enumerate(parts) if part.casefold() == "biomes"
            )
        except StopIteration:
            biome_index = -1
        if biome_index >= 0 and biome_index + 1 < len(parts) - 1:
            category = parts[biome_index + 1]
        choices.append(
            {
                "name": name,
                "category": category,
                "display": f"{category}-{name}",
                "path": str(raw_path),
            }
        )
    choices.sort(key=lambda item: (item["display"].casefold(), item["name"].casefold()))
    return choices


def celestial_star_types(assets_folder: Path) -> list[str]:
    """Return the finite system-star typeName choices from celestial.config."""

    reader = StarboundAssetReader(Path(assets_folder))
    config = reader.json("/celestial.config")
    system_types = config.get("systemTypes", {}) if isinstance(config, dict) else {}
    result: set[str] = set()
    for value in system_types.values():
        if not isinstance(value, dict):
            continue
        raw = value.get("baseParameters")
        if isinstance(raw, dict) and isinstance(raw.get("typeName"), str):
            result.add(raw["typeName"])
    return sorted(result, key=str.casefold)


def celestial_terrestrial_graphics_types(assets_folder: Path) -> list[str]:
    """Return the terrestrial ``worldImages`` appearance keys.

    Starbound does not store an arbitrary large-image path for an ordinary
    terrestrial planet.  ``celestial.worldImages`` selects one of the
    ``terrestrialGraphics`` profiles in ``/celestial.config`` using the
    world's visitable ``typeName``.  ``default`` is an internal fallback, not
    a user-facing planet style, so it is intentionally omitted.
    """

    reader = StarboundAssetReader(Path(assets_folder))
    config = reader.json("/celestial.config")
    graphics = config.get("terrestrialGraphics", {}) if isinstance(config, dict) else {}
    if not isinstance(graphics, dict):
        return []
    return sorted(
        (
            str(name)
            for name, value in graphics.items()
            if str(name).casefold() != "default" and isinstance(value, dict)
        ),
        key=str.casefold,
    )


def build_new_celestial_body(
    assets_folder: Path,
    preset: str,
    size_name: str,
    location: Sequence[int],
    planet_orbit: int,
    satellite_orbit: int = 0,
    *,
    name: str = "New Celestial Body",
    seed: int | None = None,
) -> dict[str, Any]:
    """Build a fresh celestial body from assets rather than cloning one.

    A non-zero ``satellite_orbit`` nests the new body under a planet.  Planet,
    Gas Giant, Asteroid Field and Ancient Gateway records are supported there;
    the independent Outpost FloatingDungeon preset is deliberately excluded.
    """

    preset = str(preset).strip().lower()
    # ``garden`` was used by an early development build.  Accept it when
    # reading automation calls, but new UI-created planets deliberately start
    # as the neutral Barren type requested by the user.
    if preset == "garden":
        preset = "planet"
    if preset not in CELESTIAL_BODY_PRESETS:
        raise ValueError(f"Unknown celestial body preset: {preset}")
    location_values = [int(value) for value in location]
    if len(location_values) != 3:
        raise ValueError("Celestial location requires X, Y and Z")
    planet_orbit = int(planet_orbit)
    satellite_orbit = int(satellite_orbit)
    if planet_orbit <= 0 or satellite_orbit < 0:
        raise ValueError("Celestial orbit numbers are invalid")
    if satellite_orbit and not celestial_body_preset_supports_satellite(preset):
        raise ValueError(
            f"Celestial body preset cannot be created as a satellite: {preset}"
        )
    if seed is None:
        # CelestialParameters reads this member as an unsigned integer.  Keep
        # fresh seeds inside the positive signed-64 range so both Starbound's
        # UInt reader and this editor's Dynamic/SBON writer accept the value.
        seed = random.SystemRandom().randint(0, 2**63 - 1)
    seed = int(seed)
    reader = StarboundAssetReader(Path(assets_folder))
    celestial = reader.json("/celestial.config")
    planetary_types = celestial.get("planetaryTypes", {})
    satellite_types = celestial.get("satelliteTypes", {})

    if preset == "planet":
        preset_config = planetary_types.get("Barren")
        if not isinstance(preset_config, dict):
            preset_config = planetary_types.get("Tier1", {})
        raw = _json_merge_objects(preset_config.get("baseParameters"))
        variations = preset_config.get("variationParameters", [])
        matching = next(
            (
                value
                for value in variations
                if isinstance(value, dict) and value.get("worldSize") == size_name
            ),
            None,
        )
        if matching is not None:
            raw = _json_merge_objects(raw, matching)
        else:
            image_scales = {
                "verysmall": 0.08,
                "small": 0.1,
                "medium": 0.125,
                "large": 0.15,
            }
            raw = _json_merge_objects(
                raw,
                {
                    "worldSize": size_name,
                    "imageScale": image_scales.get(size_name, 0.125),
                    "smallImageScale": 0.5 if size_name == "small" else 0.6,
                },
            )
        raw["worldType"] = "Terrestrial"
        raw["worldSize"] = size_name
        raw["terrestrialType"] = ["barren"]
        compiler = _celestial_biome_resources(Path(assets_folder))
        visitable: dict[str, Any] | None = _build_terrestrial_world_parameters(
            compiler,
            "barren",
            size_name,
            seed,
            surface_only_primary=True,
        )
    elif preset == "gasgiant":
        config = planetary_types.get("GasGiant", {})
        variations = config.get("variationParameters", [])
        variation = next((value for value in variations if isinstance(value, dict)), {})
        raw = _json_merge_objects(config.get("baseParameters"), variation)
        visitable = None
    elif preset == "asteroidfield":
        config = planetary_types.get("AsteroidField", {})
        variations = config.get("variationParameters", [])
        variation = next((value for value in variations if isinstance(value, dict)), {})
        raw = _json_merge_objects(config.get("baseParameters"), variation)
        visitable = _build_asteroids_world_parameters(reader, seed)
    else:
        dungeon_world = "ancientgateway" if preset == "ancientgateway" else "outpost"
        if preset == "ancientgateway":
            config = planetary_types.get("AncientGateway", {})
            raw = _json_merge_objects(config.get("baseParameters"))
        else:
            raw = {
                "worldType": "FloatingDungeon",
                "description": "Outpost (independent orbital copy)",
                "dungeonWorld": "outpost",
                "smallImage": "/celestial/system/gate.png",
                "image": "/celestial/system/gate/gate1.png",
                "imageScale": 1.0,
                "smallImageScale": 1.0,
                "worldSize": "outpost",
            }
        visitable = _build_floating_dungeon_world_parameters(reader, dungeon_world)

    body = {
        "coordinate": {
            "location": location_values,
            "planet": planet_orbit,
            "satellite": satellite_orbit,
        },
        "parameters": raw,
        "seed": seed,
        "name": str(name),
        "visitableParameters": visitable,
    }
    _validate_celestial_parameters(
        body, location_values, planet_orbit, satellite_orbit
    )
    return body


def resize_celestial_terrestrial_body(
    parameters: dict[str, Any], assets_folder: Path, size_name: str
) -> None:
    """Resize a terrestrial body while preserving its edited biome recipes.

    Planet-size presets change both the stored world dimensions and every
    vertical layer height.  Updating only ``worldSize`` creates an internally
    inconsistent world, so this recompiles the selected preset and transplants
    the current region recipes into the correctly scaled layers.
    """

    raw = parameters.get("parameters")
    current = parameters.get("visitableParameters")
    if not isinstance(raw, dict) or not isinstance(current, dict):
        raise ValueError("Only visitable terrestrial bodies can be resized")
    if raw.get("worldType") != "Terrestrial" or current.get("type") != "TerrestrialWorldParameters":
        raise ValueError("Only terrestrial planets and moons can be resized")
    type_choices = raw.get("terrestrialType")
    if isinstance(type_choices, list) and type_choices:
        type_name = str(type_choices[0])
    else:
        # ``typeName`` is a user-visible star-map label and may deliberately
        # differ from the generation biome, so never use it as a compiler key.
        type_name = str(current.get("primaryBiome", "barren"))
    resources = _celestial_biome_resources(Path(assets_folder))
    resized = _build_terrestrial_world_parameters(
        resources, type_name, str(size_name), int(parameters.get("seed", 0))
    )

    def preserve_recipe(new_layer: Any, old_layer: Any) -> Any:
        if not isinstance(new_layer, dict) or not isinstance(old_layer, dict):
            return new_layer
        result = deep_copy_json(new_layer)
        for key in (
            "dungeons",
            "dungeonXVariance",
            "primaryRegion",
            "primarySubRegion",
            "secondaryRegions",
            "secondarySubRegions",
            "secondaryRegionSizeRange",
            "subRegionSizeRange",
        ):
            if key in old_layer:
                result[key] = deep_copy_json(old_layer[key])
        return result

    for key in ("spaceLayer", "atmosphereLayer", "surfaceLayer", "subsurfaceLayer", "coreLayer"):
        resized[key] = preserve_recipe(resized.get(key), current.get(key))
    old_underground = current.get("undergroundLayers")
    new_underground = resized.get("undergroundLayers")
    if isinstance(old_underground, list) and isinstance(new_underground, list):
        resized["undergroundLayers"] = [
            preserve_recipe(layer, old_underground[index])
            if index < len(old_underground)
            else layer
            for index, layer in enumerate(new_underground)
        ]

    # These values are user-editable and independent from the size preset.
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
        "primaryBiome",
        "hueShift",
        "surfaceLiquid",
        "skyColoring",
        "dayLength",
    ):
        if key in current:
            resized[key] = deep_copy_json(current[key])
    raw["worldSize"] = str(size_name)
    current.clear()
    current.update(resized)


def resize_celestial_terrestrial_body_dimensions(
    parameters: dict[str, Any], width: int, height: int
) -> None:
    """Set an arbitrary terrestrial world width/height consistently.

    ``visitableParameters.worldSize`` is the numeric value used by Starbound
    when the world is generated (and by ``celestial.planetSize`` for the
    system-map icon).  Vertical layer and ocean levels are absolute tile
    coordinates, so changing only the two-element size would leave a broken
    generation recipe.  This scales every absolute vertical generation value
    while preserving the selected biome/region recipes.

    This function only prepares a *newly generated* World4.  Callers must
    remove or move aside an existing ``.world`` before saving the Celestial
    record; resizing an existing World4 in place would invalidate its sector
    grid and can destroy terrain/buildings.
    """

    if isinstance(width, bool) or isinstance(height, bool):
        raise ValueError("World width and height must be integers")
    width = int(width)
    height = int(height)
    if width < 1000 or height < 1000:
        raise ValueError("World width and height must each be at least 1000 tiles")
    raw = parameters.get("parameters")
    visitable = parameters.get("visitableParameters")
    if not isinstance(raw, dict) or not isinstance(visitable, dict):
        raise ValueError("Only visitable terrestrial bodies can be resized")
    if raw.get("worldType") != "Terrestrial" or visitable.get("type") != "TerrestrialWorldParameters":
        raise ValueError("Only terrestrial planets and moons can be resized")
    current_size = visitable.get("worldSize")
    if not (
        isinstance(current_size, list)
        and len(current_size) == 2
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in current_size)
    ):
        raise ValueError("The terrestrial body has no valid numeric worldSize")
    old_height = float(current_size[1])
    if not math.isfinite(old_height) or old_height <= 0:
        raise ValueError("The current world height is invalid")
    validate_celestial_layer_layout(parameters)
    ratio = float(height) / old_height

    def scaled(value: Any, *, integer: bool = False) -> int | float:
        result = float(value) * ratio
        if not math.isfinite(result):
            raise ValueError("Vertical generation coordinates must be finite numbers")
        if integer:
            # TerrestrialWorldParameters reads these coordinates with getInt;
            # match its truncation now, not after native generation rewrites
            # the file and makes the strict layout verification fail.
            return int(result)
        rounded = round(result)
        return int(rounded) if abs(result - rounded) < 1e-9 else result

    # These members are absolute Y tile coordinates.  Values nested in region
    # recipes (especially oceanLiquidLevel) must move with the layer itself.
    integer_vertical_keys = {
        "layerMinHeight",
        "layerBaseHeight",
        "oceanLiquidLevel",
        "dungeonBaseHeight",
        "dungeonSurfaceHeight",
        "dungeonUndergroundLevel",
        "asteroidTopLevel",
        "asteroidBottomLevel",
    }
    # Sky levels are floating point. Terrain-selector parameters.baseHeight
    # and the independent coastal-basin floor heights are not these fields.
    vertical_keys = integer_vertical_keys | {"surfaceLevel", "spaceLevel"}

    def scale_vertical_members(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if (
                    key in vertical_keys
                    and isinstance(child, (int, float))
                    and not isinstance(child, bool)
                ):
                    # Zero is the conventional "not present" liquid level.
                    value[key] = scaled(child, integer=key in integer_vertical_keys)
                else:
                    scale_vertical_members(child)
        elif isinstance(value, list):
            for child in value:
                scale_vertical_members(child)

    resized = deep_copy_json(visitable)
    scale_vertical_members(resized)
    resized["worldSize"] = [width, height]
    candidate = dict(parameters, visitableParameters=resized)

    # Catch rounding-induced inversions before an invalid recipe reaches the
    # game.  Duplicate boundaries are legal for disabled/zero-height layers;
    # an individual layer's base may never be below its minimum.
    for layer in celestial_layer_settings(candidate):
        minimum = layer.get("layerMinHeight")
        base = layer.get("layerBaseHeight")
        if not isinstance(minimum, (int, float)) or not isinstance(base, (int, float)):
            raise ValueError(f"Layer {layer['layerLabel']} has invalid height values")
        if minimum < 0 or base < minimum or base > height:
            raise ValueError(
                f"Layer {layer['layerLabel']} is outside the resized world height"
            )
    validate_celestial_layer_layout(candidate)
    # A failed check must not leave the editor with a partially resized body.
    visitable.clear()
    visitable.update(resized)


def celestial_system_map_size_for_width(
    width: int | float, assets_folder: Path | None = None
) -> float:
    """Return the system-map diameter selected for a visitable world width."""

    thresholds: Any = [
        [0, 2],
        [1000, 2],
        [3000, 4],
        [4000, 8],
        [6000, 12],
        [10000, 24],
    ]
    if assets_folder is not None and Path(assets_folder).exists():
        try:
            config = StarboundAssetReader(Path(assets_folder)).json("/systemworld.config")
            candidate = config.get("planetSizes") if isinstance(config, dict) else None
            if isinstance(candidate, list) and candidate:
                thresholds = candidate
        except Exception:
            pass
    result = 0.0
    numeric_width = float(width)
    for entry in thresholds:
        if not (
            isinstance(entry, list)
            and len(entry) == 2
            and all(isinstance(value, (int, float)) for value in entry)
        ):
            continue
        if numeric_width >= float(entry[0]):
            result = float(entry[1])
    return result


def celestial_layer_settings(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Return editable vertical boundaries for every terrestrial layer."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in celestial_layer_groups(parameters):
        layer_id = str(group["layerId"])
        if layer_id in seen:
            continue
        seen.add(layer_id)
        layer = _celestial_layer(parameters, layer_id)
        result.append(
            {
                "layerId": layer_id,
                "layerLabel": str(group["layerLabel"]),
                "layerMinHeight": layer.get("layerMinHeight"),
                "layerBaseHeight": layer.get("layerBaseHeight"),
                "dungeons": deep_copy_json(layer.get("dungeons", [])),
                "dungeonXVariance": layer.get("dungeonXVariance"),
            }
        )
    return result


def set_celestial_layer_settings(
    parameters: dict[str, Any],
    layer_id: str,
    minimum_height: int | float,
    base_height: int | float,
) -> None:
    """Set one layer's minimum/base height with basic structural checks."""

    minimum = float(minimum_height)
    base = float(base_height)
    if not math.isfinite(minimum) or not math.isfinite(base):
        raise ValueError("Layer minimum/base heights must be finite numbers")
    if not minimum.is_integer() or not base.is_integer():
        raise ValueError("Layer minimum/base heights must be whole tile coordinates (integers)")
    if minimum < 0 or base < minimum:
        raise ValueError("Layer base height must be at or above its minimum height")
    layer = _celestial_layer(parameters, str(layer_id))
    layer["layerMinHeight"] = int(minimum)
    layer["layerBaseHeight"] = int(base)


def set_celestial_layer_dungeons(
    parameters: dict[str, Any],
    layer_id: str,
    dungeons: Sequence[str],
    dungeon_x_variance: int | float | None,
) -> None:
    """Replace the major-dungeon pool for one vertical generation layer."""

    if isinstance(dungeons, (str, bytes)) or not isinstance(dungeons, Sequence):
        raise ValueError("Layer dungeons must be a JSON array of names")
    normalized: list[str] = []
    for name in dungeons:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Every layer dungeon must be a non-empty string")
        normalized.append(name.strip())
    layer = _celestial_layer(parameters, str(layer_id))
    layer["dungeons"] = normalized
    if dungeon_x_variance is None:
        layer.pop("dungeonXVariance", None)
    else:
        variance = float(dungeon_x_variance)
        if not math.isfinite(variance) or variance < 0:
            raise ValueError("Dungeon X variance must be a non-negative number")
        layer["dungeonXVariance"] = (
            int(variance) if variance.is_integer() else variance
        )


def validate_celestial_layer_layout(parameters: dict[str, Any]) -> None:
    """Validate a terrestrial body's vertical layer boundaries.

    Layer minimums are the actual contiguous boundaries.  ``layerBaseHeight``
    is the terrain/reference height inside that layer, so it must lie between
    the layer minimum and the next higher layer minimum (or the world top for
    Space).  Underground layers are stored from highest to lowest and are
    therefore reversed for this physical bottom-to-top check.
    """

    visitable = parameters.get("visitableParameters")
    if not isinstance(visitable, dict) or visitable.get("type") != "TerrestrialWorldParameters":
        return
    ordered_ids: list[str] = []
    if isinstance(visitable.get("coreLayer"), dict):
        ordered_ids.append("core")
    underground = visitable.get("undergroundLayers")
    if isinstance(underground, list):
        ordered_ids.extend(
            f"underground:{index}" for index in reversed(range(len(underground)))
        )
    for layer_id, key in (
        ("subsurface", "subsurfaceLayer"),
        ("surface", "surfaceLayer"),
        ("atmosphere", "atmosphereLayer"),
        ("space", "spaceLayer"),
    ):
        if isinstance(visitable.get(key), dict):
            ordered_ids.append(layer_id)
    if not ordered_ids:
        return

    world_size = visitable.get("worldSize")
    if not (
        isinstance(world_size, list)
        and len(world_size) == 2
        and isinstance(world_size[1], (int, float))
        and not isinstance(world_size[1], bool)
    ):
        raise ValueError("Terrestrial worldSize must contain numeric width and height")
    world_height = float(world_size[1])
    if not math.isfinite(world_height) or world_height <= 0:
        raise ValueError("Terrestrial world height must be positive")

    values: list[tuple[str, float, float]] = []
    for layer_id in ordered_ids:
        layer = _celestial_layer(parameters, layer_id)
        minimum_value = layer.get("layerMinHeight")
        base_value = layer.get("layerBaseHeight")
        if not (
            isinstance(minimum_value, (int, float))
            and not isinstance(minimum_value, bool)
            and isinstance(base_value, (int, float))
            and not isinstance(base_value, bool)
        ):
            raise ValueError(f"Layer {layer_id} needs numeric minimum/base heights")
        minimum = float(minimum_value)
        base = float(base_value)
        if not math.isfinite(minimum) or not math.isfinite(base):
            raise ValueError(f"Layer {layer_id} heights must be finite")
        if minimum < 0 or minimum > world_height:
            raise ValueError(
                f"Layer {layer_id} minimum must be within 0–{world_height:g}"
            )
        if base < minimum or base > world_height:
            raise ValueError(
                f"Layer {layer_id} base must be within its minimum and world height"
            )
        values.append((layer_id, minimum, base))

    first_id, first_minimum, _first_base = values[0]
    if first_minimum != 0:
        raise ValueError(f"Lowest layer {first_id} must begin at height 0")
    for (lower_id, lower_minimum, lower_base), (
        upper_id,
        upper_minimum,
        _upper_base,
    ) in zip(values, values[1:]):
        if upper_minimum < lower_minimum:
            raise ValueError(
                f"Layer {upper_id} must not begin below layer {lower_id}"
            )
        if lower_base > upper_minimum:
            raise ValueError(
                f"Layer {lower_id} base exceeds the next boundary ({upper_id})"
            )


def world_biome_spawn_profile(world: Path, biome_name: str) -> dict[str, Any]:
    """Read the first compiled spawn profile for a named biome."""

    wanted = str(biome_name).strip().casefold()
    if not wanted:
        raise ValueError("Biome name cannot be empty")
    matches = [
        biome
        for biome in compiled_biomes(load_metadata_only(Path(world)))
        if str(biome.get("baseName", "")).casefold() == wanted
    ]
    if not matches:
        raise ValueError(f"Compiled biome not found in generated world: {biome_name}")
    profile = matches[0].get("spawnProfile")
    if not isinstance(profile, dict):
        profile = {"spawnTypes": [], "monsterParameters": {}}
    return deep_copy_json(profile)


def write_world_biome_spawn_profile_in_place(
    world: Path,
    biome_name: str,
    spawn_types: Sequence[str],
    monster_parameters: dict[str, Any] | None = None,
    *,
    expected_fingerprint: dict[str, Any] | None = None,
) -> tuple[Path | None, int]:
    """Guarded replacement of all compiled copies of a named biome's profile.

    An explicit empty species list clears it. Identical profiles are a no-op.
    A dialog may pass its opening fingerprint to reject stale edits, including
    external changes with the same file size and modification time.
    """

    if __package__:
        from . import world_edit_guard
    else:
        import world_edit_guard

    world = Path(world).resolve()
    wanted = str(biome_name).strip().casefold()
    cleaned = [str(value).strip() for value in spawn_types if str(value).strip()]
    if not wanted:
        raise ValueError("Biome name cannot be empty")
    if monster_parameters is not None and not isinstance(monster_parameters, dict):
        raise ValueError("monsterParameters must be a JSON object")
    if monster_parameters is not None:
        validate_finite_json_numbers(monster_parameters, "monsterParameters")
        if __package__:
            from .celestial_resource_validation import validate_monster_parameters
        else:
            from celestial_resource_validation import validate_monster_parameters
        validate_monster_parameters(monster_parameters)
    world_edit_guard.assert_world_available(world)
    before = (dict(expected_fingerprint) if expected_fingerprint is not None
              else world_edit_guard.fingerprint(world))
    world_edit_guard.assert_world_unchanged(world, before)
    document = load_metadata_only(world)
    matched = changed = 0
    for biome in compiled_biomes(document):
        if str(biome.get("baseName", "")).casefold() != wanted:
            continue
        matched += 1
        old = biome.get("spawnProfile")
        profile = deep_copy_json(old) if isinstance(old, dict) else {}
        profile["spawnTypes"] = cleaned
        if monster_parameters is not None:
            profile["monsterParameters"] = deep_copy_json(monster_parameters)
        if profile != old:
            biome["spawnProfile"] = profile
            changed += 1
    world_edit_guard.assert_world_unchanged(world, before)
    if not matched:
        raise ValueError(f"Compiled biome not found in generated world: {biome_name}")
    if not changed:
        return None, 0

    def builder(backup: Path, staged: Path) -> int:
        saved = world_edit_guard.fingerprint(backup)
        if (saved["size"], saved["sha256"]) != (before["size"], before["sha256"]):
            raise RuntimeError(
                "Target .world changed while backing up; species edit was not installed"
            )
        database, records = load_records(backup)
        try:
            # This preserves every non-metadata record and verifies the staged
            # database before the final source availability/fingerprint check.
            write_metadata_document(backup, staged, database, records, document)
        finally:
            close_database(database)
        world_edit_guard.assert_world_unchanged(world, before)
        return changed

    # A failed build or commit never copies the old backup over a newer source.
    return rebuild_world_in_place_from_backup(world, builder, keep_backup=True)


CELESTIAL_RUNTIME_METADATA_KEYS = (
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
    "surfaceLiquid",
    "skyColoring",
    "dayLength",
)

# These generation values may have been changed through Tool 02 after the
# Celestial record was first created.  For an existing World4 the .world copy
# is authoritative for display/auditing in Tool 05.  They remain structural:
# editing them in Tool 05 causes a full-world regeneration rather than an
# in-place metadata update.
CELESTIAL_WORLD_GENERATION_KEYS = (
    "primaryBiome",
    "blendSize",
    "blockNoise",
    "blendNoise",
    "spaceLayer",
    "atmosphereLayer",
    "surfaceLayer",
    "subsurfaceLayer",
    "undergroundLayers",
    "coreLayer",
)


def merge_world_runtime_parameters_into_celestial(
    parameters: dict[str, Any], world: Path
) -> list[str]:
    """Overlay an existing world's authoritative runtime metadata.

    ``universe.chunks`` keeps the Celestial generation record, but tools which
    edit a generated World4 file can legitimately leave its gravity, weather,
    day or sky values newer than that record.  Tool 05 must show the values the
    game actually uses inside the generated world.  Only metadata-safe runtime
    fields are copied.  Numeric world size comes from the World4 root header,
    and the displayed generation layers come from the World4 template, so a
    stale preset name such as ``medium`` cannot hide a custom 50000x30000
    world.
    """

    visitable = parameters.get("visitableParameters")
    if not isinstance(visitable, dict):
        return []
    document = load_metadata_only(Path(world))
    template = world_template(document)
    current_raw = parameters.get("parameters")
    celestial = template.get("celestialParameters")
    world_raw = celestial.get("parameters") if isinstance(celestial, dict) else None
    current_world_type = (
        str(current_raw.get("worldType", "")).strip()
        if isinstance(current_raw, dict)
        else ""
    )
    generated_world_type = (
        str(world_raw.get("worldType", "")).strip()
        if isinstance(world_raw, dict)
        else ""
    )
    # A structural Celestial edit can leave the old generated .world beside
    # the new body definition until the next save.  Never hydrate a terrestrial
    # body from an old Asteroids/GasGiant/etc. file: those formats legitimately
    # store skyColoring and dayLength as null, which is fatal when copied into
    # TerrestrialWorldParameters.
    if (
        current_world_type
        and generated_world_type
        and current_world_type.casefold() != generated_world_type.casefold()
    ):
        return []
    values: dict[str, Any] = {}
    if isinstance(celestial, dict):
        world_visitable = celestial.get("visitableParameters")
        if isinstance(world_visitable, dict):
            for key in CELESTIAL_RUNTIME_METADATA_KEYS:
                if key in world_visitable and world_visitable[key] is not None:
                    values[key] = deep_copy_json(world_visitable[key])
    world_parameters = template.get("worldParameters")
    if isinstance(world_parameters, dict):
        for key in CELESTIAL_RUNTIME_METADATA_KEYS + CELESTIAL_WORLD_GENERATION_KEYS:
            if key in world_parameters and world_parameters[key] is not None:
                values[key] = deep_copy_json(world_parameters[key])
    sky_parameters = template.get("skyParameters")
    if isinstance(sky_parameters, dict):
        for key in ("dayLength", "skyColoring"):
            if key in sky_parameters and sky_parameters[key] is not None:
                values[key] = deep_copy_json(sky_parameters[key])
    root_size = document.get("size")
    if (
        isinstance(root_size, list)
        and len(root_size) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) for value in root_size)
    ):
        values["worldSize"] = [int(root_size[0]), int(root_size[1])]
    changed: list[str] = []
    for key, value in values.items():
        if visitable.get(key) != value:
            visitable[key] = value
            changed.append(key)
    return changed


def generated_world_matches_celestial_type(
    parameters: dict[str, Any], world: Path
) -> bool:
    """Return whether a saved World4 belongs to the body's current world type.

    A false result means that the file is stale after a structural body-type
    change and must be moved aside before Starbound generates the replacement.
    Unreadable or legacy files without an embedded type are left to the normal
    World4 validation path instead of being deleted speculatively.
    """

    raw = parameters.get("parameters")
    expected = raw.get("worldType") if isinstance(raw, dict) else None
    if not isinstance(expected, str) or not expected.strip():
        return True
    try:
        template = world_template(load_metadata_only(Path(world)))
    except Exception:
        return True
    celestial = template.get("celestialParameters")
    world_raw = celestial.get("parameters") if isinstance(celestial, dict) else None
    actual = world_raw.get("worldType") if isinstance(world_raw, dict) else None
    if not isinstance(actual, str) or not actual.strip():
        return True
    return expected.strip().casefold() == actual.strip().casefold()


def hydrate_celestial_system_from_world_files(
    editor: dict[str, Any], universe_folder: Path
) -> list[dict[str, Any]]:
    """Refresh generated bodies from their matching World4 metadata files."""

    location = [int(value) for value in editor.get("location", [])]
    if len(location) != 3:
        raise ValueError("Celestial editor location must contain X, Y and Z")
    universe_folder = Path(universe_folder)
    refreshed: list[dict[str, Any]] = []

    def refresh(parameters: dict[str, Any], planet: int, satellite: int) -> None:
        stem = "_".join(map(str, location)) + f"_{planet}"
        if satellite:
            stem += f"_{satellite}"
        world = universe_folder / f"{stem}.world"
        if not world.is_file():
            return
        changed = merge_world_runtime_parameters_into_celestial(parameters, world)
        if changed:
            refreshed.append(
                {
                    "planet": int(planet),
                    "satellite": int(satellite),
                    "world": world,
                    "fields": changed,
                }
            )

    for planet_entry in editor.get("planets", []):
        if not (
            isinstance(planet_entry, list)
            and len(planet_entry) == 2
            and isinstance(planet_entry[0], int)
            and isinstance(planet_entry[1], dict)
        ):
            continue
        planet = int(planet_entry[0])
        body = planet_entry[1]
        parameters = body.get("parameters")
        if isinstance(parameters, dict):
            refresh(parameters, planet, 0)
        for moon_entry in body.get("satellites") or []:
            if not (
                isinstance(moon_entry, list)
                and len(moon_entry) == 2
                and isinstance(moon_entry[0], int)
                and isinstance(moon_entry[1], dict)
            ):
                continue
            refresh(moon_entry[1], planet, int(moon_entry[0]))
    return refreshed


def sync_world_celestial_parameters_in_place(
    world: Path,
    parameters: dict[str, Any],
    location: Sequence[int],
    planet_orbit: int,
    satellite_orbit: int,
) -> Path:
    """Synchronize display/environment data without touching saved sectors."""

    world = Path(world)
    edited_parameters = deep_copy_json(parameters)
    _validate_celestial_parameters(
        edited_parameters,
        [int(value) for value in location],
        int(planet_orbit),
        int(satellite_orbit),
    )
    database, records, document = load_metadata_document(world)
    staged: Path | None = None
    try:
        template = world_template(document)
        template["celestialParameters"] = deep_copy_json(edited_parameters)
        visitable = edited_parameters.get("visitableParameters")
        if isinstance(visitable, dict):
            world_parameters = template.get("worldParameters")
            if not isinstance(world_parameters, dict):
                world_parameters = {}
                template["worldParameters"] = world_parameters
            for key in CELESTIAL_RUNTIME_METADATA_KEYS:
                if key in visitable:
                    world_parameters[key] = deep_copy_json(visitable[key])
            sky_parameters = template.get("skyParameters")
            if not isinstance(sky_parameters, dict):
                sky_parameters = {}
                template["skyParameters"] = sky_parameters
            for key in ("dayLength", "skyColoring"):
                if key in visitable:
                    sky_parameters[key] = deep_copy_json(visitable[key])
        with tempfile.NamedTemporaryFile(
            prefix=world.name + ".", suffix=".rebuilt", dir=world.parent, delete=False
        ) as stream:
            staged = Path(stream.name)
        staged.unlink()
        write_metadata_document(world, staged, database, records, document)
    finally:
        close_database(database)
    backup = _unique_backup_path(world)
    try:
        shutil.copy2(world, backup)
        os.replace(staged, world)
    except Exception:
        if backup.exists():
            shutil.copy2(backup, world)
        raise
    finally:
        if staged is not None and staged.exists():
            staged.unlink()
    return backup


def load_celestial_system(
    chunks_path: Path, location: Sequence[int]
) -> dict[str, Any]:
    """Load one complete system entry as an editable JSON-compatible value."""

    chunks_path = Path(chunks_path)
    location_list = [int(value) for value in location]
    body: dict[str, Any] | None = None
    with chunks_path.open("rb") as stream:
        database = BTreeDB5(stream)
        try:
            compressed = database.get(celestial_chunk_key(location_list))
            name, _version, candidate, _trailing = decode_celestial_chunk(compressed)
            if name == "CelestialChunk":
                _celestial_system_entry(candidate, location_list)
                body = candidate
        except Exception:
            body = None
    if body is None:
        _block_size, records = _load_celestial_database_records(chunks_path)
        _key, _name, _version, body, _trailing = _find_celestial_system_chunk(
            records, location_list
        )
    parameters, planets = _celestial_system_entry(body, location)
    return deep_copy_json(
        {
            "location": [int(value) for value in location],
            "systemParameters": parameters,
            "planets": planets,
        }
    )


def celestial_system_coordinate_exists(
    chunks_path: Path,
    location: Sequence[int],
    exclude_location: Sequence[int] | None = None,
) -> bool:
    """Return whether an exact X/Y/Z system coordinate is already occupied."""

    target = [int(value) for value in location]
    excluded = (
        [int(value) for value in exclude_location]
        if exclude_location is not None
        else None
    )
    _block_size, records = _load_celestial_database_records(Path(chunks_path))
    for _key, compressed in records:
        try:
            name, _version, body, _trailing = decode_celestial_chunk(compressed)
        except Exception:
            continue
        if name != "CelestialChunk":
            continue
        for entry in body.get("systemParameters", []):
            if not (isinstance(entry, list) and len(entry) == 2):
                continue
            coordinate = entry[0]
            if coordinate == target and coordinate != excluded:
                return True
    return False


def _validate_celestial_parameters(
    parameters: dict[str, Any], location: list[int], planet: int, satellite: int
) -> None:
    name = parameters.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("星系、行星和卫星名称都不能为空")
    coordinate = parameters.get("coordinate")
    if not isinstance(coordinate, dict):
        coordinate = {}
        parameters["coordinate"] = coordinate
    coordinate["location"] = list(location)
    coordinate["planet"] = int(planet)
    coordinate["satellite"] = int(satellite)
    raw = parameters.get("parameters")
    if not isinstance(raw, dict):
        raise ValueError(f"{name!r} 的 parameters 不是对象")
    validate_finite_json_numbers(parameters, f"Celestial parameters for {name!r}")
    for key in ("imageScale", "smallImageScale"):
        scale = raw.get(key)
        if key in raw and (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(float(scale))
            or float(scale) <= 0
        ):
            raise ValueError(f"{name!r} 的 {key} 必须是大于 0 的有限数值")
    if "spaceThreatLevel" in raw:
        threat = raw.get("spaceThreatLevel")
        if (
            isinstance(threat, bool)
            or not isinstance(threat, (int, float))
            or not math.isfinite(float(threat))
            or not float(threat).is_integer()
            or not 1 <= int(threat) <= 6
        ):
            raise ValueError(f"{name!r} 的 spaceThreatLevel 必须是 1–6 的整数")
    if "brightness" in raw:
        brightness = raw.get("brightness")
        if not (
            isinstance(brightness, list)
            and len(brightness) == 2
            and all(
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(float(item))
                and float(item) >= 0
                for item in brightness
            )
            and float(brightness[0]) <= float(brightness[1])
        ):
            raise ValueError(
                f"{name!r} 的 brightness 必须是递增的两个非负有限数值"
            )
    if str(raw.get("worldType", "")).strip().casefold() == "terrestrial":
        visitable = parameters.get("visitableParameters")
        if not isinstance(visitable, dict):
            raise ValueError(f"{name!r} 缺少 Terrestrial visitableParameters")
        if "skyColoring" in visitable and not isinstance(
            visitable.get("skyColoring"), dict
        ):
            raise ValueError(
                f"{name!r} 的 skyColoring 必须是完整的 JSON 对象，不能是 null"
            )
        day_length = visitable.get("dayLength")
        if "dayLength" in visitable and (
            isinstance(day_length, bool)
            or not isinstance(day_length, (int, float))
            or not math.isfinite(float(day_length))
            or float(day_length) <= 0
        ):
            raise ValueError(f"{name!r} 的 dayLength 必须是大于 0 的数值")
        gravity = visitable.get("gravity")
        if "gravity" in visitable and (
            isinstance(gravity, bool)
            or not isinstance(gravity, (int, float))
            or not math.isfinite(float(gravity))
            or float(gravity) <= 0
        ):
            raise ValueError(f"{name!r} 的 gravity 必须是大于 0 的有限数值")
        world_size = visitable.get("worldSize")
        if "worldSize" in visitable and not (
            isinstance(world_size, list)
            and len(world_size) == 2
            and all(
                isinstance(item, int)
                and not isinstance(item, bool)
                and item > 0
                for item in world_size
            )
        ):
            raise ValueError(f"{name!r} 的 worldSize 必须是两个正整数")
        if visitable.get("weatherPool"):
            weather_pool_from_editor(visitable.get("weatherPool"))
    if "seed" not in parameters or not isinstance(parameters["seed"], int):
        raise ValueError(f"{name!r} 缺少整数 seed")
    validate_celestial_region_pairings(parameters)


def normalize_celestial_system_editor(
    editor: dict[str, Any], destination_location: Sequence[int]
) -> dict[str, Any]:
    """Validate an editor document and synchronize every embedded coordinate."""

    destination = [int(value) for value in destination_location]
    if len(destination) != 3 or any(
        isinstance(value, bool) for value in destination_location
    ):
        raise ValueError("星系坐标必须包含 X、Y、Z 三个整数")
    normalized = deep_copy_json(editor)
    normalized["location"] = list(destination)
    system_parameters = normalized.get("systemParameters")
    planets = normalized.get("planets")
    if not isinstance(system_parameters, dict) or not isinstance(planets, list):
        raise ValueError("星系编辑数据缺少 systemParameters 或 planets")
    # ``generated`` is a transient GUI status (presence of a matching .world),
    # never part of Starbound's CelestialParameters records.
    system_parameters.pop("generated", None)
    repair_celestial_region_pairings(system_parameters)
    _validate_celestial_parameters(system_parameters, destination, 0, 0)

    normalized_planets: list[list[Any]] = []
    used_planet_orbits: set[int] = set()
    for entry in planets:
        if not (
            isinstance(entry, list)
            and len(entry) == 2
            and isinstance(entry[0], int)
            and not isinstance(entry[0], bool)
            and isinstance(entry[1], dict)
        ):
            raise ValueError("行星列表结构无效")
        orbit = int(entry[0])
        if orbit <= 0:
            raise ValueError("行星轨道编号必须大于 0")
        if orbit in used_planet_orbits:
            raise ValueError(f"行星轨道 {orbit} 被重复使用")
        used_planet_orbits.add(orbit)
        planet = entry[1]
        planet.pop("generated", None)
        parameters = planet.get("parameters")
        satellites = planet.get("satellites")
        if not isinstance(parameters, dict) or not isinstance(satellites, list):
            raise ValueError(f"行星轨道 {orbit} 的结构无效")
        parameters.pop("generated", None)
        repair_celestial_region_pairings(parameters)
        _validate_celestial_parameters(parameters, destination, orbit, 0)
        normalized_satellites: list[list[Any]] = []
        used_satellite_orbits: set[int] = set()
        for satellite_entry in satellites:
            if not (
                isinstance(satellite_entry, list)
                and len(satellite_entry) == 2
                and isinstance(satellite_entry[0], int)
                and not isinstance(satellite_entry[0], bool)
                and isinstance(satellite_entry[1], dict)
            ):
                raise ValueError(f"行星轨道 {orbit} 的卫星列表结构无效")
            satellite_orbit = int(satellite_entry[0])
            if satellite_orbit <= 0:
                raise ValueError("卫星轨道编号必须大于 0")
            if satellite_orbit in used_satellite_orbits:
                raise ValueError(
                    f"行星轨道 {orbit} 的卫星轨道 {satellite_orbit} 被重复使用"
                )
            used_satellite_orbits.add(satellite_orbit)
            satellite_entry[1].pop("generated", None)
            repair_celestial_region_pairings(satellite_entry[1])
            _validate_celestial_parameters(
                satellite_entry[1], destination, orbit, satellite_orbit
            )
            normalized_satellites.append([satellite_orbit, satellite_entry[1]])
        normalized_satellites.sort(key=lambda value: value[0])
        planet["satellites"] = normalized_satellites
        normalized_planets.append([orbit, planet])
    normalized_planets.sort(key=lambda value: value[0])
    normalized["planets"] = normalized_planets
    validate_finite_json_numbers(normalized, "Celestial system")
    return normalized


def save_celestial_system(
    chunks_path: Path,
    location: Sequence[int],
    editor: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Atomically replace one system in universe.chunks after making a backup."""

    chunks_path = Path(chunks_path)
    source_location = [int(value) for value in location]
    destination_location = [int(value) for value in editor.get("location", [])]
    normalized = normalize_celestial_system_editor(editor, destination_location)
    block_size, records = _load_celestial_database_records(chunks_path)
    source_key, name, version, source_body, trailing = _find_celestial_system_chunk(
        records, source_location
    )
    current_parameters, current_planets = _celestial_system_entry(
        source_body, source_location
    )
    old_name = current_parameters.get("name", "")
    old_planet_count = len(current_planets)

    if (
        destination_location != source_location
        and celestial_system_coordinate_exists(
            chunks_path, destination_location, exclude_location=source_location
        )
    ):
        raise ValueError(
            "目标星系坐标已经被占用："
            + ":".join(map(str, destination_location))
        )

    def remove_system(body: dict[str, Any], target: list[int]) -> None:
        body["systemParameters"] = [
            entry
            for entry in body.get("systemParameters", [])
            if not (isinstance(entry, list) and len(entry) == 2 and entry[0] == target)
        ]
        body["systemObjects"] = [
            entry
            for entry in body.get("systemObjects", [])
            if not (isinstance(entry, list) and len(entry) == 2 and entry[0] == target)
        ]

    def add_system(body: dict[str, Any]) -> None:
        body.setdefault("systemParameters", []).append(
            [list(destination_location), normalized["systemParameters"]]
        )
        body.setdefault("systemObjects", []).append(
            [list(destination_location), normalized["planets"]]
        )

    destination_key = celestial_chunk_key(destination_location)
    record_map = dict(records)
    replacements: dict[bytes, bytes] = {}
    if destination_key == source_key:
        remove_system(source_body, source_location)
        add_system(source_body)
        replacements[source_key] = encode_celestial_chunk(
            name, version, source_body, trailing
        )
    else:
        remove_system(source_body, source_location)
        replacements[source_key] = encode_celestial_chunk(
            name, version, source_body, trailing
        )
        if destination_key in record_map:
            destination_name, destination_version, destination_body, destination_trailing = (
                decode_celestial_chunk(record_map[destination_key])
            )
        else:
            destination_name = "CelestialChunk"
            destination_version = version
            destination_trailing = b""
            destination_body = {
                "chunkIndex": [
                    destination_location[0] // CELESTIAL_CHUNK_SIZE,
                    destination_location[1] // CELESTIAL_CHUNK_SIZE,
                ],
                "constellations": [],
                "systemParameters": [],
                "systemObjects": [],
            }
        add_system(destination_body)
        replacements[destination_key] = encode_celestial_chunk(
            destination_name,
            destination_version,
            destination_body,
            destination_trailing,
        )

    updated_records = [
        (key, replacements.get(key, value)) for key, value in records
    ]
    if destination_key not in record_map:
        updated_records.append((destination_key, replacements[destination_key]))

    temp_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=chunks_path.name + ".",
            suffix=".tmp",
            dir=chunks_path.parent,
            delete=False,
        ) as output:
            temp_file = Path(output.name)
            BTreeDB5Writer(
                block_size, CELESTIAL_IDENTIFIER, CELESTIAL_KEY_SIZE
            ).write(output, updated_records)
            output.flush()
            os.fsync(output.fileno())

        verify_block_size, actual_records = _load_celestial_database_records(temp_file)
        if verify_block_size != block_size or dict(actual_records) != dict(updated_records):
            raise WorldFormatError("重建的 universe.chunks 未通过逐记录校验")
        _key, _name, _version, verified_body, _trailing = _find_celestial_system_chunk(
            actual_records, destination_location
        )
        verified_parameters, verified_planets = _celestial_system_entry(
            verified_body, destination_location
        )
        if (
            verified_parameters != normalized["systemParameters"]
            or verified_planets != normalized["planets"]
        ):
            raise WorldFormatError("重建的星系参数未通过内容校验")
        if destination_location != source_location:
            try:
                _find_celestial_system_chunk(actual_records, source_location)
            except WorldFormatError:
                pass
            else:
                raise WorldFormatError("旧星系坐标仍然存在于重建的数据库中")

        backup = _unique_backup_path(chunks_path)
        shutil.copy2(chunks_path, backup)
        shutil.copystat(chunks_path, temp_file)
        os.replace(temp_file, chunks_path)
        return backup, {
            "oldName": old_name,
            "newName": normalized["systemParameters"].get("name", ""),
            "oldPlanetCount": old_planet_count,
            "newPlanetCount": len(normalized["planets"]),
            "sourceLocation": source_location,
            "location": destination_location,
        }
    finally:
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def json_dump(value: Any, path: Path | None = None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        sys.stdout.write(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def atomic_json_dump(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            prefix=path.name + ".", suffix=".tmp", dir=path.parent,
            delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_metadata_document(
    world: Path,
) -> tuple[BTreeDB5, list[tuple[bytes, bytes]], dict[str, Any]]:
    database, records = load_records(world)
    try:
        document = decode_world_metadata(dict(records)[WORLD_METADATA_KEY])
    except Exception:
        close_database(database)
        raise
    return database, records, document


def load_metadata_only(world: Path) -> dict[str, Any]:
    """Read only the indexed metadata record without walking all world sectors."""

    with world.open("rb") as stream:
        database = BTreeDB5(stream)
        if database.identifier != WORLD_IDENTIFIER or database.key_size != 5:
            raise WorldFormatError(
                f"Expected World4 with 5-byte keys, got "
                f"{database.identifier!r}/{database.key_size}"
            )
        return decode_world_metadata(database.get(WORLD_METADATA_KEY))


def regeneration_world_summary(
    world: Path, assets_folder: Path | None = None
) -> dict[str, Any]:
    document = load_metadata_only(world)
    compiled_groups = [
        {"name": group["name"], "indexes": list(group["indexes"]), "compiled": True}
        for group in grouped_biome_parameters(document)
    ]
    groups = compiled_groups
    asset_summary = None
    if assets_folder is not None:
        catalog = load_asset_biome_catalog(assets_folder)
        indexes_by_name: dict[str, list[int]] = {}
        for group in compiled_groups:
            indexes_by_name.setdefault(str(group["name"]), []).extend(group["indexes"])
        groups = [
            {
                "name": name,
                "indexes": sorted(set(indexes_by_name.get(name, []))),
                "compiled": name in indexes_by_name,
            }
            for name in sorted(catalog["biomes"], key=str.casefold)
        ]
        asset_summary = {
            "mode": catalog["mode"],
            "source": catalog["source"],
            "biomeCount": len(catalog["biomes"]),
            "compiledMatchCount": sum(1 for group in groups if group["compiled"]),
        }
    return {
        "size": [int(document["size"][0]), int(document["size"][1])],
        "biomes": groups,
        "assets": asset_summary,
    }


def write_metadata_document(
    source: Path,
    output: Path,
    database: BTreeDB5,
    records: Sequence[tuple[bytes, bytes]],
    document: dict[str, Any],
) -> None:
    replacement = encode_world_metadata(document)
    updated = [
        (key, replacement if key == WORLD_METADATA_KEY else value)
        for key, value in records
    ]
    atomic_write_world(source, output, updated, database)


def world_template(document: dict[str, Any]) -> dict[str, Any]:
    try:
        value = document["metadata"]["worldTemplate"]
    except (KeyError, TypeError) as exc:
        raise WorldFormatError("WorldMetadata has no worldTemplate map") from exc
    if not isinstance(value, dict):
        raise WorldFormatError("worldTemplate is not a map")
    return value


def compiled_biomes(document: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        value = world_template(document)["regionData"]["biomes"]
    except (KeyError, TypeError) as exc:
        raise WorldFormatError("worldTemplate has no compiled biome list") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise WorldFormatError("Compiled biome list is malformed")
    return value


def biome_at(document: dict[str, Any], index: int) -> dict[str, Any]:
    biomes = compiled_biomes(document)
    if type(index) is not int or not 1 <= index <= len(biomes):
        raise ValueError(f"Biome index must be in 1..{len(biomes)}, got {index}")
    return biomes[index - 1]


def load_material_catalog(assets: Path | None) -> tuple[dict[int, str], dict[str, int]]:
    by_id: dict[int, str] = {}
    by_name: dict[str, int] = {}
    if assets is None:
        return by_id, by_name
    if not assets.is_dir():
        raise ValueError(f"Assets directory does not exist: {assets}")
    id_pattern = re.compile(r'"materialId"\s*:\s*(-?\d+)')
    name_pattern = re.compile(r'"materialName"\s*:\s*"([^"]+)"')
    for path in assets.rglob("*.material"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        id_match = id_pattern.search(text)
        name_match = name_pattern.search(text)
        if not id_match or not name_match:
            continue
        material_id = int(id_match.group(1))
        material_name = name_match.group(1)
        by_id[material_id] = material_name
        by_name[material_name.lower()] = material_id
    return by_id, by_name


def resolve_material(value: str, by_name: dict[str, int]) -> int:
    try:
        return int(value, 10)
    except ValueError:
        pass
    if not by_name:
        raise ValueError(
            f"Material {value!r} is a name; provide --assets so it can be resolved"
        )
    try:
        return by_name[value.lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown material name {value!r}") from exc


def iter_compiled_cells(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    try:
        layers = world_template(document)["regionData"]["layers"]
    except (KeyError, TypeError) as exc:
        raise WorldFormatError("worldTemplate has no compiled region layers") from exc
    for layer in layers:
        for cell in layer.get("cells", []):
            if isinstance(cell, dict):
                yield cell


def biome_summary(
    document: dict[str, Any], by_id: dict[int, str]
) -> list[dict[str, Any]]:
    referenced: dict[int, int] = {}
    for cell in iter_compiled_cells(document):
        index = cell.get("blockBiomeIndex")
        if isinstance(index, int):
            referenced[index] = referenced.get(index, 0) + 1
    result = []
    for index, biome in enumerate(compiled_biomes(document), start=1):
        main_block = biome.get("mainBlock")
        sub_blocks = biome.get("subBlocks", [])
        spawn_profile = biome.get("spawnProfile") or {}
        parallax = biome.get("parallax")
        result.append(
            {
                "index": index,
                "baseName": biome.get("baseName"),
                "description": biome.get("description"),
                "compiledCellReferences": referenced.get(index, 0),
                "mainBlock": {
                    "id": main_block,
                    "name": by_id.get(main_block) if isinstance(main_block, int) else None,
                },
                "subBlocks": [
                    {"id": value, "name": by_id.get(value)}
                    for value in sub_blocks
                    if isinstance(value, int)
                ],
                "spawnTypes": spawn_profile.get("spawnTypes", []),
                "monsterParameters": spawn_profile.get("monsterParameters"),
                "parallaxLayerCount": (
                    len(parallax.get("layers", [])) if isinstance(parallax, dict) else 0
                ),
                "hasParallax": parallax is not None,
            }
        )
    return result


def replace_generated_materials(
    records: Sequence[tuple[bytes, bytes]],
    biome_index: int,
    material_map: dict[int, int],
) -> tuple[list[tuple[bytes, bytes]], dict[str, int]]:
    """Replace foreground/background material IDs in generated tile records."""

    updated: list[tuple[bytes, bytes]] = []
    foreground_changes = 0
    background_changes = 0
    touched_regions = 0
    for key, compressed in records:
        if key[0] != 1:
            updated.append((key, compressed))
            continue
        raw = bytearray(zlib.decompress(compressed))
        _version, offsets = tile_sector_offsets(raw, key)
        region_changed = False
        for offset in offsets:
            tile_biome = raw[offset + 27]
            environment_biome = raw[offset + 28]
            if biome_index not in (tile_biome, environment_biome):
                continue
            foreground = struct.unpack_from(">h", raw, offset)[0]
            background = struct.unpack_from(">h", raw, offset + 7)[0]
            if foreground in material_map:
                struct.pack_into(">h", raw, offset, material_map[foreground])
                foreground_changes += 1
                region_changed = True
            if background in material_map:
                struct.pack_into(">h", raw, offset + 7, material_map[background])
                background_changes += 1
                region_changed = True
        if region_changed:
            compressed = zlib.compress(raw, level=9)
            touched_regions += 1
        updated.append((key, compressed))
    return updated, {
        "regions": touched_regions,
        "foregroundTiles": foreground_changes,
        "backgroundTiles": background_changes,
    }


def world_sector_coordinates(key: bytes) -> tuple[int, int]:
    """Decode the two uint16 coordinates used by World4 sector keys."""

    if len(key) != 5:
        raise WorldFormatError(f"Invalid World4 record key length: {len(key)}")
    return struct.unpack(">HH", key[1:])


def tile_sector_offsets(
    raw: bytes | bytearray, key: bytes | None = None
) -> tuple[int, tuple[int, ...]]:
    """Validate native 417/418 tile serialization and locate all tile starts.

    Both formats retain the same first 31 tile bytes. Version 418 appends an
    optional rootSource Vec2I (8 bytes) after byte 30; it is not a fixed-stride
    array whenever any root is present. Return offsets into the original bytes
    so material/hue edits preserve every other field and all extension bytes.
    Unknown versions, malformed optional flags and trailing bytes fail closed.
    """

    label = f"Tile sector {key.hex()}" if key is not None else "Tile sector"
    try:
        stream = io.BytesIO(raw)
        generation = read_varuint(stream)
        version = read_varuint(stream)
        if generation not in range(6) or version not in (417, 418):
            raise ValueError(f"unsupported generation {generation} / tile version {version}")
        offset = stream.tell()
        offsets: list[int] = []
        for tile_index in range(WORLD_SECTOR_SIZE * WORLD_SECTOR_SIZE):
            offsets.append(offset)
            if offset + 31 > len(raw):
                raise ValueError(f"truncated tile {tile_index}")
            if version == 418:
                flag = raw[offset + 30]
                if flag not in (0, 1):
                    raise ValueError(f"invalid rootSource flag in tile {tile_index}: {flag}")
                offset += 31 + 8 * flag
            else:
                offset += 31
            if offset > len(raw):
                raise ValueError(f"truncated rootSource in tile {tile_index}")
        if offset != len(raw):
            raise ValueError(f"unexpected trailing tile bytes: {len(raw) - offset}")
        return version, tuple(offsets)
    except (EOFError, ValueError) as exc:
        raise WorldFormatError(f"{label}: {exc}") from exc


def _wrapped_x_ranges(x_start: int, x_end: int, world_width: int) -> list[tuple[int, int]]:
    if world_width <= 0:
        raise ValueError("世界宽度必须大于 0")
    if not 0 <= x_start < world_width or not 0 <= x_end < world_width:
        raise ValueError(f"横坐标必须在 0 到 {world_width - 1} 之间")
    if x_start <= x_end:
        return [(x_start, x_end)]
    # Starbound terrestrial worlds wrap horizontally.  A reversed range means
    # [x_start, worldWidth - 1] plus [0, x_end].
    return [(x_start, world_width - 1), (0, x_end)]


def sector_aligned_rectangle(
    world_width: int,
    world_height: int,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
) -> dict[str, Any]:
    """Expand an inclusive tile rectangle to complete 32 by 32 sectors.

    World4 cannot discard only part of a generated sector.  This reports the
    actual deletion extent while callers may still keep biome metadata edits
    on the exact requested tile boundary.  Tiles in a cleared edge sector but
    outside that metadata edit then regenerate from their original recipe.
    """

    world_width = int(world_width)
    world_height = int(world_height)
    x_start = int(x_start)
    x_end = int(x_end)
    y_start = int(y_start)
    y_end = int(y_end)
    ranges = _wrapped_x_ranges(x_start, x_end, world_width)
    if not 0 <= y_start <= y_end < world_height:
        raise ValueError(
            f"纵坐标必须递增并位于 0 到 {world_height - 1} 之间"
        )

    expanded = sorted(
        (
            (start // WORLD_SECTOR_SIZE) * WORLD_SECTOR_SIZE,
            min(
                world_width - 1,
                ((end // WORLD_SECTOR_SIZE) + 1) * WORLD_SECTOR_SIZE - 1,
            ),
        )
        for start, end in ranges
    )
    merged: list[tuple[int, int]] = []
    for start, end in expanded:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    if len(merged) == 1:
        aligned_x_start, aligned_x_end = merged[0]
    elif (
        len(merged) == 2
        and merged[0][0] == 0
        and merged[1][1] == world_width - 1
    ):
        aligned_x_start, aligned_x_end = merged[1][0], merged[0][1]
    else:
        raise WorldFormatError(
            "Sector-aligned wrapped X range could not be represented"
        )

    aligned_y_start = (y_start // WORLD_SECTOR_SIZE) * WORLD_SECTOR_SIZE
    aligned_y_end = min(
        world_height - 1,
        ((y_end // WORLD_SECTOR_SIZE) + 1) * WORLD_SECTOR_SIZE - 1,
    )
    return {
        "xStart": aligned_x_start,
        "xEnd": aligned_x_end,
        "wrapped": aligned_x_start > aligned_x_end,
        "xRanges": _wrapped_x_ranges(
            aligned_x_start, aligned_x_end, world_width
        ),
        "yStart": aligned_y_start,
        "yEnd": aligned_y_end,
        "sectorSize": WORLD_SECTOR_SIZE,
    }


def _sector_intersects_x_ranges(
    sector_x: int, ranges: Sequence[tuple[int, int]], world_width: int
) -> bool:
    sector_min = sector_x * WORLD_SECTOR_SIZE
    sector_max = min(world_width - 1, sector_min + WORLD_SECTOR_SIZE - 1)
    return any(sector_min <= end and sector_max >= start for start, end in ranges)


def _dominant_style(
    counts: dict[tuple[int, ...] | int, int]
) -> tuple[int, ...] | int:
    return min(counts, key=lambda style: (-counts[style], style))


def _reference_tile_style_maps(
    records: Sequence[tuple[bytes, bytes]],
    world_width: int,
    world_height: int,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    biome_material_hues: Sequence[int] | None = None,
) -> tuple[dict[str, dict[Any, Any]], dict[str, int]]:
    """Collect dominant per-material biome-hue/color styles from saved tiles.

    Natural terrain stores its final material hue in each tile, but materials
    placed by a biome feature may keep a local hue of zero.  Hue-only copying
    is defined in terms of the reference biome, so high-level callers provide
    the compiled biome ``materialHueShift`` table.  The raw tile hue remains a
    fallback for invalid/legacy biome indexes and low-level tests.
    """

    ranges = _wrapped_x_ranges(int(x_start), int(x_end), int(world_width))
    y_start = int(y_start)
    y_end = int(y_end)
    if not 0 <= y_start <= y_end < int(world_height):
        raise ValueError(
            f"纵坐标必须递增并位于 0 到 {int(world_height) - 1} 之间"
        )

    foreground: dict[int, dict[tuple[int, int], int]] = {}
    background: dict[int, dict[tuple[int, int], int]] = {}
    foreground_mod: dict[tuple[int, int], dict[int, int]] = {}
    background_mod: dict[tuple[int, int], dict[int, int]] = {}
    biome_hue_counts: dict[int, int] = {}
    scanned_tiles = 0
    tile_records = 0

    def count_style(mapping: dict[Any, dict[Any, int]], key: Any, style: Any) -> None:
        styles = mapping.setdefault(key, {})
        styles[style] = styles.get(style, 0) + 1

    for key, compressed in records:
        if key[0] != STORE_TILE_SECTOR:
            continue
        sector_x, sector_y = world_sector_coordinates(key)
        if not _sector_intersects_x_ranges(sector_x, ranges, int(world_width)):
            continue
        sector_y_start = sector_y * WORLD_SECTOR_SIZE
        sector_y_end = min(
            int(world_height) - 1,
            sector_y_start + WORLD_SECTOR_SIZE - 1,
        )
        if sector_y_start > y_end or sector_y_end < y_start:
            continue
        raw = zlib.decompress(compressed)
        _version, offsets = tile_sector_offsets(raw, key)
        tile_records += 1
        sector_x_start = sector_x * WORLD_SECTOR_SIZE
        for local_y in range(WORLD_SECTOR_SIZE):
            world_y = sector_y_start + local_y
            if not y_start <= world_y <= y_end:
                continue
            for local_x in range(WORLD_SECTOR_SIZE):
                world_x = sector_x_start + local_x
                if world_x >= int(world_width) or not any(
                    start <= world_x <= end for start, end in ranges
                ):
                    continue
                scanned_tiles += 1
                offset = offsets[local_y * WORLD_SECTOR_SIZE + local_x]
                foreground_material = struct.unpack_from(">h", raw, offset)[0]
                background_material = struct.unpack_from(">h", raw, offset + 7)[0]
                biome_index = raw[offset + 27]
                biome_hue = None
                if (
                    biome_material_hues is not None
                    and 1 <= biome_index <= len(biome_material_hues)
                ):
                    biome_hue = int(biome_material_hues[biome_index - 1]) & 0xFF
                    biome_hue_counts[biome_hue] = (
                        biome_hue_counts.get(biome_hue, 0) + 1
                    )
                if foreground_material > 0:
                    count_style(
                        foreground,
                        foreground_material,
                        (
                            biome_hue
                            if biome_hue is not None
                            else raw[offset + 2],
                            raw[offset + 3],
                        ),
                    )
                    foreground_mod_id = struct.unpack_from(">h", raw, offset + 4)[0]
                    if foreground_mod_id > 0:
                        count_style(
                            foreground_mod,
                            (foreground_material, foreground_mod_id),
                            raw[offset + 6],
                        )
                if background_material > 0:
                    count_style(
                        background,
                        background_material,
                        (
                            biome_hue
                            if biome_hue is not None
                            else raw[offset + 9],
                            raw[offset + 10],
                        ),
                    )
                    background_mod_id = struct.unpack_from(">h", raw, offset + 11)[0]
                    if background_mod_id > 0:
                        count_style(
                            background_mod,
                            (background_material, background_mod_id),
                            raw[offset + 13],
                        )

    if tile_records == 0 or scanned_tiles == 0:
        raise ValueError(
            "参考矩形内没有已保存的 tile sector；Hue shift only "
            "只能从已生成的区域读取物块颜色"
        )
    maps: dict[str, dict[Any, Any]] = {
        "foreground": {
            material: _dominant_style(styles)
            for material, styles in foreground.items()
        },
        "background": {
            material: _dominant_style(styles)
            for material, styles in background.items()
        },
        "foregroundMod": {
            pair: _dominant_style(styles)
            for pair, styles in foreground_mod.items()
        },
        "backgroundMod": {
            pair: _dominant_style(styles)
            for pair, styles in background_mod.items()
        },
    }
    return maps, {
        "referenceTileRecords": tile_records,
        "referenceTilesScanned": scanned_tiles,
        "referenceForegroundMaterials": len(maps["foreground"]),
        "referenceBackgroundMaterials": len(maps["background"]),
        "referenceForegroundModPairs": len(maps["foregroundMod"]),
        "referenceBackgroundModPairs": len(maps["backgroundMod"]),
        "referenceHueSource": (
            "compiledBiomeMaterialHueShift"
            if biome_material_hues is not None
            else "savedTileHue"
        ),
        "referenceBiomeMaterialHues": sorted(biome_hue_counts),
        "referenceDominantBiomeMaterialHue": (
            _dominant_style(biome_hue_counts) if biome_hue_counts else None
        ),
    }


def apply_reference_hue_to_matching_materials(
    records: Sequence[tuple[bytes, bytes]],
    world_width: int,
    world_height: int,
    reference_x_start: int,
    reference_x_end: int,
    reference_y_start: int,
    reference_y_end: int,
    target_x_start: int,
    target_x_end: int,
    target_y_start: int,
    target_y_end: int,
    biome_material_hues: Sequence[int] | None = None,
) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    """Copy dominant reference-biome hue/color to matching target materials.

    Foreground and background material IDs are never changed.  Material hue
    and color-variant bytes are copied independently; material-mod hue is only
    copied when both the material ID and mod ID match the reference.  No
    sectors or entity records are removed.
    """

    style_maps, reference_stats = _reference_tile_style_maps(
        records,
        world_width,
        world_height,
        reference_x_start,
        reference_x_end,
        reference_y_start,
        reference_y_end,
        biome_material_hues,
    )
    ranges = _wrapped_x_ranges(
        int(target_x_start), int(target_x_end), int(world_width)
    )
    target_y_start = int(target_y_start)
    target_y_end = int(target_y_end)
    if not 0 <= target_y_start <= target_y_end < int(world_height):
        raise ValueError(
            f"纵坐标必须递增并位于 0 到 {int(world_height) - 1} 之间"
        )

    updated: list[tuple[bytes, bytes]] = []
    target_records = 0
    target_tiles = 0
    touched_records = 0
    foreground_matches = 0
    background_matches = 0
    foreground_changes = 0
    background_changes = 0
    foreground_mod_changes = 0
    background_mod_changes = 0
    for key, compressed in records:
        if key[0] != STORE_TILE_SECTOR:
            updated.append((key, compressed))
            continue
        sector_x, sector_y = world_sector_coordinates(key)
        if not _sector_intersects_x_ranges(sector_x, ranges, int(world_width)):
            updated.append((key, compressed))
            continue
        sector_y_start = sector_y * WORLD_SECTOR_SIZE
        sector_y_end = min(
            int(world_height) - 1,
            sector_y_start + WORLD_SECTOR_SIZE - 1,
        )
        if sector_y_start > target_y_end or sector_y_end < target_y_start:
            updated.append((key, compressed))
            continue

        raw = bytearray(zlib.decompress(compressed))
        _version, offsets = tile_sector_offsets(raw, key)
        target_records += 1
        record_changed = False
        sector_x_start = sector_x * WORLD_SECTOR_SIZE
        for local_y in range(WORLD_SECTOR_SIZE):
            world_y = sector_y_start + local_y
            if not target_y_start <= world_y <= target_y_end:
                continue
            for local_x in range(WORLD_SECTOR_SIZE):
                world_x = sector_x_start + local_x
                if world_x >= int(world_width) or not any(
                    start <= world_x <= end for start, end in ranges
                ):
                    continue
                target_tiles += 1
                offset = offsets[local_y * WORLD_SECTOR_SIZE + local_x]
                foreground_material = struct.unpack_from(">h", raw, offset)[0]
                foreground_style = style_maps["foreground"].get(
                    foreground_material
                )
                if foreground_style is not None:
                    foreground_matches += 1
                    hue, color = foreground_style
                    if raw[offset + 2] != hue or raw[offset + 3] != color:
                        raw[offset + 2] = hue
                        raw[offset + 3] = color
                        foreground_changes += 1
                        record_changed = True
                    foreground_mod_id = struct.unpack_from(">h", raw, offset + 4)[0]
                    mod_hue = style_maps["foregroundMod"].get(
                        (foreground_material, foreground_mod_id)
                    )
                    if mod_hue is not None and raw[offset + 6] != mod_hue:
                        raw[offset + 6] = mod_hue
                        foreground_mod_changes += 1
                        record_changed = True

                background_material = struct.unpack_from(">h", raw, offset + 7)[0]
                background_style = style_maps["background"].get(
                    background_material
                )
                if background_style is not None:
                    background_matches += 1
                    hue, color = background_style
                    if raw[offset + 9] != hue or raw[offset + 10] != color:
                        raw[offset + 9] = hue
                        raw[offset + 10] = color
                        background_changes += 1
                        record_changed = True
                    background_mod_id = struct.unpack_from(">h", raw, offset + 11)[0]
                    mod_hue = style_maps["backgroundMod"].get(
                        (background_material, background_mod_id)
                    )
                    if mod_hue is not None and raw[offset + 13] != mod_hue:
                        raw[offset + 13] = mod_hue
                        background_mod_changes += 1
                        record_changed = True

        if record_changed:
            compressed = zlib.compress(raw, level=9)
            touched_records += 1
        updated.append((key, compressed))

    if target_records == 0 or target_tiles == 0:
        raise ValueError(
            "目标矩形内没有已保存的 tile sector；Hue shift only "
            "不会生成未探索区域"
        )
    result: dict[str, Any] = {
        **reference_stats,
        "targetTileRecords": target_records,
        "targetTilesScanned": target_tiles,
        "touchedTileRecords": touched_records,
        "foregroundMatches": foreground_matches,
        "backgroundMatches": background_matches,
        "foregroundTilesChanged": foreground_changes,
        "backgroundTilesChanged": background_changes,
        "foregroundModHueChanged": foreground_mod_changes,
        "backgroundModHueChanged": background_mod_changes,
        "hueShiftOnly": True,
        "removedTileRecords": 0,
        "removedEntityRecords": 0,
        "removedSectorUniqueRecords": 0,
        "removedUniqueIndexEntries": 0,
        "resetSectors": 0,
    }
    return updated, result


def _tile_sector_contains_biome_in_x_ranges(
    key: bytes,
    compressed: bytes,
    ranges: Sequence[tuple[int, int]],
    biome_indexes: set[int],
    world_width: int,
    y_start: int | None = None,
    y_end: int | None = None,
) -> bool:
    sector_x, sector_y = world_sector_coordinates(key)
    sector_min = sector_x * WORLD_SECTOR_SIZE
    sector_y_min = sector_y * WORLD_SECTOR_SIZE
    raw = zlib.decompress(compressed)
    _version, offsets = tile_sector_offsets(raw, key)
    for local_y in range(WORLD_SECTOR_SIZE):
        world_y = sector_y_min + local_y
        if y_start is not None and y_end is not None and not y_start <= world_y <= y_end:
            continue
        for local_x in range(WORLD_SECTOR_SIZE):
            world_x = sector_min + local_x
            if world_x >= world_width or not any(start <= world_x <= end for start, end in ranges):
                continue
            tile_index = local_y * WORLD_SECTOR_SIZE + local_x
            offset = offsets[tile_index]
            if raw[offset + 27] in biome_indexes or raw[offset + 28] in biome_indexes:
                return True
    return False


def read_unique_index_store(compressed: bytes) -> list[tuple[str, tuple[int, int], tuple[float, float]]]:
    """Decode a World4 type-3 unique-entity index bucket."""

    stream = io.BytesIO(zlib.decompress(compressed))
    entries = []
    for _ in range(read_varuint(stream)):
        unique_id = read_string(stream)
        sector = struct.unpack(">HH", _read_exact(stream, 4))
        position = struct.unpack(">ff", _read_exact(stream, 8))
        entries.append((unique_id, sector, position))
    if stream.read(1):
        raise WorldFormatError("Unexpected trailing data in unique-entity index")
    return entries


def write_unique_index_store(
    entries: Sequence[tuple[str, tuple[int, int], tuple[float, float]]]
) -> bytes:
    stream = io.BytesIO()
    write_varuint(stream, len(entries))
    for unique_id, sector, position in entries:
        write_string(stream, unique_id)
        stream.write(struct.pack(">HHff", sector[0], sector[1], position[0], position[1]))
    return zlib.compress(stream.getvalue(), level=9)


def reset_generated_biome_x_range(
    records: Sequence[tuple[bytes, bytes]],
    world_width: int,
    x_start: int,
    x_end: int,
    biome_indexes: Iterable[int],
    y_start: int | None = None,
    y_end: int | None = None,
    world_height: int | None = None,
) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    """Remove generated sectors for selected biomes so Starbound regenerates them.

    Deletion is necessarily sector-aligned (32 by 32 tiles).  Tile, entity and
    sector-unique records are removed together, and type-3 unique-index buckets
    are rewritten to remove only entries pointing into the reset sectors.
    """

    indexes = {int(index) for index in biome_indexes}
    if not indexes or any(index < 1 or index > 255 for index in indexes):
        raise ValueError("biome index 必须是 1 到 255 之间的整数")
    ranges = _wrapped_x_ranges(int(x_start), int(x_end), int(world_width))
    if (y_start is None) != (y_end is None):
        raise ValueError("Y 起点和终点必须同时提供")
    if y_start is not None and y_end is not None:
        if world_height is None or world_height <= 0:
            raise ValueError("使用 Y 范围时必须提供有效世界高度")
        y_start = int(y_start)
        y_end = int(y_end)
        if not 0 <= y_start <= y_end < world_height:
            raise ValueError(f"纵坐标必须递增并位于 0 到 {world_height - 1} 之间")

    selected: set[tuple[int, int]] = set()
    candidate_tile_records = 0
    for key, value in records:
        if key[0] != STORE_TILE_SECTOR:
            continue
        sector = world_sector_coordinates(key)
        if not _sector_intersects_x_ranges(sector[0], ranges, world_width):
            continue
        if y_start is not None and y_end is not None:
            sector_y_min = sector[1] * WORLD_SECTOR_SIZE
            sector_y_max = min(world_height - 1, sector_y_min + WORLD_SECTOR_SIZE - 1)
            if sector_y_min > y_end or sector_y_max < y_start:
                continue
        candidate_tile_records += 1
        if not _tile_sector_contains_biome_in_x_ranges(
            key, value, ranges, indexes, world_width, y_start, y_end
        ):
            continue
        selected.add(sector)

    if not selected:
        raise ValueError(
            "所选横坐标范围内没有找到属于该 biome 的已生成 sector；"
            "请检查 X 范围和 biome index"
        )

    removed_by_type = {
        STORE_TILE_SECTOR: 0,
        STORE_ENTITY_SECTOR: 0,
        STORE_SECTOR_UNIQUES: 0,
    }
    removed_unique_index_entries = 0
    rewritten_unique_index_records = 0
    updated: list[tuple[bytes, bytes]] = []
    for key, value in records:
        store_type = key[0]
        if store_type in removed_by_type and world_sector_coordinates(key) in selected:
            removed_by_type[store_type] += 1
            continue
        if store_type == STORE_UNIQUE_INDEX:
            entries = read_unique_index_store(value)
            kept = [entry for entry in entries if entry[1] not in selected]
            removed_unique_index_entries += len(entries) - len(kept)
            if not kept:
                continue
            if len(kept) != len(entries):
                value = write_unique_index_store(kept)
                rewritten_unique_index_records += 1
        updated.append((key, value))

    return updated, {
        "xStart": int(x_start),
        "xEnd": int(x_end),
        "wrapped": int(x_start) > int(x_end),
        "yStart": y_start,
        "yEnd": y_end,
        "biomeIndexes": sorted(indexes),
        "candidateTileRecords": candidate_tile_records,
        "resetSectors": len(selected),
        "removedTileRecords": removed_by_type[STORE_TILE_SECTOR],
        "removedEntityRecords": removed_by_type[STORE_ENTITY_SECTOR],
        "removedSectorUniqueRecords": removed_by_type[STORE_SECTOR_UNIQUES],
        "removedUniqueIndexEntries": removed_unique_index_entries,
        "rewrittenUniqueIndexRecords": rewritten_unique_index_records,
        "sectorSize": WORLD_SECTOR_SIZE,
    }


def reset_generated_rectangle(
    records: Sequence[tuple[bytes, bytes]],
    world_width: int,
    world_height: int,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    allow_empty: bool = False,
) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    """Remove every generated sector intersecting an inclusive tile rectangle."""

    ranges = _wrapped_x_ranges(int(x_start), int(x_end), int(world_width))
    y_start = int(y_start)
    y_end = int(y_end)
    if not 0 <= y_start <= y_end < world_height:
        raise ValueError(f"纵坐标必须递增并位于 0 到 {world_height - 1} 之间")

    selected: set[tuple[int, int]] = set()
    for key, _value in records:
        if key[0] != STORE_TILE_SECTOR:
            continue
        sector = world_sector_coordinates(key)
        if not _sector_intersects_x_ranges(sector[0], ranges, world_width):
            continue
        sector_y_min = sector[1] * WORLD_SECTOR_SIZE
        sector_y_max = min(world_height - 1, sector_y_min + WORLD_SECTOR_SIZE - 1)
        if sector_y_min <= y_end and sector_y_max >= y_start:
            selected.add(sector)

    if not selected and not allow_empty:
        raise ValueError("所选矩形内没有找到已生成的 sector；请检查 X/Y 范围")

    removed_by_type = {
        STORE_TILE_SECTOR: 0,
        STORE_ENTITY_SECTOR: 0,
        STORE_SECTOR_UNIQUES: 0,
    }
    removed_unique_index_entries = 0
    rewritten_unique_index_records = 0
    updated: list[tuple[bytes, bytes]] = []
    for key, value in records:
        store_type = key[0]
        if store_type in removed_by_type and world_sector_coordinates(key) in selected:
            removed_by_type[store_type] += 1
            continue
        if store_type == STORE_UNIQUE_INDEX:
            entries = read_unique_index_store(value)
            kept = [entry for entry in entries if entry[1] not in selected]
            removed_unique_index_entries += len(entries) - len(kept)
            if not kept:
                continue
            if len(kept) != len(entries):
                value = write_unique_index_store(kept)
                rewritten_unique_index_records += 1
        updated.append((key, value))

    return updated, {
        "xStart": int(x_start),
        "xEnd": int(x_end),
        "wrapped": int(x_start) > int(x_end),
        "yStart": y_start,
        "yEnd": y_end,
        "resetSectors": len(selected),
        "removedTileRecords": removed_by_type[STORE_TILE_SECTOR],
        "removedEntityRecords": removed_by_type[STORE_ENTITY_SECTOR],
        "removedSectorUniqueRecords": removed_by_type[STORE_SECTOR_UNIQUES],
        "removedUniqueIndexEntries": removed_unique_index_entries,
        "rewrittenUniqueIndexRecords": rewritten_unique_index_records,
        "sectorSize": WORLD_SECTOR_SIZE,
    }


def _stable_asset_seed(*parts: Any) -> int:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _material_hue_from_degrees(degrees: float) -> int:
    return int(float(degrees) * 255.0 / 360.0) & 0xFF


def _weighted_unique_choices(pool: Any, count: int, rng: random.Random) -> list[str]:
    candidates: list[tuple[float, str]] = []
    if isinstance(pool, list):
        for entry in pool:
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], (int, float))
                and isinstance(entry[1], str)
                and float(entry[0]) > 0
            ):
                candidates.append((float(entry[0]), entry[1]))
    selected: list[str] = []
    while candidates and len(selected) < max(0, int(count)):
        total = sum(weight for weight, _name in candidates)
        marker = rng.random() * total
        chosen_index = len(candidates) - 1
        for index, (weight, _name) in enumerate(candidates):
            marker -= weight
            if marker <= 0:
                chosen_index = index
                break
        _weight, name = candidates.pop(chosen_index)
        if name not in selected:
            selected.append(name)
    return selected


class AssetBiomeCompiler:
    """Compile a usable stored Biome object from a raw assets .biome.

    Starbound normally performs this conversion in BiomeDatabase::createBiome
    when a terraformer calls world.addBiomeRegion.  The stored world cannot
    point at the .biome name directly; it needs the resolved object below.
    """

    def __init__(self, assets_folder: Path):
        self.reader = StarboundAssetReader(Path(assets_folder))
        self.catalog = load_asset_biome_catalog(Path(assets_folder))
        self.material_ids: dict[str, int] = {}
        self.mod_ids: dict[str, int] = {}
        self.liquid_ids: dict[str, int] = {}
        self.terrain_configs: dict[str, tuple[str, dict[str, Any]]] = {}
        self.plant_configs: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {
            "grass": {},
            "bush": {},
            "modularstem": {},
            "modularfoliage": {},
        }
        for suffix, name_key, id_key, destination in (
            (".material", "materialName", "materialId", self.material_ids),
            (".matmod", "modName", "modId", self.mod_ids),
        ):
            for asset_path in self.reader.paths(suffix):
                try:
                    document = self.reader.json(asset_path)
                except Exception:
                    continue
                name = document.get(name_key) if isinstance(document, dict) else None
                identifier = document.get(id_key) if isinstance(document, dict) else None
                if isinstance(name, str) and isinstance(identifier, int):
                    destination[name.lower()] = identifier
        for asset_path in self.reader.paths(".liquid"):
            try:
                document = self.reader.json(asset_path)
            except Exception:
                continue
            if not isinstance(document, dict):
                continue
            name = document.get("name")
            identifier = document.get("liquidId")
            if isinstance(name, str) and isinstance(identifier, int):
                self.liquid_ids[name.lower()] = identifier
        for suffix in (".terrain", ".ridgeblocks"):
            for asset_path in self.reader.paths(suffix):
                try:
                    document = self.reader.json(asset_path)
                except Exception:
                    continue
                name = document.get("name") if isinstance(document, dict) else None
                if isinstance(name, str):
                    selector_type = (
                        str(document.get("type", ""))
                        if suffix == ".terrain"
                        else "ridgeblocks"
                    )
                    self.terrain_configs[name.lower()] = (
                        selector_type,
                        deep_copy_json(document),
                    )
        for extension in self.plant_configs:
            for asset_path in self.reader.paths("." + extension):
                try:
                    document = self.reader.json(asset_path)
                except Exception:
                    continue
                name = document.get("name") if isinstance(document, dict) else None
                if isinstance(name, str):
                    directory = posixpath.dirname(asset_path) + "/"
                    self.plant_configs[extension][name.lower()] = (
                        directory,
                        deep_copy_json(document),
                    )
        try:
            spawning = self.reader.json("/spawning.config")
            self.spawn_groups = spawning.get("spawnGroups", {})
        except Exception:
            self.spawn_groups = {}
        try:
            terrestrial = self.reader.json("/terrestrial_worlds.config")
        except Exception:
            terrestrial = {}
        self.region_defaults = (
            terrestrial.get("regionDefaults", {})
            if isinstance(terrestrial, dict)
            else {}
        )
        self.region_types = (
            terrestrial.get("regionTypes", {})
            if isinstance(terrestrial, dict)
            else {}
        )
        try:
            self.ore_distributions = self.reader.json(
                "/biomes/oredistributions.configfunctions"
            )
        except Exception:
            self.ore_distributions = {}

    def _material(self, name: str) -> int:
        try:
            return self.material_ids[name.lower()]
        except KeyError as exc:
            raise ValueError(f"Biome references unknown material {name!r}") from exc

    def _mod(self, name: str) -> int:
        try:
            return self.mod_ids[name.lower()]
        except KeyError as exc:
            raise ValueError(f"Biome references unknown material mod {name!r}") from exc

    def _liquid(self, name: str) -> int:
        try:
            return self.liquid_ids[name.lower()]
        except KeyError as exc:
            raise ValueError(f"Biome region references unknown liquid {name!r}") from exc

    def _tile_damage(self, reference: Any, health: Any) -> dict[str, Any]:
        path = reference if isinstance(reference, str) else "/plants/grassDamage.config"
        try:
            result = deep_copy_json(self.reader.json(path))
        except Exception:
            result = {}
        if not isinstance(result, dict):
            result = {}
        result["requiredHarvestLevel"] = 1
        result["totalHealth"] = float(health)
        return result

    @staticmethod
    def _descriptions(settings: dict[str, Any], fallback: str) -> dict[str, Any]:
        descriptions = {
            key: deep_copy_json(value)
            for key, value in settings.items()
            if key.endswith("Description")
        }
        descriptions["description"] = str(settings.get("description", fallback))
        return descriptions

    def _plant(self, kind: str, name: str) -> tuple[str, dict[str, Any]]:
        try:
            return self.plant_configs[kind][name.lower()]
        except KeyError as exc:
            raise ValueError(f"Biome references unknown {kind} {name!r}") from exc

    def _grass_variant(self, name: str, hue_shift: float) -> dict[str, Any]:
        directory, settings = self._plant("grass", name)
        return {
            "name": name,
            "directory": directory,
            "images": deep_copy_json(settings.get("images", [])),
            "hueShift": float(hue_shift),
            "descriptions": self._descriptions(settings, name),
            "ceiling": bool(settings.get("ceiling", False)),
            "ephemeral": bool(settings.get("ephemeral", True)),
            "tileDamageParameters": self._tile_damage(
                settings.get("damageTable", "/plants/grassDamage.config"),
                settings.get("health", 1.0),
            ),
        }

    def _bush_variant(
        self, settings: dict[str, Any], rng: random.Random
    ) -> dict[str, Any]:
        name = str(settings["name"])
        directory, config = self._plant("bush", name)
        mods = config.get("mods", [])
        mod_name = rng.choice(mods) if isinstance(mods, list) and mods else ""
        shapes: list[list[Any]] = []
        for shape in config.get("shapes", []):
            if not isinstance(shape, dict) or not isinstance(shape.get("base"), str):
                continue
            shape_mods = shape.get("mods", {})
            chosen_mods = (
                deep_copy_json(shape_mods.get(mod_name, []))
                if mod_name and isinstance(shape_mods, dict)
                else []
            )
            shapes.append([shape["base"], chosen_mods])
        return {
            "bushName": name,
            "modName": mod_name,
            "directory": directory,
            "shapes": shapes,
            "baseHueShift": rng.uniform(-1.0, 1.0)
            * float(settings.get("baseHueShiftMax", 0.0)),
            "modHueShift": rng.uniform(-1.0, 1.0)
            * float(settings.get("modHueShiftMax", 0.0)),
            "descriptions": self._descriptions(config, name + " with " + mod_name),
            "ceiling": bool(config.get("ceiling", False)),
            "ephemeral": bool(config.get("ephemeral", True)),
            "tileDamageParameters": self._tile_damage(
                config.get("damageTable", "/plants/bushDamage.config"),
                config.get("health", 1.0),
            ),
        }

    def _tree_variant(
        self,
        stem_name: str,
        stem_hue: float,
        foliage_name: str,
        foliage_hue: float,
    ) -> dict[str, Any]:
        stem_directory, stem = self._plant("modularstem", stem_name)
        if foliage_name:
            foliage_directory, foliage = self._plant(
                "modularfoliage", foliage_name
            )
        else:
            foliage_directory, foliage = "", {}
        return {
            "stemName": stem_name,
            "foliageName": foliage_name,
            "stemDirectory": stem_directory,
            "stemSettings": deep_copy_json(stem),
            "stemHueShift": float(stem_hue),
            "foliageDirectory": foliage_directory,
            "foliageSettings": deep_copy_json(foliage),
            "foliageHueShift": float(foliage_hue),
            "descriptions": self._descriptions(
                stem, stem_name + (" with " + foliage_name if foliage_name else "")
            ),
            "ceiling": bool(stem.get("ceiling", False)),
            "ephemeral": bool(
                stem.get(
                    "allowsBlockPlacement" if foliage_name else "ephemeral", False
                )
            ),
            "stemDropConfig": deep_copy_json(stem.get("dropConfig", {})),
            "foliageDropConfig": deep_copy_json(foliage.get("dropConfig", {})),
            "tileDamageParameters": self._tile_damage(
                stem.get("damageTable", "/plants/treeDamage.config"),
                stem.get("health", 1.0),
            ),
        }

    def _biome_item(
        self, config: dict[str, Any], rng: random.Random, hue_shift: float
    ) -> list[Any] | None:
        kind = str(config.get("type", "")).lower()
        if kind == "grass":
            names = [name for name in config.get("grasses", []) if isinstance(name, str)]
            if names:
                return ["grass", self._grass_variant(rng.choice(names), hue_shift)]
        elif kind == "bush":
            bushes = [item for item in config.get("bushes", []) if isinstance(item, dict)]
            if bushes:
                return ["bush", self._bush_variant(rng.choice(bushes), rng)]
        elif kind == "tree":
            stems = [name for name in config.get("treeStemList", []) if isinstance(name, str)]
            foliages = [name for name in config.get("treeFoliageList", []) if isinstance(name, str)]
            if not foliages:
                foliages = [""]
            pairs: list[tuple[str, str]] = []
            for stem_name in stems:
                _directory, stem = self._plant("modularstem", stem_name)
                stem_shape = stem.get("shape")
                for foliage_name in foliages:
                    if not foliage_name:
                        pairs.append((stem_name, ""))
                        continue
                    _fdir, foliage = self._plant("modularfoliage", foliage_name)
                    if stem_shape == foliage.get("shape"):
                        pairs.append((stem_name, foliage_name))
            if pairs:
                stem_name, foliage_name = rng.choice(pairs)
                stem_hue = rng.uniform(-1.0, 1.0) * float(
                    config.get("treeStemHueShiftMax", 0.0)
                )
                maximum = float(config.get("treeFoliageHueShiftMax", 0.0))
                primary = self._tree_variant(
                    stem_name,
                    stem_hue,
                    foliage_name,
                    rng.uniform(-1.0, 1.0) * maximum,
                )
                alternate = self._tree_variant(
                    stem_name,
                    stem_hue,
                    foliage_name,
                    rng.uniform(-1.0, 1.0) * maximum,
                )
                return ["treePair", [primary, alternate]]
        elif kind == "object":
            sets = [item for item in config.get("objectSets", []) if isinstance(item, dict)]
            if sets:
                chosen = rng.choice(sets)
                parameters = (
                    deep_copy_json(chosen.get("parameters", {}))
                    if isinstance(chosen.get("parameters", {}), dict)
                    else {}
                )
                pool = []
                for pair in chosen.get("pool", []):
                    if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[1], str):
                        pool.append([float(pair[0]), [pair[1], deep_copy_json(parameters)]])
                return ["objectPool", pool]
        elif kind == "treasurebox":
            names = [name for name in config.get("treasureBoxSets", []) if isinstance(name, str)]
            if names:
                return ["treasureBoxSet", rng.choice(names)]
        elif kind == "microdungeon":
            names = sorted(
                {name for name in config.get("microdungeons", []) if isinstance(name, str)}
            )
            if names:
                return ["microDungeon", names]
        return None

    @staticmethod
    def _perlin(
        seed: int,
        octaves: int,
        frequency: float,
        amplitude: float,
        bias: float,
        alpha: float,
        beta: float,
    ) -> dict[str, Any]:
        return {
            "seed": int(seed),
            "bias": float(bias),
            "alpha": float(alpha),
            "offset": 1.0,
            "octaves": int(octaves),
            "amplitude": float(amplitude),
            "type": "perlin",
            "beta": float(beta),
            "frequency": float(frequency),
            "gain": 2.0,
        }

    @staticmethod
    def _empty_perlin() -> dict[str, Any]:
        return {
            "seed": 0,
            "bias": 0.0,
            "alpha": 0.0,
            "offset": 0.0,
            "octaves": 0,
            "amplitude": 0.0,
            "type": "uninitialized",
            "beta": 0.0,
            "frequency": 0.0,
            "gain": 0.0,
        }

    def _item_distribution(
        self,
        config: dict[str, Any],
        biome_path: str,
        rng: random.Random,
        hue_shift: float,
    ) -> dict[str, Any]:
        distribution = config.get("distribution", {})
        if isinstance(distribution, str):
            distribution = self.reader.json(distribution, relative_to=biome_path)
        if not isinstance(distribution, dict):
            raise ValueError("Biome item distribution is not an object")
        distribution_type = str(distribution.get("type", "")).lower()
        variants = max(1, int(config.get("variants", 1)))
        result = {
            "mode": str(config.get("mode", "floor")),
            "distribution": distribution_type,
            "priority": float(config.get("priority", 0.0)),
            "blockProbability": 0.0,
            "blockSeed": 0,
            "randomItems": [],
            "densityFunction": self._empty_perlin(),
            "modulusDistortion": self._empty_perlin(),
            "modulus": 1,
            "modulusOffset": 0,
            "weightedItems": [],
        }
        if distribution_type == "random":
            result["blockProbability"] = float(distribution.get("blockProbability", 0.0))
            result["blockSeed"] = rng.getrandbits(63)
            for _index in range(variants):
                item = self._biome_item(config, rng, hue_shift)
                if item is not None:
                    result["randomItems"].append(item)
        elif distribution_type == "periodic":
            octaves = int(distribution.get("octaves", 1))
            alpha = float(distribution.get("alpha", 2.0))
            beta = float(distribution.get("beta", 2.0))
            modulus = max(1, int(distribution.get("modulus", 1)))
            variance = float(distribution.get("modulusVariance", 0.0))
            density_period = float(distribution.get("densityPeriod", 10.0))
            density_offset = float(distribution.get("densityOffset", 2.0))
            type_period = float(distribution.get("typePeriod", 10.0))
            result["modulus"] = modulus
            result["modulusOffset"] = rng.randint(-modulus, modulus)
            result["densityFunction"] = self._perlin(
                rng.getrandbits(63), octaves, 1.0 / density_period, 1.0,
                density_offset, alpha, beta
            )
            result["modulusDistortion"] = self._perlin(
                rng.getrandbits(63), octaves, 1.0 / modulus, variance,
                variance * 2.0, alpha, beta
            )
            for _index in range(variants):
                item = self._biome_item(config, rng, hue_shift)
                if item is not None:
                    result["weightedItems"].append(
                        [
                            item,
                            self._perlin(
                                rng.getrandbits(63), octaves, 1.0 / type_period,
                                1.0, 0.0, alpha, beta
                            ),
                        ]
                    )
        else:
            raise ValueError(f"Unsupported biome distribution type {distribution_type!r}")
        return result

    def _placeables(
        self,
        config: Any,
        biome_path: str,
        rng: random.Random,
        hue_shift: float,
    ) -> tuple[dict[str, Any], list[str]]:
        config = config if isinstance(config, dict) else {}
        warnings: list[str] = []

        def choose_mod(key: str) -> int:
            names = config.get(key, [])
            if not isinstance(names, list) or not names:
                return 65535
            choices = [name for name in names if isinstance(name, str)]
            return self._mod(rng.choice(choices)) if choices else 65535

        item_distributions: list[dict[str, Any]] = []
        for item in config.get("items", []):
            if not isinstance(item, dict):
                continue
            try:
                item_distributions.append(
                    self._item_distribution(item, biome_path, rng, hue_shift)
                )
            except Exception as exc:
                warnings.append(
                    f"placeable {item.get('type', 'unknown')} omitted: {exc}"
                )
        return {
            "grassMod": choose_mod("grassMod"),
            "grassModDensity": float(config.get("grassModDensity", 0.0)),
            "ceilingGrassMod": choose_mod("ceilingGrassMod"),
            "ceilingGrassModDensity": float(
                config.get("ceilingGrassModDensity", 0.0)
            ),
            "itemDistributions": item_distributions,
        }, warnings

    def _spawn_profile(self, config: Any, rng: random.Random) -> dict[str, Any]:
        config = config if isinstance(config, dict) else {}
        selected: list[str] = []
        for group in config.get("groups", []):
            if not isinstance(group, dict):
                continue
            pool = group.get("pool", [])
            if isinstance(pool, str):
                pool = self.spawn_groups.get(pool, [])
            for name in _weighted_unique_choices(pool, int(group.get("select", 0)), rng):
                if name not in selected:
                    selected.append(name)
        monster_parameters = config.get("monsterParameters", {})
        return {
            "spawnTypes": selected,
            "monsterParameters": (
                json.loads(json.dumps(monster_parameters))
                if isinstance(monster_parameters, dict)
                else {}
            ),
        }

    @staticmethod
    def _contains_biome_name(value: Any, biome_name: str) -> bool:
        if isinstance(value, str):
            return value.lower() == biome_name.lower()
        if isinstance(value, list):
            return any(
                AssetBiomeCompiler._contains_biome_name(item, biome_name)
                for item in value
            )
        if isinstance(value, dict):
            return any(
                AssetBiomeCompiler._contains_biome_name(item, biome_name)
                for item in value.values()
            )
        return False

    @staticmethod
    def _random_string(value: Any, rng: random.Random) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            choices = [item for item in value if isinstance(item, str) and item]
            return rng.choice(choices) if choices else None
        return None

    def region_recipe(self, biome_name: str, rng: random.Random) -> dict[str, Any]:
        """Resolve a fresh region recipe instead of inheriting the old cell."""

        selected: dict[str, Any] | None = None
        direct = self.region_types.get(biome_name)
        if isinstance(direct, dict):
            selected = direct
        else:
            for candidate in self.region_types.values():
                if isinstance(candidate, dict) and self._contains_biome_name(
                    candidate.get("biome"), biome_name
                ):
                    selected = candidate
                    break
        if selected is None:
            path = str(self.catalog["paths"].get(biome_name, "")).lower()
            if "/surface/" in path:
                selected = {
                    "blockSelector": ["remixedMildSurface"],
                    "fgCaveSelector": ["surfaceCaves"],
                    "bgCaveSelector": ["empty"],
                }
            else:
                selected = {}
        merged = deep_copy_json(self.region_defaults)
        if not isinstance(merged, dict):
            merged = {}
        merged.update(deep_copy_json(selected))
        cave_density = merged.get("caveLiquidSeedDensityRange", [1.0, 1.5])
        if isinstance(cave_density, list) and len(cave_density) == 2:
            density = rng.uniform(float(cave_density[0]), float(cave_density[1]))
        else:
            density = float(cave_density or 0.0)
        ocean_name = self._random_string(merged.get("oceanLiquid"), rng)
        cave_name = self._random_string(merged.get("caveLiquid"), rng)
        return {
            "blockSelector": self._random_string(merged.get("blockSelector"), rng),
            "fgCaveSelector": self._random_string(merged.get("fgCaveSelector"), rng),
            "bgCaveSelector": self._random_string(merged.get("bgCaveSelector"), rng),
            "fgOreSelector": self._random_string(merged.get("fgOreSelector"), rng),
            "bgOreSelector": self._random_string(merged.get("bgOreSelector"), rng),
            "subBlockSelector": self._random_string(merged.get("subBlockSelector"), rng),
            "oceanLiquid": self._liquid(ocean_name) if ocean_name else 0,
            "oceanLevelOffset": int(merged.get("oceanLevelOffset", 0)),
            "caveLiquid": self._liquid(cave_name) if cave_name else 0,
            "caveLiquidSeedDensity": density if cave_name else 0.0,
            "encloseLiquids": bool(merged.get("encloseLiquids", False)),
            "fillMicrodungeons": bool(merged.get("fillMicrodungeons", False)),
        }

    def terrain_selector(
        self,
        name: str,
        world_width: int,
        base_height: float,
        commonality: float,
        seed: int,
    ) -> dict[str, Any]:
        try:
            selector_type, raw = self.terrain_configs[name.lower()]
        except KeyError as exc:
            raise ValueError(f"Biome region references unknown terrain selector {name!r}") from exc
        return {
            "parameters": {
                "worldWidth": int(world_width),
                "seed": int(seed),
                "baseHeight": float(base_height),
                "commonality": float(commonality),
            },
            "type": selector_type,
            "config": deep_copy_json(raw),
        }

    def _ores(self, reference: Any, threat_level: float) -> list[list[Any]]:
        distribution: Any = reference
        if isinstance(reference, str):
            distribution = (
                self.ore_distributions.get(reference, [])
                if isinstance(self.ore_distributions, dict)
                else []
            )
        if not isinstance(distribution, list):
            return []
        ore_pairs: Any = distribution
        binned = [
            row
            for row in distribution
            if isinstance(row, list)
            and len(row) == 2
            and isinstance(row[0], (int, float))
            and isinstance(row[1], list)
        ]
        if binned:
            binned.sort(key=lambda row: float(row[0]))
            chosen = next(
                (row for row in binned if float(row[0]) >= float(threat_level)),
                binned[-1],
            )
            ore_pairs = chosen[1]
        result: list[list[Any]] = []
        for pair in ore_pairs:
            if (
                isinstance(pair, list)
                and len(pair) == 2
                and isinstance(pair[0], str)
                and isinstance(pair[1], (int, float))
                and float(pair[1]) > 0.0
            ):
                result.append([self._mod(pair[0]), float(pair[1])])
        return result

    @staticmethod
    def _directive_string(settings: dict[str, Any], hue_shift: float) -> str:
        directives = str(settings.get("directives", "")).strip().lstrip("?")
        if not settings.get("nohueshift", False):
            addition = f"hueshift={hue_shift:g}"
            directives = directives + ("?" if directives else "") + addition
        return directives

    def _parallax(
        self,
        biome_path: str,
        reference: Any,
        seed: int,
        vertical_base: float,
        hue_shift: float,
        rng: random.Random,
        *,
        native_extensions: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(reference, str) or not reference:
            return None
        parallax_path = _relative_asset_path(biome_path, reference)
        config = self.reader.json(parallax_path)
        if not isinstance(config, dict):
            raise ValueError(f"Parallax asset is not an object: {parallax_path}")
        vertical_origin = float(vertical_base) + float(config.get("verticalOrigin", 0.0))
        layers: list[dict[str, Any]] = []
        for settings in config.get("layers", []):
            if not isinstance(settings, dict):
                continue
            kind = str(settings.get("kind", ""))
            if not kind or kind.startswith("foliage/") or kind.startswith("stem/"):
                continue
            frequency = float(settings.get("frequency", 1.0))
            if frequency < 1.0 and rng.random() > frequency:
                continue
            base_count = max(1, int(settings.get("baseCount", 1)))
            base = rng.randint(1, base_count)
            textures = [f"/parallax/images/{kind}/base/{base}.png"]
            mod_count = max(0, int(settings.get("modCount", 0)))
            if mod_count:
                mod = rng.randint(0, mod_count)
                if mod:
                    textures.append(f"/parallax/images/{kind}/mod/{mod}.png")
            parallax_value = settings.get("parallax", 0.0)
            if isinstance(parallax_value, list) and len(parallax_value) == 2:
                parallax_pair = [float(parallax_value[0]), float(parallax_value[1])]
            else:
                parallax_pair = [float(parallax_value), float(parallax_value)]
            offset = settings.get("offset", [0.0, 0.0])
            if not isinstance(offset, list) or len(offset) != 2:
                offset = [0.0, 0.0]
            minimum_speed = settings.get("minSpeed", 0.0)
            maximum_speed = settings.get("maxSpeed", 0.0)
            minimum_speed_y = minimum_speed[1] if isinstance(minimum_speed, list) and len(minimum_speed) > 1 else 0.0
            maximum_speed_y = maximum_speed[1] if isinstance(maximum_speed, list) and len(maximum_speed) > 1 else 0.0
            if isinstance(minimum_speed, list):
                minimum_speed = minimum_speed[0] if minimum_speed else 0.0
            if isinstance(maximum_speed, list):
                maximum_speed = maximum_speed[0] if maximum_speed else 0.0
            repeat_y = bool(settings.get("repeatY", False))
            layers.append(
                {
                    "textures": textures,
                    "directives": self._directive_string(settings, hue_shift),
                    "parallaxValue": parallax_pair,
                    "repeat": [1, 1 if repeat_y else 0],
                    "tileLimitTop": settings.get("tileLimitTop") if repeat_y else None,
                    "tileLimitBottom": settings.get("tileLimitBottom") if repeat_y else None,
                    "verticalOrigin": vertical_origin,
                    "zLevel": float(parallax_pair[0] + parallax_pair[1]),
                    "parallaxOffset": [float(offset[0]), float(offset[1])],
                    "timeOfDayCorrelation": str(
                        settings.get("timeOfDayCorrelation", "")
                    ),
                    "speed": rng.uniform(float(minimum_speed), float(maximum_speed)),
                    "unlit": bool(settings.get("unlit", False)),
                    "lightMapped": bool(settings.get("lightMapped", False)),
                    "fadePercent": float(settings.get("fadePercent", 0.0)),
                }
            )
            if native_extensions:
                layers[-1].update({
                    "frameNumber": int(settings.get("frameNumber", 1)),
                    "frameOffset": int(settings.get("frameOffset", 0)),
                    "animationCycle": float(settings.get("animationCycle", 1.0)),
                    "speedY": rng.uniform(float(minimum_speed_y), float(maximum_speed_y))
                              if minimum_speed_y or maximum_speed_y else 0.0,
                })
        layers.sort(key=lambda layer: float(layer["zLevel"]), reverse=True)
        return {
            "seed": seed,
            "verticalOrigin": vertical_origin,
            "parallaxTreeVariant": None,
            "hueShift": float(hue_shift),
            "imageDirectory": "/parallax/images/",
            "layers": layers,
        }

    def compile(
        self,
        biome_name: str,
        seed: int,
        vertical_base: float,
        old_biome: dict[str, Any],
        threat_level: float = 1.0,
        *,
        hue_selection: tuple[float, int] | None = None,
        native_extensions: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        try:
            raw = self.catalog["biomes"][biome_name]
            biome_path = self.catalog["paths"][biome_name]
        except KeyError as exc:
            raise ValueError(f"所选 assets 中不存在 biome：{biome_name}") from exc
        rng = random.Random(seed)
        options = raw.get("hueShiftOptions", [])
        hue_shift = float(rng.choice(options)) if isinstance(options, list) and options else 0.0
        # Consume the historical hue draw even when keeping a world's color;
        # changing its color policy must not reshuffle all flora/objects too.
        material_hue = _material_hue_from_degrees(hue_shift)
        if hue_selection is not None:
            hue_shift, material_hue = hue_selection
            if (isinstance(hue_shift, bool) or not isinstance(hue_shift, (int, float))
                    or not math.isfinite(hue_shift)
                    or isinstance(material_hue, bool) or not isinstance(material_hue, int)
                    or not 0 <= material_hue <= 255):
                raise ValueError("Biome hue selection must contain finite degrees and a material hue byte (0–255)")
            hue_shift = float(hue_shift)
        main_name = raw.get("mainBlock")
        main_block = (
            self._material(main_name)
            if isinstance(main_name, str)
            else int(old_biome.get("mainBlock", 0))
        )
        sub_blocks = [
            self._material(name)
            for name in raw.get("subBlocks", [])
            if isinstance(name, str)
        ]
        surface, surface_warnings = self._placeables(
            raw.get("surfacePlaceables", {}), biome_path, rng, hue_shift
        )
        underground, underground_warnings = self._placeables(
            raw.get("undergroundPlaceables", {}), biome_path, rng, hue_shift
        )
        parallax = self._parallax(
            biome_path,
            raw.get("parallax"),
            seed,
            vertical_base,
            hue_shift,
            rng,
            native_extensions=native_extensions,
        )
        compiled = {
            "baseName": biome_name,
            "description": str(raw.get("description", "")),
            "mainBlock": main_block,
            "subBlocks": sub_blocks,
            "ores": self._ores(raw.get("ores", []), threat_level),
            "hueShift": hue_shift,
            "materialHueShift": material_hue,
            "surfacePlaceables": surface,
            "undergroundPlaceables": underground,
            "spawnProfile": self._spawn_profile(raw.get("spawnProfile", {}), rng),
            "parallax": parallax,
            "ambientNoises": json.loads(json.dumps(raw.get("ambientNoises"))),
            "musicTrack": json.loads(json.dumps(raw.get("musicTrack"))),
        }
        if native_extensions:
            for field in ("ambientNoises", "musicTrack"):
                if isinstance(compiled[field], dict):
                    compiled[field].setdefault("loops", -1)
        return compiled, surface_warnings + underground_warnings


def _world_uses_native_extensions(document: dict[str, Any]) -> bool:
    return isinstance(document.get("format", {}).get("subVersioning"), dict)


def _complete_native_parallax_defaults(document: dict[str, Any], parallax: Any) -> None:
    """Supply OpenSB loader defaults only on an OpenSB-format target world.

    Vanilla does not store these members. Explicit OpenSB animation/motion
    values must survive; never replace a supplied value with its default.
    """
    if not _world_uses_native_extensions(document) or not isinstance(parallax, dict):
        return
    for layer in parallax.get("layers", []):
        if isinstance(layer, dict):
            for field, default in (("frameNumber", 1), ("frameOffset", 0),
                                   ("animationCycle", 1.0), ("speedY", 0.0)):
                layer.setdefault(field, default)


def _world_biome_hue_selection(
    document: dict[str, Any],
    biome_name: str,
    raw_biome: dict[str, Any],
    *,
    preferred_indexes: Sequence[int] = (),
    explicit_overrides: dict[str, Any] | None = None,
) -> tuple[tuple[float, int], dict[str, Any]]:
    """Preserve native/chosen colors, or choose one stable planet-biome hue.

    Rectangle coordinates and ocean-floor partition heights are intentionally
    excluded from the fallback color seed. Other terrain and biome-generation
    randomness keeps its existing region seed. A legal zero hue stays legal.
    """

    overrides = explicit_overrides or {}
    explicit = any(key in overrides for key in ("hueShift", "materialHueShift", "hueShiftOptions"))
    candidates = list(preferred_indexes)
    if not explicit:
        for cell in iter_compiled_cells(document):
            candidates.extend((cell.get("blockBiomeIndex"), cell.get("environmentBiomeIndex")))
        candidates.extend(range(1, len(compiled_biomes(document)) + 1))
        visited: set[int] = set()
        for index in candidates:
            if (isinstance(index, bool) or not isinstance(index, int)
                    or index in visited or not 1 <= index <= len(compiled_biomes(document))):
                continue
            visited.add(index)
            biome = biome_at(document, index)
            if str(biome.get("baseName", "")).casefold() != biome_name.casefold():
                continue
            if "hueShift" not in biome and "materialHueShift" not in biome:
                continue
            hue = biome.get("hueShift")
            material = biome.get("materialHueShift")
            if hue is None:
                hue = float(material) * 360.0 / 255.0
            if material is None:
                material = _material_hue_from_degrees(hue)
            return (hue, material), {"source": "existingWorldBiome", "sourceBiomeIndex": index,
                                     "hueShift": hue, "materialHueShift": material}

    options = raw_biome.get("hueShiftOptions", [])
    if options is None:
        options = []  # Native BiomeDatabase treats a JSON null as zero hue.
    if not isinstance(options, list) or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
        for item in options
    ):
        raise ValueError(f"Biome {biome_name!r} hueShiftOptions must be a JSON array of finite numbers")
    seed = _stable_asset_seed(world_template(document).get("seed", 0), biome_name.casefold(), "BiomeHueShift")
    hue = float(random.Random(seed).choice(options)) if options else 0.0
    material = _material_hue_from_degrees(hue)
    if "hueShift" in overrides:
        hue = overrides["hueShift"]
        if isinstance(hue, bool) or not isinstance(hue, (int, float)) or not math.isfinite(hue):
            raise ValueError("Explicit biome hueShift must be a finite number")
        material = _material_hue_from_degrees(hue)
    if "materialHueShift" in overrides:
        material = overrides["materialHueShift"]
        if isinstance(material, bool) or not isinstance(material, int) or not 0 <= material <= 255:
            raise ValueError("Explicit biome materialHueShift must be an integer from 0 to 255")
        if "hueShift" not in overrides:
            hue = float(material) * 360.0 / 255.0
    return (float(hue), material), {"source": "explicitOverride" if explicit else "planetBiomeSeed",
                                     "hueShift": float(hue), "materialHueShift": material,
                                     "planetBiomeHueSeed": seed}


def _overlay_layer_biome(
    layer: dict[str, Any],
    world_width: int,
    x_ranges: Sequence[tuple[int, int]],
    target_biome_index: int | dict[int, int],
    sub_block_counts: dict[int, int] | None = None,
    fallback_sub_block_selector: int | None = None,
    replacement_cell: dict[str, Any] | None = None,
    environment_only: bool = False,
) -> None:
    boundaries = layer.get("boundaries")
    cells = layer.get("cells")
    if not isinstance(boundaries, list) or not isinstance(cells, list):
        raise WorldFormatError("Compiled world layer has invalid boundaries/cells")
    if len(cells) != len(boundaries) + 1:
        raise WorldFormatError("Compiled world layer cell count does not match boundaries")

    segments: list[tuple[int, int, dict[str, Any]]] = []
    starts = [0] + [int(boundary) + 1 for boundary in boundaries]
    ends = [int(boundary) for boundary in boundaries] + [world_width - 1]
    split_points = {0, world_width}
    for start, end in x_ranges:
        split_points.add(start)
        if end + 1 < world_width:
            split_points.add(end + 1)

    for cell, original_start, original_end in zip(cells, starts, ends):
        points = sorted(
            point
            for point in split_points
            if original_start <= point <= original_end + 1
        )
        if not points or points[0] != original_start:
            points.insert(0, original_start)
        if points[-1] != original_end + 1:
            points.append(original_end + 1)
        for start, stop in zip(points, points[1:]):
            if start >= stop:
                continue
            segment_cell = deep_copy_json(cell)
            if any(range_start <= start and stop - 1 <= range_end for range_start, range_end in x_ranges):
                if replacement_cell is not None:
                    segment_cell = deep_copy_json(replacement_cell)
                    segments.append((start, stop - 1, segment_cell))
                    continue
                source_key = (
                    "environmentBiomeIndex" if environment_only else "blockBiomeIndex"
                )
                old_index = int(segment_cell.get(source_key, 0))
                if isinstance(target_biome_index, dict):
                    new_index = target_biome_index.get(old_index)
                    if new_index is None:
                        new_index = next(iter(target_biome_index.values()))
                else:
                    new_index = target_biome_index
                segment_cell["environmentBiomeIndex"] = new_index
                if environment_only:
                    segments.append((start, stop - 1, segment_cell))
                    continue
                segment_cell["blockBiomeIndex"] = new_index
                if sub_block_counts is not None:
                    needed = int(sub_block_counts.get(new_index, 0))
                    current = [
                        int(value)
                        for value in segment_cell.get("subBlockSelectorIndexes", [])
                        if isinstance(value, int)
                    ]
                    if needed <= 0:
                        segment_cell["subBlockSelectorIndexes"] = []
                    else:
                        selector = (
                            current[-1]
                            if current
                            else fallback_sub_block_selector
                        )
                        if selector is None:
                            selector = int(segment_cell.get("terrainSelectorIndex", 0))
                        segment_cell["subBlockSelectorIndexes"] = (
                            current[:needed]
                            + [selector] * max(0, needed - len(current))
                        )
            segments.append((start, stop - 1, segment_cell))

    merged: list[tuple[int, int, dict[str, Any]]] = []
    for start, end, cell in segments:
        if merged and merged[-1][1] + 1 == start and merged[-1][2] == cell:
            merged[-1] = (merged[-1][0], end, cell)
        else:
            merged.append((start, end, cell))
    layer["boundaries"] = [end for _start, end, _cell in merged[:-1]]
    layer["cells"] = [cell for _start, _end, cell in merged]


def _overlay_layer_cells(
    layer: dict[str, Any],
    world_width: int,
    replacements: Sequence[tuple[int, int, dict[str, Any]]],
) -> None:
    """Overlay several disjoint horizontal cells in one stable pass."""

    originals = _layer_horizontal_segments(layer, world_width)
    ordered = sorted(
        (
            max(0, int(start)),
            min(world_width - 1, int(end)),
            cell,
        )
        for start, end, cell in replacements
        if int(start) <= int(end)
        and int(end) >= 0
        and int(start) < world_width
    )
    for index, (start, _end, _cell) in enumerate(ordered):
        if index and start <= ordered[index - 1][1]:
            raise ValueError("Replacement region cells must not overlap")

    split_points = {0, world_width}
    for start, end, _cell in originals:
        split_points.add(start)
        split_points.add(end + 1)
    for start, end, _cell in ordered:
        split_points.add(start)
        split_points.add(end + 1)
    points = sorted(split_points)

    result: list[tuple[int, int, dict[str, Any]]] = []
    original_index = 0
    replacement_index = 0
    for start, stop in zip(points, points[1:]):
        if start >= stop:
            continue
        while originals[original_index][1] < start:
            original_index += 1
        while (
            replacement_index < len(ordered)
            and ordered[replacement_index][1] < start
        ):
            replacement_index += 1
        cell = originals[original_index][2]
        if (
            replacement_index < len(ordered)
            and ordered[replacement_index][0] <= start
            and stop - 1 <= ordered[replacement_index][1]
        ):
            cell = ordered[replacement_index][2]
        cloned = deep_copy_json(cell)
        if result and result[-1][1] + 1 == start and result[-1][2] == cloned:
            result[-1] = (result[-1][0], stop - 1, cloned)
        else:
            result.append((start, stop - 1, cloned))

    layer["boundaries"] = [end for _start, end, _cell in result[:-1]]
    layer["cells"] = [cell for _start, _end, cell in result]


def _register_terrain_selector(
    region_data: dict[str, Any], selector: dict[str, Any]
) -> int:
    selectors = region_data.get("terrainSelectors")
    if not isinstance(selectors, list):
        raise WorldFormatError("worldTemplate.regionData.terrainSelectors is missing")
    try:
        return selectors.index(selector) + 1
    except ValueError:
        selectors.append(selector)
        return len(selectors)


def _terrain_selector_positive_relief(config: Any, depth: int = 0) -> float | None:
    """Conservatively estimate a terrain recipe's rise above ``baseHeight``.

    This is deliberately an upper bound rather than a visual amplitude
    estimate.  It is used only to decide whether an ocean-floor selector is
    safe beneath a liquid surface.  Unknown selector shapes return ``None``
    and are treated as unsafe near the surface.
    """

    if not isinstance(config, dict) or depth > 32:
        return None
    kind = str(config.get("type", "")).strip()
    if kind == "flatSurface":
        return max(0.0, float(config.get("adjustment", 0.0)))
    if kind == "displacement":
        source_relief = _terrain_selector_positive_relief(
            config.get("source"), depth + 1
        )
        if source_relief is None:
            return None
        clamp = config.get("yClamp")
        if isinstance(clamp, list) and len(clamp) >= 2:
            vertical_rise = max(0.0, float(clamp[0]), float(clamp[1]))
            vertical_rise += abs(float(config.get("yClampSmoothing", 0.0)))
        else:
            vertical_rise = abs(float(config.get("yAmp", 0.0)))
            vertical_rise += max(0.0, float(config.get("yBias", 0.0)))
        return source_relief + vertical_rise
    if kind in ("max", "min", "minmax"):
        sources = config.get("sources")
        if not isinstance(sources, list) or not sources:
            return None
        reliefs = [
            _terrain_selector_positive_relief(source, depth + 1)
            for source in sources
        ]
        if any(value is None for value in reliefs):
            return None
        return max(float(value) for value in reliefs if value is not None)
    if kind == "mix":
        reliefs = [
            _terrain_selector_positive_relief(config.get(key), depth + 1)
            for key in ("aSource", "bSource")
        ]
        if any(value is None for value in reliefs):
            return None
        return max(float(value) for value in reliefs if value is not None)
    # Rotation and non-surface density shapes cannot be bounded safely from
    # their JSON alone.  The caller will replace them with a flat shore recipe
    # wherever they might approach the liquid surface.
    return None


def _flat_ocean_shore_selector_index(
    region_data: dict[str, Any],
    source_index: int,
    base_height: float,
    cache: dict[tuple[int, float], int],
) -> int:
    """Register a zero-relief surface selector at one basin-profile height."""

    selectors = region_data.get("terrainSelectors")
    if not isinstance(selectors, list):
        raise WorldFormatError("worldTemplate.regionData.terrainSelectors is missing")
    if not 1 <= int(source_index) <= len(selectors):
        raise WorldFormatError("Ocean-floor terrain selector index is invalid")
    rounded_height = round(float(base_height), 3)
    key = (int(source_index), rounded_height)
    if key in cache:
        return cache[key]
    source = selectors[int(source_index) - 1]
    parameters = deep_copy_json(source.get("parameters", {}))
    if not isinstance(parameters, dict):
        parameters = {}
    parameters["baseHeight"] = rounded_height
    selector = {
        "parameters": parameters,
        "type": "displacement",
        "config": {
            "name": "starboundWorldEditorSmoothOceanShore",
            "type": "displacement",
            "xType": "perlin",
            "xOctaves": 1,
            "xFreq": 0.01,
            "xAmp": 0.0,
            "xXInfluence": 0.0,
            "xYInfluence": 0.0,
            "yType": "perlin",
            "yOctaves": 1,
            "yFreq": 0.01,
            "yAmp": 0.0,
            "yYInfluence": 0.0,
            "source": {"type": "flatSurface", "adjustment": 0.0},
        },
    }
    result = _register_terrain_selector(region_data, selector)
    cache[key] = result
    return result


def _sloped_ocean_floor_selector_index(
    region_data: dict[str, Any],
    source_index: int,
    world_width: int,
    midpoint_x: float,
    midpoint_height: float,
    slope: float,
    flat: bool,
    cache: dict[tuple[int, float, float, float, bool], int],
) -> int:
    """Clone one selector onto a continuous straight segment of the basin.

    A World4 region cell normally supplies one constant ``baseHeight``.  Using
    hundreds of narrow cells makes Starbound mark almost every tile as a biome
    transition, which suppresses plants and placeables.  A rotated selector
    instead describes the local tangent line *inside* a wide cell.  Adjacent
    lines meet at the same profile endpoint, so the engine's normal region
    blending keeps the curve continuous without 32-tile stair steps.
    """

    selectors = region_data.get("terrainSelectors")
    if not isinstance(selectors, list):
        raise WorldFormatError("worldTemplate.regionData.terrainSelectors is missing")
    if not 1 <= int(source_index) <= len(selectors):
        raise WorldFormatError("Ocean-floor terrain selector index is invalid")

    rounded_x = round(float(midpoint_x), 3)
    rounded_height = round(float(midpoint_height), 3)
    rounded_slope = round(float(slope), 9)
    key = (
        int(source_index),
        rounded_x,
        rounded_height,
        rounded_slope,
        bool(flat),
    )
    if key in cache:
        return cache[key]

    source = selectors[int(source_index) - 1]
    parameters = deep_copy_json(source.get("parameters", {}))
    if not isinstance(parameters, dict):
        parameters = {}
    parameters["worldWidth"] = int(world_width)

    # RotateSelector turns coordinates around worldWidth / 2.  For
    # y = midpointHeight + slope * (x - midpointX), rotation=-atan(slope).
    # This base-height transform makes the rotated source pass through the
    # requested midpoint while preserving the source selector's own relief.
    rotation = -math.atan(float(slope))
    centre_x = float(world_width) / 2.0
    parameters["baseHeight"] = round(
        math.sin(rotation) * (float(midpoint_x) - centre_x)
        + math.cos(rotation) * float(midpoint_height),
        6,
    )
    source_config = (
        {"type": "flatSurface", "adjustment": 0.0}
        if flat
        else deep_copy_json(source.get("config", {}))
    )
    if not isinstance(source_config, dict) or not source_config.get("type"):
        raise WorldFormatError("Ocean-floor source selector config is invalid")
    selector = {
        "parameters": parameters,
        "type": "rotate",
        "config": {
            "type": "rotate",
            "rotation": rotation,
            "source": source_config,
        },
    }
    result = _register_terrain_selector(region_data, selector)
    cache[key] = result
    return result


_REGION_CELL_SELECTOR_KEYS = (
    "terrainSelectorIndex",
    "foregroundCaveSelectorIndex",
    "backgroundCaveSelectorIndex",
)
_REGION_CELL_SELECTOR_LIST_KEYS = (
    "subBlockSelectorIndexes",
    "foregroundOreSelectorIndexes",
    "backgroundOreSelectorIndexes",
)


def _shift_region_cell_selector_heights(
    region_data: dict[str, Any],
    cell: dict[str, Any],
    base_height: float,
    cache: dict[tuple[int, float], int],
) -> dict[str, Any]:
    """Clone every selector in a cell at a new common terrain base height.

    The selector types, configs and seeds remain unchanged.  The broad basin
    curve therefore retains the ocean-floor biome's own noise, caves,
    sub-blocks and ore pattern instead of becoming a flat mathematical line.
    """

    selectors = region_data.get("terrainSelectors")
    if not isinstance(selectors, list):
        raise WorldFormatError("worldTemplate.regionData.terrainSelectors is missing")
    rounded_height = round(float(base_height), 3)

    def shifted_index(value: Any) -> Any:
        if not isinstance(value, int) or value in (0, 0xFFFFFFFF):
            return value
        if value < 1 or value > len(selectors):
            return value
        key = (value, rounded_height)
        if key in cache:
            return cache[key]
        selector = deep_copy_json(selectors[value - 1])
        parameters = selector.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
            selector["parameters"] = parameters
        parameters["baseHeight"] = rounded_height
        result = _register_terrain_selector(region_data, selector)
        cache[key] = result
        return result

    shifted = deep_copy_json(cell)
    for key in _REGION_CELL_SELECTOR_KEYS:
        if key in shifted:
            shifted[key] = shifted_index(shifted[key])
    for key in _REGION_CELL_SELECTOR_LIST_KEYS:
        values = shifted.get(key)
        if isinstance(values, list):
            shifted[key] = [shifted_index(value) for value in values]
    return shifted


def _coastal_basin_profile_segments(
    x_start: int,
    x_end: int,
    world_width: int,
    floor_height: float,
    shore_height: float,
    sample_width: int = 32,
    left_shore: bool = True,
    right_shore: bool = True,
    relief_start_height: float | None = None,
    shore_exclusion_width: int = 0,
) -> list[dict[str, Any]]:
    """Build a wrapped, mirrored quintic basin profile.

    Region cells remain deliberately wide, while each returned segment also
    carries a local line slope.  The terrain selector can therefore render the
    quintic profile continuously inside the region instead of holding one
    height for the entire cell.  When ``relief_start_height`` is supplied, a
    segment boundary is inserted at that depth so the original floor relief
    begins at the requested Y rather than one coarse sample early or late.
    """

    ranges = _wrapped_x_ranges(int(x_start), int(x_end), int(world_width))
    length = sum(end - start + 1 for start, end in ranges)
    if length <= 0:
        return []
    step = max(1, int(sample_width))
    # Region layers form a Cartesian grid.  This cap keeps exceptionally wide
    # custom oceans practical while ordinary intervals retain the requested
    # (normally one-sector / 32-tile) sampling width.
    step = max(step, int(math.ceil(length / 256.0)))
    half_span = max(1.0, (length - 1) / 2.0)

    def smootherstep(value: float) -> float:
        value = min(1.0, max(0.0, value))
        return value * value * value * (
            value * (value * 6.0 - 15.0) + 10.0
        )

    def height_at(offset: float) -> float:
        influences: list[float] = []
        if left_shore:
            influences.append(1.0 - smootherstep(offset / half_span))
        if right_shore:
            right_distance = length - 1 - offset
            influences.append(1.0 - smootherstep(right_distance / half_span))
        shore_influence = max(influences, default=0.0)
        return floor_height + (shore_height - floor_height) * shore_influence

    breakpoints = set(range(0, length, step))
    breakpoints.add(length)
    exclusion = max(0, int(shore_exclusion_width))
    if exclusion:
        if left_shore:
            breakpoints.add(min(length, exclusion + 1))
        if right_shore:
            breakpoints.add(max(0, length - exclusion - 1))
    if relief_start_height is not None:
        threshold = float(relief_start_height)
        coarse = sorted(breakpoints)
        for left, right in zip(coarse, coarse[1:]):
            if right - left <= 1:
                continue
            left_height = height_at(float(left))
            right_height = height_at(float(right - 1))
            if (left_height - threshold) * (right_height - threshold) >= 0:
                continue
            low = float(left)
            high = float(right - 1)
            increasing = right_height > left_height
            for _ in range(48):
                middle = (low + high) / 2.0
                middle_height = height_at(middle)
                if (middle_height < threshold) == increasing:
                    low = middle
                else:
                    high = middle
            split = min(
                right - 1,
                max(left + 1, int(math.floor((low + high) / 2.0)) + 1),
            )
            breakpoints.add(split)

    profile: list[dict[str, Any]] = []
    ordered_breakpoints = sorted(breakpoints)
    for offset_start, offset_after in zip(
        ordered_breakpoints, ordered_breakpoints[1:]
    ):
        offset_end = offset_after - 1
        if offset_end < offset_start:
            continue
        logical_pieces: list[tuple[int, int]] = []
        actual_start = (int(x_start) + offset_start) % world_width
        actual_end = (int(x_start) + offset_end) % world_width
        if actual_start <= actual_end:
            logical_pieces.append((offset_start, offset_end))
        else:
            first_length = world_width - actual_start
            logical_pieces.append((offset_start, offset_start + first_length - 1))
            logical_pieces.append((offset_start + first_length, offset_end))
        for logical_start, logical_end in logical_pieces:
            piece_start = (int(x_start) + logical_start) % world_width
            piece_end = (int(x_start) + logical_end) % world_width
            midpoint = (logical_start + logical_end) / 2.0
            start_height = height_at(float(logical_start))
            end_height = height_at(float(logical_end))
            base_height = height_at(midpoint)
            slope = (
                (end_height - start_height) / (logical_end - logical_start)
                if logical_end > logical_start
                else 0.0
            )
            profile.append(
                {
                    "xStart": piece_start,
                    "xEnd": piece_end,
                    "baseHeight": round(base_height, 3),
                    "startHeight": round(start_height, 6),
                    "endHeight": round(end_height, 6),
                    "slope": round(slope, 9),
                    "midpointX": round((piece_start + piece_end) / 2.0, 3),
                    "logicalStart": logical_start,
                    "logicalEnd": logical_end,
                }
            )
    return sorted(profile, key=lambda item: (item["xStart"], item["xEnd"]))


def _ocean_interval_solid_edges(
    document: dict[str, Any], x_start: int, x_end: int, probe_y: int
) -> tuple[bool, bool]:
    width = int(document["size"][0])
    ranges = _wrapped_x_ranges(int(x_start), int(x_end), width)
    if sum(end - start + 1 for start, end in ranges) >= width:
        return False, False

    def is_solid_land(x: int) -> bool:
        cell = _compiled_cell_at(document, x % width, int(probe_y))
        index = cell.get("blockBiomeIndex")
        if not isinstance(index, int):
            return True
        return ocean_biome_pair(_biome_name_for_index(document, index)) is None

    return (
        is_solid_land(int(x_start) - 1),
        is_solid_land(int(x_end) + 1),
    )


def _ocean_floor_ecology_top(
    base_height: float,
    ecology_height: int,
    submerged_ceiling: int,
) -> int:
    """Return the inclusive top Y for floor ecology without crossing the liquid surface."""

    return min(
        int(submerged_ceiling),
        int(math.floor(base_height)) + max(0, int(ecology_height)),
    )


def _anchor_biome_parallax(
    document: dict[str, Any], biome_index: int, vertical_origin: float
) -> None:
    """Keep one compiled biome's parallax at a fixed world-space baseline.

    Coastal terrain selectors are cloned at many heights, but the parallax is
    a property of the compiled biome rather than of those selector clones.
    Writing the same origin to both the parallax object and every stored layer
    makes that independence explicit and also repairs older compiled objects
    whose parent/layer origins disagree.
    """

    biome = biome_at(document, int(biome_index))
    parallax = biome.get("parallax")
    if not isinstance(parallax, dict):
        return
    origin = float(vertical_origin)
    parallax["verticalOrigin"] = origin
    layers = parallax.get("layers")
    if isinstance(layers, list):
        for layer in layers:
            if isinstance(layer, dict):
                layer["verticalOrigin"] = origin


def _distribution_items(distribution: dict[str, Any]) -> list[Any]:
    """Return the compiled biome items carried by one distribution."""

    result = list(distribution.get("randomItems") or [])
    for pair in distribution.get("weightedItems") or []:
        if isinstance(pair, list) and pair:
            result.append(pair[0])
    return result


def _biome_item_kind(item: Any) -> str:
    if isinstance(item, list) and item:
        return str(item[0]).strip().casefold()
    return ""


def _distribution_contains_kind(
    distribution: dict[str, Any], kinds: set[str]
) -> bool:
    return any(
        _biome_item_kind(item) in kinds
        for item in _distribution_items(distribution)
    )


def _json_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def _distribution_is_tall_aquatic_vegetation(
    distribution: dict[str, Any]
) -> bool:
    items = _distribution_items(distribution)
    if any(_biome_item_kind(item) == "treepair" for item in items):
        return True
    markers = ("seatree", "kelp", "toxickelp", "seaweed")
    return any(
        marker in text.casefold()
        for item in items
        for text in _json_strings(item)
        for marker in markers
    )


def _distribution_is_vegetation(distribution: dict[str, Any]) -> bool:
    plant_kinds = {"grass", "bush", "treepair"}
    if _distribution_contains_kind(distribution, plant_kinds):
        return True
    markers = (
        "algae",
        "bush",
        "cactus",
        "coral",
        "fern",
        "flower",
        "grass",
        "kelp",
        "mushroom",
        "plant",
        "reed",
        "reefpod",
        "sapling",
        "seaweed",
        "seed",
        "shroom",
        "tree",
        "vine",
    )
    return any(
        marker in text.casefold()
        for item in _distribution_items(distribution)
        for text in _json_strings(item)
        for marker in markers
    )


def _additional_density_distribution(
    distribution: dict[str, Any], extra_ratio: float
) -> dict[str, Any] | None:
    """Build an independent placement pass adding approximately ``extra_ratio``.

    Starbound's periodic distributions have integer spacing, so multiplying a
    modulus directly cannot express a 1.4x density exactly.  Keeping the
    original pass and adding an independent pass preserves all original
    placements and gives the requested statistical increase without moving
    existing plants.
    """

    if extra_ratio <= 0.0 or not _distribution_is_vegetation(distribution):
        return None
    result = deep_copy_json(distribution)
    kind = str(result.get("distribution", "")).casefold()
    if kind == "random":
        probability = max(0.0, float(result.get("blockProbability", 0.0)))
        if probability <= 0.0:
            return None
        result["blockProbability"] = min(1.0, probability * extra_ratio)
        seed = int(result.get("blockSeed", 0))
        result["blockSeed"] = ((seed ^ 0x5DEECE66D) + 0x9E3779B9) & 0x7FFFFFFFFFFFFFFF
        return result
    if kind == "periodic":
        modulus = max(1, int(result.get("modulus", 1)))
        # Compensation can add more than one original pass worth of density.
        # Round spacing UP so integer moduli never exceed the requested rate.
        result["modulus"] = max(
            1, int(math.ceil(modulus / extra_ratio - 1e-9)),
        )
        result["modulusOffset"] = (
            int(result.get("modulusOffset", 0)) + 1
        ) % int(result["modulus"])
        return result
    return None


def _prepare_placeables(
    placeables: Any,
    *,
    remove_microdungeons: bool,
    keep_only_microdungeons: bool,
    remove_tall_aquatic_plants: bool,
    remove_all_plants: bool,
    vegetation_density_multiplier: float,
) -> None:
    if not isinstance(placeables, dict):
        return
    distributions = placeables.get("itemDistributions")
    if isinstance(distributions, list):
        kept: list[Any] = []
        for distribution in distributions:
            if not isinstance(distribution, dict):
                if not keep_only_microdungeons:
                    kept.append(distribution)
                continue
            has_microdungeon = _distribution_contains_kind(
                distribution, {"microdungeon"}
            )
            if keep_only_microdungeons and not has_microdungeon:
                continue
            if remove_microdungeons and has_microdungeon:
                continue
            if remove_all_plants and _distribution_is_vegetation(distribution):
                continue
            if (
                remove_tall_aquatic_plants
                and _distribution_is_tall_aquatic_vegetation(distribution)
            ):
                continue
            kept.append(deep_copy_json(distribution))
            extra = _additional_density_distribution(
                distribution,
                max(0.0, float(vegetation_density_multiplier) - 1.0),
            )
            if extra is not None:
                kept.append(extra)
        placeables["itemDistributions"] = kept
    if remove_all_plants or keep_only_microdungeons:
        placeables["grassMod"] = 0xFFFF
        placeables["grassModDensity"] = 0.0
        placeables["ceilingGrassMod"] = 0xFFFF
        placeables["ceilingGrassModDensity"] = 0.0
    elif vegetation_density_multiplier > 1.0:
        for key in ("grassModDensity", "ceilingGrassModDensity"):
            if isinstance(placeables.get(key), (int, float)):
                placeables[key] = min(
                    1.0,
                    float(placeables[key]) * float(vegetation_density_multiplier),
                )


def _prepare_biome_placeables(
    biome: dict[str, Any],
    *,
    remove_microdungeons: bool = False,
    keep_only_microdungeons: bool = False,
    remove_tall_aquatic_plants: bool = False,
    remove_all_plants: bool = False,
    vegetation_density_multiplier: float = 1.0,
) -> None:
    for key in ("surfacePlaceables", "undergroundPlaceables"):
        _prepare_placeables(
            biome.get(key),
            remove_microdungeons=remove_microdungeons,
            keep_only_microdungeons=keep_only_microdungeons,
            remove_tall_aquatic_plants=remove_tall_aquatic_plants,
            remove_all_plants=remove_all_plants,
            vegetation_density_multiplier=vegetation_density_multiplier,
        )


def _surface_ocean_control_biome_index(
    document: dict[str, Any],
    surface_biome_index: int,
    *,
    keep_only_island_microdungeons: bool = False,
    remove_island_microdungeons: bool = False,
    clear_spawn_profile: bool = False,
    allow_ocean_plants: bool = False,
    allow_ocean_trees: bool = False,
) -> int:
    """Clone a surface-ocean biome for a controlled shore/water band."""

    biome = deep_copy_json(biome_at(document, surface_biome_index))
    if allow_ocean_plants:
        # Re-enable only the requested native plant kinds, not objectPool
        # entries whose names merely resemble plants. Filter individual items:
        # a modded distribution can mix trees, grass, objects and prefabs.
        plant_kinds = {"grass", "bush"}
        if allow_ocean_trees:
            plant_kinds.add("treepair")
        for key in ("surfacePlaceables", "undergroundPlaceables"):
            placeables = biome.get(key)
            if not isinstance(placeables, dict):
                continue
            kept = []
            for distribution in placeables.get("itemDistributions", []):
                if not isinstance(distribution, dict):
                    continue
                groups = [(plant_kinds, False)]
                if keep_only_island_microdungeons and not remove_island_microdungeons:
                    groups.append(({"microdungeon"}, True))
                for allowed_kinds, island_mode in groups:
                    filtered = deep_copy_json(distribution)
                    filtered["randomItems"] = [
                        item for item in distribution.get("randomItems", [])
                        if _biome_item_kind(item) in allowed_kinds
                    ]
                    filtered["weightedItems"] = [
                        pair for pair in distribution.get("weightedItems", [])
                        if isinstance(pair, list) and pair
                        and _biome_item_kind(pair[0]) in allowed_kinds
                    ]
                    if not _distribution_items(filtered):
                        continue
                    if island_mode:
                        filtered["mode"] = "ocean"
                    kept.append(filtered)
            placeables["itemDistributions"] = kept
            # Keep the source grass/ceiling-grass material modifiers and their
            # densities intact, independently of item distributions.
    else:
        _prepare_biome_placeables(
            biome,
            remove_microdungeons=remove_island_microdungeons,
            keep_only_microdungeons=keep_only_island_microdungeons,
            remove_all_plants=keep_only_island_microdungeons,
        )
    if keep_only_island_microdungeons and not allow_ocean_plants:
        # Vanilla's oceanislandsmicrodungeons is a *floor* distribution.  If
        # copied unchanged into a submerged water biome, a noisy biome border
        # can evaluate its surface-village prefab on the underwater basin bed.
        # Ocean mode is checked against the actual oceanLiquidLevel by
        # WorldTemplate::validBiomeItems, independently of block-biome noise.
        for key in ("surfacePlaceables", "undergroundPlaceables"):
            placeables = biome.get(key)
            if not isinstance(placeables, dict):
                continue
            for distribution in placeables.get("itemDistributions", []):
                if isinstance(distribution, dict):
                    distribution["mode"] = "ocean"
    if clear_spawn_profile:
        biome["spawnProfile"] = {
            "monsterParameters": {},
            "spawnTypes": [],
        }
    compiled_biomes(document).append(biome)
    return len(compiled_biomes(document))


def _ocean_island_and_floor_selector_index(
    region_data: dict[str, Any],
    surface_index: int,
    floor_index: int,
    cache: dict[tuple[int, int], int],
) -> int:
    """Union native sea-surface islands with the independently sloped seabed.

    Native MaxSelector shares its parameters with both source configs. Keep
    the original island parameters intact: IslandSurface hashes baseHeight as
    well as seed, so translating that recipe would change every island. The
    supported floor surface shapes use baseHeight only in flatSurface leaves;
    rebasing those leaves preserves their density at every coordinate, while
    seedBias preserves the floor's independent random seed. Unknown shapes
    fail explicitly instead of silently moving or re-seeding authored terrain.
    """

    key = (int(surface_index), int(floor_index))
    if key in cache:
        return cache[key]
    selectors = region_data.get("terrainSelectors")
    if not isinstance(selectors, list) or any(
        not 1 <= index <= len(selectors) for index in key
    ):
        raise WorldFormatError("Ocean island/floor terrain selector index is invalid")
    surface = selectors[key[0] - 1]
    floor = selectors[key[1] - 1]
    parameters = deep_copy_json(surface.get("parameters", {}))
    floor_parameters = floor.get("parameters", {})
    delta = float(floor_parameters.get("baseHeight", 0.0)) - float(
        parameters.get("baseHeight", 0.0)
    )

    def rebase(config: Any) -> dict[str, Any]:
        if not isinstance(config, dict) or not config.get("type"):
            raise WorldFormatError("Ocean-floor source selector config is invalid")
        result = deep_copy_json(config)
        kind = result["type"]
        if kind == "flatSurface":
            result["adjustment"] = float(result.get("adjustment", 0.0)) + delta
        elif kind in ("constant", "perlin"):
            pass  # These native selectors do not read baseHeight.
        elif kind in ("displacement", "rotate", "cache"):
            result["source"] = rebase(result.get("source"))
        elif kind in ("max", "minmax"):
            result["sources"] = [rebase(source) for source in result["sources"]]
        elif kind == "mix":
            for source_key in ("mixSource", "aSource", "bSource"):
                result[source_key] = rebase(result.get(source_key))
        elif delta:
            raise WorldFormatError(
                "Cannot preserve the ocean-floor selector {!r} while restoring "
                "native offshore islands; use a flat/displacement surface recipe".format(kind)
            )
        return result

    island_config = deep_copy_json(surface.get("config", {}))
    if not isinstance(island_config, dict) or not island_config.get("type"):
        raise WorldFormatError("Ocean surface terrain selector config is invalid")
    # seedBias is interpreted only by this newly added MaxSelector parent;
    # a root config's pre-existing seedBias was not used when loaded standalone.
    island_config["seedBias"] = 0
    floor_config = rebase(floor.get("config"))
    seed_bias = (
        int(floor_parameters.get("seed", 0)) - int(parameters.get("seed", 0))
    ) % (1 << 64)
    # Starbound Json stores signed int64; getUInt subsequently casts it back.
    # Writing a positive value >= 2**63 would overflow the native SBON VLQ.
    floor_config["seedBias"] = seed_bias if seed_bias < (1 << 63) else seed_bias - (1 << 64)
    result = _register_terrain_selector(region_data, {
        "type": "max", "parameters": parameters,
        "config": {"type": "max", "sources": [island_config, floor_config]},
    })
    cache[key] = result
    return result


def _floor_ecology_biome_index(
    document: dict[str, Any],
    floor_biome_index: int,
    surface_biome_index: int,
    vertical_origin: float,
    spawn_biome_index: int | None = None,
    *,
    remove_microdungeons: bool = False,
    remove_tall_aquatic_plants: bool = False,
    remove_all_plants: bool = False,
    vegetation_density_multiplier: float = 1.0,
    clear_spawn_profile: bool = False,
) -> int:
    """Build a seabed block biome for one vertical part of a local basin.

    The floor's surface distributions must remain the block biome immediately
    below every point on the curved bed.  Mirroring them into underground
    placeables also covers enclosed/cave-backed parts of a very deep local
    basin.  Surface-ocean distributions are deliberately never copied here:
    otherwise island microdungeons, coconut trees and land monsters can be
    evaluated on a submerged curved bed.

    Runtime monster spawning reads ``blockBiomeIndex`` rather than
    ``environmentBiomeIndex``.  The basin supplies the floor spawn pool for its
    ecology halo; parallax, music and ambient sound continue to come from the
    independently selected environment biome.  Safety variants remove
    microdungeons or vegetation near the liquid surface without changing the
    compiled floor recipe used in deep water.
    """

    _anchor_biome_parallax(document, floor_biome_index, vertical_origin)
    floor_biome = deep_copy_json(biome_at(document, floor_biome_index))
    if spawn_biome_index is not None:
        spawn_biome = biome_at(document, int(spawn_biome_index))
        if "spawnProfile" in spawn_biome:
            floor_biome["spawnProfile"] = deep_copy_json(
                spawn_biome["spawnProfile"]
            )
    surface_placeables = floor_biome.get("surfacePlaceables")
    if isinstance(surface_placeables, dict):
        floor_biome["undergroundPlaceables"] = deep_copy_json(surface_placeables)
    _prepare_biome_placeables(
        floor_biome,
        remove_microdungeons=remove_microdungeons,
        remove_tall_aquatic_plants=remove_tall_aquatic_plants,
        remove_all_plants=remove_all_plants,
        vegetation_density_multiplier=vegetation_density_multiplier,
    )
    if clear_spawn_profile:
        floor_biome["spawnProfile"] = {
            "monsterParameters": {}, "spawnTypes": [],
        }
    compiled_biomes(document).append(floor_biome)
    return len(compiled_biomes(document))


def _ocean_floor_placement_band(liquid_level: float, block_y: int) -> int:
    """Select a floor-placeable band using its placement Y, not profile relief.

    Starbound gets a floor item's distribution from the block at ``y - 1``.
    Consequently a cell at ``block_y`` supplies the item placed one tile above
    it. Regions store integer, lower-inclusive boundaries. The native engine
    can distort the lookup coordinate using blockNoise and suppress placement
    at region transitions; this layout classification is not an exact-Y
    runtime filter when those mechanisms are active.
    """

    depth = float(liquid_level) - (int(block_y) + 1)
    if depth <= OCEAN_FLOOR_ALL_PLANT_CLEARANCE:
        return 3
    if depth <= OCEAN_FLOOR_TALL_PLANT_CLEARANCE:
        return 2
    if depth <= OCEAN_FLOOR_MICRODUNGEON_CLEARANCE:
        return 1
    return 0


def _ocean_floor_transition_density_compensation(
    document: dict[str, Any],
    region_data: dict[str, Any],
    profile: list[dict[str, Any]],
    floor_biome_indexes: Sequence[int],
    liquid_level: float,
    y_start: int,
    y_end: int,
) -> dict[str, Any]:
    """Estimate missed nominal seabed placements without changing native rules.

    Samples follow the continuous profile's ground/air boundary, not a column
    of unrelated Y values.  This mirrors vanilla flat region weighting only:
    blockNoise, original floor relief, caves and item collisions remain native
    and are deliberately not claimed to be simulated.  A capped multiplier
    raises vegetation in the remaining eligible parts; no multiplier can make
    an entirely forbidden transition spawn an item.
    """

    import bisect

    maximum_samples = 8192
    minimum_band_samples = 8
    transition_threshold = 0.7
    maximum_compensation = 2.0
    width, height = (int(value) for value in document["size"])
    blending = max(0.0, float(region_data.get("regionBlending", 0.0)))
    layers = region_data["layers"]
    layer_starts = [int(layer["yStart"]) for layer in layers]
    ordered_profile = sorted(profile, key=lambda item: int(item["logicalStart"]))
    profile_starts = [int(item["logicalStart"]) for item in ordered_profile]
    length = max(int(item["logicalEnd"]) for item in ordered_profile) + 1
    if length <= maximum_samples:
        offsets = list(range(length))
    else:
        # Keep every generated profile endpoint (including shore-exclusion
        # and relief-depth splits), then spread the remaining budget evenly.
        essential = {0, length - 1}
        for item in ordered_profile:
            essential.add(int(item["logicalStart"]))
            essential.add(int(item["logicalEnd"]))
        if len(essential) > maximum_samples:
            ordered = sorted(essential)
            essential = {
                ordered[round(i * (len(ordered) - 1) / (maximum_samples - 1))]
                for i in range(maximum_samples)
            }
        budget = maximum_samples - len(essential)
        offsets = sorted(essential | {
            round(i * (length - 1) / max(1, budget - 1))
            for i in range(budget)
        })

    def strongest_region(x: int, y: int) -> tuple[float, int]:
        layer_index = bisect.bisect_right(layer_starts, y) - 1
        if layer_index < 0 or y >= height:
            return 0.0, 0
        if blending <= 0:
            layer = layers[layer_index]
            cell_index = bisect.bisect_left(layer["boundaries"], x % width)
            return 1.0, int(layer["cells"][cell_index]["blockBiomeIndex"])
        if layer_index > 0 and y - layer_starts[layer_index] < blending / 2.0:
            weight = 0.5 + (y - layer_starts[layer_index]) / blending
            candidates = [(layer_index, weight), (layer_index - 1, 1.0 - weight)]
        elif (
            layer_index + 1 < len(layers)
            and y > layer_starts[layer_index + 1] - blending / 2.0
        ):
            weight = 0.5 - (layer_starts[layer_index + 1] - y) / blending
            candidates = [(layer_index, 1.0 - weight), (layer_index + 1, weight)]
        else:
            candidates = [(layer_index, 1.0)]
        strongest = (0.0, 0)
        for index, y_weight in candidates:
            layer = layers[index]
            boundaries = layer["boundaries"]
            cells = layer["cells"]
            count = len(cells)
            wrapped_x = x % width
            cell_index = bisect.bisect_left(boundaries, wrapped_x)
            neighbors = (
                (cell_index, wrapped_x),
                ((cell_index - 1) % count, wrapped_x + (width if cell_index == 0 else 0)),
                ((cell_index + 1) % count, wrapped_x - (width if cell_index == count - 1 else 0)),
            )
            weights = []
            for neighbor, coordinate_x in neighbors:
                left = 0 if neighbor == 0 else int(boundaries[neighbor - 1])
                right = width if neighbor == count - 1 else int(boundaries[neighbor])
                distance = (
                    coordinate_x - right
                    if coordinate_x > (left + right) / 2.0
                    else left - coordinate_x
                )
                weights.append(max(0.0, min(1.0, 0.5 - distance / blending)))
            total = sum(weights)
            if total <= 0:
                continue
            for (neighbor, _), x_weight in zip(neighbors, weights):
                weight = x_weight * y_weight / total
                if weight > strongest[0]:
                    strongest = (weight, int(cells[neighbor]["blockBiomeIndex"]))
        return strongest

    bands = [
        {
            "band": name, "blockBiomeIndex": int(index),
            "sampledGroundPositions": 0, "eligibleGroundPositions": 0,
            "estimatedGroundHorizontalTiles": 0, "estimatedEligibleHorizontalTiles": 0,
        }
        for name, index in zip(
            ("deep", "no-microdungeons", "no-tall-plants"), floor_biome_indexes[:3],
        )
    ]
    excluded_by_shallow_rule = 0
    for sample_index, offset in enumerate(offsets):
        item_index = bisect.bisect_right(profile_starts, offset) - 1
        item = ordered_profile[item_index]
        x = int(item["xStart"]) + offset - int(item["logicalStart"])
        nominal_ground_y = (
            float(item["baseHeight"])
            + float(item["slope"]) * (x - float(item["midpointX"]))
        )
        placement_y = int(math.ceil(nominal_ground_y))
        if not int(y_start) < placement_y <= int(y_end):
            continue
        band_index = _ocean_floor_placement_band(liquid_level, placement_y - 1)
        if band_index >= len(bands):
            excluded_by_shallow_rule += 1
            continue
        band = bands[band_index]
        support_start = (
            0 if sample_index == 0 else (offsets[sample_index - 1] + offset) // 2 + 1
        )
        support_end = (
            length - 1 if sample_index + 1 == len(offsets)
            else (offset + offsets[sample_index + 1]) // 2
        )
        horizontal_tiles = support_end - support_start + 1
        band["sampledGroundPositions"] += 1
        band["estimatedGroundHorizontalTiles"] += horizontal_tiles
        weight_here, _ = strongest_region(x, placement_y)
        weight_below, index_below = strongest_region(x, placement_y - 1)
        if (
            min(weight_here, weight_below) + 1e-12 >= transition_threshold
            and index_below == band["blockBiomeIndex"]
        ):
            band["eligibleGroundPositions"] += 1
            band["estimatedEligibleHorizontalTiles"] += horizontal_tiles

    for band in bands:
        total = int(band["estimatedGroundHorizontalTiles"])
        eligible = int(band["estimatedEligibleHorizontalTiles"])
        fraction = eligible / total if total else None
        enough_samples = int(band["sampledGroundPositions"]) >= minimum_band_samples
        factor = (
            min(maximum_compensation, total / eligible)
            if enough_samples and eligible > 0 and blending > 0
            else 1.0
        )
        band.update({
            "estimatedEligibleFraction": round(fraction, 6) if fraction is not None else None,
            "compensationMultiplier": round(factor, 6),
            "totalVegetationMultiplier": round(
                OCEAN_FLOOR_VEGETATION_DENSITY_MULTIPLIER * factor, 6,
            ),
            "sufficientSamples": enough_samples,
            "zeroEligibleCannotBeRecoveredByDensity": bool(total and not eligible),
            "capped": bool(eligible and total / eligible > maximum_compensation),
        })
    return {
        "method": "nominal-seabed-ground-flat-region-weight-estimate",
        "sampledPositions": len(offsets),
        "sampleLimit": maximum_samples,
        "samplingStep": max((right - left for left, right in zip(offsets, offsets[1:])), default=1),
        "shallowNoPlantRuleSamplesExcluded": excluded_by_shallow_rule,
        "nativeTransitionThresholdAssumed": transition_threshold,
        "maximumCompensationMultiplier": maximum_compensation,
        "baseVegetationMultiplier": OCEAN_FLOOR_VEGETATION_DENSITY_MULTIPLIER,
        "nativeNoiseOrOriginalReliefSimulated": False,
        "changesNativeNoiseOrBlending": False,
        "repairsZeroPlacementTransitionGaps": False,
        "bands": bands,
    }


def _apply_ocean_coastal_basin(
    document: dict[str, Any],
    region_data: dict[str, Any],
    layers: list[dict[str, Any]],
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    floor_height: float,
    shore_height: float,
    transition_height: int,
    floor_cell: dict[str, Any],
    surface_cell: dict[str, Any],
    sample_width: int,
    ecology_height: int,
    solid_edges: tuple[bool, bool],
) -> dict[str, Any] | None:
    """Overlay a noisy ocean-floor basin and its near-bed ecology halo."""

    if not any(solid_edges) or shore_height <= floor_height:
        return None
    requested_sample_width = max(1, int(sample_width))
    region_blending = max(0.0, float(region_data.get("regionBlending", 0.0)))
    terrain_selector_index = floor_cell.get("terrainSelectorIndex")
    terrain_relief: float | None = None
    selectors = region_data.get("terrainSelectors")
    if (
        isinstance(terrain_selector_index, int)
        and isinstance(selectors, list)
        and 1 <= terrain_selector_index <= len(selectors)
    ):
        terrain_relief = _terrain_selector_positive_relief(
            selectors[terrain_selector_index - 1].get("config")
        )
    # Relief is introduced no shallower than 100 tiles below the liquid
    # surface, and is delayed further when the compiled floor selector can
    # rise by more than 100 tiles.  This guarantees that the original noise
    # can never push the seabed above the water surface.
    relief_is_bounded = terrain_relief is not None
    relief_clearance = (
        max(
            float(OCEAN_FLOOR_RELIEF_START_DEPTH),
            max(0.0, float(terrain_relief)),
        )
        if relief_is_bounded
        else max(
            float(OCEAN_FLOOR_RELIEF_START_DEPTH),
            float(shore_height) - float(floor_height) + 1.0,
        )
    )
    # validBiomeItems() deliberately skips tiles whose strongest region has a
    # weight below world_template.config's biomeTransitionThreshold (0.7 in
    # vanilla).  With 32-tile cells and an 80-tile blend, every point on this
    # world's old basin was a transition and no flora/object distribution was
    # ever run.  Four blend widths leave a large stable centre in every sample;
    # the engine still interpolates adjacent selector heights into a smooth
    # shore-to-floor curve.
    effective_sample_width = max(
        requested_sample_width,
        int(math.ceil(region_blending * 4.0)),
    )
    profile = _coastal_basin_profile_segments(
        x_start,
        x_end,
        int(document["size"][0]),
        float(floor_height),
        float(shore_height),
        effective_sample_width,
        solid_edges[0],
        solid_edges[1],
        float(shore_height) - relief_clearance,
        OCEAN_SHORE_ISLAND_EXCLUSION_WIDTH,
    )
    if not profile:
        return None

    requested_ecology_height = max(0, int(ecology_height))
    # A one-tile halo is easily crossed by the preserved native biome noise.
    # Keep a small local minimum, without reducing larger requested halos or
    # changing global noise, terrain, parallax, or the ocean's liquid level.
    ecology_height = max(OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT, requested_ecology_height)
    liquid_level = float(surface_cell.get("oceanLiquidLevel", shore_height))
    height = int(document["size"][1])
    # ``oceanLiquidLevel`` is the upper boundary of the filled liquid column.
    # The last definitely submerged tile is therefore one tile below it.
    submerged_ceiling = min(
        int(y_end), max(int(y_start), int(math.ceil(liquid_level)) - 1)
    )
    selector_cache: dict[tuple[int, float], int] = {}
    sloped_selector_cache: dict[
        tuple[int, float, float, float, bool], int
    ] = {}
    shifted_cells: dict[tuple[float, float, float, bool], dict[str, Any]] = {}
    island_selector_cache: dict[tuple[int, int], int] = {}
    floor_environment = int(floor_cell["environmentBiomeIndex"])
    surface_environment = int(surface_cell["environmentBiomeIndex"])
    _anchor_biome_parallax(document, floor_environment, float(floor_height))
    width = int(document["size"][0])
    floor_ecology_biome = _floor_ecology_biome_index(
        document,
        floor_environment,
        surface_environment,
        float(floor_height),
        floor_environment,
    )
    floor_no_microdungeon_biome = _floor_ecology_biome_index(
        document,
        floor_environment,
        surface_environment,
        float(floor_height),
        floor_environment,
        remove_microdungeons=True,
    )
    floor_no_tall_plants_biome = _floor_ecology_biome_index(
        document,
        floor_environment,
        surface_environment,
        float(floor_height),
        floor_environment,
        remove_microdungeons=True,
        remove_tall_aquatic_plants=True,
    )
    floor_no_plants_biome = _floor_ecology_biome_index(
        document,
        floor_environment,
        surface_environment,
        float(floor_height),
        floor_environment,
        remove_microdungeons=True,
        remove_all_plants=True,
        clear_spawn_profile=True,
    )
    # Ocean grass/bushes and grass modifiers are allowed throughout the water
    # column. Ocean trees are allowed only in its upper 30 layout-depth tiles
    # Ordinary objects and the surface monster pool remain disabled below
    # water. Offshore, the original Ocean block biome is restored above the
    # actual liquid level so native islands have their complete ecology. The
    # native boundary distortion is retained (and reported), not disabled.
    # Island prefabs stay in actual-sea-level ocean mode, outside the shore
    # guard. Their original floor mode must never be applied to the basin bed.
    underwater_islands_only_biome = _surface_ocean_control_biome_index(
        document,
        surface_environment,
        keep_only_island_microdungeons=True,
        clear_spawn_profile=True,
        allow_ocean_plants=True,
    )
    underwater_empty_biome = _surface_ocean_control_biome_index(
        document,
        surface_environment,
        keep_only_island_microdungeons=True,
        remove_island_microdungeons=True,
        clear_spawn_profile=True,
        allow_ocean_plants=True,
    )
    shallow_ocean_biome = _surface_ocean_control_biome_index(
        document,
        surface_environment,
        keep_only_island_microdungeons=True,
        clear_spawn_profile=True,
        allow_ocean_plants=True,
        allow_ocean_trees=True,
    )
    surface_no_islands_biome = _surface_ocean_control_biome_index(
        document,
        surface_environment,
        keep_only_island_microdungeons=True,
        remove_island_microdungeons=True,
        clear_spawn_profile=True,
        allow_ocean_plants=True,
        allow_ocean_trees=True,
    )
    # A region cell cannot change biome part-way through its vertical layer.
    # Split at every sampled local ecology ceiling so the requested halo is
    # actually bounded instead of leaking through the complete water column.
    ecology_boundaries: set[int] = set()
    for item in profile:
        segment_top = max(
            float(item["startHeight"]),
            float(item["endHeight"]),
        )
        use_floor_relief = (
            relief_is_bounded
            and segment_top <= float(shore_height) - relief_clearance
        )
        estimated_terrain_top = segment_top + (
            max(0.0, float(terrain_relief or 0.0)) if use_floor_relief else 0.0
        )
        item["estimatedTerrainTop"] = estimated_terrain_top
        ecology_top = _ocean_floor_ecology_top(
            estimated_terrain_top,
            ecology_height,
            submerged_ceiling,
        )
        item["ecologyTop"] = ecology_top
        boundary = ecology_top + 1
        if int(y_start) < boundary <= int(y_end):
            ecology_boundaries.add(boundary)
    for boundary in sorted(ecology_boundaries):
        _split_region_layer_at(layers, height, boundary)
    liquid_boundary = submerged_ceiling + 1
    if int(y_start) < liquid_boundary <= int(y_end):
        _split_region_layer_at(layers, height, liquid_boundary)

    # The profile's estimated highest point only bounds the ecology halo.  It
    # must never select safety rules for an entire sloping X cell: a 320-tile
    # cell can span hundreds of vertical tiles and would incorrectly remove
    # plants from its deep end.  These rules are horizontal placement-Y bands
    # measured from this ocean's actual saved liquid level.  Account for the
    # engine reading floor distributions one block below the placement point.
    safety_boundaries = {
        int(math.ceil(liquid_level - clearance)) - 1
        for clearance in (
            OCEAN_FLOOR_ALL_PLANT_CLEARANCE,
            OCEAN_FLOOR_TALL_PLANT_CLEARANCE,
            OCEAN_FLOOR_MICRODUNGEON_CLEARANCE,
        )
    }
    ocean_tree_boundary = int(math.ceil(
        liquid_level - OCEAN_SURFACE_TREE_MAXIMUM_DEPTH
    )) - 1
    if int(y_start) < ocean_tree_boundary <= int(y_end):
        _split_region_layer_at(layers, height, ocean_tree_boundary)
    for boundary in sorted(safety_boundaries):
        if int(y_start) < boundary <= int(y_end):
            _split_region_layer_at(layers, height, boundary)
    floor_safety_biomes = (
        floor_ecology_biome,
        floor_no_microdungeon_biome,
        floor_no_tall_plants_biome,
        floor_no_plants_biome,
    )

    logical_length = max(int(item["logicalEnd"]) for item in profile) + 1

    changed_layers = 0
    flattened_segments: set[tuple[int, int]] = set()
    for layer in layers:
        layer_y = int(layer["yStart"])
        if not int(y_start) <= layer_y <= int(y_end):
            continue
        replacements: list[tuple[int, int, dict[str, Any]]] = []
        for item in profile:
            base_height = float(item["baseHeight"])
            use_floor_relief = (
                relief_is_bounded
                and
                max(
                    float(item["startHeight"]),
                    float(item["endHeight"]),
                )
                <= float(shore_height) - relief_clearance
            )
            shifted_key = (
                base_height,
                float(item["midpointX"]),
                float(item["slope"]),
                use_floor_relief,
            )
            shifted = shifted_cells.get(shifted_key)
            if shifted is None:
                shifted = _shift_region_cell_selector_heights(
                    region_data,
                    floor_cell,
                    base_height,
                    selector_cache,
                )
                # The surface ocean owns the water column.  The floor recipe
                # owns the terrain/cave/ore selectors underneath it.
                for key in (
                    "oceanLiquid",
                    "oceanLiquidLevel",
                    "encloseLiquids",
                ):
                    if key in surface_cell:
                        shifted[key] = deep_copy_json(surface_cell[key])
                # One wide region now carries a sloped selector instead of a
                # single flat base height.  The quintic shore curve is thus
                # evaluated continuously across the cell while leaving a wide
                # non-transition centre where biome items can be placed.
                # The source floor relief starts exactly 100 tiles below the
                # water surface; the profile builder inserts a cell boundary
                # at that threshold so this choice is not sample-width bound.
                if isinstance(terrain_selector_index, int):
                    shifted["terrainSelectorIndex"] = (
                        _sloped_ocean_floor_selector_index(
                            region_data,
                            terrain_selector_index,
                            width,
                            float(item["midpointX"]),
                            base_height,
                            float(item["slope"]),
                            not use_floor_relief,
                            sloped_selector_cache,
                        )
                    )
                shifted_cells[shifted_key] = shifted
            replacement = deep_copy_json(shifted)
            # Ocean-floor ecology follows the bed in either environment band.
            # Near the liquid surface it progressively removes microdungeons,
            # tall aquatic plants and finally all plants.  The remaining water
            # column uses depth-specific Ocean plants, but no ordinary Ocean
            # objects or monsters. Island microdungeons survive only outside
            # the 750-tile horizontal shore guard and only at the true surface.
            # Environment selection independently supplies parallax, music and
            # ambient sound.  The real liquid level is retained everywhere.
            below_transition = layer_y < int(transition_height)
            in_floor_ecology = layer_y <= int(item["ecologyTop"])
            left_distance = int(item["logicalStart"])
            right_distance = logical_length - 1 - int(item["logicalEnd"])
            near_solid_shore = bool(
                solid_edges[0]
                and left_distance <= OCEAN_SHORE_ISLAND_EXCLUSION_WIDTH
                or solid_edges[1]
                and right_distance <= OCEAN_SHORE_ISLAND_EXCLUSION_WIDTH
            )
            if not near_solid_shore:
                # A basin is not a replacement for the ocean's island density
                # field. Retain both solids outside the shore guard, including
                # each island's submerged underside. Using only the floor
                # selector in every layer used to erase all offshore islands.
                surface_selector = surface_cell.get("terrainSelectorIndex")
                floor_selector = replacement.get("terrainSelectorIndex")
                if isinstance(surface_selector, int) and isinstance(floor_selector, int):
                    replacement["terrainSelectorIndex"] = (
                        _ocean_island_and_floor_selector_index(
                            region_data, surface_selector, floor_selector,
                            island_selector_cache,
                        )
                    )
                if not in_floor_ecology:
                    # The island/water column needs the Ocean's cave, subblock
                    # and ore rules too; floor caves would otherwise perforate
                    # the islands. Do not change any floor-ecology cell below.
                    for selector_key in (
                        *_REGION_CELL_SELECTOR_KEYS[1:],
                        *_REGION_CELL_SELECTOR_LIST_KEYS,
                    ):
                        if selector_key in surface_cell:
                            replacement[selector_key] = deep_copy_json(surface_cell[selector_key])
                        else:
                            replacement.pop(selector_key, None)
            if in_floor_ecology:
                replacement["blockBiomeIndex"] = floor_safety_biomes[
                    _ocean_floor_placement_band(liquid_level, layer_y)
                ]
            else:
                if not near_solid_shore and layer_y >= liquid_boundary:
                    replacement["blockBiomeIndex"] = int(surface_cell["blockBiomeIndex"])
                elif layer_y >= ocean_tree_boundary:
                    replacement["blockBiomeIndex"] = (
                        surface_no_islands_biome
                        if near_solid_shore else shallow_ocean_biome
                    )
                else:
                    replacement["blockBiomeIndex"] = (
                        underwater_empty_biome
                        if near_solid_shore else underwater_islands_only_biome
                    )
            replacement["environmentBiomeIndex"] = (
                floor_environment if below_transition else surface_environment
            )
            replacements.append(
                (int(item["xStart"]), int(item["xEnd"]), replacement)
            )
            if (
                max(float(item["startHeight"]), float(item["endHeight"]))
                > float(shore_height) - relief_clearance
            ):
                flattened_segments.add((int(item["xStart"]), int(item["xEnd"])))
        _overlay_layer_cells(layer, width, replacements)
        changed_layers += 1

    density_compensation = _ocean_floor_transition_density_compensation(
        document, region_data, profile, floor_safety_biomes, liquid_level,
        int(y_start), int(y_end),
    )
    # Each variant above was cloned from the unboosted source recipe.  Apply
    # the original +40% and the capped compensation together in ONE pass;
    # repeated rebuilding from assets must not multiply an earlier boost.
    for band in density_compensation["bands"]:
        _prepare_biome_placeables(
            biome_at(document, int(band["blockBiomeIndex"])),
            vegetation_density_multiplier=float(band["totalVegetationMultiplier"]),
        )

    return {
        "enabled": True,
        "leftSolidShore": bool(solid_edges[0]),
        "rightSolidShore": bool(solid_edges[1]),
        "profileSegments": len(profile),
        "sampleWidth": requested_sample_width,
        "effectiveSampleWidth": max(
            int(item["logicalEnd"]) - int(item["logicalStart"]) + 1
            for item in profile
        ),
        "continuousSelectorSlopes": True,
        "floorEcologyHeight": ecology_height,
        "floorEcologyRequestedHeight": requested_ecology_height,
        "floorEcologyMinimumHeight": OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT,
        "floorEcologyMaximumY": submerged_ceiling,
        "floorEcologyFollowsTerrainSurface": True,
        "floorEcologyUsesRealLiquidLevel": True,
        "floorSafetyUsesPlacementY": True,
        "floorSafetyCoordinateSpace": "layout-block-y-plus-one",
        "floorSafetyReference": "oceanLiquidLevel",
        "floorSafetyExactRuntimeYGuaranteed": not bool(region_data.get("blockNoise")),
        "floorSafetyLiquidLevel": liquid_level,
        "floorSafetyLayerBoundaries": sorted(safety_boundaries),
        # Native block noise and biome transition suppression are properties
        # of the whole layout.  Preserve them and expose their presence rather
        # than claiming that layout cutoffs override native placement rules.
        "floorSafetyNativeRegionBlending": region_blending,
        "floorSafetyNativeBlockNoise": bool(region_data.get("blockNoise")),
        # Keep the old key for project/diagnostic compatibility.  It has always
        # been a block-biome index despite the historical name.
        "floorEcologyEnvironmentBiomeIndex": floor_ecology_biome,
        "floorEcologyBlockBiomeIndex": floor_ecology_biome,
        # Compatibility alias retained for older project/audit readers.  The
        # former surface-spawn seabed variant is intentionally gone.
        "surfaceSeabedBlockBiomeIndex": floor_ecology_biome,
        "floorEcologyBlockBiomeIndexes": [
            floor_ecology_biome,
            floor_no_microdungeon_biome,
            floor_no_tall_plants_biome,
            floor_no_plants_biome,
        ],
        "underwaterSurfaceBlockBiomeIndex": underwater_islands_only_biome,
        "underwaterShoreBlockBiomeIndex": underwater_empty_biome,
        "surfaceNoIslandBlockBiomeIndex": surface_no_islands_biome,
        "shallowSurfaceBlockBiomeIndex": shallow_ocean_biome,
        "shallowShoreBlockBiomeIndex": surface_no_islands_biome,
        "surfaceControlBlockBiomeIndexes": [
            underwater_islands_only_biome,
            underwater_empty_biome,
            shallow_ocean_biome,
            surface_no_islands_biome,
        ],
        "floorParallaxVerticalOrigin": float(floor_height),
        "floorReliefStartDepth": OCEAN_FLOOR_RELIEF_START_DEPTH,
        "microdungeonClearanceBelowLiquid": (
            OCEAN_FLOOR_MICRODUNGEON_CLEARANCE
        ),
        "tallPlantClearanceBelowLiquid": OCEAN_FLOOR_TALL_PLANT_CLEARANCE,
        "allPlantClearanceBelowLiquid": OCEAN_FLOOR_ALL_PLANT_CLEARANCE,
        "floorMonsterClearanceBelowLiquid": OCEAN_FLOOR_ALL_PLANT_CLEARANCE,
        "floorShallowMonsterPoolRemoved": True,
        "vegetationDensityMultiplier": (
            OCEAN_FLOOR_VEGETATION_DENSITY_MULTIPLIER
        ),
        "transitionVegetationCompensation": density_compensation,
        "shoreIslandExclusionWidth": OCEAN_SHORE_ISLAND_EXCLUSION_WIDTH,
        "offshoreIslandTerrainRestored": True,
        "offshoreIslandTerrainSelectorIndex": surface_cell.get("terrainSelectorIndex"),
        "offshoreSurfaceBlockBiomeIndex": int(surface_cell["blockBiomeIndex"]),
        "offshoreSurfaceEcologyMinimumY": liquid_boundary,
        "offshoreSurfaceOriginalRecipeRestored": True,
        "shoreIslandExclusionCoordinateSpace": "layout-x-inclusive-distance",
        "shoreIslandExclusionExactRuntimeXGuaranteed": (
            not bool(region_data.get("blockNoise")) and region_blending == 0
        ),
        "submergedSurfaceOceanPlaceablesRemoved": False,
        "submergedSurfaceOceanObjectsRemoved": True,
        "submergedSurfaceOceanBushGrassRetained": True,
        "submergedSurfaceOceanGrassModsRetained": True,
        "surfaceOceanTreeMaximumDepth": OCEAN_SURFACE_TREE_MAXIMUM_DEPTH,
        "surfaceOceanTreeLayerBoundary": ocean_tree_boundary,
        "surfaceOceanSafetyCoordinateSpace": "layout-block-y-plus-one",
        "surfaceOceanSafetyExactRuntimeYGuaranteed": not bool(region_data.get("blockNoise")),
        "submergedSurfaceOceanMonsterPoolRemoved": True,
        "floorReliefSafetyClearance": relief_clearance,
        "floorEcologyLayerBoundaries": len(ecology_boundaries),
        "flattenedShoreSegments": len(flattened_segments),
        "estimatedFloorTerrainRelief": terrain_relief,
        "oceanFloorTransitionY": int(transition_height),
        "minimumBaseHeight": min(float(item["baseHeight"]) for item in profile),
        "maximumBaseHeight": max(float(item["baseHeight"]) for item in profile),
        "changedLayers": changed_layers,
    }


def merge_json_object(
    target: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """Recursively merge JSON-object overrides into a copied object."""

    result = deep_copy_json(target)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_json_object(result[key], value)
        else:
            result[key] = deep_copy_json(value)
    return result


def _build_asset_region_cell(
    document: dict[str, Any],
    compiler: AssetBiomeCompiler,
    region_data: dict[str, Any],
    biome_name: str,
    base_height: float,
    ocean_level_override: int | None,
    seed: int,
    biome_asset_overrides: dict[str, Any] | None = None,
    region_recipe_overrides: dict[str, Any] | None = None,
    *,
    preferred_biome_indexes: Sequence[int] = (),
    hue_audit: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, list[str]]:
    """Compile one complete biome and its fresh terrain/liquid region cell."""

    width = int(document["size"][0])
    parameters = world_template(document).get("worldParameters", {})
    threat_level = (
        float(parameters.get("threatLevel", 1.0))
        if isinstance(parameters, dict)
        else 1.0
    )
    original_biome = compiler.catalog["biomes"].get(biome_name)
    if biome_asset_overrides:
        if not isinstance(original_biome, dict):
            raise ValueError(f"Biome asset is not an object: {biome_name}")
        compiler.catalog["biomes"][biome_name] = merge_json_object(
            original_biome, biome_asset_overrides
        )
    try:
        hue_selection, hue_details = _world_biome_hue_selection(
            document, biome_name, compiler.catalog["biomes"][biome_name],
            preferred_indexes=preferred_biome_indexes,
            explicit_overrides=biome_asset_overrides,
        )
        compiled, warnings = compiler.compile(
            biome_name,
            _stable_asset_seed(seed, biome_name, "biome"),
            float(base_height),
            {},
            threat_level,
            hue_selection=hue_selection,
            native_extensions=_world_uses_native_extensions(document),
        )
        if hue_audit is not None:
            hue_audit.update(hue_details)
    finally:
        if biome_asset_overrides:
            compiler.catalog["biomes"][biome_name] = original_biome
    biomes = compiled_biomes(document)
    try:
        biome_index = biomes.index(compiled) + 1
    except ValueError:
        biomes.append(compiled)
        biome_index = len(biomes)

    recipe_rng = random.Random(_stable_asset_seed(seed, biome_name, "region"))
    recipe = compiler.region_recipe(biome_name, recipe_rng)
    if region_recipe_overrides:
        recipe = merge_json_object(recipe, region_recipe_overrides)

    def selector_index(
        name: str | None, purpose: str, commonality: float = 1.0
    ) -> int:
        if not name:
            return 0xFFFFFFFF
        selector = compiler.terrain_selector(
            name,
            width,
            float(base_height),
            float(commonality),
            _stable_asset_seed(seed, biome_name, purpose),
        )
        return _register_terrain_selector(region_data, selector)

    sub_block_indexes = [
        selector_index(
            recipe.get("subBlockSelector"), f"subBlock:{index}"
        )
        for index, _material in enumerate(compiled.get("subBlocks", []))
        if recipe.get("subBlockSelector")
    ]
    foreground_ores: list[int] = []
    background_ores: list[int] = []
    for index, ore in enumerate(compiled.get("ores", [])):
        commonality = (
            float(ore[1])
            if isinstance(ore, list) and len(ore) == 2
            else 1.0
        )
        if recipe.get("fgOreSelector"):
            foreground_ores.append(
                selector_index(
                    recipe["fgOreSelector"], f"foregroundOre:{index}", commonality
                )
            )
        if recipe.get("bgOreSelector"):
            background_ores.append(
                selector_index(
                    recipe["bgOreSelector"], f"backgroundOre:{index}", commonality
                )
            )
    ocean_level = (
        int(ocean_level_override)
        if ocean_level_override is not None
        else int(round(float(base_height))) + int(recipe.get("oceanLevelOffset", 0))
    )
    cell = {
        "terrainSelectorIndex": selector_index(
            recipe.get("blockSelector"), "terrain"
        ),
        "foregroundCaveSelectorIndex": selector_index(
            recipe.get("fgCaveSelector"), "foregroundCave"
        ),
        "backgroundCaveSelectorIndex": selector_index(
            recipe.get("bgCaveSelector"), "backgroundCave"
        ),
        "blockBiomeIndex": biome_index,
        "environmentBiomeIndex": biome_index,
        "caveLiquid": int(recipe.get("caveLiquid", 0)),
        "caveLiquidSeedDensity": float(
            recipe.get("caveLiquidSeedDensity", 0.0)
        ),
        "oceanLiquid": int(recipe.get("oceanLiquid", 0)),
        "oceanLiquidLevel": ocean_level if recipe.get("oceanLiquid") else 0,
        "encloseLiquids": bool(recipe.get("encloseLiquids", False)),
        "fillMicrodungeons": bool(recipe.get("fillMicrodungeons", False)),
        "subBlockSelectorIndexes": sub_block_indexes,
        "foregroundOreSelectorIndexes": foreground_ores,
        "backgroundOreSelectorIndexes": background_ores,
    }
    return cell, biome_index, warnings


def _fallback_sub_block_selector(region_data: dict[str, Any]) -> int | None:
    for layer in region_data.get("layers", []):
        for cell in layer.get("cells", []):
            if not isinstance(cell, dict):
                continue
            indexes = cell.get("subBlockSelectorIndexes", [])
            if isinstance(indexes, list):
                for index in indexes:
                    if isinstance(index, int):
                        return index
    return None


def _split_region_layers_for_rectangle(
    document: dict[str, Any], y_start: int, y_end: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    height = int(document["size"][1])
    if not 0 <= y_start <= y_end < height:
        raise ValueError(f"纵坐标必须递增并位于 0 到 {height - 1} 之间")
    region_data = world_template(document).get("regionData")
    layers = region_data.get("layers") if isinstance(region_data, dict) else None
    if not isinstance(layers, list) or not layers:
        raise WorldFormatError("worldTemplate.regionData.layers is missing or empty")
    layers.sort(key=lambda layer: int(layer["yStart"]))

    _split_region_layer_at(layers, height, y_start)
    _split_region_layer_at(layers, height, y_end + 1)
    return region_data, layers


def _split_region_layer_at(
    layers: list[dict[str, Any]], world_height: int, y: int
) -> None:
    if (
        y <= 0
        or y >= world_height
        or any(int(layer["yStart"]) == y for layer in layers)
    ):
        return
    source_index = max(
        index for index, layer in enumerate(layers) if int(layer["yStart"]) < y
    )
    clone = deep_copy_json(layers[source_index])
    clone["yStart"] = y
    layers.insert(source_index + 1, clone)


def set_compiled_biome_rectangle(
    document: dict[str, Any],
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    target_biome_indexes: Iterable[int],
) -> dict[str, Any]:
    """Overlay a target compiled biome on an inclusive rectangular region.

    Existing terrain/cave/ore selector references are retained for each source
    cell so the rectangle keeps its layer-appropriate height and terrain shape.
    Both biome indexes are changed, which switches materials, placeables,
    spawning, parallax, music and other resolved biome properties.
    """

    width, height = (int(document["size"][0]), int(document["size"][1]))
    x_ranges = _wrapped_x_ranges(int(x_start), int(x_end), width)
    y_start = int(y_start)
    y_end = int(y_end)
    indexes = sorted({int(index) for index in target_biome_indexes})
    biome_count = len(compiled_biomes(document))
    if not indexes or any(index < 1 or index > biome_count for index in indexes):
        raise ValueError(f"目标 biome index 必须位于 1 到 {biome_count} 之间")

    region_data, layers = _split_region_layers_for_rectangle(
        document, y_start, y_end
    )
    sub_block_counts = {
        index: len(biome_at(document, index).get("subBlocks", []))
        for index in indexes
    }
    fallback_selector = _fallback_sub_block_selector(region_data)

    changed_layers = 0
    chosen_indexes: set[int] = set()
    for layer in layers:
        layer_y = int(layer["yStart"])
        if not y_start <= layer_y <= y_end:
            continue
        referenced = {
            int(cell.get("blockBiomeIndex"))
            for cell in layer.get("cells", [])
            if isinstance(cell, dict) and isinstance(cell.get("blockBiomeIndex"), int)
        }
        target_index = next((index for index in indexes if index in referenced), indexes[0])
        _overlay_layer_biome(
            layer,
            width,
            x_ranges,
            target_index,
            sub_block_counts,
            fallback_selector,
        )
        chosen_indexes.add(target_index)
        changed_layers += 1

    return {
        "targetBiomeIndexes": indexes,
        "appliedBiomeIndexes": sorted(chosen_indexes),
        "changedLayers": changed_layers,
        "regionBlending": region_data.get("regionBlending"),
    }


def sector_round_coordinate(
    value: float,
    *,
    is_start: bool,
    direction: str,
    limit: int,
    sector_size: int = WORLD_SECTOR_SIZE,
) -> int:
    """Round one inclusive rectangle edge to a selectable sector boundary.

    ``direction`` is ``lower`` or ``upper`` in world-coordinate space.  Start
    edges are inclusive sector starts; end edges are inclusive sector ends.
    This distinction prevents the common off-by-one sector seen when an end
    value is rounded to a multiple of 32 directly. Fractional player coordinates
    are accepted and must not be truncated before choosing a boundary.
    """

    try:
        if isinstance(value, bool):
            raise ValueError()
        value = float(value)
        if not math.isfinite(value):
            raise ValueError()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Coordinate must be a finite number") from exc
    limit = int(limit)
    sector_size = int(sector_size)
    if sector_size <= 0 or limit <= 0:
        raise ValueError("Sector size and coordinate limit must be positive")
    if not 0 <= value < limit:
        raise ValueError(f"Coordinate must be at least 0 and less than {limit}")
    mode = str(direction).strip().casefold()
    if mode not in {"lower", "upper"}:
        raise ValueError("Sector rounding direction must be lower or upper")

    if is_start:
        lower = math.floor(value / sector_size) * sector_size
        upper = (
            lower
            if value == lower
            else min(limit - 1, lower + sector_size)
        )
        return lower if mode == "lower" else upper

    # An inclusive end lies immediately before a geometric sector boundary.
    lower_boundary = math.floor(value / sector_size) * sector_size
    lower = (
        min(limit - 1, sector_size - 1)
        if lower_boundary == 0
        else lower_boundary - 1
    )
    upper = min(limit - 1, lower_boundary + sector_size - 1)
    return lower if mode == "lower" else upper


def sector_round_rectangle(
    world_width: int,
    world_height: int,
    x_start: float,
    x_end: float,
    y_start: float,
    y_end: float,
    *,
    x_start_direction: str = "lower",
    x_end_direction: str = "upper",
    y_start_direction: str = "lower",
    y_end_direction: str = "upper",
) -> dict[str, int]:
    """Snap four finite input coordinates to inclusive integer sector edges."""

    rounded = {
        "xStart": sector_round_coordinate(
            x_start,
            is_start=True,
            direction=x_start_direction,
            limit=world_width,
        ),
        "xEnd": sector_round_coordinate(
            x_end,
            is_start=False,
            direction=x_end_direction,
            limit=world_width,
        ),
        "yStart": sector_round_coordinate(
            y_start,
            is_start=True,
            direction=y_start_direction,
            limit=world_height,
        ),
        "yEnd": sector_round_coordinate(
            y_end,
            is_start=False,
            direction=y_end_direction,
            limit=world_height,
        ),
    }
    if float(y_start) > float(y_end):
        raise ValueError("Y start must not exceed Y end")
    if rounded["xStart"] > rounded["xEnd"] and float(x_start) <= float(x_end):
        raise ValueError("Rounded X edges do not form a non-wrapped rectangle")
    if rounded["yStart"] > rounded["yEnd"]:
        raise ValueError("Rounded Y edges do not form a rectangle")
    return rounded


def normalize_parallax_value(value: Any) -> dict[str, Any]:
    """Accept a raw/exported parallax JSON value and validate its structure."""

    if not isinstance(value, dict):
        raise ValueError("Parallax JSON must contain an object")
    candidate: Any = value
    if isinstance(candidate.get("parallax"), dict):
        candidate = candidate["parallax"]
    elif (
        candidate.get("component") == "background"
        and isinstance(candidate.get("value"), dict)
    ):
        candidate = candidate["value"]
    if not isinstance(candidate, dict):
        raise ValueError("Parallax JSON does not contain a parallax object")
    validate_finite_json_numbers(candidate, "Parallax JSON")
    layers = candidate.get("layers")
    if not isinstance(layers, list):
        raise ValueError("Parallax object must contain a layers array")
    for index, layer in enumerate(layers, start=1):
        if not isinstance(layer, dict):
            raise ValueError(f"Parallax layer {index} is not an object")
        textures = layer.get("textures")
        if not isinstance(textures, list) or not textures or not all(
            isinstance(texture, str) and texture for texture in textures
        ):
            raise ValueError(
                f"Parallax layer {index} must contain non-empty texture paths"
            )
    return deep_copy_json(candidate)


def load_parallax_json_file(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Parallax JSON was not found: {path}")
    with path.open("r", encoding="utf-8-sig") as stream:
        try:
            return normalize_parallax_value(json.load(stream))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Parallax JSON ({path}): {exc}") from exc


def apply_ordered_biome_parallaxes(
    document: dict[str, Any],
    layer_ids: Sequence[str],
    *,
    excluded_biome_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Give configured Main/Sub cells their own compiled biome's parallax.

    The native generator may assign every region in a layer the first biome's
    environment index. Clone only the environment carrier, replacing parallax
    from the cell's block biome; preserve music, ambience and every other field.
    Call for explicitly edited Ordered layers, before ocean post-processing.
    Existing-world repairs may exclude their already customized ocean biomes.

    This is an atomic, metadata-only operation. It never renumbers existing
    indexes or modifies saved tiles. If clones exceed the native byte limit,
    the caller may compact an independent document AND its records with
    compact_biome_table, then retry; this helper cannot compact without records.
    """
    if isinstance(layer_ids, (str, bytes)) or not isinstance(layer_ids, Sequence):
        raise ValueError("Ordered parallax layer ids must be an array")
    if any(not isinstance(value, str) or not value for value in layer_ids):
        raise ValueError("Ordered parallax layer ids must be non-empty strings")
    if (isinstance(excluded_biome_names, (str, bytes))
            or not isinstance(excluded_biome_names, Sequence)
            or any(not isinstance(value, str) for value in excluded_biome_names)):
        raise ValueError("Excluded biome names must be an array of strings")
    requested = list(dict.fromkeys(layer_ids))
    excluded = {name.casefold() for name in excluded_biome_names}
    template = world_template(document)
    biomes = compiled_biomes(document)
    validate_native_biome_indexes(document)
    summary: dict[str, Any] = {
        "layerIds": requested, "changedLayers": 0, "changedCells": 0,
        "clonedBiomes": 0, "skippedNullCells": 0, "skippedExcludedCells": 0,
        "biomeCountBefore": len(biomes), "biomeCountAfter": len(biomes),
        "changes": [],
    }
    if not requested:
        return summary
    parameters = {"visitableParameters": template.get("worldParameters")}
    settings = celestial_layer_settings(parameters)
    by_id = {setting["layerId"]: setting for setting in settings}
    for layer_id in requested:
        if layer_id not in by_id:
            raise ValueError(f"Unknown Ordered parallax layer: {layer_id}")
    height = int(document["size"][1])
    for setting in settings:
        minimum = setting["layerMinHeight"]
        if type(minimum) is not int or not 0 <= minimum < height:
            raise ValueError(f"Invalid Ordered parallax layer minimum: {setting['layerId']}")
    minima = sorted({setting["layerMinHeight"] for setting in settings})
    bands = []
    for layer_id in requested:
        start = by_id[layer_id]["layerMinHeight"]
        stop = next((minimum for minimum in minima if minimum > start), height)
        bands.append((layer_id, start, stop))
    region_data = template["regionData"]
    layers = deep_copy_json(region_data["layers"])
    starts = [layer.get("yStart") for layer in layers]
    if any(type(start) is not int for start in starts):
        raise WorldFormatError("Compiled Ordered parallax layers need integer yStart")
    if any(start not in starts for _layer_id, start, _stop in bands):
        raise WorldFormatError("Compiled Ordered parallax layer does not match its configured minimum")
    clones: list[dict[str, Any]] = []
    clone_indexes: dict[tuple[int, str], int] = {}
    changed_layers: set[int] = set()
    for layer_number, layer in enumerate(layers):
        layer_id = next((name for name, start, stop in bands
                         if start <= layer["yStart"] < stop), None)
        if layer_id is None:
            continue
        for cell_number, cell in enumerate(layer["cells"]):
            block_index = cell["blockBiomeIndex"]
            environment_index = cell["environmentBiomeIndex"]
            if not block_index or not environment_index:
                summary["skippedNullCells"] += 1
                continue
            block_biome = biomes[block_index - 1]
            if str(block_biome.get("baseName", "")).casefold() in excluded:
                summary["skippedExcludedCells"] += 1
                continue
            environment = biomes[environment_index - 1]
            parallax = block_biome.get("parallax")
            if parallax is not None:
                parallax = normalize_parallax_value(parallax)
            if environment.get("parallax") == parallax:
                continue
            identity = json.dumps(parallax, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=True, allow_nan=False)
            clone_key = (environment_index, identity)
            target = clone_indexes.get(clone_key)
            if target is None:
                clone = deep_copy_json(environment)
                clone["parallax"] = deep_copy_json(parallax)
                # Reuse a matching carrier without moving any existing index.
                target = next((index for index, value in enumerate(biomes, 1)
                               if value == clone), None)
                if target is None:
                    target = len(biomes) + len(clones) + 1
                    if target > MAX_BIOME_INDEX:
                        raise BiomeIndexCapacityError(
                            "Ordered biome parallax needs more than 255 compiled biomes; "
                            "compact the staged document together with its saved records "
                            "before retrying. No world data was changed."
                        )
                    clones.append(clone)
                clone_indexes[clone_key] = target
            cell["environmentBiomeIndex"] = target
            changed_layers.add(layer_number)
            summary["changes"].append({
                "layerId": layer_id, "layerIndex": layer_number, "cellIndex": cell_number,
                "blockBiomeIndex": block_index, "sourceEnvironmentBiomeIndex": environment_index,
                "targetEnvironmentBiomeIndex": target,
            })
    summary.update({
        "changedLayers": len(changed_layers), "changedCells": len(summary["changes"]),
        "clonedBiomes": len(clones), "biomeCountAfter": len(biomes) + len(clones),
    })
    if summary["changedCells"]:
        biomes.extend(clones)
        region_data["layers"] = layers
    return summary


def _empty_environment_biome() -> dict[str, Any]:
    """Compiled native defaults for a NullBiomeIndex (0) parallax carrier.

    Do not borrow the block biome: that would introduce music and spawns.
    """
    placeables = {
        "grassMod": 65535, "grassModDensity": 0.0,
        "ceilingGrassMod": 65535, "ceilingGrassModDensity": 0.0,
        "itemDistributions": [],
    }
    return {
        "baseName": "", "description": "", "mainBlock": 65535,
        "subBlocks": [], "ores": [], "hueShift": 0.0, "materialHueShift": 0,
        "surfacePlaceables": deep_copy_json(placeables),
        "undergroundPlaceables": deep_copy_json(placeables),
        "spawnProfile": {"spawnTypes": [], "monsterParameters": None},
        "ambientNoises": None, "musicTrack": None,
    }


def set_parallax_rectangle(
    document: dict[str, Any],
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    parallax: dict[str, Any],
) -> dict[str, Any]:
    """Change only environment parallax inside an exact metadata rectangle.

    Only intersecting environment biomes are cloned. NullBiomeIndex (0)
    receives an empty environment carrier, without borrowing block ecology.
    Block biomes, selectors, liquids and saved tile/entity sectors are untouched.
    Work on staged layers so invalid selected references cannot partly apply.
    """

    normalized = normalize_parallax_value(parallax)
    _complete_native_parallax_defaults(document, normalized)
    width, height = int(document["size"][0]), int(document["size"][1])
    x_ranges = _wrapped_x_ranges(int(x_start), int(x_end), width)
    region_data = world_template(document).get("regionData")
    if not isinstance(region_data, dict):
        raise WorldFormatError("worldTemplate.regionData is missing")
    staged = {"size": document["size"], "metadata": {"worldTemplate": {
        "regionData": {"layers": deep_copy_json(region_data.get("layers"))}
    }}}
    _region_data, layers = _split_region_layers_for_rectangle(
        staged, int(y_start), int(y_end)
    )
    biomes = compiled_biomes(document)
    clone_indexes: dict[int, int] = {}
    clones: list[dict[str, Any]] = []
    changed_layers = 0
    for layer in layers:
        layer_y = int(layer["yStart"])
        if not int(y_start) <= layer_y <= int(y_end):
            continue
        boundaries, cells = layer.get("boundaries"), layer.get("cells")
        if not isinstance(boundaries, list) or not isinstance(cells, list):
            raise WorldFormatError("Compiled world layer has invalid boundaries/cells")
        if len(cells) != len(boundaries) + 1:
            raise WorldFormatError("Compiled world layer cell count does not match boundaries")
        edges = [0] + [int(boundary) + 1 for boundary in boundaries] + [width]
        if any(left >= right for left, right in zip(edges, edges[1:])):
            raise WorldFormatError("Compiled world layer boundaries must increase within world width")
        referenced: set[int] = set()
        for cell, left, stop in zip(cells, edges, edges[1:]):
            if not any(left <= end and start < stop for start, end in x_ranges):
                continue
            index = cell.get("environmentBiomeIndex") if isinstance(cell, dict) else None
            if type(index) is not int or not 0 <= index <= len(biomes):
                raise WorldFormatError(
                    f"Selected parallax region has invalid environmentBiomeIndex {index!r}; "
                    f"expected 0..{len(biomes)} at X {left}..{stop - 1}, Y {layer_y}"
                )
            referenced.add(index)
        for old_index in sorted(referenced):
            if old_index in clone_indexes:
                continue
            clone = (deep_copy_json(biome_at(document, old_index))
                     if old_index else _empty_environment_biome())
            clone["parallax"] = deep_copy_json(normalized)
            clones.append(clone)
            clone_indexes[old_index] = len(biomes) + len(clones)
        _overlay_layer_biome(
            layer,
            width,
            x_ranges,
            clone_indexes,
            environment_only=True,
        )
        changed_layers += 1
    biomes.extend(clones)
    region_data["layers"] = layers
    return {
        "parallaxOnly": True,
        "changedLayers": changed_layers,
        "sourceEnvironmentBiomeIndexes": sorted(clone_indexes),
        "targetEnvironmentBiomeIndexes": sorted(clone_indexes.values()),
    }


OCEAN_BIOME_PAIRS = {
    "oceanfloor": "ocean",
    "toxicoceanfloor": "toxic",
    "arcticoceanfloor": "arctic",
    "magmaoceanfloor": "magma",
}


def ocean_biome_pair(biome_name: str) -> tuple[str, str] | None:
    """Return ``(floor, surface)`` for either member of an ocean pair."""

    name = str(biome_name).strip().casefold()
    if name in OCEAN_BIOME_PAIRS:
        return name, OCEAN_BIOME_PAIRS[name]
    reverse = {surface: floor for floor, surface in OCEAN_BIOME_PAIRS.items()}
    if name in reverse:
        return reverse[name], name
    return None


def ocean_floor_pair_default_heights(
    parameters: dict[str, Any],
) -> tuple[float | None, int | None]:
    """Return the compiled world's normal floor base and biome boundary.

    ``parameters`` may be either a Celestial body's outer parameter object or
    the ``worldTemplate.worldParameters`` object decoded from a generated
    World4 file.  Vanilla ocean generation uses the Subsurface base height as
    the mean floor-terrain height and the Surface minimum height as the point
    where the floor biome changes to the surface-ocean biome.
    """

    source: Any = parameters
    visitable = parameters.get("visitableParameters")
    if isinstance(visitable, dict):
        source = visitable
    if not isinstance(source, dict):
        return None, None
    surface = source.get("surfaceLayer")
    subsurface = source.get("subsurfaceLayer")
    floor_value = (
        subsurface.get("layerBaseHeight")
        if isinstance(subsurface, dict)
        else None
    )
    transition_value = (
        surface.get("layerMinHeight") if isinstance(surface, dict) else None
    )
    floor = (
        float(floor_value)
        if isinstance(floor_value, (int, float)) and not isinstance(floor_value, bool)
        else None
    )
    transition = (
        int(round(float(transition_value)))
        if isinstance(transition_value, (int, float))
        and not isinstance(transition_value, bool)
        else None
    )
    return floor, transition


def ocean_floor_pair_default_bottom(parameters: dict[str, Any]) -> int | None:
    """Return the normal lower Y boundary of a paired ocean-floor biome.

    The floor biome normally occupies the Subsurface layer.  Its minimum
    height is therefore the point below which the world's existing
    Underground/Core recipe must remain untouched.  This is deliberately
    separate from the Subsurface *base* height used by the terrain selector.
    """

    source: Any = parameters
    visitable = parameters.get("visitableParameters")
    if isinstance(visitable, dict):
        source = visitable
    if not isinstance(source, dict):
        return None
    subsurface = source.get("subsurfaceLayer")
    value = (
        subsurface.get("layerMinHeight")
        if isinstance(subsurface, dict)
        else None
    )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return int(round(float(value)))


def align_surface_ocean_subregions(
    parameters: dict[str, Any],
    settings: Sequence[dict[str, Any] | None],
) -> list[int]:
    """Make selected surface-ocean sub-regions use the same ocean recipe.

    Starbound requires every main region to have a positional sub-region
    partner, so the sub-region cannot safely be removed.  Replacing that
    partner with a deep copy of the same ocean recipe yields a visually
    uniform ocean while preserving the engine's one-to-one array invariant.
    Returns the surface indexes that changed.
    """

    repair_celestial_region_pairings(parameters)
    main_regions = celestial_biome_region_objects(parameters, "surface", "regions")
    sub_regions = celestial_biome_region_objects(
        parameters, "surface", "subregions"
    )
    main_names = [str(region.get("biome", "")) for region in main_regions]
    sub_names = [str(region.get("biome", "")) for region in sub_regions]
    normalized, _added, _removed, forced = (
        normalize_uniform_ocean_subregion_names(main_names, sub_names, settings)
    )
    changed = {
        index
        for index, (before, after) in enumerate(zip(sub_names, normalized))
        if before.casefold() != after.casefold()
    }
    changed.update(int(item["index"]) for item in forced)
    if changed:
        # Every normalized name either exists in this Sub-region pool or equals
        # its corresponding Main biome, so this repair needs no asset compiler.
        set_celestial_biome_order(
            parameters, "surface", "subregions", normalized, None
        )

    # A locked ocean carries the exact complete Main-region recipe, rather than
    # a region object that merely has the same biome name.
    layer = _celestial_layer(parameters, "surface")
    main_regions = celestial_biome_region_objects(parameters, "surface", "regions")
    sub_regions = celestial_biome_region_objects(
        parameters, "surface", "subregions"
    )
    locks = uniform_ocean_subregion_locks(main_names, settings)
    for index in locks:
        replacement = deep_copy_json(main_regions[index])
        if sub_regions[index] != replacement:
            sub_regions[index] = replacement
            changed.add(index)
    if changed:
        layer["primarySubRegion"] = sub_regions[0]
        layer["secondarySubRegions"] = sub_regions[1:]
    validate_celestial_region_pairings(parameters)
    return sorted(changed)


def _world_ocean_layout_heights(
    document: dict[str, Any],
    y_start: int,
    y_end: int,
    ocean_floor_height: int | float | None = None,
    ocean_floor_transition: int | float | None = None,
    shift_ocean_levels_with_floor: bool = True,
    ocean_liquid_level: int | float | None = None,
) -> tuple[float, int, int]:
    """Resolve floor terrain, biome transition and liquid-surface heights.

    A normal terrestrial ocean uses the subsurface base as the mean sea-floor
    terrain height, the surface layer minimum as the ocean/ocean-floor biome
    transition, and the surface base as the liquid level.  Custom worlds keep
    those exact compiled values.  A floor override may move the derived
    transition and liquid level together for legacy callers.  An explicitly
    supplied liquid surface is absolute and is never silently shifted.
    """

    start = int(y_start)
    end = int(y_end)
    span = max(1, end - start)
    fallback_floor = float(start + max(1, round(span / 7.0)))
    fallback_transition = int(start + round(span * 5.0 / 7.0))
    fallback_liquid = end

    parameters = world_template(document).get("worldParameters", {})
    surface = parameters.get("surfaceLayer") if isinstance(parameters, dict) else None
    subsurface = (
        parameters.get("subsurfaceLayer") if isinstance(parameters, dict) else None
    )
    floor = (
        float(subsurface.get("layerBaseHeight"))
        if isinstance(subsurface, dict)
        and isinstance(subsurface.get("layerBaseHeight"), (int, float))
        else fallback_floor
    )
    transition = (
        int(round(float(surface.get("layerMinHeight"))))
        if isinstance(surface, dict)
        and isinstance(surface.get("layerMinHeight"), (int, float))
        else fallback_transition
    )
    liquid = (
        int(round(float(surface.get("layerBaseHeight"))))
        if isinstance(surface, dict)
        and isinstance(surface.get("layerBaseHeight"), (int, float))
        else fallback_liquid
    )
    # A layer definition elsewhere in a heavily edited world may not intersect
    # this rectangle.  In that case its heights are not useful for this local
    # ocean deployment, so retain the established 1/7 and 5/7 fallback.
    if not start <= floor < end or not start < transition <= end:
        floor, transition, liquid = (
            fallback_floor,
            fallback_transition,
            fallback_liquid,
        )

    # A local region's requested water surface is independent from whether the
    # world's global Surface/Subsurface layer heights intersect this rectangle.
    # Apply it after the fallback calculation so a wholly submerged selection
    # can still point to a liquid surface above its Y maximum.
    explicit_liquid_level = ocean_liquid_level is not None
    if explicit_liquid_level:
        liquid = int(round(float(ocean_liquid_level)))
        if not 0 <= liquid < int(document["size"][1]):
            raise ValueError(
                "Ocean liquid surface must stay inside the world's Y range "
                f"(0–{int(document['size'][1]) - 1})"
            )

    if ocean_floor_height is not None:
        requested = float(ocean_floor_height)
        if not start <= requested < end:
            raise ValueError(
                f"Ocean-floor height must be from {start} to {end - 1}"
            )
        delta = requested - floor
        floor = requested
        if shift_ocean_levels_with_floor:
            transition = int(round(transition + delta))
            if not explicit_liquid_level:
                liquid = int(round(liquid + delta))

    if ocean_floor_transition is not None:
        requested_transition = int(round(float(ocean_floor_transition)))
        if not start < requested_transition <= end:
            raise ValueError(
                f"Ocean/floor transition must be from {start + 1} to {end}"
            )
        transition = requested_transition

    transition = min(end, max(start + 1, transition, int(math.floor(floor)) + 1))
    if explicit_liquid_level and liquid < transition:
        raise ValueError(
            "Ocean liquid surface must be at or above the ocean/floor boundary "
            f"({transition})"
        )
    liquid = min(int(document["size"][1]) - 1, max(transition, liquid))
    return floor, transition, liquid


def generated_ocean_liquid_level(
    document: dict[str, Any], biome_name: str, x: int | None = None
) -> int | None:
    """Return the liquid surface stored by one generated ocean recipe.

    A custom world's Surface ``layerBaseHeight`` is only the terrain-layer
    baseline.  Individual ocean regions may intentionally use a different
    ``oceanLiquidLevel`` (for example 12797 beside a base of 12800).  Prefer
    that Celestial recipe value so a local second-stage rebuild cannot create
    a raised three-tile liquid shelf.  Compiled cells are a fallback for older
    worlds whose layer definition no longer carries the recipe.
    """

    target = str(biome_name).strip().casefold()
    values: list[int] = []
    parameters = world_template(document).get("worldParameters", {})
    surface = parameters.get("surfaceLayer") if isinstance(parameters, dict) else None
    if isinstance(surface, dict):
        regions: list[Any] = [surface.get("primaryRegion")]
        secondary = surface.get("secondaryRegions")
        if isinstance(secondary, list):
            regions.extend(secondary)
        for region in regions:
            if not isinstance(region, dict):
                continue
            if str(region.get("biome", "")).strip().casefold() != target:
                continue
            liquid = region.get("oceanLiquid")
            level = region.get("oceanLiquidLevel")
            if (
                isinstance(liquid, int)
                and liquid != 0
                and isinstance(level, (int, float))
                and not isinstance(level, bool)
                and float(level) > 0
            ):
                values.append(int(round(float(level))))
    if values:
        return max(set(values), key=lambda value: (values.count(value), value))

    if x is None:
        return None
    compiled_values: list[int] = []
    for y_start, _y_end, _layer in _layer_vertical_segments(document):
        cell = _compiled_cell_at(document, int(x), y_start)
        names: list[str] = []
        for key in ("environmentBiomeIndex", "blockBiomeIndex"):
            index = cell.get(key)
            if isinstance(index, int):
                names.append(_biome_name_for_index(document, index).casefold())
        level = cell.get("oceanLiquidLevel")
        if (
            target in names
            and isinstance(cell.get("oceanLiquid"), int)
            and int(cell["oceanLiquid"]) != 0
            and isinstance(level, (int, float))
            and not isinstance(level, bool)
            and float(level) > 0
        ):
            compiled_values.append(int(round(float(level))))
    if not compiled_values:
        return None
    return max(
        set(compiled_values),
        key=lambda value: (compiled_values.count(value), value),
    )


def _layer_vertical_segments(
    document: dict[str, Any],
) -> list[tuple[int, int, dict[str, Any]]]:
    """Return compiled region layers as inclusive vertical segments."""

    height = int(document["size"][1])
    region_data = world_template(document).get("regionData")
    layers = region_data.get("layers") if isinstance(region_data, dict) else None
    if not isinstance(layers, list) or not layers:
        raise WorldFormatError("worldTemplate.regionData.layers is missing or empty")
    ordered = sorted(layers, key=lambda layer: int(layer["yStart"]))
    result: list[tuple[int, int, dict[str, Any]]] = []
    for index, layer in enumerate(ordered):
        start = int(layer["yStart"])
        end = (
            int(ordered[index + 1]["yStart"]) - 1
            if index + 1 < len(ordered)
            else height - 1
        )
        if start <= end:
            result.append((start, end, layer))
    return result


def _layer_horizontal_segments(
    layer: dict[str, Any], world_width: int
) -> list[tuple[int, int, dict[str, Any]]]:
    boundaries = layer.get("boundaries")
    cells = layer.get("cells")
    if not isinstance(boundaries, list) or not isinstance(cells, list):
        raise WorldFormatError("Compiled world layer has invalid boundaries/cells")
    if len(cells) != len(boundaries) + 1:
        raise WorldFormatError("Compiled world layer cell count does not match boundaries")
    starts = [0] + [int(boundary) + 1 for boundary in boundaries]
    ends = [int(boundary) for boundary in boundaries] + [world_width - 1]
    return [
        (start, end, cell)
        for start, end, cell in zip(starts, ends, cells)
        if isinstance(cell, dict) and start <= end
    ]


def _compiled_cell_at(
    document: dict[str, Any], x: int, y: int
) -> dict[str, Any]:
    width, height = (int(document["size"][0]), int(document["size"][1]))
    if not 0 <= int(x) < width or not 0 <= int(y) < height:
        raise ValueError(
            f"坐标必须位于 X 0–{width - 1}、Y 0–{height - 1} 范围内"
        )
    layer = next(
        (
            candidate
            for start, end, candidate in _layer_vertical_segments(document)
            if start <= int(y) <= end
        ),
        None,
    )
    if layer is None:
        raise WorldFormatError(f"纵坐标 {y} 没有对应的 compiled region layer")
    cell = next(
        (
            candidate
            for start, end, candidate in _layer_horizontal_segments(layer, width)
            if start <= int(x) <= end
        ),
        None,
    )
    if cell is None:
        raise WorldFormatError(f"横坐标 {x} 没有对应的 compiled region cell")
    return cell


def _wrapped_range_midpoint(x_start: int, x_end: int, world_width: int) -> int:
    ranges = _wrapped_x_ranges(int(x_start), int(x_end), world_width)
    length = sum(end - start + 1 for start, end in ranges)
    offset = (length - 1) // 2
    return (int(x_start) + offset) % world_width


def _biome_name_for_index(document: dict[str, Any], index: int) -> str:
    # Native layout cells may reference NullBiomeIndex. It has no compiled
    # object to look up (and must never fall through to biomes[-1]). Keep the
    # strict object accessor separate from this label/detection helper.
    if type(index) is int and index == 0:
        return "No biome (0)"
    value = biome_at(document, index).get("baseName")
    return str(value) if value not in (None, "") else f"index {index}"


def _rectangle_boundary_transitions(
    document: dict[str, Any],
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
) -> dict[str, Any]:
    """Describe the direct biome boundaries that Starbound will blend.

    There is no per-biome edge-style object in World4 metadata.  The engine
    blends the terrain selectors of directly adjacent compiled cells using
    ``regionBlending``.  Reporting the final adjacency here also guards
    against accidentally leaving a narrow cell from the old biome between a
    copied rectangle and its real neighbour.
    """

    width = int(document["size"][0])
    region_data = world_template(document).get("regionData", {})
    blend_width = (
        float(region_data.get("regionBlending", 0.0))
        if isinstance(region_data, dict)
        else 0.0
    )
    if int(x_start) == 0 and int(x_end) == width - 1:
        return {"blendWidth": blend_width, "transitions": []}

    sides = (
        ("left", int(x_start), (int(x_start) - 1) % width),
        ("right", int(x_end), (int(x_end) + 1) % width),
    )
    transitions: list[dict[str, Any]] = []
    for layer_start, layer_end, layer in _layer_vertical_segments(document):
        start = max(int(y_start), layer_start)
        end = min(int(y_end), layer_end)
        if start > end:
            continue
        sample_y = start
        for side, inside_x, outside_x in sides:
            horizontal = _layer_horizontal_segments(layer, width)
            inside_segment = next(
                segment for segment in horizontal if segment[0] <= inside_x <= segment[1]
            )
            outside_segment = next(
                segment for segment in horizontal if segment[0] <= outside_x <= segment[1]
            )
            inside = inside_segment[2]
            outside = outside_segment[2]
            inside_pair = (
                inside.get("blockBiomeIndex"),
                inside.get("environmentBiomeIndex"),
            )
            outside_pair = (
                outside.get("blockBiomeIndex"),
                outside.get("environmentBiomeIndex"),
            )
            biome_count = len(compiled_biomes(document))
            for index in inside_pair + outside_pair:
                if type(index) is not int or not 0 <= index <= biome_count:
                    raise WorldFormatError(
                        f"Boundary region has invalid biome index {index!r}; "
                        f"expected an integer in 0..{biome_count} (0 means no biome)"
                    )
            if inside_pair == outside_pair:
                continue
            neighbour_width = (
                outside_x - outside_segment[0] + 1
                if side == "left"
                else outside_segment[1] - outside_x + 1
            )
            transitions.append(
                {
                    "side": side,
                    "x": inside_x,
                    "neighborX": outside_x,
                    "yStart": start,
                    "yEnd": end,
                    "targetBiomeIndex": inside_pair[0],
                    "targetBiome": _biome_name_for_index(
                        document, inside_pair[0]
                    ),
                    "neighborBiomeIndex": outside_pair[0],
                    "neighborBiome": _biome_name_for_index(
                        document, outside_pair[0]
                    ),
                    "targetTerrainSelectorIndex": inside.get(
                        "terrainSelectorIndex"
                    ),
                    "neighborTerrainSelectorIndex": outside.get(
                        "terrainSelectorIndex"
                    ),
                    "neighborCellStart": outside_segment[0],
                    "neighborCellEnd": outside_segment[1],
                    "neighborCellWidthFromBoundary": neighbour_width,
                    "narrowNeighborCell": bool(
                        blend_width > 0 and neighbour_width < blend_width
                    ),
                    "suggestedTargetBoundary": (
                        outside_segment[0]
                        if side == "left"
                        else outside_segment[1]
                    ),
                }
            )
    return {"blendWidth": blend_width, "transitions": transitions}


def reference_biome_rectangle_summary(
    document: dict[str, Any],
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
) -> dict[str, Any]:
    """Inspect a reference rectangle and require one precise compiled biome.

    The check covers both block and environment biome indexes of every region
    cell intersecting the rectangle.  Using an exact compiled index avoids
    silently choosing between two same-named biome variants with different
    parallax, placeables or spawn profiles.
    """

    width, height = (int(document["size"][0]), int(document["size"][1]))
    x_ranges = _wrapped_x_ranges(int(x_start), int(x_end), width)
    y_start = int(y_start)
    y_end = int(y_end)
    if not 0 <= y_start <= y_end < height:
        raise ValueError(f"参考纵坐标必须递增并位于 0 到 {height - 1} 之间")

    biome_pairs: set[tuple[int, int]] = set()
    variants: set[str] = set()
    for layer_start, layer_end, layer in _layer_vertical_segments(document):
        if layer_start > y_end or layer_end < y_start:
            continue
        for cell_start, cell_end, cell in _layer_horizontal_segments(layer, width):
            if not any(cell_start <= end and cell_end >= start for start, end in x_ranges):
                continue
            block_index = cell.get("blockBiomeIndex")
            environment_index = cell.get("environmentBiomeIndex")
            if type(block_index) is not int or type(environment_index) is not int:
                raise WorldFormatError(
                    "参考范围包含缺少 block/environment biome index 的 region cell"
                )
            biome_pairs.add((block_index, environment_index))
            variants.add(
                json.dumps(cell, sort_keys=True, separators=(",", ":"))
            )

    if not biome_pairs:
        raise ValueError("参考范围内没有找到 compiled biome")
    biome_count = len(compiled_biomes(document))
    indexes = {index for pair in biome_pairs for index in pair}
    invalid = sorted(index for index in indexes if not 0 <= index <= biome_count)
    if invalid:
        raise WorldFormatError(f"参考范围包含无效 biome index：{invalid}")
    if len(biome_pairs) != 1:
        descriptions = ", ".join(
            (
                f"block {block} ({_biome_name_for_index(document, block)}) / "
                f"environment {environment} "
                f"({_biome_name_for_index(document, environment)})"
            )
            for block, environment in sorted(biome_pairs)
        )
        raise ValueError(
            "参考范围必须只包含一种固定的 compiled biome 配方；当前检测到："
            + descriptions
        )

    block_index, environment_index = next(iter(biome_pairs))
    sample_x = _wrapped_range_midpoint(int(x_start), int(x_end), width)
    sample_y = y_start + (y_end - y_start) // 2
    sample_cell = deep_copy_json(_compiled_cell_at(document, sample_x, sample_y))
    sample_pair = (
        sample_cell.get("blockBiomeIndex"),
        sample_cell.get("environmentBiomeIndex"),
    )
    if sample_pair != (block_index, environment_index):
        raise WorldFormatError(
            "参考范围中心的 region cell 与唯一 biome 检测结果不一致"
        )

    return {
        "referenceBiomeIndex": block_index,
        "referenceBiomeName": _biome_name_for_index(document, block_index),
        "referenceEnvironmentBiomeIndex": environment_index,
        "referenceEnvironmentBiomeName": _biome_name_for_index(
            document, environment_index
        ),
        "referenceCell": sample_cell,
        "referenceCellVariants": len(variants),
        "referenceSample": [sample_x, sample_y],
        "wrappedReferenceX": int(x_start) > int(x_end),
    }


def inspect_world_reference_biome(
    world: Path,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
) -> dict[str, Any]:
    document = load_metadata_only(Path(world))
    result = reference_biome_rectangle_summary(
        document, x_start, x_end, y_start, y_end
    )
    pair = _ocean_reference_pair(
        document,
        int(result["referenceSample"][0]),
        int(result["referenceSample"][1]),
        str(result["referenceBiomeName"]),
    )
    if pair is not None:
        result.update(
            {
                "oceanFloorTransitionY": pair["transitionY"],
                "oceanFloorBiome": pair["floorName"],
                "oceanSurfaceBiome": pair["surfaceName"],
            }
        )
    result["size"] = [int(document["size"][0]), int(document["size"][1])]
    result.pop("referenceCell", None)
    return result


def inspect_world_reference_copy(
    world: Path,
    target_x_start: int,
    target_x_end: int,
    target_y_start: int,
    target_y_end: int,
    reference_x_start: int,
    reference_x_end: int,
    reference_y_start: int,
    reference_y_end: int,
) -> dict[str, Any]:
    """Preview a reference copy, including final direct boundary cells."""

    document = load_metadata_only(Path(world))
    result = set_reference_biome_rectangle(
        document,
        int(target_x_start),
        int(target_x_end),
        int(target_y_start),
        int(target_y_end),
        int(reference_x_start),
        int(reference_x_end),
        int(reference_y_start),
        int(reference_y_end),
    )
    result["size"] = [int(document["size"][0]), int(document["size"][1])]
    return result


def _ocean_reference_pair(
    document: dict[str, Any], reference_x: int, reference_y: int, base_name: str
) -> dict[str, Any] | None:
    reverse_pairs = {surface: floor for floor, surface in OCEAN_BIOME_PAIRS.items()}
    if base_name in OCEAN_BIOME_PAIRS:
        floor_name = base_name
        surface_name = OCEAN_BIOME_PAIRS[base_name]
    elif base_name in reverse_pairs:
        floor_name = reverse_pairs[base_name]
        surface_name = base_name
    else:
        return None

    profiles: list[tuple[int, int, str, int, dict[str, Any]]] = []
    for start, end, layer in _layer_vertical_segments(document):
        cell = next(
            (
                candidate
                for left, right, candidate in _layer_horizontal_segments(
                    layer, int(document["size"][0])
                )
                if left <= reference_x <= right
            ),
            None,
        )
        if cell is None:
            continue
        block_index = cell.get("blockBiomeIndex")
        environment_index = cell.get("environmentBiomeIndex")
        if not isinstance(block_index, int) or block_index != environment_index:
            continue
        name = _biome_name_for_index(document, block_index)
        profiles.append((start, end, name, block_index, cell))

    candidates: list[
        tuple[int, tuple[int, int, str, int, dict[str, Any]], tuple[int, int, str, int, dict[str, Any]]]
    ] = []
    floors = [profile for profile in profiles if profile[2] == floor_name]
    surfaces = [profile for profile in profiles if profile[2] == surface_name]
    for floor in floors:
        for surface in surfaces:
            if floor[0] < surface[0]:
                gap = max(0, surface[0] - floor[1] - 1)
                distance = min(
                    abs(reference_y - floor[0]),
                    abs(reference_y - floor[1]),
                    abs(reference_y - surface[0]),
                    abs(reference_y - surface[1]),
                )
                candidates.append((gap * (int(document["size"][1]) + 1) + distance, floor, surface))
    if not candidates:
        return None
    _score, floor, surface = min(candidates, key=lambda item: item[0])
    transition = int(surface[0])
    return {
        "floorName": floor_name,
        "surfaceName": surface_name,
        "floorIndex": int(floor[3]),
        "surfaceIndex": int(surface[3]),
        "floorCell": deep_copy_json(floor[4]),
        "surfaceCell": deep_copy_json(surface[4]),
        "transitionY": transition,
    }


def set_reference_biome_rectangle(
    document: dict[str, Any],
    target_x_start: int,
    target_x_end: int,
    target_y_start: int,
    target_y_end: int,
    reference_x_start: int,
    reference_x_end: int,
    reference_y_start: int,
    reference_y_end: int,
) -> dict[str, Any]:
    """Copy an exact compiled biome and region recipe into a target rectangle."""

    summary = reference_biome_rectangle_summary(
        document,
        reference_x_start,
        reference_x_end,
        reference_y_start,
        reference_y_end,
    )
    width, height = (int(document["size"][0]), int(document["size"][1]))
    target_ranges = _wrapped_x_ranges(
        int(target_x_start), int(target_x_end), width
    )
    target_y_start = int(target_y_start)
    target_y_end = int(target_y_end)

    reference_index = int(summary["referenceBiomeIndex"])
    reference_environment_index = int(summary["referenceEnvironmentBiomeIndex"])
    reference_name = str(summary["referenceBiomeName"])
    reference_cell = deep_copy_json(summary["referenceCell"])
    sample_x, sample_y = (int(value) for value in summary["referenceSample"])
    ocean_pair = _ocean_reference_pair(
        document, sample_x, sample_y, reference_name
    )
    region_data, layers = _split_region_layers_for_rectangle(
        document, target_y_start, target_y_end
    )
    transition: int | None = None
    if ocean_pair is not None:
        transition = int(ocean_pair["transitionY"])
        if target_y_start < transition <= target_y_end:
            _split_region_layer_at(layers, height, transition)

    changed_layers = 0
    applied_indexes: set[int] = set()
    for layer in layers:
        layer_y = int(layer["yStart"])
        if not target_y_start <= layer_y <= target_y_end:
            continue
        if ocean_pair is None:
            chosen = reference_cell
        elif layer_y < int(ocean_pair["transitionY"]):
            chosen = ocean_pair["floorCell"]
        else:
            chosen = ocean_pair["surfaceCell"]
        chosen_index = int(chosen["blockBiomeIndex"])
        chosen_environment_index = int(chosen["environmentBiomeIndex"])
        _overlay_layer_biome(
            layer,
            width,
            target_ranges,
            chosen_index,
            replacement_cell=chosen,
        )
        applied_indexes.add(chosen_index)
        applied_indexes.add(chosen_environment_index)
        changed_layers += 1

    result = {
        "referenceBiomeIndex": reference_index,
        "referenceBiomeName": reference_name,
        "referenceEnvironmentBiomeIndex": reference_environment_index,
        "referenceEnvironmentBiomeName": str(
            summary["referenceEnvironmentBiomeName"]
        ),
        "referenceCellVariants": int(summary["referenceCellVariants"]),
        "referenceSample": list(summary["referenceSample"]),
        "targetBiomeIndexes": sorted(applied_indexes),
        "appliedBiomeIndexes": sorted(applied_indexes),
        "changedLayers": changed_layers,
        "regionBlending": region_data.get("regionBlending"),
        "fullRegionRecipe": True,
        "copiedFromReference": True,
        "oceanFloorTransitionY": transition,
    }
    boundary_info = _rectangle_boundary_transitions(
        document,
        int(target_x_start),
        int(target_x_end),
        target_y_start,
        target_y_end,
    )
    result["boundaryBlendWidth"] = boundary_info["blendWidth"]
    result["boundaryTransitions"] = boundary_info["transitions"]
    if ocean_pair is not None:
        result.update(
            {
                "oceanFloorBiome": ocean_pair["floorName"],
                "oceanSurfaceBiome": ocean_pair["surfaceName"],
            }
        )
    return result


def _terrestrial_layer_base_height(document: dict[str, Any], y: int) -> float:
    parameters = world_template(document).get("worldParameters", {})
    candidates: list[tuple[int, float]] = []
    if isinstance(parameters, dict):
        layers: list[Any] = []
        for key in ("coreLayer", "subsurfaceLayer", "surfaceLayer", "atmosphereLayer", "spaceLayer"):
            layers.append(parameters.get(key))
        underground = parameters.get("undergroundLayers", [])
        if isinstance(underground, list):
            layers.extend(underground)
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            minimum = layer.get("layerMinHeight")
            base = layer.get("layerBaseHeight")
            if isinstance(minimum, (int, float)) and isinstance(base, (int, float)):
                candidates.append((int(minimum), float(base)))
    eligible = [item for item in candidates if item[0] <= y]
    if eligible:
        return max(eligible, key=lambda item: item[0])[1]
    return float(y)


def compile_asset_biome_parallax(
    document: dict[str, Any],
    assets_folder: Path,
    biome_name: str,
    y: int,
) -> dict[str, Any]:
    """Compile only the resolved parallax value for one asset biome."""

    compiler = AssetBiomeCompiler(Path(assets_folder))
    name = str(biome_name).strip()
    if name not in compiler.catalog["biomes"]:
        raise ValueError(f"所选 assets 中不存在 biome：{name}")
    parameters = world_template(document).get("worldParameters", {})
    threat = (
        float(parameters.get("threatLevel", 1.0))
        if isinstance(parameters, dict)
        else 1.0
    )
    base = _terrestrial_layer_base_height(document, int(y))
    old = compiled_biomes(document)[0] if compiled_biomes(document) else {}
    hue_selection, _hue_details = _world_biome_hue_selection(
        document, name, compiler.catalog["biomes"][name],
    )
    compiled, _warnings = compiler.compile(
        name,
        _stable_asset_seed(world_template(document).get("seed", 0), name, y),
        base,
        old,
        threat,
        hue_selection=hue_selection,
        native_extensions=_world_uses_native_extensions(document),
    )
    parallax = compiled.get("parallax")
    if not isinstance(parallax, dict):
        raise ValueError(f"Biome {name} does not compile a parallax object")
    return normalize_parallax_value(parallax)


def set_asset_biome_rectangle(
    document: dict[str, Any],
    assets_folder: Path,
    biome_name: str,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    ocean_floor_height: int | float | None = None,
    *,
    ocean_floor_biome: str | None = None,
    ocean_floor_transition: int | float | None = None,
    ocean_floor_biome_overrides: dict[str, Any] | None = None,
    ocean_floor_region_overrides: dict[str, Any] | None = None,
    preserve_ocean_surface_level: bool = False,
    ocean_liquid_level: int | float | None = None,
    ocean_coastal_slope: bool = False,
    ocean_coastal_slope_step: int = 32,
    ocean_floor_ecology_height: int = OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT,
    include_ocean_floor: bool | None = None,
    ocean_floor_only: bool = False,
    ocean_floor_bottom: int | float | None = None,
    ocean_uniform: bool = True,
    biome_parallax_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile assets and replace the rectangle with a fresh biome recipe.

    Unlike Starbound's ``addBiomeRegion``, this deliberately does not inherit
    the source cell's terrain, caves, ores or liquids.  Ocean-floor biomes are
    built as the paired lower floor / upper ocean layers used by terrestrial
    ocean planets.
    """

    width, height = (int(document["size"][0]), int(document["size"][1]))
    if not 0 <= int(y_start) <= int(y_end) < height:
        raise ValueError(f"纵坐标必须递增并位于 0 到 {height - 1} 之间")
    compiler = AssetBiomeCompiler(Path(assets_folder))
    if biome_name not in compiler.catalog["biomes"]:
        raise ValueError(f"所选 assets 中不存在 biome：{biome_name}")
    region_data, layers = _split_region_layers_for_rectangle(
        document, int(y_start), int(y_end)
    )
    x_ranges = _wrapped_x_ranges(int(x_start), int(x_end), width)
    warnings: set[str] = set()
    applied_indexes: set[int] = set()
    changed_layers = 0
    world_seed = world_template(document).get("seed", 0)
    requested_start = int(y_start)
    start = requested_start
    end = int(y_end)
    span = max(1, end - start)
    ocean_pair = ocean_biome_pair(biome_name)
    bands: list[tuple[int, int, str, float, int | None]]
    transition: int | None = None
    applied_floor_name: str | None = None
    basin_solid_edges = (False, False)
    basin_summary: dict[str, Any] | None = None
    if ocean_pair is not None:
        target_is_floor = str(biome_name).strip().casefold() == ocean_pair[0]
        pair_floor = True if include_ocean_floor is None else bool(include_ocean_floor)
        # Keep the low-level function's historical default (selecting a floor
        # biome builds its complete pair) for Tool 03 compatibility.  Tool 05's
        # rectangle-plan validator explicitly sets ``ocean_floor_only`` when a
        # floor biome is selected on its own.
        floor_only = bool(
            ocean_floor_only
            or target_is_floor and include_ocean_floor is False
        )
        if floor_only:
            pair_floor = True
        missing_ocean_biomes = [
            name for name in ocean_pair if name not in compiler.catalog["biomes"]
        ]
        if missing_ocean_biomes:
            raise ValueError(
                "The selected assets cannot build a complete ocean pair; "
                "missing biome: " + ", ".join(missing_ocean_biomes)
            )
        if pair_floor and not floor_only and span < 2:
            raise ValueError(
                "An ocean rectangle must be at least 3 tiles high so both "
                "the ocean floor and surface layers can be represented"
            )
        floor_name, surface_name = ocean_pair
        if ocean_floor_biome is not None:
            floor_name = str(ocean_floor_biome).strip()
            if not floor_name:
                raise ValueError("Ocean-floor biome cannot be empty")
            if floor_name not in compiler.catalog["biomes"]:
                raise ValueError(
                    f"The selected assets do not contain ocean-floor biome: {floor_name}"
                )
        applied_floor_name = floor_name
        if ocean_floor_bottom is not None:
            floor_bottom = int(round(float(ocean_floor_bottom)))
            if not requested_start <= floor_bottom <= end:
                raise ValueError(
                    "Floor-biome bottom must stay inside the selected rectangle "
                    f"({requested_start}–{end})"
                )
            start = floor_bottom
            span = max(1, end - start)
        else:
            floor_bottom = start
        floor_base, transition, ocean_level = _world_ocean_layout_heights(
            document,
            start,
            end,
            ocean_floor_height,
            ocean_floor_transition,
            not preserve_ocean_surface_level,
            ocean_liquid_level,
        )
        if floor_base < start or floor_base > end:
            raise ValueError(
                f"Floor terrain height must stay inside the selected rectangle ({start}–{end})"
            )
        if pair_floor and not floor_only and not start < transition <= end:
            raise ValueError(
                f"Ocean/floor boundary must stay inside the selected rectangle ({start + 1}–{end})"
            )
        if ocean_coastal_slope and pair_floor:
            basin_solid_edges = _ocean_interval_solid_edges(
                document,
                int(x_start),
                int(x_end),
                min(end, max(start, int(transition))),
            )
        if floor_only:
            transition = None
            bands = [(start, end, floor_name, floor_base, ocean_level)]
        elif pair_floor:
            _split_region_layer_at(layers, height, transition)
            bands = [
                (start, transition - 1, floor_name, floor_base, ocean_level),
                (transition, end, surface_name, float(ocean_level), ocean_level),
            ]
        else:
            transition = None
            bands = [(start, end, surface_name, float(ocean_level), ocean_level)]
    else:
        if ocean_floor_height is not None:
            raise ValueError(
                "Ocean-floor height is only valid for ocean, toxic, arctic, "
                "magma and their ocean-floor biomes"
            )
        original_base = _terrestrial_layer_base_height(
            document, start + span // 2
        )
        base = min(float(end), max(float(start), float(original_base)))
        bands = [(start, end, biome_name, base, None)]

    band_cells: list[tuple[int, int, dict[str, Any]]] = []
    hue_selections: list[dict[str, Any]] = []
    for band_index, (
        band_start,
        band_end,
        band_biome,
        base_height,
        ocean_level,
    ) in enumerate(bands):
        seed = _stable_asset_seed(
            world_seed,
            biome_name,
            band_biome,
            band_start,
            band_end,
            x_start,
            x_end,
        )
        floor_overrides = (
            ocean_floor_biome_overrides
            if ocean_pair is not None
            and str(band_biome).casefold() == str(floor_name).casefold()
            else None
        )
        floor_region_overrides = (
            ocean_floor_region_overrides
            if ocean_pair is not None
            and str(band_biome).casefold() == str(floor_name).casefold()
            else None
        )
        source_cell = _compiled_cell_at(
            document, _wrapped_range_midpoint(int(x_start), int(x_end), width),
            min(height - 1, max(0, int(base_height))),
        )
        hue_audit: dict[str, Any] = {"biome": band_biome}
        cell, biome_index, compile_warnings = _build_asset_region_cell(
            document,
            compiler,
            region_data,
            band_biome,
            base_height,
            ocean_level,
            seed,
            floor_overrides,
            floor_region_overrides,
            preferred_biome_indexes=(source_cell.get("blockBiomeIndex"), source_cell.get("environmentBiomeIndex")),
            hue_audit=hue_audit,
        )
        hue_audit["compiledBiomeIndex"] = biome_index
        hue_selections.append(hue_audit)
        if (
            biome_parallax_override is not None
            and str(band_biome).casefold() == str(biome_name).strip().casefold()
        ):
            custom_biome = deep_copy_json(biome_at(document, biome_index))
            custom_biome["parallax"] = normalize_parallax_value(
                biome_parallax_override
            )
            _complete_native_parallax_defaults(document, custom_biome["parallax"])
            compiled_biomes(document).append(custom_biome)
            biome_index = len(compiled_biomes(document))
            cell["blockBiomeIndex"] = biome_index
            cell["environmentBiomeIndex"] = biome_index
        warnings.update(compile_warnings)
        applied_indexes.add(biome_index)
        band_cells.append((band_start, band_end, cell))

    for layer in layers:
        layer_y = int(layer["yStart"])
        chosen = next(
            (
                cell
                for band_start, band_end, cell in band_cells
                if band_start <= layer_y <= band_end
            ),
            None,
        )
        if chosen is None:
            continue
        _overlay_layer_biome(
            layer,
            width,
            x_ranges,
            int(chosen["blockBiomeIndex"]),
            replacement_cell=chosen,
        )
        changed_layers += 1

    if (
        ocean_pair is not None
        and transition is not None
        and ocean_coastal_slope
        and len(band_cells) == 2
    ):
        basin_summary = _apply_ocean_coastal_basin(
            document,
            region_data,
            layers,
            int(x_start),
            int(x_end),
            start,
            end,
            float(bands[0][3]),
            min(float(end), max(float(bands[0][3]), float(bands[0][4]))),
            int(transition),
            band_cells[0][2],
            band_cells[1][2],
            int(ocean_coastal_slope_step),
            int(ocean_floor_ecology_height),
            basin_solid_edges,
        )
        if basin_summary is not None:
            for key in (
                "floorEcologyBlockBiomeIndexes",
                "surfaceControlBlockBiomeIndexes",
            ):
                applied_indexes.update(
                    int(index) for index in basin_summary.get(key, [])
                )
        changed_layers = sum(
            1 for layer in layers if start <= int(layer["yStart"]) <= end
        )
    elif (
        ocean_pair is not None
        and ocean_floor_only
        and ocean_coastal_slope
        and len(band_cells) == 1
    ):
        liquid_level = int(bands[0][4]) if bands[0][4] is not None else end + 1
        basin_summary = _apply_ocean_coastal_basin(
            document,
            region_data,
            layers,
            int(x_start),
            int(x_end),
            start,
            end,
            float(bands[0][3]),
            min(float(end), max(float(bands[0][3]), float(liquid_level - 1))),
            end + 1,
            band_cells[0][2],
            band_cells[0][2],
            int(ocean_coastal_slope_step),
            int(ocean_floor_ecology_height),
            basin_solid_edges,
        )
        if basin_summary is not None:
            for key in (
                "floorEcologyBlockBiomeIndexes",
                "surfaceControlBlockBiomeIndexes",
            ):
                applied_indexes.update(
                    int(index) for index in basin_summary.get(key, [])
                )
        changed_layers = sum(
            1 for layer in layers if start <= int(layer["yStart"]) <= end
        )

    boundary_info = _rectangle_boundary_transitions(
        document,
        int(x_start),
        int(x_end),
        int(y_start),
        int(y_end),
    )
    return {
        "targetBiomeIndexes": sorted(applied_indexes),
        "appliedBiomeIndexes": sorted(applied_indexes),
        "addedBiomeIndexes": sorted(applied_indexes),
        "changedLayers": changed_layers,
        "regionBlending": region_data.get("regionBlending"),
        "assetCompileWarnings": sorted(warnings),
        "biomeHueSelections": hue_selections,
        "fullRegionRecipe": True,
        "oceanCompanionBiome": (
            ocean_pair[1] if ocean_pair is not None and len(band_cells) == 2 else None
        ),
        "oceanFloorBiome": (
            applied_floor_name
            if ocean_pair is not None and (len(band_cells) == 2 or ocean_floor_only)
            else None
        ),
        "oceanFloorTransitionY": transition,
        "oceanFloorHeight": (
            float(bands[0][3])
            if ocean_pair is not None and (len(band_cells) == 2 or ocean_floor_only)
            else None
        ),
        "oceanLiquidLevel": (
            int(bands[0][4])
            if ocean_pair is not None and bands[0][4] is not None
            else None
        ),
        "coastalBasinProfile": basin_summary,
        "oceanFloorIncluded": bool(
            ocean_pair is not None and (len(band_cells) == 2 or ocean_floor_only)
        ),
        "oceanFloorOnly": bool(ocean_pair is not None and ocean_floor_only),
        "oceanFloorBottomY": (
            int(start)
            if ocean_pair is not None and (len(band_cells) == 2 or ocean_floor_only)
            else None
        ),
        "oceanUniform": bool(ocean_uniform),
        "customParallax": biome_parallax_override is not None,
        "boundaryBlendWidth": boundary_info["blendWidth"],
        "boundaryTransitions": boundary_info["transitions"],
    }


def normalize_ocean_floor_pair_settings(
    settings: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate the Tool 05 per-surface-ocean floor-pair configuration."""

    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(settings, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"Ocean-floor pair {index} is not an object")
        surface = str(value.get("surfaceBiome", "")).strip().casefold()
        pair = ocean_biome_pair(surface)
        if pair is None or pair[1] != surface:
            raise ValueError(
                f"Ocean-floor pair {index} has a non-surface ocean biome: {surface!r}"
            )
        floor = str(value.get("floorBiome") or pair[0]).strip()
        if not floor:
            raise ValueError(f"Ocean-floor pair {index} has no floor biome")
        bottom_value = value.get("floorBottomY")
        floor_value = value.get("floorHeight")
        transition_value = value.get("transitionY")
        normalized_bottom = (
            None
            if bottom_value in (None, "")
            else int(round(float(bottom_value)))
        )
        normalized_floor = (
            None if floor_value in (None, "") else float(floor_value)
        )
        normalized_transition = (
            None
            if transition_value in (None, "")
            else int(round(float(transition_value)))
        )
        for field_name, number in (
            ("floorBottomY", normalized_bottom),
            ("floorHeight", normalized_floor),
            ("transitionY", normalized_transition),
        ):
            if number is not None and not math.isfinite(float(number)):
                raise ValueError(
                    f"Ocean-floor pair {index} {field_name} must be finite"
                )
        if normalized_bottom is not None and normalized_bottom < 0:
            raise ValueError(
                f"Ocean-floor pair {index} floor biome bottom Y cannot be negative"
            )
        if (
            normalized_bottom is not None
            and normalized_floor is not None
            and normalized_floor < normalized_bottom
        ):
            raise ValueError(
                f"Ocean-floor pair {index} floor terrain height must be at or "
                "above its floor biome bottom Y"
            )
        if (
            normalized_floor is not None
            and normalized_transition is not None
            and normalized_transition <= normalized_floor
        ):
            raise ValueError(
                f"Ocean-floor pair {index} boundary Y must be above its "
                "floor terrain height"
            )
        if (
            normalized_bottom is not None
            and normalized_transition is not None
            and normalized_transition <= normalized_bottom
        ):
            raise ValueError(
                f"Ocean-floor pair {index} boundary Y must be above its "
                "floor biome bottom Y"
            )
        disable_subregions = value.get("disableSubRegions", True)
        if not isinstance(disable_subregions, bool):
            raise ValueError(
                f"Ocean-floor pair {index} disableSubRegions must be true or false"
            )
        coastal_slope = value.get("coastalSlopeEnabled", True)
        if not isinstance(coastal_slope, bool):
            raise ValueError(
                f"Ocean-floor pair {index} coastalSlopeEnabled must be true or false"
            )
        try:
            coastal_slope_step = int(value.get("coastalSlopeStep", 32))
            floor_ecology_height = int(value.get("floorEcologyHeight", OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Ocean-floor pair {index} slope/ecology sizes must be integers"
            ) from exc
        if not 1 <= coastal_slope_step <= 512:
            raise ValueError(
                f"Ocean-floor pair {index} coastal slope sample width must be 1–512"
            )
        if not 0 <= floor_ecology_height <= 512:
            raise ValueError(
                f"Ocean-floor pair {index} floor ecology height must be 0–512"
            )
        if coastal_slope:
            floor_ecology_height = max(
                OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT, floor_ecology_height
            )
        biome_overrides = value.get("biomeOverrides", {})
        region_overrides = value.get("regionOverrides", {})
        if not isinstance(biome_overrides, dict):
            raise ValueError(f"Ocean-floor pair {index} biomeOverrides must be an object")
        if not isinstance(region_overrides, dict):
            raise ValueError(f"Ocean-floor pair {index} regionOverrides must be an object")
        validate_finite_json_numbers(
            biome_overrides, f"Ocean-floor pair {index} biomeOverrides"
        )
        validate_finite_json_numbers(
            region_overrides, f"Ocean-floor pair {index} regionOverrides"
        )
        normalized.append(
            {
                "surfaceBiome": surface,
                "floorBiome": floor,
                "floorBottomY": normalized_bottom,
                "floorHeight": normalized_floor,
                "transitionY": normalized_transition,
                "disableSubRegions": disable_subregions,
                "coastalSlopeEnabled": coastal_slope,
                "coastalSlopeStep": coastal_slope_step,
                "floorEcologyHeight": floor_ecology_height,
                "biomeOverrides": deep_copy_json(biome_overrides),
                "regionOverrides": deep_copy_json(region_overrides),
            }
        )
    return normalized


def generated_surface_biome_x_ranges(
    document: dict[str, Any], biome_name: str
) -> list[tuple[int, int]]:
    """Return exact generated X intervals for one surface biome.

    The celestial recipe does not contain these intervals; they only become
    available after Starbound creates the World4 metadata.  Sampling the
    compiled surface layer lets Tool 05 align a paired floor to the actual
    generated ocean rather than to the unrelated subsurface ordering.
    """

    width, height = (int(document["size"][0]), int(document["size"][1]))
    parameters = world_template(document).get("worldParameters", {})
    surface = parameters.get("surfaceLayer") if isinstance(parameters, dict) else None
    candidates: list[int] = []
    if isinstance(surface, dict):
        for key in ("layerMinHeight", "layerBaseHeight"):
            value = surface.get(key)
            if isinstance(value, (int, float)):
                candidates.append(min(height - 1, max(0, int(round(value)))))
    if not candidates:
        candidates.append(height // 2)

    target = str(biome_name).strip().casefold()
    selected: list[tuple[int, int]] = []
    for probe_y in dict.fromkeys(candidates):
        layer = next(
            (
                candidate
                for start, end, candidate in _layer_vertical_segments(document)
                if start <= probe_y <= end
            ),
            None,
        )
        if layer is None:
            continue
        ranges = []
        for start, end, cell in _layer_horizontal_segments(layer, width):
            block_index = cell.get("blockBiomeIndex")
            environment_index = cell.get("environmentBiomeIndex")
            block_name = (
                _biome_name_for_index(document, block_index).casefold()
                if isinstance(block_index, int)
                else ""
            )
            environment_name = (
                _biome_name_for_index(document, environment_index).casefold()
                if isinstance(environment_index, int)
                else ""
            )
            # After a paired floor has been applied, the floor owns the block
            # recipe throughout the basin while the surface ocean owns only
            # the upper environment.  Otherwise the block recipe is the
            # authoritative generated region.  Ordered-parallax repair may
            # clone the first biome's environment carrier for every region;
            # treating that carrier's baseName as geometry would expand a
            # first-position ocean across the entire planet.
            block_pair = ocean_biome_pair(block_name)
            paired_floor = (
                block_pair is not None
                and block_pair[1].casefold() == target
                and environment_name == target
            )
            if block_name == target or paired_floor:
                ranges.append((start, end))
        if ranges:
            selected = ranges
            break

    if not selected:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in selected:
        if merged and merged[-1][1] + 1 == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    # Represent a region crossing the planet seam as one wrapped interval.
    if len(merged) > 1 and merged[0][0] == 0 and merged[-1][1] == width - 1:
        merged = [(merged[-1][0], merged[0][1])] + merged[1:-1]
    return merged


def generated_biome_vertical_start(
    document: dict[str, Any], biome_name: str, x: int
) -> int | None:
    """Return the lowest compiled Y segment using a biome at one X tile."""

    width = int(document["size"][0])
    target = str(biome_name).strip().casefold()
    starts: list[int] = []
    for y_start, _y_end, layer in _layer_vertical_segments(document):
        cell = next(
            (
                candidate
                for x_start, x_end, candidate in _layer_horizontal_segments(
                    layer, width
                )
                if x_start <= int(x) <= x_end
            ),
            None,
        )
        if cell is None:
            continue
        names = []
        for key in ("environmentBiomeIndex", "blockBiomeIndex"):
            index = cell.get(key)
            if isinstance(index, int):
                names.append(_biome_name_for_index(document, index).casefold())
        if target in names:
            starts.append(y_start)
    return min(starts) if starts else None


def set_generated_ocean_floor_pairs(
    document: dict[str, Any],
    assets_folder: Path,
    settings: Sequence[dict[str, Any]],
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Align configured ocean floors to generated surface-ocean X ranges."""

    def emit(event: str, **details: Any) -> None:
        if progress is None:
            return
        try:
            progress(event, details)
        except Exception:
            # Diagnostics must never make an otherwise valid world edit fail.
            pass

    pairs = normalize_ocean_floor_pair_settings(settings)
    emit("settings_normalized", pairCount=len(pairs), pairs=pairs)
    if not pairs:
        return {"pairsApplied": 0, "rectangles": [], "warnings": []}
    parameters = world_template(document).get("worldParameters", {})
    surface_layer = (
        parameters.get("surfaceLayer") if isinstance(parameters, dict) else None
    )
    subsurface_layer = (
        parameters.get("subsurfaceLayer") if isinstance(parameters, dict) else None
    )
    atmosphere_layer = (
        parameters.get("atmosphereLayer") if isinstance(parameters, dict) else None
    )
    if not isinstance(surface_layer, dict) or not isinstance(subsurface_layer, dict):
        raise ValueError("The generated world has no surface/subsurface layer pair")
    height = int(document["size"][1])
    surface_min = int(round(float(surface_layer.get("layerMinHeight", 0))))
    floor_min = int(round(float(subsurface_layer.get("layerMinHeight", 0))))
    surface_end = height - 1
    if isinstance(atmosphere_layer, dict) and isinstance(
        atmosphere_layer.get("layerMinHeight"), (int, float)
    ):
        surface_end = int(round(float(atmosphere_layer["layerMinHeight"]))) - 1
    floor_min = min(height - 1, max(0, floor_min))
    surface_end = min(height - 1, max(surface_min, surface_end))
    emit(
        "layer_boundaries_resolved",
        worldSize=[int(document["size"][0]), height],
        subsurfaceMinimumY=floor_min,
        surfaceMinimumY=surface_min,
        surfaceMaximumY=surface_end,
    )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        grouped.setdefault(pair["surfaceBiome"], []).append(pair)

    rectangles: list[dict[str, Any]] = []
    warnings: list[str] = []
    for surface_name, configurations in grouped.items():
        ranges = generated_surface_biome_x_ranges(document, surface_name)
        emit(
            "surface_ranges_detected",
            surfaceBiome=surface_name,
            configuredPairCount=len(configurations),
            ranges=[[start, end] for start, end in ranges],
        )
        if not ranges:
            warnings.append(
                f"No generated surface interval uses biome {surface_name!r}"
            )
            continue
        if len(configurations) > 1 and len(ranges) != len(configurations):
            warnings.append(
                f"{surface_name}: {len(configurations)} configured entries map "
                f"left-to-right across {len(ranges)} generated intervals"
            )
        for range_index, (x_start, x_end) in enumerate(ranges):
            configuration = configurations[range_index % len(configurations)]
            sample_x = _wrapped_range_midpoint(x_start, x_end, int(document["size"][0]))
            detected_transition = generated_biome_vertical_start(
                document, surface_name, sample_x
            )
            detected_liquid_level = generated_ocean_liquid_level(
                document, surface_name, sample_x
            )
            transition = (
                configuration["transitionY"]
                if configuration["transitionY"] is not None
                else (detected_transition if detected_transition is not None else surface_min)
            )
            floor_height = configuration["floorHeight"]
            configured_bottom = configuration["floorBottomY"]
            if configured_bottom is None:
                # Preserve compatibility with sidecars written before the
                # explicit lower-bound field existed.
                rectangle_start = floor_min
                if floor_height is not None:
                    rectangle_start = min(
                        rectangle_start, int(math.floor(floor_height))
                    )
            else:
                rectangle_start = int(configured_bottom)
            rectangle_start = max(0, rectangle_start)
            emit(
                "pair_values_resolved",
                surfaceBiome=surface_name,
                floorBiome=configuration["floorBiome"],
                rangeIndex=range_index,
                xStart=x_start,
                xEnd=x_end,
                sampleX=sample_x,
                detectedTransitionY=detected_transition,
                detectedOceanLiquidLevel=detected_liquid_level,
                configuredFloorBottomY=configured_bottom,
                configuredFloorTerrainHeight=floor_height,
                configuredTransitionY=configuration["transitionY"],
                effectiveFloorBottomY=rectangle_start,
                effectiveTransitionY=transition,
                surfaceMaximumY=surface_end,
            )
            layout = set_asset_biome_rectangle(
                document,
                Path(assets_folder),
                surface_name,
                x_start,
                x_end,
                rectangle_start,
                surface_end,
                floor_height,
                ocean_floor_biome=configuration["floorBiome"],
                ocean_floor_transition=transition,
                ocean_floor_biome_overrides=configuration["biomeOverrides"],
                ocean_floor_region_overrides=configuration["regionOverrides"],
                preserve_ocean_surface_level=True,
                ocean_liquid_level=detected_liquid_level,
                ocean_coastal_slope=configuration["coastalSlopeEnabled"],
                ocean_coastal_slope_step=configuration["coastalSlopeStep"],
                ocean_floor_ecology_height=configuration["floorEcologyHeight"],
            )
            emit(
                "rectangle_compiled",
                surfaceBiome=surface_name,
                floorBiome=configuration["floorBiome"],
                xStart=x_start,
                xEnd=x_end,
                yStart=rectangle_start,
                yEnd=surface_end,
                layout=layout,
            )
            rectangles.append(
                {
                    "surfaceBiome": surface_name,
                    "floorBiome": configuration["floorBiome"],
                    "xStart": x_start,
                    "xEnd": x_end,
                    "yStart": rectangle_start,
                    "yEnd": surface_end,
                    **layout,
                }
            )
    return {
        "pairsApplied": len(rectangles),
        "rectangles": rectangles,
        "warnings": warnings,
    }


def inspect_world_asset_biome_rectangle(
    world: Path,
    assets_folder: Path,
    biome_name: str,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    ocean_floor_height: int | float | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Preview Tool 03 metadata and direct terrain-selector boundaries."""

    document = load_metadata_only(Path(world))
    if bool(options.pop("parallax_only", False)):
        parallax = options.pop("biome_parallax_override", None)
        if parallax is None:
            parallax = compile_asset_biome_parallax(
                document,
                Path(assets_folder),
                str(biome_name),
                (int(y_start) + int(y_end)) // 2,
            )
        result = set_parallax_rectangle(
            document,
            int(x_start),
            int(x_end),
            int(y_start),
            int(y_end),
            parallax,
        )
    else:
        result = set_asset_biome_rectangle(
            document,
            Path(assets_folder),
            str(biome_name),
            int(x_start),
            int(x_end),
            int(y_start),
            int(y_end),
            ocean_floor_height,
            **options,
        )
    result["size"] = [int(document["size"][0]), int(document["size"][1])]
    result["resetSectorRange"] = sector_aligned_rectangle(
        int(document["size"][0]),
        int(document["size"][1]),
        int(x_start),
        int(x_end),
        int(y_start),
        int(y_end),
    )
    return result


def json_pointer_parent(document: Any, pointer: str) -> tuple[Any, str | int]:
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must start with '/': {pointer!r}")
    tokens = [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]
    if not tokens or tokens == [""]:
        raise ValueError("Replacing the entire exported document is not supported")
    current = document
    for token in tokens[:-1]:
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise ValueError(f"JSON pointer traverses a scalar at {token!r}")
    final: str | int = int(tokens[-1]) if isinstance(current, list) else tokens[-1]
    return current, final


LEGACY_PROJECT_SCHEMA = "StarboundWorldEditorProject1"
PROJECT_SCHEMA = "StarboundWorldEditorProject2"
SUPPORTED_PROJECT_SCHEMAS = {LEGACY_PROJECT_SCHEMA, PROJECT_SCHEMA}
LAYER_DEFINITION_KEYS = (
    "atmosphereLayer",
    "spaceLayer",
    "surfaceLayer",
    "subsurfaceLayer",
    "undergroundLayers",
    "coreLayer",
)
WORLD_SETTING_KEYS = (
    "gravity",
    "dayLength",
    "airless",
    "beamUpRule",
    "environmentStatusEffects",
    "globalDirectives",
    "surfaceLiquid",
    "primaryBiome",
    "threatLevel",
    "disableDeathDrops",
)

WORLD_METADATA_EDITABLE_KEYS = (
    "spawningEnabled",
    "adjustPlayerStart",
    "playerStart",
    "respawnInWorld",
    "protectedDungeonIds",
    "dungeonIdBreathable",
    "dungeonIdGravity",
)
WORLD_PARAMETER_EDITABLE_KEYS = (
    "weatherPool",
    "gravity",
    "dayLength",
    "airless",
    "beamUpRule",
    "environmentStatusEffects",
    "globalDirectives",
    "surfaceLiquid",
    "primaryBiome",
    "threatLevel",
    "disableDeathDrops",
    "overrideTech",
    "terraformed",
    "worldEdgeForceRegions",
    "hueShift",
)
WORLD_CELESTIAL_EDITABLE_KEYS = (
    "worldName",
)
SKY_EDITABLE_KEYS = (
    "skyType",
    "skyColoring",
    "horizonImages",
    "horizonClouds",
    "satellites",
    "ambientLightLevel",
    "surfaceLevel",
    "spaceLevel",
    "planet",
    "seed",
)
TERRAIN_WORLD_PARAMETER_KEYS = (
    "blendSize",
    "blockNoise",
    "blendNoise",
)
TERRAIN_REGION_DATA_KEYS = (
    "regionBlending",
    "playerStartSearchRegions",
    "compiledLayers",
    "terrainSelectors",
)
BIOME_EDITABLE_KEYS = (
    "description",
    "mainBlock",
    "subBlocks",
    "ores",
    "hueShift",
    "materialHueShift",
    "spawnProfile",
    "parallax",
    "ambientNoises",
    "musicTrack",
    "surfacePlaceables",
    "undergroundPlaceables",
)


def deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def editable_biome_parameters(biome: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deep_copy_json(biome[key])
        for key in BIOME_EDITABLE_KEYS
        if key in biome
    }


def grouped_biome_parameters(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Collapse identical compiled copies of the same named biome."""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for index, biome in enumerate(compiled_biomes(document), start=1):
        name = str(biome.get("baseName", ""))
        parameters = editable_biome_parameters(biome)
        signature = json.dumps(
            parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        key = (name, signature)
        if key not in grouped:
            grouped[key] = {
                "indexes": [],
                "name": name,
                "parameters": parameters,
            }
            order.append(key)
        grouped[key]["indexes"].append(index)
    return [grouped[key] for key in order]


def make_editable_project(
    world: Path, document: dict[str, Any], database: BTreeDB5,
    by_id: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Create the compact V2 project, with every editable value stored once."""

    template = world_template(document)
    world_parameters = template["worldParameters"]
    region_data = template["regionData"]
    metadata = document["metadata"]
    sky_parameters = template.get("skyParameters", {})
    if not isinstance(sky_parameters, dict):
        sky_parameters = {}
    celestial_parameters = template.get("celestialParameters")

    world_values = {
        key: deep_copy_json(metadata[key])
        for key in WORLD_METADATA_EDITABLE_KEYS
        if key in metadata
    }
    world_values.update(
        {
            key: deep_copy_json(world_parameters[key])
            for key in WORLD_PARAMETER_EDITABLE_KEYS
            if key in world_parameters
        }
    )
    if isinstance(celestial_parameters, dict) and "name" in celestial_parameters:
        world_values["worldName"] = deep_copy_json(celestial_parameters["name"])

    sky_values = {
        key: deep_copy_json(sky_parameters[key])
        for key in SKY_EDITABLE_KEYS
        if key in sky_parameters and key != "skyColoring"
    }
    if "skyColoring" in sky_parameters:
        sky_values["skyColoring"] = deep_copy_json(sky_parameters["skyColoring"])
    elif "skyColoring" in world_parameters:
        sky_values["skyColoring"] = deep_copy_json(world_parameters["skyColoring"])

    terrain_values = {
        key: deep_copy_json(world_parameters[key])
        for key in TERRAIN_WORLD_PARAMETER_KEYS
        if key in world_parameters
    }
    terrain_values["layerDefinitions"] = {
        key: deep_copy_json(world_parameters[key])
        for key in LAYER_DEFINITION_KEYS
        if key in world_parameters
    }
    for exported_key, source_key in (
        ("regionBlending", "regionBlending"),
        ("playerStartSearchRegions", "playerStartSearchRegions"),
        ("compiledLayers", "layers"),
        ("terrainSelectors", "terrainSelectors"),
    ):
        if source_key in region_data:
            terrain_values[exported_key] = deep_copy_json(region_data[source_key])

    return {
        "source": {
            "schema": PROJECT_SCHEMA,
            "createdUtc": datetime.now(timezone.utc).isoformat(),
            "worldFile": str(world.resolve()),
            "sha256": file_sha256(world),
            "fileBytes": world.stat().st_size,
            "worldSize": deep_copy_json(document["size"]),
            "biomeCount": len(compiled_biomes(document)),
        },
        "world": world_values,
        "sky": sky_values,
        "terrain": terrain_values,
        "biomes": grouped_biome_parameters(document),
    }


def validate_project_terrain(
    terrain: dict[str, Any], width: int, height: int, biome_count: int
) -> None:
    if type(biome_count) is not int or not 0 <= biome_count <= MAX_BIOME_INDEX:
        raise BiomeIndexCapacityError(
            f"群系表共有 {biome_count!r} 项；游戏原生群系索引为 8 位，"
            f"最多支持 {MAX_BIOME_INDEX} 项（0 保留为无群系）。"
        )
    compiled_layers = terrain.get("compiledLayers")
    selectors = terrain.get("terrainSelectors")
    if not isinstance(compiled_layers, list) or not isinstance(selectors, list):
        raise ValueError("editable.terrain 必须包含 compiledLayers 和 terrainSelectors 数组")
    if not compiled_layers:
        raise ValueError("compiledLayers 不能为空")
    for selector_number, selector in enumerate(selectors, start=1):
        if not isinstance(selector, dict):
            raise ValueError(f"terrainSelectors[{selector_number}] 不是对象")
        if not isinstance(selector.get("type"), str) or not selector["type"]:
            raise ValueError(f"terrainSelectors[{selector_number}].type 无效")
        if not isinstance(selector.get("parameters"), dict):
            raise ValueError(f"terrainSelectors[{selector_number}].parameters 不是对象")
        if not isinstance(selector.get("config"), dict):
            raise ValueError(f"terrainSelectors[{selector_number}].config 不是对象")
    last_y: int | None = None
    for layer_number, layer in enumerate(compiled_layers, start=1):
        if not isinstance(layer, dict):
            raise ValueError(f"compiledLayers[{layer_number}] 不是对象")
        y_start = layer.get("yStart")
        boundaries = layer.get("boundaries")
        cells = layer.get("cells")
        if not isinstance(y_start, int) or not 0 <= y_start < height:
            raise ValueError(f"compiledLayers[{layer_number}].yStart 超出世界高度")
        if layer_number == 1 and y_start != 0:
            raise ValueError("compiledLayers 第一层必须从 yStart=0 开始")
        if last_y is not None and y_start <= last_y:
            raise ValueError("compiledLayers 的 yStart 必须严格递增")
        last_y = y_start
        if not isinstance(boundaries, list) or not all(
            isinstance(value, int) and 0 <= value < width - 1 for value in boundaries
        ):
            raise ValueError(f"compiledLayers[{layer_number}].boundaries 无效")
        if boundaries != sorted(boundaries) or len(boundaries) != len(set(boundaries)):
            raise ValueError(f"compiledLayers[{layer_number}].boundaries 必须递增且不重复")
        if not isinstance(cells, list) or len(cells) != len(boundaries) + 1:
            raise ValueError(
                f"compiledLayers[{layer_number}] 的 cells 数必须等于 boundaries 数加一"
            )
        for cell_number, cell in enumerate(cells, start=1):
            if not isinstance(cell, dict):
                raise ValueError(f"layer {layer_number} cell {cell_number} 不是对象")
            for key in ("blockBiomeIndex", "environmentBiomeIndex"):
                index = cell.get(key)
                # NullBiomeIndex is valid in either field, even when other
                # regions in this same world reference nonzero biomes.
                if type(index) is not int or not 0 <= index <= biome_count:
                    raise ValueError(
                        f"layer {layer_number} cell {cell_number} 的 {key} 无效："
                        f"{index!r}（应为 0..{biome_count} 的整数；0 表示无群系）"
                    )
            selector_index = cell.get("terrainSelectorIndex")
            # Compiled World4 region cells use one-based selector indexes;
            # index 0 is retained as the engine's legacy/no-selector sentinel.
            if not isinstance(selector_index, int) or not 0 <= selector_index <= len(selectors):
                raise ValueError(
                    f"layer {layer_number} cell {cell_number} 的 terrainSelectorIndex 无效"
                )
            for key in (
                "foregroundCaveSelectorIndex",
                "backgroundCaveSelectorIndex",
            ):
                if key not in cell:
                    continue
                index = cell.get(key)
                if not isinstance(index, int) or not (
                    index in (0, 0xFFFFFFFF) or 1 <= index <= len(selectors)
                ):
                    raise ValueError(
                        f"layer {layer_number} cell {cell_number} 的 {key} 无效"
                    )
            for key in (
                "subBlockSelectorIndexes",
                "foregroundOreSelectorIndexes",
                "backgroundOreSelectorIndexes",
            ):
                if key not in cell:
                    continue
                indexes = cell.get(key)
                if not isinstance(indexes, list) or not all(
                    isinstance(index, int)
                    and (index in (0, 0xFFFFFFFF) or 1 <= index <= len(selectors))
                    for index in indexes
                ):
                    raise ValueError(
                        f"layer {layer_number} cell {cell_number} 的 {key} 无效"
                    )


def apply_legacy_editable_project(
    project: dict[str, Any], source_document: dict[str, Any]
) -> dict[str, Any]:
    editor = project.get("editor")
    editable = project.get("editable")
    advanced = project.get("advancedWorldDocument")
    if not isinstance(editor, dict) or editor.get("schema") != LEGACY_PROJECT_SCHEMA:
        raise ValueError("这不是受支持的 Starbound World Editor JSON")
    if not isinstance(editable, dict) or not isinstance(advanced, dict):
        raise ValueError("项目缺少 editable 或 advancedWorldDocument")

    document = json.loads(json.dumps(advanced))
    if document.get("size") != source_document.get("size"):
        raise ValueError("为了避免截断世界，本版本不允许修改根 size")
    template = world_template(document)
    source_template = world_template(source_document)
    if template.get("size") != source_template.get("size"):
        raise ValueError("为了避免截断世界，本版本不允许修改 worldTemplate.size")

    metadata = document["metadata"]
    metadata["spawningEnabled"] = bool(editable.get("spawningEnabled", True))
    world_parameters = template["worldParameters"]
    region_data = template["regionData"]

    weather = editable.get("weatherPool")
    if not isinstance(weather, list):
        raise ValueError("editable.weatherPool 必须是数组")
    # Reuse the same checks as the command-line weather editor.
    weather_specs = []
    for entry in weather:
        if not isinstance(entry, dict):
            raise ValueError("weatherPool 中每项必须是对象")
        weather_specs.append(f"{entry.get('item', '')}={entry.get('weight', '')}")
    weather = parse_weather_specs(weather_specs)
    world_parameters["weatherPool"] = json.loads(json.dumps(weather))

    settings = editable.get("worldSettings", {})
    if not isinstance(settings, dict):
        raise ValueError("editable.worldSettings 必须是对象")
    for key, value in settings.items():
        if key not in WORLD_SETTING_KEYS:
            raise ValueError(f"不支持的简易 worldSettings 字段：{key}")
        world_parameters[key] = value
    template["skyParameters"] = editable.get("skyParameters")

    terrain = editable.get("terrain")
    if not isinstance(terrain, dict):
        raise ValueError("editable.terrain 必须是对象")
    layer_definitions = terrain.get("layerDefinitions")
    if not isinstance(layer_definitions, dict):
        raise ValueError("editable.terrain.layerDefinitions 必须是对象")
    for key, value in layer_definitions.items():
        if key not in LAYER_DEFINITION_KEYS:
            raise ValueError(f"未知地形层名称：{key}")
        world_parameters[key] = value

    project_biomes = editable.get("biomes")
    target_biomes = compiled_biomes(document)
    if not isinstance(project_biomes, list) or len(project_biomes) != len(target_biomes):
        raise ValueError("editable.biomes 数量不能改变")
    seen_indexes = set()
    rebuilt_biomes: list[dict[str, Any] | None] = [None] * len(target_biomes)
    for entry in project_biomes:
        if not isinstance(entry, dict):
            raise ValueError("editable.biomes 中每项必须是对象")
        index = entry.get("index")
        data = entry.get("data")
        if not isinstance(index, int) or not 1 <= index <= len(target_biomes):
            raise ValueError(f"无效 biome index：{index}")
        if index in seen_indexes or not isinstance(data, dict):
            raise ValueError(f"biome {index} 重复或缺少 data")
        seen_indexes.add(index)
        main_block = data.get("mainBlock")
        sub_blocks = data.get("subBlocks", [])
        if not isinstance(main_block, int) or not 0 <= main_block <= 65535:
            raise ValueError(f"biome {index} 的 mainBlock 无效")
        if not isinstance(sub_blocks, list) or not all(
            isinstance(value, int) and 0 <= value <= 65535 for value in sub_blocks
        ):
            raise ValueError(f"biome {index} 的 subBlocks 无效")
        spawn_profile = data.get("spawnProfile")
        if spawn_profile is not None:
            if not isinstance(spawn_profile, dict) or not isinstance(
                spawn_profile.get("spawnTypes", []), list
            ):
                raise ValueError(f"biome {index} 的 spawnProfile 无效")
            if not all(isinstance(value, str) for value in spawn_profile.get("spawnTypes", [])):
                raise ValueError(f"biome {index} 的 spawnTypes 必须都是字符串")
        parallax = data.get("parallax")
        if parallax is not None and not isinstance(parallax, dict):
            raise ValueError(f"biome {index} 的 parallax 必须是对象或 null")
        rebuilt_biomes[index - 1] = data
    region_data["biomes"] = rebuilt_biomes

    validate_project_terrain(
        terrain, int(document["size"][0]), int(document["size"][1]), len(target_biomes)
    )
    region_data["layers"] = terrain["compiledLayers"]
    region_data["terrainSelectors"] = terrain["terrainSelectors"]
    decode_world_metadata(encode_world_metadata(document))
    return document


def project_schema(project: Any) -> str | None:
    if not isinstance(project, dict):
        return None
    source = project.get("source")
    if isinstance(source, dict) and isinstance(source.get("schema"), str):
        return source["schema"]
    editor = project.get("editor")
    if isinstance(editor, dict) and isinstance(editor.get("schema"), str):
        return editor["schema"]
    return None


def _existing_dormant_noise_nans(parameters: Any, source_parameters: Any) -> Iterator[tuple[dict[str, Any], str, float]]:
    """Match only inherited NaNs in unused native random-placement noise.

    JSON cannot retain a NaN's binary payload. Imports may carry these values
    through, but must restore the original source value and cannot introduce
    new non-finite values or enable the dormant noise containing them.
    """
    if not isinstance(parameters, dict) or not isinstance(source_parameters, dict):
        return
    for field in ("surfacePlaceables", "undergroundPlaceables"):
        proposed = parameters.get(field)
        original = source_parameters.get(field)
        if not isinstance(proposed, dict) or not isinstance(original, dict):
            continue
        distributions = proposed.get("itemDistributions")
        source_distributions = original.get("itemDistributions")
        if not isinstance(distributions, list) or not isinstance(source_distributions, list):
            continue
        for distribution, source_distribution in zip(distributions, source_distributions):
            if not isinstance(distribution, dict) or not isinstance(source_distribution, dict):
                continue
            if distribution.get("distribution") != "random" or source_distribution.get("distribution") != "random":
                continue
            for noise_key in ("densityFunction", "modulusDistortion"):
                noise, source_noise = distribution.get(noise_key), source_distribution.get(noise_key)
                if not isinstance(noise, dict) or not isinstance(source_noise, dict):
                    continue
                if noise.get("type") != "uninitialized" or source_noise.get("type") != "uninitialized":
                    continue
                for key in ("bias", "alpha", "offset", "amplitude", "beta", "frequency", "gain"):
                    value, source_value = noise.get(key), source_noise.get(key)
                    if isinstance(value, float) and math.isnan(value) and isinstance(source_value, float) and math.isnan(source_value):
                        yield noise, key, source_value


def validate_biome_parameters(parameters: dict[str, Any], label: str, source_parameters: dict[str, Any] | None = None) -> None:
    checked = copy.deepcopy(parameters)
    for noise, key, _source_value in _existing_dormant_noise_nans(checked, source_parameters):
        noise[key] = 0.0  # Validation copy only; the stored source NaN is preserved.
    validate_finite_json_numbers(checked, label)
    main_block = parameters.get("mainBlock")
    sub_blocks = parameters.get("subBlocks", [])
    if not isinstance(main_block, int) or not 0 <= main_block <= 65535:
        raise ValueError(f"{label} 的 mainBlock 无效")
    if not isinstance(sub_blocks, list) or not all(
        isinstance(value, int) and 0 <= value <= 65535 for value in sub_blocks
    ):
        raise ValueError(f"{label} 的 subBlocks 无效")
    spawn_profile = parameters.get("spawnProfile")
    if spawn_profile is not None:
        if not isinstance(spawn_profile, dict) or not isinstance(
            spawn_profile.get("spawnTypes", []), list
        ):
            raise ValueError(f"{label} 的 spawnProfile 无效")
        if not all(
            isinstance(value, str) for value in spawn_profile.get("spawnTypes", [])
        ):
            raise ValueError(f"{label} 的 spawnTypes 必须都是字符串")
    parallax = parameters.get("parallax")
    if parallax is not None:
        if not isinstance(parallax, dict) or not isinstance(parallax.get("layers"), list):
            raise ValueError(f"{label} 的 parallax 必须是含 layers 的对象或 null")
        for layer_number, layer in enumerate(parallax["layers"], start=1):
            textures = layer.get("textures") if isinstance(layer, dict) else None
            if not isinstance(textures, list) or not textures or not all(
                isinstance(value, str) and value for value in textures
            ):
                raise ValueError(
                    f"{label} 的 parallax layer {layer_number} textures 无效"
                )


def apply_compact_project(
    project: dict[str, Any], source_document: dict[str, Any]
) -> dict[str, Any]:
    source_info = project.get("source")
    if not isinstance(source_info, dict) or source_info.get("schema") != PROJECT_SCHEMA:
        raise ValueError("这不是受支持的精简 Starbound World Editor JSON")
    if source_info.get("worldSize") != source_document.get("size"):
        raise ValueError("source.worldSize 与原 world 不一致，不能修改世界尺寸")
    if source_info.get("biomeCount") != len(compiled_biomes(source_document)):
        raise ValueError("source.biomeCount 与原 world 不一致")

    document = copy.deepcopy(source_document)
    template = world_template(document)
    metadata = document["metadata"]
    world_parameters = template["worldParameters"]
    region_data = template["regionData"]
    celestial = template.get("celestialParameters")
    visitable = (
        celestial.get("visitableParameters") if isinstance(celestial, dict) else None
    )
    if not isinstance(visitable, dict):
        visitable = None
    sky_parameters = template.get("skyParameters")
    if not isinstance(sky_parameters, dict):
        sky_parameters = {}

    world_values = project.get("world")
    if not isinstance(world_values, dict):
        raise ValueError("项目缺少 world 参数组")
    allowed_world = (
        set(WORLD_METADATA_EDITABLE_KEYS)
        | set(WORLD_PARAMETER_EDITABLE_KEYS)
        | set(WORLD_CELESTIAL_EDITABLE_KEYS)
    )
    unknown_world = set(world_values) - allowed_world
    if unknown_world:
        raise ValueError(f"world 中包含不支持的字段：{sorted(unknown_world)}")
    for key in WORLD_METADATA_EDITABLE_KEYS:
        if key in world_values:
            metadata[key] = deep_copy_json(world_values[key])
    for key in ("spawningEnabled", "adjustPlayerStart", "respawnInWorld"):
        if key in world_values and not isinstance(world_values[key], bool):
            raise ValueError(f"world.{key} 必须是 true 或 false")
    if "playerStart" in world_values:
        start = world_values["playerStart"]
        if not isinstance(start, list) or len(start) != 2 or not all(
            isinstance(value, (int, float)) for value in start
        ):
            raise ValueError("world.playerStart 必须是两个数字")
    if "worldName" in world_values:
        world_name = world_values["worldName"]
        if not isinstance(world_name, str):
            raise ValueError("world.worldName 必须是字符串")
        if not isinstance(celestial, dict):
            raise ValueError("这个 world 没有 celestialParameters，无法修改星球名称")
        celestial["name"] = world_name

    if "weatherPool" not in world_values:
        raise ValueError("world 缺少 weatherPool")
    weather = world_values["weatherPool"]
    if not isinstance(weather, list):
        raise ValueError("world.weatherPool 必须是数组")
    weather_specs = []
    for entry in weather:
        if not isinstance(entry, dict):
            raise ValueError("world.weatherPool 中每项必须是对象")
        weather_specs.append(f"{entry.get('item', '')}={entry.get('weight', '')}")
    # Airless/gateway worlds legitimately export an empty native weather pool.
    # An explicitly nonempty pool still needs at least one positive weight.
    normalized_weather = parse_weather_specs(weather_specs) if weather_specs else []

    for key in WORLD_PARAMETER_EDITABLE_KEYS:
        if key not in world_values:
            continue
        value = normalized_weather if key == "weatherPool" else world_values[key]
        world_parameters[key] = deep_copy_json(value)
        if visitable is not None:
            visitable[key] = deep_copy_json(value)
    if "dayLength" in world_values:
        sky_parameters["dayLength"] = deep_copy_json(world_values["dayLength"])

    sky_values = project.get("sky")
    if not isinstance(sky_values, dict):
        raise ValueError("项目缺少 sky 参数组")
    unknown_sky = set(sky_values) - set(SKY_EDITABLE_KEYS)
    if unknown_sky:
        raise ValueError(f"sky 中包含不支持的字段：{sorted(unknown_sky)}")
    for key in SKY_EDITABLE_KEYS:
        if key == "skyColoring" or key not in sky_values:
            continue
        sky_parameters[key] = deep_copy_json(sky_values[key])
    if "skyColoring" in sky_values:
        coloring = deep_copy_json(sky_values["skyColoring"])
        sky_parameters["skyColoring"] = coloring
        world_parameters["skyColoring"] = deep_copy_json(coloring)
        if visitable is not None:
            visitable["skyColoring"] = deep_copy_json(coloring)
    template["skyParameters"] = sky_parameters

    terrain = project.get("terrain")
    if not isinstance(terrain, dict):
        raise ValueError("项目缺少 terrain 参数组")
    allowed_terrain = set(TERRAIN_WORLD_PARAMETER_KEYS) | set(
        TERRAIN_REGION_DATA_KEYS
    ) | {"layerDefinitions"}
    unknown_terrain = set(terrain) - allowed_terrain
    if unknown_terrain:
        raise ValueError(f"terrain 中包含不支持的字段：{sorted(unknown_terrain)}")
    for key in TERRAIN_WORLD_PARAMETER_KEYS:
        if key in terrain:
            world_parameters[key] = deep_copy_json(terrain[key])
            if visitable is not None:
                visitable[key] = deep_copy_json(terrain[key])
    layer_definitions = terrain.get("layerDefinitions")
    if not isinstance(layer_definitions, dict):
        raise ValueError("terrain.layerDefinitions 必须是对象")
    unknown_layers = set(layer_definitions) - set(LAYER_DEFINITION_KEYS)
    if unknown_layers:
        raise ValueError(f"未知地形层名称：{sorted(unknown_layers)}")
    for key, value in layer_definitions.items():
        world_parameters[key] = deep_copy_json(value)
        if visitable is not None:
            visitable[key] = deep_copy_json(value)
    for exported_key, target_key in (
        ("regionBlending", "regionBlending"),
        ("playerStartSearchRegions", "playerStartSearchRegions"),
        ("compiledLayers", "layers"),
        ("terrainSelectors", "terrainSelectors"),
    ):
        if exported_key in terrain:
            region_data[target_key] = deep_copy_json(terrain[exported_key])

    project_biomes = project.get("biomes")
    if not isinstance(project_biomes, list):
        raise ValueError("项目缺少 biomes 参数组")
    source_groups = grouped_biome_parameters(source_document)
    source_group_map = {
        (tuple(group["indexes"]), group["name"]): group for group in source_groups
    }
    if len(project_biomes) != len(source_group_map):
        raise ValueError("biomes 配置组数量不能改变")
    rebuilt_biomes = copy.deepcopy(compiled_biomes(document))
    seen_groups: set[tuple[tuple[int, ...], str]] = set()
    for group_number, entry in enumerate(project_biomes, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"biomes[{group_number}] 必须是对象")
        indexes = entry.get("indexes")
        name = entry.get("name")
        parameters = entry.get("parameters")
        if not isinstance(indexes, list) or not all(
            isinstance(index, int) for index in indexes
        ) or not isinstance(name, str) or not isinstance(parameters, dict):
            raise ValueError(f"biomes[{group_number}] 的标识或 parameters 无效")
        key = (tuple(indexes), name)
        source_group = source_group_map.get(key)
        if source_group is None or key in seen_groups:
            raise ValueError(
                f"biomes[{group_number}] 的 indexes/name 与原 world 不一致"
            )
        seen_groups.add(key)
        expected_keys = set(source_group["parameters"])
        if set(parameters) != expected_keys:
            raise ValueError(
                f"biomes[{group_number}].parameters 字段不能增加或删除"
            )
        validate_biome_parameters(parameters, f"biomes[{group_number}]", source_group["parameters"])
        for index in indexes:
            original = rebuilt_biomes[index - 1]
            for parameter_key in BIOME_EDITABLE_KEYS:
                if parameter_key in parameters:
                    original[parameter_key] = deep_copy_json(parameters[parameter_key])
            for noise, noise_key, source_value in _existing_dormant_noise_nans(original, compiled_biomes(source_document)[index - 1]):
                noise[noise_key] = source_value
    region_data["biomes"] = rebuilt_biomes

    validate_project_terrain(
        {
            "compiledLayers": region_data.get("layers"),
            "terrainSelectors": region_data.get("terrainSelectors"),
        },
        int(document["size"][0]),
        int(document["size"][1]),
        len(rebuilt_biomes),
    )
    decode_world_metadata(encode_world_metadata(document))
    return document


def apply_editable_project(
    project: dict[str, Any], source_document: dict[str, Any]
) -> dict[str, Any]:
    schema = project_schema(project)
    if schema == LEGACY_PROJECT_SCHEMA:
        return apply_legacy_editable_project(project, source_document)
    if schema == PROJECT_SCHEMA:
        return apply_compact_project(project, source_document)
    raise ValueError("这不是受支持的 Starbound World Editor JSON")


def export_world_project(world: Path, output: Path, assets: Path | None = None) -> dict[str, Any]:
    database, _, document = load_metadata_document(world)
    try:
        project = make_editable_project(world, document, database)
        atomic_json_dump(project, output)
        return project
    finally:
        close_database(database)


def inspect_world_reference_hues(
    world: Path,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
) -> dict[str, Any]:
    """Summarize material styles available to Hue shift only mode."""

    database, records, document = load_metadata_document(Path(world))
    try:
        biome_material_hues = [
            int(biome.get("materialHueShift", 0)) & 0xFF
            for biome in compiled_biomes(document)
        ]
        _maps, result = _reference_tile_style_maps(
            records,
            int(document["size"][0]),
            int(document["size"][1]),
            int(x_start),
            int(x_end),
            int(y_start),
            int(y_end),
            biome_material_hues,
        )
        result["size"] = [int(document["size"][0]), int(document["size"][1])]
        result["hueShiftOnly"] = True
        return result
    finally:
        close_database(database)


def copy_world_reference_hues(
    source: Path,
    output: Path,
    target_x_start: int,
    target_x_end: int,
    target_y_start: int,
    target_y_end: int,
    reference_x_start: int,
    reference_x_end: int,
    reference_y_start: int,
    reference_y_end: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a world copy with matching target materials recolored only."""

    source = Path(source)
    output = Path(output)
    if not source.is_file():
        raise FileNotFoundError(f"找不到来源 world：{source}")
    if source.resolve() == output.resolve():
        raise ValueError("输出文件不能覆盖来源 world")
    database, records, document = load_metadata_document(source)
    try:
        biome_material_hues = [
            int(biome.get("materialHueShift", 0)) & 0xFF
            for biome in compiled_biomes(document)
        ]
        updated, result = apply_reference_hue_to_matching_materials(
            records,
            int(document["size"][0]),
            int(document["size"][1]),
            int(reference_x_start),
            int(reference_x_end),
            int(reference_y_start),
            int(reference_y_end),
            int(target_x_start),
            int(target_x_end),
            int(target_y_start),
            int(target_y_end),
            biome_material_hues,
        )
        result["referenceRange"] = {
            "xStart": int(reference_x_start),
            "xEnd": int(reference_x_end),
            "yStart": int(reference_y_start),
            "yEnd": int(reference_y_end),
        }
        result["targetRange"] = {
            "xStart": int(target_x_start),
            "xEnd": int(target_x_end),
            "yStart": int(target_y_start),
            "yEnd": int(target_y_end),
        }
        atomic_write_world(source, output, updated, database)
        return result, document
    finally:
        close_database(database)


def regenerate_world_biome_x_range(
    source: Path,
    output: Path,
    x_start: int,
    x_end: int,
    biome_indexes: Iterable[int],
    y_start: int | None = None,
    y_end: int | None = None,
    assets_folder: Path | None = None,
    target_biome_name: str | None = None,
    ocean_floor_height: int | float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a new world with matching generated sectors removed."""

    database, records, document = load_metadata_document(source)
    try:
        indexes = {int(index) for index in biome_indexes}
        biome_count = len(compiled_biomes(document))
        invalid = sorted(index for index in indexes if not 1 <= index <= biome_count)
        if invalid:
            raise ValueError(
                f"biome index 超出当前世界范围 1–{biome_count}：{invalid}"
            )
        catalog = None
        if assets_folder is not None:
            if not target_biome_name:
                raise ValueError("使用 assets 时必须指定目标 biome 名称")
            catalog = load_asset_biome_catalog(assets_folder)
            if target_biome_name not in catalog["biomes"]:
                raise ValueError(
                    f"所选 assets 中不存在 biome：{target_biome_name}"
                )
        rectangle_y_start = int(y_start) if y_start is not None else 0
        rectangle_y_end = (
            int(y_end) if y_end is not None else int(document["size"][1]) - 1
        )
        reset_sector_range = sector_aligned_rectangle(
            int(document["size"][0]),
            int(document["size"][1]),
            int(x_start),
            int(x_end),
            rectangle_y_start,
            rectangle_y_end,
        )
        if assets_folder is not None and target_biome_name and catalog is not None:
            # Recompile from assets even when this biome already exists in the
            # world.  The 03 tool is a full reset: terrain/caves/liquids/ores
            # and biome placeables must come from the selected asset recipe.
            layout_changes = set_asset_biome_rectangle(
                document,
                Path(assets_folder),
                target_biome_name,
                int(x_start),
                int(x_end),
                rectangle_y_start,
                rectangle_y_end,
                ocean_floor_height,
                ocean_coastal_slope=True,
            )
        elif indexes:
            layout_changes = set_compiled_biome_rectangle(
                document,
                int(x_start),
                int(x_end),
                rectangle_y_start,
                rectangle_y_end,
                indexes,
            )
        else:
            raise ValueError("未编译的 biome 必须从 assets 创建")
        updated, result = reset_generated_rectangle(
            records,
            int(document["size"][0]),
            int(document["size"][1]),
            int(x_start),
            int(x_end),
            rectangle_y_start,
            rectangle_y_end,
        )
        result.update(layout_changes)
        result["appliedTargetRange"] = {
            "xStart": int(x_start),
            "xEnd": int(x_end),
            "yStart": rectangle_y_start,
            "yEnd": rectangle_y_end,
            "wrapped": int(x_start) > int(x_end),
        }
        result["resetSectorRange"] = {
            key: reset_sector_range[key]
            for key in ("xStart", "xEnd", "yStart", "yEnd", "wrapped")
        }
        replacement = encode_world_metadata(document)
        updated = [
            (key, replacement if key == WORLD_METADATA_KEY else value)
            for key, value in updated
        ]
        atomic_write_world(source, output, updated, database)
        return result, document
    finally:
        close_database(database)


def validate_biome_rectangle_plan(
    world_width: int,
    world_height: int,
    rectangles: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize a multi-biome plan and reject exact tile overlaps."""

    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(rectangles, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"Biome rectangle {index} is not an object")
        validate_finite_json_numbers(value, f"Biome rectangle {index}")
        biome = str(value.get("biome", "")).strip()
        if not biome:
            raise ValueError(f"Biome rectangle {index} has no biome name")
        try:
            x_start = int(value["xStart"])
            x_end = int(value["xEnd"])
            y_start = int(value["yStart"])
            y_end = int(value["yEnd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Biome rectangle {index} has invalid coordinates") from exc
        if not 0 <= x_start < int(world_width) or not 0 <= x_end < int(world_width):
            raise ValueError(
                f"Biome rectangle {index} X must be from 0 to {world_width - 1}"
            )
        if not 0 <= y_start <= y_end < int(world_height):
            raise ValueError(
                f"Biome rectangle {index} Y must increase from 0 to {world_height - 1}"
            )
        def optional_number(key: str, *, integer: bool = False) -> int | float | None:
            raw = value.get(key)
            if raw in (None, ""):
                return None
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(
                    f"Biome rectangle {index} {key} must be a finite number"
                )
            return int(round(number)) if integer else number

        parallax_value = value.get("parallax")
        normalized_parallax = (
            normalize_parallax_value(parallax_value)
            if parallax_value is not None
            else None
        )
        floor = optional_number("oceanFloorHeight")
        floor_bottom = optional_number("oceanFloorBottom", integer=True)
        floor_transition = optional_number("oceanFloorTransition", integer=True)
        liquid_level = optional_number("oceanLiquidLevel", integer=True)
        coastal_step = int(value.get("coastalSlopeStep", 32))
        ecology_height = int(value.get("floorEcologyHeight", OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT))
        if not 1 <= coastal_step <= 512:
            raise ValueError(
                f"Biome rectangle {index} basin sampling width must be 1–512"
            )
        if not 0 <= ecology_height <= 2048:
            raise ValueError(
                f"Biome rectangle {index} floor ecology height must be 0–2048"
            )
        pair = ocean_biome_pair(biome)
        include_floor = bool(value.get("includeOceanFloor", pair is not None))
        floor_only = bool(
            value.get(
                "oceanFloorOnly",
                pair is not None and biome.casefold() == pair[0],
            )
        )
        if floor_only and pair is None:
            raise ValueError(
                f"Biome rectangle {index} marks non-ocean biome {biome} as floor-only"
            )
        if floor_only:
            include_floor = True
        if pair is None and any(
            item is not None
            for item in (floor, floor_bottom, floor_transition, liquid_level)
        ):
            raise ValueError(
                f"Biome rectangle {index} uses ocean settings for non-ocean biome {biome}"
            )
        if liquid_level is not None and not 0 <= int(liquid_level) < int(world_height):
            raise ValueError(
                f"Biome rectangle {index} ocean liquid surface must be from 0 to "
                f"{int(world_height) - 1}"
            )
        if pair is not None and include_floor:
            low = y_start if floor_bottom is None else int(floor_bottom)
            if not y_start <= low <= y_end:
                raise ValueError(
                    f"Biome rectangle {index} floor bottom must stay inside its Y range"
                )
            if floor is not None and not low <= float(floor) <= y_end:
                raise ValueError(
                    f"Biome rectangle {index} floor terrain must stay inside its Y range"
                )
            if (
                not floor_only
                and floor_transition is not None
                and not low < int(floor_transition) <= y_end
            ):
                raise ValueError(
                    f"Biome rectangle {index} ocean/floor boundary must stay inside its Y range"
                )
            if (
                floor is not None
                and floor_transition is not None
                and float(floor) >= int(floor_transition)
            ):
                raise ValueError(
                    f"Biome rectangle {index} floor terrain must be below its ocean/floor boundary"
                )
        normalized.append(
            {
                "biome": biome,
                "xStart": x_start,
                "xEnd": x_end,
                "yStart": y_start,
                "yEnd": y_end,
                "oceanFloorHeight": floor,
                "oceanFloorBottom": floor_bottom,
                "oceanFloorTransition": floor_transition,
                "oceanLiquidLevel": liquid_level,
                "oceanFloorBiome": (
                    str(value.get("oceanFloorBiome") or "").strip() or None
                ),
                "includeOceanFloor": include_floor,
                "oceanFloorOnly": floor_only,
                "oceanUniform": bool(value.get("oceanUniform", True)),
                "coastalSlopeEnabled": bool(
                    value.get("coastalSlopeEnabled", False)
                ),
                "coastalSlopeStep": coastal_step,
                "floorEcologyHeight": (
                    max(OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT, ecology_height)
                    if include_floor and value.get("coastalSlopeEnabled", False)
                    and not value.get("parallaxOnly", False)
                    else ecology_height
                ),
                "parallaxOnly": bool(value.get("parallaxOnly", False)),
                "parallax": normalized_parallax,
                "parallaxPath": str(value.get("parallaxPath", "")).strip(),
            }
        )

    def x_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_ranges = _wrapped_x_ranges(
            left["xStart"], left["xEnd"], int(world_width)
        )
        right_ranges = _wrapped_x_ranges(
            right["xStart"], right["xEnd"], int(world_width)
        )
        return any(
            a_start <= b_end and b_start <= a_end
            for a_start, a_end in left_ranges
            for b_start, b_end in right_ranges
        )

    for right_index, right in enumerate(normalized):
        for left_index, left in enumerate(normalized[:right_index]):
            y_overlap = (
                left["yStart"] <= right["yEnd"]
                and right["yStart"] <= left["yEnd"]
            )
            if y_overlap and x_overlap(left, right):
                raise ValueError(
                    "Biome rectangles "
                    f"{left_index + 1} and {right_index + 1} overlap"
                )
    return normalized


def _remap_biome_index_summary(value: Any, mapping: dict[int, int]) -> Any:
    """Keep user-facing edit results in sync with the final compacted table."""
    if isinstance(value, list):
        return [_remap_biome_index_summary(item, mapping) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        # Some lists are ordered ecology roles, not sets: keep repeats/order.
        if key.endswith("BiomeIndexes") and isinstance(item, list):
            result[key] = [mapping[index] for index in item]
        elif key.endswith("BiomeIndex") and type(item) is int:
            result[key] = mapping[item]
        else:
            result[key] = _remap_biome_index_summary(item, mapping)
    return result


def regenerate_world_biome_rectangles_in_place(
    world: Path,
    assets_folder: Path,
    rectangles: Sequence[dict[str, Any]],
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Apply several disjoint asset biome rectangles to one generated world.

    Only sectors intersecting a listed rectangle are removed.  The source file
    is replaced only after the rebuilt BTreeDB5 passes complete verification,
    and a timestamped backup is always retained next to it.
    """

    def emit(event: str, **details: Any) -> None:
        if progress is None:
            return
        try:
            progress(event, details)
        except Exception:
            # A diagnostic callback must never make a valid edit fail.
            pass

    world = Path(world)
    assets_folder = Path(assets_folder)
    if not world.is_file():
        raise FileNotFoundError(world)
    if not assets_folder.exists():
        raise FileNotFoundError(assets_folder)
    emit(
        "region_apply_started",
        world=str(world),
        assets=str(assets_folder),
        sourceSize=world.stat().st_size,
        sourceModifiedNs=world.stat().st_mtime_ns,
        requestedRectangleCount=len(rectangles),
        requestedRectangles=rectangles,
    )

    def builder(backup: Path, staged: Path) -> dict[str, Any]:
        emit("region_backup_created", backup=str(backup), staged=str(staged))
        database, records, document = load_metadata_document(backup)
        try:
            emit(
                "region_world_metadata_loaded",
                recordCount=len(records),
                worldSize=list(document.get("size", [])),
                identifier=document.get("identifier"),
            )
            width, height = int(document["size"][0]), int(document["size"][1])
            plan = validate_biome_rectangle_plan(width, height, rectangles)
            if not plan:
                raise ValueError("Add at least one biome rectangle")
            emit(
                "region_plan_validated",
                rectangleCount=len(plan),
                rectangles=plan,
            )
            changed: list[dict[str, Any]] = []
            updated = list(records)
            totals = {
                "resetSectors": 0,
                "removedTileRecords": 0,
                "removedEntityRecords": 0,
                "removedSectorUniqueRecords": 0,
                "removedUniqueIndexEntries": 0,
                "rewrittenUniqueIndexRecords": 0,
            }
            for rectangle_index, item in enumerate(plan):
                emit(
                    "region_rectangle_started",
                    rectangleIndex=rectangle_index,
                    rectangle=item,
                )
                parallax = item.get("parallax")
                if parallax is None and item.get("parallaxPath"):
                    parallax = load_parallax_json_file(Path(item["parallaxPath"]))
                if item.get("parallaxOnly"):
                    if parallax is None:
                        parallax = compile_asset_biome_parallax(
                            document,
                            assets_folder,
                            item["biome"],
                            (item["yStart"] + item["yEnd"]) // 2,
                        )
                    layout = set_parallax_rectangle(
                        document,
                        item["xStart"],
                        item["xEnd"],
                        item["yStart"],
                        item["yEnd"],
                        parallax,
                    )
                    reset = {
                        "resetSectors": 0,
                        "removedTileRecords": 0,
                        "removedEntityRecords": 0,
                        "removedSectorUniqueRecords": 0,
                        "removedUniqueIndexEntries": 0,
                        "rewrittenUniqueIndexRecords": 0,
                    }
                else:
                    layout = set_asset_biome_rectangle(
                        document,
                        assets_folder,
                        item["biome"],
                        item["xStart"],
                        item["xEnd"],
                        item["yStart"],
                        item["yEnd"],
                        item["oceanFloorHeight"],
                        ocean_floor_biome=item.get("oceanFloorBiome"),
                        ocean_floor_transition=item.get("oceanFloorTransition"),
                        preserve_ocean_surface_level=True,
                        ocean_liquid_level=item.get("oceanLiquidLevel"),
                        ocean_coastal_slope=item.get("coastalSlopeEnabled", False),
                        ocean_coastal_slope_step=item.get("coastalSlopeStep", 32),
                        ocean_floor_ecology_height=item.get("floorEcologyHeight", OCEAN_FLOOR_MINIMUM_ECOLOGY_HEIGHT),
                        include_ocean_floor=item.get("includeOceanFloor"),
                        ocean_floor_only=item.get("oceanFloorOnly", False),
                        ocean_floor_bottom=item.get("oceanFloorBottom"),
                        ocean_uniform=item.get("oceanUniform", True),
                        biome_parallax_override=parallax,
                    )
                    updated, reset = reset_generated_rectangle(
                        updated,
                        width,
                        height,
                        item["xStart"],
                        item["xEnd"],
                        item["yStart"],
                        item["yEnd"],
                        allow_empty=True,
                    )
                for key in totals:
                    totals[key] += int(reset.get(key, 0))
                changed.append({**item, **layout})
                emit(
                    "region_rectangle_completed",
                    rectangleIndex=rectangle_index,
                    layout=layout,
                    reset=reset,
                )
            # A complete edit can create temporary logical indexes >255.
            # Merge only byte-for-byte equivalent biome definitions and remap
            # BOTH layout and saved tile references before anything is written.
            # Never discard distinct biomes or guess what an existing zero meant.
            compaction = None
            if len(compiled_biomes(document)) > MAX_BIOME_INDEX:
                updated, compaction = compact_biome_table(
                    document, updated, tile_offsets=tile_sector_offsets,
                    tile_store_type=STORE_TILE_SECTOR,
                )
                changed = _remap_biome_index_summary(changed, compaction["indexMapping"])
                emit("region_biome_index_capacity_compacted", **compaction)
            validate_native_biome_indexes(document)
            region_data = world_template(document).get("regionData", {})
            validate_project_terrain(
                {
                    "compiledLayers": region_data.get("layers"),
                    "terrainSelectors": region_data.get("terrainSelectors"),
                },
                width,
                height,
                len(compiled_biomes(document)),
            )
            emit(
                "region_semantic_layout_verified",
                layerCount=len(region_data.get("layers") or []),
                terrainSelectorCount=len(region_data.get("terrainSelectors") or []),
                biomeCount=len(compiled_biomes(document)),
            )
            replacement = encode_world_metadata(document)
            updated = [
                (key, replacement if key == WORLD_METADATA_KEY else value)
                for key, value in updated
            ]
            atomic_write_world(backup, staged, updated, database)
            emit(
                "region_staged_world_written",
                staged=str(staged),
                stagedSize=staged.stat().st_size,
                remainingRecordCount=len(updated),
                totals=totals,
            )
            verified_document = load_metadata_only(staged)
            verified_region_data = world_template(verified_document).get(
                "regionData", {}
            )
            validate_project_terrain(
                {
                    "compiledLayers": verified_region_data.get("layers"),
                    "terrainSelectors": verified_region_data.get("terrainSelectors"),
                },
                int(verified_document["size"][0]),
                int(verified_document["size"][1]),
                len(compiled_biomes(verified_document)),
            )
            emit(
                "region_staged_world_semantically_verified",
                outputIdentifier=verified_document.get("identifier"),
                outputSize=list(verified_document.get("size", [])),
            )
            summary = {**totals, "rectangles": changed, "size": [width, height]}
            if compaction is not None:
                summary["biomeIndexCompaction"] = compaction
            return summary
        finally:
            close_database(database)

    backup, summary = rebuild_world_in_place_from_backup(world, builder, keep_backup=True)
    if backup is None:  # keep_backup=True guarantees a retained backup.
        raise RuntimeError("The verified region edit did not retain its backup")
    emit(
        "region_world_replaced_and_verified",
        world=str(world),
        backup=str(backup),
        outputSize=world.stat().st_size,
        outputModifiedNs=world.stat().st_mtime_ns,
        summary=summary,
    )
    return backup, summary


OCEAN_PAIR_SIDECAR_FILENAME = "starbound_world_editor_ocean_pairs.json"


def ocean_pair_cache_path(universe_folder: Path) -> Path:
    """Keep editor recipes outside game saves, separately for each universe."""
    identity = os.path.normcase(str(Path(universe_folder).resolve()))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "StarboundWorldEditor" / "ocean_pairs" / (key + ".json")


def ocean_pair_storage_paths(universe_folder: Path) -> list[Path]:
    """Both inputs matter during migration and concurrent-save checks."""
    return [Path(universe_folder) / OCEAN_PAIR_SIDECAR_FILENAME,
            ocean_pair_cache_path(universe_folder)]


def _read_ocean_pair_store(path: Path, universe_folder: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or not isinstance(value.get("bodies"), dict):
        raise ValueError(f"Invalid ocean-floor pairing file: {path}")
    identity = value.get("universeFolder")
    if identity is not None and os.path.normcase(str(Path(identity).resolve())) != os.path.normcase(str(Path(universe_folder).resolve())):
        raise ValueError(f"Ocean-floor pairing cache belongs to another universe: {path}")
    return {"version": 1, "bodies": value["bodies"]}


def ocean_pair_body_key(
    location: Sequence[int], planet_orbit: int, moon_orbit: int
) -> str:
    if len(location) != 3:
        raise ValueError("A celestial location must contain X, Y and Z")
    return (
        f"{int(location[0])}:{int(location[1])}:{int(location[2])}"
        f"|{int(planet_orbit)}|{int(moon_orbit)}"
    )


def load_ocean_pair_sidecar(universe_folder: Path) -> dict[str, Any]:
    # Loading is read-only. An older editor may have saved a new legacy file
    # after migration, so use the newest complete store, never merge stale keys
    # (which could resurrect removed bodies/pairings).
    candidates = [path for path in ocean_pair_storage_paths(universe_folder) if path.is_file()]
    if not candidates:
        return {"version": 1, "bodies": {}}
    path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
    return _read_ocean_pair_store(path, universe_folder)


def save_ocean_pair_sidecar(
    universe_folder: Path, document: dict[str, Any]
) -> Path:
    """Commit effective recipes to local settings, then remove the legacy file.

    Call only at the successful end of a world-save transaction. Failed saves
    keep their previous recipes; no pending records for other bodies are lost.
    """
    legacy, path = ocean_pair_storage_paths(universe_folder)
    bodies = document.get("bodies") if isinstance(document, dict) else None
    if not isinstance(bodies, dict):
        raise ValueError("Ocean-floor pairing document has no bodies object")
    normalized_bodies = deep_copy_json(bodies)
    for body_key, record in normalized_bodies.items():
        if not isinstance(record, dict) or not isinstance(record.get("pairs"), list):
            raise ValueError(f"Invalid ocean-floor pairing body: {body_key}")
        original_pairs = record["pairs"]
        normalized_pairs = normalize_ocean_floor_pair_settings(original_pairs)
        record["pairs"] = [
            {**item, **({"surfaceIndex": original["surfaceIndex"]} if "surfaceIndex" in original else {})}
            for original, item in zip(original_pairs, normalized_pairs)
        ]
    previous = None
    if path.is_file():
        _read_ocean_pair_store(path, universe_folder)
        previous = json.loads(path.read_text(encoding="utf-8"))
    output = {"version": 1, "universeFolder": str(Path(universe_folder).resolve()),
              "bodies": normalized_bodies}
    atomic_json_dump(output, path)
    try:
        # Exact named legacy file only; never touch any .world or other editor
        # files. Its recipes have already been committed outside universe.
        legacy.unlink(missing_ok=True)
    except OSError:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic_json_dump(previous, path)
        raise
    return path


def apply_generated_ocean_floor_pairs_in_place(
    world: Path,
    assets_folder: Path,
    settings: Sequence[dict[str, Any]],
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Apply persistent Tool 05 ocean/floor pairings to one World4 file."""

    def emit(event: str, **details: Any) -> None:
        if progress is None:
            return
        try:
            progress(event, details)
        except Exception:
            # A logging callback is advisory and must not corrupt the edit.
            pass

    world = Path(world)
    assets_folder = Path(assets_folder)
    pairs = normalize_ocean_floor_pair_settings(settings)
    if not world.is_file():
        raise FileNotFoundError(world)
    if not assets_folder.exists():
        raise FileNotFoundError(assets_folder)
    emit(
        "apply_started",
        world=str(world),
        assets=str(assets_folder),
        sourceSize=world.stat().st_size,
        sourceModifiedNs=world.stat().st_mtime_ns,
        pairCount=len(pairs),
        pairs=pairs,
    )

    def builder(backup: Path, staged: Path) -> dict[str, Any]:
        emit("backup_created", backup=str(backup), staged=str(staged))
        database, records, document = load_metadata_document(backup)
        try:
            emit(
                "world_metadata_loaded",
                recordCount=len(records),
                worldSize=list(document.get("size", [])),
                identifier=document.get("identifier"),
            )
            summary = set_generated_ocean_floor_pairs(
                document,
                assets_folder,
                pairs,
                progress=lambda event, details: emit(event, **details),
            )
            if pairs and int(summary.get("pairsApplied", 0)) == 0:
                warning_text = "; ".join(
                    str(item) for item in summary.get("warnings", [])
                )
                raise ValueError(
                    "No generated ocean interval matched the configured pairing"
                    + (f": {warning_text}" if warning_text else "")
                )
            updated = list(records)
            totals = {
                "resetSectors": 0,
                "removedTileRecords": 0,
                "removedEntityRecords": 0,
                "removedSectorUniqueRecords": 0,
                "removedUniqueIndexEntries": 0,
                "rewrittenUniqueIndexRecords": 0,
            }
            for rectangle_index, item in enumerate(summary["rectangles"]):
                emit(
                    "sector_reset_started",
                    rectangleIndex=rectangle_index,
                    rectangle=item,
                )
                updated, reset = reset_generated_rectangle(
                    updated,
                    int(document["size"][0]),
                    int(document["size"][1]),
                    int(item["xStart"]),
                    int(item["xEnd"]),
                    int(item["yStart"]),
                    int(item["yEnd"]),
                    allow_empty=True,
                )
                for key in totals:
                    totals[key] += int(reset.get(key, 0))
                emit(
                    "sector_reset_completed",
                    rectangleIndex=rectangle_index,
                    reset=reset,
                )
            region_data = world_template(document).get("regionData", {})
            validate_project_terrain(
                {
                    "compiledLayers": region_data.get("layers"),
                    "terrainSelectors": region_data.get("terrainSelectors"),
                },
                int(document["size"][0]),
                int(document["size"][1]),
                len(compiled_biomes(document)),
            )
            emit(
                "semantic_layout_verified",
                layerCount=len(region_data.get("layers") or []),
                terrainSelectorCount=len(
                    region_data.get("terrainSelectors") or []
                ),
                biomeCount=len(compiled_biomes(document)),
            )
            replacement = encode_world_metadata(document)
            updated = [
                (key, replacement if key == WORLD_METADATA_KEY else value)
                for key, value in updated
            ]
            atomic_write_world(backup, staged, updated, database)
            emit(
                "staged_world_written",
                staged=str(staged),
                stagedSize=staged.stat().st_size,
                remainingRecordCount=len(updated),
                totals=totals,
            )
            verified_document = load_metadata_only(staged)
            verification: list[dict[str, Any]] = []
            verified_width = int(verified_document["size"][0])
            for item in summary["rectangles"]:
                sample_x = _wrapped_range_midpoint(
                    int(item["xStart"]),
                    int(item["xEnd"]),
                    verified_width,
                )
                floor_start = generated_biome_vertical_start(
                    verified_document, str(item["floorBiome"]), sample_x
                )
                check = {
                    "surfaceBiome": item["surfaceBiome"],
                    "floorBiome": item["floorBiome"],
                    "sampleX": sample_x,
                    "compiledFloorStartY": floor_start,
                }
                verification.append(check)
                if floor_start is None:
                    raise WorldFormatError(
                        "Staged world verification could not find compiled floor biome "
                        f"{item['floorBiome']!r} at X={sample_x}"
                    )
            summary["verification"] = verification
            emit(
                "staged_world_metadata_verified",
                verification=verification,
                outputIdentifier=verified_document.get("identifier"),
                outputSize=list(verified_document.get("size", [])),
            )
            return {**summary, **totals}
        finally:
            close_database(database)

    backup, summary = rebuild_world_in_place_from_backup(
        world, builder, keep_backup=True
    )
    emit(
        "world_replaced_and_verified",
        world=str(world),
        backup=str(backup) if backup is not None else None,
        outputSize=world.stat().st_size,
        outputModifiedNs=world.stat().st_mtime_ns,
        summary=summary,
    )
    return backup, summary


def regenerate_world_from_reference_rectangle(
    source: Path,
    output: Path,
    target_x_start: int,
    target_x_end: int,
    target_y_start: int,
    target_y_end: int,
    reference_x_start: int,
    reference_x_end: int,
    reference_y_start: int,
    reference_y_end: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Regenerate a target rectangle from an exact in-world biome recipe."""

    source = Path(source)
    output = Path(output)
    if not source.is_file():
        raise FileNotFoundError(f"找不到来源 world：{source}")
    if source.resolve() == output.resolve():
        raise ValueError("输出文件不能覆盖来源 world")

    database, records, document = load_metadata_document(source)
    try:
        requested_target = {
            "xStart": int(target_x_start),
            "xEnd": int(target_x_end),
            "yStart": int(target_y_start),
            "yEnd": int(target_y_end),
        }
        reset_sector_range = sector_aligned_rectangle(
            int(document["size"][0]),
            int(document["size"][1]),
            int(target_x_start),
            int(target_x_end),
            int(target_y_start),
            int(target_y_end),
        )
        layout_changes = set_reference_biome_rectangle(
            document,
            int(target_x_start),
            int(target_x_end),
            int(target_y_start),
            int(target_y_end),
            int(reference_x_start),
            int(reference_x_end),
            int(reference_y_start),
            int(reference_y_end),
        )
        updated, result = reset_generated_rectangle(
            records,
            int(document["size"][0]),
            int(document["size"][1]),
            int(target_x_start),
            int(target_x_end),
            int(target_y_start),
            int(target_y_end),
            allow_empty=True,
        )
        result.update(layout_changes)
        result["requestedTargetRange"] = requested_target
        result["appliedTargetRange"] = {
            **requested_target,
            "wrapped": int(target_x_start) > int(target_x_end),
        }
        result["resetSectorRange"] = {
            key: reset_sector_range[key]
            for key in ("xStart", "xEnd", "yStart", "yEnd", "wrapped")
        }
        result["referenceRange"] = {
            "xStart": int(reference_x_start),
            "xEnd": int(reference_x_end),
            "yStart": int(reference_y_start),
            "yEnd": int(reference_y_end),
        }
        replacement = encode_world_metadata(document)
        updated = [
            (key, replacement if key == WORLD_METADATA_KEY else value)
            for key, value in updated
        ]
        atomic_write_world(source, output, updated, database)
        return result, document
    finally:
        close_database(database)


def import_world_project(
    project_path: Path, output: Path, source_override: Path | None = None,
    allow_source_hash_mismatch: bool = False,
    world_name_override: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    source, document, _regeneration = import_world_project_with_regeneration(
        project_path,
        output,
        source_override,
        allow_source_hash_mismatch,
        world_name_override=world_name_override,
    )
    return source, document


def import_world_project_with_regeneration(
    project_path: Path,
    output: Path,
    source_override: Path | None = None,
    allow_source_hash_mismatch: bool = False,
    regenerate_x_range: tuple[int, int] | None = None,
    regenerate_biome_indexes: Iterable[int] | None = None,
    world_name_override: str | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    schema = project_schema(project)
    if schema not in SUPPORTED_PROJECT_SCHEMAS:
        raise ValueError("所选 JSON 不是 Starbound World Editor 项目")
    if schema == PROJECT_SCHEMA:
        source_info = project["source"]
        source = source_override or Path(str(source_info.get("worldFile", "")))
        expected_hash = str(source_info.get("sha256", "")).upper()
    else:
        editor = project["editor"]
        source = source_override or Path(str(editor.get("sourceWorld", "")))
        expected_hash = str(editor.get("sourceSha256", "")).upper()
    if not source.is_file():
        raise FileNotFoundError(f"找不到来源 world：{source}")
    actual_hash = file_sha256(source)
    if expected_hash != actual_hash and not allow_source_hash_mismatch:
        raise ValueError(
            "来源 world 的 SHA-256 与导出时不同；请选择正确原文件，或明确允许忽略校验"
        )
    database, records, source_document = load_metadata_document(source)
    try:
        document = apply_editable_project(project, source_document)
        if world_name_override is not None:
            if not isinstance(world_name_override, str) or not world_name_override.strip():
                raise ValueError("新星球名称不能为空")
            celestial = world_template(document).get("celestialParameters")
            if not isinstance(celestial, dict):
                raise WorldFormatError("这个 world 没有 celestialParameters，无法修改星球名称")
            celestial["name"] = world_name_override
        regeneration = None
        if regenerate_x_range is not None:
            if regenerate_biome_indexes is None:
                raise ValueError("重生区域时必须选择 biome")
            biome_indexes = {int(index) for index in regenerate_biome_indexes}
            biome_count = len(compiled_biomes(document))
            invalid = sorted(index for index in biome_indexes if not 1 <= index <= biome_count)
            if invalid:
                raise ValueError(
                    f"biome index 超出当前世界范围 1–{biome_count}：{invalid}"
                )
            records, regeneration = reset_generated_biome_x_range(
                records,
                int(document["size"][0]),
                int(regenerate_x_range[0]),
                int(regenerate_x_range[1]),
                biome_indexes,
            )
        write_metadata_document(source, output, database, records, document)
        return source, document, regeneration
    finally:
        close_database(database)


def command_inspect(args: argparse.Namespace) -> int:
    database, records = load_records(args.world)
    try:
        metadata_value = dict(records)[WORLD_METADATA_KEY]
        document = decode_world_metadata(metadata_value)
        layer_counts: dict[str, int] = {}
        for key, _ in records:
            layer = str(key[0])
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
        summary = {
            "path": str(args.world.resolve()),
            "database": {
                "identifier": database.identifier,
                "blockSize": database.block_size,
                "keySize": database.key_size,
                "activeRoot": 2 if database.use_alt_root else 1,
                "recordCount": len(records),
                "recordCountsByLayer": layer_counts,
            },
            "world": {
                "size": document["size"],
                "metadataVersion": document["format"]["version"],
                "topLevelMetadataKeys": list(document["metadata"].keys()),
            },
        }
        json_dump(summary, args.output)
    finally:
        close_database(database)
    return 0


def command_export(args: argparse.Namespace) -> int:
    database, records = load_records(args.world)
    try:
        document = decode_world_metadata(dict(records)[WORLD_METADATA_KEY])
        json_dump(document, args.output)
    finally:
        close_database(database)
    return 0


def command_import(args: argparse.Namespace) -> int:
    database, records = load_records(args.world)
    try:
        document = json.loads(args.metadata.read_text(encoding="utf-8"))
        replacement = encode_world_metadata(document)
        updated = [
            (key, replacement if key == WORLD_METADATA_KEY else value)
            for key, value in records
        ]
        if not any(key == WORLD_METADATA_KEY for key, _ in records):
            raise WorldFormatError("World has no metadata record")
        atomic_write_world(args.world, args.output, updated, database)
    finally:
        close_database(database)
    return 0


def command_list_biomes(args: argparse.Namespace) -> int:
    database, _, document = load_metadata_document(args.world)
    try:
        by_id, _ = load_material_catalog(args.assets)
        json_dump(
            {
                "world": str(args.world.resolve()),
                "biomeIndexBase": 1,
                "biomes": biome_summary(document, by_id),
            },
            args.output,
        )
    finally:
        close_database(database)
    return 0


def weather_pool_to_editor_pairs(pool: Any) -> list[list[Any]]:
    """Convert World4 weather objects to the compact GUI pair format."""

    if not isinstance(pool, list):
        raise ValueError("Weather pool must be an array")
    result: list[list[Any]] = []
    for index, entry in enumerate(pool, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Weather entry {index} must be an object")
        if "weight" not in entry or "item" not in entry:
            raise ValueError(f"Weather entry {index} needs weight and item")
        result.append([entry["weight"], entry["item"]])
    return result


def weather_pool_from_editor(value: Any) -> list[dict[str, Any]]:
    """Normalize the GUI's ``[[weight, name], ...]`` weather representation.

    Older object-form input remains accepted for compatibility, but every
    saved World4/Celestial record is returned in Starbound's native
    ``[{"weight": ..., "item": ...}, ...]`` form.
    """

    if not isinstance(value, list):
        raise ValueError("Weather pool must be a JSON array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    positive = False
    for index, entry in enumerate(value, start=1):
        if isinstance(entry, dict):
            weight_value = entry.get("weight")
            item_value = entry.get("item")
        elif isinstance(entry, list) and len(entry) == 2:
            weight_value, item_value = entry
        else:
            raise ValueError(
                f"Weather entry {index} must be [weight, name]"
            )
        if isinstance(weight_value, bool) or not isinstance(
            weight_value, (int, float)
        ):
            raise ValueError(f"Weather entry {index} weight must be numeric")
        weight = float(weight_value)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(
                f"Weather entry {index} weight must be finite and non-negative"
            )
        if not isinstance(item_value, str) or not item_value.strip():
            raise ValueError(f"Weather entry {index} needs a weather name")
        item = item_value.strip()
        folded = item.casefold()
        if folded in seen:
            raise ValueError(f"Duplicate weather name: {item}")
        seen.add(folded)
        positive = positive or weight > 0
        result.append({"weight": weight, "item": item})
    if not result or not positive:
        raise ValueError("At least one weather entry needs a positive weight")
    return result


def parse_weather_specs(specs: Sequence[str]) -> list[dict[str, Any]]:
    weather_pool: list[dict[str, Any]] = []
    total = 0.0
    names: set[str] = set()
    for spec in specs:
        try:
            name, weight_text = spec.rsplit("=", 1)
            weight = float(weight_text)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Weather must use NAME=WEIGHT syntax, got {spec!r}"
            ) from exc
        name = name.strip()
        if not name or name in names:
            raise ValueError(f"Empty or duplicate weather name {name!r}")
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("Weather weights must be finite and cannot be negative")
        names.add(name)
        total += weight
        weather_pool.append({"weight": weight, "item": name})
    if total <= 0:
        raise ValueError("At least one weather entry needs a positive weight")
    return weather_pool


def command_set_weather(args: argparse.Namespace) -> int:
    database, records, document = load_metadata_document(args.world)
    try:
        weather_pool = parse_weather_specs(args.weather)
        template = world_template(document)
        template["worldParameters"]["weatherPool"] = weather_pool
        write_metadata_document(args.world, args.output, database, records, document)
        json_dump({"output": str(args.output.resolve()), "weatherPool": weather_pool})
    finally:
        close_database(database)
    return 0


def command_set_biome_monsters(args: argparse.Namespace) -> int:
    database, records, document = load_metadata_document(args.world)
    try:
        biome = biome_at(document, args.biome)
        parameters: Any = biome.get("spawnProfile", {}).get("monsterParameters")
        if args.parameters is not None:
            parameters = json.loads(args.parameters.read_text(encoding="utf-8"))
        biome["spawnProfile"] = {
            "monsterParameters": parameters,
            "spawnTypes": list(args.monsters),
        }
        write_metadata_document(args.world, args.output, database, records, document)
        json_dump(
            {
                "output": str(args.output.resolve()),
                "biome": args.biome,
                "spawnProfile": biome["spawnProfile"],
            }
        )
    finally:
        close_database(database)
    return 0


def command_set_biome_blocks(args: argparse.Namespace) -> int:
    database, records, document = load_metadata_document(args.world)
    try:
        _, by_name = load_material_catalog(args.assets)
        biome = biome_at(document, args.biome)
        previous_main = biome.get("mainBlock")
        previous_subs = list(biome.get("subBlocks", []))
        main_block = resolve_material(args.main_block, by_name)
        sub_blocks = [resolve_material(value, by_name) for value in args.sub_blocks]
        biome["mainBlock"] = main_block
        biome["subBlocks"] = sub_blocks

        updated_records: Sequence[tuple[bytes, bytes]] = records
        tile_changes = None
        if args.rewrite_generated_tiles:
            material_map: dict[int, int] = {}
            if isinstance(previous_main, int):
                material_map[previous_main] = main_block
            for old, new in zip(previous_subs, sub_blocks):
                if isinstance(old, int):
                    material_map[old] = new
            updated_records, tile_changes = replace_generated_materials(
                records, args.biome, material_map
            )
        replacement = encode_world_metadata(document)
        final_records = [
            (key, replacement if key == WORLD_METADATA_KEY else value)
            for key, value in updated_records
        ]
        atomic_write_world(args.world, args.output, final_records, database)
        json_dump(
            {
                "output": str(args.output.resolve()),
                "biome": args.biome,
                "mainBlock": main_block,
                "subBlocks": sub_blocks,
                "generatedTileChanges": tile_changes,
            }
        )
    finally:
        close_database(database)
    return 0


def command_copy_biome(args: argparse.Namespace) -> int:
    database, records, document = load_metadata_document(args.world)
    try:
        source = biome_at(document, args.source)
        target = biome_at(document, args.target)
        components = set(args.components)
        if "all" in components:
            components = {"background", "terrain", "monsters"}
        unknown = components - {"background", "terrain", "monsters"}
        if unknown:
            raise ValueError(f"Unknown biome components: {sorted(unknown)!r}")

        old_main = target.get("mainBlock")
        old_subs = list(target.get("subBlocks", []))
        if "background" in components:
            target["parallax"] = json.loads(json.dumps(source.get("parallax")))
        if "monsters" in components:
            target["spawnProfile"] = json.loads(
                json.dumps(source.get("spawnProfile", {"monsterParameters": None, "spawnTypes": []}))
            )

        if "terrain" in components:
            for key in ("mainBlock", "subBlocks", "ores", "materialHueShift"):
                if key in source:
                    target[key] = json.loads(json.dumps(source[key]))
            source_cells = [
                cell
                for cell in iter_compiled_cells(document)
                if cell.get("blockBiomeIndex") == args.source
            ]
            if not source_cells:
                raise WorldFormatError(
                    f"Biome {args.source} has no compiled terrain cell to copy"
                )
            source_cell = source_cells[0]
            for target_cell in iter_compiled_cells(document):
                if target_cell.get("blockBiomeIndex") != args.target:
                    continue
                for key, value in source_cell.items():
                    if key not in {"blockBiomeIndex", "environmentBiomeIndex"}:
                        target_cell[key] = json.loads(json.dumps(value))

        updated_records: Sequence[tuple[bytes, bytes]] = records
        tile_changes = None
        if args.rewrite_generated_tiles:
            if "terrain" not in components:
                raise ValueError("--rewrite-generated-tiles requires the terrain component")
            material_map: dict[int, int] = {}
            new_main = target.get("mainBlock")
            new_subs = target.get("subBlocks", [])
            if isinstance(old_main, int) and isinstance(new_main, int):
                material_map[old_main] = new_main
            for old, new in zip(old_subs, new_subs):
                if isinstance(old, int) and isinstance(new, int):
                    material_map[old] = new
            updated_records, tile_changes = replace_generated_materials(
                records, args.target, material_map
            )

        replacement = encode_world_metadata(document)
        final_records = [
            (key, replacement if key == WORLD_METADATA_KEY else value)
            for key, value in updated_records
        ]
        atomic_write_world(args.world, args.output, final_records, database)
        json_dump(
            {
                "output": str(args.output.resolve()),
                "sourceBiome": args.source,
                "targetBiome": args.target,
                "components": sorted(components),
                "generatedTileChanges": tile_changes,
            }
        )
    finally:
        close_database(database)
    return 0


def command_export_component(args: argparse.Namespace) -> int:
    database, _, document = load_metadata_document(args.world)
    try:
        biome = biome_at(document, args.biome)
        if args.component == "background":
            value = biome.get("parallax")
        elif args.component == "monsters":
            value = biome.get("spawnProfile")
        elif args.component == "terrain":
            value = {
                "mainBlock": biome.get("mainBlock"),
                "subBlocks": biome.get("subBlocks", []),
                "ores": biome.get("ores", []),
                "materialHueShift": biome.get("materialHueShift", 0),
                "compiledCells": [
                    cell
                    for cell in iter_compiled_cells(document)
                    if cell.get("blockBiomeIndex") == args.biome
                ],
            }
        else:
            raise ValueError(f"Unknown component {args.component!r}")
        json_dump(
            {"biome": args.biome, "component": args.component, "value": value},
            args.output,
        )
    finally:
        close_database(database)
    return 0


def command_set_biome_background(args: argparse.Namespace) -> int:
    database, records, document = load_metadata_document(args.world)
    try:
        imported = json.loads(args.parallax.read_text(encoding="utf-8"))
        if (
            isinstance(imported, dict)
            and imported.get("component") == "background"
            and "value" in imported
        ):
            imported = imported["value"]
        if imported is not None and not isinstance(imported, dict):
            raise ValueError("Parallax JSON must be an object, null, or exported wrapper")
        if isinstance(imported, dict):
            layers = imported.get("layers")
            if not isinstance(layers, list):
                raise ValueError("Parallax object must contain a layers list")
            for number, layer in enumerate(layers, start=1):
                if not isinstance(layer, dict) or not isinstance(layer.get("textures"), list):
                    raise ValueError(f"Parallax layer {number} has no textures list")
        biome_at(document, args.biome)["parallax"] = imported
        write_metadata_document(args.world, args.output, database, records, document)
        json_dump(
            {
                "output": str(args.output.resolve()),
                "biome": args.biome,
                "parallaxLayerCount": (
                    len(imported.get("layers", [])) if isinstance(imported, dict) else 0
                ),
            }
        )
    finally:
        close_database(database)
    return 0


def command_apply_patch(args: argparse.Namespace) -> int:
    database, records, document = load_metadata_document(args.world)
    try:
        patch = json.loads(args.patch.read_text(encoding="utf-8"))
        operations = patch.get("operations") if isinstance(patch, dict) else patch
        if not isinstance(operations, list):
            raise ValueError("Patch must be a list or contain an operations list")
        for number, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                raise ValueError(f"Patch operation {number} is not an object")
            op = operation.get("op")
            pointer = operation.get("path")
            if not isinstance(pointer, str):
                raise ValueError(f"Patch operation {number} has no string path")
            parent, key = json_pointer_parent(document, pointer)
            if op in {"set", "replace", "add"}:
                parent[key] = operation.get("value")
            elif op == "remove":
                if isinstance(parent, list):
                    parent.pop(key)
                else:
                    del parent[key]
            else:
                raise ValueError(f"Unsupported patch operation {op!r}")
        # Force a complete encode/decode before writing so invalid SBON values
        # are reported against the patch, not after the destination is created.
        decode_world_metadata(encode_world_metadata(document))
        write_metadata_document(args.world, args.output, database, records, document)
        json_dump({"output": str(args.output.resolve()), "operations": len(operations)})
    finally:
        close_database(database)
    return 0


def command_export_project(args: argparse.Namespace) -> int:
    project = export_world_project(args.world, args.output, args.assets)
    json_dump(
        {
            "output": str(args.output.resolve()),
            "sourceSha256": project["source"]["sha256"],
            "biomeProfiles": len(project["biomes"]),
            "biomes": project["source"]["biomeCount"],
            "worldSize": project["source"]["worldSize"],
        }
    )
    return 0


def command_import_project(args: argparse.Namespace) -> int:
    source, document = import_world_project(
        args.project,
        args.output,
        args.source,
        args.allow_source_hash_mismatch,
    )
    json_dump(
        {
            "output": str(args.output.resolve()),
            "source": str(source.resolve()),
            "worldSize": document["size"],
        }
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    database, records = load_records(args.world)
    try:
        keys = [key for key, _ in records]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise WorldFormatError("B-tree keys are unsorted or duplicated")
        document = decode_world_metadata(dict(records)[WORLD_METADATA_KEY])
        result = {
            "valid": True,
            "records": len(records),
            "size": document["size"],
            "identifier": database.identifier,
        }
        json_dump(result)
    finally:
        close_database(database)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and safely rewrite Starbound World4 .world files"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="show container summary")
    inspect_parser.add_argument("world", type=Path)
    inspect_parser.add_argument("-o", "--output", type=Path)
    inspect_parser.set_defaults(func=command_inspect)

    export_parser = subparsers.add_parser(
        "export-metadata", help="export editable WorldMetadata JSON"
    )
    export_parser.add_argument("world", type=Path)
    export_parser.add_argument("output", type=Path)
    export_parser.set_defaults(func=command_export)

    import_parser = subparsers.add_parser(
        "import-metadata", help="write edited metadata into a new .world file"
    )
    import_parser.add_argument("world", type=Path)
    import_parser.add_argument("metadata", type=Path)
    import_parser.add_argument("output", type=Path)
    import_parser.set_defaults(func=command_import)

    biome_list_parser = subparsers.add_parser(
        "list-biomes", help="list compiled biome indexes and editable values"
    )
    biome_list_parser.add_argument("world", type=Path)
    biome_list_parser.add_argument("-o", "--output", type=Path)
    biome_list_parser.add_argument(
        "--assets", type=Path, help="unpacked assets root, for material names"
    )
    biome_list_parser.set_defaults(func=command_list_biomes)

    weather_parser = subparsers.add_parser(
        "set-weather", help="replace the world's weighted weather pool"
    )
    weather_parser.add_argument("world", type=Path)
    weather_parser.add_argument("output", type=Path)
    weather_parser.add_argument(
        "weather", nargs="+", metavar="NAME=WEIGHT",
        help="for example clear=0.5 rain=0.5"
    )
    weather_parser.set_defaults(func=command_set_weather)

    monsters_parser = subparsers.add_parser(
        "set-biome-monsters", help="replace one compiled biome's spawn types"
    )
    monsters_parser.add_argument("world", type=Path)
    monsters_parser.add_argument("output", type=Path)
    monsters_parser.add_argument("--biome", type=int, required=True)
    monsters_parser.add_argument(
        "--parameters", type=Path,
        help="optional JSON file for monsterParameters; otherwise preserve it"
    )
    monsters_parser.add_argument(
        "--monsters", nargs="*", required=True, metavar="SPAWN_TYPE",
        help="spawnTypes list; pass --monsters with no following values to clear it"
    )
    monsters_parser.set_defaults(func=command_set_biome_monsters)

    blocks_parser = subparsers.add_parser(
        "set-biome-blocks", help="set a biome's main and sub-block material IDs"
    )
    blocks_parser.add_argument("world", type=Path)
    blocks_parser.add_argument("output", type=Path)
    blocks_parser.add_argument("--biome", type=int, required=True)
    blocks_parser.add_argument("--main-block", required=True)
    blocks_parser.add_argument("--sub-blocks", nargs="*", default=[])
    blocks_parser.add_argument(
        "--assets", type=Path, help="unpacked assets root, required for material names"
    )
    blocks_parser.add_argument(
        "--rewrite-generated-tiles", action="store_true",
        help="also replace matching existing foreground/background tile materials"
    )
    blocks_parser.set_defaults(func=command_set_biome_blocks)

    copy_parser = subparsers.add_parser(
        "copy-biome", help="copy background, terrain and/or monsters between indexes"
    )
    copy_parser.add_argument("world", type=Path)
    copy_parser.add_argument("output", type=Path)
    copy_parser.add_argument("--source", type=int, required=True)
    copy_parser.add_argument("--target", type=int, required=True)
    copy_parser.add_argument(
        "--components", nargs="+", required=True,
        choices=["background", "terrain", "monsters", "all"]
    )
    copy_parser.add_argument(
        "--rewrite-generated-tiles", action="store_true",
        help="with terrain, also translate existing target-biome material IDs"
    )
    copy_parser.set_defaults(func=command_copy_biome)

    component_parser = subparsers.add_parser(
        "export-biome-component", help="export one background/terrain/monster component"
    )
    component_parser.add_argument("world", type=Path)
    component_parser.add_argument("output", type=Path)
    component_parser.add_argument("--biome", type=int, required=True)
    component_parser.add_argument(
        "--component", required=True, choices=["background", "terrain", "monsters"]
    )
    component_parser.set_defaults(func=command_export_component)

    background_parser = subparsers.add_parser(
        "set-biome-background", help="import a resolved parallax JSON for one biome"
    )
    background_parser.add_argument("world", type=Path)
    background_parser.add_argument("output", type=Path)
    background_parser.add_argument("--biome", type=int, required=True)
    background_parser.add_argument(
        "--parallax", type=Path, required=True,
        help="raw parallax object or export-biome-component output"
    )
    background_parser.set_defaults(func=command_set_biome_background)

    patch_parser = subparsers.add_parser(
        "apply-patch", help="apply JSON-pointer edits and write a new world"
    )
    patch_parser.add_argument("world", type=Path)
    patch_parser.add_argument("patch", type=Path)
    patch_parser.add_argument("output", type=Path)
    patch_parser.set_defaults(func=command_apply_patch)

    export_project_parser = subparsers.add_parser(
        "export-project", help="convert a world into one readable/editable project JSON"
    )
    export_project_parser.add_argument("world", type=Path)
    export_project_parser.add_argument("output", type=Path)
    export_project_parser.add_argument(
        "--assets", type=Path, help=argparse.SUPPRESS
    )
    export_project_parser.set_defaults(func=command_export_project)

    import_project_parser = subparsers.add_parser(
        "import-project", help="rebuild a new world from an editable project JSON"
    )
    import_project_parser.add_argument("project", type=Path)
    import_project_parser.add_argument("output", type=Path)
    import_project_parser.add_argument(
        "--source", type=Path, help="override source world path stored in the project"
    )
    import_project_parser.add_argument(
        "--allow-source-hash-mismatch", action="store_true",
        help="dangerous: apply to a source whose SHA-256 differs"
    )
    import_project_parser.set_defaults(func=command_import_project)

    verify_parser = subparsers.add_parser("verify", help="validate readable structure")
    verify_parser.add_argument("world", type=Path)
    verify_parser.set_defaults(func=command_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zlib.error) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
