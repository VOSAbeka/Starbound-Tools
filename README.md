# Starbound-Tools
Starbound-World-Editor
============================================

1. Purpose
----------

This toolkit reads, edits, and regenerates Starbound World4 `.world` files.

The outermost directory of the package contains three programs:

1. 01_Export_World_to_JSON.exe
   Exports world parameters suitable for manual editing from a `.world` file to JSON.

2. 02_Import_JSON_to_World.exe
   Writes modified JSON parameters into a copy of the original `.world` file and creates a new `.world` file.

3. 03_Regenerate_Biome_Region.exe
   Clears generated sectors inside a specified rectangular region and changes the region to a specified biome, allowing the game to regenerate terrain, liquids, plants, monsters, microdungeons, and other content.


2. System Requirements
----------------------

1. Windows 10/11 is supported.
2. Python 3.8 or later is required. Python 3.11 or 3.12 is recommended.
3. When installing Python, select:
   - Add python.exe to PATH
   - tcl/tk and IDLE
4. No third-party pip packages are required.
5. Extract the entire archive. Do not copy only the three EXE files; the `scipt` subfolder must remain in its original location.

You can check Python and Tk in Command Prompt:

    python --version
    python -c "import tkinter; print(tkinter.TkVersion)"

If an EXE cannot find Python, you can also double-click the matching `.cmd` launcher in the `scipt` folder.


3. Important Before You Begin
-----------------------------

1. It is recommended that you completely exit Starbound and its server before editing. At minimum, the planet being edited must not be in use.
2. Always keep a backup of the original `.world` file.
3. Do not set the output path to the source `.world` file itself; the software also prevents this operation.
4. When testing an edited world for the first time, use a test character or a copied `storage` folder.
5. Before replacing a live world, confirm that the output file can be generated and verified successfully by the tool.
6. Planet filenames contain celestial coordinates. To use an output file in the game, rename it to exactly the same filename as the original `.world` file before replacing the original.


4. Tool 01 — Export World to Editable JSON
------------------------------------------

Features:

- Reads a World4 `.world` file.
- Exports editable parameters such as weather, sky colors, planet name, gravity, day length, layer definitions, biomes, monsters, parallax, and terrain selectors.
- Does not modify the source `.world` file.
- Does not place the very large tile and entity sector data in the JSON, making the JSON easier to read and edit.

Steps:

1. Double-click `01_Export_World_to_JSON.exe`.
2. Select English, 中文, or Deutsch in the upper-right corner. The default language is English.
3. Click “Browse” and select the `.world` file to read.
4. Click “Start Export”.
5. The JSON is automatically created in the same folder as the source `.world` file, using this naming format:

       tmp_<complete original world filename>.json

   Example:

       planet.world
       tmp_planet.world.json

6. You can click “Open JSON” or “Open Folder”.

Notes:

- The `source` section records the source `.world` path, SHA-256 hash, and validation information. Editing it is not recommended.
- The main editable sections are `world`, `sky`, `terrain`, `biomes`, and `parallax`.
- Do not casually delete fields, change array lengths, or modify biome indexes you do not understand.
- Tool 01 exports parameters only; it does not redraw terrain that has already been generated.


5. Tool 02 — Import Modified JSON into a World
-----------------------------------------------

Features:

- Reads a JSON file created by Tool 01 and subsequently edited.
- Reads the corresponding source `.world` file again.
- Writes modified JSON parameters into a copy of the source world.
- Preserves unmodified parameters, generated terrain, player-built structures, objects, and entity records from the source world.
- Can change the planet name and optionally synchronize the name shown on the navigation map.

Steps:

1. Edit the JSON created by Tool 01 in VS Code, Notepad++, or Notepad, and save it.
2. Double-click `02_Import_JSON_to_World.exe`.
3. Select the modified JSON file.
4. The software reads the source `.world` path recorded in the JSON. If the file has moved, browse to and select the correct original `.world` file.
5. Select the complete output path for the new `.world` file.
6. To change the planet name, enter it in the “New Planet Name” field.
7. To synchronize the name shown on the navigation map as well:
   - Enable synchronization of the navigation-map name;
   - Select the `storage\universe` folder containing `universe.chunks`.
8. Click “Start Import/Create”.
9. If the output file already exists, the software asks whether to overwrite it using the currently selected interface language.

Tool 02 does not:

- Automatically regenerate previously explored terrain.
- Delete player-built structures.
- Modify old teleport bookmark names stored in character `.player` files.

Changes to settings read at runtime, such as weather, sky colors, music, or parallax, are normally visible after re-entering the world.

Biome blocks, terrain selectors, ore distribution, plants, and placed features are mainly world-generation rules. Already saved sectors are not redrawn automatically. Use Tool 03 when a region must be regenerated.


