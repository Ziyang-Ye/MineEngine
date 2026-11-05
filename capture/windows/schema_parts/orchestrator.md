## Windows-native runner: `compose_configs/run_config.json` and logs (New)

Written by `windows/run_windows.py` (the Windows-native replacement of
`run_laptop.sh` / `run.sh`, see `windows/CONTRACT.md`). The batch tree is the
same as in section 1; on Windows these files differ or are added:

| file | meaning |
|---|---|
| `compose_configs/run_config.json` | New. Everything the batch ran: commands, env, ports, tool versions, git HEAD, per-process exit codes, camera restarts, postprocess results (table below). Rewritten at each milestone; the final version is written after postprocessing |
| `compose_configs/docker-compose-000.yml` | reference copy of the `generate_compose.py` config the native env was derived from (not used to run anything; `GENERATOR_SETTINGS` is inlined as JSON) |
| `logs/docker-compose-000/<service>.log` | one log per native process, named like the compose services (`mc_instance_0.log`, `act_recorder_alpha_instance_0.log`, `controller_alpha_instance_0.log`, `camera_alpha_instance_0.log`, `spectator_alpha_instance_0.log`, `plugin_starter_instance_0.log`, `prep_data_instance_0.log`, ... for Bravo likewise). Every line is prefixed with an ISO-8601 UTC timestamp (`2026-10-23T13:00:52.645Z`, like `docker compose logs --timestamps`). A restarted process appends to the same file after a `[run_windows] ...` marker line |
| `logs/run_windows.log` | New. Orchestrator messages (start/stop of every process, restarts, aborts) |
| `logs/postprocess/align_<action_stem>.log` | New. Command line + output of `postprocess/process_recordings.py` for that action file |
| `logs/postprocess/verify_world_events.log` | New. Output of `postprocess/verify_world_events.py` |
| `data/0/server.properties` | written by the runner (CONTRACT §2) + keys Paper adds; additionally `server-ip=127.0.0.1` |
| `camera/data_<bot>/0/solaris_capture.stop` | stop file the runner creates at batch end; the capture mod then finalises the MKV and quits |
| `camera/output_<bot>/0/camera_<bot>_r1.mkv` (+ `_r1_meta.json`, `_r1_frames.jsonl`) | only if that camera exited early after it had already written files and was restarted (once). Nothing is overwritten. Alignment considers `camera_<bot>_meta.json` and every `camera_<bot>_r*_meta.json` and, per episode, uses the recording that covers the episode's action times (`select_camera_meta` in `postprocess/process_recordings.py`). If the camera died before writing anything, the restart reuses the normal names. Note: the camera leaving makes EpisodeManager kick its controller bot ("Your partner left"), so the episode in progress at the crash is normally lost and that instance may not produce further episodes |

`run_config.json` top-level keys:

