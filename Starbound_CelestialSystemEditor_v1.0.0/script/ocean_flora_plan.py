"""Pure planning for private native ocean-floor vegetation pregeneration.

This module never launches native processes or writes worlds. Viewports are not
an exact generation boundary: Starbound loads neighbouring sectors itself. The
caller must retain its reset-sector mask when merging a private native result.
"""
from __future__ import annotations

import copy
import math
from typing import Any

import starbound_world_editor as swe


DEFAULT_SHORE_FLORA_WIDTH = 600


class OceanFloraPlanError(ValueError):
    pass


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise OceanFloraPlanError(f"{label} must be a finite number")
    return float(value)


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    number = _number(value, label)
    if number != int(number) or number < minimum:
        raise OceanFloraPlanError(f"{label} must be an integer >= {minimum}")
    return int(number)


def _world_size(document: dict[str, Any]) -> tuple[int, int]:
    size = document.get("size")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise OceanFloraPlanError("World metadata must contain [width, height]")
    return _integer(size[0], "World width", 1), _integer(size[1], "World height", 1)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for left, right in sorted(ranges):
        if merged and left <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def shore_flora_ranges(
    rectangle: dict[str, Any], width: int,
    shore_width_tiles: int = DEFAULT_SHORE_FLORA_WIDTH,
) -> list[tuple[int, int]]:
    """Return inclusive, non-wrapping X ranges within each actual solid shore.

    A width of 600 includes the shore's first ocean column and its next 599
    columns. Compute distances in the original contiguous ocean interval before
    splitting at world X=0, so a wrap seam never invents an additional shore.
    Opposing ranges in a narrow ocean overlap/abut and are returned only once.
    Shore flags are mandatory booleans; no solid shore means no supplementation.
    """
    world_width = _integer(width, "World width", 1)
    distance = _integer(shore_width_tiles, "Shore flora width", 1)
    if not isinstance(rectangle, dict):
        raise OceanFloraPlanError("Ocean rectangle must be an object")
    start = _integer(rectangle.get("xStart"), "Ocean xStart")
    end = _integer(rectangle.get("xEnd"), "Ocean xEnd")
    if start >= world_width or end >= world_width:
        raise OceanFloraPlanError("Ocean rectangle exceeds world width")
    profile = rectangle.get("coastalBasinProfile")
    if not isinstance(profile, dict):
        raise OceanFloraPlanError("Coastal profile must be an object")
    for field in ("leftSolidShore", "rightSolidShore"):
        if not isinstance(profile.get(field), bool):
            raise OceanFloraPlanError(f"{field} must be boolean")
    length = (end-start) % world_width + 1
    logical_ranges = []
    if profile["leftSolidShore"]:
        logical_ranges.append((0, min(distance, length)-1))
    if profile["rightSolidShore"]:
        logical_ranges.append((max(0, length-distance), length-1))
    ranges = []
    for left, right in _merge_ranges(logical_ranges):
        first = (start+left) % world_width
        last = first + (right-left)
        if last < world_width:
            ranges.append((first, last))
        else:
            ranges.extend(((first, world_width-1), (0, last-world_width)))
    return _merge_ranges(ranges)


def _profile_samples(rectangle: dict[str, Any], profile: dict[str, Any], width: int) -> list[dict[str, Any]]:
    """Use explicit samples, or faithfully reconstruct the current core schema."""
    explicit = profile.get("samples")
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit:
            raise OceanFloraPlanError("Coastal profile samples must be a nonempty list")
        samples = copy.deepcopy(explicit)
    else:
        required = ("effectiveSampleWidth", "leftSolidShore", "rightSolidShore", "profileSegments",
                    "minimumBaseHeight", "maximumBaseHeight", "floorReliefSafetyClearance")
        if any(key not in profile for key in required):
            raise OceanFloraPlanError("Coastal profile has no samples or complete reconstruction metadata")
        step = _integer(profile["effectiveSampleWidth"], "Effective basin sample width", 1)
        for field in ("leftSolidShore", "rightSolidShore"):
            if not isinstance(profile[field], bool):
                raise OceanFloraPlanError(f"{field} must be boolean")
        floor = _number(rectangle.get("oceanFloorHeight"), "Floor terrain height")
        water = _number(rectangle.get("oceanLiquidLevel"), "Ocean liquid level")
        clearance = _number(profile["floorReliefSafetyClearance"], "Floor relief safety clearance")
        samples = swe._coastal_basin_profile_segments(
            rectangle["xStart"], rectangle["xEnd"], width, floor, water, step,
            profile["leftSolidShore"], profile["rightSolidShore"], water - clearance,
            _integer(profile.get("shoreIslandExclusionWidth", 0), "Shore exclusion width"),
        )
        if len(samples) != _integer(profile["profileSegments"], "Profile segment count", 1):
            raise OceanFloraPlanError("Coastal profile reconstruction changed its segment count; regenerate with complete profile metadata")
        for key, actual in (("minimumBaseHeight", min(s["baseHeight"] for s in samples)),
                            ("maximumBaseHeight", max(s["baseHeight"] for s in samples))):
            if abs(_number(profile[key], key) - actual) > 0.02:
                raise OceanFloraPlanError(f"Coastal profile reconstruction disagrees with {key}")
    for sample in samples:
        if not isinstance(sample, dict):
            raise OceanFloraPlanError("Each coastal profile sample must be an object")
        left = _integer(sample.get("xStart"), "Profile sample xStart")
        right = _integer(sample.get("xEnd"), "Profile sample xEnd")
        if not 0 <= left <= right < width:
            raise OceanFloraPlanError("Profile samples must be non-wrapping intervals inside world width")
        _number(sample.get("startHeight"), "Profile start height")
        _number(sample.get("endHeight"), "Profile end height")
    samples.sort(key=lambda sample: sample["xStart"])
    expected_ranges = swe._wrapped_x_ranges(rectangle["xStart"], rectangle["xEnd"], width)
    expected_ranges.sort()
    cursor = 0
    for left, right in expected_ranges:
        current = left
        while cursor < len(samples) and samples[cursor]["xStart"] <= right:
            sample = samples[cursor]
            if sample["xStart"] != current or sample["xEnd"] > right:
                raise OceanFloraPlanError("Profile samples overlap, contain gaps, or escape the selected ocean")
            current = sample["xEnd"] + 1
            cursor += 1
        if current != right + 1:
            raise OceanFloraPlanError("Profile samples do not cover the entire ocean interval")
    if cursor != len(samples):
        raise OceanFloraPlanError("Profile samples escape the selected ocean")
    return samples


