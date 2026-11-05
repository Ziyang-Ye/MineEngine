/**
 * forestBuild - (normal worlds only) fell 3-6 trees, then build a small hut
 * in the clearing. Felling is real player work: a few leaves are broken by
 * hand, every log is chopped bottom-up with an axe, and the rest of the
 * canopy decays (leaves_decay events; randomTickSpeed is raised before the
 * start snapshot, like iceNearFire, and reset in teardown).
 *
 * Exact world log: trees are felled per "grove" = all simple trunks (one
 * vertical column of 3-7 logs, no branches / 2x2 trunks, nothing attached)
 * whose canopies form ONE connected leaf component (6-connected, <= 700
 * non-persistent, non-waterlogged leaves) that touches no other log and
 * carries nothing (vines, cocoa, bee nests, snow on leaves...). A lone tree
 * is a grove of one. Whole groves are felled, so after the last trunk every
 * leaf of the component decays and none is left with an unlogged `distance`
 * change. The episode waits for (and breaks) the remaining canopy before it
 * ends. Because randomTickSpeed is raised, sites with blocks whose state
 * changes on random ticks without a Bukkit event (sugar cane / cactus age,
 * sapling stage, farmland moisture, turtle eggs, lit redstone ore) inside the
 * snapshot box are skipped.
 *
 * Flat worlds: setupEpisode throws before any world change (clean abort;
 * the episode is recorded as an error and skipped). The episode loop
 * schedules this type only for normal worlds (REQUIRES_NON_FLAT_WORLD).
 *
 * Plan (shared RNG): tree count 3-6 (limited by the isolated trees found),
 * palette (walls, pillar, window, roof), hut 4x4 / 4x5 / 5x5, orientation,
 * door side, corner pillars, window count, shears for the leaves.
 * Roles: both bots fell (trees alternate), both build the walls (halves),
 * the other bot roofs the hut from the inside while the actor watches.
 */

const { Vec3 } = require("vec3");

const dc = require("./diverse-common");
const sc = require("../state/state-common");
const { countMaterials, nonFullFraction, paletteMetadata, pickPalette } = require("../../utils/materials");
const { isWorldStateLeader } = require("../../utils/world-state");

const { randInt, note, sleep } = dc;

const N6 = [
  new Vec3(1, 0, 0),
  new Vec3(-1, 0, 0),
  new Vec3(0, 1, 0),
  new Vec3(0, -1, 0),
  new Vec3(0, 0, 1),
  new Vec3(0, 0, -1),
];
const GROUND_RE = /^(dirt|grass_block|podzol|coarse_dirt|rooted_dirt|mycelium|moss_block|mud|snow_block|sand|gravel|stone)$/;

function propOf(block, key) {
  try {
    const p = block.getProperties ? block.getProperties() : {};
    const v = p ? p[key] : undefined;
    if (v === undefined) return undefined;
    if (v === "true") return true;
    if (v === "false") return false;
    return v;
  } catch (_) {
    return undefined;
  }
}

const ATTACHED_TRUNK_RE = /(vine|cocoa|bee_nest|beehive|lichen|moss_carpet|snow|mushroom|hanging_roots|propagule|sign|torch)/;
const ATTACHED_CANOPY_RE = /(vine|cocoa|bee_nest|lichen|snow|moss|roots|propagule|spore)/;
// blocks whose state changes on random ticks WITHOUT a Bukkit event (no exact
// log possible while randomTickSpeed is raised): keep them out of the box
const RANDOM_TICK_RISK = ["sugar_cane", "cactus", "farmland", "turtle_egg", "sniffer_egg",
  "oak_sapling", "spruce_sapling", "birch_sapling", "jungle_sapling", "acacia_sapling",
  "dark_oak_sapling", "cherry_sapling", "mangrove_propagule", "redstone_ore", "deepslate_redstone_ore"];

function isLogAt(bot, p) {
  const b = bot.blockAt(p);
  return !!b && b.name.endsWith("_log") && !b.name.startsWith("stripped");
}

