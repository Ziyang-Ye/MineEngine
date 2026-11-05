"""Block / item palette of the capture pipeline (patched SolarisEngine state logs -> white-box G-buffer): every
Minecraft block-state string of a snapshot palette or a block_state record -> one palette id, its drawn shape, and a
colour for the proxy; held-item names -> a colour swatch for the HUD / hand layer (state_proxy.py).
  PALETTE [(name, (r, g, b))], id = index. Ids 0..11 = mc.py BLOCK_NAMES / BLOCK_RGB exactly (= palette v0 =
    data/block_palette_v0.json), so old 12-id grids stay valid; ids 12.. extend it (planks / logs of each wood,
    stone family, bricks, sand, gravel, 16 stained glass, 16 wool, 16 concrete, leaves, water, lava, fire, ice, obsidian,
    snow, farmland, mineral blocks, utility blocks, UNKNOWN; N = len(PALETTE) = 157 < 256: VoxelRenderer's block channel
    is uint8).
    rgb = approximate average of the vanilla 1.21 texture (side texture for logs / hay / pumpkin; leaves and water
    biome-tinted as in plains), then kept off the player colours: data/block_palette_v0.json forbids block colours that clash
    with the players (mc_proxy_style.TEAM_RGB2 magenta / pure yellow, so that no block is taken for a player).
    Clash = chroma (rgb / max channel, invariant to colorize's shade factor) within SEP of a player's; such an extended
    colour is pushed radially to distance SEP at the same max channel (NUDGED; e.g. yellow wool, yellow concrete, yellow
    stained glass get a little more blue / less green). SEP = 0.35 keeps both player colours >= 50 RGB from every block
    colour under the proxy's shading (blocks x 0.31..1, entities x 0.64..1; cap_selftest checks it; 0.25 left yellow
    concrete 38 RGB from the yellow team). Legacy ids 0..11 are never changed.
  BLOCK_RGB uint8 [N, 3]; NAMES; ID {name: id}; AIR = 0, WATER, LAVA, FIRE, UNKNOWN (a generic solid); COLLIDES bool [N]
    (False: air, water, lava, fire).
  block_id(state) -> int: "minecraft:oak_stairs[facing=east,...]", "oak_stairs", Bukkit "OAK_STAIRS" (None -> 0).
    exact name / alias -> its id; non-colliding decorations (torches, flowers, short grass, crops, rails, signs, buttons,
    pressure plates, vines, saplings, small attachments: lanterns, heads, pots, candles, chains) -> 0, or WATER when
    waterlogged (seagrass, kelp, bubble columns: always WATER); dyed blocks by colour (wool / carpet / bed / banner ->
    <c>_wool, stained glass (pane) -> <c>_stained_glass, concrete (powder) / terracotta / glazed / shulker -> <c>_concrete);
    wood species (stairs, slabs, fences, doors, signs, stripped logs -> <wood>_planks; log / wood / stem / hyphae ->
    log; leaves); shape suffix stripped (stone_brick_wall -> stone_bricks, iron_door -> iron_block, glass_pane -> glass);
    material families by name token (ores -> host rock, polished_granite -> granite, cut_copper -> copper_block, ...);
    else UNKNOWN.
  shape_of(state) -> "full" | "slab_bottom" | "slab_top" | "stairs" | "thin" | "liquid" | "none" (drawn geometry):
    slabs by type (double -> full), stairs, liquid (water / lava / waterlogged decorations), none (air, decorations),
    thin (fences, walls, panes, bars, doors, trapdoors, carpets, ladders, scaffolding, bamboo, snow layers < 4),
    slab_bottom also for beds / campfires / stonecutters / snow layers 4..7; everything else full (fire too: drawn as a
    full flame cell, COLLIDES False).
  ids_of(states) -> uint16 [len]; to_v0(ids) / V0 uint8 [N]: palette v0 (12 ids) view of any id grid (planks -> oak_planks,
    logs -> oak_log, stone family -> stone, cobble / bricks -> cobblestone, translucent -> glass, soils -> dirt, else
    nearest CIE Lab colour among V0_CANDS, no glass), for 12-id consumers: mc_proxy_style.colorize_frames
    clips ids > 11 to blue_wool, so extended grids need V0 or state_proxy.colorize(block_rgb=BLOCK_RGB).
  item_rgb(item) -> (r, g, b): held / hotbar items (prismarine-item names, "minecraft:" ids or Bukkit names): tools /
    weapons / armour by material, bows, shields, buckets, flint_and_steel, food, ingots, ...; blocks via their block
    colour; dyes / flowers by colour word; empty (None, "", "air") -> SKIN_RGB (the bare arm); else ITEM_UNKNOWN_RGB.
    is_empty(item) tells the empty slot / hand apart.
Folding rules: palette v0 (data/block_palette_v0.json) keeps 12 ids and folds every foreign block into them;
here the fold is V0 (the extended ids are the state layer's own); "stone bricks / bricks -> stone or cobblestone" is
taken as stone_bricks -> stone, bricks -> cobblestone.
  python -m mineengine.cap_palette --test          (selftest: ids 0..11 == mc.py and data/block_palette_v0.json, name / shape / item cases)
  python -m mineengine.cap_palette --json OUT      (the palette in block_palette_v0.json layout, for a pure-colour resource pack)
"""
import ast
import json
import os
import sys
from functools import lru_cache

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)); CODE = os.path.dirname(HERE)
MC_PY = os.path.join(HERE, "mc.py")
REF_JSON = os.path.join(HERE, "data", "block_palette_v0.json")
PLAYER_RGB = ((255, 0, 255), (255, 230, 0))                                     # mc_proxy_style.TEAM_RGB2 = block_palette_v0.json player_colors
SKY_RGB = (150, 190, 235)
SEP = 0.35                                                                       # min chroma distance of an extended block / item colour to a player (>= 50 RGB under shading)
SKIN_RGB = (196, 145, 110)
ITEM_UNKNOWN_RGB = (175, 165, 150)
SHAPES = ("full", "slab_bottom", "slab_top", "stairs", "thin", "liquid", "none")
DYES = ("light_blue", "light_gray", "white", "orange", "magenta", "yellow", "lime", "pink", "gray", "cyan", "purple", "blue",
        "brown", "green", "red", "black")                                       # two-word colours first
SPECIES = ("dark_oak", "pale_oak", "oak", "spruce", "birch", "jungle", "acacia", "mangrove", "cherry", "bamboo", "crimson", "warped")

