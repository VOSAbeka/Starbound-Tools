# Starbound `.world` 读取与改写工具

这是一个只依赖 Python 标准库的 `World4` 世界文件工具。它能读取和重建
`BTreeDB5` 数据库，解压世界记录，并解析/重新编码 Starbound 的 SBON 数据。

## Windows 双击窗口工作流

不需要 R。在本文件夹双击：

1. `01_Export_World_to_JSON.exe`：只需输入/选择原 `.world`。无需填写 JSON 输出
   路径，也无需选择 assets；程序会在原 world 的同一文件夹自动生成
   `tmp_<原 world 完整文件名>.json`。例如 `planet.world` 会生成
   `tmp_planet.world.json`；
2. 使用 VS Code、Notepad++ 等文本编辑器修改 JSON 的 `world`、`sky`、
   `terrain` 或 `biomes` 参数组；
3. `02_Import_JSON_to_World.exe`：选择修改后的 JSON、它对应的原始 `.world`，并选择
   新 `.world` 的完整输出路径。选择 JSON 后会自动填入导出时记录的来源路径和建议
   输出路径，也可以分别浏览修改；程序会校验来源文件的 SHA-256；
4. 若要让游戏重新生成某段已经保存的地形，双击
   `03_Regenerate_Biome_Region.exe`：选择 `.world`、Starbound assets 文件夹和新
   `.world` 的完整输出路径，输入 X/Y 起点、终点并选择目标 biome。先选择 assets，
   再点击“扫描 assets 并填充 biome 列表”；下拉框会列出扫描到的 `.biome`，而且
   仍允许手动输入。选择来源 world 后点击“读取 world 信息”，列表会标明哪些 biome
   已有 compiled indexes，哪些会从 assets 新编译并加入 world。assets 文件夹可以直接包含
   `packed.pak`，也可以是含 `.biome` 文件的解包目录；程序会自动识别，而且读取
   PAK 索引时不需要先把 900MB 左右的内容全部解包。程序会把矩形内
   world layout 的完整区域 cell 改成目标 biome 的新配方，
   并删除所有相交的 32×32 tile、entity、sector-unique 记录、同步清理 unique
   index；玩家进入附近时，游戏会按目标 biome 的物块、placeables、怪物、
   parallax、音乐和环境配置重生缺失 sector。terrain、前景/背景洞穴、矿脉、
   子物块、海洋/洞穴液体、植被、object、宝箱与 microdungeon 都从所选 assets
   重新编译，不再继承原区域。精简 JSON 不含 sector 记录，
   因而 03 的来源只接受 `.world`。
   测试时必须完全退出游戏/服务器，备份原 world，再用输出替换原文件，并把输出
   改回原 world 完全相同的文件名；星球坐标对应关系依赖这个文件名。

三个窗口右上角都有 `Language / 语言 / Sprache` 菜单，可即时切换 English、中文
和 Deutsch，默认语言为 English。三个 `.exe` 是无控制台窗口的 Windows 启动器，
优先调用 `scipt` 子文件夹中的 Python/Tk 图形程序（也兼容脚本与 EXE 同目录）；
这台电脑已经有可用的 Python 3 和 Tk 8.6。若 Windows 安全软件阻止小型
启动器，也可以双击同名 `.cmd`，功能相同。

02 和 03 点击 Create 后，如果目标文件名已经存在，会使用当前界面语言询问是否
覆盖。选择“否”只取消本次写入，路径、文件名、坐标和 biome 输入都会保留。

03 每次都从所选 assets 重新编译目标 biome，即使同名 biome 已经存在于来源 world。
工具按照 Starbound 的已存储格式解析 material/mod/liquid ID、地形和洞穴 selector、
矿脉、怪物池、hue shift、parallax、环境音、音乐，以及 tree、bush、grass、object、
treasureBox、microdungeon 等 `items` placeable distributions，再把新的 biome 与
terrain selector 追加到 `worldTemplate.regionData`。因此目标不必是该星球的 main
biome，也不必预先存在于该 world；若 `.biome` 中包含 microdungeon，而来源 world
从未编译过它，新区域仍会按该 distribution 生成。

海洋星球的 `ocean` 与 `oceanfloor` 是两个上下相邻的编译区域，不是一个 biome
里的开关。世界纵坐标越小越深。它们的实际分界保存在
`worldTemplate.regionData.layers[].yStart`；模板中的
`worldParameters.subsurfaceLayer.layerMinHeight` 和
`worldParameters.surfaceLayer.layerMinHeight` 会反映对应的全局层高。
`oceanLiquidLevel` 是液面高度，不是海床 biome 的分界。