/**
 * A felling-safe trunk: one vertical column of 3-7 logs standing on ground,
 * no other log in its 26-neighbourhood (branches, 2x2 trunks), nothing
 * attached to it, and a stand spot next to it.
 * @returns {{base: Vec3, type: string, logs: Vec3[]}|null}
 */
function simpleTrunk(bot, base) {
  const below = bot.blockAt(base.offset(0, -1, 0));
  if (!below || !GROUND_RE.test(below.name)) return null;
  const type = bot.blockAt(base)?.name;
  const logs = [];
  let p = base.clone();
  while (isLogAt(bot, p) && logs.length < 12) {
    logs.push(p.clone());
    p = p.offset(0, 1, 0);
  }
  if (logs.length < 3 || logs.length > 7) return null;
  const logKeys = new Set(logs.map(dc.key3));
  for (const l of logs) {
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
      const q = l.offset(dx, dy, dz);
      if (!logKeys.has(dc.key3(q)) && isLogAt(bot, q)) return null;
    }
    for (const n of N6) {
      const b = bot.blockAt(l.plus(n));
      if (b && ATTACHED_TRUNK_RE.test(b.name)) return null;
    }
  }
  let stand = false;
  for (const n of N6) {
    if (n.y !== 0) continue;
    const s = base.plus(n);
    if (dc.standable(bot, s.x, s.y, s.z)) stand = true;
  }
  return stand ? { base, type, logs } : null;
}

/**
 * Groves = sets of simple trunks whose canopies form ONE connected leaf
 * component (6-connectivity) that touches no other log: after every trunk of
 * a grove is chopped, all of its leaves decay (`leaves_decay` events), so no
 * leaf keeps an unlogged `distance` change. A lone tree is a grove of one.
 * Deterministic order (distance to `center`, then x, z).
 * @returns {Array<{trees: Array, leaves: Vec3[]}>}
 */
function findGroves(bot, center, maxDistance = 96, { maxTrees = 6, maxLeaves = 700 } = {}) {
  const ids = bot.registry.blocksArray
    .filter((b) => b.name.endsWith("_log") && !b.name.startsWith("stripped"))
    .map((b) => b.id);
  let found = [];
  try {
    found = bot.findBlocks({ point: center, matching: ids, maxDistance, count: 1500 });
  } catch (err) {
    console.log(`[${bot.username}] findBlocks failed: ${err.message}`);
    return [];
  }
  const bases = found
    .filter((p) => !isLogAt(bot, p.offset(0, -1, 0)))
    .sort((a, b) => a.distanceTo(center) - b.distanceTo(center) || a.x - b.x || a.z - b.z || a.y - b.y);
  const used = new Set();
  const groves = [];
  for (const base0 of bases) {
    if (used.has(dc.key3(base0))) continue;
    const t0 = simpleTrunk(bot, base0);
    if (!t0) continue;
    const trees = [t0];
    const logKeys = new Set(t0.logs.map(dc.key3));
    const leafKeys = new Set();
    const queue = [];
    const enqueueAround = (logs) => {
      for (const l of logs) {
        for (const n of N6) {
          const q = l.plus(n);
          const b = bot.blockAt(q);
          if (b && b.name.endsWith("_leaves") && !leafKeys.has(dc.key3(q))) {
            leafKeys.add(dc.key3(q));
            queue.push(q);
          }
        }
      }
    };
    enqueueAround(t0.logs);
    const leaves = [];
    let bad = false;
    while (queue.length && !bad) {
      const q = queue.shift();
      const b = bot.blockAt(q);
      if (!b || propOf(b, "persistent") === true || propOf(b, "waterlogged") === true) {
        bad = true;
        break;
      }
      leaves.push(q);
      if (leaves.length > maxLeaves) {
        bad = true;
        break;
      }
      for (const n of N6) {
        const r = q.plus(n);
        const k = dc.key3(r);
        const rb = bot.blockAt(r);
        if (!rb) {
          bad = true;
          break;
        }
        if (rb.name.endsWith("_leaves")) {
          if (!leafKeys.has(k)) {
            leafKeys.add(k);
            queue.push(r);
          }
        } else if (rb.name.endsWith("_log") || rb.name.endsWith("_wood")) {
          if (logKeys.has(k)) continue;
          // another trunk in the same canopy: it joins the grove (or spoils it)
          if (!isLogAt(bot, r)) {
            bad = true;
            break;
          }
          let bb = r;
          while (isLogAt(bot, bb.offset(0, -1, 0))) bb = bb.offset(0, -1, 0);
          const t = trees.length < maxTrees ? simpleTrunk(bot, bb) : null;
          if (!t || !t.logs.some((l) => dc.key3(l) === k)) {
            bad = true;
            break;
          }
          trees.push(t);
          for (const l of t.logs) logKeys.add(dc.key3(l));
          enqueueAround(t.logs);
        } else if (!dc.isAirName(rb.name)) {
          // something resting on / hanging from the canopy
          if (n.y === 1 || ATTACHED_CANOPY_RE.test(rb.name)) {
            bad = true;
            break;
          }
        }
      }
    }
    for (const t of trees) used.add(dc.key3(t.base));
    if (bad || leaves.length < 4) continue;
    for (const t of trees) t.leaves = leaves; // shared canopy
    groves.push({ trees, leaves });
    if (groves.length >= 16) break;
  }
  return groves;
}

