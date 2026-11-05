/**
 * rideMount - riding animals and vehicles.
 *
 * One variant per episode (biome weighted):
 *   horse       saddled, tamed horse / donkey / mule / skeleton horse / zombie
 *               horse (coat variants); the actor rides a loop around the arena,
 *               the observer rides its own mount the other way round (65%,
 *               they meet head-on twice per lap) or watches from the middle;
 *               riders look at each other most of the time
 *   camel       one saddled camel: the actor rides a loop (observer watches
 *               from the middle, 4-6 blocks from the path), stops next to the
 *               observer, which climbs on as second rider (60%) for a short
 *               double ride (3-5 s: the riders sit < 1 block apart)
 *   pig         saddled pig steered with a carrot on a stick (boost = use);
 *               the observer rides a second pig (50%) or watches
 *   boat        an 11x7, 2-deep water pool is made with /fill before the
 *               snapshot; one boat per bot (random wood, sometimes a chest
 *               boat) at opposite ends of the pool; both go round the pool
 *               the same way at the same speed, half a lap apart (boats
 *               collide; the old side-by-side start kept them < 1.5 blocks
 *               apart)
 *   minecart    a straight 15-block track: [block][powered rail + floor
 *               lever]...[rails]...[powered rail on a redstone block][block].
 *               The rider sits in the cart, flips the lever (launch away from
 *               the end block), flips it back, rides to the far end and back
 *               (the always-powered end rail relaunches the cart; the
 *               unpowered start rail brakes it), dismounts; then the other
 *               bot takes a turn
 *   tame_horse  untamed horse without saddle: the actor mounts repeatedly and
 *               gets bucked off until it is tamed
 *
 * Mount / dismount are real player actions (use_entity / unmount input), so
 * the per-frame `action.mount` / `action.dismount` flags fire (patched
 * Mineflayer emits action_mount / action_dismount in bot.mount / bot.dismount).
 * Client-controlled vehicles (saddled horses, pigs, camels, boats) are moved
 * with vehicle_move packets like a vanilla client; the rider's head rotation
 * is sent separately (Mineflayer stops sending it while mounted), and
 * bot.entity.position is kept at the seat so the per-frame position is right.
 *
 * Not a damage episode. All blocks (pool, rails, lever) are placed in setup;
 * during recording only the lever (player_interact + powered rail readback)
 * and natural mechanics change blocks.
 */

const { Vec3 } = require("vec3");

const { animalSummonCommand, animalVariant, biomeGroup, isColdGroup, itemNbt, summonCommand, tagsNbt } = require("../../utils/creatures");
const {
  CreatureEpisodeBase,
  dismountVehicle,
  driveVehicle,
  entitiesOfType,
  equipItem,
  glanceAtOther,
  localJitterMs,
  loopWaypoints,
  mountEntity,
  note,
  otherHead: otherHeadPos,
  outroStep,
  playerEntity,
  readyStep,
  rideAlong,
  sleep,
  startWatching,
  syncRiderPosition,
  turnTo,
  useItem,
  walkNear,
  withTimeout,
} = require("./creature-common");
const { groundAt } = require("../state/state-common");

