"""Read-only preflight for the 03 editor's weather, species and image fields.

Only explicitly selected asset sources are consulted; scripts are never run.
Catalog/patch uncertainty is not an empty valid catalog. Unknown monster
extension keys survive, while known native scalar types are checked before a
new profile is written. Empty profiles keep region_spawn_replacement semantics.

Native source anchors (work/openstarbound_source/source): StarBiomeDatabase.cpp
16-36,123-138; StarSpawnTypeDatabase.cpp 80-92,112-143;
StarMonsterDatabase.cpp 361-414 and MonsterVariant in its .hpp (damageTeam is
uint8_t even though getUInt reads it); StarAssets.cpp 725-813,948-1001,1321-1354;
StarCelestialGraphics.cpp 242-248. Image preflight checks PNG headers, effective
frame definitions and requested frame bounds, not pixel decoding or rendering.
Image directives are deliberately not executed and produce an explicit warning.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import math
import os
from pathlib import Path
import posixpath
import struct
from typing import Any, Iterable, Mapping, Sequence

if __package__:
    from .dungeon_choices import AssetCatalogError, AssetSourceStack, discover_asset_sources
    from .region_spawn_replacement import normalize_spawn_profile
    from . import starbound_world_editor as swe
    from .world_parameter_validation import native_float32, native_integer
else:
    from dungeon_choices import AssetCatalogError, AssetSourceStack, discover_asset_sources
    from region_spawn_replacement import normalize_spawn_profile
    import starbound_world_editor as swe
    from world_parameter_validation import native_float32, native_integer


TEAM_TYPES = frozenset({
    "null", "friendly", "enemy", "pvp", "passive", "ghostly", "environment",
    "indiscriminate", "assistant",
})
CLIENT_ENTITY_MODES = frozenset({"ClientSlaveOnly", "ClientMasterAllowed", "ClientPresenceMaster"})
MONSTER_FLOAT_FIELDS = frozenset({
    "scale", "walkMultiplier", "runMultiplier", "jumpMultiplier", "weightMultiplier",
    "healthMultiplier", "touchDamageMultiplier", "damageReceivedAggressiveDuration",
    "onDamagedOthersAggressiveDuration", "onFireAggressiveDuration",
})
MONSTER_STRING_FIELDS = frozenset({"colors", "powerLevelFunction", "healthLevelFunction"})
MONSTER_OPTIONAL_STRING_FIELDS = frozenset({"shortdescription", "renderLayer", "portraitIcon"})
# getX(key, default) and optX explicitly accept null (StarJson.cpp 477-516).
# Required fields below have already passed readCommonParameters in the base
# MonsterVariant before shared SpawnProfile overrides reach createMonster;
# jsonMerge(existing, null) retains them (MonsterDatabase.cpp 210-216,455;
# StarSpawner.cpp 290,305; StarJson.cpp 1038-1048). This is an OVERRIDE validator,
# not a validator for a complete final MonsterVariant without a base.
MONSTER_NULL_DEFAULT_FIELDS = frozenset({
    "damageTeam", "initialScriptDelta", "persistent", "damageTeamType", "clientEntityMode",
    "animationScripts", "animationDamageParts", "colorSwap",
}) | (MONSTER_FLOAT_FIELDS - {"scale"}) | MONSTER_STRING_FIELDS | MONSTER_OPTIONAL_STRING_FIELDS
MONSTER_NULL_INHERITED_FIELDS = frozenset({"scale", "scripts", "metaBoundBox", "mouthOffset", "feetOffset"})


def validate_monster_parameters(value: Any, *, label: str = "monsterParameters") -> Any:
    """Validate only established native fields; do not prune extension keys.

    None is a legal compiled SpawnProfile value. The explicit region-template
    UI may separately require an object. Lua/script-specific keys (including
    aggressive and arbitrary nested scriptConfig) are not assigned an invented
    closed schema. Native null-default fields and required base fields inherited
    by jsonMerge retain null in the override, not a fabricated replacement value.
    New null skills/nametagColor cannot be assumed safe just from this merge:
    an absent key is inserted as null, and their consumers do not null-default.
    This is not complete monster/mod behavior validation.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object or null")
    swe.validate_finite_json_numbers(value, label)
    for key, item in value.items():
        field = f"{label}.{key}"
        if item is None and key in MONSTER_NULL_DEFAULT_FIELDS | MONSTER_NULL_INHERITED_FIELDS:
            continue
        if key in {"damageTeam", "initialScriptDelta"}:
            maximum = 255 if key == "damageTeam" else 0xffffffff
            if type(item) not in (int, float):
                raise ValueError(f"{field} must be a JSON unsigned integer in 0..{maximum}; not an object or string")
            native_integer(item, label=field, maximum=maximum)
        elif key in MONSTER_FLOAT_FIELDS:
            if type(item) not in (int, float):
                raise ValueError(f"{field} must be a finite JSON number fitting native float32")
            native_float32(item, label=field)
        elif key in MONSTER_STRING_FIELDS:
            if not isinstance(item, str):
                raise ValueError(f"{field} must be a JSON string")
        elif key in MONSTER_OPTIONAL_STRING_FIELDS:
            if item is not None and not isinstance(item, str):
                raise ValueError(f"{field} must be a JSON string or null")
        elif key == "persistent":
            if type(item) is not bool:
                raise ValueError(f"{field} must be a JSON boolean")
        elif key in {"damageTeamType", "clientEntityMode"}:
            choices = TEAM_TYPES if key == "damageTeamType" else CLIENT_ENTITY_MODES
            if not isinstance(item, str) or item.casefold() not in {choice.casefold() for choice in choices}:
                raise ValueError(f"{field} must be one of: {', '.join(sorted(choices))}")
        elif key in {"scripts", "animationScripts", "baseSkills", "specialSkills", "skills"}:
            if not isinstance(item, list) or any(not isinstance(entry, str) for entry in item):
                raise ValueError(f"{field} must be a JSON array of strings")
        elif key in {"mouthOffset", "feetOffset"}:
            _float_vector(item, 2, field)
        elif key == "metaBoundBox":
            coordinates = _flatten_rect(item, field)
            _float_vector(coordinates, 4, field)
        elif key == "animationDamageParts":
            if not isinstance(item, dict):
                raise ValueError(f"{field} must be a JSON object")
        elif key == "colorSwap":
            if item is not None and not isinstance(item, dict):
                raise ValueError(f"{field} must be a JSON object or null")
        elif key == "nametagColor":
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError(f"{field} must contain three RGB integers in 0..255")
            for index, component in enumerate(item):
                if type(component) not in (int, float):
                    raise ValueError(f"{field}[{index}] must be a JSON integer in 0..255")
                native_integer(component, label=f"{field}[{index}]", maximum=255)
    return copy.deepcopy(value)