# (name, average texture rgb, palette-v0 class: P planks, L log, S stone, C cobblestone, G glass (translucent), D dirt,
#  None = nearest colour). Ids 0..11 are mc.py's and are listed first, unchanged.
_RAW = [
    ("air", (0, 0, 0), None), ("grass_block", (106, 170, 64), None), ("stone", (125, 125, 125), None), ("dirt", (134, 96, 67), None),
    ("bedrock", (60, 60, 60), None), ("oak_planks", (162, 130, 78), None), ("cobblestone", (110, 110, 110), None),
    ("glass", (190, 220, 230), None), ("oak_log", (102, 81, 50), None), ("tnt", (200, 50, 40), None), ("red_wool", (180, 40, 40), None),
    ("blue_wool", (50, 70, 180), None),
    # wood
    ("spruce_planks", (114, 84, 48), "P"), ("birch_planks", (192, 175, 121), "P"), ("jungle_planks", (160, 115, 80), "P"),
    ("acacia_planks", (168, 90, 50), "P"), ("dark_oak_planks", (66, 43, 20), "P"), ("mangrove_planks", (117, 54, 48), "P"),
    ("cherry_planks", (226, 178, 172), "P"), ("bamboo_planks", (193, 173, 80), "P"), ("crimson_planks", (101, 48, 70), "P"),
    ("warped_planks", (43, 104, 99), "P"),
    ("spruce_log", (58, 37, 16), "L"), ("birch_log", (216, 215, 210), "L"), ("jungle_log", (85, 67, 25), "L"), ("acacia_log", (103, 96, 86), "L"),
    ("dark_oak_log", (60, 46, 26), "L"), ("mangrove_log", (83, 66, 41), "L"), ("cherry_log", (54, 33, 44), "L"),
    ("crimson_stem", (92, 25, 29), "L"), ("warped_stem", (58, 58, 77), "L"),
    # stone family
    ("stone_bricks", (122, 121, 122), "S"), ("mossy_stone_bricks", (115, 121, 105), "S"), ("cracked_stone_bricks", (118, 117, 118), "S"),
    ("smooth_stone", (158, 158, 158), "S"), ("granite", (149, 103, 85), "S"), ("diorite", (188, 188, 188), "S"), ("andesite", (136, 136, 136), "S"),
    ("deepslate", (80, 80, 82), "S"), ("deepslate_tiles", (54, 54, 55), "S"), ("tuff", (108, 109, 102), "S"), ("calcite", (223, 224, 220), "S"),
    ("blackstone", (42, 36, 41), "S"), ("basalt", (80, 80, 85), "S"), ("end_stone", (219, 222, 158), "S"), ("quartz_block", (235, 229, 222), "S"),
    ("purpur_block", (169, 125, 169), None), ("prismarine", (99, 156, 151), None),
    ("mossy_cobblestone", (110, 118, 94), "C"), ("cobbled_deepslate", (77, 77, 80), "C"), ("bricks", (150, 97, 83), "C"),
    ("mud_bricks", (137, 103, 79), "C"), ("nether_bricks", (44, 21, 26), "C"), ("gravel", (131, 127, 126), "C"),
    ("sand", (219, 207, 163), None), ("red_sand", (190, 102, 33), None), ("sandstone", (216, 203, 155), None),
    ("red_sandstone", (181, 97, 31), None), ("clay", (160, 166, 179), None), ("terracotta", (152, 94, 67), None),
    ("netherrack", (97, 38, 38), None), ("obsidian", (15, 10, 24), None),
    # glass (stained: the dye's map colour) and ice
    ("white_stained_glass", (255, 255, 255), "G"), ("orange_stained_glass", (216, 127, 51), "G"), ("magenta_stained_glass", (178, 76, 216), "G"),
    ("light_blue_stained_glass", (102, 153, 216), "G"), ("yellow_stained_glass", (229, 229, 51), "G"), ("lime_stained_glass", (127, 204, 25), "G"),
    ("pink_stained_glass", (242, 127, 165), "G"), ("gray_stained_glass", (76, 76, 76), "G"), ("light_gray_stained_glass", (153, 153, 153), "G"),
    ("cyan_stained_glass", (76, 127, 153), "G"), ("purple_stained_glass", (127, 63, 178), "G"), ("blue_stained_glass", (51, 76, 178), "G"),
    ("brown_stained_glass", (102, 76, 51), "G"), ("green_stained_glass", (102, 127, 51), "G"), ("red_stained_glass", (153, 51, 51), "G"),
    ("black_stained_glass", (25, 25, 25), "G"),
    ("ice", (145, 183, 253), "G"), ("packed_ice", (141, 180, 250), "G"), ("blue_ice", (116, 167, 253), "G"),
    # wool (red / blue are ids 10 / 11) and concrete
    ("white_wool", (233, 236, 236), None), ("orange_wool", (240, 118, 19), None), ("magenta_wool", (189, 68, 179), None),
    ("light_blue_wool", (58, 175, 217), None), ("yellow_wool", (248, 197, 39), None), ("lime_wool", (112, 185, 25), None),
    ("pink_wool", (237, 141, 172), None), ("gray_wool", (62, 68, 71), None), ("light_gray_wool", (142, 142, 134), None),
    ("cyan_wool", (21, 137, 145), None), ("purple_wool", (121, 42, 172), None), ("brown_wool", (114, 71, 40), None),
    ("green_wool", (84, 109, 27), None), ("black_wool", (20, 21, 25), None),
    ("white_concrete", (207, 213, 214), None), ("orange_concrete", (224, 97, 0), None), ("magenta_concrete", (169, 48, 159), None),
    ("light_blue_concrete", (35, 137, 198), None), ("yellow_concrete", (240, 175, 21), None), ("lime_concrete", (94, 168, 24), None),
    ("pink_concrete", (213, 101, 142), None), ("gray_concrete", (54, 57, 61), None), ("light_gray_concrete", (125, 125, 115), None),
    ("cyan_concrete", (21, 119, 136), None), ("purple_concrete", (100, 31, 156), None), ("blue_concrete", (44, 46, 143), None),
    ("brown_concrete", (96, 59, 31), None), ("green_concrete", (73, 91, 36), None), ("red_concrete", (142, 32, 32), None),
    ("black_concrete", (8, 10, 15), None),
    # leaves (plains-tinted), liquids, fire
    ("oak_leaves", (64, 108, 28), None), ("spruce_leaves", (48, 76, 48), None), ("birch_leaves", (70, 92, 47), None),
    ("jungle_leaves", (40, 105, 15), None), ("acacia_leaves", (95, 90, 23), None), ("dark_oak_leaves", (52, 95, 26), None),
    ("mangrove_leaves", (77, 97, 21), None), ("cherry_leaves", (228, 173, 193), None),
    ("water", (55, 100, 210), None), ("lava", (207, 92, 20), None), ("fire", (226, 130, 40), None),
    # soils, snow
    ("snow_block", (249, 254, 254), None), ("snow", (240, 248, 248), None), ("farmland", (106, 70, 40), "D"), ("dirt_path", (148, 122, 65), "D"),
    ("coarse_dirt", (119, 85, 59), "D"), ("podzol", (91, 63, 24), "D"), ("mud", (60, 57, 61), "D"), ("moss_block", (89, 109, 45), None),
    ("soul_sand", (81, 62, 50), "D"),
    # mineral blocks
    ("iron_block", (220, 220, 220), None), ("gold_block", (246, 208, 61), None), ("diamond_block", (98, 237, 228), None),
    ("emerald_block", (42, 203, 87), None), ("lapis_block", (31, 67, 140), None), ("redstone_block", (175, 24, 5), None),
    ("coal_block", (16, 15, 15), None), ("copper_block", (192, 107, 79), None), ("oxidized_copper", (82, 162, 132), None),
    ("netherite_block", (66, 61, 63), None), ("amethyst_block", (133, 97, 191), None),
    # plants, light sources, utility blocks
    ("hay_block", (166, 136, 38), None), ("pumpkin", (198, 118, 24), None), ("melon", (111, 145, 30), None), ("cactus", (85, 127, 43), None),
    ("glowstone", (171, 131, 84), None), ("sea_lantern", (172, 199, 190), None), ("sponge", (195, 192, 74), None),
    ("slime_block", (111, 192, 91), "G"), ("honey_block", (251, 185, 52), "G"), ("magma_block", (142, 63, 31), None),
    ("bookshelf", (117, 94, 59), "P"), ("crafting_table", (120, 90, 55), "P"), ("chest", (162, 113, 49), "P"), ("furnace", (115, 115, 115), "C"),
    ("unknown", (140, 130, 120), "C"),                                           # a solid of no known family (C = mc_proxy_style.MONO)
]

