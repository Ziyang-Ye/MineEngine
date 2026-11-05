/**
 * test-frame-recorder.js - Self-contained test of recorder/frame-recorder.js.
 *
 *   node controller/recorder/test-frame-recorder.js
 *        [--reference <path to prismarine-viewer-colalab/lib/headless-base64.js>]
 *        [--python <python executable>] [--cadence after|deadline] [--keep]
 *
 * Drives fake Mineflayer bots through two episodes and checks:
 *   1. the wire protocol (header, uint32LE-length JSON samples, zero end
 *      marker, `episodeended` after close) against a local TCP receiver;
 *   2. if the reference headless-base64.js is found (default:
 *      node_modules/prismarine-viewer-colalab/lib/headless-base64.js, or env
 *      REF_HEADLESS_BASE64), that it is run side by side with stubbed
 *      rendering deps and that our samples have exactly its fields, key order,
 *      values and action-flag semantics, plus the new `state`/`peer` keys;
 *   3. if a Python is found (default: repo .venv), that the real
 *      controller/act_recorder/act_recorder.py accepts the stream and writes
 *      the per-episode JSON with `state`/`peer` passed through unchanged.
 * Exit code 0 = all checks passed.
 */

const assert = require("assert");
const { EventEmitter } = require("events");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const minimist = require("minimist");
const { Vec3 } = require("vec3");

const { startFrameRecorder, createStateBuilder, blockStateString } = require("./frame-recorder");

const REPO = path.resolve(__dirname, "..", "..");
const VERSION = "1.21";
const mcData = require("minecraft-data")(VERSION);
const Item = require("prismarine-item")(VERSION);
const Block = require("prismarine-block")(VERSION);
const Chunk = require("prismarine-chunk")(VERSION);
const World = require("prismarine-world")(VERSION);

const INTERVAL = 50;
const argv = minimist(process.argv.slice(2), { string: ["reference", "python", "cadence"], boolean: ["keep"] });
const CADENCE = argv.cadence || process.env.RECORDER_CADENCE || (process.platform === "win32" ? "deadline" : "after");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Child processes / temp dirs to clean up on exit (success or failure).
const cleanup = { py: null, dir: null };
let checks = 0;
function check(cond, msg) {
  assert.ok(cond, msg);
  checks++;
}

// ---------------------------------------------------------------------------
// Fake world / bot
// ---------------------------------------------------------------------------

async function makeWorld() {
  // Flat stone layer at y=64 around the origin.
  const stone = mcData.blocksByName.stone.defaultState;
  const gen = () => {
    const c = new Chunk({ minY: -64, worldHeight: 384 });
    for (let x = 0; x < 16; x++) for (let z = 0; z < 16; z++) c.setBlockStateId(new Vec3(x, 64, z), stone);
    return c;
  };
  const world = new World(gen);
  for (const [cx, cz] of [[0, 0], [0, -1], [-1, 0], [-1, -1]]) await world.getColumn(cx, cz);
  return world.sync;
}

function item(name, count, slot) {
  const it = new Item(mcData.itemsByName[name].id, count);
  it.slot = slot;
  return it;
}

/**
 * A minimal Mineflayer bot with deterministic state. `S` is shared between
 * the bots of one test so all recorders observe the same values.
 */
