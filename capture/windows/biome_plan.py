#!/usr/bin/env python3
"""Preview the world-diversity plan (round 2) without starting anything.

  flat worlds   which flat preset (biome + surface layers) each instance gets
                for a level seed (generate_compose.flat_terrain_for_instance)
  episodes      per-episode time of day, weather and target biome as the
                controllers compute them (controller/utils/env-setup.js and
                biomes.js, via node); the biome actually used on a normal
                world also depends on /locate results (fallbacks)

Examples:
  python windows/biome_plan.py --seed 01792775997 --instances 4
  python windows/biome_plan.py --bot-rng-seed 12345 --episodes 20
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import generate_compose as G  # noqa: E402

NODE_SNIPPET = r"""
const path = require("path");
const root = process.argv[1];
const E = require(path.join(root, "controller/utils/env-setup.js"));
const B = require(path.join(root, "controller/utils/biomes.js"));
const [seed, n, start, worldType, genInfo] = process.argv.slice(2);
const args = { bot_rng_seed: seed, dusk_night_fraction: 0.15, fixed_time: -1,
               world_type: worldType, world_gen_info: genInfo || "" };
const rows = [];
for (let ep = Number(start); ep < Number(start) + Number(n); ep++) {
  const env = E.chooseEpisodeEnvironment(args, ep, {});
  const plan = worldType === "flat" ? null
    : B.planEpisodeBiomes(args, ep, null, { weatherRequested: env.weather_requested });
  rows.push({ episode: ep, time_of_day: env.time_of_day, label: env.time_of_day_label,
              weather_requested: env.weather_requested, weather: env.weather,
              precipitation: env.precipitation,
              biome_target: plan ? plan.candidates[0] : null,
              fallbacks: plan ? plan.candidates.slice(1, 4) : null });
}
console.log(JSON.stringify(rows));
"""


def flat_plan(seed: str, instances: int):
    rows = []
    for i in range(instances):
        biome, surface = G.flat_terrain_for_instance(i, seed)
        layers = G.generate_terrain_settings(biome, surface)["layers"]
        top = [f'{l["block"].split(":")[1]}x{l["height"]}' for l in reversed(layers)][:4]
        rows.append({"instance": i, "biome": biome, "surface": surface, "top_layers": top})
    return rows


def episode_plan(node: str, bot_rng_seed: str, episodes: int, start: int, world_type: str, gen_info: str):
    out = subprocess.run(
        [node, "-e", NODE_SNIPPET, str(ROOT), bot_rng_seed, str(episodes), str(start), world_type, gen_info],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", default=None, help="level seed (flat preset preview)")
    ap.add_argument("--instances", type=int, default=1)
    ap.add_argument("--bot-rng-seed", default=None, help="BOT_RNG_SEED (episode preview)")
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--start-episode", type=int, default=0)
    ap.add_argument("--world-type", default="normal", choices=["normal", "flat"])
    ap.add_argument("--node", default="node")
    ap.add_argument("--json", action="store_true", help="print JSON only")
    a = ap.parse_args(argv)
    if a.seed is None and a.bot_rng_seed is None:
        ap.error("give --seed and/or --bot-rng-seed")
    result = {}
    if a.seed is not None:
        result["flat_presets"] = flat_plan(a.seed, a.instances)
    if a.bot_rng_seed is not None:
        gen_info = ""
        if a.world_type == "flat" and a.seed is not None:
            b, s = G.flat_terrain_for_instance(0, a.seed)
            gen_info = json.dumps({"flat_biome": b, "flat_surface_block": s})
        rows = episode_plan(a.node, a.bot_rng_seed, a.episodes, a.start_episode, a.world_type, gen_info)
        result["episodes"] = rows
        result["summary"] = {
            "time": dict(Counter(r["label"] for r in rows)),
            "weather": dict(Counter(r["weather_requested"] for r in rows)),
            "biome_targets": dict(Counter(r["biome_target"] for r in rows if r["biome_target"])),
        }
    if a.json:
        print(json.dumps(result, indent=2))
        return 0
    for r in result.get("flat_presets", []):
        print(f'instance {r["instance"]}: {r["biome"]:<26} surface {r["surface"]:<18} {" / ".join(r["top_layers"])}')
    for r in result.get("episodes", []):
        print(f'ep {r["episode"]:>4}: t={r["time_of_day"]:>5} {r["label"]:<5} weather={r["weather_requested"]:<7} '
              f'biome={r["biome_target"] or "-"} fallbacks={",".join(r["fallbacks"] or [])}')
    if "summary" in result:
        print(json.dumps(result["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