ALIAS = {   # name -> palette name (or another resolvable name)
    "grass_path": "dirt_path", "rooted_dirt": "coarse_dirt", "mycelium": "podzol", "powder_snow": "snow_block", "frosted_ice": "ice",
    "tinted_glass": "black_stained_glass", "flowing_water": "water", "bubble_column": "water", "flowing_lava": "lava",
    "soul_fire": "fire", "soul_soil": "soul_sand", "crimson_nylium": "netherrack", "warped_nylium": "warped_planks",
    "nether_wart_block": "red_concrete", "warped_wart_block": "cyan_concrete", "shroomlight": "orange_concrete",
    "ancient_debris": "netherrack", "crying_obsidian": "obsidian", "respawn_anchor": "obsidian", "ender_chest": "obsidian",
    "bamboo_block": "bamboo_planks", "stripped_bamboo_block": "bamboo_planks", "bamboo_mosaic": "bamboo_planks", "bamboo": "bamboo_planks",
    "scaffolding": "bamboo_planks", "ladder": "oak_planks", "mangrove_roots": "mangrove_log", "muddy_mangrove_roots": "mud",
    "pale_oak_planks": "birch_planks", "pale_oak_log": "acacia_log", "pale_oak_wood": "acacia_log", "pale_oak_leaves": "oak_leaves",
    "azalea": "oak_leaves", "flowering_azalea": "oak_leaves", "carved_pumpkin": "pumpkin", "jack_o_lantern": "pumpkin",
    "redstone_lamp": "brown_concrete", "red_mushroom_block": "red_concrete", "brown_mushroom_block": "terracotta", "mushroom_stem": "calcite",
    "bone_block": "calcite", "dried_kelp_block": "green_concrete", "target": "pink_concrete", "beacon": "glass", "trapped_chest": "chest",
    "barrel": "spruce_planks", "note_block": "jungle_planks", "jukebox": "jungle_planks", "lectern": "oak_planks", "composter": "oak_planks",
    "cartography_table": "spruce_planks", "fletching_table": "birch_planks", "smithing_table": "dark_oak_planks", "loom": "birch_planks",
    "beehive": "birch_planks", "bee_nest": "hay_block", "blast_furnace": "furnace", "smoker": "furnace", "dispenser": "furnace",
    "dropper": "furnace", "observer": "furnace", "piston": "furnace", "sticky_piston": "furnace", "piston_head": "furnace",
    "stonecutter": "stone", "grindstone": "stone", "anvil": "netherite_block", "chipped_anvil": "netherite_block", "damaged_anvil": "netherite_block",
    "iron_bars": "iron_block", "cauldron": "coal_block", "hopper": "coal_block", "enchanting_table": "obsidian", "cake": "white_concrete",
    "candle_cake": "white_concrete", "dripstone_block": "granite", "pointed_dripstone": "granite", "sculk": "black_concrete",
    "sculk_catalyst": "black_concrete", "sculk_sensor": "cyan_concrete", "calibrated_sculk_sensor": "cyan_concrete",
    "reinforced_deepslate": "deepslate", "lodestone": "stone_bricks", "chiseled_bookshelf": "bookshelf", "tnt_minecart": "tnt",
    "lily_pad": "oak_leaves", "big_dripleaf": "lime_concrete", "decorated_pot": "terracotta",
    "campfire": "oak_log", "soul_campfire": "oak_log", "daylight_detector": "oak_planks", "crafter": "furnace", "trial_spawner": "deepslate_tiles",
    "spawner": "coal_block", "vault": "deepslate_tiles", "heavy_core": "netherite_block", "suspicious_sand": "sand", "suspicious_gravel": "gravel",
}
ALIAS.update({f"{d}{k}_coral_block": "light_gray_concrete" if d else c for d in ("", "dead_") for k, c in (
    ("tube", "blue_concrete"), ("brain", "pink_concrete"), ("bubble", "purple_concrete"), ("fire", "red_concrete"), ("horn", "yellow_concrete"))})
AIRS = {"air", "cave_air", "void_air", "light", "barrier", "structure_void", "moving_piston"}
WATERY = {"water", "flowing_water", "bubble_column", "seagrass", "tall_seagrass", "kelp", "kelp_plant"}
LAVAS = {"lava", "flowing_lava"}
DECO = {   # non-colliding / tiny blocks drawn as air
    "torch", "wall_torch", "soul_torch", "soul_wall_torch", "redstone_torch", "redstone_wall_torch", "grass", "short_grass",
    "tall_grass", "fern", "large_fern", "dead_bush", "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "oxeye_daisy", "cornflower", "lily_of_the_valley", "wither_rose", "torchflower", "sunflower", "lilac", "rose_bush",
    "peony", "pitcher_plant", "pink_petals", "brown_mushroom", "red_mushroom", "crimson_fungus", "warped_fungus",
    "crimson_roots", "warped_roots", "nether_sprouts", "vine", "glow_lichen", "sculk_vein", "cave_vines", "cave_vines_plant",
    "hanging_roots", "spore_blossom", "small_dripleaf", "big_dripleaf_stem", "sweet_berry_bush", "wheat", "carrots", "potatoes",
    "beetroots", "melon_stem", "pumpkin_stem", "attached_melon_stem", "attached_pumpkin_stem", "torchflower_crop",
    "pitcher_crop", "nether_wart", "sugar_cane", "cobweb", "redstone_wire", "tripwire", "tripwire_hook", "lever", "rail",
    "powered_rail", "detector_rail", "activator_rail", "lantern", "soul_lantern", "flower_pot", "chain", "end_rod",
    "lightning_rod", "candle", "nether_portal", "end_portal", "end_gateway", "frogspawn", "repeater", "comparator",
    "turtle_egg", "sniffer_egg", "amethyst_cluster", "conduit", "bell", "brewing_stand", "dragon_egg", "seagrass",
    "tall_seagrass", "kelp", "kelp_plant", "weeping_vines", "weeping_vines_plant", "twisting_vines", "twisting_vines_plant",
    "cocoa", "bamboo_sapling", "mangrove_propagule", "short_dry_grass", "tall_dry_grass", "bush", "firefly_bush", "leaf_litter",
    "wildflowers", "cactus_flower", "resin_clump", "creaking_heart", "pale_hanging_moss", "pale_moss_carpet", "closed_eyeblossom",
    "open_eyeblossom", "sea_pickle",
}
DECO_SUFFIX = ("_sapling", "_torch", "_button", "_pressure_plate", "_sign", "_banner", "_tulip", "_coral", "_coral_fan",
               "_coral_wall_fan", "_rail", "_candle", "_head", "_skull", "_bud", "_mushroom", "_petals", "_vines", "_vines_plant")
