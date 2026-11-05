/**
 * animalCare - peaceful interaction with passive / neutral animals.
 *
 * Setup (before the start snapshot): a flat / levelled pasture; 2-3
 * activities are drawn (biome weighted, no kind twice) and their animals are
 * summoned next to the site (PersistenceRequired, variants: sheep colours,
 * wolf / cat / frog variants, rabbit types, horse coats, llama colours, ...):
 *
 *   breed      two adults; actor and observer each feed one with its food
 *              (wheat, carrots, seeds, golden carrots, hay, cactus, flowers,
 *              spider eyes, berries) -> love mode -> baby + XP orb
 *   milk       cow / goat (bucket) or mooshroom (bowl); actor sometimes drinks
 *              / eats the result
 *   shear      sheep (optionally dyed first, then sheared -> wool drops the
 *              observer collects) or mooshroom (sheared -> turns into a cow,
 *              mushrooms drop)
 *   lead       actor leashes an animal, walks it 5-7 blocks, unleashes it
 *              (the lead drops and is picked up again)
 *   tame       wolf (bones), cat / ocelot (raw cod), parrot (seeds); repeated
 *              feeding until tamed, then the actor toggles sit / walks away
 *   feed_baby  two baby animals; both bots feed them (growth)
 *   brush      armadillo brushed -> scute drops, observer picks it up
 *   eggs       actor throws eggs next to two chickens (1/8 hatch a chick)
 *
 * Not a damage episode (peaceful, resistance). The observer feeds / collects
 * in the shared activities and watches the actor otherwise.
 * Logged: entity_spawn (BREEDING babies, EGG chicks, SHEARED cow, items, XP
 * orbs, thrown eggs), entity_remove (pickups, mooshroom transform), player
 * use / arm_swing / item_consume, entities[] (all animals every tick).
 */

const { Vec3 } = require("vec3");

const { ANIMALS, DYE_COLORS, animalSummonCommand, animalVariant, biomeGroup, pickWeighted } = require("../../utils/creatures");
const {
  CreatureEpisodeBase,
  collectItems,
  consumeHeld,
  entitiesOfType,
  equipItem,
  glanceAtOther,
  hasItem,
  interactEntity,
  isAlive,
  itemEntitiesNear,
  localJitterMs,
  note,
  outroStep,
  playerEntity,
  readyStep,
  sleep,
  startWatching,
  turnTo,
  useItem,
  walkNear,
} = require("./creature-common");

const KINDS = [
  { kind: "breed", w: 4 },
  { kind: "milk", w: 2 },
  { kind: "shear", w: 2.2 },
  { kind: "lead", w: 2 },
  { kind: "tame", w: 2 },
  { kind: "feed_baby", w: 1.4 },
  { kind: "brush", w: 0.6 },
  { kind: "eggs", w: 1 },
];

const ANIMALS_FOR = {
  // turtles are not bred (a pregnant turtle walks to its home beach and lays
  // eggs long after the episode); their babies are fed instead (feed_baby)
  breed: (id, a) => a.breed && !["wolf", "cat", "ocelot", "turtle"].includes(id),
  milk: (id) => ["cow", "goat", "mooshroom"].includes(id),
  shear: (id) => ["sheep", "mooshroom"].includes(id),
  lead: (id, a) => a.leash && !["bee", "parrot"].includes(id),
  tame: (id) => ["wolf", "cat", "parrot", "ocelot"].includes(id),
  feed_baby: (id, a) => a.breed && !a.tameRequiredToBreed && !["bee", "frog"].includes(id),
  brush: (id) => id === "armadillo",
  eggs: (id) => id === "chicken",
};

const MAX_SLOTS = 3;

// Predator -> prey (vanilla AI): never put them into the same episode, the
// predator would kill the activity's animals.
const PREY = {
  fox: ["chicken", "rabbit"],
  wolf: ["sheep", "rabbit", "fox", "llama"],
  ocelot: ["chicken"],
  cat: ["rabbit", "chicken"],
  llama: ["wolf"],
};
function conflicts(a, b) {
  return (PREY[a] || []).includes(b) || (PREY[b] || []).includes(a);
}