def _float_vector(value: Any, size: int, field: str) -> None:
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"{field} must contain {size} finite JSON numbers")
    for index, component in enumerate(value):
        if type(component) not in (int, float):
            raise ValueError(f"{field}[{index}] must be a finite JSON number")
        native_float32(component, label=f"{field}[{index}]")


def _flatten_rect(value: Any, field: str) -> list[Any]:
    if isinstance(value, list):
        if len(value) == 4:
            return value
        if len(value) == 2 and all(isinstance(part, list) and len(part) == 2 for part in value):
            return value[0] + value[1]
    raise ValueError(f"{field} must be [xMin, yMin, xMax, yMax] or [[xMin, yMin], [xMax, yMax]]")


@dataclass(frozen=True)
class ResourceDiagnostic:
    field: str
    code: str
    message: str
    reference: Any
    severity: str = "error"

    @property
    def is_error(self) -> bool:
        """True only for a definite input/resource defect, not uncertainty."""
        return self.severity == "error"

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


class ResourcePreflight:
    """Reusable lazy catalogs for one explicitly selected source stack.

    Pass an existing AssetSourceStack to reuse GUI source ordering. A Path is
    resolved by discover_asset_sources; an explicit sequence retains its given
    low-to-high order. No persistent cache or filesystem output is created.
    Empty spawn/weather pools do not need asset access when their caller allows
    that semantic. Do not retain this instance after the selected assets change.
    """

    def __init__(
        self, assets: Path | Sequence[Path] | AssetSourceStack | None = None, *,
        asset_sources: Iterable[Path] | None = None, universe_folder: Path | None = None,
    ):
        self._stack = assets if isinstance(assets, AssetSourceStack) else None
        self._assets = assets
        self._sources = list(asset_sources) if asset_sources is not None else None
        self._universe_folder = universe_folder
        self._weather_names: set[str] | None = None
        self._spawn_names: set[str] | None = None
        self._image_dimensions: dict[str, tuple[int, int]] = {}
        self._frames: dict[str, _Frames | None] = {}

    @property
    def stack(self) -> AssetSourceStack:
        if self._stack is None:
            if self._sources is not None:
                sources = self._sources
            elif isinstance(self._assets, (str, os.PathLike)):
                sources = discover_asset_sources(Path(self._assets), self._universe_folder)
            elif self._assets is not None:
                sources = list(self._assets)
            else:
                sources = []
            if not sources:
                raise AssetCatalogError("Select the game's assets before validating resource names or images")
            self._stack = AssetSourceStack(sources)
        return self._stack

    def weather_type_names(self) -> set[str]:
        if self._weather_names is None:
            names: set[str] = set()
            for path in self.stack.paths(".weather"):
                definition = self.stack.json(path)
                if definition is None:  # Native scanFiles skips null assets.
                    continue
                _add_catalog_name(names, definition, path, "Weather")
            self._weather_names = names
        return set(self._weather_names)

    def spawn_type_names(self) -> set[str]:
        if self._spawn_names is None:
            names: set[str] = set()
            for path in self.stack.paths(".spawntypes"):
                definitions = self.stack.json(path)
                if not isinstance(definitions, list):
                    raise AssetCatalogError(f"Spawn type asset {path} must contain an array")
                for definition in definitions:
                    _add_catalog_name(names, definition, path, "Spawn type")
            self._spawn_names = names
        return set(self._spawn_names)

    def validate_weather_pool(
        self, value: Any, *, label: str = "Weather pool", allow_empty: bool = False,
    ) -> list[dict[str, Any]]:
        if value == [] and allow_empty:
            return []
        try:
            normalized = swe.weather_pool_from_editor(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"{label}: {exc}") from exc
        try:
            known = self.weather_type_names()
        except (AssetCatalogError, FileNotFoundError) as exc:
            raise ValueError(f"{label}: resource verification is incomplete: {exc}") from exc
        for index, entry in enumerate(normalized):
            name = entry["item"]
            if name not in known:
                raise ValueError(f"{label}[{index}].item: unknown weather type {name!r} in the selected assets")
        return normalized

    def validate_spawn_profile(self, value: Any, *, label: str = "spawnProfile") -> dict[str, Any] | None:
        try:
            profile = normalize_spawn_profile(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"{label}: {exc}") from exc
        if profile is None:
            return None
        validate_monster_parameters(profile.get("monsterParameters"), label=f"{label}.monsterParameters")
        if profile["spawnTypes"]:
            try:
                known = self.spawn_type_names()
            except (AssetCatalogError, FileNotFoundError) as exc:
                raise ValueError(f"{label}.spawnTypes: resource verification is incomplete: {exc}") from exc
            for index, name in enumerate(profile["spawnTypes"]):
                if name not in known:
                    raise ValueError(f"{label}.spawnTypes[{index}]: unknown spawnType {name!r} in the selected assets")
        return profile

    def check_image(
        self, reference: Any, *, label: str = "Image", required_frames: Iterable[str] | None = None,
    ) -> list[ResourceDiagnostic]:
        """Diagnose base PNG and native frame lookup, without rendering.

        is_error distinguishes definite failures from script/directive/catalog
        uncertainty. Callers decide whether a field blocks or requires warning
        confirmation. An unchanged legacy field can be excluded by the caller.
        required_frames checks native string concatenation (for example
        twinkle's dynamic ':0'..':N-1'). Existing frame-name prefixes may contain
        colons. Directives remain uncertain, even if the game appends a suffix.
        """
        def diagnostic(code: str, message: str, *, warning: bool = False) -> ResourceDiagnostic:
            return ResourceDiagnostic(label, code, message, reference, "warning" if warning else "error")

        try:
            path, frame, directives = _image_reference(reference)
        except ValueError as exc:
            return [diagnostic("invalid_image_path", str(exc))]
        if required_frames is not None:
            requested = list(required_frames)
            for name in requested:
                if not isinstance(name, str) or not name:
                    return [diagnostic("invalid_frame", "Frame names must be non-empty strings")]
                issues = self.check_image(f"{reference}:{name}", label=label)
                if issues:
                    return [ResourceDiagnostic(issue.field, issue.code, issue.message, reference, issue.severity)
                            for issue in issues]
            return [] if requested else self.check_image(reference, label=label)
        try:
            width, height = self._png_dimensions(path)
            frames = self._frames_for_image(path)
            requested = [] if frame is None else [frame]
            if requested and frames is None:
                return [diagnostic("missing_frames", f"No associated .frames file exists for {path!r}")]
            for name in requested:
                if not isinstance(name, str) or not name:
                    return [diagnostic("invalid_frame", "Frame names must be non-empty strings")]
                assert frames is not None
                rect = frames.rect(name)
                if rect is None:
                    return [diagnostic("missing_frame", f"No frame {name!r} exists in {frames.path!r} for {path!r}")]
                x0, y0, x1, y1 = rect
                if not (0 <= x0 <= x1 <= width and 0 <= y0 <= y1 <= height):
                    return [diagnostic("frame_out_of_bounds", f"Frame {name!r} rectangle {list(rect)} lies outside PNG dimensions {width}x{height}")]
                if x0 == x1 or y0 == y1:
                    # Grid size zero is native-legal; subImage returns an empty
                    # image rather than throwing (StarImage.cpp 424-436).
                    return [diagnostic("empty_image_frame", f"Frame {name!r} is native-legal but has no visible pixels", warning=True)]
        except FileNotFoundError as exc:
            return [diagnostic("missing_image", str(exc))]
        except _InvalidImage as exc:
            return [diagnostic("invalid_image", str(exc))]
        except _InvalidFrames as exc:
            return [diagnostic("invalid_frames", str(exc))]
        except (AssetCatalogError, OSError) as exc:
            return [diagnostic("image_unverified", f"Resource verification is incomplete: {exc}", warning=True)]
        if directives:
            return [diagnostic("image_directives_unverified", "Base PNG and requested frame exist, but image directives were not executed or validated", warning=True)]
        return []

    def check_system_images(self, raw: Mapping[str, Any], *, fields: Iterable[str] | None = None) -> list[ResourceDiagnostic]:
        """Check selected central-star fields; omitted fields are not invented."""
        selected = set(fields) if fields is not None else {"image", "twinkleFrames", "twinkleBackground"}
        result: list[ResourceDiagnostic] = []
        for key in ("image", "twinkleBackground", "twinkleFrames"):
            if key not in selected or key not in raw:
                continue
            if key != "twinkleFrames":
                result.extend(self.check_image(raw[key], label=f"System.{key}"))
                continue
            try:
                count = self.stack.json("/celestial.config:twinkleFrames")
                if type(count) not in (int, float):
                    raise AssetCatalogError("/celestial.config:twinkleFrames must be a positive integer")
                count = native_integer(count, label="/celestial.config:twinkleFrames", minimum=1, maximum=4096)
            except (ValueError, FileNotFoundError, OSError) as exc:
                result.extend(self.check_image(raw[key], label="System.twinkleFrames"))
                result.append(ResourceDiagnostic("System.twinkleFrames", "twinkle_count_unverified",
                    f"Cannot verify all dynamic twinkle frames: {exc}", raw[key], "warning"))
            else:
                result.extend(self.check_image(raw[key], label="System.twinkleFrames",
                                               required_frames=(str(index) for index in range(count))))
        return result

    def _png_dimensions(self, path: str) -> tuple[int, int]:
        key = path.casefold()
        if key not in self._image_dimensions:
            if not key.endswith(".png"):
                raise AssetCatalogError(f"Only PNG headers are supported by image preflight: {path}")
            data = self.stack.read_bytes(path)
            if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[8:16] != b"\0\0\0\rIHDR":
                raise _InvalidImage(f"Not a PNG with a complete IHDR header: {path}")
            width, height = struct.unpack(">II", data[16:24])
            if not width or not height or width > 0x7fffffff or height > 0x7fffffff:
                raise _InvalidImage(f"PNG dimensions are invalid: {path}")
            self._image_dimensions[key] = width, height
        return self._image_dimensions[key]

    def _frames_for_image(self, path: str) -> _Frames | None:
        key = path.casefold()
        if key not in self._frames:
            available = {name.casefold(): name for name in self.stack.paths(".frames")}
            directory = posixpath.dirname(path)
            stem = posixpath.splitext(posixpath.basename(path))[0]
            found = None
            while directory:
                for candidate in (posixpath.join(directory, stem + ".frames"), posixpath.join(directory, "default.frames")):
                    if candidate.casefold() in available:
                        found = available[candidate.casefold()]
                        break
                if found or directory == "/":
                    break
                directory = posixpath.dirname(directory)
            self._frames[key] = _Frames(self.stack.json(found), found) if found is not None else None
        return self._frames[key]