function makeFakeBot(worldSync, S) {
  const bot = new EventEmitter();
  bot.username = "Alpha";
  bot.version = VERSION;
  bot.registry = mcData;
  bot._client = new EventEmitter();
  bot.world = worldSync;
  const slots = new Array(46).fill(null);
  slots[36] = item("diamond_sword", 1, 36);
  slots[37] = item("stone", 64, 37);
  slots[12] = item("oak_planks", 32, 12);
  slots[5] = item("iron_helmet", 1, 5);
  slots[45] = item("shield", 1, 45);
  bot.inventory = {
    slots,
    items: () => slots.slice(9, 45).filter(Boolean),
  };
  bot.quickBarSlot = 0;
  bot.entity = {
    id: 1,
    type: "player",
    name: "player",
    username: "Alpha",
    position: new Vec3(0.5, 65, 0.5),
    get yaw() { return S.yaw; },
    get pitch() { return S.pitch; },
    height: 1.8,
    width: 0.6,
    eyeHeight: 1.62,
    onGround: true,
    isInWater: false,
    crouching: false,
    effects: S.effects,
    metadata: {},
  };
  bot.getControlState = (k) => Boolean(S.controls[k]);
  let calls = 0;
  bot.getLastCameraAction = () => {
    calls++;
    // Mineflayer returns nulls until the first look was sent.
    if (calls <= 2) return { delta: [0, 0], absolute: [null, null] };
    return { delta: [0, 0], absolute: [S.yaw, S.pitch] };
  };
  bot.getLastPhysicsFrameTime = () => 1234.5;
  bot.time = { age: 4242 };
  bot.health = 17;
  bot.food = 15;
  bot.foodSaturation = 2.5;
  bot.isAlive = true;
  // No bot.oxygenLevel: Mineflayer leaves it undefined until the air changes.
  // placeBlock -> _genericPlace (writes the block_place packet).
  bot._genericPlace = async (referenceBlock) => {
    S.placePackets = (S.placePackets || 0) + 1;
    return referenceBlock.position;
  };
  Object.defineProperty(bot, "experience", {
    get() {
      if (S.experienceThrows) throw new Error("boom");
      return { level: 3, points: 40, progress: 0.25 };
    },
  });
  const peer = {
    id: 7,
    type: "player",
    name: "player",
    username: "Bravo",
    uuid: "00000000-0000-0000-0000-0000000000b7",
    get position() { return S.peerPos; },
    yaw: 3.14159,
    pitch: -0.1,
    headYaw: 3.0,
    width: 0.6,
    height: 1.8,
  };
  // A hidden camera player at the bot's own position must never be the target.
  const cam = {
    id: 8, type: "player", name: "player", username: "CameraAlpha", uuid: "cam",
    position: new Vec3(0.5, 65, 0.5), yaw: 0, pitch: 0, width: 0.6, height: 1.8,
  };
  bot.entities = { 1: bot.entity, 7: peer, 8: cam };
  bot.players = {
    Alpha: { username: "Alpha", entity: bot.entity, gamemode: 0 },
    Bravo: { username: "Bravo", entity: peer, gamemode: 0 },
    CameraAlpha: { username: "CameraAlpha", entity: cam, gamemode: 3 },
  };
  return bot;
}

// ---------------------------------------------------------------------------
// Reference headless-base64 with stubbed rendering deps
// ---------------------------------------------------------------------------

function findReference() {
  const cands = [
    argv.reference,
    process.env.REF_HEADLESS_BASE64,
    path.join(REPO, "node_modules", "prismarine-viewer-colalab", "lib", "headless-base64.js"),
  ].filter(Boolean);
  return cands.find((p) => fs.existsSync(p)) || null;
}

function loadReference(file) {
  const src = fs.readFileSync(file, "utf8");
  const stubs = {
    three: {},
    "node-canvas-webgl/lib": {},
    "../viewer": {
      WorldView: function WorldView() { throw new Error("rendering stub"); },
      Viewer: function Viewer() { throw new Error("rendering stub"); },
      getBufferFromStream: () => { throw new Error("rendering stub"); },
    },
  };
  const mod = { exports: {} };
  const req = (name) => (name in stubs ? stubs[name] : require(name));
  // Sloppy-mode wrapper, like Node's CommonJS loader.
  new Function("require", "module", "exports", "__filename", "__dirname", src)(
    req, mod, mod.exports, file, path.dirname(file),
  );
  return mod.exports;
}

// ---------------------------------------------------------------------------
// act_recorder protocol receiver
// ---------------------------------------------------------------------------

