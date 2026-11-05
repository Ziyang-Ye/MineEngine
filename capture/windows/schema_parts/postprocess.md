## Postprocess additions (aligned camera meta, dataset, `world_events_report.json`)

### 7.x Aligned camera meta: `aligned/<action_stem>_camera_meta.json` (additions)

`postprocess/process_recordings.py` (CLI unchanged except the optional
`--match-clock auto|pts`, default `auto`, and `--pose-pairing seq|time`, default `seq`) matches action `i` to the first camera
frame whose time is `>=` the action's `epochTime`. Pose-seq pairing (T12): action samples
(controller, 20 Hz) and frames (client, 20 fps) are sampled asynchronously, so that frame
often already shows the bot's next movement packet (sent between the sample and the frame),
i.e. up to one tick of camera motion between the image and its yaw/pitch label. With the
camera pose link (the sample has `pose_seq`, the sidecar's injected frames have `pose_seq`),
a sample whose time-matched frame `k` rendered another packet is paired instead with frame
`k−1` or `k+1` if that frame rendered the sample's packet (the closer one in time; `k−1` can
precede the sample by less than one frame interval). Otherwise the time match is kept. The frame time
(`diagnostics.match_clock`) is the sidecar's exact capture time `epoch_ms/1000`
when the sidecar is usable (`sidecar_used`, all `epoch_ms` numeric and
non-decreasing) and `--match-clock auto`; otherwise the MKV PTS (wall clock), as
before and as on Linux. Reason: with the contract's command (`-f rawvideo
-framerate FPS -use_wallclock_as_timestamps 1`) ffmpeg rounds each frame's
pipe-arrival time to the input time base 1/fps, so the Windows PTS lie on a 50 ms
grid and frames that arrive in a burst share one PTS (Linux x11grab PTS are µs
precise). The PTS are still used to locate frames in the file. Frame extraction is now exact for variable-frame-rate recordings:
every seek is verified against the decoded frame's timestamp (the ffprobe PTS
list), so output frame `i` is exactly recording frame `frame_mapping[i].frame_index`
(OpenCV's own frame seek converts frame numbers to times with the nominal fps
and picks the wrong frame once capture slots were missed).
Seeks that land inside a group of frames sharing one PTS decode forward to the
next distinct PTS, so duplicate PTS do not shift the frame index.
ffprobe is taken from `$FFPROBE`, `PATH`, or next to `$FFMPEG` / `ffmpeg` on `PATH`.

Camera restarts: if the orchestrator restarted a camera, its files are
`camera_<bot>_r<n>.mkv` / `camera_<bot>_r<n>_meta.json` / `camera_<bot>_r<n>_frames.jsonl`
next to (or instead of) `camera_<bot>_meta.json`. The meta whose
`[start_epoch_seconds, stop_epoch_seconds]` (or `start + frames/fps`, open-ended if
neither is known) overlaps the episode's action times the most is used
(`camera_meta_path` names it). An episode split across two recordings is aligned
against one of them only.

| field | meaning |
|---|---|
| `camera_params` | New. Actual camera settings of the recording (T2/T13): flat copy of the camera meta's `options` (Linux / Docker metas have none: then `camera_options.json` next to the camera meta, written by `camera/launch_minecraft.py` with the same key names, and `options_source: "camera_options.json"`) (`fov_deg`, `fov_effect_scale`, `render_distance`, `simulation_distance`, `server_view_distance`, `gui_scale`, `gui_scale_effective`, `bob_view`, `damage_tilt_strength`, `screen_effect_scale`, `entity_shadows`, `clouds`, `graphics_mode`, `particles`, `max_fps`, `hud_visible`, `name_tags_visible`) plus `width`, `height`, `fps`, `encoder` (= meta `video_encoder`), and when present `capture_method`, `platform`, `mc_version`, `fabric_loader_version`, `mod_version`, `framebuffer`, `window_hidden`. `options_recorded` = false for older (Linux/x11grab) metas without `options`; then only the generic keys are present |
| `diagnostics.sidecar_used` | New. true if the per-frame sidecar `camera_<bot>_frames.jsonl` (meta `frames_file`) was attached: it exists and has exactly one valid line per video frame (line k = video frame k) |
| `diagnostics.sidecar_reason` | New. why the sidecar was not used (no `frames_file`, file missing, line count != frame count) |
| `diagnostics.sidecar_path`, `sidecar_lines`, `sidecar_bad_lines` | New. sidecar location and line counts |
| `diagnostics.match_clock` | New. `sidecar_capture_time` or `pts`: which frame times were matched against the action times (and are in `frame_time_sec`) |
| `diagnostics.pose_seq_pairing` | New. `checked` (samples with `pose_seq` whose matched frame is injected), `same` (already on their packet), `prev` / `next` (re-paired with that neighbour), `none` (no neighbour rendered the packet), `skipped_no_seq`, `skipped_not_injected`, `same_packet_fraction_before` / `_after`, and when samples were moved `min_delta_sec_after` / `max_delta_sec_after` / `duplicate_frame_count_after` (the other diagnostics describe the time match before this step; moving to `k−1` makes deltas down to about −1/fps and reuses some frames for two samples); or `{reason}` when disabled / no sidecar |
| `diagnostics.match_clock_reason` | New. why the sidecar clock was not used although the sidecar was (`match_clock=pts requested`, non-numeric or decreasing `epoch_ms`) |
| `diagnostics.pts_duplicate_frames` | New. frames whose PTS is not later than the previous frame's (quantised wall-clock PTS) |
| `diagnostics.pts_minus_sidecar_ms` | New. stats (`n`, `mean`, `min`, `p50`, `p90`, `p99`, `max`, `median`) of `PTS*1000 − sidecar epoch_ms` over the frames used by this episode. With the contract's rawvideo + wall-clock command this is pipe latency plus up to ±1/(2·fps) of rounding (can be negative); with the mod's `timestamps: capture` mode it is ~0. Not a pure latency measure |
| `diagnostics.camera_meta_frames` | New. `frames` count written by the recorder at stop (if present) |
| `diagnostics.extraction` | New. `exact_seek` (PTS-verified seeks used), `seeks`, `decoded_frames`, `cache_hits`, `extract_seconds` |

`frame_mapping[]` entries: `frame_time_sec` is the matched frame's time on the
matching clock (capture time with `match_clock = sidecar_capture_time`, else the
MKV PTS) and `delta_sec = frame_time_sec − action_time_sec`; new `frame_pts_sec` is
always the MKV PTS. When `sidecar_used`, entries also carry the pose the client
actually rendered in the matched frame (T12 ground truth):

| field | meaning |
|---|---|
| `cam_yaw`, `cam_pitch` | rendered camera yaw / pitch, Minecraft degrees (sidecar `cam_yaw`, `cam_pitch`) |
| `cam_pos` | `[x, y, z]` camera (eye) position used to render |
| `fov` | vertical FOV in degrees actually used by the GameRenderer for this frame |
| `sidecar_epoch_ms` | capture time of the frame (host clock, ms) |
| `cam_game_time` | client `level.getGameTime()` at capture |
| `cam_screen` | open screen class (null when no GUI screen covers the view) |
| `pose_source`, `pose_age_ms`, `pose_seq`, `pose_t`, `pose_held`, `pose_fallback`, `player_yaw`, `player_pitch` | copied from the sidecar when present (camera pose link, `windows/schema_parts/camera.md`): `injected` = the frame rendered the bot's own movement packet `pose_seq`; `player_*` = the server-followed camera player's look in that frame |
| `action_pose_seq` | the action sample's `pose_seq` (packet its yaw/pitch came from), when present |
| `pose_pair` | pose-seq pairing outcome for this sample: `same`, `prev`, `next`, `none` (absent when not checkable) |

To compare with the action JSON: `yaw_mc_deg = ((degrees(π − yaw) + 180) mod 360) − 180`,
`pitch_mc_deg = −degrees(pitch)`.

### 1.x Prepared dataset (addition)

`postprocess/prepare_train_dataset.py` also copies the aligned camera meta of
every valid episode/bot as `<prefix>_EEEEEE_<Bot>_instance_III_camera_meta.json`
(next to `<prefix>_EEEEEE_<Bot>_instance_III.mp4`).

### 9. `world_events_report.json` (New format `solaris-world-events-report-v2`)

Written by `postprocess/verify_world_events.py DIR --report PATH`. DIR may be a
batch dir, its `output/`, or a prepared dataset dir. Aligned metas are read from
`<batch>/aligned` (`--aligned-dir`), raw camera metas / sidecars from
`<batch>/camera` (`--camera-dir`); in a dataset dir the copied `*_camera_meta.json`
are used. Exit code: 0 = no FAIL, 1 = at least one FAIL, 2 = no world log found.
Each episode prints one line
`[OK|WARN|FAIL] episode E instance I type=… records=… changes=… ticks=… entities=… (missing N) replay_mismatch=… damage=… srv_cam_p99=… | Alpha: … vis=… cam_p99=…[, injected=… inj_p99=… paired_inj_p99=… pose_matched_p99=… pose_age_p50=…ms] | Bravo: …` (the bracketed part only with pose-link sidecars)
followed by its errors / warnings, then one batch summary line.

Top level: `format`, `generated_utc`, `dir`, `summary`, `episodes` (list).
(The previous version was a bare list of episode reports.)

`summary`:

| field | meaning |
|---|---|
| `episodes` | episodes checked |
| `status_counts` | `{OK, WARN, FAIL}` |
| `episode_types` | histogram of `episode_type` |
| `encountered_error` | `episode_info_files`, `true`, `ratio`, `per_bot` {`files`, `true`, `ratio`} from `episode_info.encountered_error` |
| `t12_deg` | pooled over the batch: `server_combined` (tick.cameras, the per-episode `basis` measure below), `server_one_tick_input_combined` (`dyaw/dpitch_to_target`, informational), `rendered_best_lag_combined`, `rendered_paired_combined` (stats `n`, `mean`, `p50`, `p90`, `p99`, `max`, degrees), `rendered_best_lag_ms_median`; pose link: `rendered_injected_best_lag_combined`, `rendered_injected_lag0_combined`, `rendered_server_best_lag_combined`, `rendered_pose_matched_combined`, `pose_age_ms`, `pose_source_frames` {source: frames}, `injected_fraction` (null without pose fields); frame vs its action label (the training pairs): `rendered_paired_injected_combined`, `rendered_paired_server_frames_combined`, `paired_injected_frac_over_threshold`, and on the same injected frames `paired_same_frames_injected_combined` vs `paired_same_frames_server_followed_combined` (the server-followed `player_yaw/pitch` of those frames vs the same labels), `pose_pair` {kind: samples} |
| `visibility` | per bot and `all`: `frames`, `visible`, `fraction` (T8) |
| `replay_mismatch_total`, `episodes_with_replay_mismatch` | replay check over the batch |
| `thresholds` | thresholds used (CLI options) |

Episode report: upstream fields (`episode`, `instance`, `events_file`, `records`,
`bad_lines`, `counts`, `seed`, `episode_type`, `game_time_range`, `epoch_ms_range`,
`snapshot_box`, `replay_changed_voxels`, `replay_mismatches`, `replay_mismatch_examples`,
`bots` {bot: `action_file`, `frames`, `epoch_ms_range`, `yaw_pitch_present`,
`est_game_time_minus_worldAgeTicks`, `change_records_within_frames`, `change_records_total`},
`alignment_file`) plus `status`, `errors`, `warnings`, `encountered_error` {bot: bool} and `checks`:

| `checks.` | content | status rule (defaults) |
|---|---|---|
| `yaw_pitch.<bot>` | `frames`, `missing` (frames whose `yaw` or `pitch` is not a finite number), `first_missing_indices` (≤10) | FAIL if `missing > 0` (T4) |
| `ticks` | `records`, `distinct_game_times`, `every` (= `heartbeat_ticks`), `span`, `missing_ticks`, `gaps`, `max_gap`, `gap_examples` [[from, to]] | WARN if `missing_ticks > 0` |
| `entities` | same as `ticks` with `every` = `entities_every`, plus `entities_every`, `entities_radius`, `entities_per_record` (stats), `kinds` {kind: count over records}, `head_gap_ticks`, `tail_gap_ticks` (first/last entities record vs first/last tick) | FAIL if no `entities` record although `episode_begin.entities_every > 0`; WARN if gaps, or no records and logging not announced |
| `time_weather.<bot>` | `time_of_day`, `time_of_day_label`, `weather`, `daylight_cycle`, `weather_cycle`, `difficulty`, `damage_enabled` from `episode_info.state_metadata` | WARN if `time_of_day` or `weather` missing (T1) |
| `camera_params.<bot>` | `source` (`aligned_meta.camera_params`, `camera_meta.options`, or `camera_options.json` = the Linux launcher's file next to the camera meta when the meta has no `options`), `camera_meta` (raw camera meta file chosen under `<batch>/camera/output_<bot>/<instance>/`, restart-aware as above), `fov_deg`, `fov_effect_scale`, `render_distance`, `simulation_distance`, `gui_scale`, `gui_scale_effective`, `bob_view`, `width`, `height`, `fps`, `hud_visible`, `name_tags_visible`, `aligned_meta`, `sidecar_used`, `pts_minus_sidecar_ms` | WARN if not recorded; FAIL if `fov_deg != --expected-fov` (70) or `fov_effect_scale != 0` (T2/T13) |
| `t12.server.<bot>` | from `tick.cameras` (`target` = bot), stats in degrees (`combined` = max(abs dyaw, abs dpitch) per tick): `ticks`; `follow` {`yaw`, `pitch`, `combined`, `pos` (blocks)} from `dyaw/dpitch/dpos_since_sync` (camera pose vs. the pose commanded at the last sync: the follow error); `lag_compensated` {`yaw`, `pitch`, `combined`}: camera pose in tick N vs. the target's `players[]` pose in tick N−1 (consecutive ticks only); `one_tick_input` {`yaw`, `pitch`, `combined`} from `dyaw/dpitch_to_target` (sampled at tick start before the sync, i.e. how far the bot turned in one tick; informational, not an error); `basis` = `follow`, or `lag_compensated` for logs without `*_since_sync`; `combined` = stats of the basis; `flying_fraction`, `gamemode` histogram. Any of the three groups is null when absent | WARN if `combined.p99 >= --t12-max-p99` (0.2) |
| `t12.rendered.<bot>` | rendered pose (aligned `frame_mapping` cam_* or, without aligned metas, the raw sidecar within the episode's action span) vs the bot's action-JSON yaw/pitch converted to MC degrees and linearly interpolated at the frame's capture time: `frames`, `source`, `lag0` {`yaw`,`pitch`,`combined`}, `best_lag_ms` (lag L in −250..1000 ms, step 10, minimising the mean error of rendered(t) vs bot(t − L); positive = camera behind the bot), `best_lag` {…}, `paired` (frame vs its own action entry, i.e. the error a model sees), `paired_detail` {`samples`, `combined`, `frac_over_threshold`, `by_source.<src>` {`samples`, `combined`, `frac_over_threshold`}, `injected_vs_server_followed_same_frames` {`samples`, `injected`, `server_followed`, `*_frac_over_threshold`}, `pose_pair` {kind: n}, `simulated` (true without aligned metas: the samples are paired with the raw sidecar frames exactly like `process_recordings.py`, first frame at or after the sample + pose-seq pairing)}, `fov_used` {`min`, `max`, `frames_off_expected`}, `threshold_basis` (which statistic the WARN rule used), and with pose-link sidecars `pose_link` (below) | WARN if the p99 of the threshold basis `>= 0.2`: with injected frames `paired_detail.by_source.injected` ("injected frames vs their action labels (paired)"), else `best_lag.combined`; with injected frames also WARN if `pose_link.pose_matched` p99 `>= 0.2` ("camera fidelity"); WARN if the camera's pose link is enabled (meta) or rendered any injected frame but fewer than `--t12-min-injected` (0.95) of the frames in the episode span are injected (with `server_frame_reasons`); WARN if the sidecar has pose fields but no injected frame; WARN if any rendered frame used fov != 70 (±0.05) |
| `t12.rendered.<bot>.pose_link` | split by the sidecar's `pose_source` (camera pose link; pose fields from `frame_mapping`, or joined from the raw sidecar on `sidecar_epoch_ms` for older aligned metas): `frames` {`injected`, `server`, `unknown`: n}, `injected_fraction`, `server_frame_reasons` {`pose_fallback`: n}, `held_frames`, `pose_age_ms` and `pose_age_ms_not_held` (stats), `by_source.<src>` (`lag0`, `best_lag_ms`, `best_lag`, as above, over that subset), `pose_matched` {`samples`, `combined`}: exact check without interpolation — an injected frame rendered packet `pose_seq`, which was the bot's pose from `pose_t` until at least the camera setup (`pose_t + pose_age_ms`); every action sample taken in that window (minus 5 ms of clock margin) is compared with the rendered yaw/pitch, `outdated` {`checked`, `outdated`, `max_newer_packet_ms`}: injected frames that showed packet n although packet n+1 (send time known from another frame) had already been sent before the camera setup (the UDP transit, ~1 ms). The interpolated measures (`lag0`, `best_lag`, `paired`) cannot reach 0.2° whenever the bot turns: the bot turns in per-tick steps (e.g. 9° per 50 ms) and the 20 Hz samples are interpolated linearly, so a frame showing the exact step differs from the interpolated line by up to one step | informational |
| `visibility.<bot>` | `frames`, `visible`, `fraction`, `median_distance`: the other bot's body centre (feet + 0.9) inside this bot's frustum (eye = feet + 1.62, vertical FOV 70°, 16:9, ≤ 64 blocks, no occlusion), using the other bot's sample nearest in time (≤ 500 ms) | WARN if `fraction < --min-visibility` (0.40, T8) |
| `damage` | `damage_episode` (`state_metadata.damage_enabled` or type in pvp/pve/pveMobs/pvpMelee/pvpRanged), `entity_damage`, `player_damage` (victim is a tracked player), `entity_death`, `min_player_health_after` | WARN if damage episode without `entity_damage` (T14) |
| `swing_before_damage` | `pvp_hits` (tracked player hurt by the other tracked player), `melee_hits` (no projectile), `with_swing` (attacker `arm_swing` within [−500 ms, +100 ms] of the hit; 1.21 clients send the swing packet right after the attack packet), `fraction`, `window_ms`, `arm_swing_records` | WARN if `fraction < --min-swing-fraction` (0.95, T15) |
| `dig_before_break` | `player_breaks`, `excluded` {reason: n} (`insta_break flag` on block_break/dig_start, `creative` gamemode, `instant-break block` such as torches, flowers, crops, TNT), `checked`, `with_dig` (a `dig_start` or `dig_progress` at that position since its last break/abort), `with_increasing_progress` (non-decreasing stages), `fraction`, `dig_records`, `examples_without_dig` | WARN if `fraction < --min-dig-fraction` (0.90, T17) |
| `frame_fields.<bot>` | `frames`, `missing` {key: frames} for `state`, `state.health/food/hotbar/selected_slot/cursor`, `state.cursor{block,entity}`, `peer`, `peer.name/known/pos/yaw/pitch/head_yaw/distance`; `optional_state_present` {key: frames} for saturation, offhand, armor, xp_level, xp_progress, oxygen, effects, body_yaw, head_yaw, on_ground, sneaking, sprinting, flying, in_water, alive | WARN if any required key is missing (T16) |

Unchanged FAIL causes: missing / empty / unparseable world log, first record not
`episode_begin`, `seq` not strictly increasing. Unchanged WARN causes: last record
not `episode_end`, replay mismatches, missing snapshots, worldAgeTicks disagreement,
no action JSON. A check that crashes marks only that episode FAIL (`check crashed: …`).

The world log is streamed (one pass); `entities` payloads are only summarised.
