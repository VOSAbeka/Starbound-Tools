import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
import zlib

import starbound_world_editor as swe


class PathNamingTests(unittest.TestCase):
    def test_export_path_keeps_the_complete_world_filename(self):
        world = Path("example.world")
        self.assertEqual(
            swe.automatic_project_path(world), Path("tmp_example.world.json")
        )

    def test_import_path_removes_the_automatic_prefix_and_suffix(self):
        project = Path("tmp_example.world.json")
        self.assertEqual(
            swe.suggested_world_output_path(project), Path("example_edited.world")
        )

    def test_import_path_remains_compatible_with_old_project_names(self):
        project = Path("example.world.editable.json")
        self.assertEqual(
            swe.suggested_world_output_path(project), Path("example_edited.world")
        )


class SbonTests(unittest.TestCase):
    def test_dynamic_round_trip(self):
        value = {
            "nothing": None,
            "bools": [True, False],
            "integers": [0, 1, -1, 127, 128, -(2**63) + 1, 2**63 - 1],
            "float": 0.125,
            "text": "星界边境",
            "map": {"x": 1, "y": [2, 3]},
        }
        stream = io.BytesIO()
        swe.write_dynamic(stream, value)
        stream.seek(0)
        self.assertEqual(swe.read_dynamic(stream), value)
        self.assertEqual(stream.read(), b"")

    def test_versioned_json_round_trip(self):
        stream = io.BytesIO()
        swe.write_versioned_json(stream, "WorldMetadata", 26, {"seed": 123})
        stream.seek(0)
        self.assertEqual(
            swe.read_versioned_json(stream),
            ("WorldMetadata", 26, {"seed": 123}),
        )