function isBaby(e) {
  // AgeableMob DATA_BABY_ID is metadata index 16 in 1.21
  return Boolean(e && e.metadata && e.metadata[16] === true);
}

class AnimalCareEpisode extends CreatureEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 8;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;
  static LEVEL_HALF_SIZE = 9;

  planEpisode(bot, rng, args, frame, isActor, info) {
    const group = biomeGroup(info.biome);
    const nAct = rng() < 0.55 ? 3 : 2;
    const used = new Set();
    const acts = [];
    for (let i = 0; i < MAX_SLOTS; i++) {
      const uKind = rng();
      const uAnimal = rng();
      const uFood = rng();
      const uExtra = rng();
      const variantDraws = [rng(), rng()];
      if (i >= nAct) continue;
      const kindItem = pickWeighted(() => uKind, KINDS, (k) => {
        if (used.has(k.kind)) return 0;
        if (k.kind === "eggs" && acts.some((x) => conflicts(x.animal, "chicken"))) return 0;
        if (k.kind === "brush") return ["savanna", "badlands"].includes(group) ? 3 : k.w;
        return k.w;
      });
      if (!kindItem) continue;
      const kind = kindItem.kind;
      used.add(kind);
      // biome weighted animal for this kind (one draw consumed above)
      const items = Object.keys(ANIMALS).map((id) => {
        const a = ANIMALS[id];
        const m = a.mult && a.mult[group] !== undefined ? a.mult[group] : 1;
        const clash = acts.some((x) => conflicts(x.animal, id) || (kind === "eggs" && conflicts(x.animal, "chicken")));
        return { id, w: ANIMALS_FOR[kind](id, a) && !clash ? a.w * m : 0 };
      });
      const animalItem = pickWeighted(() => uAnimal, items);
      if (!animalItem) continue;
      const animal = animalItem.id;
      const a = ANIMALS[animal];
      let k = 0;
      const variant = animalVariant(() => variantDraws[k++ % 2], animal, info.biome);
      const food = a.food[Math.floor(uFood * a.food.length) % a.food.length];
      acts.push({ kind, animal, food, extra: uExtra, variant, slot: i });
    }
    // anchors: animals 3-4 blocks in front of the bots, spread sideways
    const offsets = acts.length === 3 ? [-4, 0, 4] : [-2.5, 2.5];
    const actorItems = [];
    const observerItems = [];
    const summons = [];
    const meta = [];
    acts.forEach((act, idx) => {
      act.anchor = frame.at(-3, offsets[idx]);
      const base = { variantNbt: act.variant.nbt, yawDeg: 0 };
      const at = (du, dp) => {
        const c = frame.at(-3 + du, offsets[idx] + dp);
        return new Vec3(c.x + 0.5, frame.level, c.z + 0.5);
      };
      const tame = ["horse", "donkey", "llama"].includes(act.animal);
      switch (act.kind) {
        case "breed":
          summons.push(animalSummonCommand(act.animal, at(0, -1), { ...base, tame }));
          summons.push(animalSummonCommand(act.animal, at(0, 1), { ...base, tame }));
          actorItems.push([act.food, 3]);
          observerItems.push([act.food, 2]);
          break;
        case "milk":
          summons.push(animalSummonCommand(act.animal, at(0, 0), base));
          actorItems.push([act.animal === "mooshroom" ? "bowl" : "bucket", 1]);
          act.consume = act.extra < 0.6;
          break;
        case "shear":
          summons.push(animalSummonCommand(act.animal, at(0, 0), base));
          actorItems.push(["shears", 1]);
          if (act.animal === "sheep" && act.extra < 0.5) {
            act.dye = DYE_COLORS[Math.floor(act.extra * 2 * 16) % 16];
            actorItems.push([`${act.dye}_dye`, 1]);
          }
          break;
        case "lead":
          summons.push(animalSummonCommand(act.animal, at(0, 0), { ...base, tame }));
          actorItems.push(["lead", 1]);
          act.walkSide = act.extra < 0.5 ? -1 : 1;
          break;
        case "tame": {
          const far = act.animal === "cat" || act.animal === "ocelot" ? -2 : 0;
          summons.push(animalSummonCommand(act.animal, at(far, 0), base));
          const tameItem = ANIMALS[act.animal].tame;
          act.tameItem = tameItem;
          actorItems.push([tameItem, act.animal === "parrot" ? 12 : 8]);
          break;
        }
        case "feed_baby":
          summons.push(animalSummonCommand(act.animal, at(0, -1), { ...base, baby: true }));
          summons.push(animalSummonCommand(act.animal, at(0, 1), { ...base, baby: true }));
          if (act.extra < 0.6) summons.push(animalSummonCommand(act.animal, at(-1.5, 0), base));
          actorItems.push([act.food, 4]);
          observerItems.push([act.food, 3]);
          break;
        case "brush":
          summons.push(animalSummonCommand(act.animal, at(0, 0), base));
          actorItems.push(["brush", 1]);
          break;
        case "eggs":
          summons.push(animalSummonCommand("chicken", at(-1, -1), base));
          summons.push(animalSummonCommand("chicken", at(-1, 1), base));
          actorItems.push(["egg", 8]);
          break;
        default:
          break;
      }
      meta.push({
        kind: act.kind,
        animal: act.animal,
        variant: act.variant.variant,
        food: ["breed", "feed_baby"].includes(act.kind) ? act.food : act.kind === "tame" ? act.tameItem : null,
        dye: act.dye || null,
        anchor: [act.anchor.x, act.anchor.y, act.anchor.z],
      });
    });
    return {
      actorStand: frame.at(2, -1),
      observerStand: frame.at(2, 2),
      items: { actor: actorItems, observer: observerItems },
      levelHalf: 9,
      summonCommands: summons,
      acts,
      metadata: {
        variant: "animal_care",
        activities: meta,
        animals: [...new Set(acts.map((x) => x.animal))],
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor, other, frame } = ctx;
    const anchorPoint = (act) => new Vec3(act.anchor.x + 0.5, frame.level + 0.6, act.anchor.z + 0.5);
    const animalsNear = (act, type = act.animal) => entitiesOfType(bot, type, anchorPoint(act), 26);
    const nearestAnimal = (act, type = act.animal) => {
      const list = animalsNear(act, type);
      if (!list.length) return null;
      list.sort((x, y) => x.position.distanceTo(bot.entity.position) - y.position.distanceTo(bot.entity.position));
      return list[0];
    };
    const actorEntity = () => (isActor ? bot.entity : playerEntity(bot, other));
    const midpoint = (a, b) => (a && b ? a.position.plus(b.position).scaled(0.5).offset(0, 1, 0) : null);
    /** walk close to an entity and right click it with `item` (null: empty hand) */
    const useOnAnimal = async (e, item, range = 2.2) => {
      if (!e) return false;
      if (item) {
        if (!(await equipItem(bot, item))) return false;
      } else if (bot.heldItem) {
        try {
          await bot.unequip("hand");
        } catch (_) {}
      }
      // follow a wandering animal for up to ~7 s
      const chaseUntil = Date.now() + 7000;
      while (Date.now() < chaseUntil && !bot._episodeStopping) {
        const cur = bot.entities[e.id];
        if (!cur) return false;
        if (cur.position.distanceTo(bot.entity.position) <= range + 0.3) break;
        await walkNear(bot, cur.position, range, Math.min(2500, Math.max(300, chaseUntil - Date.now())));
      }
      const fresh = bot.entities[e.id] || e;
      if (fresh.position.distanceTo(bot.entity.position) > 4.2) return false;
      const ok = await interactEntity(bot, fresh);
      if (isActor && Math.random() < 0.4) {
        await sleep(localJitterMs(200, 400));
        await glanceAtOther(bot, other, localJitterMs(500, 900));
      }
      return ok;
    };
    /**
     * Observer position: on the far side of the animal as seen from the
     * actor, so the actor's camera (looking at the animal) also shows the
     * observer, and the observer sees the actor behind the animal.
     */
    const observerSpot = (act) => {
      const ac = actorEntity();
      const an = nearestAnimal(act);
      if (!ac || !an) return null;
      const dx = an.position.x - ac.position.x;
      const dz = an.position.z - ac.position.z;
      const d = Math.hypot(dx, dz) || 1;
      const side = (bot.username.charCodeAt(0) % 2 ? 1 : -1) * 1.2;
      return new Vec3(
        an.position.x + (dx / d) * 3.2 + (-dz / d) * side,
        frame.level,
        an.position.z + (dz / d) * 3.2 + (dx / d) * side,
      );
    };
    const repositionObserver = async (act, maxMs = 3000) => {
      const spot = observerSpot(act);
      if (spot && spot.distanceTo(bot.entity.position) > 2.0) await walkNear(bot, spot, 1.0, maxMs);
    };
    const watchActivity = async (act, ms) => {
      const until = Date.now() + ms;
      if (!isActor) await repositionObserver(act, Math.min(3000, ms));
      const stop = startWatching(bot, () => {
        const an = nearestAnimal(act);
        const ac = actorEntity();
        if (Math.random() < 0.55 && ac) return ac.position.offset(0, 1.5, 0);
        return midpoint(an, ac) || anchorPoint(act);
      });
      let nextMove = Date.now() + 3500;
      while (Date.now() < until && !bot._episodeStopping) {
        if (!isActor && Date.now() >= nextMove) {
          nextMove = Date.now() + 3500;
          const spot = observerSpot(act);
          if (spot && spot.distanceTo(bot.entity.position) > 3.0) {
            await stop();
            await walkNear(bot, spot, 1.0, Math.max(300, Math.min(2500, until - Date.now())));
            return watchActivity(act, Math.max(0, until - Date.now()));
          }
        }
        await sleep(100);
      }
      await stop();
    };

    const runners = {
      breed: async (act) => {
        const list = animalsNear(act);
        const mine = isActor ? list[0] : list[1] || list[0];
        const fed = await useOnAnimal(mine, act.food);
        await note(rcon, args, bot, episodeNum, "breed_feed", { animal: act.animal, food: act.food, fed, id: mine ? mine.id : null });
        await sleep(localJitterMs(300, 600));
        await watchActivity(act, localJitterMs(5500, 7000));
        const babies = animalsNear(act).filter(isBaby).length;
        if (isActor) await note(rcon, args, bot, episodeNum, "breed_result", { animal: act.animal, count: animalsNear(act).length, babies });
        // the actor feeds the baby once if there is one
        const baby = animalsNear(act).find(isBaby);
        if (isActor && baby && hasItem(bot, act.food)) await useOnAnimal(baby, act.food);
      },
      milk: async (act) => {
        if (!isActor) return watchActivity(act, localJitterMs(6000, 8000));
        const tool = act.animal === "mooshroom" ? "bowl" : "bucket";
        const ok = await useOnAnimal(nearestAnimal(act), tool);
        await sleep(400);
        const result = act.animal === "mooshroom" ? "mushroom_stew" : "milk_bucket";
        const got = hasItem(bot, result);
        await note(rcon, args, bot, episodeNum, "milk", { animal: act.animal, tool, ok, got });
        await glanceAtOther(bot, other, localJitterMs(600, 1000));
        if (got && act.consume && (await equipItem(bot, result))) {
          await consumeHeld(bot, 1800);
          await note(rcon, args, bot, episodeNum, "consume", { item: result });
        }
      },
      shear: async (act) => {
        if (isActor) {
          const target = nearestAnimal(act);
          if (act.dye) {
            await useOnAnimal(target, `${act.dye}_dye`);
            await note(rcon, args, bot, episodeNum, "dye", { animal: act.animal, color: act.dye });
            await sleep(localJitterMs(700, 1100));
          }
          const ok = await useOnAnimal(bot.entities[target ? target.id : -1] || nearestAnimal(act), "shears");
          await note(rcon, args, bot, episodeNum, "shear", { animal: act.animal, ok });
          await sleep(localJitterMs(800, 1300));
          await glanceAtOther(bot, other, localJitterMs(1500, 2500));
        } else {
          // watch until the drops appear (or ~9 s), then collect them
          const until = Date.now() + 9000;
          while (Date.now() < until && !bot._episodeStopping) {
            await watchActivity(act, 1000);
            if (itemEntitiesNear(bot, anchorPoint(act), 9).length) break;
          }
          await sleep(localJitterMs(300, 700));
          const n = await collectItems(bot, anchorPoint(act), { radius: 9, max: 4, timeoutMs: 7000 });
          await note(rcon, args, bot, episodeNum, "collect_drops", { walked_to: n });
        }
      },
      lead: async (act) => {
        if (!isActor) {
          await watchActivity(act, localJitterMs(2000, 3000));
          // follow the walk at a distance
          const stop = startWatching(bot, () => {
            const ac = actorEntity();
            return ac ? ac.position.offset(0, 1.4, 0) : null;
          });
          await sleep(localJitterMs(7000, 9000));
          await stop();
          return;
        }
        const target = nearestAnimal(act);
        const ok = await useOnAnimal(target, "lead");
        await note(rcon, args, bot, episodeNum, "leash", { animal: act.animal, ok });
        const dest = frame.at(-1, (act.walkSide || 1) * 6);
        await walkNear(bot, new Vec3(dest.x + 0.5, frame.level, dest.z + 0.5), 1, 6000);
        const a2 = target ? bot.entities[target.id] : null;
        if (a2) await turnTo(bot, a2.position.offset(0, 0.8, 0), 600, 12);
        await sleep(localJitterMs(800, 1200));
        // unleash (empty hand), then pick the lead up again
        const unl = await useOnAnimal(a2, null, 2.5);
        await note(rcon, args, bot, episodeNum, "unleash", { animal: act.animal, ok: unl });
        await sleep(500);
        if (!hasItem(bot, "lead")) await collectItems(bot, bot.entity.position, { radius: 5, max: 1, timeoutMs: 3000 });
        await glanceAtOther(bot, other, localJitterMs(600, 1000));
      },
      tame: async (act) => {
        if (!isActor) return watchActivity(act, localJitterMs(11000, 14000));
        const item = act.tameItem;
        const skittish = act.animal === "cat" || act.animal === "ocelot";
        let tries = 0;
        const until = Date.now() + 13000;
        await equipItem(bot, item);
        while (Date.now() < until && tries < 6 && hasItem(bot, item) && !bot._episodeStopping) {
          const target = nearestAnimal(act);
          if (!target) break;
          if (skittish) {
            // hold the fish and stay still until it comes close (tempt goal)
            await turnTo(bot, target.position.offset(0, 0.5, 0), 500, 10);
            const waitUntil = Date.now() + 3000;
            while (Date.now() < waitUntil && target.position.distanceTo(bot.entity.position) > 2.8) await sleep(100);
            if (target.position.distanceTo(bot.entity.position) > 3.5) {
              bot.setControlState("sneak", true);
              await walkNear(bot, target.position, 2, 2500);
              bot.setControlState("sneak", false);
            }
          }
          await useOnAnimal(target, item, 2.0);
          tries++;
          await sleep(localJitterMs(700, 1100));
        }
        await note(rcon, args, bot, episodeNum, "tame_attempts", { animal: act.animal, item, tries });
        // a tamed wolf / cat sits: toggle it once (empty hand) and walk off
        const t2 = nearestAnimal(act);
        if (t2 && act.animal !== "parrot") {
          await useOnAnimal(t2, null, 2.5);
          await sleep(400);
          const away = frame.at(1, 0);
          await walkNear(bot, new Vec3(away.x + 0.5, frame.level, away.z + 0.5), 1, 3000);
          await turnTo(bot, t2.position.offset(0, 0.5, 0), 600, 10);
        }
      },
      feed_baby: async (act) => {
        const babies = animalsNear(act).filter(isBaby);
        const list = babies.length ? babies : animalsNear(act);
        let fed = 0;
        for (let i = 0; i < (isActor ? 3 : 2); i++) {
          const e = list.length ? list[(isActor ? i : i + 1) % list.length] : null;
          if (!e || !hasItem(bot, act.food)) break;
          if (await useOnAnimal(bot.entities[e.id] || e, act.food)) fed++;
          await sleep(localJitterMs(600, 1000));
        }
        await note(rcon, args, bot, episodeNum, "feed_baby", { animal: act.animal, food: act.food, fed, babies: babies.length });
        await glanceAtOther(bot, other, localJitterMs(600, 1000));
      },
      brush: async (act) => {
        if (isActor) {
          let ok = false;
          for (let i = 0; i < 2 && !ok; i++) ok = await useOnAnimal(nearestAnimal(act), "brush", 2.0);
          await sleep(500);
          await note(rcon, args, bot, episodeNum, "brush", { animal: act.animal, ok });
          await glanceAtOther(bot, other, localJitterMs(2000, 3000));
        } else {
          const until = Date.now() + 8000;
          while (Date.now() < until && !bot._episodeStopping) {
            await watchActivity(act, 1000);
            if (itemEntitiesNear(bot, anchorPoint(act), 8).length) break;
          }
          const n = await collectItems(bot, anchorPoint(act), { radius: 8, max: 2, timeoutMs: 5000 });
          await note(rcon, args, bot, episodeNum, "collect_scute", { walked_to: n });
        }
      },
      eggs: async (act) => {
        if (!isActor) return watchActivity(act, localJitterMs(8000, 10000));
        if (!(await equipItem(bot, "egg"))) return;
        let thrown = 0;
        const target = anchorPoint(act);
        for (let i = 0; i < 6 && hasItem(bot, "egg"); i++) {
          const p = target.offset((Math.random() - 0.5) * 3, -0.5, (Math.random() - 0.5) * 3);
          await turnTo(bot, p, 350, 14);
          useItem(bot, false);
          bot.swingArm("right");
          thrown++;
          await sleep(localJitterMs(500, 900));
        }
        await note(rcon, args, bot, episodeNum, "throw_eggs", { thrown });
        await watchActivity(act, localJitterMs(2000, 3000));
        await note(rcon, args, bot, episodeNum, "eggs_result", { chickens: animalsNear(act, "chicken").length });
      },
    };

    const steps = [readyStep("ready", true)];
    plan.acts.forEach((act, idx) => {
      steps.push({
        name: `act${idx}_${act.kind}`,
        run: async function () {
          const seen = {};
          for (const e of Object.values(bot.entities)) {
            if (e && e !== bot.entity && e.position && e.position.distanceTo(anchorPoint(act)) < 24) seen[e.name] = (seen[e.name] || 0) + 1;
          }
          console.log(`[${bot.username}] activity ${act.kind} ${act.animal}: entities near anchor ${JSON.stringify(seen)}`);
          await note(rcon, args, bot, episodeNum, `activity_${act.kind}`, { animal: act.animal, n: animalsNear(act).length });
          try {
            await runners[act.kind](act);
          } finally {
            bot.setControlState("sneak", false);
          }
          await glanceAtOther(bot, other, localJitterMs(500, 900));
          return {};
        },
      });
    });
    steps.push(outroStep());
    return steps;
  }
}

module.exports = { AnimalCareEpisode, isBaby };
