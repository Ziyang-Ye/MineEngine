/**
 * pvpMelee - sword / axe duel with real damage.
 *
 * Setup (before the start snapshot): both bots get a random weapon (sword or
 * axe, wooden..netherite), a random armour set (none..netherite, random
 * pieces), optionally a shield in the off hand and a golden apple; they stand
 * 5-10 blocks apart on a flat / levelled arena facing each other; their spawn
 * point is set to the arena (doImmediateRespawn + keepInventory are on, so a
 * killed bot respawns there with its gear and fights on).
 *
 * Steps: face_off (eye contact, draw weapon) -> round_1 (melee: approach,
 * strafing, jump crits, sprint hits, shield blocks) -> breather (both back off,
 * low-health bots eat a golden apple, eye contact) -> round_2 -> outro.
 *
 * Damage episode (DAMAGE_ENABLED): difficulty normal, no resistance.
 * Logged: entity_damage (player_attack, victim_blocking), entity_knockback,
 * arm_swing, use_item_start/stop (shield, golden apple), item_consume,
 * entity_death + respawn ticks.
 */

const {
  CreatureEpisodeBase,
  backOff,
  chooseLoadout,
  consumeHeld,
  equipItem,
  glanceAtOther,
  hasItem,
  isAlive,
  localJitterMs,
  meleeEngage,
  note,
  outroStep,
  playerEntity,
  sleep,
} = require("./creature-common");

class PvpMeleeEpisode extends CreatureEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 6;
  static INIT_MAX_BOTS_DISTANCE = 12;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static DAMAGE_ENABLED = true;
  static SITE_HALF_SIZE = 3;
  static LEVEL_HALF_SIZE = 8;
  static REMOVE_EQUIPMENT_AFTER = true;

  planEpisode(bot, rng, args, frame, isActor, info) {
    // Loadouts: index 0 = actor, 1 = observer (roles only decide who starts).
    const actorLoadout = chooseLoadout(rng, { axeChance: 0.35, shieldChance: 0.35 });
    const observerLoadout = chooseLoadout(rng, { axeChance: 0.35, shieldChance: 0.35 });
    const startDist = 5 + Math.floor(rng() * 6); // 5..10
    const round1Ms = 18000 + Math.floor(rng() * 12000);
    const round2Ms = 12000 + Math.floor(rng() * 10000);
    const breather = rng() < 0.85;
    const aggrA = 0.1 + rng() * 0.35; // crit chance
    const aggrB = 0.1 + rng() * 0.35;
    const strafeA = 0.3 + rng() * 0.6;
    const strafeB = 0.3 + rng() * 0.6;
    const half = Math.ceil(startDist / 2);
    const items = (lo) => {
      const list = [[lo.weapon, 1]];
      if (lo.golden_apple) list.push(["golden_apple", 1]);
      list.push(["cooked_beef", 4]);
      return list;
    };
    return {
      actorStand: frame.at(half, 0),
      observerStand: frame.at(-(startDist - half), 0),
      items: { actor: items(actorLoadout), observer: items(observerLoadout) },
      equipment: { actor: actorLoadout, observer: observerLoadout },
      levelHalf: 8,
      round1Ms,
      round2Ms,
      breather,
      style: {
        actor: { crit: aggrA, strafe: strafeA },
        observer: { crit: aggrB, strafe: strafeB },
      },
      metadata: {
        variant: "melee_duel",
        loadouts: { actor: actorLoadout, observer: observerLoadout },
        start_distance: startDist,
        round_ms: [round1Ms, breather ? round2Ms : 0],
        breather,
        crit_chance: { actor: Number(aggrA.toFixed(2)), observer: Number(aggrB.toFixed(2)) },
        strafe: { actor: Number(strafeA.toFixed(2)), observer: Number(strafeB.toFixed(2)) },
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor, other } = ctx;
    const me = isActor ? plan.equipment.actor : plan.equipment.observer;
    const style = isActor ? plan.style.actor : plan.style.observer;
    const target = () => {
      const e = playerEntity(bot, other);
      return e && e.position ? e : null;
    };
    let deaths = 0;
    ctx.episode.listen(bot, "death", () => {
      deaths++;
    });
    const fight = (name, ms) => ({
      name,
      run: async function () {
        await equipItem(bot, me.weapon);
        await note(rcon, args, bot, episodeNum, `${name}_start`, { health: bot.health, weapon: me.weapon });
        const res = await meleeEngage(bot, target, {
          durationMs: ms,
          shield: me.shield,
          blockChance: 0.6,
          // human-like pace (vanilla cooldown x 1.2-2.0): fewer deaths per minute
          cooldownScale: [1.2, 2.0],
          critChance: style.crit,
          strafe: style.strafe,
          returnWhenNoTarget: false,
          onTick: async () => {
            // a respawned bot re-equips its weapon
            if (isAlive(bot) && (!bot.heldItem || bot.heldItem.name !== me.weapon)) await equipItem(bot, me.weapon);
          },
        });
        await note(rcon, args, bot, episodeNum, `${name}_end`, {
          health: bot.health,
          attacks: res.attacks,
          blocks: res.blocks,
          deaths,
        });
        return res;
      },
    });
    const steps = [
      {
        name: "face_off",
        run: async function () {
          await glanceAtOther(bot, other, localJitterMs(700, 1100));
          await equipItem(bot, me.weapon);
          await glanceAtOther(bot, other, localJitterMs(500, 900));
          await note(rcon, args, bot, episodeNum, "face_off", {
            weapon: me.weapon,
            armor: me.armor_tier,
            shield: me.shield,
          });
          return {};
        },
      },
      fight("round_1", plan.round1Ms),
    ];
    if (plan.breather) {
      steps.push({
        name: "breather",
        run: async function () {
          await backOff(bot, () => {
            const e = target();
            return e ? e.position.offset(0, 1.5, 0) : null;
          }, localJitterMs(900, 1500));
          if (isAlive(bot) && bot.health < 14 && hasItem(bot, "golden_apple")) {
            if (await equipItem(bot, "golden_apple")) {
              await note(rcon, args, bot, episodeNum, "eat_golden_apple", { health: bot.health });
              await consumeHeld(bot, 1900);
            }
          } else if (isAlive(bot) && bot.food < 18 && hasItem(bot, "cooked_beef")) {
            if (await equipItem(bot, "cooked_beef")) {
              await note(rcon, args, bot, episodeNum, "eat_beef", { food: bot.food });
              await consumeHeld(bot, 1900);
            }
          }
          await glanceAtOther(bot, other, localJitterMs(900, 1500));
          return {};
        },
      });
      steps.push(fight("round_2", plan.round2Ms));
    }
    steps.push(outroStep());
    return steps;
  }
}

module.exports = { PvpMeleeEpisode };