THIN = {"iron_bars", "ladder", "scaffolding", "bamboo", "lily_pad"}
THIN_SUFFIX = ("_fence", "_fence_gate", "_wall", "_pane", "_door", "_trapdoor", "_carpet")
SLABLIKE = {"campfire", "soul_campfire", "stonecutter", "daylight_detector", "sculk_sensor", "calibrated_sculk_sensor", "cake",
            "candle_cake", "decorated_pot"}
SHAPE_SUFFIX = ("_stairs", "_slab", "_wall", "_fence_gate", "_fence", "_pane", "_trapdoor", "_door", "_button", "_pressure_plate", "_carpet")
DYED_WOOL = ("wool", "carpet", "bed", "banner", "wall_banner")
DYED_CONCRETE = ("concrete", "concrete_powder", "terracotta", "glazed_terracotta", "shulker_box", "candle", "candle_cake")
FAMILY = [   # name token(s) -> palette name, first match wins (specific before general)
    ("nether_brick", "nether_bricks"), ("netherrack", "netherrack"), ("nylium", "netherrack"), ("mud_brick", "mud_bricks"),
    ("end_stone", "end_stone"), ("mossy_cobblestone", "mossy_cobblestone"), ("mossy_stone_brick", "mossy_stone_bricks"),
    ("cracked_stone_brick", "cracked_stone_bricks"), ("stone_brick", "stone_bricks"), ("cobbled_deepslate", "cobbled_deepslate"),
    ("deepslate_tile", "deepslate_tiles"), ("deepslate", "deepslate"), ("blackstone", "blackstone"), ("basalt", "basalt"),
    ("red_sandstone", "red_sandstone"), ("sandstone", "sandstone"), ("red_sand", "red_sand"), ("sand", "sand"),
    ("prismarine", "prismarine"), ("quartz", "quartz_block"), ("purpur", "purpur_block"), ("oxidized", "oxidized_copper"),
    ("weathered", "oxidized_copper"), ("copper", "copper_block"), ("cobblestone", "cobblestone"), ("granite", "granite"),
    ("diorite", "diorite"), ("andesite", "andesite"), ("tuff", "tuff"), ("calcite", "calcite"), ("brick", "bricks"),
    ("terracotta", "terracotta"), ("concrete", "white_concrete"), ("wool", "white_wool"), ("glass", "glass"), ("ice", "ice"),
    ("snow", "snow_block"), ("obsidian", "obsidian"), ("dirt", "dirt"), ("mud", "mud"), ("moss", "moss_block"),
    ("leaves", "oak_leaves"), ("smooth_stone", "smooth_stone"), ("stone", "stone"), ("iron", "iron_block"), ("gold", "gold_block"),
    ("golden", "gold_block"), ("diamond", "diamond_block"), ("emerald", "emerald_block"), ("lapis", "lapis_block"),
    ("redstone", "redstone_block"), ("coal", "coal_block"), ("amethyst", "amethyst_block"), ("netherite", "netherite_block"),
    ("honey", "honey_block"), ("slime", "slime_block"), ("hay", "hay_block"), ("pumpkin", "pumpkin"), ("melon", "melon"),
    ("planks", "oak_planks"), ("log", "oak_log"), ("wood", "oak_log"),
]


def _chroma(c):
    c = np.asarray(c, np.float64); m = c.max()
    return c / m if m > 0 else c


def _separate(rgb, sep=SEP):
    """rgb -> rgb whose chroma is >= sep from every PLAYER_RGB chroma (same max channel; module docstring)."""
    c = np.asarray(rgb, np.float64); m = c.max()
    if m <= 0:
        return tuple(int(v) for v in rgb)
    q = c / m
    for _ in range(4):
        moved = False
        for p in PLAYER_RGB:
            p = _chroma(p); d = q - p; n = float(np.linalg.norm(d))
            if n < sep:
                u = d / n if n > 1e-9 else np.array([0.0, 0.0, 1.0]); q = np.clip(p + u * sep * 1.03, 0, 1); moved = True
        if not moved:
            break
    return tuple(int(v) for v in np.clip(np.round(q * m), 0, 255))


