# Third-party components

The first table lists the few third-party files that are stored in this repository. Nothing in the second table is stored here: each item is downloaded or installed at setup, under its own licence. Check each licence before you redistribute captured data or binaries.

Captured videos are Minecraft gameplay footage. Sharing them falls under Mojang's [Minecraft Usage Guidelines](https://www.minecraft.net/en-us/usage-guidelines).

The outputs also record absolute paths of the machine that made them: for example `compose_configs/run_config.json`, the `*_meta.json` files in `aligned/` and the `episode.json` files `mineengine` writes. By default `capture/linux/run_headless.sh` also names its output directory after the host. These can show your user name, mount points and host name, so check them before you share a dataset.

## Stored in this repository

| component | version | how it is obtained | licence |
|---|---|---|---|
| SolarisEngine | upstream commit `78f23e7` (2026-07-23) | stored in the repository: `capture/` is derived from it; see [NOTICE](NOTICE) | Apache-2.0 |
| Gradle Wrapper | 8.14.3 | stored in the repository: `capture/camera/capture-mod/gradlew`, `gradlew.bat`, `gradle/wrapper/gradle-wrapper.jar` | Apache-2.0 |
| SkinsRestorer configuration | — | stored in the repository: `capture/server/plugins/SkinsRestorer/config.yml`, the plugin's default file as kept in upstream SolarisEngine, modified here (`advanced.noConnections: true`, `api.fetchRecommendedSkins: false`, so the plugin makes no web requests; noted at the top of the file) | GPL-3.0 (SkinsRestorer, © SRTeam); text in [LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt) |
| bStats configuration | — | stored in the repository: `capture/server/plugins/bStats/config.yml`, written by the bStats library inside the plugins and kept in upstream SolarisEngine, modified here (`enabled: false` and the server id removed, so no usage statistics are sent; noted at the top of the file) | MIT (bStats, © 2021 Bastian Oppermann); text in [LICENSES/MIT-bStats.txt](LICENSES/MIT-bStats.txt) |
| Minecraft content in screenshots | — | stored in the repository: the gameplay frames in `docs/assets/` and `capture/docs/source/_static/dataset-collage.gif` | © Mojang / Microsoft, shown under the Minecraft Usage Guidelines; not covered by Apache-2.0 |

## Downloaded or installed at setup