function startReceiver() {
  const episodes = [];
  const server = net.createServer((sock) => {
    let buf = Buffer.alloc(0);
    const ep = { header: null, frames: [], ended: false, closed: false };
    episodes.push(ep);
    sock.on("data", (d) => {
      buf = Buffer.concat([buf, d]);
      for (;;) {
        if (buf.length < 4) return;
        const n = buf.readUInt32LE(0);
        if (n === 0) {
          ep.ended = true;
          buf = buf.subarray(4);
          continue;
        }
        if (buf.length < 4 + n) return;
        const obj = JSON.parse(buf.subarray(4, 4 + n).toString("utf8"));
        buf = buf.subarray(4 + n);
        if (ep.header === null) ep.header = obj;
        else ep.frames.push(obj);
      }
    });
    sock.on("close", () => { ep.closed = true; });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, episodes, port: server.address().port }));
  });
}

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

function waitEvent(emitter, name, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`timeout waiting for ${name}`)), ms);
    emitter.once(name, () => { clearTimeout(t); resolve(); });
  });
}

async function waitFor(pred, ms, what) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (pred()) return;
    await sleep(50);
  }
  throw new Error(`timeout waiting for ${what}`);
}

function findPython() {
  const cands = [
    argv.python,
    path.join(REPO, ".venv", "Scripts", "python.exe"),
    path.join(REPO, ".venv", "bin", "python"),
  ].filter(Boolean);
  return cands.find((p) => fs.existsSync(p)) || null;
}

// ---------------------------------------------------------------------------
// Comparisons
// ---------------------------------------------------------------------------

const ONE_SHOT = ["attack", "use", "mount", "dismount", "place_block", "place_entity",
  "hotbar.1", "hotbar.2", "hotbar.3", "hotbar.4", "hotbar.5", "hotbar.6", "hotbar.7", "hotbar.8", "hotbar.9"];

function stripVolatile(frame) {
  const f = JSON.parse(JSON.stringify(frame));
  delete f.renderTime;
  for (const k of ONE_SHOT) delete f.action[k];
  delete f.action.mine;
  delete f.state;
  delete f.peer;
  return f;
}