6. Tool 03 — Regenerate a Rectangular Biome Region
--------------------------------------------------

Features:

- Accepts a rectangular X/Y range.
- Reads the target `.biome` from the selected Starbound assets.
- Recompiles a complete biome-region recipe for the selection.
- Deletes every 32×32 sector intersecting the selection so that the game regenerates it when a player approaches.

Recompiled content includes:

- Main and sub-block materials;
- Terrain shape;
- Foreground and background caves;
- Foreground and background ore distribution;
- Ocean liquid, sea level, and cave liquid;
- Grass, bushes, and trees;
- Objects and treasure boxes;
- Microdungeons specified by the `.biome`;
- Monster spawn tables;
- Parallax, ambient sounds, and music.

Even if the target biome or one of its microdungeons has never been compiled into the source `.world`, Tool 03 compiles and adds it from the assets.

Steps:

1. Double-click `03_Regenerate_Biome_Region.exe`.
2. Select the source `.world` file.
3. Select the Starbound assets:
   - You may select an unpacked assets folder containing directories such as `biomes`, `terrain`, and `parallax`;
   - You may select a Starbound assets folder containing `packed.pak`;
   - If you select the parent directory of `assets`, the tool also attempts to detect common layouts automatically.
4. Select the output path for the new `.world` file.
5. Click “Scan Assets and Populate Biomes” and wait for the biome drop-down list to be generated.
6. Click “Read World Information”.
7. Enter the range:
   - X start and X end;
   - Y start and Y end;
   - Coordinates are tile coordinates, and both endpoints are included.
8. Select the target biome from the drop-down list. You may also type the name of a biome that exists in the selected assets.
9. Click “Create Regenerated World”.
10. Read the confirmation window and continue after confirming the information.
11. Wait while the program compiles the biome, removes sectors, rebuilds the file, and verifies the World4 data.

Ranges and sectors:

- The biome range in metadata is written using the entered coordinates.
- Actual terrain deletion is expanded outward to 32×32 sector boundaries.
- Therefore, the actual deleted tile/entity area may be slightly larger than the entered rectangle.

Important warning:

- Tool 03 deletes all saved content in every intersecting sector.
- This includes natural terrain, player-built structures, placed objects, liquids, monsters, NPCs, stagehands, and unique-entity records.
- Starbound tile data cannot reliably distinguish naturally generated blocks from player-placed blocks, so natural terrain cannot be regenerated independently while preserving buildings.
- The source `.world` file is not modified. All changes are written only to the new output file.


7. Ocean and Ocean-Floor Handling in Tool 03
---------------------------------------------

When one of the following ocean-floor biomes is selected:

- oceanfloor
- toxicoceanfloor
- arcticoceanfloor
- magmaoceanfloor

Tool 03 automatically creates two vertical layers:

- The lower layer uses the corresponding ocean-floor biome and generates uneven seabed terrain, underwater plants, treasure boxes, microdungeons, and seabed parallax;
- The upper layer automatically uses `ocean`, `toxic`, `arctic`, or `magma`, generating seawater, island rules, and ocean parallax;
- Both layers use one continuous liquid surface;
- The boundary is normally placed at approximately 5/7 of the selected Y-range height;
- The completion window displays the actual boundary Y coordinate.

Therefore, you do not need to generate the ocean floor first and then run the tool a second time to generate the ocean.


8. Installing an Output File in the Game
----------------------------------------

1. Completely exit the game and server.
2. Back up the original `.world` file.
3. Copy the output file into the original `.world` file's directory.
4. Rename the output file to exactly the same filename as the original world.
5. Replace the original file with the output file.
6. Enter the game and test it.

If the world does not load:

- Exit the game immediately;
- Restore the backup;
- Check whether the wrong source world, assets, or biome was selected;
- Inspect the latest Error and Warn entries in Starbound's `storage\starbound.log`.


9. Frequently Asked Questions
-----------------------------

1. Nothing happens when I double-click an EXE

   Check that Python is installed, added to PATH, and that Tk is available. You can also run the corresponding `.cmd` file in `scipt`.

2. The tool reports that a biome cannot be found

   Select the correct assets folder again and click “Scan Assets”. When using a mod biome, the assets must actually contain all resources required by that mod.

3. The output file already exists

   The software asks whether to overwrite it. If you select “No”, it returns to the previous window without clearing your entries.

4. An old region does not change after I edit the JSON

   Tool 02 changes world-generation parameters but does not delete saved sectors. Use Tool 03 to reset the region that must be regenerated.

5. Buildings disappear after Tool 03 runs

   This is expected for a complete sector reset. Run Tool 03 only on regions where all saved content may be cleared.

6. The planet name changed, but an old teleport bookmark still shows the previous name

   Teleport bookmark names are stored in the character `.player` file. Delete the old bookmark in the game and add it again.
