/**
 * camera-pose-link.js - Forward the bot's pose straight to its camera client (T12).
 *
 * The camera client normally follows the bot through the server: the bot
 * sends a movement packet, the plugin teleports the camera player at the next
 * server tick, the camera client receives the teleport and renders it. That
 * round trip lags the bot by 50-80 ms (measured), which is several degrees
 * whenever the bot turns quickly.
 *
 * This module taps the bot's outgoing movement packets (position, look,
 * position_look - exactly what the server receives) and sends each resulting
 * pose as one UDP datagram to the camera's solaris-capture mod on 127.0.0.1.
 * The mod renders that pose directly (latency ~1 ms) and records it in the
 * frames sidecar; the server-side teleport stays for chunk loading.
 *
 * Datagram (UTF-8 JSON, one per packet):
 *   {"v":1,"bot":"Alpha","seq":n,"t":<epoch ms, float>,"x","y","z" (feet),
 *    "yaw","pitch" (Minecraft degrees, as sent),"sneak":bool,"riding":bool,
 *    "pkt":"position_look"}
 *
 * "riding": the bot sits on a vehicle. Mineflayer then sends no position
 * packets (the vehicle moves with vehicle_move), only bare look packets, so
 * x/y/z are the stale mount point; the camera mod keeps its server-followed
 * (seated) camera for such datagrams (fallback reason "riding").
 *
 * The returned handle's lastSeq() is the seq of the last datagram sent (null
 * before the first one). frame-recorder.js writes it into every action sample
 * as `pose_seq`, so postprocessing can pair each sample with a video frame
 * that rendered exactly that packet (frames carry `pose_seq` in the sidecar).
 *
 * Enabled when CAMERA_POSE_PORT (env) or options.port is a positive port.
 * Never throws into the bot: every failure is swallowed after one warning.
 */

const dgram = require("dgram");
const { performance } = require("perf_hooks");

const MOVE_PACKETS = new Set(["position", "look", "position_look"]);

/**
 * Epoch milliseconds with sub-millisecond resolution (same clock as Date.now()).
 * @returns {number}
 */
function epochMs() {
  return performance.timeOrigin + performance.now();
}

/**
 * Start forwarding movement packets of `bot` to its camera.
 * @param {*} bot - Mineflayer bot
 * @param {{port?: number, host?: string}} [options]
 * @returns {{close: Function, port: number}|null} null when disabled
 */
function startCameraPoseLink(bot, options = {}) {
  const port = Number(options.port ?? process.env.CAMERA_POSE_PORT ?? 0);
  if (!Number.isInteger(port) || port <= 0) return null;
  const host = options.host || process.env.CAMERA_POSE_HOST || "127.0.0.1";
  const client = bot._client;
  if (!client || typeof client.write !== "function") return null;

  const socket = dgram.createSocket("udp4");
  socket.on("error", (err) => {
    console.warn(`[${bot.username}] [pose-link] socket error: ${err.message}`);
  });
  socket.unref();

  // Last pose sent to the server (feet position + Minecraft-degree look).
  const pose = { x: null, y: null, z: null, yaw: null, pitch: null };
  let seq = 0;
  let lastSeq = null;
  let warned = false;

  const originalWrite = client.write.bind(client);
  client.write = function patchedWrite(name, params, ...rest) {
    const result = originalWrite(name, params, ...rest);
    if (MOVE_PACKETS.has(name) && params) {
      try {
        if (name !== "look") {
          pose.x = params.x;
          pose.y = params.y;
          pose.z = params.z;
        }
        if (name !== "position") {
          pose.yaw = params.yaw;
          pose.pitch = params.pitch;
        }
        if (pose.x !== null && pose.yaw !== null) {
          const msg = Buffer.from(
            JSON.stringify({
              v: 1,
              bot: bot.username,
              seq: seq++,
              t: epochMs(),
              x: pose.x,
              y: pose.y,
              z: pose.z,
              yaw: pose.yaw,
              pitch: pose.pitch,
              sneak: Boolean(bot.getControlState && bot.getControlState("sneak")),
              riding: Boolean(bot.vehicle),
              pkt: name,
            }),
          );
          lastSeq = seq - 1;
          socket.send(msg, port, host);
        }
      } catch (err) {
        if (!warned) {
          warned = true;
          console.warn(`[${bot.username}] [pose-link] send failed: ${err.message}`);
        }
      }
    }
    return result;
  };

  console.log(`[${bot.username}] [pose-link] forwarding movement packets to udp://${host}:${port}`);
  return {
    port,
    /** seq of the last datagram sent (the pose of the last movement packet), or null */
    lastSeq: () => lastSeq,
    close() {
      client.write = originalWrite;
      try {
        socket.close();
      } catch (err) {
        // already closed
      }
    },
  };
}

module.exports = {
  startCameraPoseLink,
};
