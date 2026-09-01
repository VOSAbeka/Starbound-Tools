#!/usr/bin/env python3
"""Read and safely rewrite Starbound World4 (.world) files.

This module intentionally uses only Python's standard library.  It understands
the BTreeDB5 container, zlib-compressed World4 records and Starbound's binary
object notation (SBON).  The command line interface is defined near the end of
the file.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import random
import re
import shutil
import struct
import sys
import tempfile
from typing import Any, BinaryIO, Iterable, Iterator, Sequence
import zlib


HEADER_SIZE = 512
MAGIC = b"BTreeDB5"
WORLD_IDENTIFIER = "World4"
CELESTIAL_IDENTIFIER = "Celestial2"
CELESTIAL_KEY_SIZE = 32
CELESTIAL_CHUNK_SIZE = 50
MAX_U32 = 0xFFFFFFFF
WORLD_METADATA_KEY = b"\x00\x00\x00\x00\x00"
WORLD_SECTOR_SIZE = 32
STORE_TILE_SECTOR = 1
STORE_ENTITY_SECTOR = 2
STORE_UNIQUE_INDEX = 3
STORE_SECTOR_UNIQUES = 4
PAK_MAGIC = b"SBAsset6"
PAK_INDEX_MAGIC = b"INDEX"


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
    if stream.read(1):
        raise WorldFormatError("Unexpected trailing data in WorldMetadata record")
    return {
        "format": {"name": name, "version": version},
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
    raw = io.BytesIO()
    raw.write(struct.pack(">ii", int(width), int(height)))
    write_versioned_json(
        raw,
        str(format_info.get("name", "WorldMetadata")),
        format_info.get("version"),
        body,
    )
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
            if actual != expected:
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
    if not 1 <= index <= len(biomes):
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
        expected_size = 3 + 1024 * 31
        if len(raw) != expected_size:
            raise WorldFormatError(
                f"Tile region {key.hex()} has {len(raw)} bytes, expected {expected_size}"
            )
        region_changed = False
        for tile_index in range(1024):
            offset = 3 + tile_index * 31
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


def _sector_intersects_x_ranges(
    sector_x: int, ranges: Sequence[tuple[int, int]], world_width: int
) -> bool:
    sector_min = sector_x * WORLD_SECTOR_SIZE
    sector_max = min(world_width - 1, sector_min + WORLD_SECTOR_SIZE - 1)
    return any(sector_min <= end and sector_max >= start for start, end in ranges)


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
    expected_size = 3 + WORLD_SECTOR_SIZE * WORLD_SECTOR_SIZE * 31
    if len(raw) != expected_size:
        raise WorldFormatError(
            f"Tile sector {key.hex()} has {len(raw)} bytes, expected {expected_size}"
        )
    for local_y in range(WORLD_SECTOR_SIZE):
        world_y = sector_y_min + local_y
        if y_start is not None and y_end is not None and not y_start <= world_y <= y_end:
            continue
        for local_x in range(WORLD_SECTOR_SIZE):
            world_x = sector_min + local_x
            if world_x >= world_width or not any(start <= world_x <= end for start, end in ranges):
                continue
            tile_index = local_y * WORLD_SECTOR_SIZE + local_x
            offset = 3 + tile_index * 31
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

    if not selected:
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
    ) -> tuple[dict[str, Any], list[str]]:
        try:
            raw = self.catalog["biomes"][biome_name]
            biome_path = self.catalog["paths"][biome_name]
        except KeyError as exc:
            raise ValueError(f"所选 assets 中不存在 biome：{biome_name}") from exc
        rng = random.Random(seed)
        options = raw.get("hueShiftOptions", [])
        hue_shift = float(rng.choice(options)) if isinstance(options, list) and options else 0.0
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
        )
        compiled = {
            "baseName": biome_name,
            "description": str(raw.get("description", "")),
            "mainBlock": main_block,
            "subBlocks": sub_blocks,
            "ores": self._ores(raw.get("ores", []), threat_level),
            "hueShift": hue_shift,
            "materialHueShift": _material_hue_from_degrees(hue_shift),
            "surfacePlaceables": surface,
            "undergroundPlaceables": underground,
            "spawnProfile": self._spawn_profile(raw.get("spawnProfile", {}), rng),
            "parallax": parallax,
            "ambientNoises": json.loads(json.dumps(raw.get("ambientNoises"))),
            "musicTrack": json.loads(json.dumps(raw.get("musicTrack"))),
        }
        return compiled, surface_warnings + underground_warnings


def _overlay_layer_biome(
    layer: dict[str, Any],
    world_width: int,
    x_ranges: Sequence[tuple[int, int]],
    target_biome_index: int | dict[int, int],
    sub_block_counts: dict[int, int] | None = None,
    fallback_sub_block_selector: int | None = None,
    replacement_cell: dict[str, Any] | None = None,
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
                old_index = int(segment_cell.get("blockBiomeIndex", 0))
                if isinstance(target_biome_index, dict):
                    new_index = target_biome_index.get(old_index)
                    if new_index is None:
                        new_index = next(iter(target_biome_index.values()))
                else:
                    new_index = target_biome_index
                segment_cell["blockBiomeIndex"] = new_index
                segment_cell["environmentBiomeIndex"] = new_index
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


def _build_asset_region_cell(
    document: dict[str, Any],
    compiler: AssetBiomeCompiler,
    region_data: dict[str, Any],
    biome_name: str,
    base_height: float,
    ocean_level_override: int | None,
    seed: int,
) -> tuple[dict[str, Any], int, list[str]]:
    """Compile one complete biome and its fresh terrain/liquid region cell."""

    width = int(document["size"][0])
    parameters = world_template(document).get("worldParameters", {})
    threat_level = (
        float(parameters.get("threatLevel", 1.0))
        if isinstance(parameters, dict)
        else 1.0
    )
    compiled, warnings = compiler.compile(
        biome_name,
        _stable_asset_seed(seed, biome_name, "biome"),
        float(base_height),
        {},
        threat_level,
    )
    biomes = compiled_biomes(document)
    try:
        biome_index = biomes.index(compiled) + 1
    except ValueError:
        biomes.append(compiled)
        biome_index = len(biomes)

    recipe_rng = random.Random(_stable_asset_seed(seed, biome_name, "region"))
    recipe = compiler.region_recipe(biome_name, recipe_rng)

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


def set_asset_biome_rectangle(
    document: dict[str, Any],
    assets_folder: Path,
    biome_name: str,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
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
    start = int(y_start)
    end = int(y_end)
    span = max(1, end - start)
    ocean_companions = {
        "oceanfloor": "ocean",
        "toxicoceanfloor": "toxic",
        "arcticoceanfloor": "arctic",
        "magmaoceanfloor": "magma",
    }
    companion = ocean_companions.get(biome_name)
    bands: list[tuple[int, int, str, float, int | None]]
    transition: int | None = None
    if companion and companion in compiler.catalog["biomes"] and span >= 2:
        # Vanilla ocean proportions: floor min/base/surface-min/surface-base are
        # 800/850/1050/1150, i.e. 1/7 floor rise and a 5/7 transition.
        floor_base = float(start + max(1, round(span / 7.0)))
        transition = min(end, max(start + 1, start + round(span * 5.0 / 7.0)))
        ocean_base = float(end)
        _split_region_layer_at(layers, height, transition)
        bands = [
            (start, transition - 1, biome_name, floor_base, end),
            (transition, end, companion, ocean_base, end),
        ]
    else:
        original_base = _terrestrial_layer_base_height(
            document, start + span // 2
        )
        base = min(float(end), max(float(start), float(original_base)))
        bands = [(start, end, biome_name, base, None)]

    band_cells: list[tuple[int, int, dict[str, Any]]] = []
    for band_start, band_end, band_biome, base_height, ocean_level in bands:
        seed = _stable_asset_seed(
            world_seed,
            biome_name,
            band_biome,
            band_start,
            band_end,
            x_start,
            x_end,
        )
        cell, biome_index, compile_warnings = _build_asset_region_cell(
            document,
            compiler,
            region_data,
            band_biome,
            base_height,
            ocean_level,
            seed,
        )
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

    return {
        "targetBiomeIndexes": sorted(applied_indexes),
        "appliedBiomeIndexes": sorted(applied_indexes),
        "addedBiomeIndexes": sorted(applied_indexes),
        "changedLayers": changed_layers,
        "regionBlending": region_data.get("regionBlending"),
        "assetCompileWarnings": sorted(warnings),
        "fullRegionRecipe": True,
        "oceanCompanionBiome": companion if transition is not None else None,
        "oceanFloorTransitionY": transition,
    }


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
    compiled_layers = terrain.get("compiledLayers")
    selectors = terrain.get("terrainSelectors")
    if not isinstance(compiled_layers, list) or not isinstance(selectors, list):
        raise ValueError("editable.terrain 必须包含 compiledLayers 和 terrainSelectors 数组")
    last_y = -1
    for layer_number, layer in enumerate(compiled_layers, start=1):
        if not isinstance(layer, dict):
            raise ValueError(f"compiledLayers[{layer_number}] 不是对象")
        y_start = layer.get("yStart")
        boundaries = layer.get("boundaries")
        cells = layer.get("cells")
        if not isinstance(y_start, int) or not 0 <= y_start <= height:
            raise ValueError(f"compiledLayers[{layer_number}].yStart 超出世界高度")
        if y_start < last_y:
            raise ValueError("compiledLayers 的 yStart 必须从小到大")
        last_y = y_start
        if not isinstance(boundaries, list) or not all(
            isinstance(value, int) and 0 <= value < width for value in boundaries
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
                if not isinstance(index, int) or not 1 <= index <= biome_count:
                    raise ValueError(
                        f"layer {layer_number} cell {cell_number} 的 {key} 无效"
                    )
            selector_index = cell.get("terrainSelectorIndex")
            if not isinstance(selector_index, int) or not 0 <= selector_index < len(selectors):
                raise ValueError(
                    f"layer {layer_number} cell {cell_number} 的 terrainSelectorIndex 无效"
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


def validate_biome_parameters(parameters: dict[str, Any], label: str) -> None:
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

    document = deep_copy_json(source_document)
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
    normalized_weather = parse_weather_specs(weather_specs)

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
    rebuilt_biomes = deep_copy_json(compiled_biomes(document))
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
        validate_biome_parameters(parameters, f"biomes[{group_number}]")
        for index in indexes:
            original = rebuilt_biomes[index - 1]
            for parameter_key in BIOME_EDITABLE_KEYS:
                if parameter_key in parameters:
                    original[parameter_key] = deep_copy_json(parameters[parameter_key])
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
        if weight < 0:
            raise ValueError("Weather weights cannot be negative")
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
