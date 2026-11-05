"""Core types of the block-world program (Action, World).

A World is a program that owns the one authoritative state of a scene. Every agent has its own control input and its
own camera; at every render frame all agents' views are drawn from the same state snapshot (same tick), so who is where
and what happened agree across views by construction.

World protocol (subclass World, one file per scene):
  reset(seed)                  start / restart the scene
  agents()                     ids of the agents that have a view (and may be controlled)
  step(actions)                advance one tick; actions = {agent_id: Action}; agents without an action follow the
                               scene's own policy (bot / log replay); returns a list of event dicts
  entities()                   dynamic objects to draw: list[Entity] (agents' bodies included)
  camera(agent_id)             that agent's Camera at the current tick
  static_mesh()                (vertices [V, 3] float, faces [F, 3] int) of the static scene, world units, z up
  prompt()                     text description of the look (shared by all views)
  first_frame(agent_id)        optional real / reference first frame: (video_path, frame_index) or None
  tick_hz, render_hz           program rate and video rate (render_hz must divide tick_hz)
Camera convention: yaw / pitch in degrees, forward =
(cos y cos p, sin y cos p, -sin p), right = (fy, -fx, 0) normalised, up = right x forward; pinhole with hfov / vfov.
"""
import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Camera:
    eye: np.ndarray                    # [3] world position
    yaw: float                         # degrees
    pitch: float                       # degrees, positive looks down
    hfov: float = 106.26               # degrees (a 90-degree 4:3 view widened to 16:9)
    vfov: float = 73.74


@dataclass
class Entity:
    id: int                            # stable id, 0..254 (drawn as id + 1 in the id buffer)
    kind: str                          # shape: "cylinder" | "box"
    pos: np.ndarray                    # [3] base centre (bottom of the shape)
    size: tuple                        # cylinder: (radius, height); box: (sx, sy, sz)
    yaw: float = 0.0                   # degrees (boxes)
    color: tuple = (200, 200, 200)     # proxy colour (e.g. team colour), RGB
    owner: int = -1                    # agent id whose body this is (hidden in that agent's own view), -1 = none
    alive: bool = True
    attrs: dict = field(default_factory=dict)


@dataclass
class Action:
    move: tuple = (0.0, 0.0)           # (forward, right) in [-1, 1]
    dyaw: float = 0.0                  # degrees per tick
    dpitch: float = 0.0
    fire: bool = False
    extra: dict = field(default_factory=dict)


def camera_axes(yaw, pitch):
    y, p = math.radians(float(yaw)), math.radians(float(pitch))
    f = np.array([math.cos(y) * math.cos(p), math.sin(y) * math.cos(p), -math.sin(p)])
    r = np.array([f[1], -f[0], 0.0]); r /= np.linalg.norm(r) + 1e-12
    return f, r, np.cross(r, f)


def project(cam, pts):
    """World points -> normalised (u, v) in [0, 1] (v down) and depth along the view direction."""
    f, r, u = camera_axes(cam.yaw, cam.pitch)
    d = np.asarray(pts, np.float64) - cam.eye
    z = d @ f
    s = np.maximum(z, 1.0)
    th, tv = math.tan(math.radians(cam.hfov) / 2), math.tan(math.radians(cam.vfov) / 2)
    return np.stack([(d @ r / s) / th * 0.5 + 0.5, 0.5 - (d @ u / s) / tv * 0.5], -1), z


class World:
    tick_hz = 32
    render_hz = 16
    name = "world"

    def reset(self, seed=0):
        raise NotImplementedError

    def agents(self):
        raise NotImplementedError

    def step(self, actions):
        raise NotImplementedError

    def entities(self):
        raise NotImplementedError

    def camera(self, agent_id):
        raise NotImplementedError

    def static_mesh(self):
        raise NotImplementedError

    def prompt(self):
        return ""

    def first_frame(self, agent_id):
        return None

    def info(self):
        return {"name": self.name}
