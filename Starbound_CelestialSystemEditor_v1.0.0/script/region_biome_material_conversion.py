"""Convert saved natural terrain materials to one biome palette.

This operation deliberately does *not* edit world layout, biome indexes,
terrain shape, tile mods, liquids, or entity records.  A saved material is
eligible only when it is the mainBlock or one of the subBlocks of that tile's
saved block biome.  Its existing main/sub-block role is mapped to the selected
biome's material palette, preserving the clump arrangement already generated
by Starbound.

Hard boundaries use the exact inclusive rectangle.  Fuzzy boundaries sample
the serialized world's native blockNoise and blendNoise coordinate distortion
on a four-tile grid and interpolate it, a bounded source-derived version of the
first stage used by WorldTemplate::blockBiomeInfo.  The target mask can
consequently include saved tiles just outside the nominal rectangle and exclude
saved tiles just inside it, without changing the layout itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Any, Mapping, Sequence
import zlib

import region_tile_replacement as tile_replacement
import starbound_world_editor as swe
import world_edit_guard


LAYERS = tile_replacement.LAYERS
_U32_MASK = (1 << 32) - 1
_U64_MASK = (1 << 64) - 1
_PERLIN_SAMPLE_SIZE = 512


def _semantic_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _RandomSource:
    """Source-compatible multiply-with-carry generator used by StarPerlin."""

    def __init__(self, seed: int):
        seed &= _U64_MASK
        self.carry = seed % 809430660
        self.data = [seed & _U32_MASK, (seed >> 32) & _U32_MASK]
        for index in range(2, 256):
            self.data.append(
                (69069 * self.data[index - 2] + 362437) & _U32_MASK
            )
        self.index = 255
        for _index in range(32):
            self.gen32()

    def gen32(self) -> int:
        self.index = (self.index + 1) & 255
        value = 809430660 * self.data[self.index] + self.carry
        self.carry = value >> 32
        self.data[self.index] = value & _U32_MASK
        return self.data[self.index]

    def randu64(self) -> int:
        return (self.gen32() << 32) | self.gen32()

    def rand_uint(self, maximum: int) -> int:
        if type(maximum) is not int or maximum < 0:
            raise ValueError("Random upper bound must be a non-negative integer")
        denominator = _U64_MASK // (maximum + 1)
        return self.randu64() // denominator

    def rand_int(self, minimum: int, maximum: int) -> int:
        if maximum < minimum:
            raise ValueError("Random maximum must not be below minimum")
        return self.rand_uint(maximum - minimum) + minimum


class _Perlin:
    """Scalar StarPerlin evaluator for saved world boundary noise.

    Python uses double precision while native WorldTemplate uses PerlinF.
    This can move the vanishingly small set of samples exactly on an integer
    threshold by one tile; the algorithm, seeds, gradients and interpolation
    otherwise follow StarPerlin.hpp.  The UI therefore calls this
    "Starbound-style" rather than claiming pointwise engine identity.
    """

    def __init__(self, config: Mapping[str, Any], label: str):
        if not isinstance(config, Mapping):
            raise swe.WorldFormatError(f"{label} is not a saved Perlin object")
        kind = str(config.get("type", "")).casefold()
        aliases = {
            "perlin": "perlin", "billow": "billow",
            "ridgedmulti": "ridgedmulti", "ridged multi": "ridgedmulti",
        }
        if kind not in aliases:
            raise swe.WorldFormatError(f"{label} has unsupported Perlin type {config.get('type')!r}")
        self.kind = aliases[kind]
        self.octaves = config.get("octaves", 1)
        self.seed = config.get("seed")
        if type(self.octaves) is not int or not 0 <= self.octaves <= 64:
            raise swe.WorldFormatError(f"{label}.octaves must be an integer from 0 to 64")
        if type(self.seed) is not int or not -(1 << 63) <= self.seed <= _U64_MASK:
            raise swe.WorldFormatError(f"{label}.seed must fit a serialized 64-bit integer")
        # Stored Variant integers are signed; Json::getUInt restores the same
        # underlying 64 bits for native Perlin construction.
        self.seed &= _U64_MASK
        for field, default in (
            ("frequency", 1.0), ("amplitude", 1.0), ("bias", 0.0),
            ("alpha", 2.0), ("beta", 2.0), ("offset", 1.0), ("gain", 2.0),
        ):
            value = config.get(field, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise swe.WorldFormatError(f"{label}.{field} must be finite")
            setattr(self, field, float(value))
        if self.octaves > 1 and self.alpha == 0.0:
            raise swe.WorldFormatError(f"{label}.alpha cannot be zero with multiple octaves")

        random_source = _RandomSource(self.seed)
        size = _PERLIN_SAMPLE_SIZE
        total = size + size + 2
        self.p = [0] * total
        self.g1 = [0.0] * total
        self.g2 = [[0.0, 0.0] for _index in range(total)]
        self.g3 = [[0.0, 0.0, 0.0] for _index in range(total)]
        for index in range(size):
            self.p[index] = index
            self.g1[index] = random_source.rand_int(-size, size) / size
            vector2 = [random_source.rand_int(-size, size) / size for _axis in range(2)]
            vector3 = [random_source.rand_int(-size, size) / size for _axis in range(3)]
            self.g2[index] = self._normalize(vector2)
            self.g3[index] = self._normalize(vector3)
        for index in range(size - 1, 0, -1):
            other = random_source.rand_uint(size - 1)
            self.p[index], self.p[other] = self.p[other], self.p[index]
        for index in range(size + 2):
            self.p[size + index] = self.p[index]
            self.g1[size + index] = self.g1[index]
            self.g2[size + index] = list(self.g2[index])
            self.g3[size + index] = list(self.g3[index])

    @staticmethod
    def _normalize(values: list[float]) -> list[float]:
        length = math.sqrt(sum(value * value for value in values))
        if length == 0.0:
            return [1.0] + [0.0] * (len(values) - 1)
        return [value / length for value in values]

    @staticmethod
    def _setup(value: float) -> tuple[int, int, float, float, float]:
        integer = math.floor(value)
        relative0 = value - integer
        relative1 = relative0 - 1.0
        curve = relative0 * relative0 * (3.0 - 2.0 * relative0)
        return integer & 511, (integer + 1) & 511, relative0, relative1, curve

    @staticmethod
    def _lerp(amount: float, left: float, right: float) -> float:
        return left + amount * (right - left)

    def _noise1(self, x: float) -> float:
        bx0, bx1, rx0, rx1, sx = self._setup(x)
        left = rx0 * self.g1[self.p[bx0]]
        right = rx1 * self.g1[self.p[bx1]]
        return self._lerp(sx, left, right)

    def _noise2(self, x: float, y: float) -> float:
        bx0, bx1, rx0, rx1, sx = self._setup(x)
        by0, by1, ry0, ry1, sy = self._setup(y)
        first, second = self.p[bx0], self.p[bx1]
        b00, b10 = self.p[first + by0], self.p[second + by0]
        b01, b11 = self.p[first + by1], self.p[second + by1]

        def at(index: int, dx: float, dy: float) -> float:
            gradient = self.g2[index]
            return dx * gradient[0] + dy * gradient[1]

        lower = self._lerp(sx, at(b00, rx0, ry0), at(b10, rx1, ry0))
        upper = self._lerp(sx, at(b01, rx0, ry1), at(b11, rx1, ry1))
        return self._lerp(sy, lower, upper)

    def _noise3(self, x: float, y: float, z: float) -> float:
        bx0, bx1, rx0, rx1, sx = self._setup(x)
        by0, by1, ry0, ry1, sy = self._setup(y)
        bz0, bz1, rz0, rz1, sz = self._setup(z)
        first, second = self.p[bx0], self.p[bx1]
        b00, b10 = self.p[first + by0], self.p[second + by0]
        b01, b11 = self.p[first + by1], self.p[second + by1]

        def at(index: int, dx: float, dy: float, dz: float) -> float:
            gradient = self.g3[index]
            return dx * gradient[0] + dy * gradient[1] + dz * gradient[2]

        def plane(bz: int, dz: float) -> float:
            lower = self._lerp(
                sx, at(b00 + bz, rx0, ry0, dz), at(b10 + bz, rx1, ry0, dz)
            )
            upper = self._lerp(
                sx, at(b01 + bz, rx0, ry1, dz), at(b11 + bz, rx1, ry1, dz)
            )
            return self._lerp(sy, lower, upper)

        return self._lerp(sz, plane(bz0, rz0), plane(bz1, rz1))

    def get(self, *coordinates: float) -> float:
        if len(coordinates) not in (1, 2, 3):
            raise ValueError("Perlin expects one, two or three coordinates")
        point = [float(value) * self.frequency for value in coordinates]
        total = 0.0
        scale = 1.0
        weight = 1.0
        for _octave in range(self.octaves):
            if len(point) == 1:
                value = self._noise1(point[0])
            elif len(point) == 2:
                value = self._noise2(point[0], point[1])
            else:
                value = self._noise3(point[0], point[1], point[2])
            if self.kind == "billow":
                value = 2.0 * abs(value) - 1.0
            elif self.kind == "ridgedmulti":
                value = self.offset - abs(value)
                value = value * value * weight
                weight = max(0.0, min(1.0, value * self.gain))
            total += value / scale
            scale *= self.alpha
            point = [value * self.beta for value in point]
        if self.kind == "ridgedmulti":
            total = total * 1.25 - 1.0
        elif self.kind == "billow":
            total += 0.5
        return total * self.amplitude + self.bias

    def conservative_bound(self, dimensions: int) -> float:
        scale = 1.0
        octave_sum = 0.0
        for _octave in range(self.octaves):
            octave_sum += 1.0 / abs(scale)
            scale *= self.alpha
        # s_curve interpolation weights stay within 0..1, so the result cannot
        # exceed the largest unit-gradient dot product at a lattice corner.
        raw_bound = math.sqrt(float(dimensions))
        if self.kind == "billow":
            transformed_bound = max(1.0, 2.0 * raw_bound - 1.0)
            return abs(self.bias) + abs(self.amplitude) * (
                0.5 + transformed_bound * octave_sum
            )
        if self.kind == "ridgedmulti":
            transformed_bound = max(
                self.offset * self.offset,
                (self.offset - raw_bound) * (self.offset - raw_bound),
            )
            return abs(self.bias) + abs(self.amplitude) * (
                1.0 + 1.25 * transformed_bound * octave_sum
            )
        return abs(self.bias) + abs(self.amplitude) * raw_bound * octave_sum


class _FuzzyBoundary:
    GRID = 4

    def __init__(self, document: dict[str, Any], area: dict[str, Any]):
        width, height = (int(value) for value in document["size"])
        region = swe.world_template(document).get("regionData", {})
        block_noise = region.get("blockNoise")
        if not isinstance(block_noise, dict):
            raise ValueError(
                "This world has no saved blockNoise; Starbound-style fuzzy boundaries "
                "are unavailable. Turn off fuzzy boundary for an exact rectangle."
            )
        self.width, self.height, self.area = width, height, area
        self.horizontal = _Perlin(block_noise.get("horizontalNoise"), "blockNoise.horizontalNoise")
        self.vertical = _Perlin(block_noise.get("verticalNoise"), "blockNoise.verticalNoise")
        self.x_noise = _Perlin(block_noise.get("xNoise"), "blockNoise.xNoise")
        self.y_noise = _Perlin(block_noise.get("yNoise"), "blockNoise.yNoise")
        blend_noise = region.get("blendNoise")
        self.blend = _Perlin(blend_noise, "blendNoise") if isinstance(blend_noise, dict) else None
        self._circle_cache: dict[int, tuple[float, float]] = {}
        self._horizontal_cache: dict[int, float] = {}
        self._vertical_cache: dict[int, float] = {}
        self._offset_cache: dict[tuple[int, int], tuple[float, float]] = {}
        self.x_margin = int(math.ceil(
            self.horizontal.conservative_bound(1)
            + self.x_noise.conservative_bound(3)
            + (self.blend.conservative_bound(2) if self.blend is not None else 0.0)
        )) + 2
        self.y_margin = int(math.ceil(
            self.vertical.conservative_bound(2) + self.y_noise.conservative_bound(3)
        )) + 2

    def _offset(self, x: int, y: int) -> tuple[float, float]:
        x %= self.width
        y = max(0, min(self.height, y))
        key = (x, y)
        cached = self._offset_cache.get(key)
        if cached is not None:
            return cached
        circle = self._circle_cache.get(x)
        if circle is None:
            angle = (x / float(self.width)) * 2.0 * math.pi
            circle = (
                math.sin(angle) / (2.0 * math.pi) * self.width,
                math.cos(angle) / (2.0 * math.pi) * self.width,
            )
            self._circle_cache[x] = circle
        circle_x, circle_z = circle
        horizontal = self._horizontal_cache.get(y)
        if horizontal is None:
            horizontal = self.horizontal.get(y)
            self._horizontal_cache[y] = horizontal
        vertical = self._vertical_cache.get(x)
        if vertical is None:
            vertical = self.vertical.get(circle_x, circle_z)
            self._vertical_cache[x] = vertical
        x_offset = horizontal + self.x_noise.get(circle_x, y, circle_z)
        y_offset = vertical + self.y_noise.get(circle_x, y, circle_z)
        if self.blend is not None:
            # C++ conversion from float to int truncates toward zero.
            x_offset += int(self.blend.get(x, y))
        result = (x_offset, y_offset)
        self._offset_cache[key] = result
        return result

    def selected(self, x: int, y: int) -> bool:
        grid = self.GRID
        x0 = (x // grid) * grid
        x1 = x0 + grid
        y0 = (y // grid) * grid
        y1 = min(self.height, y0 + grid)
        tx = (x - x0) / float(grid)
        ty = 0.0 if y1 == y0 else (y - y0) / float(y1 - y0)
        dx00, dy00 = self._offset(x0, y0)
        dx10, dy10 = self._offset(x1, y0)
        dx01, dy01 = self._offset(x0, y1)
        dx11, dy11 = self._offset(x1, y1)
        dx0 = dx00 + tx * (dx10 - dx00)
        dx1 = dx01 + tx * (dx11 - dx01)
        dy0 = dy00 + tx * (dy10 - dy00)
        dy1 = dy01 + tx * (dy11 - dy01)
        noisy_x = math.floor(x + dx0 + ty * (dx1 - dx0))
        noisy_y = math.floor(y + dy0 + ty * (dy1 - dy0))
        noisy_y = max(0, min(self.height, noisy_y))
        noisy_x %= self.width
        return (
            any(start <= noisy_x <= end for start, end in self.area["xRanges"])
            and self.area["yStart"] <= noisy_y <= self.area["yEnd"]
        )


def _expanded_x_ranges(
    ranges: Sequence[tuple[int, int]], width: int, margin: int
) -> list[tuple[int, int]]:
    if sum(end - start + 1 for start, end in ranges) >= width or margin * 2 + 1 >= width:
        return [(0, width - 1)]
    points: list[tuple[int, int]] = []
    for start, end in ranges:
        raw_start, raw_end = start - margin, end + margin
        if raw_start < 0:
            points.append((raw_start % width, width - 1))
            points.append((0, raw_end))
        elif raw_end >= width:
            points.append((raw_start, width - 1))
            points.append((0, raw_end % width))
        else:
            points.append((raw_start, raw_end))
    merged: list[list[int]] = []
    for start, end in sorted(points):
        start, end = max(0, start), min(width - 1, end)
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _contracted_x_ranges(area: Mapping[str, Any], width: int, margin: int) -> list[tuple[int, int]]:
    selected_length = sum(end - start + 1 for start, end in area["xRanges"])
    if selected_length >= width:
        return [(0, width - 1)]
    if selected_length <= 2 * margin:
        return []
    start = (area["xStart"] + margin) % width
    end = (area["xEnd"] - margin) % width
    return [(start, end)] if start <= end else [(start, width - 1), (0, end)]


def _contains(area: Mapping[str, Any], x: int, y: int) -> bool:
    return (
        area["yStart"] <= y <= area["yEnd"]
        and any(start <= x <= end for start, end in area["xRanges"])
    )


def _material_palette(biome: Mapping[str, Any], label: str) -> list[int]:
    main = biome.get("mainBlock")
    sub_blocks = biome.get("subBlocks", [])
    if type(main) is not int or not 0 <= main < tile_replacement.FIRST_META_MATERIAL_ID:
        raise swe.WorldFormatError(f"{label}.mainBlock is not a placeable material ID")
    if not isinstance(sub_blocks, list) or any(
        type(value) is not int or not 0 <= value < tile_replacement.FIRST_META_MATERIAL_ID
        for value in sub_blocks
    ):
        raise swe.WorldFormatError(f"{label}.subBlocks contains an invalid material ID")
    # Native lists can theoretically repeat a material.  First occurrence is
    # the only observable role in a saved tile, so remove later duplicates.
    return list(dict.fromkeys([main] + sub_blocks))


def load_target_biome_palette(
    assets: Path, target_biome: str
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], str]:
    if not isinstance(target_biome, str) or not target_biome.strip():
        raise ValueError("Choose a target biome")
    requested = target_biome.strip()
    biome_catalog = swe.load_asset_biome_catalog(Path(assets))
    exact = biome_catalog.get("biomes", {}).get(requested)
    name = requested
    if exact is None:
        matches = [
            candidate for candidate in biome_catalog.get("biomes", {})
            if candidate.casefold() == requested.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(f"The selected Assets do not contain biome {requested!r}")
        name = matches[0]
        exact = biome_catalog["biomes"][name]
    if not isinstance(exact, dict):
        raise ValueError(f"Biome {name!r} is not a JSON object")
    main_name = exact.get("mainBlock")
    sub_names = exact.get("subBlocks", [])
    if not isinstance(main_name, str) or not main_name:
        raise ValueError(
            f"Biome {name!r} has no concrete mainBlock and cannot be used for saved terrain conversion"
        )
    if not isinstance(sub_names, list) or any(type(value) is not str or not value for value in sub_names):
        raise ValueError(f"Biome {name!r}.subBlocks must be an array of material names")
    material_catalog = tile_replacement.load_block_item_catalog(Path(assets))
    palette_items = [tile_replacement.resolve_block_item(material_catalog, main_name)]
    palette_items.extend(
        tile_replacement.resolve_block_item(material_catalog, value) for value in sub_names
    )
    unique_items: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for item in palette_items:
        if item["materialId"] not in seen_ids:
            seen_ids.add(item["materialId"])
            unique_items.append(item)
    palette = {
        "biomeName": name,
        "biomePath": biome_catalog.get("paths", {}).get(name),
        "mainMaterial": unique_items[0],
        "subMaterials": unique_items[1:],
        "materialIds": [item["materialId"] for item in unique_items],
        "materialNames": [item["materialName"] for item in unique_items],
    }
    by_id = {item["materialId"]: item for item in material_catalog.values()}
    signature = _semantic_digest({
        "biomeName": name,
        "mainBlock": main_name,
        "subBlocks": sub_names,
        "materials": [
            {
                key: item[key]
                for key in (
                    "materialId", "materialName", "materialPath", "collisionKind",
                    "falling", "cascading", "foregroundOnly", "supportsMods",
                    "liquidInteractions", "materialDefinitionSignature",
                )
            }
            for item in unique_items
        ],
    })
    return palette, by_id, signature


def _role_maps(
    compiled_biomes: Sequence[Mapping[str, Any]], target_ids: Sequence[int]
) -> tuple[dict[int, dict[int, int]], list[dict[str, Any]]]:
    maps: dict[int, dict[int, int]] = {}
    descriptions: list[dict[str, Any]] = []
    for biome_index, biome in enumerate(compiled_biomes, start=1):
        try:
            source = _material_palette(biome, f"compiled biome {biome_index}")
        except swe.WorldFormatError as exc:
            # Empty/space carrier biomes can legitimately use a meta material
            # rather than a placeable terrain palette.  They are not natural
            # block candidates and are left untouched if a saved tile refers
            # to them.
            descriptions.append({
                "biomeIndex": biome_index,
                "biomeName": biome.get("baseName"),
                "sourceMaterialIds": [],
                "mapping": {},
                "skippedReason": str(exc),
            })
            continue
        mapping = {
            material_id: target_ids[min(role, len(target_ids) - 1)]
            for role, material_id in enumerate(source)
        }
        maps[biome_index] = mapping
        descriptions.append({
            "biomeIndex": biome_index,
            "biomeName": biome.get("baseName"),
            "sourceMaterialIds": source,
            "mapping": {str(source_id): target_id for source_id, target_id in mapping.items()},
        })
    return maps, descriptions


def _physics_warnings(
    role_maps: Mapping[int, Mapping[int, int]],
    material_by_id: Mapping[int, dict[str, Any]],
    layer: str,
) -> list[str]:
    if layer not in LAYERS:
        raise ValueError("Layer must be 'both', 'foreground', or 'background'")
    pairs = {
        (source_id, target_id)
        for mapping in role_maps.values()
        for source_id, target_id in mapping.items()
        if source_id != target_id
    }
    target_ids = {target for _source, target in pairs}
    missing = sorted(
        {value for pair in pairs for value in pair if value not in material_by_id}
    )
    if missing:
        raise ValueError(
            "Selected Assets do not define material IDs used by the world's natural biome palette: "
            + ", ".join(str(value) for value in missing)
        )
    if layer != "foreground":
        names = sorted({
            material_by_id[target]["materialName"] for target in target_ids
            if material_by_id[target]["foregroundOnly"]
        })
        if names:
            raise ValueError(
                "These target biome materials are foreground-only; select foreground only: "
                + ", ".join(names)
            )
    if layer != "background":
        mismatches = sorted({
            (
                material_by_id[source]["materialName"],
                material_by_id[source]["collisionKind"],
                material_by_id[target]["materialName"],
                material_by_id[target]["collisionKind"],
            )
            for source, target in pairs
            if material_by_id[source]["collisionKind"] != material_by_id[target]["collisionKind"]
        })
        if mismatches:
            source, source_kind, target, target_kind = mismatches[0]
            raise ValueError(
                f"Cannot safely convert foreground collision kind {source_kind!r} ({source}) "
                f"to {target_kind!r} ({target}). Choose background only or a biome with "
                "compatible solid materials."
            )
    warnings: list[str] = []
    if layer != "background" and any(
        material_by_id[target]["falling"] or material_by_id[target]["cascading"]
        for target in target_ids
    ):
        warnings.append(
            "The target palette contains falling/cascading materials. The game can move "
            "them later and detach supported objects or plants."
        )
    if any(not material_by_id[target]["supportsMods"] for target in target_ids):
        warnings.append(
            "Some target materials do not support tile mods. Existing grass/ore/mod bytes "
            "are preserved, but the game can hide or remove unsupported mods."
        )
    if any(material_by_id[target]["liquidInteractions"] for target in target_ids):
        warnings.append(
            "Some target materials have liquid interactions and can transform after the "
            "world runs."
        )
    return warnings


def convert_biome_material_rectangle_records(
    records: Sequence[tuple[bytes, bytes]],
    document: dict[str, Any],
    target_palette: Mapping[str, Any],
    rectangle: Mapping[str, Any],
    layer: str = "both",
    fuzzy_boundary: bool = False,
) -> tuple[list[tuple[bytes, bytes]], dict[str, Any]]:
    """Pure saved-record conversion; only foreground/background IDs can change."""
    if layer not in LAYERS:
        raise ValueError("Layer must be 'both', 'foreground', or 'background'")
    if type(fuzzy_boundary) is not bool:
        raise ValueError("fuzzy_boundary must be true or false")
    width, height = (int(value) for value in document["size"])
    area = tile_replacement._rectangle(rectangle, width, height)
    target_ids = target_palette.get("materialIds")
    if not isinstance(target_ids, list) or not target_ids:
        raise ValueError("Target biome material palette is empty")
    target_ids = [
        tile_replacement._material_id(value, "Target biome") for value in target_ids
    ]
    role_maps, role_descriptions = _role_maps(swe.compiled_biomes(document), target_ids)
    fuzzy = _FuzzyBoundary(document, area) if fuzzy_boundary else None
    x_margin = fuzzy.x_margin if fuzzy is not None else 0
    y_margin = fuzzy.y_margin if fuzzy is not None else 0
    candidate_x_ranges = _expanded_x_ranges(area["xRanges"], width, x_margin)
    candidate_y_start = max(0, area["yStart"] - y_margin)
    candidate_y_end = min(height - 1, area["yEnd"] + y_margin)
    certain_area = {
        "xRanges": _contracted_x_ranges(area, width, x_margin),
        "yStart": area["yStart"] + y_margin,
        "yEnd": area["yEnd"] - y_margin,
    }
    selected_sector_x = {
        sector
        for start, end in candidate_x_ranges
        for sector in range(start // 32, end // 32 + 1)
    }
    expected_sectors = len(selected_sector_x) * (
        candidate_y_end // 32 - candidate_y_start // 32 + 1
    )
    nominal_tiles = sum(
        end - start + 1 for start, end in area["xRanges"]
    ) * (area["yEnd"] - area["yStart"] + 1)
    stats: dict[str, Any] = {
        "rectangle": area,
        "layer": layer,
        "fuzzyBoundary": fuzzy_boundary,
        "fuzzyAlgorithm": "savedWorldBlockNoiseAndBlendNoise" if fuzzy_boundary else None,
        "fuzzySamplingGrid": fuzzy.GRID if fuzzy is not None else None,
        "fuzzyXMargin": x_margin,
        "fuzzyYMargin": y_margin,
        "candidateXRanges": candidate_x_ranges,
        "candidateYStart": candidate_y_start,
        "candidateYEnd": candidate_y_end,
        "nominalTiles": nominal_tiles,
        "savedTilesScanned": 0,
        "selectedSavedTiles": 0,
        "skippedNullBiomeTiles": 0,
        "fuzzyIncludedOutsideTiles": 0,
        "fuzzyExcludedInsideTiles": 0,
        "savedSectorsScanned": 0,
        "missingSectors": 0,
        "changedSectors": 0,
        "eligibleNaturalForeground": 0,
        "eligibleNaturalBackground": 0,
        "foregroundTiles": 0,
        "backgroundTiles": 0,
        "changedTileLayers": 0,
        "generatedSectors": 0,
        "preservedNonMaterialBytes": True,
        "preservedMetadataBytes": True,
        "preservedEntityBytes": True,
        "roleMappings": role_descriptions,
    }
    output: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
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
        if (
            sector_x not in selected_sector_x
            or not candidate_y_start // 32 <= sector_y <= candidate_y_end // 32
        ):
            output.append((key, compressed))
            continue
        try:
            raw = bytearray(zlib.decompress(compressed))
        except zlib.error as exc:
            raise swe.WorldFormatError(
                f"Tile sector {key.hex()} cannot be decompressed: {exc}"
            ) from exc
        _version, offsets = swe.tile_sector_offsets(raw, key)
        stats["savedSectorsScanned"] += 1
        sector_changed = False
        for local_y in range(32):
            world_y = sector_y * 32 + local_y
            if not candidate_y_start <= world_y <= candidate_y_end:
                continue
            for local_x in range(32):
                world_x = sector_x * 32 + local_x
                if world_x >= width or not any(
                    start <= world_x <= end for start, end in candidate_x_ranges
                ):
                    continue
                stats["savedTilesScanned"] += 1
                nominal = _contains(area, world_x, world_y)
                if fuzzy is None:
                    selected = nominal
                elif _contains(certain_area, world_x, world_y):
                    selected = True
                else:
                    selected = fuzzy.selected(world_x, world_y)
                if not selected:
                    if fuzzy is not None and nominal:
                        stats["fuzzyExcludedInsideTiles"] += 1
                    continue
                stats["selectedSavedTiles"] += 1
                if fuzzy is not None and not nominal:
                    stats["fuzzyIncludedOutsideTiles"] += 1
                tile_offset = offsets[local_y * 32 + local_x]
                biome_index = raw[tile_offset + 27]
                if biome_index == 0:
                    # Native NullBiomeIndex is valid, but has no natural-material
                    # palette. Do not guess provenance from nearby biome/hue.
                    stats["skippedNullBiomeTiles"] += 1
                    continue
                mapping = role_maps.get(biome_index)
                if mapping is None:
                    if not 1 <= biome_index <= len(role_descriptions):
                        raise swe.WorldFormatError(
                            f"Saved tile ({world_x}, {world_y}) has invalid block biome index {biome_index}"
                        )
                    continue
                for relative in LAYERS[layer]:
                    material_offset = tile_offset + relative
                    source_id = struct.unpack_from(">H", raw, material_offset)[0]
                    target_id = mapping.get(source_id)
                    if target_id is None:
                        continue
                    prefix = "foreground" if relative == 0 else "background"
                    stats["eligibleNatural" + prefix.capitalize()] += 1
                    if source_id != target_id:
                        struct.pack_into(">H", raw, material_offset, target_id)
                        stats[prefix + "Tiles"] += 1
                        sector_changed = True
        if sector_changed:
            stats["changedSectors"] += 1
            output.append((key, zlib.compress(raw, level=9)))
        else:
            output.append((key, compressed))
    stats["missingSectors"] = expected_sectors - stats["savedSectorsScanned"]
    stats["changedTileLayers"] = stats["foregroundTiles"] + stats["backgroundTiles"]
    stats["unchangedReason"] = (
        None if stats["changedTileLayers"] else
        "No eligible saved natural terrain material needs conversion in the selected mask"
    )
    return output, stats


def _prepare(
    world: Path,
    assets: Path,
    target_biome: str,
    rectangle: Mapping[str, Any],
    layer: str,
    fuzzy_boundary: bool,
    expected_fingerprint: Mapping[str, Any] | None,
    expected_asset_signature: str | None,
) -> tuple[Any, list[tuple[bytes, bytes]], dict[str, Any]]:
    world = Path(world)
    fingerprint = tile_replacement.world_file_fingerprint(world)
    if expected_fingerprint is not None:
        tile_replacement._assert_fingerprint(world, expected_fingerprint)
    palette, material_by_id, asset_signature = load_target_biome_palette(
        Path(assets), target_biome
    )
    if expected_asset_signature is not None and asset_signature != expected_asset_signature:
        raise RuntimeError(
            "The target biome/material assets changed after preview. Nothing was written; "
            "inspect and apply again."
        )
    database, records = swe.load_records(world)
    try:
        metadata = [value for key, value in records if key == swe.WORLD_METADATA_KEY]
        if len(metadata) != 1:
            raise swe.WorldFormatError("Expected exactly one WorldMetadata record")
        document = swe.decode_world_metadata(metadata[0])
        role_maps, _descriptions = _role_maps(
            swe.compiled_biomes(document), palette["materialIds"]
        )
        warnings = _physics_warnings(role_maps, material_by_id, layer)
        updated, summary = convert_biome_material_rectangle_records(
            records, document, palette, rectangle, layer, fuzzy_boundary
        )
        tile_replacement._assert_fingerprint(world, fingerprint)
        if summary["skippedNullBiomeTiles"]:
            warnings.append(
                f"{summary['skippedNullBiomeTiles']} selected saved tile(s) have native "
                "null block biome index 0. They are preserved because their natural "
                "material palette cannot be identified safely."
            )
        if summary["missingSectors"]:
            warnings.append(
                f"{summary['missingSectors']} candidate sector(s) are not saved/generated. "
                "Only existing saved tiles are converted; missing sectors stay absent."
            )
        warnings.append(
            "Only natural main/sub-block material IDs are changed. Terrain shape, biome "
            "indexes, hue/paint/mod bytes, liquids and every entity record are preserved."
        )
        summary.update({
            "world": str(world),
            "worldSize": list(document["size"]),
            "targetBiome": palette["biomeName"],
            "targetMaterialIds": palette["materialIds"],
            "targetMaterialNames": palette["materialNames"],
            "sourceFingerprint": fingerprint,
            "assetSignature": asset_signature,
            "warnings": warnings,
        })
        return database, updated, summary
    except Exception:
        swe.close_database(database)
        raise


def inspect_world_biome_material_rectangle(
    world: Path,
    assets: Path,
    target_biome: str,
    rectangle: Mapping[str, Any],
    layer: str = "both",
    fuzzy_boundary: bool = False,
) -> dict[str, Any]:
    """Read-only preview with exact saved change counts and a stale guard."""
    database, _records, summary = _prepare(
        Path(world), Path(assets), target_biome, rectangle, layer,
        fuzzy_boundary, None, None,
    )
    swe.close_database(database)
    return summary


def replace_world_biome_material_rectangle_in_place(
    world: Path,
    assets: Path,
    target_biome: str,
    rectangle: Mapping[str, Any],
    layer: str = "both",
    fuzzy_boundary: bool = False,
    *,
    keep_backup: bool = True,
    expected_fingerprint: Mapping[str, Any] | None = None,
    expected_asset_signature: str | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Back up, verify and atomically install a material-only conversion."""
    world = Path(world)
    database, records, summary = _prepare(
        world, Path(assets), target_biome, rectangle, layer, fuzzy_boundary,
        expected_fingerprint, expected_asset_signature,
    )
    backup: Path | None = None
    staged: Path | None = None
    try:
        if not summary["changedTileLayers"]:
            return None, summary
        fingerprint = summary["sourceFingerprint"]
        tile_replacement._assert_fingerprint(world, fingerprint)
        backup = swe._unique_world_edit_backup_path(world)
        shutil.copy2(world, backup)
        if tile_replacement.world_file_fingerprint(backup)["sha256"] != fingerprint["sha256"]:
            raise RuntimeError("The world changed during backup. Nothing was written to the source.")
        with tempfile.NamedTemporaryFile(
            prefix=world.name + ".", suffix=".biome-material-conversion",
            dir=world.parent, delete=False,
        ) as stream:
            staged = Path(stream.name)
        swe.atomic_write_world(backup, staged, records, database)
        swe.close_database(database)
        database = None
        tile_replacement._assert_fingerprint(world, fingerprint)
        world_edit_guard.assert_world_available(world)
        os.replace(staged, world)
        staged = None
        if not keep_backup:
            try:
                backup.unlink()
                backup = None
            except OSError:
                summary["warnings"].append(
                    "The conversion succeeded, but its optional backup could not be removed."
                )
        summary["outputFingerprint"] = tile_replacement.world_file_fingerprint(world)
        return backup, summary
    finally:
        if database is not None:
            swe.close_database(database)
        if staged is not None and staged.exists():
            staged.unlink()