选择 `oceanfloor`、`toxicoceanfloor`、`arcticoceanfloor` 或
`magmaoceanfloor` 时，03 会一次自动建立海洋星球式的两层结构：选区下部使用对应
oceanfloor 的起伏海床、洞穴、植被和海底 parallax，上部自动使用 `ocean`、`toxic`、
`arctic` 或 `magma` 的海水/岛屿与海洋 parallax。分界按原版海洋层的比例放在选区
高度约 5/7 处，液面连接到选区上边界；完成窗口会显示实际分界 Y。

新版项目 JSON 只有五个顶层区域：

- `source`：来源 world 路径、SHA-256、原尺寸和 biome 数量。这是导回校验信息，
  不应修改；`worldSize` 只在这里显示一次，而且不能通过本工具改变；
- `world`：天气、重力、昼长、空气、beam-up、出生点、是否生成怪物等世界参数；
- `sky`：天空颜色、地平线图片、卫星、明暗、地表与太空分界等；
- `terrain`：层定义、噪声、编译后的纵向层/cell 和 terrain selector；
- `biomes`：各 biome 的物块、矿物、怪物、parallax 背景、环境音、音乐和
  placeables。完全相同的编译副本会合并为一个配置，`indexes` 表示该配置适用的
  原 biome 编号，`name` 是原名称，实际可编辑值全部位于 `parameters`。

项目不再包含 `advancedWorldDocument`。导回时程序重新读取 SHA-256 对应的原
`.world`，只替换键 `0` 的 `WorldMetadata` 记录；tile sector、entity sector、
sector-unique 和 unique index 记录会逐字节保留，因此已经生成的地形、液体状态、
对象和玩家建筑不会被 02 重置。程序再把上述唯一参数同步写回游戏内部所有重复位置。
例如只需改一次
`world.dayLength`，程序会同时更新 world、sky 和 celestial 中对应的昼长；只需
改一次 `sky.skyColoring`，所有对应位置也会同步。旧版
`StarboundWorldEditorProject1` JSON 仍可导入。

01 会把 `.world` 内的 `celestialParameters.name` 导出为唯一参数
`world.worldName`。02 的窗口也提供“新星球名称”输入框。修改 `.world` 只会改变
进入星球后使用的名称；如需同时修改星图，勾选同步选项并选择包含
`universe.chunks` 的 `storage\universe` 文件夹。02 会精确修改目标星球所在的
CelestialChunk，并先建立带时间戳的 `universe.chunks.bak_before_rename_*` 备份。
此操作必须在游戏和服务器完全退出时执行。`universe.dat` 只保存 universe 设置，
并不保存星球名称；`.system` 也不保存星球名称。

角色已经保存的传送书签会把当时的 `targetName` 保存在 `.player` 中，因此旧书签
可能继续显示旧语言名称。工具不会改写角色文件；在游戏里删除该书签并重新添加即可。

可编辑字段范围：

- `world`：`worldName`（星球名称）、`spawningEnabled`、`adjustPlayerStart`、`playerStart`、
  `respawnInWorld`、`protectedDungeonIds`、`dungeonIdBreathable`、
  `dungeonIdGravity`、`weatherPool`、`gravity`、`dayLength`、`airless`、
  `beamUpRule`、`environmentStatusEffects`、`globalDirectives`、
  `surfaceLiquid`、`primaryBiome`、`threatLevel`、`disableDeathDrops`、
  `overrideTech`、`terraformed`、`worldEdgeForceRegions` 和 `hueShift`；
- `sky`：`skyType`、`skyColoring`、`horizonImages`、`horizonClouds`、
  `satellites`、`ambientLightLevel`、`surfaceLevel`、`spaceLevel`、`planet`
  和 `seed`；
- `terrain`：`blendSize`、`blockNoise`、`blendNoise`、`layerDefinitions`、
  `regionBlending`、`playerStartSearchRegions`、`compiledLayers` 和
  `terrainSelectors`；
- `biomes[].parameters`：`description`、`mainBlock`、`subBlocks`、`ores`、
  `hueShift`、`materialHueShift`、`spawnProfile`、`parallax`、
  `ambientNoises`、`musicTrack`、`surfacePlaceables` 和
  `undergroundPlaceables`。