| component | version | how it is obtained | licence |
|---|---|---|---|
| Minecraft (client, server, assets) | 1.21 | the capture setup installs it from Mojang; `scripts/setup_assets.py` reads the block models, the default skins and the air bubble HUD sprite from your client jar | Minecraft EULA (Mojang) |
| Mojang Java runtime (`java-runtime-delta`) | the one Minecraft 1.21 asks for | installed with the camera client by `camera/windows/launch_camera.py` (minecraft-launcher-lib); the cameras run on it | an OpenJDK build: GPL-2.0 with the Classpath Exception |
| Paper server | 1.21 build 130 | the capture setup (`capture/windows/common.py` pins the sha256) | GPL-3.0, inherited from Spigot / CraftBukkit (patches by the contributors listed in Paper's `LICENSE.md` are MIT) |
| ProtocolLib | 5.4.0 | `scripts/setup_assets.py`, from github.com/dmulloy2/ProtocolLib releases, sha256 `ee2e7ab9…5caccf` | GPL-2.0 |
| SkinsRestorer | 15.8.2 | `scripts/setup_assets.py`, from github.com/SkinsRestorer/SkinsRestorer releases, sha256 `7defc4d6…10a2cd` | GPL-3.0 |
| MirrorBot (SolarisEngine's server plugin, extended here) | 1.0 | built from `capture/server/episode-manager-plugin` by `setup_windows.cmd` (Maven) or `capture/server/build_plugin.sh` (Docker) | Apache-2.0 |
| MirrorBot build-time APIs | paper-api 1.21-R0.1-SNAPSHOT, skinsrestorer-api 15.8.2, ProtocolLib 5.4.0 | Maven, at plugin build time only (`provided` scope, not bundled into the jar) | GPL-3.0 (Paper API, SkinsRestorer API), GPL-2.0 (ProtocolLib) |
| Apache Maven, with the maven-compiler, maven-shade and maven-antrun plugins | 3.9.9; plugins 3.10.1, 3.5.0, 3.1.0 | `capture/windows/setup_windows.py` downloads Maven from archive.apache.org if it finds none and checks it against a pinned sha512; Maven fetches the plugins | Apache-2.0 |
| Gradle | 8.14.3 | `gradlew` downloads the distribution at the mod build | Apache-2.0 |
| Fabric Loader | 0.16.14 (`capture/camera/capture-mod/gradle.properties`) | Gradle (mod build), the camera client install | Apache-2.0 |
| Fabric Loom, Mojang's official mappings | Loom 1.10.5 | Gradle, at mod build time only | MIT (Loom); the mappings under Mojang's mappings licence (free to use for development; not to be redistributed complete and unmodified) |
| Mineflayer and npm dependencies | see `capture/package.json` / `package-lock.json` (Mineflayer and mineflayer-pathfinder are pinned GitHub forks) | `npm ci` | package licences: of the 249 packages in `package-lock.json`, 177 MIT, 51 ISC, the rest Apache-2.0, BSD, CC0, BlueOak-1.0.0 or 0BSD. When their install scripts run (the Docker image build does this; the native setup uses `npm ci --ignore-scripts`), canvas 3.2.3 and 2.11.2 download prebuilt binaries that bundle LGPL native libraries (Cairo, Pango, GLib). The lockfile has axios 0.21.4, which has published security advisories; it comes in through `@xboxreplay/xboxlive-auth`, used only for Microsoft logins, which offline capture never does |
| rcon-client | ^4.0.6 (no lockfile) | `npm install` in `capture/server/plugin-starter` | MIT |
| Python packages: numpy, Pillow, OpenCV (headless), PyTorch, PyAV | see `pyproject.toml` | `pip install -e .` | BSD-3-Clause, MIT-CMU, Apache-2.0, BSD-3-Clause, BSD-3-Clause; the OpenCV and PyAV wheels bundle FFmpeg, and PyAV's build includes x264 (GPL-2.0+); CUDA wheels of PyTorch pull NVIDIA-licensed CUDA libraries |
| Capture-side Python packages | see `capture/windows/setup_windows.py` | `setup_windows.cmd` / the Linux setup, into `capture/.venv` | minecraft-launcher-lib 6.5 BSD-2-Clause, PyYAML MIT, tqdm MPL-2.0 and MIT, pywin32 (Windows only) PSF-2.0, plus numpy / OpenCV / Pillow as above |
| FFmpeg (camera encoding) | the latest gyan.dev release | Windows: `setup_windows.py` downloads the gyan.dev essentials build if it finds none. The download is not pinned to a version and not checksum-checked, so two setups can get different versions. Linux: your own | gyan.dev builds are GPL-3.0 |
| Xvfb, VirtualGL (headless Linux only) | — | your distribution / the VirtualGL releases | MIT-style (Xvfb), wxWindows Library Licence 3.1 (VirtualGL) |
| nvdiffrast (optional) | — | `pip install` from github.com/NVlabs/nvdiffrast; without it, `python -m mineengine.cap2proxy --cpu-render` uses the built-in CPU rasteriser, which gives the same channels | Nvidia Source Code License (1-Way Commercial): research or evaluation use only, no commercial use |
| MineSkin web service and `requests` (optional) | — | only `capture/regenerate_skin_cache.py --upload`, which uploads skin PNGs to api.mineskin.org to get Mojang-signed skins (needed only on the Docker path) | MineSkin's terms of service; `requests` Apache-2.0 |
| Docker path only (`capture/run.sh`, `run_laptop.sh`) | see `capture/Dockerfile`, `capture/camera/Dockerfile`, `capture/generate_compose.py`, `capture/requirements-docker.txt` | pulled by Docker, apt and git when the images are built or started: itzg/minecraft-server, nvidia/cuda, node, busybox, alpine (fixes output file ownership), maven:3.9.9-eclipse-temurin-21 (`capture/server/build_plugin.sh`), the Temurin 21 JRE, x11vnc, noVNC, VirtualGL, prismarine-viewer-colalab, and the Python packages of `requirements-docker.txt` (it pins opencv-python-headless 4.6.0.66, an old build whose bundled libwebp has a known security advisory) | NVIDIA Deep Learning Container License (CUDA images), GPL-2.0 (x11vnc), MPL-2.0 (noVNC), MIT (prismarine-viewer-colalab), wxWindows Library Licence 3.1 (VirtualGL), the others as published |
| Documentation build (upstream docs site) | see `capture/docs/requirements.txt`, `capture/docs/package.json` | `pip install -r capture/docs/requirements.txt` (Sphinx, MyST, Furo, sphinx-js and others) and `npm install` in `capture/docs` (jsdoc); not needed for capture | as published by each package |
