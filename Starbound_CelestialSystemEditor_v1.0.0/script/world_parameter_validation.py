"""Native-safe validation and branch-local staging for the 03 world form.

No filesystem writes, window creation, or game/native execution. Source anchors
refer to work/openstarbound_source/source: StarWorldParameters.cpp:31-32,109-126
and .hpp:54-55,124; StarSkyTypes.cpp:26-53; StarJsonExtra.cpp:212-233;
StarColor.cpp:29-50,221-230,612-635. These distinguish valid JSON from values
which native float32/int32/uint32/color constructors can actually consume.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import struct
from typing import Any, Mapping, Sequence


UINT32_MAX = 0xffffffff
INT32_MAX = 0x7fffffff
SKY_COLOR_KEYS = (
    "mainColor", "morningColors", "dayColors", "eveningColors", "nightColors",
    "morningLightColor", "dayLightColor", "eveningLightColor", "nightLightColor",
)
SKY_GRADIENT_KEYS = frozenset({"morningColors", "dayColors", "eveningColors", "nightColors"})
NATIVE_COLOR_NAMES = frozenset({
    "red", "orange", "yellow", "green", "blue", "indigo", "violet", "black", "white",
    "magenta", "darkmagenta", "cyan", "darkcyan", "cornflowerblue", "gray", "lightgray",
    "darkgray", "darkgreen", "pink", "clear",
})


def native_float32(
    value: Any, *, label: str = "Value", positive: bool = False, nonnegative: bool = False
) -> float:
    """Keep input precision, but ensure its native float32 value is safe."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite native 32-bit floating-point number")
    try:
        original = Decimal(str(value).strip())
        if not original.is_finite():
            raise ValueError()
        number = float(value)
        native = struct.unpack("!f", struct.pack("!f", number))[0]
    except (TypeError, ValueError, InvalidOperation, OverflowError, struct.error) as exc:
        raise ValueError(f"{label} must fit a finite native 32-bit floating-point number") from exc
    if not math.isfinite(number) or not math.isfinite(native):
        raise ValueError(f"{label} must fit a finite native 32-bit floating-point number")
    if positive and (original <= 0 or native <= 0):
        raise ValueError(f"{label} must stay positive when stored as a native 32-bit float")
    if nonnegative and (original < 0 or native < 0):
        raise ValueError(f"{label} must be non-negative")
    # Any nonzero input becoming zero is lossy, even for signed fields such
    # as magnitude/hue. Explicit zero remains allowed unless positive=True.
    if original != 0 and native == 0:
        raise ValueError(f"{label} is too small for a non-zero native 32-bit float")
    return number