/** Any random-tick-risk block inside the snapshot box around `site`? */
function hasRandomTickRisk(bot, site, args) {
  const ids = RANDOM_TICK_RISK.map((n) => bot.registry.blocksByName[n]?.id).filter((id) => id !== undefined);
  const r = Number(args.world_snapshot_radius) || 32;
  const down = Number(args.world_snapshot_down) || 24;
  const up = Number(args.world_snapshot_up) || 40;
  let hits = [];
  try {
    hits = bot.findBlocks({ point: site, matching: ids, maxDistance: Math.ceil(Math.sqrt(2 * r * r + up * up)) + 1, count: 64 });
  } catch (_) {
    return false;
  }
  return hits.some((p) => Math.abs(p.x - site.x) <= r + 1 && Math.abs(p.z - site.z) <= r + 1 && p.y >= site.y - down - 1 && p.y <= site.y + up + 1);
}

class ForestBuildEpisode extends dc.DiverseEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 10;
  static WORKS_IN_NON_FLAT_WORLD = true;
  // hint for the scheduler: never pick this type in flat worlds
  static REQUIRES_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 2;

  preSetup(bot, args) {
    if (this.isFlatWorld(args)) {
      throw new Error("[ForestBuildEpisode] needs a normal (non-flat) world with trees; skipping in a flat world");
    }
  }

  chooseSite(bot, args, mid) {
    const groves = findGroves(bot, mid, 96);
    const baseOf = (g) => g.trees[0].base;
    for (const anchorGrove of groves) {
      const anchor = baseOf(anchorGrove);
      const cluster = groves
        .filter((g) => baseOf(g).distanceTo(anchor) <= 24)
        .sort((a, b) => baseOf(a).distanceTo(anchor) - baseOf(b).distanceTo(anchor) || baseOf(a).x - baseOf(b).x || baseOf(a).z - baseOf(b).z);
      let x = 0;
      let z = 0;
      let n = 0;
      for (const g of cluster) for (const t of g.trees) {
        x += t.base.x;
        z += t.base.z;
        n++;
      }
      x = Math.floor(x / n);
      z = Math.floor(z / n);
      const gt = dc.groundTop(bot, x, z, anchor.y + 16, anchor.y - 16);
      const gy = gt && !gt.liquid ? gt.y : anchor.y - 1;
      const site = new Vec3(x, gy, z);
      // The plugin's periodic box diff (block_state records, source
      // "box_diff") now records random-tick changes that fire no Bukkit
      // event, so these sites are only skipped on request
      // (FOREST_SKIP_RANDOM_TICK_RISK=1, the round-2 behaviour).
      if (process.env.FOREST_SKIP_RANDOM_TICK_RISK === "1" && hasRandomTickRisk(bot, site, args)) {
        console.log(`[${bot.username}] forestBuild: random-tick-risk blocks near ${site}, trying the next grove`);
        continue;
      }
      return { site, prepared: false, commands: [], groves: cluster };
    }
    return null;
  }

  /**
   * Hut footprint near the clearing: solid ground (cells one block lower are
   * allowed and get a foundation block, <= 40% of the footprint), free space
   * above (blocks of the selected trees excepted).
   * @returns {{site: Vec3, foundation: Vec3[]}|null}
   */
  findHut(bot, centers, trees, W, D, axis, side) {
    const treeBlocks = new Set();
    for (const t of trees) {
      for (const l of t.logs) treeBlocks.add(dc.key3(l));
      for (const l of t.leaves) treeBlocks.add(dc.key3(l));
    }
    const free = (p, ring, dy) => {
      const b = bot.blockAt(p);
      if (!b) return false;
      if (dc.isAirName(b.name) || treeBlocks.has(dc.key3(p))) return true;
      if (b.boundingBox === "empty" && !dc.isLiquidName(b.name) && !b.name.includes("water")) return true;
      return ring && dy >= 2; // the ring only needs walking room
    };
    const ok = (s) => {
      const frame = sc.makeFrame(s, axis, side);
      const u0 = -Math.floor((D - 1) / 2);
      const p0 = -Math.floor((W - 1) / 2);
      const foundation = [];
      for (let du = u0 - 1; du <= u0 + D; du++) {
        for (let dp = p0 - 1; dp <= p0 + W; dp++) {
          const ring = du < u0 || du >= u0 + D || dp < p0 || dp >= p0 + W;
          if (!ring) {
            const g = bot.blockAt(frame.at(du, dp, -1));
            if (!dc.isSolid(g) || dc.isTreeBlock(g)) {
              const g2 = bot.blockAt(frame.at(du, dp, -2));
              if (!g || !free(frame.at(du, dp, -1), false, -1) || !dc.isSolid(g2) || dc.isTreeBlock(g2)) return null;
              foundation.push(frame.at(du, dp, -1));
            }
          }
          for (let dy = 0; dy <= 4; dy++) {
            if (!free(frame.at(du, dp, dy), ring, dy)) return null;
          }
        }
      }
      if (foundation.length > 0.4 * W * D) return null;
      return { site: s, foundation };
    };
    for (const center of centers) {
      for (let r = 0; r <= 9; r++) {
        for (let dx = -r; dx <= r; dx++) {
          for (let dz = -r; dz <= r; dz++) {
            if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
            const gt = dc.groundTop(bot, center.x + dx, center.z + dz, center.y + 8, center.y - 8);
            if (!gt || gt.liquid) continue;
            const res = ok(new Vec3(center.x + dx, gt.y, center.z + dz));
            if (res) return res;
          }
        }
      }
    }
    return null;
  }

  planEpisode(bot, rng, args, frame, isActor, chosen) {
    const want = randInt(rng, 3, 6);
    const palette = pickPalette(rng);
    const size = Math.min(2, Math.floor(rng() * 3)); // 0: 4x4, 1: 4x5, 2: 5x5
    const hutAxis = rng() < 0.5 ? "x" : "z";
    const hutSide = rng() < 0.5 ? -1 : 1;
    const doorSide = ["u+", "u-", "p+", "p-"][Math.min(3, Math.floor(rng() * 4))];
    const pillars = rng() < 0.6;
    const windowCount = randInt(rng, 1, 2);
    const windowSeed = rng();
    const leafBreaks = randInt(rng, 2, 5);

    // whole groves only (felling part of a shared canopy would leave leaves
    // with unlogged distance changes)
    const groves = [];
    let nTrees = 0;
    for (const g of chosen.groves) {
      if (groves.length === 0 || nTrees + g.trees.length <= 6) {
        groves.push(g);
        nTrees += g.trees.length;
      }
      if (nTrees >= want) break;
    }
    const trees = groves.flatMap((g, gi) => g.trees.map((t) => Object.assign(t, { grove: gi })));
    const leafSet = new Map();
    for (const g of groves) for (const l of g.leaves) leafSet.set(dc.key3(l), l);
    const allLeaves = [...leafSet.values()];
    // alternate the trees between the bots, nearest-to-site first
    const site = frame.site;
    trees.sort((a, b) => a.base.distanceTo(site) - b.base.distanceTo(site) || a.base.x - b.base.x || a.base.z - b.base.z);
    const actorTrees = trees.filter((_, i) => i % 2 === 0);
    const otherTrees = trees.filter((_, i) => i % 2 === 1);

    const sizes = [[4, 4], [4, 5], [5, 5]];
    // many trees take long to fell: keep the episode within ~2 min with a 4x4 hut
    let [W, D] = trees.length >= 3 ? sizes[0] : sizes[size];
    // the clearing: around the trees' centroid, then around each felled trunk
    const centers = [new Vec3(site.x, site.y, site.z), ...trees.map((t) => t.base.offset(0, -1, 0))];
    let hut = this.findHut(bot, centers, trees, W, D, hutAxis, hutSide);
    if (!hut && (W > 4 || D > 4)) {
      [W, D] = [4, 4];
      hut = this.findHut(bot, centers, trees, W, D, hutAxis, hutSide);
    }
    const hutSite = hut ? hut.site : null;
    const H = 3;
    let house = null;
    let hutFrame = null;
    if (hutSite) {
      hutFrame = sc.makeFrame(hutSite, hutAxis, hutSide);
      house = dc.makeHouse(hutFrame, {
        width: W,
        depth: D,
        height: H,
        palette,
        floor: false,
        pillars,
        doorSide,
        windowCount,
        windowSeed,
        roofStyle: "flat",
        door: false,
      });
    }
    const foundation = hut ? hut.foundation.map((pos) => {
      // local coordinates for the half split (dp sign)
      const d = pos.minus(hutSite);
      const dp = hutFrame.p.x * d.x + hutFrame.p.z * d.z;
      return { pos, material: palette.floor, part: "foundation", local: [0, dp, -1] };
    }) : [];
    const walls = house ? [...foundation, ...house.placements.filter((p) => p.part !== "roof")] : [];
    const roof = house ? house.placements.filter((p) => p.part === "roof") : [];
    const actorWalls = walls.filter((p) => p.local[1] >= 0);
    const otherWalls = walls.filter((p) => p.local[1] < 0);
    const toItems = (list) => Object.entries(countMaterials(list)).map(([m, c]) => [m, c + 3]);
    const tickSpeed = Number(args.state_random_tick_speed) || 100;

    const standNear = (t) => {
      if (!t) return frame.at(2, 0);
      for (const n of N6) {
        if (n.y !== 0) continue;
        const s = t.base.plus(n).plus(n);
        return new Vec3(s.x, frame.level, s.z);
      }
      return frame.at(2, 0);
    };
    const all = [...walls, ...roof];
    const cb = hutSite
      ? { x1: hutSite.x - 4, z1: hutSite.z - 4, x2: hutSite.x + 4, z2: hutSite.z + 4, y1: hutSite.y + 1, y2: hutSite.y + 2 }
      : { x1: site.x - 2, z1: site.z - 2, x2: site.x + 2, z2: site.z + 2, y1: frame.level, y2: frame.level + 1 };
    return {
      actorStand: standNear(actorTrees[0]),
      observerStand: standNear(otherTrees[0] || actorTrees[1]),
      items: {
        actor: [...toItems([...actorWalls]), ["shears", 1]],
        observer: [...toItems([...otherWalls, ...roof]), ["shears", 1]],
      },
      trees,
      leaves: allLeaves,
      actorTrees,
      otherTrees,
      leafBreaks,
      house,
      hutFrame,
      actorWalls,
      otherWalls,
      roof,
      clearBox: cb,
      leaderSetupCommands: [`gamerule randomTickSpeed ${tickSpeed}`],
      metadata: {
        trees_requested: want,
        trees: trees.map((t) => ({
          base: [t.base.x, t.base.y, t.base.z],
          type: t.type,
          height: t.logs.length,
          grove: t.grove,
          feller: actorTrees.includes(t) ? (isActor ? bot.username : args.other_bot_name) : (isActor ? args.other_bot_name : bot.username),
        })),
        groves: groves.map((g) => ({ trees: g.trees.length, leaves: g.leaves.length })),
        felling: "leaves broken by hand + logs chopped bottom-up; remaining canopy decays (leaves_decay)",
        random_tick_speed: tickSpeed,
        hut: house
          ? {
              origin: [hutSite.x, hutSite.y, hutSite.z],
              axis: hutAxis,
              side: hutSide,
              size: [W, D],
              wall_height: H,
              pillars,
              door_side: doorSide,
              door_gap: [house.door.x, house.door.y, house.door.z],
              windows: house.windows.map((p) => [p.x, p.y, p.z]),
              foundation: foundation.map((f) => [f.pos.x, f.pos.y, f.pos.z]),
              blocks: dc.placementsMetadata(all),
            }
          : null,
        hut_skipped: !house,
        materials: house ? paletteMetadata(palette, ["walls", ...(pillars ? ["pillar"] : []), "window", "roof", ...(foundation.length ? ["floor"] : [])]) : {},
        material_counts: countMaterials(all),
        non_full_fraction: nonFullFraction(all),
        roles: {
          walls_plus_half: isActor ? bot.username : args.other_bot_name,
          walls_minus_half_and_roof: isActor ? args.other_bot_name : bot.username,
        },
        observer_policy: "co-workers; idle bot watches the other (T8)",
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan } = ctx;
    const h = plan.house;
    const center = () => (h ? h.center : ctx.siteCenter);
    const inHut = (x, z) => (h ? h.inFootprint(x, z) : false);
    const observe = () => ({ focus: center, avoid: inHut, minR: 4, maxR: 10, moveProb: 0.3 });
    const leafAlive = (p) => {
      const b = bot.blockAt(p);
      return !!b && b.name.endsWith("_leaves");
    };

    const fell = (mine) => async () => {
      const deadline = Date.now() + 60000;
      let logs = 0;
      let leaves = 0;
      for (const t of mine) {
        if (Date.now() > deadline || bot._episodeStopping) break;
        const trunk = (x, z) => x === t.base.x && z === t.base.z;
        const spot = dc.findStandSpot(bot, t.base, { maxH: 2.3, minH: 0.9, forbid: trunk, otherName: ctx.otherName, yUp: 1, yDown: 2, reach: dc.REACH_HIGH });
        if (spot) await dc.moveTo(bot, spot, { timeoutMs: 8000 });
        await dc.lookAtPoint(bot, new Vec3(t.base.x + 0.5, t.base.y + 2.5, t.base.z + 0.5), 120);
        await sleep(dc.rand(300, 600));
        // a few leaves by hand / shears (nearest first)
        const near = t.leaves
          .filter(leafAlive)
          .sort((a, b) => dc.distEyeTo(bot, a) - dc.distEyeTo(bot, b))
          .filter((p) => dc.distEyeTo(bot, p) <= dc.REACH)
          .slice(0, plan.leafBreaks);
        for (const p of near) {
          if (await dc.digBlockAt(bot, p, { only: () => false })) leaves++;
        }
        // logs bottom-up
        for (const l of t.logs) {
          if (Date.now() > deadline) break;
          if (await dc.digBlockAt(bot, l, { reach: dc.REACH_HIGH, maxH: 2.3, forbid: (x, z, y) => trunk(x, z) && y > l.y - 2, otherName: ctx.otherName, yUp: 0, yDown: 9 })) logs++;
          await sleep(dc.rand(80, 200));
        }
        await note(rcon, args, bot, episodeNum, "tree_felled", { base: t.base, logs: t.logs.length });
        await dc.glance(ctx, 0.8, [600, 1000]);
      }
      await note(rcon, args, bot, episodeNum, "felling_done", { logs, leaves });
      return {};
    };

    // break the canopy leaves that are still inside the hut volume
    const clearHutVolume = async () => {
      if (!h) return {};
      const deadline = Date.now() + 15000;
      const t0 = Date.now();
      // give the decay a few seconds first
      while (Date.now() - t0 < 4000) {
        await dc.watchFor(bot, center, 800);
      }
      const inVol = [];
      for (const p of plan.leaves) {
        if (!leafAlive(p)) continue;
        if (h.inFootprint(p.x, p.z) && p.y >= plan.hutFrame.level && p.y <= plan.hutFrame.level + h.H + 1) inVol.push(p);
      }
      const mine = inVol.filter((_, i) => (i % 2 === 0) === ctx.isActor);
      const res = await dc.digPositions(ctx, mine, { deadline, glanceProb: 0.1, standOpts: { reach: dc.REACH_HIGH } });
      await note(rcon, args, bot, episodeNum, "hut_clearing", { leaves_broken: res.dug.length });
      return {};
    };

    const buildWalls = (list) => async () => {
      if (!h) return {};
      const order = dc.startNearest(bot, list).sort((a, b) => a.pos.y - b.pos.y);
      const res = await dc.buildPlacements(ctx, order, {
        deadline: Date.now() + 45000,
        standOpts: { forbid: (x, z) => inHut(x, z), otherName: ctx.otherName },
      });
      await note(rcon, args, bot, episodeNum, "hut_walls", { placed: res.placed.length, failed: res.failed.length });
      return {};
    };

    const buildRoof = async () => {
      if (!h) return {};
      await dc.moveTo(bot, new Vec3(h.doorInside.x, h.doorInside.y, h.doorInside.z), { timeoutMs: 8000 });
      const inside = { only: (x, z) => h.inInterior(x, z), otherName: ctx.otherName, maxH: 3.2 };
      const res = await dc.buildPlacements(ctx, plan.roof, { deadline: Date.now() + 40000, standOpts: inside, glanceProb: 0.3 });
      await note(rcon, args, bot, episodeNum, "hut_roof", { placed: res.placed.length, failed: res.failed.length });
      const out = new Vec3(h.doorOutside.x, h.doorOutside.y, h.doorOutside.z);
      const dir = out.minus(h.door);
      const away = dc.surfaceAt(bot, out.x + dir.x * 2, out.z + dir.z * 2, plan.hutFrame.level) || out;
      await dc.moveTo(bot, away, { timeoutMs: 8000 });
      return {};
    };

    // wait for the decay of every selected canopy, break what is left in reach
    const canopyCheck = async () => {
      const all = plan.leaves;
      const t0 = Date.now();
      while (Date.now() - t0 < 12000 && all.some(leafAlive)) {
        await dc.watchFor(bot, center, 1000);
      }
      const left = all.filter(leafAlive);
      const mine = left.filter((_, i) => (i % 2 === 0) === ctx.isActor);
      const res = await dc.digPositions(ctx, mine, { deadline: Date.now() + 15000, glanceProb: 0.3, standOpts: { reach: dc.REACH_HIGH } });
      await note(rcon, args, bot, episodeNum, "canopy_check", { left: left.length, broken: res.dug.length, still: all.filter(leafAlive).length });
      return {};
    };

    return dc.bindSteps(ctx, [
      dc.greetStep(),
      dc.dualStep("fell", fell(plan.actorTrees), fell(plan.otherTrees), { maxMs: 70000, observe }),
      dc.dualStep("clearing", clearHutVolume, clearHutVolume, { maxMs: 25000, observe }),
      dc.dualStep("hut_walls", buildWalls(plan.actorWalls), buildWalls(plan.otherWalls), { maxMs: 50000, observe }),
      dc.dualStep("hut_roof", null, buildRoof, { maxMs: 50000, observe }),
      dc.dualStep("canopy", canopyCheck, canopyCheck, { maxMs: 30000, observe }),
      dc.lookStep("admire", [2000, 3000], center),
      dc.outroStep(),
    ]);
  }

  async tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args) {
    await super.tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args);
    if (isWorldStateLeader(bot, args)) {
      try {
        const res = await rcon.send("gamerule randomTickSpeed 3");
        console.log(`[${bot.username}] reset randomTickSpeed: ${res}`);
      } catch (err) {
        console.log(`[${bot.username}] reset randomTickSpeed failed: ${err.message}`);
      }
    }
  }
}

module.exports = { ForestBuildEpisode, findGroves, hasRandomTickRisk };