def _height_envelope(samples: list[dict[str, Any]], left: int, right: int, relief: float) -> tuple[float, float]:
    bounds = []
    for sample in samples:
        begin, end = max(left, sample["xStart"]), min(right, sample["xEnd"])
        if begin > end:
            continue
        span = sample["xEnd"] - sample["xStart"]
        slope = (sample["endHeight"] - sample["startHeight"]) / span if span else 0.0
        base_begin = sample["startHeight"] + slope * (begin - sample["xStart"])
        base_end = sample["startHeight"] + slope * (end - sample["xStart"])
        # Rotate selectors rotate the source relief too. A relief distance in
        # selector Y projects to a larger world-Y interval on a steep slope.
        projected_relief = relief * math.sqrt(1 + slope * slope)
        bounds.append((min(base_begin, base_end) - projected_relief,
                       max(base_begin, base_end) + projected_relief))
    if not bounds:
        raise OceanFloraPlanError("No profile samples intersect a requested viewport")
    return min(b[0] for b in bounds), max(b[1] for b in bounds)


def plan_ocean_flora_windows(
    document: dict[str, Any], ocean_summary: dict[str, Any], *,
    max_window_width: int = 600, max_window_height: int = 512,
    margin_tiles: int = 64, max_windows: int = 512, max_target_sectors: int = 50000,
    shore_width_tiles: int = DEFAULT_SHORE_FLORA_WIDTH,
) -> dict[str, Any]:
    """Plan bounded windows only along the first 600 columns at each solid shore.

    Supports the current compact ``coastalBasinProfile`` summaries and explicit
    ``samples`` with xStart/xEnd/startHeight/endHeight. Bounds include compiled
    relief projected through the local slope, requested ecology height and the
    extra margin. They are conservative planning bounds, not a proof that every
    possible prefab/plant extent lies inside the viewport. The complete basin
    curve is still reconstructed and validated; only extra flora's X scope is
    restricted. Deep-ocean curve terrain/normal generation are unchanged.
    """
    width, height = _world_size(document)
    window_width = _integer(max_window_width, "Maximum window width", 32) // 32 * 32
    window_height = _integer(max_window_height, "Maximum window height", 32) // 32 * 32
    margin = _integer(margin_tiles, "Viewport margin")
    limit = _integer(max_windows, "Maximum viewport count", 1)
    sector_limit = _integer(max_target_sectors, "Maximum target sectors", 1)
    shore_width = _integer(shore_width_tiles, "Shore flora width", 1)
    rectangles = ocean_summary.get("rectangles", [])
    if not isinstance(rectangles, list):
        raise OceanFloraPlanError("Ocean summary rectangles must be an array")
    windows: list[list[int]] = []
    seen_windows: set[tuple[int, ...]] = set()
    sectors: set[tuple[int, int]] = set()
    profiles = []
    all_shore_ranges = []
    for index, original in enumerate(rectangles):
        if not isinstance(original, dict):
            raise OceanFloraPlanError("Each ocean summary rectangle must be an object")
        profile = original.get("coastalBasinProfile")
        if not profile or original.get("parallaxOnly"):
            continue
        if not isinstance(profile, dict):
            raise OceanFloraPlanError("Coastal profile must be an object")
        if profile.get("enabled") is False:
            continue
        if profile.get("enabled") is not True:
            raise OceanFloraPlanError("Coastal profile enabled flag must be true or false")
        rectangle = dict(original)
        for field in ("xStart", "xEnd", "yStart", "yEnd"):
            rectangle[field] = _integer(rectangle.get(field), field)
        if rectangle["xStart"] >= width or rectangle["xEnd"] >= width:
            raise OceanFloraPlanError("Ocean rectangle exceeds world width")
        if not 0 <= rectangle["yStart"] <= rectangle["yEnd"] < height:
            raise OceanFloraPlanError("Ocean rectangle exceeds world height or has reversed Y bounds")
        water = _number(rectangle.get("oceanLiquidLevel"), "Ocean liquid level")
        if not 0 <= water <= height:
            raise OceanFloraPlanError("Ocean liquid level must remain within world height")
        relief = _number(profile.get("estimatedFloorTerrainRelief"), "Estimated floor terrain relief")
        if relief < 0:
            raise OceanFloraPlanError("Estimated floor terrain relief cannot be negative")
        ecology = _integer(profile.get("floorEcologyHeight"), "Floor ecology height")
        samples = _profile_samples(rectangle, profile, width)
        if any(not 0 <= sample[key] <= height for sample in samples for key in ("startHeight", "endHeight")):
            raise OceanFloraPlanError("Coastal profile terrain heights must remain within world height")
        before = len(windows)
        shore_ranges = shore_flora_ranges(rectangle, width, shore_width)
        all_shore_ranges.extend(shore_ranges)
        selected_y0 = rectangle["yStart"]
        selected_y1 = rectangle["yEnd"] + 1
        for first, last in shore_ranges:
            x0 = first // 32 * 32
            x1 = min(width, (last // 32 + 1) * 32)
            for grid_left in range(x0, x1, window_width):
                # Interior edges are sector aligned; exact outer selection
                # and 600-column edges are clipped without changing the curve.
                left = max(first, grid_left)
                right_after = min(last+1, grid_left + window_width)
                low, high = _height_envelope(samples, max(first, left), min(last, right_after-1), relief)
                bottom = max(selected_y0, int(math.floor(low - margin)) // 32 * 32)
                top = min(selected_y1, (int(math.ceil(min(high + ecology, water) + margin)) + 31) // 32 * 32)
                if top <= bottom:
                    raise OceanFloraPlanError("Basin envelope does not intersect its selected rectangle")
                for y0 in range(bottom, top, window_height):
                    y1 = min(top, y0 + window_height)
                    window = (left, y0, right_after-left, y1-y0)
                    if window in seen_windows:
                        continue
                    if len(windows) >= limit:
                        raise OceanFloraPlanError(f"Ocean vegetation pregeneration needs more than {limit} windows; reduce selected scope or explicitly raise the limit")
                    seen_windows.add(window)
                    windows.append(list(window))
                    for sx in range(left//32, (right_after-1)//32+1):
                        for sy in range(y0//32, (y1-1)//32+1):
                            sectors.add((sx, sy))
                            if len(sectors) > sector_limit:
                                raise OceanFloraPlanError(f"Ocean vegetation pregeneration exceeds {sector_limit} target sectors; reduce selected scope or explicitly raise the limit")
        profiles.append({"rectangleIndex": index, "profileSegments": len(samples),
                         "viewportCount": len(windows)-before, "floorEcologyHeight": ecology,
                         "estimatedSelectorRelief": relief, "shoreWidthTiles": shore_width,
                         "shoreXRanges": [list(r) for r in shore_ranges]})
    return {"worldSize": [width, height], "windows": windows, "viewportCount": len(windows),
            "targetSectorCount": len(sectors), "targetTileCapacity": len(sectors)*32*32,
            "targetSectors": [list(s) for s in sorted(sectors)], "profiles": profiles,
            "shoreWidthTiles": shore_width,
            "shoreXRanges": [list(r) for r in _merge_ranges(all_shore_ranges)],
            "sectorAlignedExceptSelectionEdges": True,
            "warnings": ["Native viewports generate neighbouring sectors outside these rectangles. Merge only permitted reset sectors; do not install uncontrolled padding into an existing world.",
                         "Window bounds include the compiled relief estimate, ecology height and margin; arbitrary prefab/plant extents are not mathematically bounded by this estimate.",
                         "Sector alignment can extend a selection by up to 31 tiles; the last world-edge sector may be partial. Enforce shoreXRanges again when importing plants so sector padding cannot bypass the shore-width limit."]}


def generation_seed_snapshot(document: dict[str, Any]) -> dict[str, Any]:
    """Pure native-seed/material-hue guard; not a replacement for layout checks.

    Record actual seed fields recursively under worldTemplate, plus each biome's
    material identity/hue declarations. Native generation must use these values,
    never a fixed replacement plant arrangement. Full metadata comparison and
    protected-sector byte comparison remain the caller's responsibility.
    """
    template = swe.world_template(document)
    seeds: dict[str, Any] = {}
    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key.casefold().endswith("seed"):
                    seeds[child_path] = copy.deepcopy(child)
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + "/" + str(index))
    visit(template, "/worldTemplate")
    appearances = []
    for biome in swe.compiled_biomes(document):
        appearances.append({key: copy.deepcopy(biome[key]) for key in
                            ("baseName", "mainBlock", "subBlocks", "materialHueShift", "hueShift") if key in biome})
    return {"worldSize": list(_world_size(document)), "seedFields": seeds, "biomeMaterialAppearance": appearances}