class AssetCatalogTests(unittest.TestCase):
    @staticmethod
    def build_pak(path: Path, assets) -> None:
        offsets = []
        payload = bytearray()
        cursor = 16
        for asset_path, data in assets:
            offsets.append((asset_path, cursor, len(data)))
            payload.extend(data)
            cursor += len(data)

        index = io.BytesIO()
        index.write(swe.PAK_INDEX_MAGIC)
        metadata = {"name": "test", "priority": 0}
        swe.write_varuint(index, len(metadata))
        for key, value in metadata.items():
            swe.write_string(index, key)
            swe.write_dynamic(index, value)
        swe.write_varuint(index, len(offsets))
        for asset_path, offset, size in offsets:
            swe.write_string(index, asset_path)
            index.write(struct.pack(">QQ", offset, size))

        with path.open("wb") as stream:
            stream.write(swe.PAK_MAGIC)
            stream.write(struct.pack(">Q", cursor))
            stream.write(payload)
            stream.write(index.getvalue())

    def test_reads_unpacked_biome_folder_with_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            biome = root / "biomes" / "surface" / "test.biome"
            biome.parent.mkdir(parents=True)
            biome.write_text(
                '{\n// Starbound comment\n"name":"testbiome","mainBlock":"dirt"\n}',
                encoding="utf-8",
            )
            catalog = swe.load_asset_biome_catalog(root)
            self.assertEqual(catalog["mode"], "unpacked")
            self.assertEqual(list(catalog["biomes"]), ["testbiome"])

    def test_parent_of_unpacked_root_resolves_absolute_asset_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "assets"
            root = selected / "packed"

            def write(relative, value):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")

            write(
                "biomes/surface/oceanfloor.biome",
                {
                    "name": "oceanfloor",
                    "mainBlock": "sand",
                    "parallax": "/parallax/surface/oceanfloor.parallax",
                },
            )
            write(
                "parallax/surface/oceanfloor.parallax",
                {"verticalOrigin": 2, "layers": []},
            )
            write(
                "tiles/materials/sand.material",
                {"materialName": "sand", "materialId": 12},
            )
            write("spawning.config", {"spawnGroups": {}})

            reader = swe.StarboundAssetReader(selected)
            self.assertEqual(reader.root, root)
            compiler = swe.AssetBiomeCompiler(selected)
            compiled, warnings = compiler.compile(
                "oceanfloor", 123, 50.0, {"mainBlock": 1, "ores": []}
            )
            self.assertEqual(compiled["mainBlock"], 12)
            self.assertEqual(compiled["parallax"]["verticalOrigin"], 52.0)
            self.assertEqual(warnings, [])

    def test_reads_biomes_directly_from_packed_pak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_pak(
                root / "packed.pak",
                [
                    ("/readme.txt", b"ignored"),
                    (
                        "/biomes/surface/test.biome",
                        b'{/* comment */"name":"packedbiome","mainBlock":"dirt"}',
                    ),
                ],
            )
            catalog = swe.load_asset_biome_catalog(root)
            self.assertEqual(catalog["mode"], "packed.pak")
            self.assertEqual(list(catalog["biomes"]), ["packedbiome"])
            self.assertEqual(
                catalog["paths"]["packedbiome"],
                "/biomes/surface/test.biome",
            )

    def test_compiles_biome_missing_from_world_and_registers_new_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write(relative, value):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")

            write(
                "biomes/surface/new.biome",
                {
                    "name": "newbiome",
                    "mainBlock": "dirt",
                    "subBlocks": ["stone"],
                    "hueShiftOptions": [15],
                    "spawnProfile": {
                        "groups": [{"select": 1, "pool": [[1, "gleap"]]}],
                        "monsterParameters": {"aggressive": False},
                    },
                    "parallax": "/parallax/surface/new.parallax",
                    "surfacePlaceables": {
                        "items": [
                            {
                                "type": "microdungeon",
                                "microdungeons": ["newMicroDungeon"],
                                "distribution": {
                                    "type": "random",
                                    "blockProbability": 0.25,
                                },
                            }
                        ]
                    },
                },
            )
            write(
                "parallax/surface/new.parallax",
                {
                    "verticalOrigin": 10,
                    "layers": [
                        {
                            "kind": "test",
                            "parallax": [2, 3],
                            "offset": [4, 5],
                        }
                    ],
                },
            )
            write(
                "tiles/materials/dirt.material",
                {"materialName": "dirt", "materialId": 8},
            )
            write(
                "tiles/materials/stone.material",
                {"materialName": "stone", "materialId": 3},
            )
            write(
                "terrestrial_worlds.config",
                {
                    "regionDefaults": {
                        "blockSelector": ["solid"],
                        "fgCaveSelector": ["empty"],
                        "bgCaveSelector": ["empty"],
                        "fgOreSelector": ["empty"],
                        "bgOreSelector": ["empty"],
                        "subBlockSelector": ["largeClumps"],
                    },
                    "regionTypes": {
                        "newbiome": {"biome": [[0, ["newbiome"]]]}
                    },
                },
            )
            write("terrain/solid.terrain", {"name": "solid", "type": "constant", "value": 1})
            write("terrain/empty.terrain", {"name": "empty", "type": "constant", "value": -1})
            write(
                "terrain/large.ridgeblocks",
                {"name": "largeClumps", "amplitude": 1.0},
            )
            write("spawning.config", {"spawnGroups": {}})
            old_biome = {
                "baseName": "old",
                "mainBlock": 1,
                "subBlocks": [],
                "ores": [[10, 0.5]],
            }
            document = {
                "size": [100, 100],
                "metadata": {
                    "worldTemplate": {
                        "seed": 123,
                        "worldParameters": {
                            "surfaceLayer": {
                                "layerMinHeight": 0,
                                "layerBaseHeight": 50,
                            }
                        },
                        "regionData": {
                            "biomes": [old_biome],
                            "terrainSelectors": [{"type": "flat"}],
                            "layers": [
                                {
                                    "yStart": 0,
                                    "boundaries": [],
                                    "cells": [
                                        {
                                            "blockBiomeIndex": 1,
                                            "environmentBiomeIndex": 1,
                                            "terrainSelectorIndex": 0,
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                },
            }
            result = swe.set_asset_biome_rectangle(
                document, root, "newbiome", 10, 20, 10, 20
            )
            self.assertEqual(result["targetBiomeIndexes"], [2])
            biome = swe.biome_at(document, 2)
            self.assertEqual(biome["baseName"], "newbiome")
            self.assertEqual(biome["mainBlock"], 8)
            self.assertEqual(biome["subBlocks"], [3])
            self.assertEqual(biome["ores"], [])
            self.assertEqual(biome["spawnProfile"]["spawnTypes"], ["gleap"])
            self.assertEqual(biome["parallax"]["verticalOrigin"], 30.0)
            self.assertEqual(biome["parallax"]["layers"][0]["parallaxValue"], [2.0, 3.0])
            distribution = biome["surfacePlaceables"]["itemDistributions"][0]
            self.assertEqual(
                distribution["randomItems"],
                [["microDungeon", ["newMicroDungeon"]]],
            )
            region_data = document["metadata"]["worldTemplate"]["regionData"]
            selected_layer = next(
                layer for layer in region_data["layers"] if layer["yStart"] == 10
            )
            selected_cell = selected_layer["cells"][1]
            self.assertNotEqual(selected_cell["terrainSelectorIndex"], 0)
            self.assertEqual(selected_cell["blockBiomeIndex"], 2)

    def test_compiles_missing_biome_directly_from_packed_pak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            encode = lambda value: json.dumps(value).encode("utf-8")
            self.build_pak(
                root / "packed.pak",
                [
                    (
                        "/biomes/surface/new.biome",
                        encode(
                            {
                                "name": "packednew",
                                "mainBlock": "dirt",
                                "spawnProfile": {
                                    "groups": [
                                        {"select": 1, "pool": [[1, "gleap"]]}
                                    ]
                                },
                            }
                        ),
                    ),
                    (
                        "/tiles/materials/dirt.material",
                        encode({"materialName": "dirt", "materialId": 8}),
                    ),
                    ("/spawning.config", encode({"spawnGroups": {}})),
                ],
            )
            compiler = swe.AssetBiomeCompiler(root)
            compiled, warnings = compiler.compile(
                "packednew", 123, 50.0, {"mainBlock": 1, "ores": []}
            )
            self.assertEqual(compiled["baseName"], "packednew")
            self.assertEqual(compiled["mainBlock"], 8)
            self.assertEqual(compiled["spawnProfile"]["spawnTypes"], ["gleap"])
            self.assertEqual(warnings, [])

    def test_oceanfloor_reset_builds_floor_and_upper_ocean_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write(relative, value):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding="utf-8")

            write(
                "biomes/surface/oceanfloor.biome",
                {
                    "name": "oceanfloor",
                    "mainBlock": "sand",
                    "surfacePlaceables": {
                        "items": [
                            {
                                "type": "microdungeon",
                                "microdungeons": ["oceanMicro"],
                                "distribution": {
                                    "type": "random",
                                    "blockProbability": 0.3,
                                },
                            }
                        ]
                    },
                },
            )
            write(
                "biomes/surface/ocean.biome",
                {"name": "ocean", "mainBlock": "sand"},
            )
            write("tiles/sand.material", {"materialName": "sand", "materialId": 22})
            write("liquids/water.liquid", {"name": "water", "liquidId": 1})
            for name, value in (
                ("ledgesSurface", 1),
                ("remixedIslandsSurface", 1),
                ("empty", -1),
            ):
                write(
                    f"terrain/{name}.terrain",
                    {"name": name, "type": "constant", "value": value},
                )
            write(
                "terrain/large.ridgeblocks",
                {"name": "largeClumps", "amplitude": 1.0},
            )
            defaults = {
                "fgCaveSelector": ["empty"],
                "bgCaveSelector": ["empty"],
                "subBlockSelector": ["largeClumps"],
            }
            write(
                "terrestrial_worlds.config",
                {
                    "regionDefaults": defaults,
                    "regionTypes": {
                        "oceanfloor": {
                            "biome": [[0, ["oceanfloor"]]],
                            "oceanLiquid": ["water"],
                            "oceanLevelOffset": 1000,
                            "blockSelector": ["ledgesSurface"],
                        },
                        "ocean": {
                            "biome": [[0, ["ocean"]]],
                            "oceanLiquid": ["water"],
                            "blockSelector": ["remixedIslandsSurface"],
                        },
                    },
                },
            )
            write("spawning.config", {"spawnGroups": {}})
            document = {
                "size": [100, 100],
                "metadata": {
                    "worldTemplate": {
                        "seed": 99,
                        "worldParameters": {"threatLevel": 1},
                        "regionData": {
                            "biomes": [{"baseName": "old", "mainBlock": 1}],
                            "terrainSelectors": [{"type": "old"}],
                            "layers": [
                                {
                                    "yStart": 0,
                                    "boundaries": [],
                                    "cells": [
                                        {
                                            "blockBiomeIndex": 1,
                                            "environmentBiomeIndex": 1,
                                            "terrainSelectorIndex": 1,
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                },
            }
            result = swe.set_asset_biome_rectangle(
                document, root, "oceanfloor", 10, 20, 10, 80
            )
            self.assertEqual(result["oceanCompanionBiome"], "ocean")
            self.assertEqual(result["oceanFloorTransitionY"], 60)
            region_data = document["metadata"]["worldTemplate"]["regionData"]
            layers = {layer["yStart"]: layer for layer in region_data["layers"]}
            floor_cell = layers[10]["cells"][1]
            ocean_cell = layers[60]["cells"][1]
            self.assertEqual(floor_cell["oceanLiquidLevel"], 80)
            self.assertEqual(ocean_cell["oceanLiquidLevel"], 80)
            self.assertEqual(
                swe.biome_at(document, floor_cell["blockBiomeIndex"])["baseName"],
                "oceanfloor",
            )
            self.assertEqual(
                swe.biome_at(document, ocean_cell["blockBiomeIndex"])["baseName"],
                "ocean",
            )
            self.assertEqual(
                swe.biome_at(document, floor_cell["blockBiomeIndex"])
                ["surfacePlaceables"]["itemDistributions"][0]["randomItems"],
                [["microDungeon", ["oceanMicro"]]],
            )


class BTreeTests(unittest.TestCase):
    def build_and_read(self, count):
        records = [
            (struct.pack(">BI", index % 5, index), zlib.compress(bytes([index % 256]) * 25))
            for index in range(count)
        ]
        writer = swe.BTreeDB5Writer(2048, "World4", 5)
        stream = io.BytesIO()
        writer.write(stream, records)
        stream.seek(0)
        database = swe.BTreeDB5(stream)
        self.assertEqual(list(database.records()), sorted(records, key=lambda item: item[0]))

    def test_empty_and_small_trees(self):
        for count in (0, 1, 2, 3, 4, 9):
            with self.subTest(count=count):
                self.build_and_read(count)

    def test_multi_level_and_partition_edges(self):
        for count in (453, 454, 455, 7901):
            with self.subTest(count=count):
                self.build_and_read(count)


class CelestialRenameTests(unittest.TestCase):
    @staticmethod
    def celestial_record(name):
        body = {
            "chunkIndex": [2, -1],
            "constellations": [],
            "systemParameters": [],
            "systemObjects": [
                [
                    [123, -45, 6789],
                    [
                        [
                            4,
                            {
                                "parameters": {
                                    "coordinate": {
                                        "location": [123, -45, 6789],
                                        "planet": 4,
                                        "satellite": 0,
                                    },
                                    "seed": 1,
                                    "name": name,
                                    "parameters": {},
                                    "visitableParameters": None,
                                },
                                "satellites": [],
                            },
                        ]
                    ],
                ]
            ],
        }
        return swe.encode_celestial_chunk("CelestialChunk", 12, body)

    @staticmethod
    def document(name="Old Planet"):
        return {
            "metadata": {
                "worldTemplate": {
                    "celestialParameters": {
                        "coordinate": {
                            "location": [123, -45, 6789],
                            "planet": 4,
                            "satellite": 0,
                        },
                        "name": name,
                    }
                }
            }
        }

    @staticmethod
    def read_target_name(path):
        stream = path.open("rb")
        try:
            database = swe.BTreeDB5(stream)
            record = database.get(swe.celestial_chunk_key([123, -45, 6789]))
            _name, _version, body, _trailing = swe.decode_celestial_chunk(record)
            return body["systemObjects"][0][1][0][1]["parameters"]["name"]
        finally:
            stream.close()

    def test_renames_universe_chunk_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            chunks = Path(directory) / "universe.chunks"
            key = swe.celestial_chunk_key([123, -45, 6789])
            with chunks.open("wb") as stream:
                swe.BTreeDB5Writer(2048, swe.CELESTIAL_IDENTIFIER, 32).write(
                    stream, [(key, self.celestial_record("Old Planet"))]
                )

            backup = swe.rename_world_in_celestial_database(
                chunks, self.document(), "新星球"
            )

            self.assertTrue(backup.is_file())
            self.assertEqual(self.read_target_name(chunks), "新星球")
            self.assertEqual(self.read_target_name(backup), "Old Planet")


class MetadataTests(unittest.TestCase):
    def test_metadata_round_trip(self):
        document = {
            "format": {"name": "WorldMetadata", "version": 26},
            "size": [3000, 2000],
            "metadata": {"worldTemplate": {"worldParameters": {"weatherPool": []}}},
        }
        self.assertEqual(swe.decode_world_metadata(swe.encode_world_metadata(document)), document)

    def test_source_overwrite_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "a.world"
            source.write_bytes(b"test")
            with self.assertRaises(ValueError):
                swe.atomic_write_world(source, source, [], None)  # type: ignore[arg-type]

    def test_metadata_write_preserves_every_nonmetadata_record_byte_for_byte(self):
        original_document = {
            "format": {"name": "WorldMetadata", "version": 26},
            "size": [3000, 2000],
            "metadata": {"worldTemplate": {"worldParameters": {"gravity": 80.0}}},
        }
        edited_document = json.loads(json.dumps(original_document))
        edited_document["metadata"]["worldTemplate"]["worldParameters"]["gravity"] = 25.0
        preserved = {
            struct.pack(">BHH", swe.STORE_TILE_SECTOR, 12, 34): b"tile-sector-bytes",
            struct.pack(">BHH", swe.STORE_ENTITY_SECTOR, 12, 34): b"entity-sector-bytes",
            struct.pack(">BI", swe.STORE_UNIQUE_INDEX, 77): b"unique-index-bytes",
            struct.pack(">BHH", swe.STORE_SECTOR_UNIQUES, 12, 34): b"sector-unique-bytes",
        }
        records = [(swe.WORLD_METADATA_KEY, swe.encode_world_metadata(original_document))]
        records.extend(preserved.items())

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.world"
            output = Path(directory) / "edited.world"
            with source.open("w+b") as stream:
                swe.BTreeDB5Writer(2048, "World4", 5).write(stream, records)

            database, loaded_records = swe.load_records(source)
            try:
                swe.write_metadata_document(
                    source, output, database, loaded_records, edited_document
                )
            finally:
                swe.close_database(database)

            rebuilt_database, rebuilt_records = swe.load_records(output)
            try:
                rebuilt = dict(rebuilt_records)
                self.assertEqual(
                    swe.decode_world_metadata(rebuilt[swe.WORLD_METADATA_KEY]),
                    edited_document,
                )
                for key, value in preserved.items():
                    self.assertEqual(rebuilt[key], value)
            finally:
                swe.close_database(rebuilt_database)

    def test_compact_project_has_one_canonical_value_and_rebuilds_from_source(self):
        biome = {
            "baseName": "testbiome",
            "description": "",
            "mainBlock": 3,
            "subBlocks": [4],
            "ores": [[5, 1.0]],
            "hueShift": 0.0,
            "materialHueShift": 0,
            "spawnProfile": {"spawnTypes": ["poptop"], "monsterParameters": {}},
            "parallax": {
                "seed": 1,
                "imageDirectory": "/parallax/images/",
                "verticalOrigin": 100.0,
                "hueShift": 0.0,
                "layers": [
                    {
                        "textures": ["/assetMissing.png"],
                        "directives": "",
                        "parallaxValue": [1.0, 1.0],
                        "repeat": [1, 1],
                        "tileLimitTop": None,
                        "tileLimitBottom": None,
                        "verticalOrigin": 100.0,
                        "zLevel": 2.0,
                        "parallaxOffset": [0.0, 0.0],
                        "timeOfDayCorrelation": "",
                        "speed": 0.0,
                        "unlit": False,
                        "lightMapped": True,
                        "fadePercent": 0.0,
                    }
                ],
                "parallaxTreeVariant": None,
            },
            "ambientNoises": None,
            "musicTrack": None,
            "surfacePlaceables": {},
            "undergroundPlaceables": {},
        }
        coloring = {
            "mainColor": [255, 255, 255],
            "dayColors": [[100, 120, 140]],
        }
        world_parameters = {
            "weatherPool": [{"weight": 1.0, "item": "clear"}],
            "gravity": 80.0,
            "dayLength": 600.0,
            "airless": False,
            "skyColoring": coloring,
            "blendSize": 10.0,
            "blockNoise": {},
            "blendNoise": {},
            "surfaceLayer": {},
        }
        compiled_layer = {
            "yStart": 0,
            "boundaries": [],
            "cells": [
                {
                    "blockBiomeIndex": 1,
                    "environmentBiomeIndex": 1,
                    "terrainSelectorIndex": 0,
                }
            ],
        }
        document = {
            "format": {"name": "WorldMetadata", "version": 26},
            "size": [3000, 2000],
            "metadata": {
                "spawningEnabled": True,
                "adjustPlayerStart": False,
                "playerStart": [100.0, 200.0],
                "respawnInWorld": False,
                "worldProperties": {"largeHiddenValue": "x" * 1000},
                "worldTemplate": {
                    "size": [3000, 2000],
                    "worldParameters": world_parameters,
                    "skyParameters": {
                        "seed": 9,
                        "dayLength": 600.0,
                        "skyType": "atmospheric",
                        "skyColoring": coloring,
                    },
                    "celestialParameters": {
                        "name": "Original Planet",
                        "visitableParameters": json.loads(json.dumps(world_parameters))
                    },
                    "regionData": {
                        "worldSize": [3000, 2000],
                        "layers": [compiled_layer],
                        "terrainSelectors": [{"type": "flat"}],
                        "biomes": [
                            json.loads(json.dumps(biome)),
                            json.loads(json.dumps(biome)),
                        ],
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.world"
            source.write_bytes(b"test world source")
            project = swe.make_editable_project(
                source, document, SimpleNamespace(identifier="World4")
            )

        self.assertEqual(list(project), ["source", "world", "sky", "terrain", "biomes"])
        self.assertNotIn("advancedWorldDocument", project)
        self.assertNotIn("editable", project)
        self.assertNotIn("worldProperties", json.dumps(project))
        self.assertEqual(project["biomes"][0]["indexes"], [1, 2])
        self.assertEqual(len(project["biomes"]), 1)
        self.assertNotIn("dayLength", project["sky"])
        self.assertNotIn("skyColoring", project["world"])
        self.assertEqual(project["world"]["worldName"], "Original Planet")

        project["world"]["worldName"] = "新的星球名称"
        project["world"]["gravity"] = 25.0
        project["world"]["dayLength"] = 900.0
        project["sky"]["skyColoring"]["mainColor"] = [1, 2, 3]
        project["biomes"][0]["parameters"]["spawnProfile"]["spawnTypes"] = ["gleap"]
        rebuilt = swe.apply_editable_project(project, document)
        template = rebuilt["metadata"]["worldTemplate"]
        visitable = template["celestialParameters"]["visitableParameters"]
        self.assertEqual(template["celestialParameters"]["name"], "新的星球名称")
        self.assertEqual(template["worldParameters"]["gravity"], 25.0)
        self.assertEqual(visitable["gravity"], 25.0)
        self.assertEqual(template["worldParameters"]["dayLength"], 900.0)
        self.assertEqual(template["skyParameters"]["dayLength"], 900.0)
        self.assertEqual(visitable["dayLength"], 900.0)
        self.assertEqual(template["worldParameters"]["skyColoring"]["mainColor"], [1, 2, 3])
        self.assertEqual(template["skyParameters"]["skyColoring"]["mainColor"], [1, 2, 3])
        self.assertEqual(visitable["skyColoring"]["mainColor"], [1, 2, 3])
        self.assertEqual(
            [
                item["spawnProfile"]["spawnTypes"]
                for item in template["regionData"]["biomes"]
            ],
            [["gleap"], ["gleap"]],
        )
        self.assertEqual(rebuilt["metadata"]["worldProperties"], {"largeHiddenValue": "x" * 1000})


class SectorRegenerationTests(unittest.TestCase):
    @staticmethod
    def tile_sector_key(sector_x, sector_y):
        return struct.pack(">BHH", swe.STORE_TILE_SECTOR, sector_x, sector_y)

    @staticmethod
    def entity_sector_key(sector_x, sector_y):
        return struct.pack(">BHH", swe.STORE_ENTITY_SECTOR, sector_x, sector_y)

    @staticmethod
    def unique_sector_key(sector_x, sector_y):
        return struct.pack(">BHH", swe.STORE_SECTOR_UNIQUES, sector_x, sector_y)

    @staticmethod
    def tile_sector(block_biome=1, environment_biome=1):
        raw = bytearray(3 + 1024 * 31)
        for tile_index in range(1024):
            offset = 3 + tile_index * 31
            raw[offset + 27] = block_biome
            raw[offset + 28] = environment_biome
        return zlib.compress(raw)

    def test_resets_matching_tile_and_entity_sector_only(self):
        matching = self.tile_sector_key(2, 5)
        other_x = self.tile_sector_key(8, 5)
        other_biome = self.tile_sector_key(3, 5)
        records = [
            (swe.WORLD_METADATA_KEY, b"metadata"),
            (matching, self.tile_sector(7, 7)),
            (self.entity_sector_key(2, 5), b"entities"),
            (other_x, self.tile_sector(7, 7)),
            (other_biome, self.tile_sector(3, 3)),
        ]
        updated, result = swe.reset_generated_biome_x_range(
            records, 400, 64, 127, [7]
        )
        keys = {key for key, _value in updated}
        self.assertNotIn(matching, keys)
        self.assertNotIn(self.entity_sector_key(2, 5), keys)
        self.assertIn(other_x, keys)
        self.assertIn(other_biome, keys)
        self.assertEqual(result["resetSectors"], 1)
        self.assertEqual(result["removedEntityRecords"], 1)

    def test_removes_sector_unique_and_its_type3_index_entry(self):
        tile_key = self.tile_sector_key(2, 5)
        unique_index_key = struct.pack(">BI", swe.STORE_UNIQUE_INDEX, 123)
        unique_entries = [
            ("reset-me", (2, 5), (65.0, 170.0)),
            ("keep-me", (9, 5), (300.0, 170.0)),
        ]
        records = [
            (swe.WORLD_METADATA_KEY, b"metadata"),
            (tile_key, self.tile_sector(7, 7)),
            (self.entity_sector_key(2, 5), b"entities"),
            (self.unique_sector_key(2, 5), b"unique ids"),
            (unique_index_key, swe.write_unique_index_store(unique_entries)),
        ]
        updated, result = swe.reset_generated_biome_x_range(
            records, 400, 64, 127, [7]
        )
        values = dict(updated)
        self.assertNotIn(tile_key, values)
        self.assertNotIn(self.entity_sector_key(2, 5), values)
        self.assertNotIn(self.unique_sector_key(2, 5), values)
        self.assertEqual(
            swe.read_unique_index_store(values[unique_index_key]), [unique_entries[1]]
        )
        self.assertEqual(result["resetSectors"], 1)
        self.assertEqual(result["removedSectorUniqueRecords"], 1)
        self.assertEqual(result["removedUniqueIndexEntries"], 1)

    def test_reversed_x_range_wraps_at_world_edge(self):
        records = [
            (swe.WORLD_METADATA_KEY, b"metadata"),
            (self.tile_sector_key(0, 1), self.tile_sector(9, 9)),
            (self.tile_sector_key(9, 1), self.tile_sector(9, 9)),
            (self.tile_sector_key(5, 1), self.tile_sector(9, 9)),
        ]
        updated, result = swe.reset_generated_biome_x_range(
            records, 320, 288, 31, [9]
        )
        keys = {key for key, _value in updated}
        self.assertNotIn(self.tile_sector_key(0, 1), keys)
        self.assertNotIn(self.tile_sector_key(9, 1), keys)
        self.assertIn(self.tile_sector_key(5, 1), keys)
        self.assertTrue(result["wrapped"])

    def test_y_range_limits_selected_sectors(self):
        records = [
            (swe.WORLD_METADATA_KEY, b"metadata"),
            (self.tile_sector_key(2, 3), self.tile_sector(7, 7)),
            (self.tile_sector_key(2, 4), self.tile_sector(7, 7)),
        ]
        updated, result = swe.reset_generated_biome_x_range(
            records,
            400,
            64,
            127,
            [7],
            y_start=100,
            y_end=110,
            world_height=300,
        )
        keys = {key for key, _value in updated}
        self.assertNotIn(self.tile_sector_key(2, 3), keys)
        self.assertIn(self.tile_sector_key(2, 4), keys)
        self.assertEqual(result["yStart"], 100)
        self.assertEqual(result["yEnd"], 110)

    def test_rectangle_reset_does_not_filter_by_existing_biome(self):
        tile_key = self.tile_sector_key(2, 3)
        records = [
            (swe.WORLD_METADATA_KEY, b"metadata"),
            (tile_key, self.tile_sector(2, 2)),
            (self.entity_sector_key(2, 3), b"entities"),
        ]
        updated, result = swe.reset_generated_rectangle(
            records, 400, 300, 70, 75, 100, 110
        )
        keys = {key for key, _value in updated}
        self.assertNotIn(tile_key, keys)
        self.assertNotIn(self.entity_sector_key(2, 3), keys)
        self.assertEqual(result["resetSectors"], 1)


class BiomeRectangleLayoutTests(unittest.TestCase):
    @staticmethod
    def cell(biome, selector):
        return {
            "blockBiomeIndex": biome,
            "environmentBiomeIndex": biome,
            "terrainSelectorIndex": selector,
        }

    @staticmethod
    def biome_at(layer, x):
        index = 0
        for boundary in layer["boundaries"]:
            if x <= boundary:
                break
            index += 1
        return layer["cells"][index]["blockBiomeIndex"]

    def test_rectangle_splits_x_and_y_while_preserving_selectors(self):
        document = {
            "size": [100, 100],
            "metadata": {
                "worldTemplate": {
                    "regionData": {
                        "regionBlending": 8.0,
                        "biomes": [
                            {"baseName": "one"},
                            {"baseName": "two"},
                            {"baseName": "target"},
                        ],
                        "layers": [
                            {
                                "yStart": 0,
                                "boundaries": [49],
                                "cells": [self.cell(1, 10), self.cell(2, 20)],
                            },
                            {
                                "yStart": 50,
                                "boundaries": [49],
                                "cells": [self.cell(1, 30), self.cell(2, 40)],
                            },
                        ],
                    }
                }
            },
        }
        result = swe.set_compiled_biome_rectangle(
            document, 20, 79, 10, 69, [3]
        )
        layers = document["metadata"]["worldTemplate"]["regionData"]["layers"]
        self.assertEqual([layer["yStart"] for layer in layers], [0, 10, 50, 70])
        for layer in (layers[1], layers[2]):
            self.assertEqual(self.biome_at(layer, 19), 1)
            self.assertEqual(self.biome_at(layer, 20), 3)
            self.assertEqual(self.biome_at(layer, 79), 3)
            self.assertEqual(self.biome_at(layer, 80), 2)
        self.assertEqual(layers[1]["cells"][1]["terrainSelectorIndex"], 10)
        self.assertEqual(result["changedLayers"], 2)


if __name__ == "__main__":
    unittest.main()
