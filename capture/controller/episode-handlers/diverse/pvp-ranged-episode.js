/**
 * pvpRanged - bow / crossbow duel at 8-20 blocks, then close-in melee.
 *
 * Setup (before the start snapshot): each bot gets a bow (optionally Power /
 * Punch) or a crossbow (optionally Quick Charge), 24-48 arrows, a sword for
 * the close-in phase, a random (lighter) armour set; in some episodes one bot
 * carries a shield in the off hand. They stand 8-20 blocks apart on a flat /
 * levelled corridor. Spawn points are set to the arena (deaths respawn there).
 *
 * Steps: face_off -> ranged (draw with bot.activateItem-equivalent use_item,
 * strafe while drawing, aim with arrow drop + target lead, release; the
 * shield bot raises its shield between shots) -> close_in (switch to the
 * sword, sprint at each other, melee) -> collect_arrows (walk over arrows
 * lying near the arena, picks them up) -> outro.
 *
 * Damage episode (DAMAGE_ENABLED). Logged: bow_shoot (force), entity_spawn /
 * entity_remove of minecraft:arrow, entities[] arrow trajectories,
 * entity_damage (minecraft:arrow / player_attack, victim_blocking),
 * entity_knockback, use_item_start/stop, entity_death.
 */

const {
  CreatureEpisodeBase,
  chooseLoadout,
  collectItems,
  entitiesWhere,
  equipItem,
  glanceAtOther,
  isAlive,
  localJitterMs,
  meleeEngage,
  note,
  outroStep,
  playerEntity,
  rangedEngage,
  sleep,
  walkNear,
} = require("./creature-common");

const BOW_ENCHANTS = [null, null, "power", "punch", "power"];

