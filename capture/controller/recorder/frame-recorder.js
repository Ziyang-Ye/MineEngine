/**
 * frame-recorder.js - Per-frame action/state recorder (controller side).
 *
 * Drop-in replacement for `prismarine-viewer-colalab/lib/headless-base64.js`
 * (commit 4ed884d) when viewer rendering is disabled. That package needs the
 * native `gl`/`canvas` modules, which are not built on Windows, so it must not
 * be required on the disabled path.
 *
 * Behaviour reproduced from headless-base64 (disableRendering = true):
 *   - one TCP connection to the act_recorder per episode, opened on
 *     `startepisode(episodeNum)`: header = uint32LE length + JSON {"episode": N};
 *   - every `interval` ms (setTimeout chain, re-armed after each sample) one
 *     sample: uint32LE length + JSON (same fields, same order);
 *   - samples are taken all the time but only written while connected;
 *   - `endepisode` sends a zero length, ends the socket and emits
 *     `episodeended` once the socket is closed;
 *   - `action_*` events set one-shot flags that are read-and-reset by the next
 *     sample, except `mine` which is set by `action_mine_start` and cleared by
 *     `action_mine_finish` (read without reset).
 *
 * Differences (all invisible to act_recorder for ASCII payloads):
 *   - the length prefix is the UTF-8 byte length (headless-base64 used the
 *     UTF-16 string length, which corrupts the stream for non-ASCII JSON);
 *   - errors inside a sample never stop the timer chain (headless-base64
 *     could throw from its catch block before a socket existed);
 *   - cadence: by default on Windows the samples are scheduled on a deadline
 *     grid (sample k at t0 + k*interval). Windows timers have ~15.6 ms
 *     granularity, so headless-base64's "re-arm interval ms after the
 *     sample" gives 62.5 ms (16 Hz) there instead of ~51 ms. Elsewhere the
 *     headless-base64 cadence ("after") is kept unless env RECORDER_CADENCE
 *     is set (windows/run_windows.py sets "deadline" on Linux). Option
 *     `cadence` overrides both;
 *   - New (T16): two keys are appended to every sample: `state` (own player
 *     state, crosshair target) and `peer` (the other bot as seen by this
 *     client). See windows/schema_parts/controller.md. Building them never
 *     throws: any failing part is null.
 *   - New (T12): with the camera pose link (env CAMERA_POSE_PORT) every sample
 *     also has `pose_seq`, the seq of the last movement packet sent to the
 *     server and forwarded to the camera (see camera-pose-link.js).
 *
 * When rendering is enabled the call is delegated unchanged to
 * `require("prismarine-viewer-colalab").headless` (lazy require), so the
 * Docker path with rendering keeps working (without the T16 keys).
 */

const net = require("net");
const { performance } = require("perf_hooks");
const { Vec3 } = require("vec3");

const { startCameraPoseLink } = require("./camera-pose-link");

/**
 * Default sample cadence: env RECORDER_CADENCE ("deadline" | "after") when set
 * (the native runner sets "deadline" on Linux, so Linux matches Windows), else
 * "deadline" on Windows and "after" elsewhere (Docker path, unchanged).
 * @returns {"after"|"deadline"}
 */
function defaultCadence() {
  const env = String(process.env.RECORDER_CADENCE || "").toLowerCase();
  if (env === "deadline" || env === "after") return env;
  return process.platform === "win32" ? "deadline" : "after";
}

// Crosshair reach used for `state.cursor` (blocks, from the eye).
const CURSOR_MAX_DISTANCE = 5;
// bot.oxygenLevel of a player with full air (300 ticks / 15).
const FULL_OXYGEN = 20;

// prismarine-world BlockFace -> face name (the face the ray entered through).
const FACE_NAMES = {
  0: "down",
  1: "up",
  2: "north",
  3: "south",
  4: "west",
  5: "east",
};