`world`、`sky`、biome 的背景/怪物/音乐等通常在世界重新加载后直接使用。
`terrain` 以及 biome 的物块、矿物和 placeables 主要是生成模板；修改它们不会
自动重画已经保存在 tile/entity 记录里的区域，只会影响仍需由游戏生成的区域，
除非另外执行明确的已生成 tile 替换操作。

为避免损坏，窗口导入器会验证天气、biome 数量、物块 ID、地形层顺序、边界、
cell 数和 selector 引用，并暂时禁止修改整个世界的宽高。输出后还会重新打开
新 world，逐条比较全部数据库记录。

当前功能：

- 检查 `.world` 容器与记录数量；
- 导出、编辑、导回完整 `WorldMetadata`；
- 查看 biome 编号、名称、物块、背景层和怪物生成表；
- 修改天气池；
- 修改 biome 的怪物生成表；
- 修改 biome 的主物块和子物块；
- 在同一个世界内复制 biome 的背景、地形和怪物设置；
- 可选择把物块替换应用到已经生成的 tile 区域；
- 使用 JSON Pointer 对任意元数据字段进行高级修改；
- 每次只生成新文件，拒绝覆盖输入文件；写完后会完整重读并逐记录验证。

## 最重要的安全规则

1. 完全退出游戏和服务器后再编辑。
2. 永远保留原始 `.world` 和 `.world.bak*`。
3. 输出文件必须使用新名称；本工具会拒绝原地覆盖。
4. 第一次测试修改后的世界时，先使用单独的测试角色或测试 storage。
5. biome、天气、怪物或素材名称必须在实际加载的原版/Mod assets 中存在。
6. Starbound tile 不保存“自然生成/玩家放置”来源；03 会完整清除匹配 sector，
   其中的玩家建筑物块、object、液体和实体也会消失。无法可靠只重生自然地形。
   X/Y 输入是格坐标且包含首尾，实际删除范围会向外对齐到 32×32 sector 边界。

## 基本用法

以下命令都在本目录执行。Windows 中可以使用 `python` 或你的 Python 3
可执行文件：

```powershell
python .\starbound_world_editor.py inspect "D:\path\planet.world"
python .\starbound_world_editor.py verify "D:\path\planet.world"
```

导出完整元数据：

```powershell
python .\starbound_world_editor.py export-metadata `
  "D:\path\planet.world" `
  "D:\path\planet.metadata.json"
```

把编辑后的元数据写进一个新世界：

```powershell
python .\starbound_world_editor.py import-metadata `
  "D:\path\planet.world" `
  "D:\path\planet.metadata.json" `
  "D:\path\planet_edited.world"
```

## 查看 biome

```powershell
python .\starbound_world_editor.py list-biomes `
  "D:\path\planet.world" `
  --assets "D:\Starbound\assets\unpacked" `
  --output "D:\path\planet.biomes.json"
```

`--assets` 应指向解包后的 assets 根目录。它不是必需参数，但提供后，工具会把
`mainBlock: 81` 同时显示为 `magmarock` 等人类可读名称。

这里的 biome 编号从 **1** 开始。生成后的世界可能含有多个同名 biome 实例，
因为不同层、主区域和子区域会分别编译；因此修改前要看
`compiledCellReferences`，不能只看 `baseName`。

## 修改天气

```powershell
python .\starbound_world_editor.py set-weather `
  "D:\path\planet.world" `
  "D:\path\planet_weather.world" `
  clear=0.5 rain=0.4 storm=0.1
```

权重不强制相加等于 1，游戏会按比例使用；但至少要有一个正数。命令会改写
`worldParameters.weatherPool`；这是样本和公开元数据结构中的实际天气池字段。

## 修改 biome 怪物

```powershell
python .\starbound_world_editor.py set-biome-monsters `
  "D:\path\planet.world" `
  "D:\path\planet_monsters.world" `
  --biome 19 `
  --monsters poptop gleap passiveSmallFlyingDay
```

这些字符串是 biome 的 `spawnTypes`，不是任意 `/spawnmonster` 参数。清空列表会
停止该 biome 的环境怪物生成。已经存在于世界实体记录里的怪物不会被删除。

如果要替换 `monsterParameters`，把它写成一个 JSON 文件并增加
`--parameters parameters.json`；不提供时会保留原值。

## 修改 biome 物块

使用数字 material ID：

```powershell
python .\starbound_world_editor.py set-biome-blocks `
  "D:\path\planet.world" `
  "D:\path\planet_blocks.world" `
  --biome 17 --main-block 8 --sub-blocks 3 53
```