class PvpRangedEpisode extends CreatureEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 10;
  static INIT_MAX_BOTS_DISTANCE = 20;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static DAMAGE_ENABLED = true;
  static SITE_HALF_SIZE = 3;
  static LEVEL_HALF_SIZE = 11;
  static REMOVE_EQUIPMENT_AFTER = true;

  planEpisode(bot, rng, args, frame, isActor, info) {
    const mk = () => {
      const lo = chooseLoadout(rng, { axeChance: 0, shieldChance: 0, noArmorWeight: 3 });
      const uRanged = rng();
      const uEnch = rng();
      const uLvl = rng();
      const uArrows = rng();
      const crossbow = uRanged < 0.3;
      const ench = crossbow ? (uEnch < 0.4 ? "quick_charge" : null) : BOW_ENCHANTS[Math.floor(uEnch * BOW_ENCHANTS.length) % BOW_ENCHANTS.length];
      const level = ench ? 1 + Math.floor(uLvl * 2) : 0;
      return {
        ...lo,
        ranged: crossbow ? "crossbow" : "bow",
        enchant: ench,
        enchant_level: level,
        arrows: 24 + Math.floor(uArrows * 25),
      };
    };
    const actor = mk();
    const observer = mk();
    const uShield = rng();
    const uWho = rng();
    // one bot (sometimes) carries a shield
    if (uShield < 0.45) {
      if (uWho < 0.5) actor.shield = true;
      else observer.shield = true;
    }
    const dist = 8 + Math.floor(rng() * 13); // 8..20
    const rangedMs = 16000 + Math.floor(rng() * 12000);
    const meleeMs = 8000 + Math.floor(rng() * 8000);
    const strafeA = 0.4 + rng() * 0.5;
    const strafeB = 0.4 + rng() * 0.5;
    const collect = rng() < 0.7;
    const half = Math.ceil(dist / 2);
    const items = (lo) => {
      const rangedItem = lo.enchant
        ? `${lo.ranged}[minecraft:enchantments={levels:{"minecraft:${lo.enchant}":${lo.enchant_level}}}]`
        : lo.ranged;
      return [
        [lo.weapon, 1],
        ["arrow", lo.arrows],
        ["cooked_beef", 3],
        { raw: rangedItem, name: lo.ranged },
      ];
    };
    return {
      actorStand: frame.at(half, 0),
      observerStand: frame.at(-(dist - half), 0),
      items: { actor: items(actor), observer: items(observer) },
      equipment: { actor, observer },
      levelHalf: Math.max(8, half + 2),
      rangedMs,
      meleeMs,
      collect,
      strafe: { actor: strafeA, observer: strafeB },
      metadata: {
        variant: "ranged_duel",
        loadouts: { actor, observer },
        start_distance: dist,
        ranged_ms: rangedMs,
        melee_ms: meleeMs,
        collect_arrows: collect,
        strafe: { actor: Number(strafeA.toFixed(2)), observer: Number(strafeB.toFixed(2)) },
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor, other } = ctx;
    const me = isActor ? plan.equipment.actor : plan.equipment.observer;
    const strafe = isActor ? plan.strafe.actor : plan.strafe.observer;
    const target = () => {
      const e = playerEntity(bot, other);
      return e && e.position ? e : null;
    };
    let shotsTotal = 0;
    return [
      {
        name: "face_off",
        run: async function () {
          await glanceAtOther(bot, other, localJitterMs(600, 1000));
          await equipItem(bot, me.ranged);
          await glanceAtOther(bot, other, localJitterMs(400, 800));
          await note(rcon, args, bot, episodeNum, "face_off", {
            ranged: me.ranged,
            enchant: me.enchant,
            shield: me.shield,
            distance: Number((target() ? target().position.distanceTo(bot.entity.position) : -1).toFixed(1)),
          });
          return {};
        },
      },
      {
        name: "ranged",
        run: async function () {
          const until = Date.now() + plan.rangedMs;
          while (Date.now() < until && !bot._episodeStopping) {
            if (!isAlive(bot)) {
              await sleep(300);
              continue;
            }
            const res = await rangedEngage(bot, target, {
              durationMs: Math.max(500, until - Date.now()),
              crossbow: me.ranged === "crossbow",
              minDist: 4,
              strafe,
              shield: me.shield,
            });
            shotsTotal += res.shots;
            if (res.reason === "close") {
              // opponent too close: short melee flurry, then back to range
              await equipItem(bot, me.weapon);
              await meleeEngage(bot, target, { durationMs: Math.min(3500, Math.max(300, until - Date.now())), shield: me.shield, critChance: 0.2, strafe: 0.4, returnWhenNoTarget: false });
              continue;
            }
            if (res.reason === "far") {
              const t = target();
              if (t) await walkNear(bot, t.position, 12, Math.min(3000, Math.max(300, until - Date.now())));
              continue;
            }
            if (res.reason !== "timeout") await sleep(300);
          }
          await note(rcon, args, bot, episodeNum, "ranged_end", { shots: shotsTotal, health: bot.health });
          return {};
        },
      },
      {
        name: "close_in",
        run: async function () {
          await equipItem(bot, me.weapon);
          await note(rcon, args, bot, episodeNum, "close_in", { weapon: me.weapon, health: bot.health });
          const res = await meleeEngage(bot, target, {
            durationMs: plan.meleeMs,
            shield: me.shield,
            blockChance: 0.4,
            critChance: 0.25,
            strafe: 0.5,
            cooldownScale: [1.2, 2.0],
            returnWhenNoTarget: false,
            onTick: async () => {
              if (isAlive(bot) && (!bot.heldItem || bot.heldItem.name !== me.weapon)) await equipItem(bot, me.weapon);
            },
          });
          await note(rcon, args, bot, episodeNum, "close_in_end", { attacks: res.attacks, health: bot.health });
          return {};
        },
      },
      {
        name: "collect_arrows",
        run: async function () {
          if (!plan.collect) {
            await glanceAtOther(bot, other, localJitterMs(800, 1300));
            return {};
          }
          // arrows stuck in the ground near this bot (Mineflayer name "arrow")
          const until = Date.now() + 7000;
          let picked = 0;
          while (Date.now() < until && !bot._episodeStopping && picked < 3) {
            const arrows = entitiesWhere(
              bot,
              (e) => (e.name === "arrow" || e.name === "spectral_arrow") && e.position.distanceTo(bot.entity.position) < 12,
            ).sort((a, b) => a.position.distanceTo(bot.entity.position) - b.position.distanceTo(bot.entity.position));
            if (!arrows.length) break;
            await walkNear(bot, arrows[0].position, 0.5, Math.max(400, Math.min(3000, until - Date.now())));
            await sleep(300);
            picked++;
          }
          if (picked === 0) await collectItems(bot, bot.entity.position, { radius: 6, max: 2, timeoutMs: 3000 });
          await note(rcon, args, bot, episodeNum, "collect_arrows", { walked_to: picked });
          return {};
        },
      },
      outroStep(),
    ];
  }
}

module.exports = { PvpRangedEpisode };
