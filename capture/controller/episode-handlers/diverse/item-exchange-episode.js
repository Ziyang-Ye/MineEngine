/**
 * itemExchange - handing items over: tossing, picking up, chests, villagers.
 *
 * Setup (before the start snapshot): the bots stand 4 blocks apart facing
 * each other on a flat / levelled site; each gets 3-4 random item stacks from
 * a diverse pool (gems, food, flowers, tools, blocks, mob drops, ...). In
 * some episodes a villager (random profession, biome type) or a wandering
 * trader with fixed trades is summoned next to the site and the bots get
 * emeralds.
 *
 * Steps (the middle ones in a random order, 3-5 of them):
 *   toss          actor throws 1-3 items (Q-drop: single / Ctrl+Q: stack)
 *                 towards the observer, the observer walks over them
 *   toss_back     the observer throws something back
 *   drop_at_feet  actor walks next to the observer and drops an item at its
 *                 feet; the observer steps on it after the pickup delay
 *   chest         actor places a chest / trapped chest (block_place), opens
 *                 it, deposits items, closes; the observer opens it and takes
 *                 items out; sometimes the actor breaks the chest afterwards
 *                 (block_break, the chest and its remaining content drop)
 *   trade         both trade emeralds with the villager / wandering trader
 *                 (merchant window, XP orbs)
 *
 * Not a damage episode. Logged: entity_spawn reason ITEM (dropped items),
 * entity_remove (pickups), block_place / block_break / player_interact
 * (chest), dig_*, arm_swing, held_item_change, entity_spawn experience_orb
 * (trades). Chest CONTENTS are block-entity data and are not in the world
 * log (see OUTPUT_SCHEMA 5.5); the notes record what was deposited/withdrawn.
 */

const { Vec3 } = require("vec3");

const { VILLAGER_PROFESSIONS, biomeGroup, summonCommand, tagsNbt, villagerTypeFor } = require("../../utils/creatures");
const {
  CreatureEpisodeBase,
  collectItems,
  countItem,
  dropHeld,
  equipItem,
  glanceAtOther,
  itemEntitiesNear,
  localJitterMs,
  note,
  outroStep,
  playerEntity,
  readyStep,
  sleep,
  startWatching,
  turnTo,
  walkNear,
  withTimeout,
} = require("./creature-common");
const { digAt, placeFullBlock } = require("../state/state-common");

const ITEM_POOL = [
  "diamond", "emerald", "gold_ingot", "iron_ingot", "copper_ingot", "lapis_lazuli", "redstone", "coal", "amethyst_shard", "quartz",
  "apple", "bread", "cookie", "golden_carrot", "carrot", "potato", "baked_potato", "melon_slice", "sweet_berries", "pumpkin_pie", "cooked_cod",
  "poppy", "dandelion", "cornflower", "oxeye_daisy", "blue_orchid", "allium", "azure_bluet", "lily_of_the_valley",
  "oak_log", "birch_log", "cobblestone", "bricks", "glass", "white_wool", "red_wool", "torch", "ladder",
  "arrow", "feather", "string", "bone", "leather", "slime_ball", "gunpowder", "ender_pearl", "rabbit_foot", "nautilus_shell", "prismarine_shard",
  "book", "paper", "compass", "clock", "name_tag", "bucket", "glass_bottle", "iron_sword", "stone_pickaxe", "shears", "fishing_rod", "saddle",
];
const UNSTACKABLE = new Set(["iron_sword", "stone_pickaxe", "shears", "fishing_rod", "saddle", "bucket", "compass", "clock", "name_tag", "pumpkin_pie"]);
const CHEST_TYPES = ["chest", "chest", "trapped_chest"];
const TRADE_GOODS = ["bread", "apple", "arrow", "cookie", "glass", "book", "map", "iron_ingot", "lantern", "carrot", "golden_carrot", "redstone", "white_wool"];