function flagCounts(frames) {
  const out = {};
  for (const k of [...ONE_SHOT, "mine"]) out[k] = frames.filter((f) => f.action[k]).length;
  return out;
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

function compareEpisode(label, ours, ref) {
  check(ours.header && ours.header.episode === ref.header.episode, `${label}: header`);
  check(ours.ended && ref.ended, `${label}: zero end marker received`);
  check(ours.frames.length >= 5, `${label}: enough frames (${ours.frames.length})`);
  const refKeys = Object.keys(ref.frames[0]);
  for (const f of ours.frames) {
    assert.deepStrictEqual(Object.keys(f), [...refKeys, "state", "peer"], `${label}: top-level keys/order`);
    assert.deepStrictEqual(Object.keys(f.action), Object.keys(ref.frames[0].action), `${label}: action keys/order`);
    assert.deepStrictEqual(Object.keys(f.inventory), Object.keys(ref.frames[0].inventory), `${label}: inventory keys/order`);
    assert.deepStrictEqual(stripVolatile(f), stripVolatile(ref.frames[0]), `${label}: values equal to reference`);
    checks += 4;
  }
  // One-shot flags: each event shows up in exactly as many samples as in the
  // reference; `mine` (held) depends on how many samples fall in the hold.
  const oc = flagCounts(ours.frames);
  const rc = flagCounts(ref.frames);
  const om = oc.mine;
  const rm = rc.mine;
  delete oc.mine;
  delete rc.mine;
  assert.deepStrictEqual(oc, rc, `${label}: one-shot flag counts`);
  check(CADENCE === "after" ? om === rm : Math.abs(om - rm) <= 2 && (om > 0) === (rm > 0), `${label}: mine samples ${om} vs ${rm}`);
  checks++;
  // `mine` is read without reset: true over consecutive samples.
  const mineIdx = ours.frames.map((f, i) => (f.action.mine ? i : -1)).filter((i) => i >= 0);
  if (mineIdx.length) {
    check(mineIdx.length >= 3, `${label}: mine held over several samples`);
    check(mineIdx[mineIdx.length - 1] - mineIdx[0] === mineIdx.length - 1, `${label}: mine contiguous`);
  }
  const dts = ours.frames.slice(1).map((f, i) => f.renderTime - ours.frames[i].renderTime);
  if (CADENCE === "deadline") {
    // Mean period = interval (individual gaps jitter with the OS timer granularity).
    const n = ours.frames.length - 1;
    const mean = (ours.frames[n].renderTime - ours.frames[0].renderTime) / n;
    check(mean >= INTERVAL - 4 && mean <= INTERVAL + 6, `${label}: deadline cadence mean ${mean.toFixed(1)} ms`);
  } else {
    // headless-base64: interval after each sample (+ OS timer granularity).
    const m = median(dts);
    check(m >= INTERVAL - 2 && m <= INTERVAL + 16, `${label}: cadence median ${m} ms`);
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const worldSync = await makeWorld();
  const S = {
    yaw: 0, // exactly 0: bot.blockAtCursor() would return null here
    pitch: -0.6,
    controls: { forward: true },
    effects: { 10: { id: 10, amplifier: 255, duration: -1 } },
    peerPos: new Vec3(0.5, 65, -2.5),
    experienceThrows: false,
  };

  const recvOurs = await startReceiver();
  const recvRef = await startReceiver();
  const bots = [];

  const ours = makeFakeBot(worldSync, S);
  startFrameRecorder(ours, {
    output: `127.0.0.1:${recvOurs.port}`,
    interval: INTERVAL,
    disableRendering: 1,
    peerName: "Bravo",
    cadence: CADENCE,
  });
  bots.push(ours);

  const refFile = findReference();
  let ref = null;
  if (refFile) {
    console.log(`[test] reference: ${refFile}`);
    const headlessBase64 = loadReference(refFile);
    ref = makeFakeBot(worldSync, S);
    headlessBase64(ref, { output: `127.0.0.1:${recvRef.port}`, interval: INTERVAL, disableRendering: true });
    bots.push(ref);
  } else {
    console.warn("[test] reference headless-base64.js not found: skipping the side-by-side comparison");
  }

  const python = findPython();
  let py = null;
  let pyOut = null;
  let pyBot = null;
  if (python) {
    const pyPort = await freePort();
    pyOut = cleanup.dir = fs.mkdtempSync(path.join(os.tmpdir(), "frame-recorder-test-"));
    console.log(`[test] act_recorder.py: ${python} port ${pyPort} -> ${pyOut}`);
    py = cleanup.py = spawn(python, ["-u",
      path.join(REPO, "controller", "act_recorder", "act_recorder.py"),
      "--port", String(pyPort), "--host", "127.0.0.1", "--name", "Alpha",
      "--output_path", pyOut, "--instance_id", "0", "--start_id", "0",
      "--viewer_rendering_disabled", "1",
    ], {
      env: { ...process.env, WORLD_SEED_CONFIG: "12345", WORLD_GEN_INFO: JSON.stringify({ world_type: "flat", level_type: "minecraft:flat" }) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let pyLog = "";
    py.stdout.on("data", (d) => { pyLog += d; });
    py.stderr.on("data", (d) => { pyLog += d; });
    py.on("exit", (code) => { if (code) console.error(`[test] act_recorder exited ${code}\n${pyLog.slice(-2000)}`); });
    await waitFor(() => pyLog.includes("Socket now listening"), 20000, "act_recorder to listen");
    pyBot = makeFakeBot(worldSync, S);
    startFrameRecorder(pyBot, { output: `127.0.0.1:${pyPort}`, interval: INTERVAL, disableRendering: true, peerName: "Bravo", cadence: CADENCE });
    bots.push(pyBot);
  } else {
    console.warn("[test] no python found: skipping the act_recorder.py check");
  }

  const emitAll = (...a) => bots.forEach((b) => b.emit(...a));
  const endAll = async () => {
    const waits = bots.map((b) => waitEvent(b, "episodeended", 5000));
    emitAll("endepisode");
    await Promise.all(waits);
  };

  // Before any episode: samples are taken but not sent; flags are consumed.
  await sleep(150);
  emitAll("action_attack");
  await sleep(120);

  // ---- episode 7: looking down at the ground, one of each action ----
  emitAll("startepisode", 7);
  await sleep(300);
  emitAll("action_attack");
  emitAll("action_use");
  emitAll("action_hotbar_change", 3);
  emitAll("action_mine_start");
  await sleep(300);
  emitAll("action_place_block");
  emitAll("action_mine_finish");
  await sleep(300);
  await endAll();

  // ---- episode 8: pitch exactly 0, peer in front, sneaking, flying ----
  S.pitch = 0;
  S.controls = { sneak: true };
  S.effects[22] = { id: 22, amplifier: 0, duration: 37 };
  bots.forEach((b) => b._client.emit("abilities", { flags: 0x02 | 0x04, flyingSpeed: 0.05, walkingSpeed: 0.1 }));
  S.experienceThrows = true; // a failing part must only null that field
  await sleep(120);
  emitAll("startepisode", 8);
  await sleep(500);
  await endAll();

  // ---- episode 9: place a block on (0,64,-2), then attack ----
  S.pitch = -0.6;
  S.controls = {};
  emitAll("startepisode", 9);
  await sleep(200);
  // placeBlock: packet sent while the crosshair is on the reference block,
  // the event fires once the new block is in the client world.
  for (const b of bots) await b._genericPlace({ position: new Vec3(0, 64, -2) }, new Vec3(0, 1, 0));
  await sleep(10);
  worldSync.setBlockStateId(new Vec3(0, 65, -2), mcData.blocksByName.stone.defaultState);
  await sleep(30);
  emitAll("action_place_block");
  await sleep(200);
  emitAll("action_attack");
  await sleep(200);
  await endAll();

  // ---- protocol / reference checks ----
  await waitFor(() => recvOurs.episodes.length === 3 && recvOurs.episodes.every((e) => e.closed), 3000, "receiver close");
  const [o7, o8, o9] = recvOurs.episodes;
  check(o7.header.episode === 7 && o8.header.episode === 8, "headers carry the episode number");
  check(o7.ended && o8.ended, "zero end marker");
  if (ref) {
    await waitFor(() => recvRef.episodes.length === 3 && recvRef.episodes.every((e) => e.closed), 3000, "ref close");
    compareEpisode("ep7", o7, recvRef.episodes[0]);
    compareEpisode("ep8", o8, recvRef.episodes[1]);
    compareEpisode("ep9", o9, recvRef.episodes[2]);
    check(flagCounts(o7.frames).attack === 1, "pre-episode attack not leaked, in-episode attack once");
  }

  // ---- T16 state checks ----
  const s7 = o7.frames[o7.frames.length - 1];
  check(s7.yaw === 0 && s7.pitch === -0.6, "absolute yaw/pitch");
  const st = s7.state;
  check(st.health === 17 && st.food === 15 && st.saturation === 2.5, "health/food/saturation");
  check(st.xp_level === 3 && st.xp_progress === 0.25, "xp");
  check(st.oxygen === 20, `oxygen defaults to full air without an air_supply update (${st.oxygen})`);
  assert.deepStrictEqual(st.effects, [{ id: "minecraft:resistance", amplifier: 255, duration: -1 }]);
  check(st.hotbar.length === 9 && st.hotbar[0].id === "minecraft:diamond_sword" && st.hotbar[1].count === 64 && st.hotbar[2].id === null, "hotbar");
  check(st.selected_slot === 0, "selected_slot");
  assert.deepStrictEqual(st.offhand, { id: "minecraft:shield", count: 1 });
  assert.deepStrictEqual(st.armor, { head: { id: "minecraft:iron_helmet", count: 1 }, chest: null, legs: null, feet: null });
  check(st.on_ground === true && st.sneaking === false && st.sprinting === false && st.flying === false, "movement flags");
  check(st.in_water === false && st.alive === true && st.head_yaw === 0 && st.body_yaw === null, "misc");
  // Eye (0.5, 66.62, 0.5), pitch -0.6: hits the top of stone (0,64,-2) at 1.62/sin(0.6).
  check(st.cursor.block && st.cursor.block.face === "up", "cursor face");
  assert.deepStrictEqual(st.cursor.block.pos, [0, 64, -2]);
  check(st.cursor.block.name === "minecraft:stone" && st.cursor.block.state === "minecraft:stone", "cursor block name/state");
  check(Math.abs(st.cursor.block.distance - 1.62 / Math.sin(0.6)) < 0.002, `cursor distance ${st.cursor.block.distance}`);
  check(st.cursor.entity === null, "no entity under the cursor in ep7");
  assert.deepStrictEqual(s7.peer, { name: "Bravo", known: true, pos: [0.5, 65, -2.5], yaw: 3.14159, pitch: -0.1, head_yaw: 3, distance: 3 });

  const s8 = o8.frames[o8.frames.length - 1].state;
  check(s8.cursor.block === null, "nothing within 5 blocks straight ahead");
  check(s8.cursor.entity && s8.cursor.entity.uuid === peerUuid(), "peer under the cursor (camera player ignored)");
  check(s8.cursor.entity.id === "minecraft:player" && s8.cursor.entity.name === "Bravo" && s8.cursor.entity.type === "player", "cursor entity fields");
  check(Math.abs(s8.cursor.entity.distance - 2.7) < 0.002, `entity distance ${s8.cursor.entity.distance}`);
  check(s8.sneaking === true && s8.flying === true, "sneaking/flying");
  check(s8.xp_level === null && s8.health === 17, "failing getter only nulls its field");
  check(s8.effects.some((e) => e.id === "minecraft:saturation" && e.duration === 37), "effect names");

  // ---- cursor_at_action ----
  for (const ep of [o7, o8, o9]) {
    for (const f of ep.frames) {
      const ca = f.state.cursor_at_action;
      const fired = ["attack", "use", "place_block"].filter((k) => f.action[k]);
      check(fired.length ? ca !== null : ca === null, "cursor_at_action present iff an action fired");
      for (const k of ["attack", "use", "place_block"]) {
        if (ca) check(f.action[k] ? ca[k] !== null : ca[k] === null, `cursor_at_action.${k} iff action.${k}`);
      }
    }
  }
  const atk7 = o7.frames.find((f) => f.action.attack).state.cursor_at_action;
  assert.deepStrictEqual(atk7.attack.block.pos, [0, 64, -2]);
  check(atk7.use && atk7.use.block.face === "up", "attack and use in the same sample");
  const pl9 = o9.frames.find((f) => f.action.place_block);
  check(Boolean(pl9), "place_block frame in ep9");
  // The live cursor already sees the new block; the latched one is the
  // block placed against (latched when the packet was sent).
  assert.deepStrictEqual(pl9.state.cursor.block.pos, [0, 65, -2]);
  assert.deepStrictEqual(pl9.state.cursor_at_action.place_block.block.pos, [0, 64, -2]);
  check(pl9.state.cursor_at_action.place_block.block.face === "up", "place_block latched face");
  const atk9 = o9.frames.find((f) => f.action.attack).state.cursor_at_action;
  assert.deepStrictEqual(atk9.attack.block.pos, [0, 65, -2]);
  checks += 4;

  // ---- direct state-builder checks: oxygen sources, non-colliding blocks ----
  {
    const w = await makeWorld();
    const S2 = { ...S, yaw: 0, pitch: -0.6, controls: {}, experienceThrows: false };
    const b = makeFakeBot(w, S2);
    const sb = createStateBuilder(b, { peerName: "Bravo" });
    check(sb.buildState([0, -0.6]).oxygen === 20, "oxygen default 20");
    b.entity.metadata = { 1: 150 };
    check(sb.buildState([0, -0.6]).oxygen === 10, "oxygen from metadata air supply");
    b.oxygenLevel = 7;
    check(sb.buildState([0, -0.6]).oxygen === 7, "oxygen from bot.oxygenLevel");
    // Grass on top of the stone the ray hits: vanilla targets the grass.
    const ray = (name) => {
      w.setBlockStateId(new Vec3(0, 65, -2), mcData.blocksByName[name].defaultState);
      return sb.buildState([0, -0.6]).cursor.block;
    };
    let cb = ray("short_grass");
    check(cb.name === "minecraft:short_grass" && cb.pos[1] === 65, `cursor hits short_grass (${JSON.stringify(cb)})`);
    check(cb.face === "south" && Math.abs(cb.distance - 1.625 / Math.cos(0.6)) < 0.002, "grass outline box hit");
    cb = ray("torch");
    // Torch outline z in [-1.625, -1.375]: hit through its south face.
    check(cb.name === "minecraft:torch" && cb.face === "south" &&
      Math.abs(cb.distance - 1.875 / Math.cos(0.6)) < 0.002, `cursor hits torch outline (${JSON.stringify(cb)})`);
    cb = ray("rail");
    // Flat 1/16 box (not a full cube): hit on its top face where the ray lands.
    check(cb.name === "minecraft:rail" && cb.face === "up" &&
      Math.abs(cb.distance - (1.62 - 1 / 16) / Math.sin(0.6)) < 0.002, `cursor hits rail top (${JSON.stringify(cb)})`);
    cb = ray("cobweb");
    check(cb.name === "minecraft:cobweb", "cobweb (no collision) targeted as full cube");
    cb = ray("water");
    check(cb.name === "minecraft:stone" && cb.pos[1] === 64, "fluids ignored");
    cb = ray("air");
    check(cb.name === "minecraft:stone", "air ignored");
    // Nothing within reach: pitch slightly down, ground beyond 5 blocks.
    check(sb.buildState([0, -0.2]).cursor.block === null, "no block within 5 blocks");
  }

  // Block state strings use the world-log format (properties sorted).
  const stairs = Block.fromProperties("oak_stairs", { facing: "east", half: "bottom", shape: "straight", waterlogged: false }, 0);
  check(blockStateString(stairs) === "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]", "block state string");

  // ---- act_recorder.py ----
  if (py) {
    await waitFor(() => fs.readdirSync(pyOut).filter((f) => /_00000[78]_Alpha_instance_000\.json$/.test(f)).length === 2, 15000, "act_recorder output");
    for (const [ep, mine] of [[7, o7], [8, o8]]) {
      const file = fs.readdirSync(pyOut).find((f) => f.endsWith(`_00000${ep}_Alpha_instance_000.json`));
      const data = JSON.parse(fs.readFileSync(path.join(pyOut, file), "utf8"));
      const meta = JSON.parse(fs.readFileSync(path.join(pyOut, file.replace(/\.json$/, "_meta.json")), "utf8"));
      check(data.length >= 5 && meta.frames === data.length && meta.episode_id === ep, `act_recorder ep${ep}: ${data.length} frames`);
      const last = data[data.length - 1];
      const expect = mine.frames[mine.frames.length - 1];
      assert.deepStrictEqual(last.state, expect.state, `act_recorder ep${ep}: state passed through`);
      assert.deepStrictEqual(last.peer, expect.peer, `act_recorder ep${ep}: peer passed through`);
      checks += 2;
      check(last.episode_id === ep && last.frame_count === data.length - 1, "frame_count/episode_id");
      check(typeof last.epochTime === "number" && typeof last.relativeTimeMs === "number", "epochTime");
      check(last.yaw === 0 && last.yaw_mc_deg === -180 && typeof last.pitch_mc_deg === "number", "yaw/pitch handling");
      check(last.extra_info && last.extra_info.seed === 12345 && last.extra_info.world_type === "flat", "extra_info");
    }
  }

  console.log(`[test] OK (cadence ${CADENCE}): ${checks} checks passed (ep7 ${o7.frames.length} frames, ep8 ${o8.frames.length} frames)`);
  if (py) py.kill();
  if (pyOut && !argv.keep) fs.rmSync(pyOut, { recursive: true, force: true });
  recvOurs.server.close();
  recvRef.server.close();
  process.exit(0);
}

function peerUuid() {
  return "00000000-0000-0000-0000-0000000000b7";
}

main().catch((err) => {
  console.error("[test] FAILED:", err);
  if (cleanup.py) cleanup.py.kill();
  if (cleanup.dir) console.error(`[test] act_recorder output kept in ${cleanup.dir}`);
  process.exit(1);
});
