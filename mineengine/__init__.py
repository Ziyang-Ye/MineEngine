"""mineengine: exact-state white boxes for multiplayer Minecraft captures.

capture/ records two (or more) players' first-person videos with exact actions, camera poses and a replayable world-state
log; this package turns such a capture into training data: per-pixel block type / block state / face / instance / effect
G-buffers rendered from the replayed world (the "white box"), aligned clips, reference sheets, statistics and viewers.

Command-line tools (python -m mineengine.<module> --help):
  cap_reader      read and check a capture batch (world events, frames, poses, skins)
  cap2proxy       capture batch -> per-episode white-box G-buffers, clips and videos
  mc_registry     block registry (geometry, states, face crops) built from your own Minecraft client jar
  cap_synth       synthetic capture episodes in the capture output format (tests, no Minecraft needed)
  cap_selftest    end-to-end CPU self-test on synthetic episodes
  entity_refs     per-clip reference tiles of the other player (its skin, crops from the real video)
  view            side-by-side viewer videos (real | white box | state)
  block_stats     block / state statistics of a capture
Libraries (the ones marked --test run a self-test with python -m mineengine.<module> --test):
  voxel_proxy     voxel world -> G-buffer renderer (torch; nvdiffrast on CUDA, or numpy: cap2proxy --cpu-render)
  state_proxy     optional state overlays (HUD / entity layers) for visualisation and ablations (--test)
  cap_palette     block / item palette and drawn shapes (--test)
  mc_proxy_style  G-buffer -> RGB proxy frames (--test)
  core            World / Action types of the block-world program
  mc              block world as code: players, scripted bots (build / fight / raid), TNT, edits (used by cap_synth)
"""
__version__ = "0.1.0.dev0"