| field | type | meaning |
|---|---|---|
| `schema` | str | `"solaris-windows-run-config/1"` |
| `written_utc` | str | ISO-8601 UTC time of this write |
| `runner` | str | `"windows/run_windows.py"` |
| `argv` | list[str] | command line of the runner |
| `options` | object | all resolved CLI options (`mode`, `episodes`, `types` = EPISODE_TYPES actually used, `weights`, `num_flat`, `num_normal`, `width`, `height`, `render_distance`, `no_cameras`, ports, ...) |
| `status` | str | `starting`, `running`, `completed` (all controllers exited by themselves), `completed_with_kills` (all controllers exited, but at least one was stopped `--controller-exit-grace` seconds after its partner in the same instance had exited; see `services.controller_*.killed_after_partner_grace`), `timeout` (`--batch-timeout`), `interrupted` (Ctrl+C), `aborted` (server died / not ready, recorder failed at start, ports busy), `killed` (second Ctrl+C) |
| `abort_reason` | str or null | human-readable reason for `aborted` / `interrupted` |
| `git` | object | `commit` (HEAD sha), `branch`, `dirty` (tracked files modified) |
| `tools` | object | resolved paths: `java`, `node`, `python`, `ffmpeg_dir`, `ffmpeg`, `paper_jar`, `paper_bundler_repo`, `mc_home`, `capture_mod_jar`, `launch_camera`, `mirrorbot_jar`, `setup_json` |
| `tool_versions` | object | `java`, `node`, `python`, `ffmpeg` (first line of `-version`), `paper_jar_sha256`, `mirrorbot_jar_sha256`, `mirrorbot_has_world_state_logger`, `capture_mod_jar_sha256`, `os` |
| `camera` | object or null | camera settings passed to `launch_camera.py`: `width`, `height`, `fps`, `encoder`, `render_distance`, `gui_scale`, `hide_window`, `max_memory`, `pose_link` (false with `--no-pose-link`); null with `--no-cameras`. The values the client actually used are in `camera_<bot>_meta.json` (`pose_link` object there). The pose ports are in `batch.instances[].ports`; each camera gets `--pose-port`, its controller env `CAMERA_POSE_PORT` (`0` = off) |
| `env_note` | str | how the service env is composed (values below on top of the runner's own environment; `PATH` prefixed with the ffmpeg and node dirs; `FFMPEG`, `FFPROBE`, `PYTHONUNBUFFERED=1`, `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`) |
| `batch` | object | `batch`, `batch_dir`, `output_dir`, `aligned_dir`, `compose_dir`, `instances` (below) |
| `timeline` | list | `{utc, event, ...}`: `batch_start`, `servers_ready`, `all_started`, `camera_restart`, `controller_grace_kill` (`service`, `instance`), `controllers_exited` (`exit_codes`, `grace_killed`), `shutdown_start`, `shutdown_done` |
| `postprocess` | object | `alignment`: `{jobs, ok, failed, failed_files, skipped_no_camera_meta, logs}` or `{skipped}`; `verify_world_events`: `{exit_code, report, log}` |

`batch.instances[]`:

| field | type | meaning |
|---|---|---|
| `instance_id` | int | k (log dir `docker-compose-00k`) |
| `world_type` | str | `flat` / `normal` |
| `seed` | str | level-seed string (`WORLD_SEED_CONFIG`) |
| `world_gen_info` | object | same object as the controllers' `WORLD_GEN_INFO` |
| `ports` | object | `game`, `rcon` (base + k, as generate_compose.py), `act_recorder_alpha`/`_bravo`, `coord_alpha`/`_bravo`, and with cameras and the pose link `pose_alpha`/`_bravo` (UDP; base, base + 1, plus 30·k; defaults 8110/8120/8130). The runner refuses to start when any two roles of any instances share a port |
| `server_dir` | str | `BATCH/data/<k>` |
| `server_properties` | object | the keys the runner wrote into `server.properties` |
| `eula_txt` | bool | whether `eula.txt` was written (`--accept-minecraft-eula`) |
| `camera_dirs` | object | `{Alpha: {output, data}, Bravo: {...}}` |
| `logs_dir` | str | `BATCH/logs/docker-compose-00k` |
| `services` | object | service name -> `{kind, cmd (list), cwd, env (only the vars set for this service), log, runs, restarts?}`; `kind` is `server`, `act_recorder`, `camera`, `spectator`, `plugin_starter` or `controller`; camera entries also have `bot`, `video_name`, `stop_file` |
| `services.*.runs[]` | list | one per process start: `pid`, `started_utc`, `ended_utc`, `exit_code` (processes the runner stops are terminated, so their code is 1), camera runs also `video_name`; restarts also `cmd` |
| `services.controller_*.killed_after_partner_grace` | bool | present (`true`) only if the runner killed this controller because its partner in the same instance had exited more than `--controller-exit-grace` seconds earlier |
| `services.camera_*.restarts[]` | list | `{utc, previous_exit_code, video_name}` for each restart (at most one) |
