"""Create fresh native worlds in a private server, then finish ocean pairings.

The private protocol-747 client is deliberately not a game client: it does not
send a player entity, read player saves, render a window, or access the real
universe.  Native world initialization is nevertheless required here because
loading an empty World4 skips the engine's first-time major-dungeon pass.
"""

from __future__ import annotations

import io
import collections
import copy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Sequence
import uuid
import zlib

import starbound_world_editor as swe
import world_edit_guard


class NativeGenerationError(RuntimeError):
    """Native generation failed; caller must retain the original universe."""


_NATIVE_DEFAULT_PACKET_BYTES = 64 * 1024 * 1024
_NATIVE_FALLBACK_PACKET_BYTES = 256 * 1024 * 1024
_NATIVE_INPUT_CHUNK_BYTES = 64 * 1024
_NATIVE_INFLATE_CHUNK_BYTES = 1024 * 1024
_NATIVE_MEMORY_HEADROOM_FACTOR = 8


def _checked_native_packet_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= sys.maxsize // 8:
        raise ValueError("Native packet limit must be a positive integer within this process's address space")
    return value


def _native_wire_limit(decoded_limit: int) -> int:
    # zlib can slightly expand incompressible data. Bound the wire allocation
    # separately without rejecting a legal level-0 frame at the decoded limit.
    return decoded_limit + decoded_limit // 1000 + 64


def _bounded_native_zlib_chunks(
    chunks: Iterable[bytes], limit: int, *, description: str,
    tick: Callable[[], None] | None = None,
) -> Iterator[bytes]:
    """Decode exactly one complete zlib member, with bounded output per call.

    ``flush(length)`` is not an output limit, so never use it on network data.
    Drain unconsumed input (and any internally buffered output) through capped
    decompress calls instead. The one-byte overflow probe is also bounded.
    """
    _checked_native_packet_limit(limit)
    inflater = zlib.decompressobj()
    total = 0
    try:
        for incoming in chunks:
            if not incoming:
                continue
            if inflater.eof:
                raise NativeGenerationError(f"Invalid {description}: trailing data after zlib stream")
            pending = incoming
            while True:
                if tick:
                    tick()
                allowance = min(_NATIVE_INFLATE_CHUNK_BYTES, limit - total + 1)
                decoded = inflater.decompress(pending, allowance)
                pending = inflater.unconsumed_tail
                total += len(decoded)
                if total > limit:
                    raise NativeGenerationError(
                        f"{description} exceeds size limit ({limit} decoded bytes)"
                    )
                if inflater.unused_data:
                    raise NativeGenerationError(f"Invalid {description}: trailing data after zlib stream")
                if decoded:
                    yield decoded
                if inflater.eof:
                    break
                if pending:
                    continue
                if len(decoded) < allowance:
                    break
                # An output cap can leave bytes buffered inside zlib even if
                # the current input chunk was consumed completely.
                pending = b""
    except zlib.error as exc:
        raise NativeGenerationError(f"Invalid {description}: malformed zlib stream ({exc})") from exc
    if not inflater.eof:
        raise NativeGenerationError(f"Invalid {description}: truncated zlib stream")