def native_integer(value: Any, *, label: str, minimum: int = 0, maximum: int = INT32_MAX) -> int:
    """Parse whole numeric text without float rounding of a large integer."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}") from exc
    if not number.is_finite() or number != number.to_integral_value() or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}")
    return int(number)


def native_station_orbit(radius: Any, angle: Any, direction: Any, *,
                         radius_label: str = "Station orbit radius",
                         angle_label: str = "Station orbit angle",
                         direction_label: str = "Station orbit direction") -> tuple[float, float, int]:
    """Validate the Vec2F magnitude arithmetic used by SystemWorld::orbitPosition.

    A finite scalar float32 is insufficient: the two squared components and
    their float32 sum must remain finite AND nonzero before sqrt. This tests
    the actual requested direction instead of imposing an invented radius cap.
    """
    radius_value = native_float32(radius, label=radius_label, positive=True)
    angle_value = native_float32(angle, label=angle_label)
    direction_value = native_integer(direction, label=direction_label, minimum=-1, maximum=1)
    if direction_value not in (-1, 1):
        raise ValueError(f"{direction_label}: -1 / 1")
    radians = math.radians(angle_value)
    def f32(value: float) -> float:
        return struct.unpack("!f", struct.pack("!f", value))[0]
    try:
        x = f32(radius_value * math.cos(radians))
        y = f32(radius_value * math.sin(radians))
        squared = f32(f32(x * x) + f32(y * y))
    except (OverflowError, struct.error) as exc:
        raise ValueError(f"{radius_label}: native float32 x*x + y*y must stay finite and > 0") from exc
    if not math.isfinite(squared) or squared <= 0:
        raise ValueError(f"{radius_label}: native float32 x*x + y*y must stay finite and > 0")
    return radius_value, angle_value, direction_value


def world_dimensions(width_text: str, height_text: str) -> tuple[int, int]:
    """Use the established canonical-integer UI policy, plus native width."""
    values = []
    for label, text in (("World width", width_text), ("World height", height_text)):
        text = str(text).strip()
        number = native_integer(text, label=label, minimum=1, maximum=UINT32_MAX)
        if str(number) != text:
            raise ValueError(f"{label} must be written as a whole integer")
        values.append(number)
    return values[0], values[1]


def validate_native_color(value: Any, *, label: str) -> Any:
    """Accept native Color strings or lossless 8-bit RGB/RGBA arrays."""
    if isinstance(value, str):
        if value.casefold() in NATIVE_COLOR_NAMES or re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", value):
            return value
        raise ValueError(f"{label} must be a native named color or #RGB/#RGBA/#RRGGBB/#RRGGBBAA")
    if not isinstance(value, list) or len(value) not in (3, 4):
        raise ValueError(f"{label} must be an RGB/RGBA array or native Color string")
    # Native getInt/uint8 casts truncate or wrap; reject newly entered values
    # whose meaning would change during that conversion.
    normalized = []
    for index, component in enumerate(value):
        if type(component) not in (int, float):
            raise ValueError(f"{label}[{index}] must be a whole color component in 0..255")
        normalized.append(native_integer(component, label=f"{label}[{index}]", maximum=255))
    return normalized


def validate_sky_value(key: str, value: Any) -> Any:
    if key not in SKY_COLOR_KEYS:
        raise ValueError(f"Unknown editable sky field: {key}")
    if key in SKY_GRADIENT_KEYS:
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError(f"{key} must contain exactly two native colors")
        return [validate_native_color(color, label=f"{key}[{index}]") for index, color in enumerate(value)]
    return validate_native_color(value, label=key)


def native_clear_sky() -> dict[str, Any]:
    """SkyColoring() defaults, not invented weather/biome preset colors."""
    return {key: [[0, 0, 0, 0], [0, 0, 0, 0]] if key in SKY_GRADIENT_KEYS else [0, 0, 0, 0]
            for key in SKY_COLOR_KEYS}


def sky_updates(existing: Any, fields: Mapping[str, str], *, terrestrial: bool) -> dict[str, Any] | None:
    """Blank means retain; native required keys must never be deleted.

    A non-terrestrial source may legitimately omit the complete sky object.
    With no new color input, preserve that omission. Creating its first custom
    sky begins with the native SkyColoring() clear defaults, then applies input.
    Native terrestrial sky objects require all nine keys; no per-key fallback
    exists in their disk constructor. Unknown existing extension keys survive.
    """
    parsed = {}
    for key, text in fields.items():
        if key not in SKY_COLOR_KEYS:
            raise ValueError(f"Unknown editable sky field: {key}")
        if str(text).strip():
            try:
                value = json.loads(str(text).strip())
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must contain valid JSON") from exc
            parsed[key] = validate_sky_value(key, value)
    if existing is None and not terrestrial and not parsed:
        return None
    if isinstance(existing, dict):
        candidate = dict(existing)
    elif not terrestrial and existing is None:
        candidate = native_clear_sky()
    else:
        raise ValueError("skyColoring must contain a complete native sky object")
    candidate.update(parsed)
    for key in SKY_COLOR_KEYS:
        if key not in candidate:
            raise ValueError(f"Native skyColoring requires {key}; blank preserves but cannot create a missing value")
        validate_sky_value(key, candidate[key])
    return candidate


def shallow_parameter_candidate(parameters: dict[str, Any]) -> dict[str, Any]:
    """Copy only writable owner dictionaries; large untouched data is shared."""
    candidate = dict(parameters)
    raw, visitable = parameters.get("parameters"), parameters.get("visitableParameters")
    if not isinstance(raw, dict) or not isinstance(visitable, dict):
        raise ValueError("A visitable body needs parameters and visitableParameters objects")
    candidate["parameters"] = dict(raw)
    candidate["visitableParameters"] = dict(visitable)
    return candidate


def stage_layer_edits(parameters: dict[str, Any], edits: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """Clone only changed layer owners; never commit before whole-form checks."""
    import starbound_world_editor as swe

    candidate = shallow_parameter_candidate(parameters)
    visitable = candidate["visitableParameters"]
    underground_copied = False
    for layer_id, values in edits.items():
        if not isinstance(layer_id, str):
            raise ValueError("Layer identifier must be text")
        if layer_id.startswith("underground:"):
            suffix = layer_id.split(":", 1)[1]
            if not re.fullmatch(r"[0-9]+", suffix):
                raise ValueError("Underground layer index must be a non-negative integer")
        if len(values) != 4:
            raise ValueError("A layer edit must contain minimum/base/dungeons/variance")
        minimum_text, base_text, dungeon_text, variance_text = values
        minimum = native_integer(minimum_text, label="Layer minimum")
        base = native_integer(base_text, label="Layer base")
        if base < minimum:
            raise ValueError("Layer base height must be at or above its minimum height")
        original_layer = swe._celestial_layer(parameters, layer_id)
        layer = dict(original_layer)
        if layer_id.startswith("underground:"):
            if not underground_copied:
                visitable["undergroundLayers"] = list(visitable["undergroundLayers"])
                underground_copied = True
            index = int(layer_id.split(":", 1)[1])
            visitable["undergroundLayers"][index] = layer
        else:
            key = {"core": "coreLayer", "surface": "surfaceLayer", "subsurface": "subsurfaceLayer",
                   "atmosphere": "atmosphereLayer", "space": "spaceLayer"}[layer_id]
            visitable[key] = layer
        try:
            dungeons = json.loads(dungeon_text or "[]")
        except (TypeError, ValueError) as exc:
            raise ValueError("Layer dungeons must be a valid JSON array") from exc
        if not isinstance(dungeons, list) or any(not isinstance(value, str) or not value.strip() for value in dungeons):
            raise ValueError("Every layer dungeon must be a non-empty string in a JSON array")
        if str(variance_text).strip():
            variance = native_integer(variance_text, label="Dungeon X variance")
        else:
            if "dungeonXVariance" not in original_layer:
                raise ValueError("Dungeon X variance is required by native saved layer data")
            variance = native_integer(original_layer["dungeonXVariance"], label="Dungeon X variance")
        layer.update(layerMinHeight=minimum, layerBaseHeight=base,
                     dungeons=[name.strip() for name in dungeons], dungeonXVariance=variance)
    return candidate


def validate_native_layer_numbers(parameters: dict[str, Any]) -> None:
    import starbound_world_editor as swe

    visitable = parameters["visitableParameters"]
    if visitable.get("type") != "TerrestrialWorldParameters":
        return
    # UI layer groups deliberately omit empty region collections, but their
    # saved layer dictionaries still reach native integer readers.
    layers = [(layer_id, visitable[key]) for layer_id, key in (
        ("core", "coreLayer"), ("surface", "surfaceLayer"), ("subsurface", "subsurfaceLayer"),
        ("atmosphere", "atmosphereLayer"), ("space", "spaceLayer"),
    ) if key in visitable]
    underground = visitable.get("undergroundLayers", [])
    if not isinstance(underground, list):
        raise ValueError("Native undergroundLayers must be an array")
    layers.extend((f"underground:{index}", layer) for index, layer in enumerate(underground))
    for layer_id, layer in layers:
        if not isinstance(layer, dict):
            raise ValueError(f"Native layer {layer_id} must be an object")
        for field in ("layerMinHeight", "layerBaseHeight", "dungeonXVariance"):
            if field not in layer:
                raise ValueError(f"Native layer {layer_id} requires {field}")
            native_integer(layer[field], label=layer_id + "." + field)


def preset_dimensions(parameters: dict[str, Any], assets: Path, size_name: str) -> tuple[int, int]:
    """Get preset dimensions without installing a regenerated/default recipe."""
    import starbound_world_editor as swe

    probe = shallow_parameter_candidate(parameters)
    # The existing preset function reads nested recipes and replaces only its
    # owner dicts; all borrowed nested source branches remain read-only.
    swe.resize_celestial_terrestrial_body(probe, Path(assets), size_name)
    size = probe["visitableParameters"]["worldSize"]
    return world_dimensions(str(size[0]), str(size[1]))


def validate_active_ocean_levels(parameters: dict[str, Any]) -> None:
    """A resized active ocean must not extend above the new world ceiling."""
    import starbound_world_editor as swe

    height = parameters["visitableParameters"]["worldSize"][1]
    for group in swe.celestial_layer_groups(parameters):
        for region in swe.celestial_biome_region_objects(parameters, group["layerId"], group["group"]):
            if region.get("oceanLiquid"):
                level = native_integer(region.get("oceanLiquidLevel"), label="Ocean liquid level")
                if level > height:
                    raise ValueError(f"Active ocean liquid level {level} exceeds resized world height {height}")