// Entities the vanilla crosshair cannot target (not "pickable").
const NON_PICKABLE_ENTITY_NAMES = new Set([
  "item",
  "experience_orb",
  "falling_block",
  "area_effect_cloud",
  "marker",
  "lightning_bolt",
  "arrow",
  "spectral_arrow",
  "trident",
  "snowball",
  "egg",
  "ender_pearl",
  "potion",
  "experience_bottle",
  "fishing_bobber",
  "firework_rocket",
  "item_display",
  "block_display",
  "text_display",
  "interaction",
]);

// Blocks the vanilla crosshair passes through (no outline shape). Fluids are
// ignored like in ClipContext.Fluid.NONE.
const NO_OUTLINE_BLOCKS = new Set([
  "air",
  "cave_air",
  "void_air",
  "water",
  "lava",
  "bubble_column",
  "light",
  "moving_piston",
]);

// Outline boxes ([x0,y0,z0,x1,y1,z1], block units) of common blocks without
// a collision shape. Other non-colliding blocks (wall torches, crops,
// saplings, signs, ...) are approximated by the full cube.
const px = (...v) => v.map((x) => x / 16);
const FLAT_BOX = [px(0, 0, 0, 16, 1, 16)];
const PLATE_BOX = [px(1, 0, 1, 15, 1, 15)];
const TORCH_BOX = [px(6, 0, 6, 10, 10, 10)];
const GRASS_BOX = [px(2, 0, 2, 14, 13, 14)];
const FLOWER_BOX = [px(5, 0, 5, 11, 10, 11)];
const FULL_BOX = [[0, 0, 0, 1, 1, 1]];
const FLOWERS = new Set([
  "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "red_tulip",
  "orange_tulip", "white_tulip", "pink_tulip", "oxeye_daisy", "cornflower",
  "lily_of_the_valley", "wither_rose", "torchflower",
]);

/**
 * Shapes the crosshair ray is tested against: the collision shapes when the
 * block has any, otherwise an approximate outline (vanilla targets outline
 * shapes, so grass, flowers, torches, rails, ... are hit too), or null if
 * the block cannot be targeted at all.
 * @param {*} block - prismarine-block
 * @returns {Array|null}
 */
function targetShapes(block) {
  if (!block || NO_OUTLINE_BLOCKS.has(block.name)) return null;
  if (Array.isArray(block.shapes) && block.shapes.length > 0) return block.shapes;
  const n = block.name || "";
  if (n === "rail" || n.endsWith("_rail") || n === "redstone_wire" || n === "tripwire") return FLAT_BOX;
  if (n.endsWith("_pressure_plate")) return PLATE_BOX;
  if (n === "snow") return [px(0, 0, 0, 16, 2, 16)]; // layers=1 (no collision)
  if (n === "torch" || n === "soul_torch" || n === "redstone_torch") return TORCH_BOX;
  if (n === "short_grass" || n === "grass" || n === "fern" || n === "dead_bush") return GRASS_BOX;
  if (FLOWERS.has(n)) return FLOWER_BOX;
  return FULL_BOX;
}

// Armor slots of the player inventory window.
const ARMOR_SLOTS = { head: 5, chest: 6, legs: 7, feet: 8 };
const OFFHAND_SLOT = 45;
const HOTBAR_FIRST_SLOT = 36;

/**
 * Round a finite number, pass anything else through as null.
 * @param {*} v
 * @param {number} dp
 * @returns {number|null}
 */
function r(v, dp) {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  const f = 10 ** dp;
  return Math.round(v * f) / f;
}

/**
 * Run fn and return its value, or null if it throws.
 * @param {Function} fn
 * @returns {*}
 */
function safe(fn) {
  try {
    const v = fn();
    return v === undefined ? null : v;
  } catch (err) {
    return null;
  }
}

/**
 * Namespaced id for a registry name ("stone" -> "minecraft:stone").
 * @param {string|null|undefined} name
 * @returns {string|null}
 */
