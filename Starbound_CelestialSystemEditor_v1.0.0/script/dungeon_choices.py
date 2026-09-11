"""Read-only, explicitly sourced dungeon and patched-JSON asset catalogs.

Real dungeon IDs and MPI display IDs are deliberately separate.  The helpers
never edit assets, run Lua, infer another installation, or drop an existing GUI
selection.  A catalog error means verification is incomplete, not an empty
valid catalog.  Sources supplied to AssetSourceStack are in engine load order.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
from pathlib import Path
import posixpath
import re
import struct
from typing import Any, Iterable, Sequence

if __package__:
    from . import starbound_world_editor as swe
else:
    import starbound_world_editor as swe


MAX_JSON_BYTES = 64 * 1024 * 1024
COCKPIT_CONFIG = "/interface/cockpit/cockpit.config"
MPI_SUPPORTED_LAYER_IDS = frozenset({"space", "atmosphere", "surface", "subsurface"})

# Audited against the installed OpenStarbound 0.1.15.1 asset source and
# StarAssets.cpp (Source ID e604e078e89310f1212dd20f5cd01bd59d25300b).
# runLoadScripts reads the script directly from its declaring source, not the
# effective overlay.  This exact script only changes cursors, .object patches,
# songbook/playerinventory UI, .abc locations and cosmetic.animation.N files.
# Its generated cursor patch names always end in .cursor.patch.  None of those
# mutations changes the catalog domains below.  This is a scoped noninterference
# proof, NOT an implementation of Lua or of the whole dynamic asset tree.
# Eager cursor image/frames inputs are also checked to exclude indirect Lua.
_OPENSB_POSTLOAD_PATH = "/scripts/opensb/assets/postload.lua"
_OPENSB_POSTLOAD_SHA256 = "0e4c728ebfa8e3aa04c77d26efbba44fe1f932cae1d2aaed29215d6a3596f976"
_OPENSB_CATALOG_SUFFIXES = frozenset({".dungeon", ".spawntypes", ".weather", ".frames", ".png"})
_OPENSB_CATALOG_JSON_PATHS = frozenset({COCKPIT_CONFIG, "/celestial.config"})
_OPENSB_DYNAMIC_CURSOR_FRAMES = "/cursors/cursors.frames"


class AssetCatalogError(ValueError):
    """A source or patch could not be completely and safely interpreted."""


def _asset_key(path: str) -> str:
    plain = str(path).split(":", 1)[0].replace("\\", "/")
    if ".." in plain.split("/"):
        raise AssetCatalogError(f"Parent traversal is not an asset path: {path}")
    return posixpath.normpath("/" + plain.lstrip("/")).lower()


def _looks_like_root(path: Path) -> bool:
    return path.is_dir() and any((path / name).exists() for name in (
        "_metadata", ".metadata", "biomes", "dungeons", "interface", "monsters",
        "celestial.config", "dungeon_worlds.config", "terrestrial_worlds.config",
    ))


class AssetSourceReader:
    """One unpacked virtual root or arbitrarily named SBAsset6 .pak.

    Unlike the generation reader, a mod need not contain a .biome file and a
    packed mod need not be named packed.pak.  Reading remains bounded and all
    unpacked paths are checked to stay within the selected source.
    """

    def __init__(self, source: Path):
        source = Path(source)
        self.root: Path | None = None
        self.pak: Path | None = None
        self.entries: dict[str, tuple[str, int, int]] = {}
        self.metadata: dict[str, Any] = {}
        self._files: dict[str, tuple[str, Path]] | None = None
        if source.is_dir():
            root = next((candidate for candidate in (
                source, source / "packed", source / "assets" / "packed",
            ) if _looks_like_root(candidate)), None)
            if root is None:
                pak = next((candidate for candidate in (
                    source / "packed.pak", source / "assets" / "packed.pak",
                ) if candidate.is_file()), None)
                if pak is not None:
                    source = pak
                else:
                    root = source
            if root is not None:
                self.root = root.resolve()
        if self.root is None:
            if not source.is_file():
                raise AssetCatalogError(f"Asset source does not exist: {source}")
            self.pak = source.resolve()
            self._read_pak_index()
        self.source = self.root or self.pak
        if self.root is not None:
            for name in ("_metadata", ".metadata"):
                path = self.root / name
                if path.is_file():
                    self.metadata = self._read_json_bytes(self._disk_bytes(path), str(path))
                    break
        if not isinstance(self.metadata, dict):
            raise AssetCatalogError(f"Asset source metadata must be an object: {self.source}")

    def _read_pak_index(self) -> None:
        assert self.pak is not None
        size = self.pak.stat().st_size
        try:
            with self.pak.open("rb") as stream:
                if swe._read_exact(stream, 8) != swe.PAK_MAGIC:
                    raise AssetCatalogError(f"Not an SBAsset6 source: {self.pak}")
                index = struct.unpack(">Q", swe._read_exact(stream, 8))[0]
                if not 16 <= index < size:
                    raise AssetCatalogError(f"Invalid SBAsset6 index: {self.pak}")
                stream.seek(index)
                if swe._read_exact(stream, 5) != swe.PAK_INDEX_MAGIC:
                    raise AssetCatalogError(f"Missing SBAsset6 INDEX: {self.pak}")
                count = swe.read_varuint(stream)
                if count > size:
                    raise AssetCatalogError(f"Invalid packed metadata count: {self.pak}")
                for _ in range(count):
                    key = swe.read_string(stream)
                    self.metadata[key] = swe.read_dynamic(stream)
                count = swe.read_varuint(stream)
                if count > size // 18:
                    raise AssetCatalogError(f"Invalid packed asset count: {self.pak}")
                for _ in range(count):
                    path = swe.read_string(stream)
                    offset, length = struct.unpack(">QQ", swe._read_exact(stream, 16))
                    if offset < 16 or offset + length > index:
                        raise AssetCatalogError(f"Packed asset outside data area: {path}")
                    self.entries[_asset_key(path)] = (path, offset, length)
        except AssetCatalogError:
            raise
        except Exception as exc:
            raise AssetCatalogError(f"Cannot read packed asset source {self.pak}: {exc}") from exc

    def _unpacked_files(self) -> dict[str, tuple[str, Path]]:
        assert self.root is not None
        if self._files is None:
            self._files = {}
            for path in sorted(self.root.rglob("*")):
                if path.is_file():
                    virtual = "/" + path.relative_to(self.root).as_posix()
                    self._files[_asset_key(virtual)] = (virtual, path)
        return self._files

    def paths(self, suffix: str = "") -> Iterable[str]:
        suffix = suffix.casefold()
        values = (value[0] for value in (
            self._unpacked_files().values() if self.root is not None else self.entries.values()
        ))
        return iter(sorted((value for value in values if value.casefold().endswith(suffix)
                            and value not in ("/_metadata", "/.metadata")), key=str.casefold))

    def read_bytes(self, path: str) -> bytes:
        key = _asset_key(path)
        if self.root is not None:
            item = self._unpacked_files().get(key)
            if item is None:
                raise FileNotFoundError(f"Asset {path} not found in {self.source if hasattr(self, 'source') else self.root}")
            return self._disk_bytes(item[1])
        entry = self.entries.get(key)
        if entry is None:
            raise FileNotFoundError(f"Asset {path} not found in {self.pak}")
        _name, offset, size = entry
        if size > MAX_JSON_BYTES:
            raise AssetCatalogError(f"Catalog asset exceeds {MAX_JSON_BYTES} bytes: {path}")
        assert self.pak is not None
        with self.pak.open("rb") as stream:
            stream.seek(offset)
            return swe._read_exact(stream, size)

    def _disk_bytes(self, path: Path) -> bytes:
        assert self.root is not None
        disk = path.resolve()
        try:
            disk.relative_to(self.root)
        except ValueError as exc:
            raise AssetCatalogError(f"Asset resolves outside its source: {path}") from exc
        if disk.stat().st_size > MAX_JSON_BYTES:
            raise AssetCatalogError(f"Catalog asset exceeds {MAX_JSON_BYTES} bytes: {path}")
        return disk.read_bytes()

    @staticmethod
    def _read_json_bytes(data: bytes, label: str) -> Any:
        try:
            value = swe.parse_starbound_json(data, label)
            swe.validate_finite_json_numbers(value, label)
            return value
        except Exception as exc:
            raise AssetCatalogError(f"Cannot parse catalog JSON {label}: {exc}") from exc

    def json(self, reference: str) -> Any:
        path, separator, query = reference.partition(":")
        value = self._read_json_bytes(self.read_bytes(path), f"{self.source}:{path}")
        return swe._asset_json_query(value, query) if separator else value


class _PatchTestFailed(Exception):
    pass


def _tokens(path: Any) -> list[str]:
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise AssetCatalogError(f"Invalid JSON pointer: {path!r}")
    if re.search(r"~(?:[^01]|$)", path):
        raise AssetCatalogError(f"Invalid JSON pointer escape: {path}")
    return [value.replace("~1", "/").replace("~0", "~") for value in path[1:].split("/")] if path else []


def _array_index(token: str, length: int, *, append: bool = False) -> int:
    if append and token == "-":
        return length
    if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
        raise KeyError(token)
    index = int(token)
    if not 0 <= index < length + int(append):
        raise KeyError(token)
    return index


def _get(value: Any, tokens: Sequence[str]) -> Any:
    for token in tokens:
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list):
            value = value[_array_index(token, len(value))]
        else:
            raise KeyError(token)
    return value


def _edit(value: Any, tokens: Sequence[str], operation: str, replacement: Any = None) -> Any:
    if not tokens:
        if operation == "remove":
            raise AssetCatalogError("Removing the entire JSON document is not a supported catalog patch")
        return copy.deepcopy(replacement)
    parent = _get(value, tokens[:-1])
    key = tokens[-1]
    if isinstance(parent, dict):
        if operation != "add" and key not in parent:
            raise KeyError(key)
        if operation == "remove":
            del parent[key]
        else:
            parent[key] = copy.deepcopy(replacement)
    elif isinstance(parent, list):
        index = _array_index(key, len(parent), append=operation == "add")
        if operation == "remove":
            parent.pop(index)
        elif operation == "add":
            parent.insert(index, copy.deepcopy(replacement))
        else:
            parent[index] = copy.deepcopy(replacement)
    else:
        raise KeyError(key)
    return value


def _merge(left: Any, right: Any, *, nulling: bool = False) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        result = copy.deepcopy(left)
        for key, value in right.items():
            if nulling and value is None:
                result.pop(key, None)
            else:
                result[key] = _merge(result.get(key), value, nulling=nulling)
        return result
    if right is None and not nulling:
        return copy.deepcopy(left)
    return copy.deepcopy(right)


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def _operation(value: Any, operation: dict[str, Any]) -> Any:
    if "search" in operation:
        raise AssetCatalogError("Catalog verification does not support Starbound search patches")
    name = operation.get("op")
    tokens = _tokens(operation.get("path"))
    if name == "test":
        inverse = operation.get("inverse", False)
        if not isinstance(inverse, bool):
            raise AssetCatalogError("Patch test inverse must be a boolean")
        try:
            actual = _get(value, tokens)
            matched = "value" not in operation or _json_equal(actual, operation["value"])
        except KeyError:
            matched = False
        if matched == inverse:
            raise _PatchTestFailed()
        return value
    if name == "remove":
        return _edit(value, tokens, "remove")
    if name in ("add", "replace"):
        return _edit(value, tokens, name, operation["value"])
    if name == "merge":
        merged = _merge(_get(value, tokens), operation["value"])
        return _edit(value, tokens, "replace", merged)
    if name in ("copy", "move"):
        source = _tokens(operation.get("from"))
        child = copy.deepcopy(_get(value, source))
        if name == "move":
            if tokens[:len(source)] == source and len(tokens) > len(source):
                raise AssetCatalogError("A patch cannot move a value into its own child")
            if tokens == source:
                return value
            value = _edit(value, source, "remove")
        return _edit(value, tokens, "add", child)
    raise AssetCatalogError(f"Unsupported catalog patch operation: {name!r}")


def _patch_array(original: Any, operations: list[Any]) -> Any:
    value = copy.deepcopy(original)
    for operation in operations:
        if isinstance(operation, list):
            try:
                value = _patch_array(value, operation)
            except _PatchTestFailed:
                pass  # A nested conditional group is atomic, just like native.
        elif isinstance(operation, dict):
            value = _operation(value, operation)
        else:
            raise AssetCatalogError("External-reference/string JSON patches are not supported by the catalog")
    return value


class AssetSourceStack:
    """Resolve explicit low-to-high sources and their JSON patches.

    Full-file overrides replace the base document, not its previously attached
    patch list (matching StarAssets::addSource). A patch before any target asset
    exists is ignored by native and is ignored here. .patch, .patch0..9 and
    .patchlist are supported; Lua/search/external-reference patches fail closed.
    One byte-pinned OpenSB load script is proven not to change specified catalog
    domains.  A stack containing it remains unavailable outside those domains;
    a changed or unknown script still prevents verification of the entire stack.
    """

    def __init__(self, sources: Sequence[Path]):
        self.readers = [AssetSourceReader(path) for path in sources]
        self._files: dict[str, tuple[AssetSourceReader, str]] = {}
        self._patches: dict[str, list[tuple[AssetSourceReader, str]]] = {}
        self._cache: dict[str, Any] = {}
        self._has_scoped_opensb_script = False
        for reader in self.readers:
            self._has_scoped_opensb_script |= self._verify_load_scripts(reader)
            for path in reader.paths():
                key = _asset_key(path)
                target = re.sub(r"\.patch(?:[0-9]|\.lua)?$", "", key)
                if target != key and target in self._files:
                    self._patches.setdefault(target, []).append((reader, path))
                elif key.endswith(".patchlist"):
                    patches = reader.json(path)
                    if not isinstance(patches, list):
                        raise AssetCatalogError(f"patchlist must be an array: {path}")
                    for pair_index, pair in enumerate(patches):
                        if not isinstance(pair, dict) or not isinstance(pair.get("paths"), list) or not isinstance(pair.get("patches"), list):
                            raise AssetCatalogError(f"Unsupported patchlist entry: {path}")
                        for target_path in pair["paths"]:
                            if not isinstance(target_path, str):
                                raise AssetCatalogError(f"Invalid patchlist target: {path}")
                            target_key = _asset_key(target_path)
                            if target_key in self._files:
                                for index, patch in enumerate(pair["patches"]):
                                    reference = patch if isinstance(patch, str) else f"{path}:[{pair_index}].patches[{index}]"
                                    self._patches.setdefault(target_key, []).append((reader, reference))
                self._files[key] = (reader, path)
        if self._has_scoped_opensb_script:
            self._verify_opensb_load_inputs()

    @staticmethod
    def _verify_load_scripts(reader: AssetSourceReader) -> bool:
        scripts = reader.metadata.get("scripts")
        if scripts is None or scripts == {}:
            return False
        # Empty, well-formed groups do not execute code.  Reject malformed groups
        # and unknown nonempty groups as well as all unreviewed script bodies.
        if (isinstance(scripts, dict)
                and all(isinstance(group, list) and not group for group in scripts.values())):
            return False
        expected = {"postLoad": [_OPENSB_POSTLOAD_PATH]}
        if (scripts == expected
                and reader.metadata.get("name") == "opensb_base"
                and reader.metadata.get("version") == "0.1.15.1"):
            try:
                digest = hashlib.sha256(reader.read_bytes(_OPENSB_POSTLOAD_PATH)).hexdigest()
            except (OSError, ValueError) as exc:
                raise AssetCatalogError(f"Cannot verify OpenSB asset-load Lua script: {reader.source}: {exc}") from exc
            if digest == _OPENSB_POSTLOAD_SHA256:
                return True
        raise AssetCatalogError(
            f"Asset-load Lua scripts prevent complete catalog verification: {reader.source}. "
            "Only the byte-verified OpenSB 0.1.15.1 postLoad script is supported, "
            "and only for its audited unaffected catalog domains."
        )

    def _verify_opensb_load_inputs(self) -> None:
        # The known script eagerly loads this image and its frames before it
        # mutates anything.  Unknown Lua reached through an image/frames patch
        # must not inherit the main script's fingerprint exemption (Lua contexts
        # shallow-copy their default environment).  Inspect only static data;
        # never execute those patches.  A missing input makes native stop this
        # script and therefore cannot add effects to the audited domains.
        for _reader, patch in self._patches.get("/cursors/cursors.png", []):
            if patch.partition(":")[0].casefold().endswith(".lua"):
                raise AssetCatalogError(f"Lua cursor image patch invalidates OpenSB load-script verification: {patch}")
        # bestFramesSpecification searches the image directory then its parents,
        # checking <image-name>.frames before default.frames at each level.
        candidates = ("/cursors/cursors.frames", "/cursors/default.frames", "/cursors.frames", "/default.frames")
        frames = next((path for path in candidates if path in self._files), None)
        dependencies = {path for path in (frames, _OPENSB_DYNAMIC_CURSOR_FRAMES) if path in self._files}
        for path in sorted(dependencies):
            try:
                self._resolved_json(path)
            except (OSError, ValueError) as exc:
                raise AssetCatalogError(f"Cannot statically verify OpenSB load-script input {path}: {exc}") from exc

    def _verify_script_scope(self, path: str, *, enumeration: bool = False) -> None:
        if not self._has_scoped_opensb_script:
            return
        if enumeration:
            permitted = path.casefold() in _OPENSB_CATALOG_SUFFIXES
        else:
            key = _asset_key(path)
            permitted = (key in _OPENSB_CATALOG_JSON_PATHS
                         or (any(key.endswith(suffix) for suffix in _OPENSB_CATALOG_SUFFIXES)
                             and key != _OPENSB_DYNAMIC_CURSOR_FRAMES))
        if not permitted:
            raise AssetCatalogError(
                f"OpenSB asset-load Lua verification does not cover this asset domain: {path!r}. "
                "Dynamic cursor/object/UI/song/animation assets are not interpreted."
            )

    def paths(self, suffix: str = "") -> Iterable[str]:
        self._verify_script_scope(suffix, enumeration=True)
        return iter(sorted((path for _reader, path in self._files.values()
                            if path.casefold().endswith(suffix.casefold())), key=str.casefold))

    def read_bytes(self, path: str) -> bytes:
        """Read one effective overlay file only when it has no attached patches.

        This does not execute Lua or apply image/JSON patches and never pretends
        that unpatched source bytes are the final patched asset.  A caller must
        remove image subframes/directives before asking for the base image.
        """
        if ":" in path or "?" in path:
            raise AssetCatalogError("Raw catalog bytes require a base asset path without subpaths or directives")
        self._verify_script_scope(path)
        key = _asset_key(path)
        if self._patches.get(key):
            raise AssetCatalogError(f"Patched asset bytes cannot be completely verified without interpretation: {path}")
        source = self._files.get(key)
        if source is None:
            raise FileNotFoundError(f"Asset is not present in the explicit source stack: {path}")
        reader, source_path = source
        return reader.read_bytes(source_path)

    def json(self, reference: str) -> Any:
        path = reference.partition(":")[0]
        self._verify_script_scope(path)
        return self._resolved_json(reference)

    def _resolved_json(self, reference: str) -> Any:
        """Static resolver, also used internally to verify load-script inputs."""
        path, separator, query = reference.partition(":")
        key = _asset_key(path)
        if key not in self._cache:
            source = self._files.get(key)
            if source is None:
                raise FileNotFoundError(f"Asset is not present in the explicit source stack: {path}")
            reader, source_path = source
            value = reader.json(source_path)
            for patch_reader, patch_path in self._patches.get(key, []):
                if patch_path.partition(":")[0].casefold().endswith(".lua"):
                    raise AssetCatalogError(f"Lua asset patch prevents complete catalog verification: {patch_path}")
                patch = patch_reader.json(patch_path)
                try:
                    if isinstance(patch, dict):
                        value = _merge(value, patch, nulling=True)
                    elif isinstance(patch, list):
                        value = _patch_array(value, patch)
                    else:
                        raise AssetCatalogError("JSON patch must be an array or object")
                except _PatchTestFailed:
                    pass  # Native skips the entire top-level conditional patch.
                except Exception as exc:
                    raise AssetCatalogError(f"Cannot completely apply {patch_reader.source}:{patch_path}: {exc}") from exc
            self._cache[key] = value
        result = copy.deepcopy(self._cache[key])
        return swe._asset_json_query(result, query) if separator else result


def _ordered_sources(paths: Sequence[Path]) -> list[Path]:
    records = []
    named: dict[str, dict[str, Any]] = {}
    seen = set()
    for path in paths:
        reader = AssetSourceReader(path)
        source = Path(reader.source)
        canonical = str(source.resolve()).casefold()
        if canonical in seen:
            continue
        seen.add(canonical)
        metadata = reader.metadata
        name = metadata.get("name")
        if name is not None and (not isinstance(name, str) or not name):
            raise AssetCatalogError(f"Invalid asset source name: {source}")
        priority = metadata.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)) or not math.isfinite(priority):
            raise AssetCatalogError(f"Invalid asset source priority: {source}")
        for field in ("requires", "includes"):
            value = metadata.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise AssetCatalogError(f"Invalid asset source {field}: {source}")
        record = {"source": source, "name": name, "priority": priority, "metadata": metadata}
        if name is not None and name in named:
            old = named[name]
            if priority >= old["priority"]:
                old.update(record)
        else:
            records.append(record)
            if name is not None:
                named[name] = record
    records.sort(key=lambda item: (item["priority"], item["name"] or str(item["source"])))
    active, visited = set(), set()
    result = []

    def visit(record):
        identity = id(record)
        if identity in active:
            raise AssetCatalogError("Asset source dependencies form a cycle")
        if identity in visited:
            return
        active.add(identity)
        for field in ("includes", "requires"):
            for name in record["metadata"].get(field, []):
                dependency = named.get(name)
                if dependency is not None:
                    visit(dependency)
                elif field == "requires":
                    raise AssetCatalogError(f"Asset source {record['name']!r} requires missing source {name!r}")
        active.remove(identity)
        visited.add(identity)
        result.append(record["source"])

    for record in records:
        visit(record)
    return result


def discover_asset_sources(assets: Path, universe_folder: Path | None = None) -> list[Path]:
    """Discover only conventional sources in the explicitly selected install.

    Prefer an explicitly selected unpacked base over its duplicate packed.pak.
    When a universe is supplied, its storage parent selects the mods directory;
    a detached QA copy does not magically identify a different live install.
    Callers can instead supply an explicit source sequence to AssetSourceStack.
    """
    assets = Path(assets)
    reader = AssetSourceReader(assets)
    sources = [Path(reader.source)]
    if reader.root is None and assets.is_dir():
        container = assets if (assets / "packed.pak").is_file() else assets / "assets"
        if container.is_dir():
            sources = [path for path in sorted(container.iterdir()) if not path.name.startswith((".", "_"))
                       and (path.is_dir() or path.suffix.casefold() == ".pak")]
    if universe_folder is not None:
        universe = Path(universe_folder).resolve()
        # Only the conventional, explicitly supplied universe/storage layout.
        install = universe.parent.parent if universe.parent.name.casefold() == "storage" else None
    else:
        anchor = Path(reader.source)
        if anchor.is_file():
            anchor = anchor.parent
        if anchor.name.casefold() == "packed" and anchor.parent.name.casefold() == "assets":
            anchor = anchor.parent
        install = anchor.parent if anchor.name.casefold() == "assets" else None
    if install is not None:
        mods = install / "mods"
        if mods.is_dir():
            sources.extend(path for path in sorted(mods.iterdir()) if not path.name.startswith((".", "_"))
                           and (path.is_dir() or path.suffix.casefold() == ".pak"))
    return _ordered_sources(sources)


def real_dungeon_names(assets: Path | Sequence[Path] | AssetSourceStack) -> list[str]:
    """Actual dungeon metadata.name values, not filenames or MPI labels."""
    if isinstance(assets, AssetSourceStack):
        stack = assets
    else:
        sources = [Path(assets)] if isinstance(assets, (str, os.PathLike)) else list(assets)
        stack = AssetSourceStack(sources)
    result = set()
    for path in stack.paths(".dungeon"):
        document = stack.json(path)
        metadata = document.get("metadata") if isinstance(document, dict) else None
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise AssetCatalogError(f"Dungeon is missing a non-empty metadata.name: {path}")
        result.add(name)
    return sorted(result, key=lambda value: (value.casefold(), value))


def mpi_dungeon_names(asset_sources: Sequence[Path] | AssetSourceStack) -> list[str]:
    """Effective MPI display keys; this does not verify real dungeon assets."""
    stack = asset_sources if isinstance(asset_sources, AssetSourceStack) else AssetSourceStack(asset_sources)
    try:
        document = stack.json(COCKPIT_CONFIG)
    except FileNotFoundError:
        return []
    display = document.get("displayDungeons", {}) if isinstance(document, dict) else None
    if not isinstance(display, dict):
        raise AssetCatalogError("Effective cockpit displayDungeons must be an object")
    result = []
    for name, value in display.items():
        if name == "notInDatabase":
            continue
        if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
            raise AssetCatalogError(f"Invalid MPI display dungeon entry: {name!r}")
        if not isinstance(value.get("icon"), str) or not value["icon"] or not isinstance(value.get("description"), str):
            raise AssetCatalogError(f"MPI dungeon entry needs icon and description: {name}")
        result.append(name)
    return sorted(result, key=lambda value: (value.casefold(), value))


# Explicitly named alias retained for GUI call sites that only need MPI sources.
discover_mpi_asset_sources = discover_asset_sources