def _native_available_memory_bytes() -> int | None:
    """Best-effort current physical/commit/address headroom; never reserve RAM."""
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
                    (name, ctypes.c_uint64) for name in (
                        "total_phys", "available_phys", "total_commit", "available_commit",
                        "total_virtual", "available_virtual", "extended_virtual",
                    )
                ]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            query = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
            query.argtypes = [ctypes.POINTER(MemoryStatus)]
            query.restype = ctypes.c_int
            if query(ctypes.byref(status)):
                return min(status.available_phys, status.available_commit, status.available_virtual)
            return None
        pages, page_size = os.sysconf("SC_AVPHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size) if pages >= 0 and page_size > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def _native_world_metadata_size(world: Path, limit: int) -> int:
    """Count this private World's metadata without expanding it into an object.

    The all-zero metadata key is the first key in a valid World4 B-tree. Follow
    only the leftmost index path, then stream only that record. In particular,
    BTreeDB5.get currently materializes the entire containing leaf; do not use
    it here or allocate/decode all the world's tile records to size one packet.
    """
    with world.open("rb") as stream:
        database = swe.BTreeDB5(stream)
        if database.identifier != swe.WORLD_IDENTIFIER or database.key_size != 5:
            raise NativeGenerationError(f"Invalid private native world metadata source: {world.name}")
        block, is_leaf = database.root_block, database.root_is_leaf
        seen: set[int] = set()
        while not is_leaf:
            if block in seen:
                raise NativeGenerationError("Invalid private world metadata index: B-tree cycle")
            seen.add(block)
            stream.seek(database.block_offset(block))
            if swe._read_exact(stream, 2) != b"II":
                raise NativeGenerationError("Invalid private world metadata index")
            height = swe._read_exact(stream, 1)[0]
            _count, block = struct.unpack(">II", swe._read_exact(stream, 8))
            is_leaf = height == 0
        reader = swe.LeafStream(database, block)
        count = struct.unpack(">I", reader.read(4))[0]
        if not count or reader.read(database.key_size) != swe.WORLD_METADATA_KEY:
            raise NativeGenerationError(f"Private native world metadata record is missing: {world.name}")
        remaining = swe.read_varuint(reader)
        if remaining > _native_wire_limit(limit):
            raise NativeGenerationError(f"Private native world metadata exceeds memory budget ({limit} bytes)")

        def chunks():
            nonlocal remaining
            while remaining:
                size = min(remaining, _NATIVE_INPUT_CHUNK_BYTES)
                yield reader.read(size)
                remaining -= size

        return sum(len(chunk) for chunk in _bounded_native_zlib_chunks(
            chunks(), limit, description="Private native world metadata",
        ))


def _native_parameter_size(parameters: dict[str, Any], limit: int) -> int:
    class Counter:
        size = 0

        def write(self, data):
            self.size += len(data)
            if self.size > limit:
                raise NativeGenerationError(f"Native world parameters exceed memory budget ({limit} bytes)")
            return len(data)

    counter = Counter()
    swe.write_dynamic(counter, parameters)
    return counter.size


def _native_packet_budget(
    universe: Path, targets: Sequence[tuple[dict[str, Any], tuple[int, ...], Path]], *,
    max_packet_bytes: int | None = None,
) -> dict[str, Any]:
    """Choose a finite per-frame budget from local input and current headroom.

    Existing worlds grow with their actual expanded metadata, not their often
    tiny compressed size. A new world has no compiled metadata yet: allow the
    memory-bounded ceiling rather than assuming a 64/512 MiB template maximum.
    The memory snapshot is a conservative ceiling, not an allocation guarantee;
    parsing SBON/native assets also needs memory and other processes may grow.
    """
    if max_packet_bytes is not None:
        _checked_native_packet_limit(max_packet_bytes)
    available = _native_available_memory_bytes()
    if available is None:
        ceiling = max_packet_bytes or _NATIVE_FALLBACK_PACKET_BYTES
        memory_source = "explicit-limit" if max_packet_bytes is not None else "memory-query-unavailable-fallback"
    else:
        ceiling = available // _NATIVE_MEMORY_HEADROOM_FACTOR
        memory_source = "current-available-memory"
        if max_packet_bytes is not None:
            ceiling = min(ceiling, max_packet_bytes)
    ceiling = min(ceiling, sys.maxsize // 8)
    if ceiling < 1:
        raise NativeGenerationError("Insufficient available memory for a bounded native packet")
    largest = 0
    missing_worlds = 0
    inputs = []
    for parameters, coordinate, _output in targets:
        parameter_size = _native_parameter_size(parameters, ceiling)
        world = universe / _world_filename(coordinate)
        metadata_size = None
        if world.is_file():
            metadata_size = _native_world_metadata_size(world, ceiling)
        else:
            missing_worlds += 1
        largest = max(largest, parameter_size, metadata_size or 0)
        inputs.append({"world": world.name, "parameterSbonBytes": parameter_size,
                       "expandedMetadataBytes": metadata_size})
    requested = max(_NATIVE_DEFAULT_PACKET_BYTES, 2 * largest + 16 * 1024 * 1024)
    limit = ceiling if missing_worlds else min(requested, ceiling)
    return {"maxPacketBytes": limit, "maxCompressedWireBytes": _native_wire_limit(limit),
            "inputDerivedBytes": requested, "memoryCeilingBytes": ceiling,
            "availableMemoryBytes": available, "memoryBudgetSource": memory_source,
            "memoryHeadroomFactor": _NATIVE_MEMORY_HEADROOM_FACTOR,
            "configuredMaxPacketBytes": max_packet_bytes, "inputs": inputs,
            "missingWorldCount": missing_worlds,
            "limitBasis": "memory-bounded-new-world" if missing_worlds else "expanded-input-metadata",
            "protocol": "747-legacy-zlib", "inflateChunkBytes": _NATIVE_INFLATE_CHUNK_BYTES}


def _native_fatal_log_detail(diagnostic: Path) -> dict[str, str]:
    """Show a native fatal cause, not only its opaque Windows process code.

    Ordinary placeholder warnings are deliberately excluded: a tolerated image
    warning must not be mislabeled as the cause of a later unrelated timeout.
    """
    for phase in ("verify", "create"):
        path = Path(diagnostic) / (phase + "-server-console.log")
        try:
            with path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                source.seek(max(0, source.tell() - 256 * 1024))
                tail = source.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = [line.strip() for line in tail.splitlines() if (
            "Fatal Exception caught:" in line or "Fatal Error:" in line
        )]
        if lines:
            return {"nativeFatalError": lines[-1][:1600], "nativeFatalLog": str(path)}
    return {}


def resolve_server_executable(chunks_path: Path, assets: Path) -> Path:
    """Prefer the selected universe's engine, including OpenSB's ``win`` layout.

    A client-only OpenStarbound distribution must not silently fall through to
    an unrelated vanilla server. That server may not understand its assets or
    generate the same world, even though both engines use protocol 747.
    """
    roots = [Path(chunks_path).resolve().parent.parent.parent, Path(assets).resolve().parent]
    for root in roots:
        opensb_win_client = (root / "win" / "starbound.exe").is_file()
        # A vanilla win64 folder can coexist with OpenSB's win folder. Once
        # the latter identifies the selected runtime, do not use that sibling
        # as an implicit replacement for its missing dedicated server.
        directories = ("win",) if opensb_win_client else ("win", "win64", "win32", "linux", "osx")
        for directory in directories:
            for name in ("starbound_server.exe", "starbound_server"):
                candidate = root / directory / name
                if candidate.is_file():
                    return candidate
        if (root / "assets" / "opensb.pak").is_file() or opensb_win_client:
            raise NativeGenerationError(
                f"This OpenStarbound installation has no dedicated server: {root}. "
                "Native world generation requires a matching OpenStarbound "
                "starbound_server.exe (normally in win), with the same assets/mods. "
                "The client-only starbound.exe cannot be used as a private server, "
                "and an unrelated vanilla server will not be substituted. "
                "Offline universe reading and editing do not require this server."
            )
    raise NativeGenerationError(
        "Native world generation requires this game's dedicated starbound_server "
        "executable (win for OpenStarbound, win64/win32 for vanilla Windows). "
        "No server executable was found beside the selected storage/assets."
    )


def _coordinate(parameters: dict[str, Any]) -> tuple[int, int, int, int, int]:
    value = parameters.get("coordinate")
    if not isinstance(value, dict):
        raise ValueError("The generation job must contain a structured celestial coordinate")
    location = value.get("location")
    if not isinstance(location, list) or len(location) != 3:
        raise ValueError("Celestial coordinate must have three location components")
    components = [*location, value.get("planet", 0), value.get("satellite", 0)]
    if any(isinstance(v, bool) or not isinstance(v, int) or not -(2 ** 31) <= v < 2 ** 31 for v in components):
        raise ValueError("Celestial coordinates must contain signed 32-bit integers")
    if components[3] < 1 or components[4] < 0:
        raise ValueError("A generated body needs a positive planet orbit and nonnegative moon orbit")
    return tuple(components)  # type: ignore[return-value]


def _world_filename(coordinate: Sequence[int]) -> str:
    parts = list(coordinate)
    if parts[-1] == 0:
        parts.pop()
    return "_".join(map(str, parts)) + ".world"


def _frame(packet_type: int, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    buffer.write(bytes([packet_type]))
    swe.write_varint(buffer, len(payload))
    buffer.write(payload)
    return buffer.getvalue()


def _connect_payload(player_uuid: uuid.UUID, account: str = "") -> bytes:
    output = io.BytesIO()
    swe.write_bytes(output, b"")  # Local private server explicitly permits asset mismatch.
    output.write(b"\x01")
    output.write(player_uuid.bytes)
    swe.write_string(output, "Tool05 generation")
    swe.write_string(output, "human")
    swe.write_varuint(output, 0)  # Empty ship chunks: engine supplies a stock ship if needed.
    output.write(struct.pack(">IIIff", 0, 0, 0, 1.0, 0.0))
    swe.write_varuint(output, 0)  # Ship capabilities StringSet.
    output.write(b"\x01")  # Intro already complete.
    swe.write_string(output, account)
    return output.getvalue()


def _warp_payload(coordinate: Sequence[int]) -> bytes:
    # MVariant indices: WarpToWorld = 1, CelestialWorldId = 1, null SpawnTarget = 0.
    return b"\x01\x01" + struct.pack(">iiiii", *coordinate) + b"\x00\x00"


def _viewport_windows(windows: Sequence[Sequence[int]], world_size: Sequence[int] | None = None) -> list[tuple[int, int, int, int]]:
    """Validate bounded, non-wrapping native interest rectangles."""
    result: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    if len(windows) > 10000:
        raise ValueError("Too many native generation viewports")
    for window in windows:
        if (not isinstance(window, (tuple, list)) or len(window) != 4
                or any(isinstance(v, bool) or not isinstance(v, int) for v in window)):
            raise ValueError("Native viewport must contain four integers: X, Y, width, height")
        x, y, width, height = window
        if x < 0 or y < 0 or not 1 <= width <= 1024 or not 1 <= height <= 1024:
            raise ValueError("Native viewport requires non-negative coordinates and dimensions from 1 to 1024")
        if x + width > 0x7FFFFFFF or y + height > 0x7FFFFFFF:
            raise ValueError("Native viewport exceeds engine coordinate limits")
        if world_size is not None and (x + width > world_size[0] or y + height > world_size[1]):
            raise ValueError("Native viewport exceeds world bounds; split wrapping rectangles first")
        item = (x, y, width, height)
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _viewport_sector_set(window: Sequence[int]) -> set[tuple[int, int]]:
    x, y, width, height = window
    return {(sx, sy) for sx in range(x // 32, (x + width - 1) // 32 + 1)
            for sy in range(y // 32, (y + height - 1) // 32 + 1)}


def native_viewport_timeout_budget(
    windows: Sequence[Sequence[int]], *, phase: str,
    minimum_seconds: float = 300, maximum_seconds: float = 14400,
) -> dict[str, Any]:
    """A finite allowance for ONE private terrain or plant-finalize pass.

    This is a conservative timeout ceiling, not a promised duration/ETA. A
    measured 13,935-sector coast needs about 49 minutes for normal terrain
    alone, so sharing the old one-hour deadline with plant completion fails
    healthy work. Charge unique sectors, overlap and window activation, then
    impose an absolute four-hour cap PER phase. Never extend a running pass's
    caller-supplied absolute deadline implicitly.
    """
    if phase not in ("terrain", "plants"):
        raise ValueError("Native viewport timeout phase must be terrain or plants")
    for name, value in (("minimum", minimum_seconds), ("maximum", maximum_seconds)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= 14400):
            raise ValueError(f"Native viewport {name} timeout must be a finite number within 0..14400 seconds")
    if minimum_seconds > maximum_seconds:
        raise ValueError("Native viewport minimum timeout exceeds its maximum")
    checked = _viewport_windows(windows)
    unique: set[tuple[int, int]] = set()
    requested = 0
    for window in checked:
        sectors = _viewport_sector_set(window)
        requested += len(sectors)
        unique.update(sectors)
    # Native timings differ by CPU, assets and dungeon complexity; these
    # coefficients allow roughly 2-3x the observed large-coast throughput.
    seconds_per_sector = 0.65 if phase == "terrain" else 0.40
    overlap = requested - len(unique)
    work_seconds = math.ceil(180 + len(unique) * seconds_per_sector
                             + overlap * 0.10 + len(checked) * 4)
    uncapped = max(float(minimum_seconds), work_seconds)
    budget = min(float(maximum_seconds), uncapped)
    return {"phase": phase, "timeoutSeconds": budget,
            "uncappedTimeoutSeconds": uncapped, "maximumSeconds": float(maximum_seconds),
            "minimumSeconds": float(minimum_seconds), "capped": uncapped > maximum_seconds,
            "uniqueSectorCount": len(unique), "requestedSectorVisits": requested,
            "overlapSectorVisits": overlap, "windowCount": len(checked),
            "secondsPerUniqueSector": seconds_per_sector,
            "secondsPerOverlapSector": 0.10, "startupAllowanceSeconds": 180,
            "secondsPerWindow": 4, "isTimeoutCeilingNotDurationEstimate": True,
            "scope": "one-private-native-phase; terrain and plants need independent deadlines"}


def _viewport_state_payload(window: Sequence[int]) -> bytes:
    # NetElementTop initial full state: five signed VLQs (window and null
    # player EntityId), then an empty presence EntityId list, in a ByteArray.
    state = io.BytesIO()
    state.write(b"\x01")
    for value in (*window, 0):
        swe.write_varint(state, value)
    swe.write_varuint(state, 0)
    output = io.BytesIO()
    swe.write_bytes(output, state.getvalue())
    return output.getvalue()


def _native_tile_array_rectangles(payload: bytes) -> list[tuple[int, int, int, int]]:
    """Read EVERY TileArrayUpdate in a same-type native packet frame.

    Frames may concatenate many packets with no extra lengths between them.
    NetTile is variable-length, unlike the on-disk ServerTile representation.
    """
    stream = io.BytesIO(payload)
    rectangles = []
    try:
        while stream.tell() < len(payload):
            rectangle = (swe.read_varint(stream), swe.read_varint(stream),
                         swe.read_varuint(stream), swe.read_varuint(stream))
            if not 0 < rectangle[2] <= 1024 or not 0 < rectangle[3] <= 1024:
                raise ValueError("invalid tile array dimensions")
            for _tile in range(rectangle[2] * rectangle[3]):
                for _layer in range(2):
                    material = struct.unpack(">H", swe._read_exact(stream, 2))[0]
                    if material:
                        swe._read_exact(stream, 2)  # Material hue and colour.
                        mod = struct.unpack(">H", swe._read_exact(stream, 2))[0]
                        if mod:
                            swe._read_exact(stream, 1)
                swe._read_exact(stream, 3)  # Collision and two biome indexes.
                if swe._read_exact(stream, 1)[0]:
                    swe._read_exact(stream, 1)  # Quantized liquid level.
                swe.read_varuint(stream)  # DungeonId.
            rectangles.append(rectangle)
    except (ValueError, struct.error, swe.WorldFormatError) as exc:
        raise NativeGenerationError(f"Invalid native tile-array packet: {exc}") from exc
    return rectangles


def _native_entity_creates(payload: bytes) -> list[dict[str, Any]]:
    stream = io.BytesIO(payload)
    result = []
    try:
        while stream.tell() < len(payload):
            kind = swe._read_exact(stream, 1)[0]
            if kind > 9:
                raise ValueError("invalid vanilla entity type")
            store = swe.read_bytes(stream)
            swe.read_bytes(stream)  # Initial net state, not needed by this client.
            item = {"entityId": swe.read_varint(stream), "typeId": kind}
            if kind == 0:
                plant = io.BytesIO(store)
                item["tilePosition"] = [swe.read_varint(plant), swe.read_varint(plant)]
            result.append(item)
    except (ValueError, swe.WorldFormatError) as exc:
        raise NativeGenerationError(f"Invalid native entity-create packet: {exc}") from exc
    return result


class _NativeClient:
    """Bounded protocol-747 reader (vanilla and OpenSB legacy connections)."""

    def __init__(self, connection: socket.socket, *, max_packet_bytes: int = _NATIVE_DEFAULT_PACKET_BYTES):
        self.connection = connection
        self.buffer = bytearray()
        self.max_packet_bytes = _checked_native_packet_limit(max_packet_bytes)
        self.keepalive_enabled = False
        self.last_keepalive = time.monotonic()

    def send(self, packet_type: int, payload: bytes) -> None:
        self.connection.sendall(_frame(packet_type, payload))

    def _receive_tick(self, deadline: float) -> None:
        now = time.monotonic()
        if now >= deadline:
            raise NativeGenerationError("Timed out waiting for the native world generator")
        if self.keepalive_enabled and now - self.last_keepalive >= 2.0:
            self.send(49, b"\x00")
            self.last_keepalive = now

    def receive(self, deadline: float) -> tuple[int, bytes]:
        while time.monotonic() < deadline:
            self._receive_tick(deadline)
            if self.buffer:
                packet_type = self.buffer[0]
                encoded_size = 0
                end = None
                for offset, value in enumerate(self.buffer[1:11], 1):
                    encoded_size = (encoded_size << 7) | (value & 127)
                    if not value & 128:
                        end = offset + 1
                        break
                if end is not None:
                    length = -(encoded_size // 2) - 1 if encoded_size & 1 else encoded_size // 2
                    wire_limit = _native_wire_limit(self.max_packet_bytes) if length < 0 else self.max_packet_bytes
                    if abs(length) > wire_limit or length == 0:
                        raise NativeGenerationError(f"Invalid native packet length (limit {wire_limit} bytes)")
                    if len(self.buffer) >= end + abs(length):
                        with memoryview(self.buffer)[end:end + abs(length)] as frame:
                            if length < 0:
                                chunks = (frame[offset:offset + _NATIVE_INPUT_CHUNK_BYTES]
                                          for offset in range(0, len(frame), _NATIVE_INPUT_CHUNK_BYTES))
                                output = io.BytesIO()
                                for chunk in _bounded_native_zlib_chunks(
                                        chunks, self.max_packet_bytes, description="Native compressed packet",
                                        tick=lambda: self._receive_tick(deadline)):
                                    output.write(chunk)
                                payload = output.getvalue()
                            else:
                                payload = bytes(frame)
                        del self.buffer[:end + abs(length)]
                        return packet_type, payload
                elif len(self.buffer) >= 11:
                    raise NativeGenerationError("Invalid native packet header")
            self.connection.settimeout(min(1.0, max(0.01, deadline - time.monotonic())))
            try:
                data = self.connection.recv(65536)
            except socket.timeout:
                continue
            if not data:
                if self.buffer:
                    raise NativeGenerationError("Truncated native packet: private client disconnected before the frame completed")
                raise NativeGenerationError("Private generation client was disconnected")
            self.buffer.extend(data)
        raise NativeGenerationError("Timed out waiting for the native world generator")

    def connect(
        self, deadline: float, player_uuid: uuid.UUID | None = None,
        account: str = "", password: str = "",
    ) -> None:
        self.send(0, struct.pack(">I", 747))
        packet_type, payload = self.receive(deadline)
        if packet_type != 1 or not payload or payload[0] != 1:
            raise NativeGenerationError("The game server did not accept vanilla protocol 747")
        self.send(13, _connect_payload(player_uuid or uuid.uuid4(), account))
        challenge_seen = False
        while True:
            packet_type, payload = self.receive(deadline)
            if packet_type == 3:
                if account and not challenge_seen:
                    raise NativeGenerationError("Private game server accepted a connection without the required account challenge")
                self.keepalive_enabled = True
                self.last_keepalive = time.monotonic()
                return
            if packet_type == 5 and account:
                if challenge_seen:
                    raise NativeGenerationError("Private game server sent a repeated account challenge")
                challenge_seen = True
                salt = swe.read_bytes(io.BytesIO(payload))
                password_hash = hashlib.sha256((password + account).encode("utf-8") + salt).digest()
                response = io.BytesIO()
                swe.write_bytes(response, password_hash)
                self.send(15, response.getvalue())
                continue
            if packet_type in (2, 4, 5):
                try:
                    reason = swe.read_string(io.BytesIO(payload))
                except Exception:
                    reason = "private connection rejected"
                raise NativeGenerationError(f"Native connection failed: {reason}")

    def generate(self, coordinate: Sequence[int], deadline: float) -> dict[str, Any]:
        self.send(16, _warp_payload(coordinate))
        while True:
            packet_type, payload = self.receive(deadline)
            if packet_type == 21:  # WorldStart starts with an SBON WorldTemplate.
                template = swe.read_dynamic(io.BytesIO(payload))
                if isinstance(template, dict):
                    parameters = template.get("celestialParameters")
                    if isinstance(parameters, dict) and _coordinate(parameters) == tuple(coordinate):
                        self.send(48, b"\x00")
                        return template
            if packet_type == 9 and payload and not payload[0]:
                raise NativeGenerationError("The native server could not create or enter the requested world; see its log")
            if packet_type in (2, 4):
                raise NativeGenerationError("Native world initialization disconnected the private client; see its log")

    def generate_viewports(
        self, windows: Sequence[Sequence[int]], deadline: float, *,
        world_size: Sequence[int] | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Generate real sectors without a player save, GUI or fabricated flora.

        Completion requires native TileArrayUpdate coverage and another 120
        native simulation steps, never a short quiet period or a nonzero plant
        count. Native periodic rules and each compiled biome seed stay intact.
        """
        if not math.isfinite(deadline) or deadline <= time.monotonic():
            raise ValueError("Native viewport deadline must be finite and in the future")
        checked = _viewport_windows(windows, world_size)
        covered: set[tuple[int, int]] = set()
        wanted_all: set[tuple[int, int]] = set()
        plant_ids: set[int] = set()
        plant_examples: list[dict[str, Any]] = []
        frame_counts: collections.Counter[int] = collections.Counter()
        results: list[dict[str, Any]] = []

        def emit(event: str, **details: Any) -> None:
            if progress:
                try:
                    progress(event, details)
                except Exception:
                    pass

        remote_step: int | None = None
        for number, window in enumerate(checked):
            wanted = _viewport_sector_set(window)
            wanted_all.update(wanted)
            started = time.monotonic()
            last_progress = started
            coverage_seen = False
            completion_step: int | None = None
            self.send(46, _viewport_state_payload(window))
            emit("native_viewport_requested", windowIndex=number, windowCount=len(checked),
                 window=list(window), targetSectorCount=len(wanted),
                 deadlineRemainingSeconds=round(max(0, deadline - time.monotonic()), 3))
            while time.monotonic() < deadline:
                packet_type, payload = self.receive(deadline)
                frame_counts[packet_type] += 1
                if packet_type == 26:
                    for rectangle in _native_tile_array_rectangles(payload):
                        covered.update(_viewport_sector_set(rectangle))
                elif packet_type == 50:
                    for item in _native_entity_creates(payload):
                        if item["typeId"] == 0 and item["entityId"] not in plant_ids:
                            plant_ids.add(item["entityId"])
                            if len(plant_examples) < 32:
                                plant_examples.append(item)
                elif packet_type == 61:
                    stream = io.BytesIO(payload)
                    try:
                        while stream.tell() < len(payload):
                            step = swe.read_varuint(stream)
                            if remote_step is not None and step < remote_step:
                                raise NativeGenerationError("Native world simulation clock moved backwards during viewport generation")
                            remote_step = step
                    except swe.WorldFormatError as exc:
                        raise NativeGenerationError(f"Invalid native simulation-step packet: {exc}") from exc
                elif packet_type in (2, 4, 21, 22) or (packet_type == 9 and payload and not payload[0]):
                    raise NativeGenerationError(
                        f"Native world stopped or disconnected while generating a viewport (packet {packet_type}); see its log"
                    )
                if wanted <= covered:
                    if not coverage_seen:
                        coverage_seen = True
                        emit("native_viewport_tiles_received", windowIndex=number, window=list(window),
                             targetSectorCount=len(wanted), nativeStep=remote_step)
                    if completion_step is None and remote_step is not None:
                        completion_step = remote_step
                if completion_step is not None and remote_step is not None and remote_step - completion_step >= 120:
                    result = {"window": list(window), "coveredSectorCount": len(wanted),
                              "extraNativeSteps": remote_step - completion_step,
                              "elapsedSeconds": round(time.monotonic() - started, 3)}
                    results.append(result)
                    emit("native_viewport_completed", windowIndex=number, **result)
                    break
                if time.monotonic() - last_progress >= 10:
                    emit("native_viewport_progress", windowIndex=number, windowCount=len(checked),
                         window=list(window), coveredSectorCount=len(wanted & covered),
                         targetSectorCount=len(wanted), nativeStep=remote_step,
                         deadlineRemainingSeconds=round(max(0, deadline - time.monotonic()), 3))
                    last_progress = time.monotonic()
            else:
                raise NativeGenerationError("Native viewport generation exceeded its deadline")
        return {"windowCount": len(checked), "targetSectorCount": len(wanted_all),
                "coveredSectorCount": len(wanted_all & covered), "windows": results,
                "networkPlantIdsSeen": len(plant_ids), "plantExamples": plant_examples,
                "packetFrameCounts": dict(frame_counts), "completionExtraNativeSteps": 120,
                "floraSource": "unchanged-compiled-biome-distributions-and-seeds"}


class _RconCleanEof(NativeGenerationError):
    """Peer closed before the first byte of a new acknowledgement frame."""


class _Rcon:
    """Source RCON framing used solely to stop our own private server cleanly."""

    def __init__(self, connection: socket.socket, password: str):
        self.connection = connection
        self.connection.settimeout(10)
        self._send(1, 3, password)
        while True:
            request_id, packet_type, _text = self._receive()
            if request_id == -1:
                raise NativeGenerationError("Private server RCON authentication failed")
            if packet_type == 2:
                break

    def _send(self, request_id: int, packet_type: int, text: str) -> None:
        payload = struct.pack("<ii", request_id, packet_type) + text.encode("utf-8") + b"\0\0"
        self.connection.sendall(struct.pack("<i", len(payload)) + payload)

    def _read_exact(self, size: int, deadline: float | None = None, *, allow_clean_eof: bool = False) -> bytes:
        result = bytearray()
        while len(result) < size:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeGenerationError("Timed out waiting for private RCON shutdown acknowledgement")
                self.connection.settimeout(remaining)
            data = self.connection.recv(size - len(result))
            if not data:
                if allow_clean_eof and not result:
                    raise _RconCleanEof("Private RCON closed before shutdown acknowledgement")
                raise NativeGenerationError("Private RCON connection closed")
            result.extend(data)
        return bytes(result)

    def _receive(self, deadline: float | None = None, *, allow_clean_eof: bool = False) -> tuple[int, int, str]:
        length = struct.unpack("<i", self._read_exact(4, deadline, allow_clean_eof=allow_clean_eof))[0]
        if not 10 <= length <= 4 * 1024 * 1024:
            raise NativeGenerationError("Invalid RCON response size")
        payload = self._read_exact(length, deadline)
        request_id, packet_type = struct.unpack("<ii", payload[:8])
        if payload[-2:] != b"\0\0":
            raise NativeGenerationError("Invalid RCON response terminator")
        return request_id, packet_type, payload[8:-2].decode("utf-8", "replace")

    def stop(self) -> str | None:
        """Return an exact ACK, or clean EOF pending independent exit checks."""
        self.connection.settimeout(5)
        deadline = time.monotonic() + 5
        try:
            self._send(2, 2, "stop")
            request_id, packet_type, response = self._receive(deadline, allow_clean_eof=True)
        except _RconCleanEof:
            # Native shutdown may close its RCON server before sendCmdResponse.
            # This is NOT success by itself: the caller must require a clean
            # process exit and validate the generated world before publishing.
            return None
        except socket.timeout as exc:
            raise NativeGenerationError("Timed out waiting for private RCON shutdown acknowledgement") from exc
        if request_id != 2 or packet_type != 0 or response.strip() != "OK: shutting down":
            raise NativeGenerationError(
                "Private RCON did not acknowledge shutdown: "
                f"request={request_id}, type={packet_type}, response={response!r}"
            )
        return response


def _close_private_socket(connection: socket.socket | None) -> None:
    """Release our sockets before joining a native blocking receive thread."""
    if connection is None:
        return
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # An already-disconnected private socket still needs close().
    finally:
        try:
            connection.close()
        except OSError:
            pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_private_client_context(universe: Path, player_uuid: uuid.UUID, coordinate: Sequence[int]) -> None:
    """Start directly at the requested planet, without looking for a starter system."""
    location = {"location": list(coordinate[:3]), "planet": coordinate[3], "satellite": coordinate[4]}
    context = {
        "shipCoordinate": location,
        "systemLocation": ["coordinate", location],
        "isAdmin": False,
        "team": {"type": "friendly", "team": 0},
        "reviveWarp": {"world": "CelestialWorld:" + ":".join(map(str, coordinate)), "target": None},
        "returnWarp": None,
    }
    buffer = io.BytesIO()
    buffer.write(swe.VERSIONED_JSON_FILE_MAGIC)
    swe.write_versioned_json(buffer, "ClientContext", 6, context)
    (universe / (player_uuid.hex + ".clientcontext")).write_bytes(buffer.getvalue())


def _connect_when_ready(port: int, process: subprocess.Popen, deadline: float) -> socket.socket:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise NativeGenerationError(f"Private Starbound server exited with code {process.returncode}")
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1)
        except OSError:
            time.sleep(0.25)
    raise NativeGenerationError("Timed out while loading native game assets")


def _private_boot_config(server: Path, storage: Path, game_port: int, rcon_port: int, password: str) -> dict[str, Any]:
    source = server.parent / "sbinit.config"
    if not source.is_file():
        raise NativeGenerationError(f"The server's sbinit.config is missing: {source}")
    config = swe.parse_starbound_json(source.read_bytes(), str(source))
    if not isinstance(config, dict):
        raise NativeGenerationError("The server boot configuration is not an object")
    config = swe.deep_copy_json(config)
    # Match the normal game client's asset fallback policy. Native dedicated
    # server defaults omit these placeholders, so otherwise a pre-existing
    # missing image/audio tolerated by the game can prevent world generation.
    # Root reads this top-level setting, not defaultConfiguration. Keep explicit
    # custom paths (and null, which intentionally disables a fallback) intact.
    assets_settings = config.setdefault("assetsSettings", {})
    if not isinstance(assets_settings, dict):
        raise NativeGenerationError("Invalid assetsSettings in server boot configuration: expected an object")
    assets_settings.setdefault("missingImage", "/assetmissing.png")
    assets_settings.setdefault("missingAudio", "/assetmissing.wav")
    for key in ("assetDirectories", "assetSources"):
        paths = config.get(key, [])
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise NativeGenerationError(f"Invalid {key} in server boot configuration")
        config[key] = [str((server.parent / path).resolve()) for path in paths]
    defaults = config.setdefault("defaultConfiguration", {})
    if not isinstance(defaults, dict):
        raise NativeGenerationError("Invalid defaultConfiguration in server boot configuration: expected an object")
    connection_settings = defaults.setdefault("connectionSettings", {})
    if not isinstance(connection_settings, dict):
        raise NativeGenerationError("Invalid connectionSettings in server boot configuration: expected an object")
    # An uncompressed protocol-747 request selects OpenSB's vanilla-compatible
    # connection rules. Permit that client in THIS private server even when the
    # real installation requires the latest OpenSB client. The legacy packet
    # stream remains zlib-framed; no OpenSB/Zstd negotiation is required.
    connection_settings.update({"requireLatestVersion": False, "compression": "None"})
    defaults.update({
        "gameServerBind": "127.0.0.1", "gameServerPort": game_port,
        "runQueryServer": False, "queryServerBind": "127.0.0.1",
        "runRconServer": True, "rconServerBind": "127.0.0.1", "rconServerPort": rcon_port,
        "rconServerPassword": password, "rconServerTimeout": 10000,
        "allowAssetsMismatch": True, "allowAnonymousConnections": False,
        "serverUsers": {}, "serverPasswords": [], "maxPlayers": 1,
        "checkAssetsDigest": False,
    })
    config.update({"storageDirectory": str(storage), "logDirectory": str(storage), "includeUGC": False})
    return config


def _values_match(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _values_match(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_values_match(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, int) and not isinstance(expected, bool) and isinstance(actual, int) and not isinstance(actual, bool):
        return actual == expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-5)
    return actual == expected


# StarColor.cpp NamedColors, not the CSS palette (native "red", for example,
# is 255,73,66). Keep the spelling set aligned with validate_native_color.
_NATIVE_NAMED_COLOR_RGBA = {
    "red": (255, 73, 66, 255), "orange": (255, 180, 47, 255),
    "yellow": (255, 239, 30, 255), "green": (79, 230, 70, 255),
    "blue": (38, 96, 255, 255), "indigo": (75, 0, 130, 255),
    "violet": (160, 119, 255, 255), "black": (0, 0, 0, 255),
    "white": (255, 255, 255, 255), "magenta": (221, 92, 249, 255),
    "darkmagenta": (142, 33, 144, 255), "cyan": (0, 220, 233, 255),
    "darkcyan": (0, 137, 165, 255), "cornflowerblue": (100, 149, 237, 255),
    "gray": (160, 160, 160, 255), "lightgray": (192, 192, 192, 255),
    "darkgray": (128, 128, 128, 255), "darkgreen": (0, 128, 0, 255),
    "pink": (255, 162, 187, 255), "clear": (0, 0, 0, 0),
}


def _native_sky_value_comparison_form(key: str, value: Any) -> Any:
    """Exact native RGBA bytes for one of SkyColoring's nine known fields.

    StarJsonExtra's jsonToColor accepts names, hex, and RGB/RGBA arrays;
    jsonFromColor writes byte arrays and omits an opaque alpha. All 256 byte
    inputs survive Color's float32 /255 then *255 round trip, including the
    alpha accessor's truncation, so no numeric tolerance is needed here.
    Keep transparent RGB bytes as well: equal alpha alone is not equality.
    Validate before converting, never silently wrap/truncate invalid inputs.
    """
    from world_parameter_validation import SKY_GRADIENT_KEYS, validate_sky_value

    def rgba(color: Any) -> tuple[int, ...]:
        if isinstance(color, str):
            if not color.startswith("#"):
                try:
                    return _NATIVE_NAMED_COLOR_RGBA[color.casefold()]
                except KeyError as exc:
                    raise ValueError(f"Unsupported native color name: {color}") from exc
            digits = color[1:]
            if len(digits) in (3, 4):
                digits = "".join(digit * 2 for digit in digits)
            components = tuple(int(digits[index:index + 2], 16) for index in range(0, len(digits), 2))
        else:
            components = tuple(color)
        return components + (255,) if len(components) == 3 else components

    normalized = validate_sky_value(key, value)
    if key in SKY_GRADIENT_KEYS:
        return tuple(rgba(color) for color in normalized)
    return rgba(normalized)


def _native_world_parameters_match(actual: Any, expected: Any) -> bool:
    """Only allow native color representation changes at the real sky path.

    Keep the existing expected-key/subset contract and numeric comparison for
    every other world parameter and unknown sky extension. Do not recursively
    treat arbitrary strings, RGB-shaped arrays, or namesakes as colors.
    """
    from world_parameter_validation import SKY_COLOR_KEYS

    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key not in actual:
            return False
        if key != "skyColoring" or not isinstance(value, dict):
            if not _values_match(actual[key], value):
                return False
            continue
        sky = actual[key]
        if not isinstance(sky, dict):
            return False
        for sky_key, color in value.items():
            if sky_key not in sky:
                return False
            if sky_key not in SKY_COLOR_KEYS:
                if not _values_match(sky[sky_key], color):
                    return False
                continue
            try:
                if (_native_sky_value_comparison_form(sky_key, sky[sky_key])
                        != _native_sky_value_comparison_form(sky_key, color)):
                    return False
            except (TypeError, ValueError):
                return False
    return True


def _native_layout_comparison_form(layout: Any) -> tuple[Any, list[str]]:
    """Normalize only fields whose native storage order is not significant.

    SpawnProfile and MicroDungeonNames use StringSet. Parallax sorts its layers
    stably by descending zLevel when loaded. Terrain layers, region cells,
    selectors, placement distributions, weighted items and all other arrays
    remain ordered. Never mutate the world being validated or installed.
    """
    # JSON copying canonicalizes NaN payloads. Keep the exact native bit
    # patterns so the narrowly scoped unused-noise equality check can verify
    # that even those inherited fields were not changed.
    result = copy.deepcopy(layout)
    changes: list[str] = []

    def string_set(owner: Any, key: Any, path: str) -> None:
        value = owner[key]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            normalized = sorted(set(value))
            if normalized != value:
                changes.append(path)
            owner[key] = normalized

    def biome_item(item: Any, path: str) -> None:
        if isinstance(item, list) and len(item) == 2 and item[0] == "microDungeon":
            string_set(item, 1, path + "/1")

    biomes = result.get("biomes") if isinstance(result, dict) else None
    if not isinstance(biomes, list):
        return result, changes
    for index, biome in enumerate(biomes):
        if not isinstance(biome, dict):
            continue
        base = f"/biomes/{index}"
        profile = biome.get("spawnProfile")
        if isinstance(profile, dict) and "spawnTypes" in profile:
            string_set(profile, "spawnTypes", base + "/spawnProfile/spawnTypes")
        for field in ("surfacePlaceables", "undergroundPlaceables"):
            placeables = biome.get(field)
            distributions = placeables.get("itemDistributions") if isinstance(placeables, dict) else None
            if not isinstance(distributions, list):
                continue
            for distribution_index, distribution in enumerate(distributions):
                if not isinstance(distribution, dict):
                    continue
                path = f"{base}/{field}/itemDistributions/{distribution_index}"
                random_items = distribution.get("randomItems")
                if isinstance(random_items, list):
                    for item_index, item in enumerate(random_items):
                        biome_item(item, f"{path}/randomItems/{item_index}")
                weighted_items = distribution.get("weightedItems")
                if isinstance(weighted_items, list):
                    for item_index, item in enumerate(weighted_items):
                        if isinstance(item, list) and len(item) == 2:
                            biome_item(item[0], f"{path}/weightedItems/{item_index}/0")
        parallax = biome.get("parallax")
        layers = parallax.get("layers") if isinstance(parallax, dict) else None
        if isinstance(layers, list) and all(
            isinstance(layer, dict) and isinstance(layer.get("zLevel"), (int, float))
            and not isinstance(layer["zLevel"], bool) and math.isfinite(layer["zLevel"])
            for layer in layers
        ):
            ordered = sorted(layers, key=lambda layer: layer["zLevel"], reverse=True)
            if ordered != layers:
                changes.append(base + "/parallax/layers")
            parallax["layers"] = ordered
    return result, changes


def _native_unused_nan_fields(layout: Any) -> dict[str, bytes]:
    """Only random placement's unused, explicitly uninitialized Perlin data.

    BiomeItemDistribution::itemToPlace never reads either noise for `random`.
    Older native saves nevertheless serialize their uninitialized float
    members, sometimes including NaNs. Do not extend this to periodic noise,
    terrain selectors, probabilities, seeds, or user-editable active fields.
    """
    result: dict[str, bytes] = {}
    biomes = layout.get("biomes") if isinstance(layout, dict) else None
    if not isinstance(biomes, list):
        return result
    for index, biome in enumerate(biomes):
        if not isinstance(biome, dict):
            continue
        for field in ("surfacePlaceables", "undergroundPlaceables"):
            placeables = biome.get(field)
            distributions = placeables.get("itemDistributions") if isinstance(placeables, dict) else None
            if not isinstance(distributions, list):
                continue
            for dist_index, distribution in enumerate(distributions):
                if not isinstance(distribution, dict) or distribution.get("distribution") != "random":
                    continue
                for noise_key in ("densityFunction", "modulusDistortion"):
                    noise = distribution.get(noise_key)
                    if not isinstance(noise, dict) or noise.get("type") != "uninitialized":
                        continue
                    for member in ("bias", "alpha", "offset", "amplitude", "beta", "frequency", "gain"):
                        value = noise.get(member)
                        if isinstance(value, float) and math.isnan(value):
                            path = f"/biomes/{index}/{field}/itemDistributions/{dist_index}/{noise_key}/{member}"
                            result[path] = struct.pack(">d", value)
    return result


def _first_layout_difference(actual: Any, expected: Any, path: str = "", *,
                             unchanged_unused_nan_paths: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    def detail(kind: str, a: Any, e: Any) -> dict[str, Any]:
        # Keep the failure log useful without dumping a whole megabyte layout.
        def bounded(value: Any) -> Any:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            return value if len(encoded) <= 500 else encoded[:500] + "..."
        return {"path": path or "/", "kind": kind, "actual": bounded(a), "expected": bounded(e)}

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return detail("type", actual, expected)
        for key, value in expected.items():
            child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in actual:
                return {"path": child, "kind": "missing", "expected": "required field", "actual": None}
            difference = _first_layout_difference(actual[key], value, child,
                unchanged_unused_nan_paths=unchanged_unused_nan_paths)
            if difference:
                return difference
        for key in actual.keys() - expected.keys():
            child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            return {"path": child, "kind": "added", "expected": None, "actual": "unexpected field"}
        return None
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return detail("type", actual, expected)
        if len(actual) != len(expected):
            return detail("length", len(actual), len(expected))
        for index, (a, e) in enumerate(zip(actual, expected)):
            difference = _first_layout_difference(a, e, f"{path}/{index}",
                unchanged_unused_nan_paths=unchanged_unused_nan_paths)
            if difference:
                return difference
        return None
    if isinstance(actual, bool) != isinstance(expected, bool):
        return detail("type", actual, expected)
    if (path in unchanged_unused_nan_paths and isinstance(actual, float)
            and isinstance(expected, float) and math.isnan(actual) and math.isnan(expected)
            and struct.pack(">d", actual) == struct.pack(">d", expected)):
        return None
    if isinstance(expected, int) and not isinstance(expected, bool) and isinstance(actual, float):
        # Seed serialization is uint64, not an approximate float. Other native
        # float fields can legitimately serialize an integer input as 1.0.
        if path.rsplit("/", 1)[-1].lower().endswith("seed"):
            return detail("type", actual, expected)
        if not math.isfinite(actual) or not actual.is_integer() or int(actual) != expected:
            return detail("value", actual, expected)
    return None if _values_match(actual, expected) else detail("value", actual, expected)


def _compare_native_layout(actual: Any, expected: Any) -> dict[str, Any]:
    a, actual_changes = _native_layout_comparison_form(actual)
    e, expected_changes = _native_layout_comparison_form(expected)
    actual_unused = _native_unused_nan_fields(a)
    expected_unused = _native_unused_nan_fields(e)
    same_unused = frozenset(path for path, bits in expected_unused.items()
                           if actual_unused.get(path) == bits)
    difference = _first_layout_difference(a, e, unchanged_unused_nan_paths=same_unused)
    return {"matches": difference is None, "difference": difference,
            "unchangedNativeUnusedNaNFields": sorted(same_unused),
            "normalizations": {"actual": actual_changes, "expected": expected_changes}}


def _verify_parameters(document: dict[str, Any], parameters: dict[str, Any]) -> None:
    template = swe.world_template(document)
    if _coordinate(template.get("celestialParameters", {})) != _coordinate(parameters):
        raise NativeGenerationError("Native output has the wrong celestial coordinate")
    expected = parameters.get("visitableParameters")
    if not _native_world_parameters_match(template.get("worldParameters"), expected):
        raise NativeGenerationError("Native output does not contain every edited world parameter")
    if list(document["size"]) != expected.get("worldSize"):
        raise NativeGenerationError("Native output has the wrong world dimensions")


def _inspect_native_dungeon_removals(backup: Path | None, rectangles: list[dict[str, Any]], rewritten_world: Path) -> Any:
    """Inspect the pre-ocean native snapshot before publishing a rewritten world."""
    if backup is None or not Path(backup).is_file():
        raise NativeGenerationError(
            "The ocean rewrite did not retain its native world snapshot; "
            "the requested sector reset and dungeon removals cannot be verified."
        )
    from native_dungeon_guard import inspect_ocean_reset_major_dungeon_removals
    return inspect_ocean_reset_major_dungeon_removals(Path(backup), rectangles, rewritten_world=rewritten_world)


def _add_private_biome_transition_overlay(
    boot: dict[str, Any], diagnostic: Path, threshold: float,
) -> tuple[dict[str, Any], Path]:
    """Create a temporary native-generation-only asset package.

    Vanilla 1.4.4 scans *children* of assetDirectories and ignores the newer
    assetSources boot option. Do not put a loose patch directly in either the
    real asset folder or an assetDirectories package root.
    """
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or not 0 <= threshold <= 1):
        raise ValueError("Private biome transition threshold must be a finite number from 0 to 1")
    diagnostic = Path(diagnostic).resolve(strict=True)
    container = Path(tempfile.mkdtemp(prefix="private-flora-assets-", dir=str(diagnostic))).resolve()
    if container.parent != diagnostic:
        raise NativeGenerationError("Private flora asset directory escaped the diagnostic directory")
    package = container / "shore-flora"
    package.mkdir()
    swe.atomic_json_dump(
        {"name": "swe05_private_shore_flora", "version": "1", "priority": 1000000000},
        package / "_metadata",
    )
    swe.atomic_json_dump(
        [{"op": "replace", "path": "/biomeTransitionThreshold", "value": float(threshold)}],
        package / "world_template.config.patch",
    )
    result = swe.deep_copy_json(boot)
    result.setdefault("assetDirectories", []).append(str(container))
    return result, package


def _verify_native_viewport_sectors(world: Path, windows: Sequence[Sequence[int]]) -> dict[str, Any]:
    """Require all requested on-disk sectors to have completed native generation."""
    targets: set[tuple[int, int]] = set()
    for window in windows:
        targets.update(_viewport_sector_set(window))
    database, records = swe.load_records(world)
    found: set[tuple[int, int]] = set()
    incomplete: list[dict[str, Any]] = []
    try:
        for key, compressed in records:
            if not key or key[0] != swe.STORE_TILE_SECTOR:
                continue
            sector = swe.world_sector_coordinates(key)
            if sector not in targets:
                continue
            try:
                raw = zlib.decompress(compressed)
                stream = io.BytesIO(raw)
                generation_level = swe.read_varuint(stream)
                version = swe.read_varuint(stream)
                if version not in (417, 418) or generation_level not in range(6):
                    raise ValueError(f"unsupported sector version {version} / generation {generation_level}")
            except (ValueError, zlib.error, swe.WorldFormatError) as exc:
                raise NativeGenerationError(f"Cannot verify generated viewport sector {sector}: {exc}") from exc
            found.add(sector)
            if generation_level < 4:
                incomplete.append({"sector": list(sector), "generationLevel": generation_level})
        missing = targets - found
        if missing or incomplete:
            raise NativeGenerationError(
                "Native viewport sectors were not fully saved: "
                f"{len(missing)} missing, {len(incomplete)} incomplete; "
                f"examples {sorted(missing)[:4]} {incomplete[:4]}"
            )
    finally:
        swe.close_database(database)
    return {"targetSectorCount": len(targets), "savedCompleteSectorCount": len(found),
            "minimumGenerationLevel": 4}


def _run_native_pass(
    server: Path, diagnostic: Path, storage: Path,
    targets: Sequence[tuple[dict[str, Any], tuple[int, ...], Path]],
    deadline: float, emit: Callable[..., None], phase: str, *,
    viewport_windows_by_coordinate: dict[tuple[int, ...], Sequence[Sequence[int]]] | None = None,
    biome_transition_threshold: float | None = None,
    max_packet_bytes: int | None = None,
) -> None:
    """Run and fully close one private server before any World4 rewrite."""
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= time.monotonic()):
        raise ValueError("Native phase deadline must be finite and in the future")
    requested_viewports = viewport_windows_by_coordinate or {}
    target_coordinates = {coordinate for _parameters, coordinate, _output in targets}
    if set(requested_viewports) - target_coordinates:
        raise ValueError("Native viewport request contains a coordinate not included in this private pass")
    prepared_viewports = {
        coordinate: _viewport_windows(
            requested_viewports[coordinate], parameters["visitableParameters"]["worldSize"],
        )
        for parameters, coordinate, _output in targets if coordinate in requested_viewports
    }
    if biome_transition_threshold is not None and (
        isinstance(biome_transition_threshold, bool)
        or not isinstance(biome_transition_threshold, (int, float))
        or not math.isfinite(biome_transition_threshold)
        or not 0 <= biome_transition_threshold <= 1
    ):
        raise ValueError("Private biome transition threshold must be a finite number from 0 to 1")
    emit("native_phase_timeout_started", phase=phase,
         deadlineRemainingSeconds=round(max(0, deadline - time.monotonic()), 3),
         deadlineScope="this-private-native-pass", worldCount=len(targets))
    universe = storage / "universe"
    player_uuid = uuid.uuid4()
    _write_private_client_context(universe, player_uuid, targets[0][1])
    game_port, rcon_port = _free_port(), _free_port()
    while rcon_port == game_port:
        rcon_port = _free_port()
    boot = _private_boot_config(server, storage, game_port, rcon_port, secrets.token_hex(24))
    flora_overlay: Path | None = None
    if biome_transition_threshold is not None:
        boot, flora_overlay = _add_private_biome_transition_overlay(boot, diagnostic, biome_transition_threshold)
        emit("native_private_flora_overlay_prepared", phase=phase, overlay=str(flora_overlay),
             biomeTransitionThreshold=biome_transition_threshold, realAssetsModified=False)
    account, account_password = "tool05_" + secrets.token_hex(8), secrets.token_hex(24)
    boot["defaultConfiguration"]["serverUsers"] = {account: {"password": account_password, "admin": False}}
    # Large custom planets may take minutes to initialize. RCON's timeout is
    # a socket read timeout, so keep our authenticated shutdown channel alive
    # for the entire bounded pass, not only the default ten seconds.
    boot["defaultConfiguration"]["rconServerTimeout"] = max(60000, int(max(0, deadline - time.monotonic()) * 1000) + 60000)
    boot_path = diagnostic / (phase + "-sbinit.config")
    swe.atomic_json_dump(boot, boot_path)
    # Root persists a full runtime configuration into storage. On verification
    # that old file would override the new ports/passwords in boot defaults.
    # Retain it only as a diagnostic snapshot before starting this private pass.
    runtime_config = storage / "starbound_server.config"
    if runtime_config.is_file():
        previous_runtime = diagnostic / (phase + "-previous-runtime.config")
        os.replace(runtime_config, previous_runtime)
        emit("native_private_runtime_config_reset", phase=phase, retainedConfig=str(previous_runtime))
    process: subprocess.Popen | None = None
    rcon: _Rcon | None = None
    game_socket: socket.socket | None = None
    rcon_socket: socket.socket | None = None
    try:
        with (diagnostic / (phase + "-server-console.log")).open("wb") as console:
            process = subprocess.Popen(
                [str(server), "-bootconfig", str(boot_path), "-logfile", phase + "-native-generation.log"],
                cwd=str(server.parent), stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            emit("native_server_started", phase=phase, pid=process.pid, storageDirectory=str(storage),
                 assetDirectories=boot["assetDirectories"], assetSources=boot["assetSources"],
                 imageAudioFallback={name: boot.get("assetsSettings", {}).get(name)
                                     for name in ("missingImage", "missingAudio")})
            rcon_socket = _connect_when_ready(rcon_port, process, deadline)
            rcon = _Rcon(rcon_socket, boot["defaultConfiguration"]["rconServerPassword"])
            # Assets are loaded now, so sample actual remaining headroom. No
            # game client has connected yet and these private target worlds
            # have not been opened for generation while we size their metadata.
            packet_budget = _native_packet_budget(universe, targets, max_packet_bytes=max_packet_bytes)
            emit("native_private_packet_budget", phase=phase, **packet_budget)
            game_socket = _connect_when_ready(game_port, process, deadline)
            client = _NativeClient(game_socket, max_packet_bytes=packet_budget["maxPacketBytes"])
            client.connect(deadline, player_uuid, account, account_password)
            emit("native_private_client_connected", phase=phase)
            for parameters, coordinate, _output in targets:
                emit("native_world_creation_requested", phase=phase, coordinate=list(coordinate))
                template = client.generate(coordinate, deadline)
                _verify_parameters({"metadata": {"worldTemplate": template}, "size": template["size"]}, parameters)
                emit("native_world_initialized", phase=phase, coordinate=list(coordinate), worldSize=template["size"])
                if prepared_viewports.get(coordinate):
                    viewport_summary = client.generate_viewports(
                        prepared_viewports[coordinate], deadline, world_size=template["size"],
                        progress=lambda event, details: emit(event, phase=phase, coordinate=list(coordinate), **details),
                    )
                    emit("native_viewports_completed", phase=phase, coordinate=list(coordinate), summary=viewport_summary)
            client.send(14, b"\x00")
            _close_private_socket(game_socket)
            game_socket = None
            emit("native_server_shutdown_requested", phase=phase)
            try:
                acknowledgement = rcon.stop()
            finally:
                # Native TcpSocket::receive holds a read lock while blocking.
                # The server's close() needs the write lock; leaving this RCON
                # socket open until process.wait() deadlocks shutdown until its
                # long idle timeout. Release it on ACK failure as well.
                _close_private_socket(rcon_socket)
                rcon_socket = None
                rcon = None
            if acknowledgement is not None:
                emit("native_server_shutdown_acknowledged", phase=phase, response=acknowledgement)
            else:
                emit("native_server_shutdown_ack_absent_awaiting_exit", phase=phase)
            process.wait(timeout=60)
            if process.returncode != 0:
                raise NativeGenerationError(f"Native server exited unsuccessfully: {process.returncode}")
            if acknowledgement is None:
                emit("native_server_shutdown_ack_absent_clean_exit", phase=phase, returnCode=0)
            emit("native_server_stopped", phase=phase, returnCode=process.returncode)
            if flora_overlay is not None:
                console_text = (diagnostic / (phase + "-server-console.log")).read_text(encoding="utf-8", errors="replace")
                if not any("Loading assets from:" in line and str(flora_overlay).casefold() in line.casefold()
                           for line in console_text.splitlines()):
                    raise NativeGenerationError("The native server did not load the private shore-flora asset overlay")
                emit("native_private_flora_overlay_loaded", phase=phase, overlay=str(flora_overlay))
            for _parameters, coordinate, _output in targets:
                if prepared_viewports.get(coordinate):
                    saved = _verify_native_viewport_sectors(universe / _world_filename(coordinate), prepared_viewports[coordinate])
                    emit("native_viewport_saved_sectors_verified", phase=phase, coordinate=list(coordinate), summary=saved)
    finally:
        try:
            if process is not None and process.poll() is None and rcon is not None:
                try:
                    response = rcon.stop()
                    emit("native_cleanup_shutdown_acknowledged" if response is not None
                         else "native_cleanup_shutdown_connection_closed", phase=phase, response=response)
                except Exception as cleanup_error:
                    emit("native_cleanup_shutdown_request_failed", phase=phase, error=str(cleanup_error))
        finally:
            # This ordering is essential even after authentication/generation
            # exceptions: never wait for a child while retaining its sockets.
            _close_private_socket(game_socket)
            _close_private_socket(rcon_socket)
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                        emit("native_private_process_terminated", phase=phase, pid=process.pid)
            except Exception as cleanup_error:
                # Retain the original generation/ACK failure, not an incidental
                # wait/terminate exception raised while disposing of our child.
                emit("native_private_process_cleanup_failed", phase=phase,
                     error=str(cleanup_error), pid=process.pid)


def apply_ordered_parallaxes_in_place(
    world: Path, layer_ids: Sequence[str], *, keep_backup: bool = False,
    excluded_biome_names: Sequence[str] = (),
) -> tuple[Path | None, dict[str, Any]]:
    """Repair parallax metadata atomically, optionally retaining a backup.

    Keep saved sectors intact. Capacity recovery, when needed, remaps every
    layout AND saved-tile reference using the existing exact compactor.
    """
    # The same routine also handles explicit same-order repairs. Preserve a
    # strong snapshot until the staged database has passed all writer checks.
    world_edit_guard.assert_world_available(world)
    before = world_edit_guard.fingerprint(world)
    backup = None
    with tempfile.TemporaryDirectory(prefix=".swe-parallax-", dir=world.parent) as staging:
        output = Path(staging) / world.name
        database, records, document = swe.load_metadata_document(world)
        try:
            compaction = None
            try:
                summary = swe.apply_ordered_biome_parallaxes(
                    document, layer_ids, excluded_biome_names=excluded_biome_names,
                )
            except swe.BiomeIndexCapacityError:
                records, compaction = swe.compact_biome_table(
                    document, records, tile_offsets=swe.tile_sector_offsets,
                    tile_store_type=swe.STORE_TILE_SECTOR,
                )
                summary = swe.apply_ordered_biome_parallaxes(
                    document, layer_ids, excluded_biome_names=excluded_biome_names,
                )
            if summary["changedCells"] or compaction is not None:
                swe.write_metadata_document(world, output, database, records, document)
            if compaction is not None:
                summary["capacityCompaction"] = compaction
        finally:
            swe.close_database(database)
        if output.is_file():
            world_edit_guard.assert_world_unchanged(world, before)
            if keep_backup:
                backup = swe._unique_backup_path(world)
                shutil.copy2(world, backup)
                world_edit_guard.assert_world_unchanged(world, before)
            os.replace(output, world)
    return backup, summary


def regenerate_celestial_worlds(
    chunks_path: Path,
    source_location: Sequence[int],
    editor: dict[str, Any],
    assets: Path,
    jobs: Sequence[dict[str, Any]],
    *,
    server_executable: Path | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    timeout_seconds: float = 3600,
) -> dict[str, Any]:
    """Stage complete reset worlds without a manual intermediate game visit.

    Each job supplies ``parameters``, an unused ``output`` path, and
    ``oceanPairs``.  This function never installs outputs into the real universe;
    the caller owns its existing save transaction and rollback policy.
    Diagnostic files are retained under the operating system temporary folder.
    """
    if not jobs:
        return {"worlds": [], "nativeGeneration": True}
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 14400):
        raise ValueError("Native generation phase timeout must be a finite number within 0..14400 seconds")
    chunks_path, assets = Path(chunks_path).resolve(), Path(assets).resolve()
    server = Path(server_executable).resolve() if server_executable else resolve_server_executable(chunks_path, assets)
    if not server.is_file():
        raise NativeGenerationError(f"Native game server is missing: {server}")
    targets: list[tuple[dict[str, Any], tuple[int, ...], Path]] = []
    seen_coordinates: set[tuple[int, ...]] = set()
    seen_outputs: set[Path] = set()
    for job in jobs:
        parameters = swe.deep_copy_json(job["parameters"])
        coordinate = _coordinate(parameters)
        output = Path(job["output"]).resolve()
        if output.exists() or output == chunks_path:
            raise ValueError(f"Generation output must be a new staging path: {output}")
        if coordinate in seen_coordinates or output in seen_outputs:
            raise ValueError("Duplicate native generation job coordinate/output")
        swe.normalize_ocean_floor_pair_settings(job.get("oceanPairs", []))
        targets.append((parameters, coordinate, output))
        seen_coordinates.add(coordinate)
        seen_outputs.add(output)
    diagnostic = Path(tempfile.mkdtemp(prefix="swe05-native-generation-"))
    storage = diagnostic / "storage"
    universe = storage / "universe"
    universe.mkdir(parents=True)
    audit_path = diagnostic / "generation.jsonl"

    def emit(event: str, **details: Any) -> None:
        with audit_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"event": event, **details}, ensure_ascii=False, default=str) + "\n")
        if progress:
            try:
                progress(event, details)
            except Exception:
                pass

    completed: list[dict[str, Any]] = []
    emit("native_generation_preparing", diagnosticDirectory=str(diagnostic), server=str(server), worldCount=len(jobs))
    try:
        private_chunks = universe / "universe.chunks"
        shutil.copy2(chunks_path, private_chunks)
        swe.save_celestial_system(private_chunks, source_location, editor)
        settings = chunks_path.parent / "universe.dat"
        if settings.is_file():
            shutil.copy2(settings, universe / "universe.dat")
        _run_native_pass(server, diagnostic, storage, targets,
                         time.monotonic() + timeout_seconds, emit, "create")
        ocean_summaries: list[dict[str, Any]] = []
        parallax_summaries: list[dict[str, Any]] = []
        final_layouts: list[dict[str, Any]] = []
        for job, (parameters, coordinate, output) in zip(jobs, targets):
            native_world = universe / _world_filename(coordinate)
            document = swe.load_metadata_only(native_world)
            _verify_parameters(document, parameters)
            parallax_summary: dict[str, Any] = {}
            if job.get("orderedParallaxLayers"):
                _backup, parallax_summary = apply_ordered_parallaxes_in_place(
                    native_world, job["orderedParallaxLayers"],
                )
                emit("native_ordered_parallaxes_applied", coordinate=list(coordinate), summary=parallax_summary)
            parallax_summaries.append(parallax_summary)
            ocean_summary: dict[str, Any] = {"pairsApplied": 0, "rectangles": []}
            if job.get("oceanPairs"):
                _backup, ocean_summary = swe.apply_generated_ocean_floor_pairs_in_place(
                    native_world, assets, job["oceanPairs"],
                    progress=lambda event, details: emit(event, coordinate=list(coordinate), **details),
                )
                emit("native_dungeon_removal_inspection_started", coordinate=list(coordinate), backup=str(_backup))
                dungeon_removals = _inspect_native_dungeon_removals(_backup, ocean_summary.get("rectangles", []), native_world)
                ocean_summary["nativeMajorDungeonRemoval"] = dungeon_removals
                emit("native_dungeon_removals_inspected", coordinate=list(coordinate), summary=dungeon_removals)
            ocean_summaries.append(ocean_summary)
            final_layouts.append(swe.world_template(swe.load_metadata_only(native_world))["regionData"])
        # Native transition blending vetoes flora in the narrow ecology bands
        # around the smooth coast. Finish only those sectors in an isolated
        # pass, with this world's original seeds/biomes/hues. The completion
        # merger preserves all outside records and the complete original layout.
        from ocean_flora_completion import complete_in_place
        for ocean_summary, (parameters, coordinate, output) in zip(ocean_summaries, targets):
            native_world = universe / _world_filename(coordinate)
            ocean_summary["nativeCoastalFlora"] = complete_in_place(
                native_world, assets, ocean_summary, universe_folder=universe,
                server_executable=server, progress=lambda event, details: emit(event, **details),
                # Completion allocates separate bounded terrain/plants budgets
                # from its actual coast-sector plan, not create's stale clock.
                timeout_seconds=timeout_seconds,
            )
        # Reopen the finished curves with the native engine before any real
        # output is staged. A malformed selector/biome must fail this save.
        _run_native_pass(server, diagnostic, storage, targets,
                         time.monotonic() + timeout_seconds, emit, "verify")
        for ocean_summary, parallax_summary, expected_layout, (parameters, coordinate, output) in zip(ocean_summaries, parallax_summaries, final_layouts, targets):
            native_world = universe / _world_filename(coordinate)
            database, records, document = swe.load_metadata_document(native_world)
            try:
                _verify_parameters(document, parameters)
                data = swe.world_template(document)["regionData"]
                layout_comparison = _compare_native_layout(data, expected_layout)
                emit("native_layout_comparison", coordinate=list(coordinate), **layout_comparison)
                if not layout_comparison["matches"]:
                    raise NativeGenerationError(
                        "The native engine changed the completed biome/ocean layout at "
                        f"{layout_comparison['difference']['path']}. "
                        "The final world was not installed; see native_layout_comparison in the log."
                    )
                swe.validate_project_terrain(
                    {"compiledLayers": data["layers"], "terrainSelectors": data["terrainSelectors"]},
                    int(document["size"][0]), int(document["size"][1]), len(swe.compiled_biomes(document)),
                )
                swe.atomic_write_world(native_world, output, records, database)
            finally:
                swe.close_database(database)
            summary = {"output": str(output), "coordinate": list(coordinate), "oceanSummary": ocean_summary,
                       "orderedParallaxSummary": parallax_summary,
                       "worldSize": document["size"], "recordCount": len(records), "nativeGeneration": True,
                       "nativeFinalLoadVerified": True}
            completed.append(summary)
            emit("native_final_world_staged", **summary)
        return {"worlds": completed, "diagnosticDirectory": str(diagnostic), "nativeGeneration": True}
    except Exception as exc:
        fatal_detail = _native_fatal_log_detail(diagnostic)
        emit("native_generation_failed", error=str(exc), errorType=type(exc).__name__, **fatal_detail)
        explanation = (
            "\nNative server error: " + fatal_detail["nativeFatalError"]
            if fatal_detail else ""
        )
        raise NativeGenerationError(
            f"{exc}{explanation}\nNative generation diagnostics: {diagnostic}"
        ) from exc
