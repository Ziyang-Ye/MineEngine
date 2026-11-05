/**
 * pveMobs - both bots fight a summoned group of hostile / neutral mobs together.
 *
 * Setup (before the start snapshot):
 *  - flat / levelled arena; biome = the leader's server-resolved biome at the
 *    site (creature-common handshake; the client biome is `biome_client`);
 *  - mob group from controller/utils/creatures.js chooseHostileGroup: 1-3 types,
 *    3-7 mobs, biome weighted (husks in deserts, strays in snowy biomes,
 *    bogged / slimes / witches in swamps, drowned only near water, ...);
 *    daylight-burning mobs get an unbreakable helmet (or a carved pumpkin)
 *    by day, archers get bows, pillagers crossbows, vindicators axes, piglins
 *    are immune to zombification; neutral mobs (enderman, zombified piglin)
 *    are provoked by the bots' first hit;
 *  - optionally an allied iron golem / snow golem;
 *  - the bots stand side by side (4 blocks apart) facing the mob area; each
 *    gets a sword / axe, armour, sometimes a shield; one bot may be an archer
 *    (bow + arrows, opens at range). Spawn points are set to the arena.
 *  - the leader summons the mobs 7-12 blocks in front of the bots as the LAST
 *    setup action (env-setup.js already killed leftover hostiles in the
 *    teleport phase; natural spawning is off; mobGriefing stays on, so
 *    creeper explosions leave craters and endermen move blocks).
 *
 * Steps: ready (look at the mob area, glance at the partner) -> fight (until
 * no hostile is left within 40 blocks for 3 s or 30-55 s passed: nearest-mob
 * targeting, hit-and-run against creepers, archer kiting, periodic glances at
 * the partner, regrouping) -> loot (walk over drops) -> outro.
 *
 * Damage episode (DAMAGE_ENABLED). Logged: entity_spawn (summoned mobs are in
 * the start snapshot; split slimes, arrows, potions, vexes, drops, xp orbs),
 * entity_damage, entity_knockback, entity_death, entity_explode + block
 * changes (creepers), entity_change_block (endermen), bow_shoot.
 */

const { Vec3 } = require("vec3");

const {
  HOSTILE_ENTITY_NAMES,
  allySummonCommand,
  chooseAlly,
  chooseHostileGroup,
  hostileSummonCommand,
} = require("../../utils/creatures");
const {
  CreatureEpisodeBase,
  backOff,
  chooseLoadout,
  collectItems,
  consumeHeld,
  entitiesWhere,
  equipItem,
  glanceAtOther,
  hasItem,
  isAlive,
  keepApart,
  localJitterMs,
  meleeEngage,
  note,
  outroStep,
  playerEntity,
  rangedEngage,
  sleep,
  turnTo,
  walkNear,
} = require("./creature-common");
const { groundAt } = require("../state/state-common");

const MAX_MOB_SLOTS = 12;