function nsId(name) {
  if (!name) return null;
  const s = String(name);
  return s.includes(":") ? s : `minecraft:${s}`;
}

/**
 * CamelCase effect name from minecraft-data ("InstantHealth") -> snake_case.
 * @param {string} name
 * @returns {string}
 */
function snakeCase(name) {
  return String(name)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase();
}

/**
 * {id, count} of an inventory item, or null for an empty slot.
 * @param {*} item - prismarine-item or null
 * @returns {{id: string, count: number}|null}
 */
function itemRef(item) {
  if (!item) return null;
  return { id: nsId(item.name), count: item.count };
}

/**
 * Block state string in the same format as the world log
 * (`minecraft:oak_stairs[facing=east,half=bottom,...]`, properties sorted by
 * name like Minecraft's StateDefinition).
 * @param {*} block - prismarine-block
 * @returns {string|null}
 */
function blockStateString(block) {
  const id = nsId(block.name);
  const props = safe(() => block.getProperties());
  if (!props || typeof props !== "object") return id;
  const keys = Object.keys(props).sort();
  if (keys.length === 0) return id;
  return `${id}[${keys.map((k) => `${k}=${props[k]}`).join(",")}]`;
}

/**
 * Create the per-bot T16 state builder. Listeners it needs (abilities packet
 * for the flying flag, minecraft-data lookup) are installed once.
 * @param {*} bot - Mineflayer bot
 * @param {Object} opts
 * @param {string|null} opts.peerName - username of the other bot
 * @returns {{buildState: Function, buildPeer: Function}}
 */