class ItemExchangeEpisode extends CreatureEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 8;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;
  static LEVEL_HALF_SIZE = 7;

  planEpisode(bot, rng, args, frame, isActor, info) {
    const group = biomeGroup(info.biome);
    const pickItems = (n) => {
      const out = [];
      for (let i = 0; i < 4; i++) {
        const u = rng();
        const uc = rng();
        if (i >= n) continue;
        let name = ITEM_POOL[Math.floor(u * ITEM_POOL.length) % ITEM_POOL.length];
        if (out.some((x) => x[0] === name)) name = ITEM_POOL[(Math.floor(u * ITEM_POOL.length) + 7) % ITEM_POOL.length];
        const count = UNSTACKABLE.has(name) ? 1 : 2 + Math.floor(uc * 14);
        out.push([name, count]);
      }
      return out;
    };
    const actorItems = pickItems(4);
    const observerItems = pickItems(3);
    const uChest = rng();
    const uChestType = rng();
    const uBreak = rng();
    const uTrade = rng();
    const uTrader = rng();
    const uProf = rng();
    const uGoods = [rng(), rng(), rng()];
    const uOrder = [rng(), rng(), rng(), rng(), rng()];
    const uFeet = rng();
    const useChest = uChest < 0.75;
    const chestType = CHEST_TYPES[Math.floor(uChestType * CHEST_TYPES.length) % CHEST_TYPES.length];
    const trade = uTrade < 0.4;
    const trader = uTrader < 0.25 ? "wandering_trader" : "villager";
    const profession = VILLAGER_PROFESSIONS[Math.floor(uProf * VILLAGER_PROFESSIONS.length) % VILLAGER_PROFESSIONS.length];
    // step order
    const middle = ["toss", "toss_back"];
    if (uFeet < 0.6) middle.push("drop_at_feet");
    if (useChest) middle.push("chest");
    if (trade) middle.push("trade");
    const keyed = middle.map((m, i) => ({ m, k: m === "toss" ? -1 : uOrder[i % uOrder.length] }));
    keyed.sort((a, b) => a.k - b.k);
    const order = keyed.map((x) => x.m);
    // toss before toss_back
    const ti = order.indexOf("toss");
    const bi = order.indexOf("toss_back");
    if (bi < ti) {
      order.splice(bi, 1);
      order.splice(order.indexOf("toss") + 1, 0, "toss_back");
    }
    const chestPos = frame.at(0, 2);
    const summons = [];
    let offers = [];
    if (trade) {
      offers = uGoods.map((u, i) => {
        const good = TRADE_GOODS[Math.floor(u * TRADE_GOODS.length) % TRADE_GOODS.length];
        return { cost: 1 + i, good, count: 1 + Math.floor(u * 6) };
      });
      const recipes = offers
        .map((o) => `{buy:{id:"minecraft:emerald",count:${o.cost}},sell:{id:"minecraft:${o.good}",count:${o.count}},maxUses:12,uses:0,rewardExp:1b,xp:2}`)
        .join(",");
      const vpos = frame.at(-1, -3);
      const parts = [
        "PersistenceRequired:1b",
        tagsNbt(["solaris_r2_trader"]),
        `Offers:{Recipes:[${recipes}]}`,
        "Rotation:[0.0f,0.0f]",
      ];
      if (trader === "villager") parts.push(`VillagerData:{type:"minecraft:${villagerTypeFor(group)}",profession:"minecraft:${profession}",level:2}`, "Xp:10");
      else parts.push("DespawnDelay:48000");
      summons.push(summonCommand(trader, new Vec3(vpos.x + 0.5, frame.level, vpos.z + 0.5), parts));
    }
    const extraActor = trade ? [["emerald", 8]] : [];
    if (useChest) extraActor.push([chestType, 1]);
    const extraObserver = trade ? [["emerald", 6]] : [];
    return {
      actorStand: frame.at(2, 0),
      observerStand: frame.at(-2, 0),
      items: { actor: [...actorItems, ...extraActor], observer: [...observerItems, ...extraObserver] },
      levelHalf: 7,
      summonCommands: summons,
      order,
      chestType,
      chestPos,
      breakChest: uBreak < 0.5,
      trade,
      trader,
      offers,
      actorItems,
      observerItems,
      metadata: {
        variant: "item_exchange",
        actor_items: actorItems,
        observer_items: observerItems,
        order,
        chest: useChest ? { type: chestType, pos: [chestPos.x, chestPos.y, chestPos.z], break_after: uBreak < 0.5 } : null,
        trade: trade ? { trader, profession: trader === "villager" ? profession : null, offers } : null,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor, other, frame } = ctx;
    const L = frame.level;
    const myItems = () => (isActor ? plan.actorItems : plan.observerItems).map((x) => x[0]).filter((n) => countItem(bot, n) > 0);
    const otherPos = () => {
      const o = playerEntity(bot, other);
      return o ? o.position : null;
    };
    const watchOther = async (ms) => {
      const stop = startWatching(bot, () => {
        const p = otherPos();
        return p ? p.offset(0, 1.4, 0) : null;
      });
      await sleep(ms);
      await stop();
    };
    /** Throw `n` items towards the other bot. */
    const tossTowardsOther = async (n) => {
      const thrown = [];
      const names = myItems();
      for (let i = 0; i < n && i < names.length; i++) {
        const name = names[(i + Math.floor(Math.random() * names.length)) % names.length];
        if (thrown.some((t) => t.item === name)) continue;
        if (!(await equipItem(bot, name))) continue;
        const p = otherPos();
        if (p) await turnTo(bot, p.offset(0, 0.9, 0), 500, 14);
        const all = Math.random() < 0.35;
        const before = countItem(bot, name);
        dropHeld(bot, all);
        await sleep(400);
        thrown.push({ item: name, all, count: Math.max(0, before - countItem(bot, name)) });
        await sleep(localJitterMs(300, 700));
      }
      return thrown;
    };
    /** Walk over items lying between the bots (pickup delay is 2 s). */
    let collected = 0;
    ctx.episode.listen(bot, "playerCollect", (collector) => {
      if (collector === bot.entity) collected++;
    });
    const pickUp = async (timeoutMs = 7000) => {
      const until = Date.now() + timeoutMs;
      const before = collected;
      await sleep(600);
      while (Date.now() < until && !bot._episodeStopping) {
        const items = itemEntitiesNear(bot, bot.entity.position, 7);
        if (!items.length) break;
        items.sort((a, b) => a.position.distanceTo(bot.entity.position) - b.position.distanceTo(bot.entity.position));
        const it = items[0];
        await walkNear(bot, it.position, 0.3, Math.max(500, Math.min(3000, until - Date.now())));
        const t0 = Date.now();
        while (Date.now() - t0 < 2200 && bot.entities[it.id]) await sleep(100);
      }
      return collected - before;
    };
    const chestBlockPos = new Vec3(plan.chestPos.x, L, plan.chestPos.z);

    const runners = {
      toss: async () => {
        if (isActor) {
          const thrown = await tossTowardsOther(1 + Math.floor(Math.random() * 3));
          await note(rcon, args, bot, episodeNum, "toss", { to: other, thrown });
          await watchOther(localJitterMs(2500, 3500));
        } else {
          await watchOther(localJitterMs(1800, 2600));
          const got = await pickUp(7000);
          await note(rcon, args, bot, episodeNum, "pickup", { got });
          await glanceAtOther(bot, other, localJitterMs(500, 800));
        }
      },
      toss_back: async () => {
        if (!isActor) {
          const thrown = await tossTowardsOther(1 + Math.floor(Math.random() * 2));
          await note(rcon, args, bot, episodeNum, "toss_back", { to: other, thrown });
          await watchOther(localJitterMs(2500, 3500));
        } else {
          await watchOther(localJitterMs(1800, 2600));
          const got = await pickUp(7000);
          await note(rcon, args, bot, episodeNum, "pickup", { got });
          await glanceAtOther(bot, other, localJitterMs(500, 800));
        }
      },
      drop_at_feet: async () => {
        if (isActor) {
          const p = otherPos();
          if (p) await walkNear(bot, p, 1.6, 4000);
          const names = myItems();
          if (names.length && (await equipItem(bot, names[0]))) {
            const q = otherPos();
            if (q) await turnTo(bot, q.offset(0, 0.1, 0), 500, 14);
            dropHeld(bot, false);
            await note(rcon, args, bot, episodeNum, "drop_at_feet", { item: names[0] });
          }
          await sleep(300);
          // step back so the partner can grab it
          bot.setControlState("back", true);
          await sleep(500);
          bot.setControlState("back", false);
          await watchOther(localJitterMs(2500, 3500));
        } else {
          await watchOther(localJitterMs(2500, 3200));
          const got = await pickUp(5000);
          await note(rcon, args, bot, episodeNum, "pickup", { got });
          await glanceAtOther(bot, other, localJitterMs(500, 800));
        }
      },
      chest: async () => {
        if (isActor) {
          // place the chest, open, deposit, close
          await walkNear(bot, chestBlockPos.offset(0.5, 0, 0.5), 2.2, 4000);
          const cover = bot.blockAt(chestBlockPos);
          if (cover && cover.name !== "air" && cover.name !== "cave_air" && (cover.name === "snow" || cover.boundingBox === "empty")) {
            // snow layer / grass on the spot: break it first (shovel)
            await equipItem(bot, "diamond_shovel");
            await withTimeout(digAt(bot, chestBlockPos), 4000, false, `${bot.username} clear chest spot`);
            await sleep(300);
          }
          const placed = await withTimeout(placeFullBlock(bot, chestBlockPos, plan.chestType), 8000, false, `${bot.username} place chest`);
          const blk = bot.blockAt(chestBlockPos);
          const deposited = [];
          if (blk && blk.name === plan.chestType) {
            await sleep(300);
            const win = await withTimeout(bot.openContainer(blk), 4000, null, `${bot.username} open chest`);
            if (win) {
              for (const name of myItems().slice(0, 2)) {
                const it = bot.inventory.items().find((x) => x.name === name);
                if (!it) continue;
                const n = Math.max(1, Math.ceil(it.count / 2));
                const r = await withTimeout(win.deposit(it.type, null, n), 3000, "timeout");
                if (r !== "timeout") deposited.push([name, n]);
                await sleep(localJitterMs(300, 600));
              }
              await sleep(localJitterMs(400, 800));
              try {
                win.close();
              } catch (_) {}
            }
          }
          await note(rcon, args, bot, episodeNum, "chest_deposit", { placed: Boolean(placed), chest: plan.chestType, deposited });
          // step aside and watch the partner use the chest
          const aside = frame.at(1, 4);
          await walkNear(bot, new Vec3(aside.x + 0.5, L, aside.z + 0.5), 1.0, 3000);
          await watchOther(localJitterMs(5000, 6500));
          if (plan.breakChest) {
            const b2 = bot.blockAt(chestBlockPos);
            if (b2 && b2.name.includes("chest")) {
              await walkNear(bot, chestBlockPos.offset(0.5, 0, 0.5), 2.5, 3000);
              await equipItem(bot, "diamond_axe");
              const ok = await withTimeout(digAt(bot, chestBlockPos), 8000, false, `${bot.username} break chest`);
              await note(rcon, args, bot, episodeNum, "chest_break", { ok: Boolean(ok) });
              await sleep(500);
              await collectItems(bot, chestBlockPos.offset(0.5, 0.5, 0.5), { radius: 4, max: 3, timeoutMs: 5000 });
            }
          }
        } else {
          // wait until the actor has finished (it steps aside), then take items out
          const stop = startWatching(bot, () => {
            const p = otherPos();
            return p ? p.offset(0, 1.2, 0) : chestBlockPos.offset(0.5, 0.5, 0.5);
          });
          const until = Date.now() + 12000;
          await sleep(3000);
          while (Date.now() < until) {
            const p = otherPos();
            const b = bot.blockAt(chestBlockPos);
            if (b && b.name.includes("chest") && p && p.distanceTo(chestBlockPos) > 3.5) break;
            await sleep(250);
          }
          await stop();
          const b = bot.blockAt(chestBlockPos);
          const withdrawn = [];
          if (b && b.name.includes("chest")) {
            await walkNear(bot, chestBlockPos.offset(0.5, 0, 0.5), 2.2, 4000);
            const win = await withTimeout(bot.openContainer(b), 4000, null, `${bot.username} open chest`);
            if (win) {
              const inside = win.containerItems();
              for (const it of inside.slice(0, 2)) {
                const n = Math.max(1, Math.floor(it.count / 2));
                const r = await withTimeout(win.withdraw(it.type, null, n), 3000, "timeout");
                if (r !== "timeout") withdrawn.push([it.name, n]);
                await sleep(localJitterMs(300, 600));
              }
              try {
                win.close();
              } catch (_) {}
            }
          }
          await note(rcon, args, bot, episodeNum, "chest_withdraw", { withdrawn });
          const back = frame.at(-2, 0);
          await walkNear(bot, new Vec3(back.x + 0.5, L, back.z + 0.5), 1.2, 3000);
          await watchOther(plan.breakChest ? localJitterMs(4000, 5000) : localJitterMs(800, 1200));
        }
      },
      trade: async () => {
        const trader = Object.values(bot.entities).find(
          (e) => e && (e.name === "villager" || e.name === "wandering_trader") && e.position.distanceTo(bot.entity.position) < 20,
        );
        if (!trader) {
          await note(rcon, args, bot, episodeNum, "trade", { ok: false, reason: "no_trader" });
          await watchOther(2000);
          return;
        }
        // actor trades first, the observer watches, then the other way round
        const doTrade = async () => {
          await walkNear(bot, trader.position, 2.0, 4000);
          await turnTo(bot, trader.position.offset(0, 1.5, 0), 500, 14);
          // Mineflayer's openVillager asserts the villager entity type; a
          // wandering trader opens the same merchant window, so pass a view of
          // the entity with the villager type id.
          const vid = bot.registry.entitiesByName.villager.id;
          const target = trader.entityType === vid ? trader : Object.assign(Object.create(trader), { entityType: vid });
          let win = null;
          for (let attempt = 0; attempt < 2 && !win; attempt++) {
            bot.emit("action_use");
            win = await withTimeout(bot.openVillager(target), 4000, null, `${bot.username} open ${trader.name}`);
            if (!win) {
              await sleep(800);
              await walkNear(bot, trader.position, 1.8, 2000);
            }
          }
          if (!win) return { ok: false, reason: "open_failed" };
          let done = 0;
          try {
            const trades = win.trades || [];
            for (let i = 0; i < Math.min(2, trades.length); i++) {
              const t = trades[i];
              if (!t || t.tradeDisabled) continue;
              if (countItem(bot, "emerald") < (t.inputItem1 ? t.inputItem1.count : 1)) continue;
              const r = await withTimeout(bot.trade(win, i, 1), 4000, "timeout");
              if (r !== "timeout") done++;
              await sleep(localJitterMs(400, 800));
            }
          } catch (err) {
            console.log(`[${bot.username}] trade failed: ${err.message}`);
          }
          try {
            win.close();
          } catch (_) {}
          return { ok: done > 0, trades: done };
        };
        if (isActor) {
          const r = await doTrade();
          await note(rcon, args, bot, episodeNum, "trade", { trader: trader.name, ...r });
          await glanceAtOther(bot, other, localJitterMs(800, 1200));
          await watchOther(localJitterMs(5000, 6500));
        } else {
          const stop = startWatching(bot, () => {
            const p = otherPos();
            return Math.random() < 0.5 && p ? p.offset(0, 1.4, 0) : trader.position.offset(0, 1.5, 0);
          });
          await sleep(localJitterMs(5500, 7000));
          await stop();
          const r = await doTrade();
          await note(rcon, args, bot, episodeNum, "trade", { trader: trader.name, ...r });
          await glanceAtOther(bot, other, localJitterMs(800, 1200));
        }
      },
    };

    const steps = [readyStep("ready", true)];
    plan.order.forEach((kind, i) => {
      steps.push({
        name: `${i}_${kind}`,
        run: async function () {
          try {
            await runners[kind]();
          } catch (err) {
            console.log(`[${bot.username}] ${kind} failed: ${err.stack || err}`);
          } finally {
            bot.clearControlStates();
          }
          return {};
        },
      });
    });
    steps.push(outroStep());
    return steps;
  }
}

module.exports = { ItemExchangeEpisode };