def _lab(rgb):
    """sRGB uint8 [..., 3] -> CIE Lab (D65)."""
    c = np.asarray(rgb, np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = c @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T / [0.95047, 1.0, 1.08883]
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


NUDGED = {}
PALETTE = []
for _i, (_n, _c, _k) in enumerate(_RAW):
    _s = tuple(_c) if _i < 12 else _separate(_c)
    if _s != tuple(_c):
        NUDGED[_n] = (tuple(_c), _s)
    PALETTE.append((_n, _s))
N = len(PALETTE)
NAMES = [n for n, _ in PALETTE]
ID = {n: i for i, n in enumerate(NAMES)}
BLOCK_RGB = np.array([c for _, c in PALETTE], np.uint8)
AIR, WATER, LAVA, FIRE, TNT, UNKNOWN = 0, ID["water"], ID["lava"], ID["fire"], ID["tnt"], ID["unknown"]
COLLIDES = np.ones(N, bool); COLLIDES[[AIR, WATER, LAVA, FIRE]] = False
V0_CANDS = (1, 2, 3, 5, 6, 7, 8, 10, 11)                                       # the v0 material candidates (no air, bedrock, tnt)
_V0K = {"P": 5, "L": 8, "S": 2, "C": 6, "G": 7, "D": 3}
_op = [c for c in V0_CANDS if c != 7]; _labs = _lab(BLOCK_RGB[_op])
V0 = np.array([i if i < 12 else _V0K[k] if k else _op[int(np.argmin(((_labs - _lab(BLOCK_RGB[i])) ** 2).sum(1)))]
               for i, (_, _, k) in enumerate(_RAW)], np.uint8)
del _i, _n, _c, _k, _s, _op, _labs


def norm(s):
    """Block-state / item string -> bare lower-case id ("minecraft:Oak_Stairs[facing=east]" -> "oak_stairs")."""
    s = str(s).strip()
    for ch in "[{":
        i = s.find(ch); s = s[:i] if i >= 0 else s
    s = s.strip().lower().replace(" ", "_")
    s = s.split(":", 1)[1] if ":" in s else s
    return s[7:] if s.startswith("legacy_") else s


def props(state):
    """Block-state properties as a dict ("x[a=1,b=c]" -> {"a": "1", "b": "c"})."""
    s = str(state); i, j = s.find("["), s.rfind("]")
    if i < 0 or j < i:
        return {}
    return dict(kv.split("=", 1) for kv in s[i + 1:j].split(",") if "=" in kv)


def _has(n, key):
    """Name token(s) key in n ("stone_brick" in "mossy_stone_bricks_wall"; a plural s on its last token allowed)."""
    t = f"_{n}_"
    return f"_{key}_" in t or f"_{key}s_" in t


def _deco(n):
    return n in DECO or (n.endswith(DECO_SUFFIX) and n != "piston_head") or n.startswith("potted_")


@lru_cache(maxsize=None)
def _resolve(n):
    """Bare name -> palette id ignoring decoration status (module docstring, block_id)."""
    if n in ID:
        return ID[n]
    if n in ALIAS:
        return _resolve(ALIAS[n])
    if not n or n in AIRS:
        return AIR
    for p in ("waxed_", "infested_", "potted_"):
        if n.startswith(p):
            return _resolve(n[len(p):])
    for c in DYES:
        if n.startswith(c + "_"):
            r = n[len(c) + 1:]
            if r in DYED_WOOL:
                return ID[f"{c}_wool"]
            if r.startswith("stained_glass"):
                return ID[f"{c}_stained_glass"]
            if r in DYED_CONCRETE:
                return ID[f"{c}_concrete"]
            break
    for sp in SPECIES:
        if _has(n, sp):
            s = {"pale_oak": "birch", "bamboo": "bamboo"}.get(sp, sp)
            if "leaves" in n:
                return ID.get(f"{sp}_leaves", ID["oak_leaves"])
            if not n.startswith("stripped_") and any(_has(n, k) for k in ("log", "wood", "stem", "hyphae")):
                return ID.get({"crimson": "crimson_stem", "warped": "warped_stem", "pale_oak": "acacia_log", "bamboo": "bamboo_planks"}.get(sp, f"{sp}_log"), ID["oak_log"])
            if sp in ("crimson", "warped") and _has(n, "nylium"):
                return _resolve(f"{sp}_nylium")
            return ID[f"{s}_planks"]
    for suf in SHAPE_SUFFIX:
        if n.endswith(suf) and len(n) > len(suf):
            b = n[:-len(suf)]
            for c in (b, b + "s", b + "_block", b + "_planks"):
                if c in ID or c in ALIAS:
                    return _resolve(c)
            return _resolve(b)
    if n.endswith("_ore"):
        return ID["deepslate"] if n.startswith("deepslate_") else ID["netherrack"] if "nether" in n else ID["stone"]
    for key, tgt in FAMILY:
        if _has(n, key):
            return ID[tgt]
    return UNKNOWN


@lru_cache(maxsize=65536)
def block_id(state):
    """Block-state string (or bare / Bukkit id; None -> air) -> palette id (module docstring)."""
    if state is None:
        return AIR
    if isinstance(state, (int, np.integer)):
        return int(state) if 0 <= int(state) < N else UNKNOWN
    n = norm(state)
    if n in WATERY:
        return WATER
    if n in LAVAS:
        return LAVA
    if n in AIRS or not n:
        return AIR
    if _deco(n):
        return WATER if props(state).get("waterlogged") == "true" else AIR
    return _resolve(n)


@lru_cache(maxsize=65536)
def shape_of(state):
    """Block-state string -> one of SHAPES (module docstring)."""
    if state is None:
        return "none"
    n, p = norm(state), props(state)
    if n in WATERY or n in LAVAS:
        return "liquid"
    if not n or n in AIRS:
        return "none"
    if _deco(n):
        return "liquid" if p.get("waterlogged") == "true" else "none"
    if n.endswith("_slab"):
        return {"top": "slab_top", "double": "full"}.get(p.get("type", "bottom"), "slab_bottom")
    if n.endswith("_stairs"):
        return "stairs"
    if n == "snow":
        k = int(p.get("layers", 1)) if str(p.get("layers", 1)).isdigit() else 1
        return "full" if k >= 8 else "slab_bottom" if k >= 4 else "thin"
    if n in THIN or n.endswith(THIN_SUFFIX):
        return "thin"
    if n in SLABLIKE or n.endswith("_bed"):
        return "slab_bottom"
    return "full"


def ids_of(states):
    """Iterable of block-state strings (a snapshot palette) -> uint16 [len] palette ids."""
    return np.array([block_id(s) for s in states], np.uint16)


def to_v0(ids):
    """Any palette-id array -> palette v0 ids (the 12 mc.py ids), same shape, uint8."""
    return V0[np.minimum(np.asarray(ids, np.int64), N - 1)]


# ---- items ----------------------------------------------------------------------------------------------------------
TOOL_MAT = {"wooden": (150, 116, 65), "wood": (150, 116, 65), "stone": (128, 128, 128), "iron": (210, 210, 210),
            "golden": (234, 200, 70), "gold": (234, 200, 70), "diamond": (80, 220, 205), "netherite": (75, 65, 70),
            "leather": (140, 85, 50), "chainmail": (150, 150, 155), "turtle": (70, 160, 60), "copper": (200, 110, 80)}
TOOL_KIND = ("sword", "axe", "pickaxe", "shovel", "hoe", "helmet", "chestplate", "leggings", "boots", "horse_armor", "spear")
_ITEM = {
    "bow": (125, 90, 50), "crossbow": (100, 75, 50), "arrow": (160, 150, 140), "spectral_arrow": (210, 180, 80), "tipped_arrow": (160, 150, 140),
    "trident": (80, 150, 140), "mace": (120, 115, 120), "shield": (150, 110, 70), "fishing_rod": (120, 90, 60),
    "flint_and_steel": (125, 125, 130), "shears": (200, 200, 200), "bucket": (185, 185, 190), "water_bucket": (65, 110, 215),
    "lava_bucket": (210, 95, 25), "milk_bucket": (235, 235, 235), "powder_snow_bucket": (230, 240, 245), "stick": (115, 85, 45),
    "torch": (235, 185, 90), "soul_torch": (90, 190, 200), "redstone_torch": (200, 40, 20), "lantern": (90, 85, 80),
    "apple": (205, 35, 30), "golden_apple": (235, 190, 60), "enchanted_golden_apple": (235, 190, 60), "bread": (185, 135, 65),
    "cookie": (195, 125, 60), "cake": (230, 215, 200), "pumpkin_pie": (205, 125, 55), "melon_slice": (205, 60, 50),
    "sweet_berries": (170, 30, 40), "glow_berries": (240, 170, 60), "carrot": (240, 140, 30), "golden_carrot": (240, 195, 60),
    "potato": (205, 170, 95), "baked_potato": (195, 150, 75), "poisonous_potato": (175, 170, 80), "beetroot": (165, 40, 50),
    "beetroot_soup": (150, 40, 40), "mushroom_stew": (175, 125, 90), "rabbit_stew": (170, 110, 70), "suspicious_stew": (175, 125, 90),
    "beef": (195, 65, 60), "cooked_beef": (125, 75, 50), "porkchop": (230, 145, 140), "cooked_porkchop": (205, 155, 110),
    "chicken": (235, 195, 175), "cooked_chicken": (205, 145, 85), "mutton": (200, 70, 65), "cooked_mutton": (150, 95, 70),
    "rabbit": (215, 160, 140), "cooked_rabbit": (175, 115, 75), "cod": (175, 150, 120), "cooked_cod": (200, 175, 140),
    "salmon": (170, 70, 60), "cooked_salmon": (190, 110, 80), "tropical_fish": (230, 120, 40), "pufferfish": (220, 190, 60),
    "rotten_flesh": (135, 85, 60), "spider_eye": (150, 40, 60), "honey_bottle": (240, 170, 50), "dried_kelp": (60, 75, 40),
    "chorus_fruit": (140, 100, 140), "potion": (90, 70, 190), "splash_potion": (90, 70, 190), "lingering_potion": (90, 70, 190),
    "experience_bottle": (140, 220, 90), "ender_pearl": (35, 95, 85), "ender_eye": (60, 130, 90), "snowball": (240, 250, 250),
    "egg": (225, 205, 165), "compass": (125, 125, 135), "recovery_compass": (60, 90, 110), "clock": (225, 190, 60),
    "map": (210, 200, 170), "filled_map": (210, 200, 170), "book": (115, 75, 45), "writable_book": (115, 75, 45),
    "written_book": (115, 75, 45), "enchanted_book": (130, 70, 150), "paper": (230, 230, 220), "feather": (230, 230, 230),
    "flint": (60, 60, 60), "coal": (35, 35, 35), "charcoal": (50, 45, 40), "iron_ingot": (215, 215, 215), "gold_ingot": (240, 205, 70),
    "copper_ingot": (200, 110, 80), "netherite_ingot": (70, 60, 65), "netherite_scrap": (90, 70, 65), "diamond": (90, 220, 210),
    "emerald": (45, 200, 95), "redstone": (175, 25, 15), "lapis_lazuli": (40, 75, 165), "quartz": (230, 225, 215),
    "amethyst_shard": (150, 105, 200), "bone": (225, 225, 205), "bone_meal": (230, 230, 225), "gunpowder": (105, 105, 105),
    "string": (235, 235, 235), "leather": (140, 85, 50), "rabbit_hide": (190, 150, 110), "slime_ball": (110, 190, 90),
    "blaze_rod": (230, 170, 50), "blaze_powder": (230, 150, 40), "fire_charge": (165, 85, 40), "firework_rocket": (180, 60, 50),
    "totem_of_undying": (225, 195, 90), "elytra": (130, 130, 150), "saddle": (140, 85, 50), "lead": (160, 120, 80),
    "name_tag": (200, 190, 170), "wheat": (215, 190, 90), "wheat_seeds": (90, 160, 50), "sugar": (240, 240, 240),
    "brick": (160, 90, 70), "nether_brick": (70, 35, 40), "clay_ball": (165, 170, 185), "glowstone_dust": (230, 190, 90),
    "glass_bottle": (200, 215, 225), "bowl": (130, 95, 55), "minecart": (130, 130, 135), "tnt_minecart": (200, 50, 40),
    "poppy": (190, 30, 25), "dandelion": (230, 200, 40), "sugar_cane": (140, 190, 90), "short_grass": (90, 140, 50),
    "tall_grass": (90, 140, 50), "fern": (80, 130, 50), "vine": (60, 110, 30), "cobweb": (220, 220, 220), "rail": (130, 110, 90),
    "redstone_wire": (175, 25, 15), "lever": (120, 100, 80), "flower_pot": (120, 65, 50), "chain": (60, 65, 75), "end_rod": (230, 225, 210),
    "lightning_rod": (200, 110, 80), "candle": (225, 210, 170), "spyglass": (180, 120, 70), "goat_horn": (180, 170, 150),
    "brush": (170, 140, 100), "wind_charge": (200, 210, 230), "breeze_rod": (200, 210, 230), "ink_sac": (40, 40, 50),
    "glow_ink_sac": (80, 190, 170), "nautilus_shell": (220, 200, 180), "heart_of_the_sea": (40, 120, 200), "phantom_membrane": (200, 190, 170),
}
ITEM_RGB = {k: _separate(v) for k, v in _ITEM.items()}
_TOOL = {k: _separate(v) for k, v in TOOL_MAT.items()}
_EMPTY = {"", "air", "none", "null", "empty", "cave_air", "void_air"}


def is_empty(item):
    """True for an empty slot / hand (None, "", "air", a {"name": ...} dict of those, count 0)."""
    if isinstance(item, dict):
        return item.get("name") is None or norm(item["name"]) in _EMPTY or item.get("count", 1) == 0
    return item is None or norm(item) in _EMPTY


@lru_cache(maxsize=4096)
def _item_rgb(n):
    if n in ITEM_RGB:
        return ITEM_RGB[n]
    for m in TOOL_MAT:
        if n.startswith(m + "_") and n[len(m) + 1:] in TOOL_KIND:
            return _TOOL[m]
    if n.endswith("_dye"):
        c = n[:-4]; return tuple(int(v) for v in BLOCK_RGB[ID[f"{c}_wool"]]) if f"{c}_wool" in ID else ITEM_UNKNOWN_RGB
    if n.endswith("_spawn_egg") or n.startswith("music_disc") or n.endswith(("_pottery_sherd", "_banner_pattern", "_smithing_template")):
        return ITEM_UNKNOWN_RGB
    if n.endswith("_bucket"):
        return ITEM_RGB["water_bucket"]
    b = block_id(n)
    if b not in (AIR, UNKNOWN):
        return tuple(int(v) for v in BLOCK_RGB[b])
    for c in DYES:                                                              # flowers, candles, banners of a colour
        if _has(n, c):
            return tuple(int(v) for v in BLOCK_RGB[ID[f"{c}_wool"]])
    if any(_has(n, k) for k in ("sapling", "leaves", "grass", "fern", "vine", "vines", "propagule", "petals", "bush")):
        return _separate((70, 130, 40))
    b = _resolve(n)                                                             # signs, boats, buttons of a wood / stone
    return tuple(int(v) for v in BLOCK_RGB[b]) if b not in (AIR, UNKNOWN) else ITEM_UNKNOWN_RGB


def item_rgb(item):
    """Held / hotbar item (name string, Bukkit name, prismarine-item dict, None) -> (r, g, b) (module docstring)."""
    if isinstance(item, dict):
        item = item.get("name") if item.get("name") else None
    if is_empty(item):
        return SKIN_RGB
    return _item_rgb(norm(item))


# ---- selftest ---------------------------------------------------------------------------------------------------------
def _mc_literals(path=MC_PY):
    """mc.py's BLOCK_NAMES / BLOCK_RGB read with ast (no torch import)."""
    tree = ast.parse(open(path).read()); out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            nm = node.targets[0].id
            if nm == "BLOCK_NAMES":
                out[nm] = ast.literal_eval(node.value)
            elif nm == "BLOCK_RGB":
                out[nm] = ast.literal_eval(node.value.args[0])
    return out


def selftest():
    """-> list of failed checks (empty = pass)."""
    fails = []

    def check(ok, msg):
        print(("PASS " if ok else "FAIL ") + msg, flush=True)
        if not ok:
            fails.append(msg)
    lit = _mc_literals()
    check(NAMES[:12] == lit["BLOCK_NAMES"] and BLOCK_RGB[:12].tolist() == lit["BLOCK_RGB"], "ids 0..11 == mc.py BLOCK_NAMES / BLOCK_RGB (ast)")
    try:
        from mineengine import mc as MCW  # noqa: E402  (pulls torch via voxel_proxy)
        check(NAMES[:12] == MCW.BLOCK_NAMES and np.array_equal(BLOCK_RGB[:12], MCW.BLOCK_RGB), "ids 0..11 == imported mc.BLOCK_NAMES / BLOCK_RGB")
    except Exception as e:                                                      # noqa: BLE001
        print(f"SKIP import mc ({type(e).__name__}: {e})")
    if os.path.exists(REF_JSON):
        ref = json.load(open(REF_JSON, encoding="utf-8"))
        check([(b["id"], b["name"], tuple(b["rgb"])) for b in ref["blocks"]] == [(i, n, c) for i, (n, c) in enumerate(PALETTE[:12])]
              and [tuple(ref["player_colors"][k]) for k in ("team0", "team1")] == list(PLAYER_RGB) and tuple(ref["sky_rgb"]) == SKY_RGB,
              "ids 0..11, player and sky colours == block_palette_v0.json")
    check(len(set(NAMES)) == N and 100 <= N < 256 and BLOCK_RGB.shape == (N, 3) and BLOCK_RGB.dtype == np.uint8, f"{N} unique names, uint8 [N, 3], N < 256")
    check(all(block_id(n) == i for i, n in enumerate(NAMES)) and all(block_id(f"minecraft:{n}") == i for i, n in enumerate(NAMES)),
          "block_id(name) == id for every palette name (bare and minecraft:)")
    cases = {
        "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]": "oak_planks", "OAK_STAIRS": "oak_planks",
        "minecraft:spruce_slab[type=top,waterlogged=false]": "spruce_planks", "dark_oak_fence": "dark_oak_planks",
        "minecraft:dark_oak_log[axis=y]": "dark_oak_log", "stripped_birch_log": "birch_planks", "birch_wood": "birch_log",
        "crimson_hyphae": "crimson_stem", "crimson_nylium": "netherrack", "minecraft:stone_brick_stairs[facing=north]": "stone_bricks",
        "mossy_stone_brick_wall": "mossy_stone_bricks", "cracked_stone_bricks": "cracked_stone_bricks", "chiseled_stone_bricks": "stone_bricks",
        "end_stone_brick_wall": "end_stone", "brick_slab": "bricks", "nether_brick_fence": "nether_bricks", "red_nether_bricks": "nether_bricks",
        "smooth_stone_slab": "smooth_stone", "polished_granite": "granite", "polished_blackstone_brick_stairs": "blackstone",
        "deepslate_tile_wall": "deepslate_tiles", "deepslate_iron_ore": "deepslate", "iron_ore": "stone", "nether_gold_ore": "netherrack",
        "cut_sandstone": "sandstone", "smooth_red_sandstone_slab": "red_sandstone", "red_sand": "red_sand", "quartz_slab": "quartz_block",
        "waxed_oxidized_cut_copper_stairs": "oxidized_copper", "cut_copper": "copper_block", "iron_door": "iron_block",
        "iron_bars": "iron_block", "glass_pane": "glass", "minecraft:lime_stained_glass_pane[east=true]": "lime_stained_glass",
        "white_carpet": "white_wool", "red_bed[part=head]": "red_wool", "blue_wool": "blue_wool", "light_blue_concrete_powder": "light_blue_concrete",
        "light_gray_glazed_terracotta": "light_gray_concrete", "gray_terracotta": "gray_concrete", "terracotta": "terracotta",
        "minecraft:water[level=0]": "water", "flowing_lava": "lava", "seagrass": "water", "kelp_plant": "water", "bubble_column[drag=true]": "water",
        "minecraft:fire[age=3]": "fire", "soul_fire": "fire", "tnt[unstable=false]": "tnt", "snow[layers=3]": "snow", "powder_snow": "snow_block",
        "farmland[moisture=7]": "farmland", "dirt_path": "dirt_path", "grass_path": "dirt_path", "oak_leaves[distance=3]": "oak_leaves",
        "azalea_leaves": "oak_leaves", "cherry_leaves": "cherry_leaves", "pale_oak_planks": "birch_planks", "bamboo_block": "bamboo_planks",
        "obsidian": "obsidian", "crying_obsidian": "obsidian", "packed_ice": "packed_ice", "frosted_ice": "ice", "gravel": "gravel",
        "jack_o_lantern": "pumpkin", "hay_block[axis=y]": "hay_block", "infested_stone_bricks": "stone_bricks", "raw_iron_block": "iron_block",
        "minecraft:foo_bar_baz": "unknown", "piston[extended=false]": "furnace", "piston_head[facing=up]": "furnace", "moss_carpet": "moss_block",
        "dead_brain_coral_block": "light_gray_concrete", "horn_coral_block": "yellow_concrete", "sea_pickle[waterlogged=false]": "air",
    }
    bad = {s: (NAMES[block_id(s)], want) for s, want in cases.items() if block_id(s) != ID[want]}
    check(not bad, f"block_id on {len(cases)} named states / families {bad if bad else ''}")
    deco = ["minecraft:torch", "wall_torch[facing=east]", "minecraft:short_grass", "grass", "tall_grass[half=upper]", "poppy", "red_tulip",
            "minecraft:rail[shape=north_south]", "powered_rail", "oak_sapling[stage=0]", "oak_sign[rotation=0]", "oak_wall_sign", "stone_button",
            "oak_pressure_plate", "redstone_wire[power=0]", "vine[east=true]", "wheat[age=7]", "lantern[hanging=false]", "potted_poppy",
            "white_candle", "creeper_head", "brain_coral_fan", "cobweb", "sugar_cane", "air", "cave_air", "barrier", None]
    check(all(block_id(s) == AIR for s in deco) and block_id("lantern[waterlogged=true]") == WATER and block_id("tube_coral[waterlogged=true]") == WATER,
          f"decorations -> 0 ({len(deco)} states), waterlogged decorations -> water")
    shp = {"stone": "full", "oak_slab": "slab_bottom", "oak_slab[type=top,waterlogged=false]": "slab_top", "oak_slab[type=double]": "full",
           "minecraft:oak_stairs[half=top]": "stairs", "glass_pane": "thin", "oak_fence[east=true]": "thin", "cobblestone_wall": "thin",
           "oak_door[half=lower]": "thin", "iron_trapdoor": "thin", "white_carpet": "thin", "ladder": "thin", "snow[layers=1]": "thin",
           "snow[layers=5]": "slab_bottom", "snow[layers=8]": "full", "red_bed[part=foot]": "slab_bottom", "water[level=0]": "liquid",
           "lava": "liquid", "seagrass": "liquid", "sea_pickle[waterlogged=true]": "liquid", "piston_head[facing=up]": "full", "lantern[waterlogged=true]": "liquid",
           "brain_coral_block": "full", "torch": "none", "air": "none", "short_grass": "none", "fire[age=0]": "full", "tnt": "full", "foo_bar": "full", None: "none",
           "oak_wall_sign[waterlogged=false]": "none", "andesite_wall[up=true]": "thin", "stone_brick_slab[type=bottom]": "slab_bottom"}
    bad = {s: (shape_of(s), want) for s, want in shp.items() if shape_of(s) != want}
    check(not bad and all(shape_of(n) in SHAPES for n in NAMES), f"shape_of on {len(shp)} states, every palette name in SHAPES {bad if bad else ''}")
    check(not COLLIDES[[AIR, WATER, LAVA, FIRE]].any() and COLLIDES[[1, 2, TNT, UNKNOWN]].all(), "COLLIDES: air / water / lava / fire False")
    ids = ids_of(["minecraft:stone", "oak_stairs[facing=east]", "minecraft:torch", "water"])
    check(ids.dtype == np.uint16 and ids.tolist() == [2, ID["oak_planks"], 0, WATER], "ids_of(snapshot palette) -> uint16")
    v = to_v0(np.arange(N).reshape(-1, 1))
    check(v.dtype == np.uint8 and v.shape == (N, 1) and (V0[:12] == np.arange(12)).all() and set(V0[12:].tolist()) <= set(V0_CANDS)
          and V0[ID["spruce_planks"]] == 5 and V0[ID["birch_log"]] == 8 and V0[ID["stone_bricks"]] == 2 and V0[ID["bricks"]] == 6
          and V0[ID["ice"]] == 7 and V0[ID["red_concrete"]] == 10 and V0[ID["blue_concrete"]] == 11 and V0[ID["oak_leaves"]] == 1
          and V0[ID["water"]] == 11 and V0[UNKNOWN] == 6, "V0 / to_v0: legacy ids fixed, extended ids -> V0_CANDS by class / Lab colour")
    pc = np.stack([_chroma(p) for p in PLAYER_RGB])
    dist = np.array([np.linalg.norm(_chroma(c)[None] - pc, axis=1).min() if max(c) > 0 else 9 for _, c in PALETTE])
    check(bool((dist[12:] >= SEP - 1e-3).all()), f"extended block colours >= {SEP} chroma from the player colours (min {dist[12:].min():.3f}; nudged {len(NUDGED)}: "
          + ", ".join(f"{k} {a}->{b}" for k, (a, b) in NUDGED.items()) + ")")
    print(f"     legacy ids' chroma distance to the players (unchanged): min {dist[1:12].min():.3f} ({NAMES[1 + int(np.argmin(dist[1:12]))]})")
    it = {"diamond_sword": _TOOL["diamond"], "minecraft:iron_pickaxe": _TOOL["iron"], "GOLDEN_AXE": _TOOL["golden"], "wooden_shovel": _TOOL["wooden"],
          "bow": ITEM_RGB["bow"], "flint_and_steel": ITEM_RGB["flint_and_steel"], "water_bucket": ITEM_RGB["water_bucket"], "cod_bucket": ITEM_RGB["water_bucket"],
          "cooked_beef": ITEM_RGB["cooked_beef"], "oak_planks": tuple(BLOCK_RGB[5].tolist()), "minecraft:white_wool": tuple(BLOCK_RGB[ID["white_wool"]].tolist()),
          "oak_stairs": tuple(BLOCK_RGB[5].tolist()), "red_dye": tuple(BLOCK_RGB[10].tolist()), "red_tulip": tuple(BLOCK_RGB[10].tolist()),
          "oak_sign": tuple(BLOCK_RGB[5].tolist()), "torch": ITEM_RGB["torch"], None: SKIN_RGB, "": SKIN_RGB, "minecraft:air": SKIN_RGB,
          "zombie_spawn_egg": ITEM_UNKNOWN_RGB, "foo_widget": ITEM_UNKNOWN_RGB, "tnt": tuple(BLOCK_RGB[TNT].tolist()),
          "oak_sapling": _separate((70, 130, 40)), "diamond_helmet": _TOOL["diamond"]}
    bad = {str(k): (item_rgb(k), w) for k, w in it.items() if tuple(item_rgb(k)) != tuple(w)}
    allrgb = [item_rgb(k) for k in list(_ITEM) + [f"{m}_{k}" for m in TOOL_MAT for k in TOOL_KIND] + NAMES]
    check(not bad and all(len(c) == 3 and all(isinstance(x, int) and 0 <= x <= 255 for x in c) for c in allrgb),
          f"item_rgb on {len(it)} items; every item colour is 3 ints in 0..255 {bad if bad else ''}")
    check(item_rgb({"name": "bow", "count": 1}) == ITEM_RGB["bow"] and is_empty({"name": "air"}) and is_empty(None) and is_empty({"name": "stone", "count": 0})
          and not is_empty("stone") and not is_empty({"name": "bow", "count": 1}), "item_rgb / is_empty on prismarine-item dicts")
    idist = [np.linalg.norm(_chroma(c)[None] - pc, axis=1).min() for c in list(ITEM_RGB.values()) + list(_TOOL.values()) if max(c) > 0]
    check(min(idist) >= SEP - 1e-3, f"item colours >= {SEP} chroma from the player colours (min {min(idist):.3f})")
    return fails


def write_json(path):
    """The palette in data/block_palette_v0.json layout (+ each id's palette v0 fold)."""
    js = {"note": "cap_palette.py: ids 0..11 = mc.py / palette v0; v0_id = the 12-id fold (to_v0)",
          "blocks": [{"id": i, "name": n, "rgb": list(c), "v0_id": int(V0[i])} for i, (n, c) in enumerate(PALETTE)],
          "player_colors": {"team0": list(PLAYER_RGB[0]), "team1": list(PLAYER_RGB[1])}, "sky_rgb": list(SKY_RGB)}
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


if __name__ == "__main__":
    if "--test" in sys.argv:
        f = selftest()
        print("[cap_palette]", "PASS" if not f else f"FAIL {f}", flush=True)
        sys.exit(1 if f else 0)
    if "--json" in sys.argv:
        write_json(sys.argv[sys.argv.index("--json") + 1]); print(f"[cap_palette] {N} blocks -> {sys.argv[sys.argv.index('--json') + 1]}")