function createStateBuilder(bot, { peerName = null } = {}) {
  let abilitiesFlying = false;
  try {
    if (bot._client && typeof bot._client.on === "function") {
      bot._client.on("abilities", (packet) => {
        // Abilities flags: 0x01 invulnerable, 0x02 flying, 0x04 allow flight, 0x08 creative.
        abilitiesFlying = Boolean(packet && packet.flags & 0x02);
      });
    }
  } catch (err) {
    // ignore: flying falls back to elytraFlying
  }

  let effectNames = null;
  function effectId(id) {
    if (effectNames === null) {
      effectNames = {};
      try {
        const mcData = bot.registry || require("minecraft-data")(bot.version);
        for (const e of Object.values(mcData.effects || {})) {
          effectNames[e.id] = `minecraft:${snakeCase(e.name)}`;
        }
      } catch (err) {
        // keep the empty map: ids are reported numerically
      }
    }
    return effectNames[id] || id;
  }

  let iterators = null;
  function getIterators() {
    if (iterators === null) {
      iterators = safe(() => require("prismarine-world").iterators) || false;
    }
    return iterators || null;
  }

  /**
   * Eye position and view direction of the rendered camera.
   * @param {Array} absolute - [yaw, pitch] last sent to the server (may hold nulls)
   */
  function viewRay(absolute) {
    const ent = bot.entity;
    const yaw = absolute && absolute[0] != null ? absolute[0] : ent.yaw;
    const pitch = absolute && absolute[1] != null ? absolute[1] : ent.pitch;
    const eyeHeight = typeof ent.eyeHeight === "number" ? ent.eyeHeight : 1.62;
    const eye = ent.position.offset(0, eyeHeight, 0);
    const cp = Math.cos(pitch);
    const dir = new Vec3(-Math.sin(yaw) * cp, Math.sin(pitch), -Math.cos(yaw) * cp);
    return { eye, dir };
  }

  function cursorBlock(ray) {
    // Like bot.blockAtCursor(5) (prismarine-world raycast) but from the real
    // eye height and the rendered (last sent) look, and also hitting blocks
    // without a collision shape (see targetShapes). blockAtCursor uses
    // entity.height (1.8), returns null whenever yaw or pitch is exactly 0
    // and only tests collision shapes.
    const it = getIterators();
    if (!it || !it.RaycastIterator) return null;
    const iter = new it.RaycastIterator(ray.eye, ray.dir, CURSOR_MAX_DISTANCE);
    let pos = ray.eye;
    while (pos) {
      const position = new Vec3(Math.floor(pos.x), Math.floor(pos.y), Math.floor(pos.z));
      const block = bot.world.getBlock(position);
      const shapes = targetShapes(block);
      if (shapes) {
        const hit = iter.intersect(shapes, position);
        if (hit) {
          const distance = ray.eye.distanceTo(hit.pos);
          if (distance > CURSOR_MAX_DISTANCE) return null;
          return {
            pos: [position.x, position.y, position.z],
            face: FACE_NAMES[hit.face] ?? null,
            name: nsId(block.name),
            state: blockStateString(block),
            distance: r(distance, 3),
          };
        }
      }
      pos = iter.next();
    }
    return null;
  }

  function isPickable(entity) {
    if (!entity || entity === bot.entity || !entity.position) return false;
    if (entity.username && entity.username === bot.username) return false;
    if (entity.type === "object" || entity.type === "orb" || entity.type === "projectile") return false;
    if (NON_PICKABLE_ENTITY_NAMES.has(entity.name)) return false;
    if (entity.type === "player") {
      // Camera / spectator clients are hidden from the bots, but never let
      // them shadow the crosshair if they ever become visible.
      const u = entity.username || "";
      if (u.startsWith("Camera")) return false;
      const info = bot.players && bot.players[u];
      if (info && info.gamemode === 3) return false;
    }
    return true;
  }

  function cursorEntity(ray, maxDistance) {
    // Same algorithm as bot.entityAtCursor(5), from the real eye position.
    const it = getIterators();
    if (!it || !it.RaycastIterator) return null;
    const iterator = new it.RaycastIterator(ray.eye, ray.dir, maxDistance);
    let best = null;
    let bestDist = maxDistance;
    for (const entity of Object.values(bot.entities || {})) {
      if (!isPickable(entity)) continue;
      if (entity.position.distanceTo(ray.eye) > maxDistance + 3) continue;
      const w = (entity.width || 0.6) / 2;
      const h = entity.height || 1.8;
      const hit = iterator.intersect([[-w, 0, -w, w, h, w]], entity.position);
      if (!hit) continue;
      if (hit.pos.minus(ray.eye).dot(ray.dir) < 0) continue; // behind the eye
      const d = ray.eye.distanceTo(hit.pos);
      if (d < bestDist) {
        best = entity;
        bestDist = d;
      }
    }
    if (!best) return null;
    return {
      uuid: best.uuid || null,
      id: nsId(best.name),
      entity_id: best.id ?? null,
      type: best.type || null,
      name: best.username || best.name || null,
      distance: r(bestDist, 3),
    };
  }

  function cursor(absolute) {
    const ray = safe(() => viewRay(absolute));
    if (!ray) return { block: null, entity: null };
    const block = safe(() => cursorBlock(ray));
    const maxEntityDistance =
      block && block.distance != null ? block.distance : CURSOR_MAX_DISTANCE;
    const entity = safe(() => cursorEntity(ray, maxEntityDistance));
    return { block, entity };
  }

  function effects() {
    const list = [];
    for (const e of Object.values(bot.entity.effects || {})) {
      if (!e) continue;
      list.push({ id: effectId(e.id), amplifier: e.amplifier, duration: e.duration });
    }
    return list;
  }

  function hotbar() {
    const slots = bot.inventory.slots;
    const out = [];
    for (let i = 0; i < 9; i++) {
      const it = slots[HOTBAR_FIRST_SLOT + i];
      out.push({ slot: i, id: it ? nsId(it.name) : null, count: it ? it.count : 0 });
    }
    return out;
  }

  function armor() {
    const slots = bot.inventory.slots;
    const out = {};
    for (const [k, s] of Object.entries(ARMOR_SLOTS)) out[k] = itemRef(slots[s]);
    return out;
  }

  function oxygen() {
    if (typeof bot.oxygenLevel === "number") return bot.oxygenLevel;
    // Fallback: raw air supply (ticks, max 300) from the entity metadata.
    const air = bot.entity.metadata && bot.entity.metadata[1];
    if (typeof air === "number") return Math.round(air / 15);
    // Mineflayer only sets oxygenLevel on an air_supply metadata update, which
    // the server sends only once the air changes: until then it is full.
    return FULL_OXYGEN;
  }

  /**
   * Own player state (T16). `absolute` = cameraAction.absolute of this sample.
   * @param {Array|null} absolute
   * @returns {Object}
   */
  function buildState(absolute, cursorAtAction = null) {
    const ent = bot.entity || {};
    const yaw = absolute && absolute[0] != null ? absolute[0] : ent.yaw;
    return {
      health: safe(() => bot.health),
      food: safe(() => bot.food),
      saturation: safe(() => bot.foodSaturation),
      xp_level: safe(() => bot.experience.level),
      xp_progress: safe(() => r(bot.experience.progress, 4)),
      oxygen: safe(oxygen),
      effects: safe(effects),
      hotbar: safe(hotbar),
      selected_slot: safe(() => bot.quickBarSlot),
      offhand: safe(() => itemRef(bot.inventory.slots[OFFHAND_SLOT])),
      armor: safe(armor),
      on_ground: safe(() => Boolean(ent.onGround)),
      sneaking: safe(() => Boolean(bot.getControlState("sneak") || ent.crouching)),
      sprinting: safe(() => Boolean(bot.getControlState("sprint"))),
      flying: safe(() => Boolean(abilitiesFlying || ent.elytraFlying)),
      in_water: safe(() => Boolean(ent.isInWater)),
      alive: safe(() => bot.health > 0 && bot.isAlive !== false),
      head_yaw: safe(() => r(yaw, 5)),
      body_yaw: null,
      cursor: safe(() => cursor(absolute)),
      cursor_at_action: cursorAtAction,
    };
  }

  /**
   * The other bot as seen by this client (T16). Mineflayer conventions.
   * @returns {Object|null}
   */
  function buildPeer() {
    if (!peerName) return null;
    const info = bot.players && bot.players[peerName];
    const ent = info && info.entity;
    if (!ent || !ent.position) {
      return { name: peerName, known: false, pos: null, yaw: null, pitch: null, head_yaw: null, distance: null };
    }
    const p = ent.position;
    return {
      name: peerName,
      known: true,
      pos: [r(p.x, 4), r(p.y, 4), r(p.z, 4)],
      yaw: r(ent.yaw, 5),
      pitch: r(ent.pitch, 5),
      head_yaw: r(ent.headYaw, 5),
      distance: safe(() => r(bot.entity.position.distanceTo(p), 3)),
    };
  }

  return {
    buildState: (absolute, cursorAtAction = null) => safe(() => buildState(absolute, cursorAtAction)),
    buildPeer: () => safe(buildPeer),
    // Crosshair target right now, from the bot's current look (entity
    // yaw/pitch; equal to the sent look once a lookAt has completed).
    // getLastCameraAction() is not used: it resets the camera delta.
    cursorNow: () => safe(() => cursor(null)),
  };
}