class PveMobsEpisode extends CreatureEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 8;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static DAMAGE_ENABLED = true;
  static SITE_HALF_SIZE = 3;
  static LEVEL_HALF_SIZE = 12;
  static REMOVE_EQUIPMENT_AFTER = true;

  planEpisode(bot, rng, args, frame, isActor, info) {
    // Test hook: PVE_MOBS_FORCE="witch,blaze+breeze,slime" forces the mob
    // types (one comma entry per episode, cycled by episode number; "+" joins
    // up to 3 types). Both bots read the same env; RNG draws are unchanged.
    const forcedEntries = String(process.env.PVE_MOBS_FORCE || "")
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    const force = forcedEntries.length
      ? forcedEntries[(Number(info.episodeNum) || 0) % forcedEntries.length].split("+").map((x) => x.trim())
      : null;
    const group = chooseHostileGroup(rng, info.biome, {
      isDay: info.is_day,
      waterNear: info.water_near,
      maxTypes: 3,
      maxTotal: 7,
      force,
    });
    // ensure at least 4 mobs: top up with the first type (no extra draws)
    while (group.mobs.length < 4 && group.mobs.length > 0) {
      const m = { ...group.mobs[0], index: group.mobs.length };
      group.mobs.push(m);
      group.types[m.id] = (group.types[m.id] || 0) + 1;
    }
    const ally = chooseAlly(rng, info.biome, 0.25);
    const actorLoadout = chooseLoadout(rng, { axeChance: 0.3, shieldChance: 0.3, noArmorWeight: 0.5 });
    const observerLoadout = chooseLoadout(rng, { axeChance: 0.3, shieldChance: 0.3, noArmorWeight: 0.5 });
    const uArcher = rng();
    const uWhich = rng();
    const archer = uArcher < 0.35 ? (uWhich < 0.5 ? "actor" : "observer") : null;
    const fightMs = 30000 + Math.floor(rng() * 25000);
    // spawn layout (fixed number of draws)
    const spots = [];
    for (let i = 0; i < MAX_MOB_SLOTS; i++) {
      const r = 7 + rng() * 5; // 7..12 in front
      const off = (rng() - 0.5) * 12; // -6..6 sideways
      spots.push({ r, off });
    }
    const allyOff = (rng() - 0.5) * 4;
    const mobs = group.mobs.map((m, i) => ({ ...m, spot: spots[i % MAX_MOB_SLOTS] }));
    const items = (lo, isArcher) => {
      const l = [[lo.weapon, 1], ["cooked_beef", 4]];
      if (lo.golden_apple) l.push(["golden_apple", 1]);
      if (isArcher) {
        l.push(["bow", 1]);
        l.push(["arrow", 32]);
      }
      return l;
    };
    const summonCommands = (b) => {
      const cmds = [];
      const face = frame.u.x !== 0 ? (frame.u.x > 0 ? 270 : 90) : frame.u.z > 0 ? 0 : 180; // facing +u (toward the bots)
      for (const m of mobs) {
        const col = frame.at(-m.spot.r, m.spot.off);
        const g = groundAt(b, Math.floor(col.x), Math.floor(col.z));
        const y = g ? g.y + 1 : frame.level;
        cmds.push(hostileSummonCommand(m, new Vec3(Math.floor(col.x) + 0.5, y, Math.floor(col.z) + 0.5), face));
      }
      if (ally) {
        const col = frame.at(1, allyOff);
        cmds.push(allySummonCommand(ally, new Vec3(Math.floor(col.x) + 0.5, frame.level, Math.floor(col.z) + 0.5), face + 180));
      }
      return cmds;
    };
    return {
      actorStand: frame.at(2, 2),
      observerStand: frame.at(2, -2),
      items: { actor: items(actorLoadout, archer === "actor"), observer: items(observerLoadout, archer === "observer") },
      equipment: { actor: actorLoadout, observer: observerLoadout },
      levelHalf: 12,
      summonCommands,
      archer,
      fightMs,
      mobs,
      ally,
      metadata: {
        variant: "pve_group",
        mobs: mobs.map((m) => ({
          type: m.id,
          hand: m.hand,
          helmet: m.helmet,
          baby: m.baby,
          size: m.size,
          neutral: m.neutral,
          villager_type: m.villagerType,
          profession: m.profession,
          offset: [Number((-m.spot.r).toFixed(2)), Number(m.spot.off.toFixed(2))],
        })),
        mob_counts: group.types,
        forced_mobs: force,
        mob_total: mobs.length,
        ally,
        archer,
        fight_ms: fightMs,
        is_day: info.is_day,
        loadouts: { actor: actorLoadout, observer: observerLoadout },
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor, other, frame } = ctx;
    const me = isActor ? plan.equipment.actor : plan.equipment.observer;
    const amArcher = plan.archer === (isActor ? "actor" : "observer");
    const site = ctx.siteCenter;
    const mobArea = frame.at(-9, 0).offset(0.5, 1, 0.5);
    const hostiles = () =>
      entitiesWhere(
        bot,
        (e) => HOSTILE_ENTITY_NAMES.has(e.name) && e.position.distanceTo(site) < 40 && e.isValid !== false,
      );
    const nearestHostile = () => {
      const list = hostiles();
      if (!list.length || !bot.entity) return null;
      list.sort((a, b) => a.position.distanceTo(bot.entity.position) - b.position.distanceTo(bot.entity.position));
      return list[0];
    };
    let kills = 0;
    const killed = (e) => {
      if (e && HOSTILE_ENTITY_NAMES.has(e.name)) kills++;
    };
    ctx.episode.listen(bot, "entityDead", killed);
    let deaths = 0;
    ctx.episode.listen(bot, "death", () => {
      deaths++;
    });

    return [
      {
        name: "ready",
        run: async function () {
          await turnTo(bot, mobArea, 700, 10);
          await equipItem(bot, amArcher ? "bow" : me.weapon);
          await sleep(localJitterMs(300, 600));
          await glanceAtOther(bot, other, localJitterMs(500, 800));
          await turnTo(bot, () => {
            const t = nearestHostile();
            return t ? t.position.offset(0, 1, 0) : mobArea;
          }, 600, 10);
          await note(rcon, args, bot, episodeNum, "pve_ready", {
            visible_hostiles: hostiles().length,
            archer: amArcher,
            weapon: me.weapon,
          });
          return {};
        },
      },
      {
        name: "fight",
        run: async function () {
          const until = Date.now() + plan.fightMs;
          let quietSince = null;
          let nextGlance = Date.now() + localJitterMs(1500, 3000);
          let rounds = 0;
          while (Date.now() < until && !bot._episodeStopping) {
            if (!isAlive(bot)) {
              await sleep(300);
              continue;
            }
            const t = nearestHostile();
            if (!t) {
              if (quietSince === null) quietSince = Date.now();
              if (Date.now() - quietSince > 3000) break;
              const partner = playerEntity(bot, other);
              if (partner && partner.position.distanceTo(bot.entity.position) > 8) {
                await walkNear(bot, partner.position, 4, 2500);
              } else if (!(await keepApart(bot, other, 2.5))) {
                await glanceAtOther(bot, other, 400);
              }
              continue;
            }
            quietSince = null;
            // partner glances (03 section 5: partner in view >= 40 %; side by
            // side the partner is outside the view while facing the mobs)
            if (Date.now() >= nextGlance) {
              await glanceAtOther(bot, other, localJitterMs(800, 1300));
              nextGlance = Date.now() + localJitterMs(1500, 3200);
            }
            rounds++;
            const dist = t.position.distanceTo(bot.entity.position);
            const left = Math.max(300, until - Date.now());
            if (amArcher && dist > 7 && hasItem(bot, "arrow")) {
              await rangedEngage(bot, nearestHostile, { durationMs: Math.min(4000, left), minDist: 6, strafe: 0.5 });
              continue;
            }
            if (!bot.heldItem || bot.heldItem.name !== me.weapon) await equipItem(bot, me.weapon);
            const lockId = t.id;
            const locked = () => {
              const e = bot.entities[lockId];
              return e && e.isValid !== false ? e : null;
            };
            if (t.name === "creeper") {
              // hit and run
              await meleeEngage(bot, locked, { durationMs: Math.min(1400, left), critChance: 0.1, strafe: 0.2, shield: me.shield, blockChance: 0.2 });
              await backOff(bot, () => {
                const e = locked();
                return e ? e.position.offset(0, 1, 0) : null;
              }, 800, true);
            } else {
              await meleeEngage(bot, locked, {
                durationMs: Math.min(4500, left),
                critChance: 0.3,
                strafe: 0.4,
                shield: me.shield,
                blockChance: 0.4,
              });
            }
            // low health: eat
            if (isAlive(bot) && bot.health < 8 && hasItem(bot, "golden_apple") && (await equipItem(bot, "golden_apple"))) {
              await note(rcon, args, bot, episodeNum, "eat_golden_apple", { health: bot.health });
              await consumeHeld(bot, 1900);
            }
          }
          await note(rcon, args, bot, episodeNum, "fight_end", {
            remaining_hostiles: hostiles().length,
            kills_seen: kills,
            deaths,
            health: bot.health,
            rounds,
          });
          return {};
        },
      },
      {
        name: "loot",
        run: async function () {
          // actor walks over the drops, the partner watches it (and checks the
          // battlefield / crater), then both regroup
          if (isActor) {
            const n = await collectItems(bot, site, { radius: 12, max: 3, timeoutMs: 6000 });
            await note(rcon, args, bot, episodeNum, "loot", { walked_to_items: n });
          } else {
            await turnTo(bot, mobArea, 600, 10);
            await sleep(localJitterMs(600, 1200));
            await glanceAtOther(bot, other, localJitterMs(2500, 4000));
          }
          const partner = playerEntity(bot, other);
          if (partner && partner.position.distanceTo(bot.entity.position) > 6) await walkNear(bot, partner.position, 4, 3000);
          await keepApart(bot, other, 2.5);
          await glanceAtOther(bot, other, localJitterMs(800, 1300));
          return {};
        },
      },
      outroStep(),
    ];
  }
}

module.exports = { PveMobsEpisode };