也可以使用名称，但必须提供解包 assets：

```powershell
python .\starbound_world_editor.py set-biome-blocks `
  "D:\path\planet.world" `
  "D:\path\planet_blocks.world" `
  --biome 17 --main-block dirt --sub-blocks cobblestone plantmatter `
  --assets "D:\Starbound\assets\unpacked"
```

默认只改世界的 biome 模板；已经保存的 tile 不会凭空重画。若明确希望把该
biome 已生成区域中的旧主物块/子物块翻译成新 ID，再加：

```text
--rewrite-generated-tiles
```

这个选项会改前景和背景 material ID，但不会重新运行完整世界生成器，也不会
重建洞穴、微型地牢、植物或对象。

## 在 biome 之间复制组件

把 biome 19 的背景和怪物生成表复制给 biome 17：

```powershell
python .\starbound_world_editor.py copy-biome `
  "D:\path\planet.world" `
  "D:\path\planet_copy.world" `
  --source 19 --target 17 --components background monsters
```

可用组件：

- `background`：复制 resolved `parallax`；
- `monsters`：复制 `spawnProfile`；
- `terrain`：复制主/子物块、矿物和编译后的 terrain/cave/ore selector 引用；
- `all`：以上全部。

`terrain` 仍默认只影响模板和以后生成的区域。要同步替换现有主/子物块，可在
同一条命令增加 `--rewrite-generated-tiles`。

## 导出单个组件

```powershell
python .\starbound_world_editor.py export-biome-component `
  "D:\path\planet.world" "D:\path\background.json" `
  --biome 19 --component background
```

`component` 可以是 `background`、`terrain` 或 `monsters`。这是查看 resolved
数据的便捷方式；来自另一个世界的 `terrainSelectorIndex` 不能直接移植，因为
它引用的是该世界自己的 selector 数组。

导出的 `background.json` 可以编辑后直接导入另一个新世界文件：

```powershell
python .\starbound_world_editor.py set-biome-background `
  "D:\path\planet.world" "D:\path\planet_background.world" `
  --biome 19 --parallax "D:\path\background.json"
```

每一层的 `textures` 必须指向实际存在的 assets 图片。也可以把 parallax 写成
JSON `null` 来移除该 biome 背景。

## 高级 JSON patch

`example_patch.json` 展示了格式。路径从导出的文档根开始，支持 `set`、
`replace`、`add` 和 `remove`：

```powershell
python .\starbound_world_editor.py apply-patch `
  "D:\path\planet.world" `
  ".\example_patch.json" `
  "D:\path\planet_patched.world"
```

高级修改不会替你猜测游戏语义；不存在的 weather、parallax 图片、spawn type、
material ID 或错位的 selector index 都可能使世界无法加载。

## 文件结构概要

- 前 512 字节是 `BTreeDB5` 头；正式版星球数据库名为 `World4`，key 长度为 5。
- 记录 key 是 `layer:uint8 + x:uint16 + y:uint16`（大端序）。
- `{0,0,0}` 是世界元数据；解压后先是宽、高两个大端 `int32`，然后是
  `WorldMetadata` versioned JSON/SBON。
- layer 1 是 32×32 tile region；layer 2 是 entity region；还有索引记录层。
- 所有 World4 记录值均由 zlib 压缩。
- BTreeDB5 有两个交替 root，用于保存时的事务式切换。本工具读取当前 active
  root，但输出一个干净的新数据库；游戏以后仍可正常建立 alternate root。

## 已知边界

- 工具不会执行 Starbound 世界生成器，只能编辑已保存的 resolved 参数和 tile。
- 改背景通常在完全卸载并重新进入世界后生效。
- 改 biome 生成表不会删除现有怪物；改模板地形不会自动重建已生成地形。
- `--rewrite-generated-tiles` 只处理 material ID，不改变碰撞、对象、液体和实体。
- 当前只支持正式版 `World4`，不支持很老的 beta `WRLDB` 文件。

## 自检

```powershell
python -m unittest -v .\test_world_editor.py
```

本工具基于公开的 py-starbound 格式说明/读取实现、sbutils 的 BTreeDB5
读写实现，以及 xStarbound 公开的世界元数据描述交叉验证；并使用真实
`World4` 样本完成逐记录无损往返测试。