/**
 * Start the per-frame recorder for a bot (call once per process).
 *
 * @param {*} bot - Mineflayer bot (georgysavva fork: getLastCameraAction, getLastPhysicsFrameTime)
 * @param {Object} options
 * @param {string} [options.output="output.mp4"] - "host:port" of the act_recorder
 * @param {number} [options.interval=16] - ms between samples (setTimeout chain)
 * @param {boolean|number} [options.disableRendering=false] - true: no images (this module);
 *   false: delegate to prismarine-viewer-colalab's headless renderer
 * @param {string|null} [options.peerName=null] - other bot's username for `peer`
 * @param {boolean} [options.extraState=true] - append the T16 `state`/`peer` keys
 * @param {"after"|"deadline"} [options.cadence] - "after": next sample `interval` ms
 *   after the previous one finished (headless-base64); "deadline": mean period of
 *   exactly `interval` ms. Default: env RECORDER_CADENCE, else "deadline" on Windows,
 *   "after" elsewhere
 * @returns {void}
 */
function startFrameRecorder(bot, options = {}) {
  const {
    output = "output.mp4",
    interval = 16,
    disableRendering = false,
    peerName = null,
    extraState = true,
    cadence = defaultCadence(),
  } = options;
  const renderingDisabled = Boolean(Number(disableRendering));
  // T12: forward the bot's movement packets to its camera client (no-op unless
  // CAMERA_POSE_PORT is set; see camera-pose-link.js).
  const poseLink = startCameraPoseLink(bot);
  if (!renderingDisabled) {
    // Rendering needs the full viewer (native gl/canvas); only load it here.
    const headless = require("prismarine-viewer-colalab").headless;
    const { peerName: _p, extraState: _e, cadence: _c, ...viewerOptions } = options;
    return headless(bot, { ...viewerOptions, disableRendering: false });
  }

  console.log("disableRendering", disableRendering);
  // Instance-level flags for tracking bot actions
  const actionFlags = {
    attack: false,
    use: false,
    mount: false,
    dismount: false,
    place_block: false,
    place_entity: false,
    mine: false,
    "hotbar.1": false,
    "hotbar.2": false,
    "hotbar.3": false,
    "hotbar.4": false,
    "hotbar.5": false,
    "hotbar.6": false,
    "hotbar.7": false,
    "hotbar.8": false,
    "hotbar.9": false,
  };

  // Getter function to read a flag value
  function getActionFlag(flagName) {
    return actionFlags[flagName] || false;
  }

  // Getter function to read and reset a flag to false
  function getAndResetActionFlag(flagName) {
    const value = actionFlags[flagName] || false;
    actionFlags[flagName] = false;
    return value;
  }

  const stateBuilder = extraState ? createStateBuilder(bot, { peerName }) : null;

  // Crosshair target latched at the moment of an attack/use/place_block
  // action (first one per sample), reported in `state.cursor_at_action`.
  // The sample itself is taken up to `interval` ms later, and for
  // place_block Mineflayer only emits the event once the server confirmed
  // the new block, when the crosshair already sees that block.
  const ACTION_CURSOR_KEYS = ["attack", "use", "place_block"];
  const actionCursors = { attack: null, use: null, place_block: null };
  let placePacketCursor = null; // crosshair when the last block_place packet was sent
  function latchActionCursor(key, value) {
    if (!stateBuilder || actionCursors[key] !== null) return;
    actionCursors[key] = value !== undefined ? value : stateBuilder.cursorNow();
  }
  function takeActionCursors() {
    if (!stateBuilder) return null;
    let any = false;
    const out = {};
    for (const k of ACTION_CURSOR_KEYS) {
      out[k] = actionCursors[k];
      if (out[k] !== null) any = true;
      actionCursors[k] = null;
    }
    return any ? out : null;
  }
  if (stateBuilder && typeof bot._genericPlace === "function") {
    // placeBlock -> _genericPlace looks at the reference block and writes the
    // block_place packet; capture the crosshair right then (the server's
    // block update cannot have been processed yet).
    const genericPlace = bot._genericPlace;
    bot._genericPlace = async function (...a) {
      const res = await genericPlace.apply(this, a);
      placePacketCursor = stateBuilder.cursorNow();
      return res;
    };
  }

  let client = null;
  let ended = true; // Initially true - episode hasn't started
  let clientConnected = false;
  const [host, port] = String(output).split(":");

  function socketState() {
    return client
      ? {
          writable: client.writable,
          destroyed: client.destroyed,
          connecting: client.connecting,
          bytesWritten: client.bytesWritten,
          bytesRead: client.bytesRead,
        }
      : null;
  }

  /**
   * uint32LE byte length + UTF-8 payload.
   * @param {string} text
   * @returns {Buffer}
   */
  function frameMessage(text) {
    const body = Buffer.from(text, "utf8");
    const head = Buffer.alloc(4);
    head.writeUInt32LE(body.length, 0);
    return Buffer.concat([head, body]);
  }

  function writeAsync(socket, data) {
    return new Promise((resolve, reject) => {
      const canWrite = socket.write(data, (err) => (err ? reject(err) : resolve()));
      if (!canWrite) {
        socket.once("drain", resolve);
      }
    });
  }

  function connectClient(episodeNum) {
    client = new net.Socket();
    const socket = client;
    socket.on("error", (err) => {
      console.error("[recorder] Socket error:", err);
    });
    socket.on("close", () => {
      console.log("[recorder] Socket closed");
    });
    socket.on("end", () => {
      console.log("[recorder] Socket ended");
      console.log("[recorder] Socket state at end:", socketState());
    });
    socket.connect(parseInt(port, 10), host, async () => {
      console.log("[recorder] Connected to server");
      // Send a one-time header with just the episode number when we first connect
      try {
        await writeAsync(socket, frameMessage(JSON.stringify({ episode: episodeNum })));
      } catch (e) {
        console.error("[recorder] Failed to send episode header:", e);
      }
      if (!ended) {
        clientConnected = true;
      }
    });
  }

  async function update() {
    try {
      // Get camera action once - contains both delta and absolute values
      const cameraAction = bot.getLastCameraAction();
      const pos = {
        x: bot.entity.position.x,
        y: bot.entity.position.y,
        z: bot.entity.position.z,
        yaw: cameraAction.absolute[0], // may be null in the first tick(s)
        pitch: cameraAction.absolute[1], // may be null in the first tick(s)
        action: {
          forward: bot.getControlState("forward"),
          back: bot.getControlState("back"),
          left: bot.getControlState("left"),
          right: bot.getControlState("right"),
          jump: bot.getControlState("jump"),
          sprint: bot.getControlState("sprint"),
          sneak: bot.getControlState("sneak"),
          camera: cameraAction.delta,
          // Action flags - use getAndResetActionFlag for all except mine
          attack: getAndResetActionFlag("attack"),
          use: getAndResetActionFlag("use"),
          mount: getAndResetActionFlag("mount"),
          dismount: getAndResetActionFlag("dismount"),
          place_block: getAndResetActionFlag("place_block"),
          place_entity: getAndResetActionFlag("place_entity"),
          // Use getActionFlag for mine (just read, don't reset)
          mine: getActionFlag("mine"),
          "hotbar.1": getAndResetActionFlag("hotbar.1"),
          "hotbar.2": getAndResetActionFlag("hotbar.2"),
          "hotbar.3": getAndResetActionFlag("hotbar.3"),
          "hotbar.4": getAndResetActionFlag("hotbar.4"),
          "hotbar.5": getAndResetActionFlag("hotbar.5"),
          "hotbar.6": getAndResetActionFlag("hotbar.6"),
          "hotbar.7": getAndResetActionFlag("hotbar.7"),
          "hotbar.8": getAndResetActionFlag("hotbar.8"),
          "hotbar.9": getAndResetActionFlag("hotbar.9"),
        },
        inventory: (() => {
          const allItems = bot.inventory.items();
          const mainInventory = allItems.filter((item) => item.slot >= 9 && item.slot <= 35);
          const hotbar = allItems.filter((item) => item.slot >= 36 && item.slot <= 44);
          const heldItemIndex = bot.quickBarSlot + 36;
          const heldItem = bot.inventory.slots[heldItemIndex] || null;
          const offHandItem = bot.inventory.slots[45] || null;
          return {
            quickBarSlot: bot.quickBarSlot,
            mainInventory,
            hotbar,
            heldItem,
            offHandItem,
          };
        })(),
        renderTime: Date.now(),
        physicsTime: bot.getLastPhysicsFrameTime(),
        worldAgeTicks: bot.time.age,
        isInWater: bot.entity.isInWater,
      };
      if (poseLink) {
        // seq of the last movement packet forwarded to the camera (its yaw/pitch
        // = cameraAction.absolute); the camera sidecar has the rendered pose_seq
        pos.pose_seq = poseLink.lastSeq();
      }
      if (stateBuilder) {
        pos.state = stateBuilder.buildState(cameraAction.absolute, takeActionCursors());
        pos.peer = stateBuilder.buildPeer();
      }

      // Only skip writing to connection, not the processing above
      if (clientConnected) {
        await writeAsync(client, frameMessage(JSON.stringify(pos)));
      }
    } catch (err) {
      console.error("[recorder] Error in update:", err);
      console.error("[recorder] Error details:", {
        code: err && err.code,
        message: err && err.message,
        stack: err && err.stack,
      });
      console.error("[recorder] Socket state at error:", socketState());
    }

    scheduleNext();
  }

  let nextDeadline = null;
  function scheduleNext() {
    if (cadence !== "deadline") {
      setTimeout(update, interval);
      return;
    }
    const now = performance.now();
    nextDeadline = nextDeadline === null ? now + interval : nextDeadline + interval;
    // After a long stall (GC, blocked event loop) re-anchor instead of bursting.
    if (nextDeadline < now - 2 * interval) nextDeadline = now + interval;
    setTimeout(update, Math.max(0, nextDeadline - now));
  }

  function sendEndSignal() {
    if (!clientConnected) {
      console.error("[recorder] no connection to send end signal");
      return;
    }
    console.log("[recorder] sending zero end of stream");
    clientConnected = false;
    const socket = client;
    socket.write(Buffer.alloc(4)); // uint32LE 0 = end of episode
    socket.end();

    // Fire episodeended event when connection is actually closed
    socket.once("close", () => {
      console.log("[recorder] connection closed, firing episodeended event");
      ended = true;
      bot.emit("episodeended");
    });
  }

  bot.on("startepisode", (episodeNum) => {
    if (ended) {
      // Only start if episode has ended
      console.log(
        `[recorder] starting episode - connecting to receiver, episode number ${episodeNum}`,
      );
      ended = false;
      if (client && !client.destroyed) {
        console.error("[recorder] episode already in progress");
      } else {
        console.log("[recorder] creating new connection");
        connectClient(episodeNum);
      }
    } else {
      console.log("[recorder] ignoring startepisode - episode already in progress");
    }
  });

  bot.on("endepisode", () => {
    console.log("[recorder] ending episode");
    sendEndSignal();
  });

  // Subscribe to action events and manage flags
  bot.on("action_attack", () => {
    actionFlags.attack = true;
    latchActionCursor("attack");
  });
  bot.on("action_use", () => {
    actionFlags.use = true;
    latchActionCursor("use");
  });
  bot.on("action_mount", () => {
    actionFlags.mount = true;
  });
  bot.on("action_dismount", () => {
    actionFlags.dismount = true;
  });
  bot.on("action_place_block", () => {
    actionFlags.place_block = true;
    latchActionCursor("place_block", placePacketCursor !== null ? placePacketCursor : undefined);
    placePacketCursor = null;
  });
  bot.on("action_place_entity", () => {
    actionFlags.place_entity = true;
  });
  bot.on("action_hotbar_change", (key) => {
    actionFlags[`hotbar.${key}`] = true;
  });
  bot.on("action_mine_start", () => {
    actionFlags.mine = true;
  });
  bot.on("action_mine_finish", () => {
    actionFlags.mine = false;
  });

  setTimeout(update, interval);
}

module.exports = {
  startFrameRecorder,
  // exported for tests
  createStateBuilder,
  blockStateString,
};