const VARIANTS = [
  { v: "horse", w: 4, mult: { plains: 2, savanna: 2, mountain: 1.5 } },
  { v: "camel", w: 1, mult: { desert: 8, badlands: 3, savanna: 2 } },
  { v: "pig", w: 2, mult: { forest: 1.5, plains: 1.3 } },
  { v: "boat", w: 2.5, mult: { ocean: 3, river: 3, beach: 3, swamp: 2, snowy: 0 } },
  { v: "minecart", w: 2.5, mult: { mountain: 1.5, badlands: 1.5 } },
  { v: "tame_horse", w: 1.5, mult: { plains: 2, savanna: 2 } },
];
const HORSE_TYPES = ["horse", "horse", "horse", "donkey", "mule", "skeleton_horse", "zombie_horse"];
const BOAT_WOODS = ["oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "bamboo"];
const TRACK_BLOCKS = ["polished_andesite", "stone_bricks", "cobblestone", "oak_planks", "smooth_stone", "bricks"];
const SPEED = { horse: 0.3, donkey: 0.22, mule: 0.24, skeleton_horse: 0.3, zombie_horse: 0.25, camel: 0.13, pig: 0.11, boat: 0.2 };

class RideMountEpisode extends CreatureEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 8;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;
  static LEVEL_HALF_SIZE = 10;

  planEpisode(bot, rng, args, frame, isActor, info) {
    const group = biomeGroup(info.biome);
    const uVar = rng();
    const uMountA = rng();
    const uMountB = rng();
    const uBoth = rng();
    const uDur = rng();
    const uWood = rng();
    const uWood2 = rng();
    const uBlock = rng();
    const uDir = rng();
    const varA = [rng(), rng()];
    const varB = [rng(), rng()];
    const total = VARIANTS.reduce((a, x) => a + x.w * (x.mult[group] !== undefined ? x.mult[group] : 1), 0);
    let acc = 0;
    let variant = "horse";
    for (const x of VARIANTS) {
      acc += x.w * (x.mult[group] !== undefined ? x.mult[group] : 1);
      if (uVar * total < acc) {
        variant = x.v;
        break;
      }
    }
    if (variant === "boat" && isColdGroup(group)) variant = "horse"; // water would freeze
    // Test hook: RIDE_MOUNT_VARIANT=<variant> forces the variant (both bots
    // read the same env; the RNG draws above are unchanged).
    // A comma list cycles by episode number.
    const forcedList = String(process.env.RIDE_MOUNT_VARIANT || "").split(",").map((x) => x.trim()).filter((x) => VARIANTS.some((y) => y.v === x));
    if (forcedList.length) variant = forcedList[(Number(info.episodeNum) || 0) % forcedList.length];
    const L = frame.level;
    const pt = (du, dp, dy = 0) => {
      const c = frame.at(du, dp, dy);
      return new Vec3(c.x + 0.5, c.y, c.z + 0.5);
    };
    const yawFace = frame.u.x !== 0 ? (frame.u.x > 0 ? 270 : 90) : frame.u.z > 0 ? 0 : 180;
    const plan = {
      variant,
      levelHalf: 10,
      items: { actor: [], observer: [] },
      terrainCommands: [],
      summons: [],
      metadata: { variant },
    };
    const rideMs = 15000 + Math.floor(uDur * 12000);
    plan.rideMs = rideMs;
    plan.metadata.ride_ms = rideMs;
    plan.lookAtOtherShare = 0.85;
    const dirSign = uDir < 0.5 ? 1 : -1;
    plan.dirSign = dirSign;

    if (variant === "horse") {
      const tA = HORSE_TYPES[Math.floor(uMountA * HORSE_TYPES.length) % HORSE_TYPES.length];
      const tB = HORSE_TYPES[Math.floor(uMountB * HORSE_TYPES.length) % HORSE_TYPES.length];
      const both = uBoth < 0.65;
      let k = 0;
      const vA = tA === "horse" ? animalVariant(() => varA[k++ % 2], "horse", info.biome) : { nbt: [], variant: null };
      k = 0;
      const vB = tB === "horse" ? animalVariant(() => varB[k++ % 2], "horse", info.biome) : { nbt: [], variant: null };
      plan.mountA = { type: tA, spot: pt(-2, -2) };
      plan.summons.push(animalSummonCommand(tA, plan.mountA.spot.offset(0, L - plan.mountA.spot.y, 0), { variantNbt: vA.nbt, tame: true, saddle: true, yawDeg: yawFace }));
      if (both) {
        plan.mountB = { type: tB, spot: pt(-2, 2) };
        plan.summons.push(animalSummonCommand(tB, plan.mountB.spot.offset(0, L - plan.mountB.spot.y, 0), { variantNbt: vB.nbt, tame: true, saddle: true, yawDeg: yawFace }));
      }
      plan.loop = { halfU: 6, halfP: 4 };
      plan.metadata.mounts = { actor: tA, observer: both ? tB : null };
      plan.metadata.coats = { actor: vA.variant, observer: both ? vB.variant : null };
    } else if (variant === "camel") {
      plan.mountA = { type: "camel", spot: pt(-2, 0) };
      plan.summons.push(animalSummonCommand("camel", plan.mountA.spot, { saddle: true, yawDeg: yawFace }));
      plan.loop = { halfU: 5, halfP: 4 };
      plan.sharedCamel = uBoth < 0.6;
      plan.metadata.mounts = { actor: "camel", observer: plan.sharedCamel ? "camel_passenger" : null };
      plan.passengerMs = 3000 + Math.floor(uWood * 2000);
      plan.metadata.passenger_ms = plan.sharedCamel ? plan.passengerMs : 0;
    } else if (variant === "pig") {
      const both = uBoth < 0.5;
      plan.mountA = { type: "pig", spot: pt(-2, -2) };
      plan.summons.push(animalSummonCommand("pig", plan.mountA.spot, { saddle: true, yawDeg: yawFace }));
      plan.items.actor.push(["carrot_on_a_stick", 1]);
      if (both) {
        plan.mountB = { type: "pig", spot: pt(-2, 2) };
        plan.summons.push(animalSummonCommand("pig", plan.mountB.spot, { saddle: true, yawDeg: yawFace }));
        plan.items.observer.push(["carrot_on_a_stick", 1]);
      }
      plan.loop = { halfU: 4, halfP: 3 };
      plan.metadata.mounts = { actor: "pig", observer: both ? "pig" : null };
    } else if (variant === "boat") {
      const hu = 5;
      const hp = 3;
      const site = frame.site;
      const c1 = frame.at(-hu, -hp);
      const c2 = frame.at(hu, hp);
      const x1 = Math.min(c1.x, c2.x);
      const x2 = Math.max(c1.x, c2.x);
      const z1 = Math.min(c1.z, c2.z);
      const z2 = Math.max(c1.z, c2.z);
      // pool: 2 deep, surface at the ground level (site.y), rim stays solid
      plan.terrainCommands.push(`fill ${x1} ${site.y - 1} ${z1} ${x2} ${site.y} ${z2} minecraft:water`);
      plan.pool = { min: [x1, site.y - 1, z1], max: [x2, site.y, z2] };
      const wood = BOAT_WOODS[Math.floor(uWood * BOAT_WOODS.length) % BOAT_WOODS.length];
      const wood2 = BOAT_WOODS[Math.floor(uWood2 * BOAT_WOODS.length) % BOAT_WOODS.length];
      // one boat per bot (a shared boat keeps the two cameras 1 block apart,
      // back to back - bad for mutual visibility)
      const boatNbt = (w) => ["PersistenceRequired:1b", tagsNbt(["solaris_r2_vehicle"]), `Type:"${w}"`, `Rotation:[${yawFace}.0f,0.0f]`];
      // the boats start at opposite ends of the pool (half a lap apart)
      const bA = pt(hu - 2, -1);
      plan.mountA = { type: "boat", spot: bA };
      plan.summons.push(summonCommand("boat", new Vec3(bA.x, site.y + 0.6, bA.z), boatNbt(wood)));
      {
        const bB = pt(-(hu - 2), 1);
        plan.mountB = { type: "boat", spot: bB };
        plan.summons.push(summonCommand(uWood2 < 0.25 ? "chest_boat" : "boat", new Vec3(bB.x, site.y + 0.6, bB.z), boatNbt(wood2)));
      }
      plan.loop = { halfU: hu - 1.6, halfP: hp - 1.3 };
      plan.actorStand = frame.at(hu + 1, -1);
      plan.observerStand = frame.at(-(hu + 1), 1);
      plan.metadata.boats = [wood, uWood2 < 0.25 ? `${wood2}_chest_boat` : wood2];
      plan.metadata.pool = plan.pool;
    } else if (variant === "minecart") {
      const block = TRACK_BLOCKS[Math.floor(uBlock * TRACK_BLOCKS.length) % TRACK_BLOCKS.length];
      const shape = frame.axis === "x" ? "east_west" : "north_south";
      const dp = 2; // track line is 2 blocks to the side of the site centre
      const n = 7;
      const at = (du) => frame.at(du * dirSign, dp);
      const cmds = [];
      const S = at(-n);
      const P = at(-n + 1);
      const P2 = at(n - 1);
      const S2 = at(n);
      cmds.push(`setblock ${S.x} ${S.y} ${S.z} minecraft:${block}`);
      cmds.push(`setblock ${S2.x} ${S2.y} ${S2.z} minecraft:${block}`);
      cmds.push(`setblock ${P2.x} ${P2.y - 1} ${P2.z} minecraft:redstone_block`);
      for (let du = -n + 2; du <= n - 2; du++) {
        const r = at(du);
        cmds.push(`setblock ${r.x} ${r.y} ${r.z} minecraft:rail[shape=${shape}]`);
      }
      cmds.push(`setblock ${P.x} ${P.y} ${P.z} minecraft:powered_rail[shape=${shape},powered=false]`);
      cmds.push(`setblock ${P2.x} ${P2.y} ${P2.z} minecraft:powered_rail[shape=${shape},powered=true]`);
      const lever = frame.at((-n + 1) * dirSign, dp + 1);
      const leverFacing = frame.axis === "x" ? "south" : "east";
      cmds.push(`setblock ${lever.x} ${lever.y} ${lever.z} minecraft:lever[face=floor,facing=${leverFacing},powered=false]`);
      plan.terrainCommands.push(...cmds);
      plan.track = {
        start_block: [S.x, S.y, S.z],
        start_powered_rail: [P.x, P.y, P.z],
        end_powered_rail: [P2.x, P2.y, P2.z],
        end_block: [S2.x, S2.y, S2.z],
        lever: [lever.x, lever.y, lever.z],
        block,
        shape,
        length: 2 * n + 1,
      };
      plan.mountA = { type: "minecart", spot: new Vec3(P.x + 0.5, P.y, P.z + 0.5) };
      plan.summons.push(summonCommand("minecart", new Vec3(P.x + 0.5, P.y + 0.1, P.z + 0.5), ["PersistenceRequired:1b", tagsNbt(["solaris_r2_vehicle"])]));
      plan.actorStand = frame.at((-n + 2) * dirSign, dp + 2);
      plan.observerStand = frame.at(0, dp + 4);
      plan.metadata.track = plan.track;
      plan.lapsActor = 2 + (uBoth < 0.3 ? 1 : 0);
      plan.observerRides = uMountB < 0.85;
      plan.metadata.actor_launches = plan.lapsActor;
      plan.metadata.observer_rides = plan.observerRides;
    } else if (variant === "tame_horse") {
      const t = uMountA < 0.75 ? "horse" : uMountA < 0.9 ? "donkey" : "mule";
      let k = 0;
      const vA = t === "horse" ? animalVariant(() => varA[k++ % 2], "horse", info.biome) : { nbt: [], variant: null };
      plan.mountA = { type: t, spot: pt(-3, 0) };
      plan.summons.push(animalSummonCommand(t, plan.mountA.spot, { variantNbt: vA.nbt, yawDeg: yawFace }));
      plan.attempts = 3 + Math.floor(uBoth * 3);
      plan.metadata.mounts = { actor: t, observer: null };
      plan.metadata.coat = vA.variant;
      plan.metadata.max_attempts = plan.attempts;
    }
    if (!plan.actorStand) plan.actorStand = frame.at(1, -2);
    if (!plan.observerStand) plan.observerStand = frame.at(1, 2);
    plan.summonCommands = plan.summons;
    if (plan.loop) plan.metadata.loop = { half_u: plan.loop.halfU, half_p: plan.loop.halfP, direction: dirSign };
    return plan;
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor, other, frame } = ctx;
    const L = frame.level;
    // other bot's head (vehicle aware: Mineflayer does not move passengers)
    const otherHead = () => otherHeadPos(bot, other);
    let lookOther = true;
    let lookSwitch = 0;
    const lookTarget = () => {
      const now = Date.now();
      if (now >= lookSwitch) {
        lookOther = Math.random() < plan.lookAtOtherShare;
        lookSwitch = now + 1500 + Math.random() * 2500;
      }
      return lookOther ? otherHead() : null;
    };
    const myMount = () => {
      const m = isActor ? plan.mountA : plan.mountB;
      if (!m) return null;
      const type = m.type === "boat" ? null : m.type;
      const list = Object.values(bot.entities).filter(
        (e) => e && e.position && (type ? e.name === type : e.name === "boat" || e.name === "chest_boat") && e.position.distanceTo(m.spot) < 14,
      );
      list.sort((a, b) => a.position.distanceTo(m.spot) - b.position.distanceTo(m.spot));
      return list[0] || null;
    };
    const groundY = (x, z) => {
      const g = groundAt(bot, Math.floor(x), Math.floor(z));
      return g ? g.y + 1 : null;
    };
    const walkTo = async (e, range = 1.8, ms = 5000) => {
      if (!e) return false;
      if (bot.heldItem) {
        try {
          await bot.unequip("hand");
        } catch (_) {}
      }
      if (e.position.distanceTo(bot.entity.position) > range + 0.5) await walkNear(bot, e.position, range, ms);
      return e.position.distanceTo(bot.entity.position) < 4;
    };
    const tryMount = async (e) => {
      for (let i = 0; i < 3; i++) {
        if (!e) return false;
        await walkTo(e);
        const fresh = bot.entities[e.id] || e;
        if (await mountEntity(bot, fresh, 2500)) return true;
        await sleep(400);
      }
      return false;
    };
    const watch = async (ms, target = otherHead) => {
      const stop = startWatching(bot, target);
      await sleep(ms);
      await stop();
    };

    const steps = [readyStep("ready", true)];

    if (plan.variant === "horse" || plan.variant === "pig" || plan.variant === "boat") {
      steps.push({
        name: "mount",
        run: async function () {
          const m = myMount();
          if (!m) {
            // no own mount: watch the other one mount
            await watch(localJitterMs(2500, 3500));
            return { mounted: false };
          }
          const ok = await tryMount(m);
          await note(rcon, args, bot, episodeNum, "mount", { type: m.name, ok });
          return { mounted: ok };
        },
      });
      steps.push({
        name: "ride",
        run: async function () {
          if (!bot.vehicle) {
            // spectator: stand in the middle of the loop and follow the rider
            const mid = new Vec3(frame.site.x + 0.5, L, frame.site.z + 0.5);
            if (plan.variant !== "boat") await walkNear(bot, mid, 1.5, 3000);
            await watch(plan.rideMs);
            return {};
          }
          if (plan.variant === "pig") await equipItem(bot, "carrot_on_a_stick");
          const type = bot.vehicle.name;
          const baseSpeed = SPEED[type] || 0.2;
          let speed = baseSpeed;
          const y = plan.variant === "boat" ? bot.vehicle.position.y : L;
          const wps = loopWaypoints(frame, plan.loop.halfU, plan.loop.halfP, y);
          // Horses / pigs: the observer rides the loop the other way round, the
          // riders meet head-on twice per lap. Boats collide with each other,
          // so both boats go the same way at the same speed, half a lap apart
          // (they start at opposite ends of the pool).
          const forward = plan.variant === "boat" ? plan.dirSign > 0 : (plan.dirSign > 0) === isActor;
          const ordered = forward ? wps : wps.slice().reverse();
          // the two riders start at opposite sides of the loop
          const res = await driveVehicle(bot, ordered, {
            speed,
            getSpeed: () => speed,
            kind: plan.variant === "boat" ? "boat" : "land",
            durationMs: plan.rideMs,
            groundY: plan.variant === "boat" ? null : groundY,
            getLookTarget: lookTarget,
            partnerHead: otherHead,
            onBoost:
              plan.variant === "pig"
                ? () => {
                    useItem(bot, false); // carrot on a stick boost
                    speed = 0.2;
                    setTimeout(() => {
                      speed = baseSpeed;
                    }, 2000);
                  }
                : null,
          });
          await note(rcon, args, bot, episodeNum, "ride_end", { type, ...res });
          return {};
        },
      });
      steps.push({
        name: "dismount",
        run: async function () {
          if (bot.vehicle) {
            const ok = await dismountVehicle(bot);
            await note(rcon, args, bot, episodeNum, "dismount", { ok });
          }
          await sleep(500);
          await glanceAtOther(bot, other, localJitterMs(1000, 1500));
          return {};
        },
      });
    } else if (plan.variant === "camel") {
      const camel = () => {
        const list = entitiesOfType(bot, "camel", plan.mountA.spot, 24);
        return list[0] || null;
      };
      const loopW = () => loopWaypoints(frame, plan.loop.halfU, plan.loop.halfP, L);
      steps.push({
        name: "driver_mounts",
        run: async function () {
          if (isActor) {
            const ok = await tryMount(camel());
            await note(rcon, args, bot, episodeNum, "mount", { type: "camel", role: "driver", ok });
          } else {
            const mid = new Vec3(frame.site.x + 0.5, L, frame.site.z + 0.5);
            await walkNear(bot, mid, 1.2, 3000);
            await watch(localJitterMs(1200, 2000));
          }
          return {};
        },
      });
      steps.push({
        name: "solo_ride",
        run: async function () {
          if (!isActor || !bot.vehicle) {
            await watch(plan.rideMs);
            return {};
          }
          const wps = loopW();
          const res = await driveVehicle(bot, plan.dirSign > 0 ? wps : wps.slice().reverse(), {
            speed: SPEED.camel,
            kind: "land",
            durationMs: plan.rideMs,
            groundY,
            getLookTarget: lookTarget,
            partnerHead: otherHead,
          });
          // stop next to the observer so it can climb on
          const o = playerEntity(bot, other);
          if (o && plan.sharedCamel && bot.vehicle) {
            const p = bot.vehicle.position;
            const d = o.position.minus(p);
            const len = Math.hypot(d.x, d.z) || 1;
            const stopAt = new Vec3(o.position.x - (d.x / len) * 2.5, L, o.position.z - (d.z / len) * 2.5);
            await driveVehicle(bot, [stopAt], { speed: SPEED.camel, kind: "land", durationMs: 6000, groundY, getLookTarget: otherHead, loop: false });
          }
          await note(rcon, args, bot, episodeNum, "ride_end", { type: "camel", ...res });
          return {};
        },
      });
      steps.push({
        name: "passenger_mounts",
        run: async function () {
          if (!plan.sharedCamel) {
            await glanceAtOther(bot, other, localJitterMs(800, 1200));
            return {};
          }
          if (!isActor) {
            const ok = await tryMount(camel());
            await note(rcon, args, bot, episodeNum, "mount", { type: "camel", role: "passenger", ok });
          } else if (bot.vehicle) {
            await rideAlong(bot, localJitterMs(2500, 3500), otherHead);
          } else {
            await glanceAtOther(bot, other, localJitterMs(1500, 2500));
          }
          return {};
        },
      });
      steps.push({
        name: "double_ride",
        run: async function () {
          if (!plan.sharedCamel) {
            await glanceAtOther(bot, other, localJitterMs(800, 1200));
            return {};
          }
          if (isActor && bot.vehicle) {
            const wps = loopW();
            await driveVehicle(bot, plan.dirSign > 0 ? wps : wps.slice().reverse(), {
              speed: SPEED.camel,
              kind: "land",
              durationMs: plan.passengerMs,
              groundY,
              getLookTarget: () => null,
            });
          } else if (bot.vehicle) {
            let target = null;
            let next = 0;
            await rideAlong(bot, plan.passengerMs, () => {
              if (Date.now() >= next) {
                next = Date.now() + 1500 + Math.random() * 2000;
                const yaw = bot.entity.yaw + (Math.random() - 0.5) * 2.4;
                target = Math.random() < 0.7 ? otherHead() : bot.entity.position.offset(-Math.sin(yaw) * 10, 1.2, -Math.cos(yaw) * 10);
              }
              return target;
            });
          } else {
            await watch(plan.passengerMs);
          }
          return {};
        },
      });
      steps.push({
        name: "dismount",
        run: async function () {
          // passenger first, then the driver
          if (!isActor && bot.vehicle) await dismountVehicle(bot);
          else await sleep(700);
          if (isActor && bot.vehicle) await dismountVehicle(bot);
          await note(rcon, args, bot, episodeNum, "dismount", { ok: !bot.vehicle });
          await sleep(500);
          // step away from each other a little before the outro
          await glanceAtOther(bot, other, localJitterMs(800, 1200));
          return {};
        },
      });
    } else if (plan.variant === "minecart") {
      const tr = plan.track;
      const leverPos = new Vec3(tr.lever[0], tr.lever[1], tr.lever[2]);
      const startRail = new Vec3(tr.start_powered_rail[0] + 0.5, tr.start_powered_rail[1], tr.start_powered_rail[2] + 0.5);
      const cart = () => {
        const list = Object.values(bot.entities).filter((e) => e && e.position && e.name === "minecart" && e.position.distanceTo(startRail) < 20);
        list.sort((a, b) => a.position.distanceTo(startRail) - b.position.distanceTo(startRail));
        return list[0] || null;
      };
      // Click the lever on the face that points at the start powered rail:
      // the world log re-reads the clicked block and its face neighbour, so
      // the rail's powered=true/false change is logged too.
      const railPos = new Vec3(tr.start_powered_rail[0], tr.start_powered_rail[1], tr.start_powered_rail[2]);
      const leverFace = railPos.minus(leverPos);
      const flipLever = async () => {
        const b = bot.blockAt(leverPos);
        if (!b || b.name !== "lever") return false;
        await turnTo(bot, leverPos.offset(0.5, 0.2, 0.5), 400, 18);
        bot.emit("action_use");
        try {
          await withTimeout(bot.activateBlock(b, leverFace), 1500, null);
          bot.swingArm("right");
          return true;
        } catch (err) {
          console.log(`[${bot.username}] lever failed: ${err.message}`);
          return false;
        }
      };
      const rideTurn = async (launches) => {
        const c = cart();
        if (!c) return { ok: false, reason: "no_cart" };
        // wait until the cart is (nearly) stopped at the start rail
        const until = Date.now() + 9000;
        while (Date.now() < until && c.position.distanceTo(startRail) > 1.6) await watch(300, () => c.position.offset(0, 0.5, 0));
        if (!(await tryMount(c))) return { ok: false, reason: "mount_failed" };
        const log = [];
        for (let i = 0; i < launches; i++) {
          await rideAlong(bot, localJitterMs(500, 900), () => leverPos.offset(0.5, 0.2, 0.5));
          const on = await flipLever();
          await rideAlong(bot, localJitterMs(350, 550), () => leverPos.offset(0.5, 0.2, 0.5));
          const off = await flipLever();
          log.push({ on, off });
          // ride to the far end and back until the cart stops at the start
          let movedAway = false;
          const tEnd = Date.now() + 14000;
          await rideAlong(bot, 14000, () => (Math.random() < 0.02 ? null : otherHead()), () => {
            const v = bot.vehicle;
            if (!v) return true;
            const d = v.position.distanceTo(startRail);
            if (d > 4) movedAway = true;
            return (movedAway && d < 1.2) || Date.now() > tEnd;
          });
          await rideAlong(bot, 800, otherHead);
        }
        const ok = await dismountVehicle(bot);
        syncRiderPosition(bot);
        await sleep(400);
        // step off the track
        const off = frame.at(0, 6);
        await walkNear(bot, new Vec3(off.x + 0.5, L, off.z + 0.5), 2, 2500);
        return { ok: true, launches: log, dismounted: ok };
      };
      steps.push({
        name: "actor_rides",
        run: async function () {
          if (isActor) {
            const r = await rideTurn(plan.lapsActor);
            await note(rcon, args, bot, episodeNum, "minecart_ride", r);
          } else {
            await watch(localJitterMs(1000, 1500));
            const stop = startWatching(bot, () => {
              const o = playerEntity(bot, other);
              const c = cart();
              if (o && Math.random() < 0.6) return o.position.offset(0, 1.4, 0);
              return c ? c.position.offset(0, 0.6, 0) : otherHead();
            });
            const until = Date.now() + 9000 + plan.lapsActor * 12000;
            // wait until the actor got off (it walks off the track) or timeout
            let mounted = false;
            while (Date.now() < until && !bot._episodeStopping) {
              const c = cart();
              const o = playerEntity(bot, other);
              const riding = Boolean(c && o && o.vehicle && o.vehicle.id === c.id);
              if (riding) mounted = true;
              if (mounted && !riding && o && c && o.position.distanceTo(c.position) > 3) break;
              await sleep(200);
            }
            await stop();
          }
          return {};
        },
      });
      steps.push({
        name: "observer_rides",
        run: async function () {
          if (!plan.observerRides) {
            await glanceAtOther(bot, other, localJitterMs(1500, 2200));
            return {};
          }
          if (!isActor) {
            const r = await rideTurn(1);
            await note(rcon, args, bot, episodeNum, "minecart_ride", r);
          } else {
            const stop = startWatching(bot, () => {
              const o = playerEntity(bot, other);
              return o ? o.position.offset(0, 1.4, 0) : null;
            });
            const until = Date.now() + 22000;
            let mounted = false;
            while (Date.now() < until && !bot._episodeStopping) {
              const c = cart();
              const o = playerEntity(bot, other);
              const riding = Boolean(c && o && o.vehicle && o.vehicle.id === c.id);
              if (riding) mounted = true;
              if (mounted && !riding && o && c && o.position.distanceTo(c.position) > 3) break;
              await sleep(200);
            }
            await stop();
          }
          return {};
        },
      });
    } else if (plan.variant === "tame_horse") {
      steps.push({
        name: "taming",
        run: async function () {
          if (!isActor) {
            const stop = startWatching(bot, () => {
              const o = playerEntity(bot, other);
              return o ? o.position.offset(0, 1.6, 0) : null;
            });
            await sleep(6000 + plan.attempts * 6500);
            await stop();
            return {};
          }
          const horse = () => {
            const list = entitiesOfType(bot, plan.mountA.type, plan.mountA.spot, 24);
            return list[0] || null;
          };
          const attempts = [];
          const until = Date.now() + 6000 + plan.attempts * 6500;
          for (let i = 0; i < plan.attempts && Date.now() < until; i++) {
            const h = horse();
            if (!h) break;
            const ok = await tryMount(h);
            if (!ok) {
              attempts.push({ mounted: false });
              continue;
            }
            const t0 = Date.now();
            const bucked = await rideAlong(bot, 5500, otherHead);
            attempts.push({ mounted: true, bucked, ms: Date.now() - t0 });
            if (!bucked) {
              // stayed on: tamed (or calm); get off voluntarily
              await dismountVehicle(bot);
              attempts[attempts.length - 1].tamed_guess = true;
              break;
            }
            await sleep(localJitterMs(600, 1000));
            await glanceAtOther(bot, other, localJitterMs(500, 800));
          }
          await note(rcon, args, bot, episodeNum, "taming", { attempts });
          return {};
        },
      });
    }
    steps.push(outroStep());
    return steps;
  }
}

module.exports = { RideMountEpisode };