def _add_catalog_name(names: set[str], definition: Any, path: str, kind: str) -> None:
    if not isinstance(definition, dict):
        raise AssetCatalogError(f"{kind} asset {path} must contain objects")
    name = definition.get("name")
    if not isinstance(name, str) or not name or name != name.strip():
        raise AssetCatalogError(f"{kind} asset {path} contains an invalid name {name!r}")
    if name in names:
        raise AssetCatalogError(f"Duplicate {kind.lower()} named {name!r} in {path}")
    names.add(name)


def _image_reference(value: Any) -> tuple[str, str | None, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Enter a non-empty absolute asset image path without surrounding whitespace")
    reference, _separator, directives = value.partition("?")
    path, separator, frame = reference.partition(":")
    if not path.startswith("/") or path.startswith("//") or "\\" in path or ".." in path.split("/"):
        raise ValueError("Image paths must be absolute game assets such as /celestial/star.png; not disk, relative, or parent-traversal paths")
    if any(ord(character) < 32 for character in value):
        raise ValueError("Image path contains a control character")
    # StarAssetPath.cpp 39-48: only the FIRST colon is structural. Later
    # colons are part of the frame name; a trailing empty colon has no frame.
    return path, frame if separator and frame else None, directives


class _InvalidImage(ValueError):
    pass


class _InvalidFrames(ValueError):
    pass


class _Frames:
    """Native frameList/grid/alias lookup without enumerating huge grids."""

    def __init__(self, config: Any, path: str):
        self.path = path
        self.frames: dict[str, tuple[int, int, int, int]] = {}
        self.aliases: dict[str, str] = {}
        self.automatic_grid: tuple[int, int, int, int, int, int] | None = None
        try:
            if not isinstance(config, dict):
                raise ValueError("frames definition must be an object")
            listing = config.get("frameList", {})
            if not isinstance(listing, dict):
                raise ValueError("frameList must be an object")
            for name, value in listing.items():
                coordinates = _flatten_rect(value, f"frameList.{name}")
                rect = tuple(_frame_integer(item) for item in coordinates)
                if rect[0] >= rect[2] or rect[1] >= rect[3]:
                    raise ValueError(f"frameList.{name} has an empty rectangle")
                self.frames[name] = rect
            if "frameGrid" in config:
                grid = config["frameGrid"]
                if not isinstance(grid, dict):
                    raise ValueError("frameGrid must be an object")
                begin = _frame_pair(grid.get("begin", [0, 0]))
                size = _frame_pair(grid.get("size"))
                dimensions = _frame_pair(grid.get("dimensions"))
                if not all(dimensions):
                    raise ValueError("frameGrid dimensions must be positive")
                if "names" not in grid:
                    self.automatic_grid = (*begin, *size, *dimensions)
                else:
                    rows = grid["names"]
                    if not isinstance(rows, list) or len(rows) > dimensions[1]:
                        raise ValueError("frameGrid.names rows exceed grid dimensions")
                    for y, row in enumerate(rows):
                        if row is None:
                            continue
                        if not isinstance(row, list) or len(row) > dimensions[0]:
                            raise ValueError("frameGrid.names columns exceed grid dimensions")
                        for x, name in enumerate(row):
                            if name is None or name == "":
                                continue
                            if not isinstance(name, str):
                                raise ValueError("frameGrid.names entries must be strings or null")
                            x0, y0 = begin[0] + x * size[0], begin[1] + y * size[1]
                            self.frames[name] = (x0, y0, x0 + size[0], y0 + size[1])
            aliases = config.get("aliases", {})
            if aliases is None:
                aliases = {}
            if not isinstance(aliases, dict):
                raise ValueError("aliases must be an object")
            for name, target in aliases.items():
                seen = {name}
                if not isinstance(target, str):
                    raise ValueError(f"alias {name!r} target must be a string")
                while target in aliases:
                    if target in seen:
                        raise ValueError(f"alias cycle at {name!r}")
                    seen.add(target)
                    target = aliases[target]
                    if not isinstance(target, str):
                        raise ValueError(f"alias {name!r} target must be a string")
                if self.rect(target) is None:
                    raise ValueError(f"alias {name!r} refers to missing frame {target!r}")
                self.aliases[name] = target
        except (ValueError, TypeError, OverflowError) as exc:
            raise _InvalidFrames(f"Invalid frames specification {path}: {exc}") from exc

    def rect(self, name: str) -> tuple[int, int, int, int] | None:
        name = self.aliases.get(name, name)
        if self.automatic_grid is not None and len(name) <= 20 and name.isascii() and name.isdigit():
            # Native auto-names use exact decimal strings, without leading zeroes.
            index = int(name)
            bx, by, sx, sy, dx, dy = self.automatic_grid
            if str(index) == name and index < dx * dy:
                x0, y0 = bx + index % dx * sx, by + index // dx * sy
                return x0, y0, x0 + sx, y0 + sy
        return self.frames.get(name)


def _frame_integer(value: Any) -> int:
    # Asset data is consumed with getInt rather than editor input parsing.
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("frame coordinates must be finite JSON numbers")
    result = int(value)
    if result < 0 or result > 0x7fffffff:
        raise ValueError("frame coordinates must fit non-negative int32")
    return result


def _frame_pair(value: Any) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("frame grid vectors must contain two coordinates")
    return _frame_integer(value[0]), _frame_integer(value[1])
