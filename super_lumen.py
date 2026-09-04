#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SUPER LUMEN / AURORA WORLDS                         Single-file edition 1.0
An original five-world 2.5D platformer. No Nintendo assets, code or music.

Ubuntu 24.04 (system Python):
    sudo apt update
    sudo apt install python3-numpy python3-pil libsdl2-2.0-0
    /usr/bin/python3 super_lumen.py

GPU-heavy settings (not a promise of any particular frame rate):
    /usr/bin/python3 super_lumen.py --width 2560 --height 1440 --quality extreme
    /usr/bin/python3 super_lumen.py --fullscreen --quality extreme --scale 1.5

Controls: A/D or arrows move; Space/Z jump (hold for height); Shift run;
X/Ctrl throw fire when powered; S/Down ground-pound in air; Enter confirm;
Esc/P pause; R respawn; M mute; F1 help; F2 quality; F3 statistics;
F11 fullscreen; F12 screenshot. Xbox-style controller supported via SDL2.

All meshes, materials, GLSL, levels, music and effects are generated here.
Pillow is used ONLY to build the font atlas, not to render the game world.
SDL2 is loaded with ctypes; OpenGL calls are batched and instanced.
Renderer: HDR, Cook-Torrance-inspired lighting, PCF shadow maps, screen-space
AO, shadow-sampled volumetric light, bloom pyramid, procedural sky, FXAA,
ACES-style tone mapping. No hardware ray tracing, DLSS or external assets.

Saves: $XDG_DATA_HOME/super_lumen/save.json (normally ~/.local/share/...).
Screenshots and benchmark reports: the same directory. --no-save disables saves.
--self-test runs deterministic game-logic tests without opening a window.
--smoke-test renders every world and checks GL errors (requires a display).
--benchmark 30 runs a 30-second, non-interactive camera tour; no save changes.
--level 1..5 selects a world for direct play, independently of menu unlocks.

Technical references:
https://wiki.libsdl.org/SDL2/SDL_GL_CreateContext
https://wiki.libsdl.org/SDL2/SDL_QueueAudio
https://registry.khronos.org/OpenGL-Refpages/gl4/

This is a playable procedural indie game, not a commercial AAA remaster.
Designed for desktop Linux/OpenGL 3.3+. Python >= 3.10.
"""
from __future__ import annotations

import argparse
import ctypes as C
import ctypes.util
import json
import math
import os
import random
import struct
import sys
import time
import traceback
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict, deque

try:
    import numpy as np
except ImportError:
    raise SystemExit("Manca NumPy. Ubuntu: sudo apt install python3-numpy\n"
                     "Poi avvia con /usr/bin/python3 super_lumen.py")

TAU = math.tau
FIXED_DT = 1.0 / 120.0
GRAVITY = 30.0
JUMP_SPEED = 13.2
RUN_SPEED = 10.2
WALK_SPEED = 6.6
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home()/".local/share"))/"super_lumen"
SAVE_FILE = DATA_DIR / "save.json"
QUALITY = {
    "high": dict(shadow=2048, pcf=1, ao=8, volume=8, cloud=3, scale=1.0),
    "ultra": dict(shadow=4096, pcf=2, ao=16, volume=20, cloud=5, scale=1.0),
    "extreme": dict(shadow=4096, pcf=3, ao=24, volume=40, cloud=6, scale=1.5),
}


def clamp(x, a, b):
    return max(a, min(b, x))


def approach(x, target, step):
    return min(target, x + step) if x < target else max(target, x - step)


def overlap(ax, ay, aw, ah, bx, by, bw, bh):
    return ax < bx+bw and ax+aw > bx and ay < by+bh and ay+ah > by


def vnormalize(v):
    v = np.asarray(v, dtype=np.float32)
    return v / max(float(np.linalg.norm(v)), 1e-8)


def look_at(eye, target, up=(0, 1, 0)):
    eye = np.array(eye, dtype=np.float32)
    f = vnormalize(np.array(target)-eye)
    s = vnormalize(np.cross(f, up))
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[:3, 3] = -m[:3, :3] @ eye
    return m


def ortho(left, right, bottom, top, near, far):
    return np.array([[2/(right-left), 0, 0, -(right+left)/(right-left)],
                     [0, 2/(top-bottom), 0, -(top+bottom)/(top-bottom)],
                     [0, 0, -2/(far-near), -(far+near)/(far-near)],
                     [0, 0, 0, 1]], dtype=np.float32)


@dataclass
class Theme:
    name: str
    subtitle: str
    sky_top: tuple
    sky_bottom: tuple
    sun: tuple
    ground: tuple
    top: tuple
    accent: tuple
    fog: tuple
    ambient: float
    kind: int


THEMES = [
    Theme("GIARDINI DELL'AURORA", "Tra chiome giganti e sentieri sospesi",
          (.10,.32,.61), (.71,.86,.83), (1.0,.82,.57),
          (.38,.23,.18), (.29,.72,.40), (.30,1.0,.79), (.50,.73,.74), .58, 0),
    Theme("GROTTE DI LUCELUNA", "Funghi di luce, rimbalzi e ponti che svaniscono",
          (.024,.028,.12), (.17,.12,.31), (.51,.64,1.0),
          (.19,.17,.29), (.31,.31,.56), (.35,.95,1.0), (.12,.12,.27), .42, 1),
    Theme("OFFICINE DEL CIELO", "Ingranaggi, ascensori e corrente ascensionale",
          (.16,.28,.49), (.94,.64,.36), (1.0,.74,.41),
          (.26,.30,.35), (.62,.47,.27), (1.0,.68,.22), (.55,.43,.38), .54, 2),
    Theme("CATTEDRALE DI BRINA", "Cristalli, ghiaccio vivo e raffiche di neve",
          (.055,.13,.30), (.51,.74,.86), (.71,.84,1.0),
          (.19,.37,.52), (.87,.95,1.0), (.42,.95,1.0), (.39,.64,.76), .57, 3),
    Theme("FORTEZZA DELL'ECLISSE", "Ponti incandescenti e il guardiano del sole",
          (.047,.023,.09), (.54,.15,.12), (1.0,.39,.16),
          (.20,.16,.22), (.34,.30,.35), (1.0,.33,.10), (.28,.11,.15), .35, 4),
]


@dataclass(eq=False)
class Platform:
    x: float
    y: float
    w: float
    h: float = .48
    kind: str = "ledge"
    axis: str = "x"
    amplitude: float = 0.
    speed: float = 1.
    phase: float = 0.
    base_x: float = 0.
    base_y: float = 0.
    old_x: float = 0.
    old_y: float = 0.
    timer: float = -1.
    active: bool = True
    used: bool = False
    payload: str = "coin"
    bump: float = 0.

    def __post_init__(self):
        self.base_x, self.base_y = self.x, self.y
        self.old_x, self.old_y = self.x, self.y

    @property
    def top(self):
        return self.y+self.h

    @property
    def oneway(self):
        return self.kind in ("moving", "phase", "crumble", "ledge", "spring")


@dataclass
class Pickup:
    x: float
    y: float
    kind: str = "coin"
    taken: bool = False
    phase: float = 0.


@dataclass
class Enemy:
    x: float
    y: float
    kind: str = "walker"
    vx: float = -1.9
    vy: float = 0.
    hp: int = 1
    timer: float = 0.
    alive: bool = True
    shell: bool = False
    hurt: float = 0.
    base_y: float = 0.
    phase: int = 0
    vulnerable: float = 0.
    grounded: bool = False

    def __post_init__(self):
        self.base_y = self.y
        if self.kind == "boss":
            self.hp = 7
            self.vx = 0

    @property
    def w(self):
        return 2.2 if self.kind == "boss" else .85

    @property
    def h(self):
        if self.kind == "boss":
            return 2.5
        return .48 if self.shell else .86


@dataclass
class Projectile:
    x: float
    y: float
    vx: float
    vy: float
    hostile: bool = False
    life: float = 3.
    radius: float = .16


@dataclass
class Particle:
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    life: float
    maxlife: float
    size: float
    color: tuple
    glow: float = 0.


@dataclass
class Player:
    x: float = 3.
    y: float = .05
    vx: float = 0.
    vy: float = 0.
    power: int = 1
    facing: int = 1
    grounded: bool = False
    support: Platform | None = None
    coyote: float = 0.
    jump_buffer: float = 0.
    jump_cut: bool = False
    invuln: float = 0.
    star_time: float = 0.
    fire_cool: float = 0.
    pound: bool = False
    squash: float = 0.
    last_y: float = 0.

    @property
    def w(self):
        return .66

    @property
    def h(self):
        return 1.12 if self.power == 1 else 1.52


@dataclass
class Controls:
    axis: float = 0.
    jump: bool = False
    jump_pressed: bool = False
    run: bool = False
    fire: bool = False
    down: bool = False


class Level:
    """Authored macro-layouts with deterministic decorative/coin placement."""
    def __init__(self, index):
        self.index = index
        self.theme = THEMES[index]
        self.length = [199., 207., 217., 222., 234.][index]
        self.platforms: list[Platform] = []
        self.pickups: list[Pickup] = []
        self.enemies: list[Enemy] = []
        self.hazards: list[tuple] = []
        self.checkpoints: list[tuple] = []
        self.checkpoint_id = -1
        self.exit_x = self.length-5
        self.boss: Enemy | None = None
        self.build()

    def ground(self, a, b, y=0.):
        self.platforms.append(Platform(a, y-4., b-a, 4., "ground"))

    def ledge(self, x, top, w=3., kind="ledge", **kwargs):
        p = Platform(x, top-.48, w, .48, kind, **kwargs)
        self.platforms.append(p)
        return p

    def block(self, x, top, payload="coin", brick=False):
        p = Platform(x, top-.95, .95, .95, "brick" if brick else "question",
                     payload=payload)
        self.platforms.append(p)
        return p

    def coins(self, a, b, y, arc=0.):
        n = max(2, int((b-a)/1.4)+1)
        for i in range(n):
            f = i/(n-1)
            self.pickups.append(Pickup(a+(b-a)*f, y+math.sin(f*math.pi)*arc,
                                       phase=i*.7))

    def star(self, x, y):
        self.pickups.append(Pickup(x, y, "shard"))

    def build(self):
        i = self.index
        gaps = [
            [(27,31),(49,53),(78,83),(108,113),(139,144),(170,175)],
            [(24,29),(54,59),(86,91),(117,122),(150,155),(181,186)],
            [(28,33),(60,65),(93,99),(127,133),(161,167),(192,198)],
            [(30,35),(63,68),(96,101),(130,135),(163,168),(194,199)],
            [(27,32),(57,62),(89,94),(121,126),(153,158),(180,185)],
        ][i]
        start = -10.
        for a,b in gaps:
            self.ground(start, a)
            self.coins(a-.3,b+.3,1.4,1.8)
            start=b
        self.ground(start, self.length+12)
        for a,b in gaps:
            if i == 1:
                self.ledge(a+.6, -.1, b-a-1.2, "phase", phase=a*.17)
            elif i == 2:
                self.ledge(a+.5, .8, 2.8, "moving", axis="y", amplitude=1.1,
                           speed=1.05, phase=a*.09)
            elif i == 4:
                self.hazards.append((a,-.4,b-a,.8,"lava",0.))
        # Two optional upper routes and an elevated final secret in each world.
        for base in (10., 66., 143.):
            self.ledge(base, 1.8, 3.5)
            self.ledge(base+4.8, 3.5, 3.3)
            self.ledge(base+9.5, 5.1, 4.1,
                       "crumble" if i in (2,3,4) else "ledge")
            self.coins(base+1,base+12.5,6.0)
            self.star(base+11.6,6.3)
        self.block(18,3.35,"grow")
        self.block(20,3.35,"coin",True)
        self.block(21,3.35,"coin")
        self.block(37,3.5,"fire")
        self.block(39,3.5,"coin",True)
        self.block(72,3.2,"star")
        self.block(102,3.35,"grow")
        self.block(134 if i==0 else 137,3.35,"fire")
        self.block(self.length-(25 if i==4 else 17),3.5,"fire")
        for x in (43., 98., 173.):
            self.pickups.append(Pickup(x,1.1,"grow"))
        # Generous, safe checkpoints before the harder set pieces.
        safe_cp = [[59,118,179],[64,127,189],[70,139,202],
                   [73,140,203],[66,132,191]][i]
        self.checkpoints = [(float(x),0.) for x in safe_cp]
        # Main-route enemies: placement avoids gap edges and checkpoint poles.
        xs = [[23,43,67,94,124,157,186], [19,43,72,103,137,169,197],
              [23,47,78,113,147,179,209], [22,51,83,113,148,181,213],
              [20,45,77,106,141,170]][i]
        for j,x in enumerate(xs):
            kind = ["walker","turtle","hopper"][j%3]
            if i in (1,2) and j%3 == 2:
                kind="flyer"
            self.enemies.append(Enemy(float(x),2.3 if kind=="flyer" else .03,
                                      kind, timer=j*.63))
        # World-specific set pieces, intentionally not random level generation.
        if i == 0:
            self.ledge(88, .28, 1.3, "spring")
            self.ledge(89.5,4.4,6.,"moving",amplitude=2.,speed=.7)
            self.coins(88.5,98.5,5.5,1.2)
            self.ledge(157,2.6,4.,"moving",axis="y",amplitude=1.0,speed=.85)
        elif i == 1:
            for j in range(4):
                self.ledge(95+j*4.1,1.8+(j%2)*1.3,3.,"phase",phase=j*.9)
            self.ledge(33,.25,1.4,"spring")
            self.ledge(35,4.4,6.)
            self.coins(35,40,5.3)
            self.hazards.extend([(79,.0,2.,.6,"spikes",0.),
                                 (163,.0,2.,.6,"spikes",0.)])
        elif i == 2:
            for j in range(4):
                self.ledge(106+j*4.4,2.+(j%2),3.2,"moving",axis="y",
                           amplitude=1.1,speed=.85,phase=j*1.6)
            self.ledge(172,.3,1.4,"spring")
            self.hazards.extend([(83,.0,2.3,.65,"spikes",0.),
                                 (151,.0,2.,.65,"spikes",0.)])
        elif i == 3:
            for j in range(5):
                self.ledge(105+j*3.6,2.4+j*.30,3.0,"crumble")
            self.hazards.extend([(55,.0,2.,.65,"spikes",0.),
                                 (153,.0,2.,.65,"spikes",0.),
                                 (185,.0,2.,.65,"spikes",0.)])
            self.ledge(171,.3,1.4,"spring")
        elif i == 4:
            for j,x in enumerate((39.,73.,103.,137.,165.)):
                self.hazards.append((x,0.,1.5,3.1,"vent",j*.8))
            for j in range(4):
                self.ledge(96+j*4.3,2.4+(j%2)*.8,3.,"crumble")
            self.boss=Enemy(217.,0.,"boss")
            self.enemies.append(self.boss)
            self.pickups.append(Pickup(196,1.1,"fire"))
            self.ledge(201,1.6,3.0)
            self.ledge(224,1.6,3.0)
        for p in self.platforms:
            if p.kind=="ground" and p.w>10:
                self.coins(p.x+4,p.x+p.w-3,1.0)
        # Never place a ground-level coin inside a damage volume.
        self.pickups = [p for p in self.pickups if p.kind!="coin" or not any(
            h[0]-.4 < p.x < h[0]+h[2]+.4 and p.y<h[3]+.6 for h in self.hazards)]

    def update(self, t, dt, player):
        for p in self.platforms:
            p.old_x,p.old_y=p.x,p.y
            p.bump=max(0.,p.bump-dt)
            if p.kind=="moving":
                off=math.sin(t*p.speed+p.phase)*p.amplitude
                if p.axis=="x": p.x=p.base_x+off
                else: p.y=p.base_y+off
            elif p.kind=="phase":
                # Player already standing on a bridge gets a grace period.
                p.active=(t+p.phase)%4.8 < 3.45 or player.support is p
            elif p.kind=="crumble" and p.timer>=0:
                p.timer+=dt
                p.active=p.timer<.65
                if p.timer>4.5:
                    p.timer=-1.;p.active=True

    def vent_active(self, h, t):
        return (t+h[5])%3.7 > 2.2


class SilentAudio:
    def effect(self, name): pass
    def update(self, *args): pass
    def close(self): pass
    muted=False


class Game:
    def __init__(self, save=True, audio=None):
        self.audio=audio or SilentAudio()
        self.save_enabled=save
        self.unlocked=1
        self.best={}
        if save:
            try:
                data=json.loads(SAVE_FILE.read_text())
                self.unlocked=clamp(int(data.get("unlocked",1)),1,5)
                self.best=data.get("best",{}) if isinstance(data.get("best",{}),dict) else {}
            except (OSError,ValueError,TypeError): pass
        self.selected=0
        self.state="menu"
        self.help=False
        self.stats=False
        self.score=0
        self.coins=0
        self.lives=5
        self.total_shards=0
        self.rng=random.Random(4617)
        self.time=0.
        self.fx_time=0.
        self.elapsed=0.
        self.cam_x=9.
        self.cam_y=4.
        self.shake=0.
        self.toast=""
        self.toast_time=0.
        self.banner=0.
        self.dead_time=0.
        self.level_shards=0
        self.last_complete=None
        self.particles=[]
        self.projectiles=[]
        self.load_level(0)

    def save(self):
        if not self.save_enabled: return
        try:
            DATA_DIR.mkdir(parents=True,exist_ok=True)
            tmp=SAVE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version":1,"unlocked":self.unlocked,
                                       "best":self.best},indent=2))
            tmp.replace(SAVE_FILE)
        except OSError as exc:
            print("Salvataggio non disponibile:",exc,file=sys.stderr)

    def load_level(self, index):
        self.level=Level(index)
        self.player=Player()
        self.particles=[]
        self.projectiles=[]
        self.elapsed=0.
        self.level_shards=0
        self.pending_power=0
        self.time=0.
        self.banner=3.7
        self.cam_x=9.
        self.cam_y=4.
        self.toast_time=0.
        self.dead_time=0.

    def start(self, index=0):
        self.score=0;self.coins=0;self.lives=5;self.total_shards=0
        self.load_level(index)
        self.state="play"

    def message(self, text, duration=2.6):
        self.toast=text;self.toast_time=duration

    def burst(self,x,y,color,n=18,force=3.,glow=1.):
        for _ in range(n):
            life=self.rng.uniform(.3,.8)
            self.particles.append(Particle(x,y,self.rng.uniform(-.2,.7),
                self.rng.uniform(-force,force),self.rng.uniform(.3,force*1.5),
                self.rng.uniform(-1.5,1.5),life,life,self.rng.uniform(.045,.12),color,glow))
        if len(self.particles)>650:
            del self.particles[:-650]

    def add_coin(self,x,y):
        self.coins+=1;self.score+=100
        if self.coins%100==0:
            self.lives+=1
            self.message("100 MONETE  /  VITA EXTRA")
        self.audio.effect("coin")
        self.burst(x,y,(1.,.77,.20),8,1.6,2.)

    def promote(self,desired):
        """Grow without clipping a ceiling or the floor; defer in a tight tunnel."""
        p=self.player
        new_height=1.52
        def room(y):
            return not any(b.active and not b.oneway and overlap(
                p.x+.001,y+.001,p.w-.002,new_height-.002,b.x,b.y,b.w,b.h)
                for b in self.level.platforms)
        if room(p.y):
            p.power=max(p.power,desired);self.pending_power=0;return True
        lowered=p.y-max(0.,new_height-p.h)
        if room(lowered):
            p.y=lowered;p.power=max(p.power,desired);self.pending_power=0;return True
        self.pending_power=max(self.pending_power,desired)
        return False

    def apply_power(self,kind):
        p=self.player
        if kind=="grow":
            if p.power<2: self.promote(2)
            else: self.score+=300
            self.message("LUMEN POTENZIATO  /  UN COLPO IN PIU'")
        elif kind=="fire":
            self.promote(3)
            self.message("FIAMMA SOLARE  /  X O CTRL PER SPARARE")
        elif kind=="star":
            p.star_time=10.
            self.message("AURA STELLARE  /  INVULNERABILE PER 10 SECONDI")
        self.audio.effect("power")
        self.burst(p.x+.3,p.y+.8,(.4,1.,.85),24,4.,3.)

    def hit_block(self,b):
        p=self.player
        if b.kind not in ("question","brick") or b.used: return
        b.bump=.23
        if b.kind=="brick" and p.power>=2:
            b.used=True;b.active=False
            self.burst(b.x+.5,b.y+.5,(.72,.38,.2),22,4.,0.)
            self.audio.effect("break")
            self.score+=50
        elif b.kind=="question":
            b.used=True
            if b.payload=="coin": self.add_coin(b.x+.5,b.top+.6)
            else: self.apply_power(b.payload)
        else:
            self.audio.effect("bump")

    def damage(self, source_x):
        p=self.player
        if p.invuln>0 or p.star_time>0 or self.state!="play": return
        if p.power>1:
            p.power=1;p.invuln=2.2
            p.vx=5. if p.x>source_x else -5.
            p.vy=5.
            p.grounded=False;p.support=None
            self.shake=.26
            self.audio.effect("hurt")
        else: self.die()

    def die(self):
        if self.state!="play": return
        self.state="dead";self.dead_time=1.25
        self.player.vy=9.;self.player.support=None
        self.player.pound=False
        self.lives-=1
        self.shake=.45
        self.audio.effect("hurt")

    def respawn(self):
        cp=self.level.checkpoint_id
        x,y=self.level.checkpoints[cp] if cp>=0 else (3.,0.)
        self.player=Player(x=x,y=y+.05,invuln=2.5)
        self.pending_power=0
        self.projectiles=[]
        self.cam_x=max(9.,x+3)
        self.cam_y=4.
        # Reset crumbling bridges; moving platform phase is intentionally kept.
        for b in self.level.platforms:
            if b.kind=="crumble": b.timer=-1.;b.active=True
        if self.level.boss and self.level.boss.alive:
            b=self.level.boss
            b.hp=7;b.x=217.;b.y=0.;b.timer=0.;b.phase=0
            b.vulnerable=0.;b.hurt=0.;b.vy=0.
        self.state="play"

    def complete(self):
        if self.state!="play": return
        i=self.level.index
        self.total_shards+=self.level_shards
        self.score+=1000+self.level_shards*500
        old=self.best.get(str(i),{})
        self.best[str(i)]={"time":min(float(old.get("time",1e9)),self.elapsed),
                           "shards":max(int(old.get("shards",0)),self.level_shards)}
        self.unlocked=max(self.unlocked,min(5,i+2))
        self.save()
        self.state="victory" if i==4 else "complete"
        self.audio.effect("win")
        self.burst(self.player.x,self.player.y+1.5,(1.,.75,.2),90,8.,3.)

    def next_level(self):
        if self.state=="complete":
            power=self.player.power
            self.load_level(self.level.index+1)
            self.player.power=power
            self.state="play"
        elif self.state=="victory":
            self.state="menu";self.load_level(0)

    def move_actor(self,a,dt,player=False):
        old_y=a.y
        blocks=[b for b in self.level.platforms if b.active and
                b.x<a.x+a.w+3 and b.x+b.w>a.x-3]
        a.x+=a.vx*dt
        for b in blocks:
            if b.oneway: continue
            if overlap(a.x,a.y+.04,a.w,a.h-.08,b.x,b.y,b.w,b.h):
                a.x=b.x-a.w if a.vx>0 else b.x+b.w
                if player: a.vx=0.
                else: a.vx=-a.vx
        a.y+=a.vy*dt
        a.grounded=False
        if player: a.support=None
        # Highest valid landing first, independent of construction order.
        landings=[]
        for b in blocks:
            if a.x+a.w<=b.x+.025 or a.x>=b.x+b.w-.025: continue
            if a.vy<=0 and old_y>=b.old_y+b.h-.14 and a.y<=b.top:
                if a.y+a.h>b.y: landings.append(b)
            elif not b.oneway and a.vy>0 and old_y+a.h<=b.y+.12 and a.y+a.h>=b.y:
                a.y=b.y-a.h;a.vy=0.
                if player: self.hit_block(b)
        if landings:
            b=max(landings,key=lambda q:q.top)
            impact=a.vy
            a.y=b.top;a.vy=0.;a.grounded=True
            if player:
                a.support=b
                a.jump_cut=False
                if impact < -4:
                    a.squash=.16
                    self.burst(a.x+.3,a.y+.05,self.level.theme.top,8,1.8,0.)
                if b.kind=="crumble" and b.timer<0: b.timer=0.
                if b.kind=="spring":
                    a.vy=18.2;a.grounded=False;a.support=None
                    b.bump=.30;self.audio.effect("jump")
                if a.pound:
                    a.pound=False;self.shake=.18
                    self.burst(a.x+.3,a.y+.1,(1.,.72,.2),24,4.,1.)
                    for e in self.level.enemies:
                        if e.alive and e.kind!="boss" and abs(e.x-a.x)<2.4 and abs(e.y-a.y)<1.4:
                            self.kill_enemy(e)
        if player: a.last_y=old_y

    def kill_enemy(self,e):
        if not e.alive: return
        e.alive=False
        self.score+=300 if e.kind!="boss" else 3000
        self.audio.effect("stomp")
        self.burst(e.x+e.w/2,e.y+e.h/2,(1.,.53,.2),20,3.,1.)
        if e.kind=="boss":
            self.shake=.7
            self.message("IL SOLE E' LIBERO! RAGGIUNGI IL PORTALE",4.)
            self.audio.effect("win")

    def hit_boss(self,e):
        if e.hurt>0 or e.vulnerable<=0: return False
        e.hp-=1;e.hurt=.8
        self.shake=.22
        self.audio.effect("stomp")
        self.burst(e.x+1,e.y+2,(1.,.84,.3),30,5.,3.)
        if e.hp<=0: self.kill_enemy(e)
        return True

    def update_boss(self,e,dt):
        p=self.player
        if p.x<195: return
        e.timer+=dt
        e.vulnerable=max(0.,e.vulnerable-dt)
        if e.phase==0:  # obvious charging pose / warning ring
            e.vx=0.
            if e.timer>1.25:
                e.phase=1;e.timer=0.
                e.vy=12.5
                e.vx=clamp((p.x-e.x)*1.3,-7.,7.)
        elif e.phase==1:
            e.vy-=GRAVITY*dt
            self.move_actor(e,dt)
            e.x=clamp(e.x,199.,225.)
            if e.grounded:
                e.phase=2;e.timer=0.;e.vulnerable=2.15;e.vx=0.
                self.shake=.3
                self.burst(e.x+1,.15,(1.,.32,.08),40,6.,2.)
                # Ground shockwaves can be jumped and are telegraphed by the leap.
                for direction in (-1,1):
                    self.projectiles.append(Projectile(e.x+1,.27,direction*6.,0.,True,3.,.24))
        elif e.phase==2:
            if e.timer>2.3:
                e.phase=3;e.timer=0.
        elif e.phase==3:
            if e.timer> .55:
                e.phase=0;e.timer=0.
                count=3 if e.hp>3 else 5
                for j in range(count):
                    self.projectiles.append(Projectile(e.x+1,e.y+1.8,
                        (-1 if p.x<e.x else 1)*(4.+j*.65),2.+j*.7,True,3.4,.18))

    def update_enemies(self,dt):
        p=self.player
        for e in self.level.enemies:
            if not e.alive or abs(e.x-p.x)>32: continue
            e.hurt=max(0.,e.hurt-dt)
            if e.kind=="boss": self.update_boss(e,dt)
            elif e.kind=="flyer":
                e.timer+=dt
                e.x+=math.cos(e.timer*.8)*dt*1.8
                e.y=e.base_y+math.sin(e.timer*2.)*.9
            else:
                e.timer+=dt
                if e.kind=="hopper" and e.grounded and e.timer>1.7:
                    e.vy=8.;e.timer=0.
                if e.grounded and not(e.shell and abs(e.vx)>3):
                    ahead=e.x+(e.w+.22 if e.vx>0 else -.22)
                    floor=any(b.active and b.x<=ahead<=b.x+b.w and
                              abs(b.top-e.y)<.30 for b in self.level.platforms)
                    if not floor: e.vx=-e.vx
                e.vy=max(-25,e.vy-GRAVITY*dt)
                self.move_actor(e,dt)
                if e.y < -8: e.alive=False
            if not e.alive: continue
            if e.shell and abs(e.vx)>3:
                for other in self.level.enemies:
                    if other is not e and other.alive and other.kind!="boss" and overlap(
                        e.x,e.y,e.w,e.h,other.x,other.y,other.w,other.h):
                        self.kill_enemy(other)
            if overlap(p.x,p.y,p.w,p.h,e.x,e.y,e.w,e.h):
                if p.star_time>0:
                    if e.kind=="boss": self.hit_boss(e)
                    else: self.kill_enemy(e)
                    continue
                stomp=p.vy<-.4 and p.last_y>=e.y+e.h-.30
                if e.kind=="boss":
                    if stomp and self.hit_boss(e):
                        p.vy=10.5;p.y=e.y+e.h+.05;p.grounded=False;p.jump_cut=False
                    else: self.damage(e.x)
                elif stomp:
                    if e.kind=="turtle":
                        if not e.shell:
                            e.shell=True;e.vx=0.
                        elif abs(e.vx)>1: e.vx=0.
                        else:
                            e.vx=11*p.facing;e.hurt=.3
                    else: self.kill_enemy(e)
                    p.vy=9. if not p.pound else 11.
                    p.pound=False;p.grounded=False;p.support=None;p.jump_cut=False
                    self.audio.effect("stomp")
                elif e.shell and abs(e.vx)<1:
                    e.vx=11*p.facing;e.hurt=.3
                elif e.hurt<=0: self.damage(e.x)

    def update_projectiles(self,dt):
        p=self.player
        for s in self.projectiles:
            s.life-=dt
            old_y=s.y
            s.x+=s.vx*dt
            s.y+=s.vy*dt
            if not s.hostile: s.vy-=19*dt
            elif s.vy!=0: s.vy-=5*dt
            for b in self.level.platforms:
                if not b.active or not (b.x < s.x < b.x+b.w): continue
                if s.vy<0 and old_y-s.radius>=b.top-.06 and s.y-s.radius<b.top:
                    if s.hostile: s.life=0
                    else: s.y=b.top+s.radius;s.vy=6.0
                elif not b.oneway and b.y<s.y<b.top: s.life=0
            if s.hostile:
                if overlap(s.x-s.radius,s.y-s.radius,s.radius*2,s.radius*2,p.x,p.y,p.w,p.h):
                    self.damage(s.x);s.life=0
            else:
                for e in self.level.enemies:
                    if e.alive and overlap(s.x-s.radius,s.y-s.radius,s.radius*2,s.radius*2,e.x,e.y,e.w,e.h):
                        if e.kind=="boss": self.hit_boss(e)
                        else: self.kill_enemy(e)
                        s.life=0;break
        self.projectiles=[s for s in self.projectiles if s.life>0 and abs(s.x-p.x)<38 and s.y>-7]

    def tick(self,dt,c):
        self.fx_time+=dt
        if self.state not in ("pause",):
            for q in self.particles:
                q.life-=dt;q.x+=q.vx*dt;q.y+=q.vy*dt;q.z+=q.vz*dt;q.vy-=7*dt
            self.particles=[q for q in self.particles if q.life>0]
        self.shake=max(0.,self.shake-dt)
        if self.state=="menu":
            self.time+=dt
            self.level.update(self.time,dt,self.player)
            return
        if self.state=="dead":
            self.player.y+=self.player.vy*dt
            self.player.vy-=GRAVITY*dt
            self.dead_time-=dt
            if self.dead_time<=0:
                if self.lives<=0: self.state="gameover"
                else: self.respawn()
            return
        if self.state!="play": return
        self.time+=dt;self.elapsed+=dt
        self.banner=max(0.,self.banner-dt)
        self.toast_time=max(0.,self.toast_time-dt)
        p=self.player
        old_support=p.support
        self.level.update(self.time,dt,p)
        if old_support is not None and old_support.active:
            p.x+=old_support.x-old_support.old_x
            p.y+=old_support.y-old_support.old_y
        p.invuln=max(0.,p.invuln-dt)
        p.star_time=max(0.,p.star_time-dt)
        p.fire_cool=max(0.,p.fire_cool-dt)
        p.squash=max(0.,p.squash-dt)
        p.coyote=.10 if p.grounded else max(0.,p.coyote-dt)
        p.jump_buffer=.13 if c.jump_pressed else max(0.,p.jump_buffer-dt)
        ice=self.level.index==3 and p.grounded
        speed=RUN_SPEED if c.run else WALK_SPEED
        target=c.axis*speed
        accel=(13. if ice else 48.) if p.grounded else 29.
        if c.axis==0: accel=5.5 if ice else (44. if p.grounded else 13.)
        p.vx=approach(p.vx,target,accel*dt)
        if c.axis: p.facing=1 if c.axis>0 else -1
        if self.level.index==2 and 99<p.x<133 and not p.grounded:
            p.vx+=math.sin(self.time*.8)*2.4*dt
            # Gentle updraft without changing the double-jump rules.
            p.vy+=5*dt
        if p.jump_buffer>0 and p.coyote>0:
            p.vy=JUMP_SPEED;p.grounded=False;p.support=None;p.coyote=0.
            p.jump_buffer=0.;p.pound=False;p.jump_cut=True
            self.audio.effect("jump")
        if p.jump_cut and not c.jump and p.vy>4.8:
            p.vy=4.8;p.jump_cut=False
        if c.down and not p.grounded and p.vy<1:
            p.pound=True;p.vy=-21.
        if c.fire and p.power==3 and p.fire_cool<=0:
            active=sum(not s.hostile for s in self.projectiles)
            if active<4:
                self.projectiles.append(Projectile(p.x+.3+p.facing*.45,p.y+p.h*.58,
                                                   p.facing*13.,2.5))
                p.fire_cool=.23;self.audio.effect("fire")
        p.vy=max(-27.,p.vy-GRAVITY*dt)
        self.move_actor(p,dt,True)
        if self.pending_power: self.promote(self.pending_power)
        p.x=clamp(p.x,-1.,self.level.length)
        if p.y < -7.5: self.die();return
        for pickup in self.level.pickups:
            if pickup.taken: continue
            if abs(pickup.x-(p.x+p.w/2))<.65 and p.y-.4<pickup.y<p.y+p.h+.4:
                pickup.taken=True
                if pickup.kind=="coin": self.add_coin(pickup.x,pickup.y)
                elif pickup.kind=="shard":
                    self.level_shards+=1;self.score+=700
                    self.burst(pickup.x,pickup.y,(.4,1.,.9),45,5.,3.)
                    self.audio.effect("shard")
                    self.message(f"FRAMMENTO AURORA  /  {self.level_shards} DI 3")
                else: self.apply_power(pickup.kind)
        for j,(x,y) in enumerate(self.level.checkpoints):
            if j>self.level.checkpoint_id and abs(p.x-x)<1.2 and p.y<y+2:
                self.level.checkpoint_id=j
                self.message("CHECKPOINT ATTIVATO")
                self.audio.effect("checkpoint")
                self.burst(x,y+1.5,(.3,1.,.8),30,3.,2.)
        for h in self.level.hazards:
            x,y,w,hh,kind,phase=h
            if kind=="vent" and not self.level.vent_active(h,self.time): continue
            if overlap(p.x+.1,p.y+.05,p.w-.2,p.h-.1,x,y,w,hh):
                if kind=="lava": self.die()
                else: self.damage(x+w/2)
        if self.state!="play": return
        self.update_enemies(dt)
        if self.state!="play": return
        self.update_projectiles(dt)
        if p.x>self.level.exit_x and (not self.level.boss or not self.level.boss.alive):
            self.complete()
        self.cam_x+=(clamp(p.x+3.8,9.,self.level.length-9)-self.cam_x)*(1-math.exp(-5*dt))
        self.cam_y+=(clamp(p.y+2.2,4.,7.8)-self.cam_y)*(1-math.exp(-3.4*dt))

# ---------------------------------------------------------------------------
# Native SDL2 window, input and queued procedural audio. No Python SDL wrapper.
# ---------------------------------------------------------------------------
class SDL:
    def __init__(self):
        path=ctypes.util.find_library("SDL2-2.0") or "libSDL2-2.0.so.0"
        try: self.lib=C.CDLL(path)
        except OSError as exc:
            raise RuntimeError("SDL2 non disponibile: sudo apt install libsdl2-2.0-0") from exc
        def bind(name, ret, *args):
            f=getattr(self.lib,"SDL_"+name);f.restype=ret;f.argtypes=list(args)
            setattr(self,name,f)
        I,U,P,S=C.c_int,C.c_uint32,C.c_void_p,C.c_char_p
        bind("Init",I,U);bind("Quit",None);bind("GetError",S)
        bind("GL_SetAttribute",I,I,I);bind("GL_GetProcAddress",P,S)
        bind("CreateWindow",P,S,I,I,I,I,U);bind("DestroyWindow",None,P)
        bind("GL_CreateContext",P,P);bind("GL_DeleteContext",None,P)
        bind("GL_SetSwapInterval",I,I);bind("GL_SwapWindow",None,P)
        bind("GL_GetDrawableSize",None,P,C.POINTER(I),C.POINTER(I))
        bind("SetWindowFullscreen",I,P,U);bind("SetWindowTitle",None,P,S)
        bind("PollEvent",I,P);bind("GetKeyboardState",C.POINTER(C.c_uint8),C.POINTER(I))
        bind("GetWindowFlags",U,P);bind("SetHint",I,S,S)
        bind("GetNumAudioDevices",I,I)
        bind("OpenAudioDevice",U,S,I,P,P,I);bind("CloseAudioDevice",None,U)
        bind("PauseAudioDevice",None,U,I);bind("QueueAudio",I,U,P,U)
        bind("GetQueuedAudioSize",U,U);bind("ClearQueuedAudio",None,U)
        bind("NumJoysticks",I);bind("IsGameController",I,I)
        bind("GameControllerOpen",P,I);bind("GameControllerClose",None,P)
        bind("GameControllerGetAttached",I,P)
        bind("GameControllerGetAxis",C.c_int16,P,I)
        bind("GameControllerGetButton",C.c_uint8,P,I)

    def error(self):
        return (self.GetError() or b"Errore SDL").decode("utf-8","replace")


class AudioSpec(C.Structure):
    _fields_=[("freq",C.c_int),("format",C.c_uint16),("channels",C.c_uint8),
              ("silence",C.c_uint8),("samples",C.c_uint16),("padding",C.c_uint16),
              ("size",C.c_uint32),("callback",C.c_void_p),("userdata",C.c_void_p)]


class SynthAudio:
    def __init__(self,sdl,disabled=False):
        self.sdl=sdl;self.device=0;self.muted=disabled
        self.rate=44100;self.cursor=0;self.voices=[]
        self.rng=np.random.default_rng(123)
        self.cache={}
        self.last_fx={}
        if disabled: return
        desired=AudioSpec(self.rate,0x8010,2,0,1024,0,0,None,None)
        obtained=AudioSpec()
        self.device=sdl.OpenAudioDevice(None,0,C.byref(desired),C.byref(obtained),0)
        if not self.device:
            print("Audio non disponibile; continuo senza audio:",sdl.error())
            return
        sdl.PauseAudioDevice(self.device,0)

    def effect(self,name):
        if not self.device or self.muted: return
        now=time.monotonic()
        if now-self.last_fx.get(name,-10)<.045: return
        self.last_fx[name]=now
        if name not in self.cache:
            duration={"jump":.17,"coin":.14,"power":.6,"shard":.7,"win":1.2,
                      "hurt":.42,"fire":.12,"stomp":.16,"checkpoint":.5,
                      "break":.2,"bump":.08}.get(name,.2)
            t=np.arange(int(duration*self.rate),dtype=np.float32)/self.rate
            if name=="jump":
                signal=np.sin(TAU*(200*t+1800*t*t))*.25
            elif name in ("coin","shard","checkpoint","power","win"):
                base={"coin":990,"shard":660,"checkpoint":520,"power":440,"win":523}[name]
                steps=np.minimum((t/duration*6).astype(int),5)
                freq=base*np.array([1,1.25,1.5,2,1.5,2.],dtype=np.float32)[steps]
                phase=np.cumsum(freq)/self.rate*TAU
                signal=(np.sin(phase)+.3*np.sin(phase*2))*.19
            elif name in ("hurt","break"):
                signal=(self.rng.uniform(-1,1,len(t))*.45+
                        np.sin(TAU*(130*t-65*t*t))*.3)*np.exp(-7*t)
            elif name=="fire":
                signal=np.sin(TAU*(600*t-1700*t*t))*.24
            else:
                signal=np.sin(TAU*(160*t-300*t*t))*.34
            envelope=np.minimum(t/.008,1)*np.power(np.maximum(1-t/duration,0),1.4)
            self.cache[name]=(signal*envelope).astype(np.float32)
        self.voices.append([self.cache[name],0])
        self.voices=self.voices[-18:]

    def update(self,world,state):
        if not self.device: return
        if self.muted:
            self.sdl.ClearQueuedAudio(self.device)
            self.voices=[]
            return
        target_bytes=3072*4
        loops=0
        while self.sdl.GetQueuedAudioSize(self.device)<target_bytes and loops<4:
            loops+=1;n=1024
            t=(np.arange(n,dtype=np.float64)+self.cursor)/self.rate
            bpm=[108,92,122,88,130][world]
            beat=t*bpm/60
            step=np.floor(beat*2).astype(np.int64)
            # Original pentatonic melody and chord progression.
            melody=np.array([0,7,12,7,4,7,9,4,2,7,11,7,4,9,12,16])
            root=[48,45,50,47,43][world]
            chord=np.array([0,5,9,7])[(step//16)%4]
            freq=440*2.**((root+12+melody[step%16]+chord-69)/12.)
            phase=(beat*2)%1
            note=np.sin(TAU*freq*t)+.20*np.sin(TAU*freq*t*2)
            music=note*np.minimum(phase*35,1)*np.exp(-phase*5)*.055
            bass=440*2.**((root+chord-69)/12.)
            music+=np.sin(TAU*bass*t)*.045*np.exp(-(beat%1)*3)
            # Soft kick and a fine shaker, entirely synthesized.
            kickpos=beat%1
            music+=np.sin(TAU*(52*t+np.exp(-kickpos*15)*.05))*np.exp(-kickpos*22)*.055
            music+=self.rng.uniform(-1,1,n)*np.exp(-((beat*2)%1)*25)*.012
            if state=="pause": music*=.35
            if state in ("dead","gameover"): music*=.2
            for voice in self.voices:
                data,pos=voice
                take=min(n,len(data)-pos)
                if take>0: music[:take]+=data[pos:pos+take]
                voice[1]+=take
            self.voices=[v for v in self.voices if v[1]<len(v[0])]
            stereo=np.repeat((np.tanh(music)*24000).astype(np.int16)[:,None],2,axis=1)
            self.sdl.QueueAudio(self.device,C.c_void_p(stereo.ctypes.data),stereo.nbytes)
            self.cursor+=n

    def close(self):
        if self.device:
            self.sdl.CloseAudioDevice(self.device);self.device=0


class Window:
    def __init__(self,args):
        self.sdl=SDL()
        self.handle=None;self.context=None;self.controller=None
        self.closed=False;self.fullscreen=args.fullscreen
        self.jump_was_down=False;self.pad_buttons=set()
        self.sdl.SetHint(b"SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR",b"0")
        if self.sdl.Init(0x20|0x2000)!=0:
            raise RuntimeError("Inizializzazione SDL fallita: "+self.sdl.error())
        # Audio is optional; failure must never prevent playing.
        self.sdl.Init(0x10)
        for attr,value in ((17,3),(18,3),(21,1),(5,1),(6,24),(7,8)):
            self.sdl.GL_SetAttribute(attr,value)
        flags=0x2|0x20|0x2000  # OPENGL | RESIZABLE | ALLOW_HIGHDPI
        if args.fullscreen: flags|=0x1001
        if args.smoke_test: flags|=0x8
        self.handle=self.sdl.CreateWindow(b"SUPER LUMEN / Aurora Worlds",0x2FFF0000,
                                         0x2FFF0000,args.width,args.height,flags)
        if not self.handle:
            self.sdl.Quit()
            raise RuntimeError("Creazione finestra fallita: "+self.sdl.error())
        self.context=self.sdl.GL_CreateContext(self.handle)
        if not self.context:
            self.close()
            raise RuntimeError("Serve OpenGL 3.3 e un driver funzionante: "+self.sdl.error())
        self.sdl.GL_SetSwapInterval(0 if args.no_vsync or args.benchmark or args.smoke_test else 1)
        self.event=C.create_string_buffer(64)
        self.connect_pad()

    def connect_pad(self):
        if self.controller and self.sdl.GameControllerGetAttached(self.controller): return
        if self.controller:
            self.sdl.GameControllerClose(self.controller);self.controller=None
        for i in range(self.sdl.NumJoysticks()):
            if self.sdl.IsGameController(i):
                self.controller=self.sdl.GameControllerOpen(i);break

    def size(self):
        w,h=C.c_int(),C.c_int()
        self.sdl.GL_GetDrawableSize(self.handle,C.byref(w),C.byref(h))
        return max(1,w.value),max(1,h.value)

    def poll(self):
        pressed=set();lost_focus=False
        while self.sdl.PollEvent(self.event):
            raw=self.event.raw
            typ=struct.unpack_from("I",raw,0)[0]
            if typ==0x100: self.closed=True
            elif typ==0x300 and raw[13]==0:  # non-repeated SDL_KEYDOWN
                pressed.add(struct.unpack_from("i",raw,16)[0])
            elif typ==0x200:
                if raw[12]==13: lost_focus=True
                elif raw[12]==14: self.closed=True
            elif typ in (0x653,0x654): self.connect_pad()
        count=C.c_int()
        keys=self.sdl.GetKeyboardState(C.byref(count))
        def key(sc): return bool(keys[sc]) if sc<count.value else False
        left=key(4) or key(80);right=key(7) or key(79)
        jump=key(44) or key(29) or key(26) or key(82)
        run=key(225) or key(229)
        fire=key(27) or key(224) or key(228)
        down=key(22) or key(81)
        axis=float(right)-float(left)
        jump_pressed=bool({44,29,26,82}&pressed)
        if self.controller and self.sdl.GameControllerGetAttached(self.controller):
            pad=self.controller
            analog=self.sdl.GameControllerGetAxis(pad,0)/32767
            if abs(analog)>.22: axis=clamp(analog,-1,1)
            btns={i for i in range(15) if self.sdl.GameControllerGetButton(pad,i)}
            newly=btns-self.pad_buttons;self.pad_buttons=btns
            if 13 in btns: axis=-1.
            if 14 in btns: axis=1.
            jump=jump or 0 in btns;fire=fire or 2 in btns
            run=run or 1 in btns or 10 in btns
            down=down or 12 in btns
            jump_pressed=jump_pressed or 0 in newly
            if 6 in newly: pressed.add(41)
            if 0 in newly: pressed.add(40)
            if 13 in newly: pressed.add(80)
            if 14 in newly: pressed.add(79)
        self.jump_was_down=jump
        return Controls(axis,jump,jump_pressed,run,fire,down),pressed,lost_focus

    def toggle_fullscreen(self):
        self.fullscreen=not self.fullscreen
        if self.sdl.SetWindowFullscreen(self.handle,0x1001 if self.fullscreen else 0)!=0:
            self.fullscreen=not self.fullscreen

    def close(self):
        if self.controller: self.sdl.GameControllerClose(self.controller);self.controller=None
        if self.context: self.sdl.GL_DeleteContext(self.context);self.context=None
        if self.handle: self.sdl.DestroyWindow(self.handle);self.handle=None
        self.sdl.Quit()


# ---------------------------------------------------------------------------
# Minimal typed OpenGL loader and resources. Geometry is GPU-instanced.
# ---------------------------------------------------------------------------
GL_FLOAT=0x1406;GL_UNSIGNED_BYTE=0x1401;GL_UNSIGNED_INT=0x1405
GL_ARRAY_BUFFER=0x8892;GL_DYNAMIC_DRAW=0x88E8;GL_STATIC_DRAW=0x88E4
GL_TEXTURE_2D=0x0DE1;GL_FRAMEBUFFER=0x8D40;GL_DEPTH_TEST=0x0B71
GL_COLOR_BUFFER_BIT=0x4000;GL_DEPTH_BUFFER_BIT=0x0100


class GL:
    def __init__(self,sdl):
        U,I,F,P,B=C.c_uint,C.c_int,C.c_float,C.c_void_p,C.c_ubyte
        def bind(name,ret,*args):
            addr=sdl.GL_GetProcAddress(("gl"+name).encode())
            if not addr: raise RuntimeError("Funzione OpenGL mancante: gl"+name)
            setattr(self,name,C.CFUNCTYPE(ret,*args)(addr))
        bind("GetString",C.c_char_p,U);bind("GetIntegerv",None,U,C.POINTER(I))
        bind("GetError",U);bind("Viewport",None,I,I,I,I)
        bind("Enable",None,U);bind("Disable",None,U);bind("DepthFunc",None,U)
        bind("DepthMask",None,B);bind("BlendFunc",None,U,U)
        bind("ClearColor",None,F,F,F,F);bind("Clear",None,U)
        bind("GenVertexArrays",None,I,C.POINTER(U));bind("BindVertexArray",None,U)
        bind("GenBuffers",None,I,C.POINTER(U));bind("BindBuffer",None,U,U)
        bind("BufferData",None,U,C.c_ssize_t,P,U)
        bind("BufferSubData",None,U,C.c_ssize_t,C.c_ssize_t,P)
        bind("EnableVertexAttribArray",None,U)
        bind("VertexAttribPointer",None,U,I,U,B,I,P)
        bind("VertexAttribDivisor",None,U,U)
        bind("CreateShader",U,U);bind("ShaderSource",None,U,I,C.POINTER(C.c_char_p),P)
        bind("CompileShader",None,U);bind("GetShaderiv",None,U,U,C.POINTER(I))
        bind("GetShaderInfoLog",None,U,I,P,P);bind("DeleteShader",None,U)
        bind("CreateProgram",U);bind("AttachShader",None,U,U);bind("LinkProgram",None,U)
        bind("GetProgramiv",None,U,U,C.POINTER(I));bind("GetProgramInfoLog",None,U,I,P,P)
        bind("UseProgram",None,U);bind("GetUniformLocation",I,U,C.c_char_p)
        bind("Uniform1i",None,I,I);bind("Uniform1f",None,I,F)
        bind("Uniform2f",None,I,F,F);bind("Uniform3f",None,I,F,F,F)
        bind("Uniform4fv",None,I,I,P);bind("UniformMatrix4fv",None,I,I,B,P)
        bind("GenTextures",None,I,C.POINTER(U));bind("BindTexture",None,U,U)
        bind("ActiveTexture",None,U);bind("TexParameteri",None,U,U,I)
        bind("TexImage2D",None,U,I,I,I,I,I,U,U,P)
        bind("DeleteTextures",None,I,C.POINTER(U));bind("PixelStorei",None,U,I)
        bind("GenFramebuffers",None,I,C.POINTER(U));bind("BindFramebuffer",None,U,U)
        bind("FramebufferTexture2D",None,U,U,U,U,I)
        bind("CheckFramebufferStatus",U,U);bind("DrawBuffers",None,I,C.POINTER(U))
        bind("DrawBuffer",None,U);bind("ReadBuffer",None,U)
        bind("DeleteFramebuffers",None,I,C.POINTER(U))
        bind("DrawArrays",None,U,I,I);bind("DrawArraysInstanced",None,U,I,I,I)
        bind("ReadPixels",None,I,I,I,I,U,U,P);bind("Finish",None)
        bind("GenQueries",None,I,C.POINTER(U));bind("BeginQuery",None,U,U)
        bind("EndQuery",None,U);bind("GetQueryObjectiv",None,U,U,C.POINTER(I))
        bind("GetQueryObjectui64v",None,U,U,C.POINTER(C.c_uint64))

    def gen(self,kind):
        value=C.c_uint();getattr(self,"Gen"+kind)(1,C.byref(value));return value.value


class Program:
    def __init__(self,gl,vert,frag,name):
        self.gl=gl;self.name=name;self.uniforms={}
        shaders=[]
        for kind,source in ((0x8B31,vert),(0x8B30,frag)):
            sid=gl.CreateShader(kind)
            txt=C.c_char_p(source.encode());gl.ShaderSource(sid,1,C.byref(txt),None)
            gl.CompileShader(sid)
            okay=C.c_int();gl.GetShaderiv(sid,0x8B81,C.byref(okay))
            if not okay.value:
                log=C.create_string_buffer(16384);gl.GetShaderInfoLog(sid,len(log),None,log)
                raise RuntimeError(f"Shader {name}: {log.value.decode(errors='replace')}")
            shaders.append(sid)
        self.id=gl.CreateProgram()
        for sid in shaders: gl.AttachShader(self.id,sid)
        gl.LinkProgram(self.id)
        okay=C.c_int();gl.GetProgramiv(self.id,0x8B82,C.byref(okay))
        if not okay.value:
            log=C.create_string_buffer(16384);gl.GetProgramInfoLog(self.id,len(log),None,log)
            raise RuntimeError(f"Link {name}: {log.value.decode(errors='replace')}")
        for sid in shaders: gl.DeleteShader(sid)

    def use(self): self.gl.UseProgram(self.id)

    def loc(self,name):
        if name not in self.uniforms:
            self.uniforms[name]=self.gl.GetUniformLocation(self.id,name.encode())
        return self.uniforms[name]

    def f(self,name,x): self.gl.Uniform1f(self.loc(name),float(x))
    def i(self,name,x): self.gl.Uniform1i(self.loc(name),int(x))
    def v2(self,name,x,y): self.gl.Uniform2f(self.loc(name),x,y)
    def v3(self,name,v): self.gl.Uniform3f(self.loc(name),*v)

    def mat(self,name,m):
        a=np.ascontiguousarray(m.T,dtype=np.float32)
        self.gl.UniformMatrix4fv(self.loc(name),1,0,C.c_void_p(a.ctypes.data))

    def vec4_array(self,name,data):
        a=np.ascontiguousarray(data,dtype=np.float32)
        self.gl.Uniform4fv(self.loc(name),len(a),C.c_void_p(a.ctypes.data))


class Texture:
    def __init__(self,gl,w,h,kind="hdr",pixels=None):
        self.gl=gl;self.id=gl.gen("Textures");self.w=w;self.h=h
        gl.BindTexture(GL_TEXTURE_2D,self.id)
        # clamp-to-edge for depth/normal and post processing targets.
        for param,value in ((0x2801,0x2601),(0x2800,0x2601),(0x2802,0x812F),(0x2803,0x812F)):
            gl.TexParameteri(GL_TEXTURE_2D,param,value)
        internal,fmt,dtype={"hdr":(0x881A,0x1908,GL_FLOAT),
                            "depth":(0x81A6,0x1902,GL_FLOAT),
                            "font":(0x8229,0x1903,GL_UNSIGNED_BYTE)}[kind]
        if kind=="depth":
            gl.TexParameteri(GL_TEXTURE_2D,0x2801,0x2600)
            gl.TexParameteri(GL_TEXTURE_2D,0x2800,0x2600)
        gl.PixelStorei(0x0CF5,1)
        data=np.ascontiguousarray(pixels) if pixels is not None else None
        gl.TexImage2D(GL_TEXTURE_2D,0,internal,w,h,0,fmt,dtype,
                      C.c_void_p(data.ctypes.data) if data is not None else None)

    def bind(self,unit):
        self.gl.ActiveTexture(0x84C0+unit);self.gl.BindTexture(GL_TEXTURE_2D,self.id)

    def delete(self):
        if self.id:
            x=C.c_uint(self.id);self.gl.DeleteTextures(1,C.byref(x));self.id=0


class Target:
    def __init__(self,gl,w,h,ncolors=1,depth=False):
        self.gl=gl;self.w=w;self.h=h;self.id=gl.gen("Framebuffers")
        self.colors=[Texture(gl,w,h) for _ in range(ncolors)]
        self.depth=Texture(gl,w,h,"depth") if depth else None
        gl.BindFramebuffer(GL_FRAMEBUFFER,self.id)
        for i,tex in enumerate(self.colors):
            gl.FramebufferTexture2D(GL_FRAMEBUFFER,0x8CE0+i,GL_TEXTURE_2D,tex.id,0)
        if self.depth:
            gl.FramebufferTexture2D(GL_FRAMEBUFFER,0x8D00,GL_TEXTURE_2D,self.depth.id,0)
        if ncolors:
            att=(C.c_uint*ncolors)(*[0x8CE0+i for i in range(ncolors)])
            gl.DrawBuffers(ncolors,att)
        else:
            gl.DrawBuffer(0);gl.ReadBuffer(0)
        if gl.CheckFramebufferStatus(GL_FRAMEBUFFER)!=0x8CD5:
            raise RuntimeError(f"Framebuffer {w}x{h} incompleto; ridurre --scale/--quality.")

    def use(self):
        self.gl.BindFramebuffer(GL_FRAMEBUFFER,self.id)
        self.gl.Viewport(0,0,self.w,self.h)

    def delete(self):
        for t in self.colors: t.delete()
        if self.depth: self.depth.delete()
        if self.id:
            value=C.c_uint(self.id);self.gl.DeleteFramebuffers(1,C.byref(value));self.id=0


# Geometry builders. Meshes use positions and normals; all sizes are unit-scale.
def mesh_cube(rounded=False):
    vertices=[]
    n=6 if rounded else 1
    for axis in range(3):
        other=[k for k in range(3) if k!=axis]
        for side in (-1,1):
            def point(u,v):
                pos=np.zeros(3);pos[axis]=side*.5;pos[other]=[u,v]
                norm=np.zeros(3);norm[axis]=side
                if rounded:
                    centre=np.clip(pos,-.40,.40)
                    norm=vnormalize(pos-centre)
                    pos=centre+norm*.10
                return tuple(pos)+tuple(norm)
            for a in range(n):
                for b in range(n):
                    u=a/n-.5;v=b/n-.5;d=1/n
                    q=[point(u,v),point(u+d,v),point(u+d,v+d),point(u,v+d)]
                    vertices.extend([q[0],q[1],q[2],q[0],q[2],q[3]])
    return np.array(vertices,dtype=np.float32)


def mesh_sphere(rings=12,segments=20):
    vertices=[]
    def pt(a,b):
        theta=a/rings*math.pi;phi=b/segments*TAU
        n=(math.sin(theta)*math.cos(phi),math.cos(theta),math.sin(theta)*math.sin(phi))
        return tuple(v*.5 for v in n)+n
    for a in range(rings):
        for b in range(segments):
            q=[pt(a,b),pt(a+1,b),pt(a+1,b+1),pt(a,b+1)]
            vertices.extend([q[0],q[1],q[2],q[0],q[2],q[3]])
    return np.array(vertices,dtype=np.float32)


def mesh_cylinder(cone=False,segments=24):
    vertices=[]
    for j in range(segments):
        a=j/segments*TAU;b=(j+1)/segments*TAU
        ca,sa,cb,sb=math.cos(a),math.sin(a),math.cos(b),math.sin(b)
        top=0.02 if cone else .5
        na=vnormalize((ca,.5 if cone else 0,sa));nb=vnormalize((cb,.5 if cone else 0,sb))
        q=[(.5*ca,-.5,.5*sa,*na),(.5*cb,-.5,.5*sb,*nb),
           (top*cb,.5,top*sb,*nb),(top*ca,.5,top*sa,*na)]
        vertices.extend([q[0],q[1],q[2],q[0],q[2],q[3]])
        for y,r,sgn in ((-.5,.5,-1),(.5,top,1)):
            vertices.extend([(0,y,0,0,sgn,0),(r*ca,y,r*sa,0,sgn,0),
                             (r*cb,y,r*sb,0,sgn,0)])
    return np.array(vertices,dtype=np.float32)


def mesh_torus():
    vertices=[];ns=32;nt=10
    def pt(i,j):
        a=i/ns*TAU;b=j/nt*TAU
        r=.39+.11*math.cos(b)
        return (r*math.cos(a),r*math.sin(a),.11*math.sin(b),
                math.cos(b)*math.cos(a),math.cos(b)*math.sin(a),math.sin(b))
    for i in range(ns):
        for j in range(nt):
            q=[pt(i,j),pt(i+1,j),pt(i+1,j+1),pt(i,j+1)]
            vertices.extend([q[0],q[1],q[2],q[0],q[2],q[3]])
    return np.array(vertices,dtype=np.float32)


def mesh_grass():
    vertices=[]
    for angle in (0,TAU/3,TAU*2/3):
        dx,dz=math.cos(angle)*.5,math.sin(angle)*.5
        normal=(-math.sin(angle),0,math.cos(angle))
        vertices.extend([(-dx,0,-dz,*normal),(dx,0,dz,*normal),(.1,1,0,*normal)])
    return np.array(vertices,dtype=np.float32)


class Mesh:
    def __init__(self,gl,vertices):
        self.gl=gl;self.count=len(vertices);self.instances=0
        self.vao=gl.gen("VertexArrays");self.vbo=gl.gen("Buffers");self.ibo=gl.gen("Buffers")
        gl.BindVertexArray(self.vao)
        gl.BindBuffer(GL_ARRAY_BUFFER,self.vbo)
        gl.BufferData(GL_ARRAY_BUFFER,vertices.nbytes,C.c_void_p(vertices.ctypes.data),GL_STATIC_DRAW)
        for loc,offset in ((0,0),(1,12)):
            gl.EnableVertexAttribArray(loc);gl.VertexAttribPointer(loc,3,GL_FLOAT,0,24,C.c_void_p(offset))
        gl.BindBuffer(GL_ARRAY_BUFFER,self.ibo)
        gl.BufferData(GL_ARRAY_BUFFER,56,None,GL_DYNAMIC_DRAW)
        for loc,size,offset in ((2,3,0),(3,3,12),(4,4,24),(5,4,40)):
            gl.EnableVertexAttribArray(loc)
            gl.VertexAttribPointer(loc,size,GL_FLOAT,0,56,C.c_void_p(offset))
            gl.VertexAttribDivisor(loc,1)

    def upload(self,data):
        self.instances=len(data)
        if not data: return
        a=np.asarray(data,dtype=np.float32)
        self.gl.BindBuffer(GL_ARRAY_BUFFER,self.ibo)
        # Buffer orphaning prevents CPU/GPU synchronization on the previous frame.
        self.gl.BufferData(GL_ARRAY_BUFFER,a.nbytes,C.c_void_p(a.ctypes.data),GL_DYNAMIC_DRAW)

    def draw(self):
        if self.instances:
            self.gl.BindVertexArray(self.vao)
            self.gl.DrawArraysInstanced(0x0004,0,self.count,self.instances)

# ---------------------------------------------------------------------------
# Embedded GLSL. Every post effect runs on the GPU, not in a Python pixel loop.
# ---------------------------------------------------------------------------
FULLSCREEN_VERT = r"""#version 330 core
out vec2 vUV;
void main(){
    vec2 p=vec2((gl_VertexID<<1)&2,gl_VertexID&2);
    vUV=p; gl_Position=vec4(p*2.0-1.0,0.0,1.0);
}
"""

MESH_VERT = r"""#version 330 core
layout(location=0) in vec3 aPos;
layout(location=1) in vec3 aNormal;
layout(location=2) in vec3 iPos;
layout(location=3) in vec3 iScale;
layout(location=4) in vec4 iColor;
layout(location=5) in vec4 iMisc;
uniform mat4 uVP;
uniform float uTime;
out vec3 vWorld;
out vec3 vNormal;
out vec3 vLocal;
out vec4 vColor;
out vec2 vSurface;
void main(){
    float c=cos(iMisc.x),s=sin(iMisc.x),cy=cos(iMisc.y),sy=sin(iMisc.y);
    mat3 rz=mat3(c,s,0,-s,c,0,0,0,1);
    mat3 ry=mat3(cy,0,-sy,0,1,0,sy,0,cy);
    mat3 rotation=rz*ry;
    vec3 p=aPos;
    if(iColor.a>3.5 && iColor.a<4.5){
        p.x+=sin(uTime*1.7+iPos.x*.6+iPos.z)*p.y*p.y*.16;
        p.z+=cos(uTime*1.2+iPos.x)*p.y*.07;
    }
    vWorld=iPos+rotation*(p*iScale);
    vNormal=normalize(rotation*(aNormal/max(abs(iScale),vec3(.001))));
    vLocal=aPos;
    vColor=iColor;vSurface=iMisc.zw;
    gl_Position=uVP*vec4(vWorld,1.0);
}
"""

SHADOW_FRAG = r"""#version 330 core
void main(){}
"""

SCENE_FRAG = r"""#version 330 core
in vec3 vWorld;
in vec3 vNormal;
in vec3 vLocal;
in vec4 vColor;
in vec2 vSurface;
layout(location=0) out vec4 oColor;
layout(location=1) out vec4 oNormal;
uniform vec3 uEye,uSunDir,uSunColor,uFog;
uniform float uTime,uAmbient;
uniform int uTheme,uPCF;
uniform mat4 uLightVP;
uniform sampler2D uShadow;
uniform vec4 uLights[6];
uniform vec4 uLightColors[6];
const float PI=3.14159265;
float hash(vec3 p){p=fract(p*.1031);p+=dot(p,p.yzx+33.33);return fract((p.x+p.y)*p.z);}
float noise(vec3 p){
    vec3 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);
    return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x),
                   mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
               mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),
                   mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);
}
float shadow(vec3 n){
    vec4 clip=uLightVP*vec4(vWorld,1.0);
    vec3 p=clip.xyz/clip.w*.5+.5;
    if(any(lessThan(p,vec3(0))) || any(greaterThan(p,vec3(1)))) return 1.0;
    vec2 texel=1.0/vec2(textureSize(uShadow,0));
    float bias=max(.00015,texel.x*2.2)*(1.0-max(dot(n,uSunDir),0.0)*.78);
    float shade=0.0,count=0.0;
    for(int x=-3;x<=3;x++)for(int y=-3;y<=3;y++){
        if(abs(x)>uPCF||abs(y)>uPCF)continue;
        float d=texture(uShadow,p.xy+vec2(x,y)*texel*1.15).r;
        shade+=p.z-bias<=d?1.0:0.0;count+=1.0;
    }
    return shade/count;
}
vec3 brdf(vec3 base,vec3 n,vec3 v,vec3 l,float rough,float metal){
    vec3 h=normalize(v+l);
    float nl=max(dot(n,l),0.0),nv=max(dot(n,v),.001),nh=max(dot(n,h),0.0);
    float a=rough*rough,a2=a*a;
    float denom=nh*nh*(a2-1.0)+1.0;
    float d=a2/max(PI*denom*denom,.001);
    float k=(rough+1.0)*(rough+1.0)/8.0;
    float g=(nl/(nl*(1.0-k)+k))*(nv/(nv*(1.0-k)+k));
    vec3 f0=mix(vec3(.04),base,metal);
    vec3 f=f0+(1.0-f0)*pow(1.0-max(dot(h,v),0.0),5.0);
    vec3 spec=d*g*f/max(4.0*nl*nv,.001);
    return ((1.0-f)*base*(1.0-metal)/PI+spec)*nl;
}
void main(){
    vec3 n=normalize(vNormal);
    if(!gl_FrontFacing)n=-n;
    // Meshes intentionally accept both winding orders; use geometric outward normal.
    n=normalize(vNormal);
    vec3 v=normalize(uEye-vWorld);
    vec3 base=pow(max(vColor.rgb,vec3(.0)),vec3(2.2));
    int mat=int(vColor.a+.1);
    float rough=clamp(vSurface.x,.08,1.0),metal=0.;
    float emit=vSurface.y;
    float grain=noise(vWorld*5.0);
    if(mat==1){
        base*=.72+.35*noise(vWorld*2.7)+.12*grain;
        base*=.86+.14*smoothstep(.0,.16,abs(sin(vWorld.y*2.1+noise(vWorld*.7))));
    }else if(mat==2){
        base*=.80+.30*grain;
    }else if(mat==3){
        vec2 uv=vWorld.xy*vec2(1.05,2.1);
        uv.x+=mod(floor(uv.y),2.0)*.5;
        vec2 grid=abs(fract(uv)-.5);
        float edge=smoothstep(.44,.49,max(grid.x,grid.y));
        base=mix(base*(.9+.18*grain),base*.34,edge);
    }else if(mat==4){
        base*=.8+.35*max(vLocal.y,0.0);rough=.8;
    }else if(mat==5){
        n=normalize(n+vec3(sin(vWorld.x*2.4+uTime)*.10,0,cos(vWorld.z*2.3-uTime)*.12));
        float foam=pow(.5+.5*sin(vWorld.x*2+vWorld.z*1.7+uTime),18.);
        base+=foam*.13;rough=.18;metal=.4;
    }else if(mat==6){
        float veins=noise(vWorld*1.8+vec3(uTime*.19,0,-uTime*.1));
        float hot=smoothstep(.37,.67,veins);
        base=mix(vec3(.045,.005,.009),vec3(1.,.23,.014),hot);
        emit+=hot*4.;rough=.35;
    }else if(mat==7){
        rough=.13;metal=.5;
        base*=.8+.30*sin(vWorld.y*3.1);
    }else if(mat==8){
        base*=.94+.1*grain;rough=.42;
        float sparkle=pow(hash(floor(vWorld*70.)),90.);
        emit+=sparkle*.5;
    }else if(mat==9){metal=.72;rough=max(.24,rough);}
    float sunshadow=shadow(n);
    vec3 color=brdf(base,n,v,normalize(uSunDir),rough,metal)*uSunColor*3.1*sunshadow;
    vec3 hemi=mix(uFog*.48,vec3(.66,.78,.95),n.y*.5+.5);
    color+=base*hemi*uAmbient;
    color+=base*vec3(.20,.14,.12)*max(-n.y,0.0)*.25;
    for(int i=0;i<6;i++){
        vec3 delta=uLights[i].xyz-vWorld;
        float dist=length(delta);
        float atten=uLights[i].w/(1.0+dist*dist*.7);
        color+=brdf(base,n,v,delta/max(dist,.001),rough,metal)*uLightColors[i].rgb*atten;
    }
    float rim=pow(1.0-max(dot(n,v),0.0),3.0);
    color+=rim*base*.17;
    color+=base*emit;
    float fog=1.-exp(-max(0.,-vWorld.z-2.)*.027);
    color=mix(color,uFog*.65,min(.75,fog));
    oColor=vec4(max(color,vec3(0)),1);
    oNormal=vec4(n*.5+.5,1);
}
"""

SKY_FRAG = r"""#version 330 core
in vec2 vUV;
layout(location=0) out vec4 oColor;
layout(location=1) out vec4 oNormal;
uniform vec3 uTop,uBottom,uSun;
uniform float uTime,uCamera,uAspect;
uniform int uTheme,uCloud;
float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
float noise(vec2 p){
    vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
    return mix(mix(hash(i),hash(i+vec2(1,0)),f.x),
               mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x),f.y);
}
float fbm(vec2 p){
    float f=0.,a=.5;
    for(int i=0;i<6;i++){
        if(i>=uCloud)break;
        f+=a*noise(p);p=mat2(1.6,1.2,-1.2,1.6)*p+4.7;a*=.5;
    }
    return f;
}
void main(){
    vec2 uv=vUV;
    vec3 col=mix(uBottom,uTop,smoothstep(.02,1.,uv.y));
    vec2 p=vec2(uv.x*uAspect,uv.y);
    vec2 sunp=vec2(uAspect*.77,.78);
    float sd=length(p-sunp);
    float glow=exp(-sd*5.);
    col+=uSun*glow*.22;
    if(uTheme!=1){
        float disk=1.-smoothstep(.055,.059,sd);
        if(uTheme==4){
            float inner=1.-smoothstep(.049,.051,length(p-sunp-vec2(-.009,.006)));
            col=mix(col,vec3(.018,.008,.032),inner);
            disk=max(0.,disk-inner);
            col+=uSun*exp(-abs(sd-.054)*110.)*.7;
        }
        col+=uSun*disk*2.4;
    }
    vec2 cpos=vec2(uv.x*4.+uCamera*.012+uTime*.015,uv.y*5.4);
    float cloudy=fbm(cpos+fbm(cpos*.5)*1.2);
    float cloud=smoothstep(.47,.68,cloudy)*smoothstep(.28,.65,uv.y);
    vec3 cloudCol=mix(uBottom*.83,vec3(1.0,.97,.90),.64);
    if(uTheme==1||uTheme==4) cloudCol=uBottom*.65;
    col=mix(col,cloudCol,cloud*.80);
    if(uTheme==1||uTheme==3||uTheme==4){
        vec2 stars=vec2(uv.x*uAspect+uCamera*.0007,uv.y)*650.;
        vec2 cell=floor(stars);
        float star=step(.997,hash(cell))*(1.-smoothstep(.0,.19,length(fract(stars)-.5)));
        col+=star*(.6+.4*sin(uTime+hash(cell)*20.))*vec3(.7,.85,1.5);
        if(uTheme==3){
            float band=sin(uv.x*7.+uCamera*.02+uTime*.04)*.05+.79;
            float aurora=exp(-abs(uv.y-band)*21.)*fbm(vec2(uv.x*9.,uTime*.04));
            col+=vec3(.08,.70,.48)*aurora*.7;
        }
    }
    oColor=vec4(col,1);
    oNormal=vec4(.5,.5,1,0);
}
"""

EFFECT_FRAG = r"""#version 330 core
in vec2 vUV;
out vec4 oColor;
uniform sampler2D uDepth,uNormal,uShadow;
uniform mat4 uInvVP,uLightVP;
uniform vec3 uSunColor,uEye;
uniform vec2 uResolution;
uniform int uAO,uVolume;
uniform float uDensity;
vec3 world(vec2 uv,float d){vec4 p=uInvVP*vec4(uv*2.-1.,d*2.-1.,1.);return p.xyz/p.w;}
float hash(vec2 p){return fract(sin(dot(p,vec2(12.9898,78.233)))*43758.5453);}
void main(){
    float d=texture(uDepth,vUV).r;
    vec4 normalData=texture(uNormal,vUV);
    if(d>.99999 || normalData.a<.5){oColor=vec4(0,0,0,1);return;}
    vec3 pos=world(vUV,d),n=normalize(normalData.xyz*2.-1.);
    float occlusion=0.;float count=0.;
    float angle=hash(floor(vUV*uResolution/2.))*6.283185;
    for(int i=0;i<24;i++){
        if(i>=uAO)break;
        float fi=float(i)+.5;
        float r=(3.+sqrt(fi/float(uAO))*24.);
        float a=fi*2.39996+angle;
        vec2 uv=vUV+vec2(cos(a),sin(a))*r/uResolution;
        float qd=texture(uDepth,uv).r;
        if(qd>.99999)continue;
        vec3 delta=world(uv,qd)-pos;
        float dist=length(delta);
        float occ=max(0.,dot(n,delta/max(dist,.001))-.16);
        occlusion+=occ*(1.-smoothstep(.15,1.5,dist));count+=1.;
    }
    float ao=1.-clamp(occlusion/max(count,1.)*2.1,0.,.58);
    vec3 nearpos=world(vUV,.0);
    vec3 ray=pos-nearpos;
    float len=min(length(ray),38.);
    vec3 dir=normalize(ray);
    float lit=0.;
    for(int i=0;i<40;i++){
        if(i>=uVolume)break;
        float f=(float(i)+.5)/float(uVolume);
        vec3 samplepos=pos-dir*(len*f);
        vec4 q=uLightVP*vec4(samplepos,1);
        vec3 s=q.xyz/q.w*.5+.5;
        float valid=step(0.,s.x)*step(s.x,1.)*step(0.,s.y)*step(s.y,1.);
        float visible=step(s.z-.0007,texture(uShadow,s.xy).r);
        lit+=mix(1.,visible,valid)*exp(-f*len*.023);
    }
    vec3 fog=uSunColor*(lit/max(float(uVolume),1.))*uDensity*(1.-exp(-len*.055));
    oColor=vec4(fog,ao);
}
"""

BLOOM_FRAG = r"""#version 330 core
in vec2 vUV;
out vec4 oColor;
uniform sampler2D uSource;
uniform vec2 uPixel;
uniform int uFirst;
void main(){
    vec3 c=texture(uSource,vUV).rgb*4.;
    c+=texture(uSource,vUV+uPixel*vec2(1,0)).rgb*2.;
    c+=texture(uSource,vUV+uPixel*vec2(-1,0)).rgb*2.;
    c+=texture(uSource,vUV+uPixel*vec2(0,1)).rgb*2.;
    c+=texture(uSource,vUV+uPixel*vec2(0,-1)).rgb*2.;
    c+=texture(uSource,vUV+uPixel*vec2(1,1)).rgb;
    c+=texture(uSource,vUV+uPixel*vec2(-1,1)).rgb;
    c+=texture(uSource,vUV+uPixel*vec2(1,-1)).rgb;
    c+=texture(uSource,vUV+uPixel*vec2(-1,-1)).rgb;
    c/=16.;
    if(uFirst==1){
        float b=max(max(c.r,c.g),c.b);
        float knee=clamp((b-.65)/.7,0.,1.);
        c*=max(b-1.,0.)/max(b,.0001)+knee*.16;
    }
    oColor=vec4(c,1);
}
"""

FINAL_FRAG = r"""#version 330 core
in vec2 vUV;
out vec4 oColor;
uniform sampler2D uScene,uEffects,uBloom0,uBloom1,uBloom2,uBloom3,uBloom4;
uniform vec2 uPixel;
uniform float uTime;
vec3 aces(vec3 x){return clamp((x*(2.51*x+.03))/(x*(2.43*x+.59)+.14),0.,1.);}
float luma(vec3 v){return dot(v,vec3(.299,.587,.114));}
void main(){
    vec3 c=texture(uScene,vUV).rgb;
    vec3 a=texture(uScene,vUV+uPixel*vec2(-1,-1)).rgb;
    vec3 b=texture(uScene,vUV+uPixel*vec2(1,-1)).rgb;
    vec3 d=texture(uScene,vUV+uPixel*vec2(-1,1)).rgb;
    vec3 e=texture(uScene,vUV+uPixel*vec2(1,1)).rgb;
    float lo=min(luma(c),min(min(luma(a),luma(b)),min(luma(d),luma(e))));
    float hi=max(luma(c),max(max(luma(a),luma(b)),max(luma(d),luma(e))));
    if(hi-lo>max(.05,hi*.12)){
        vec2 dir=vec2(-((luma(a)+luma(b))-(luma(d)+luma(e))),
                       (luma(a)+luma(d))-(luma(b)+luma(e)));
        float red=max((luma(a)+luma(b)+luma(d)+luma(e))*.03125,.0078125);
        dir=clamp(dir/(min(abs(dir.x),abs(dir.y))+red),vec2(-5),vec2(5))*uPixel;
        c=(texture(uScene,vUV-dir*.1667).rgb+texture(uScene,vUV+dir*.1667).rgb)*.5;
    }
    vec4 eff=texture(uEffects,vUV);
    c=c*eff.a+eff.rgb;
    vec3 bloom=texture(uBloom0,vUV).rgb*.16+texture(uBloom1,vUV).rgb*.20+
               texture(uBloom2,vUV).rgb*.26+texture(uBloom3,vUV).rgb*.28+
               texture(uBloom4,vUV).rgb*.34;
    c+=bloom*.60;
    c=aces(c*1.12);
    c=pow(c,vec3(1./2.2));
    float vignette=1.-dot(vUV-.5,vUV-.5)*.24;
    c*=vignette;
    oColor=vec4(c,1);
}
"""

UI_VERT = r"""#version 330 core
layout(location=0)in vec2 aPos;
layout(location=1)in vec2 aUV;
layout(location=2)in vec4 aColor;
out vec2 vUV;out vec4 vColor;
uniform vec2 uSize;
uniform vec2 uOffset;
uniform float uScale;
void main(){vUV=aUV;vColor=aColor;vec2 p=aPos*uScale+uOffset;
    gl_Position=vec4(p/uSize*vec2(2,-2)+vec2(-1,1),0,1);}
"""
UI_FRAG = r"""#version 330 core
in vec2 vUV;in vec4 vColor;out vec4 oColor;
uniform sampler2D uFont;
void main(){oColor=vec4(vColor.rgb,vColor.a*texture(uFont,vUV).r);}
"""

# ---------------------------------------------------------------------------
# Procedural art direction and animated character models.
# Instance format: position.xyz, scale.xyz, color.rgb/material, rz/ry/rough/glow.
# ---------------------------------------------------------------------------
class Scene:
    def __init__(self):
        self.batches=defaultdict(list)
        self.decor=[];self.level_id=None
        self.camera=0.

    def add(self,mesh,pos,scale,color,mat=0,rz=0.,ry=0.,rough=.65,glow=0.):
        self.batches[mesh].append((*pos,*scale,*color,float(mat),rz,ry,rough,glow))

    def make_decor(self,level):
        self.decor=[];r=random.Random(912+level.index)
        th=level.theme;i=level.index
        def add(mesh,pos,scale,color,mat=0,parallax=1.,**kw):
            self.decor.append((parallax,mesh,(*pos,*scale,*color,float(mat),
                 kw.get("rz",0.),kw.get("ry",0.),kw.get("rough",.75),kw.get("glow",0.))))
        # Rounded silhouette islands, well behind the actual collision plane.
        for k in range(-4,35):
            x=k*9+r.uniform(-3,3);y=r.uniform(-4,0);z=r.uniform(-28,-15)
            c=tuple(v*.7+.08 for v in th.ground)
            add("sphere",(x,y,z),(r.uniform(8,14),r.uniform(8,15),6),c,1,.42)
            if i in (0,2,3):
                add("sphere",(x,y+3,z),(9,2.6,6),th.top,8 if i==3 else 2,.42)
        # Monument silhouettes: dense enough for depth without obscuring the player.
        for k in range(-2,41):
            x=k*6.5+r.uniform(-1.5,1.5)
            z=r.uniform(-12,-6);height=r.uniform(3.8,8.8)
            if i==0:
                add("cylinder",(x,height*.40-1.4,z),(.75,height,1.),(.38,.23,.17),1,.70)
                for j in range(4):
                    add("sphere",(x+r.uniform(-1.7,1.7),height*.76+r.uniform(-.8,.8),z+r.uniform(-.7,.7)),
                        (r.uniform(3.5,5.7),r.uniform(2.5,4.1),3.7),
                        (.22+r.random()*.09,.46+r.random()*.14,.31),2,.70)
            elif i==1:
                add("cylinder",(x,height*.42-1.7,z),(.65,height,.65),(.42,.39,.66),7,.72)
                capcolor=[(.30,.38,.95),(.69,.22,.65),(.18,.65,.64)][k%3]
                add("sphere",(x,height*.85-1.5,z),(4.6,1.8,3.7),capcolor,7,.72,glow=.55)
                add("torus",(x,height*.8-1.5,z+.8),(3.1,.42,1.),(.34,.93,1.),7,.72,glow=1.5)
                for j in range(3):
                    add("sphere",(x+(j-1)*.9,height*.85-1.,z+1.6),(.23,.18,.15),(.6,1.,1.),0,.72,glow=2.)
            elif i==2:
                add("cube",(x,height*.34-1.8,z),(2.8,height,3.),(.32,.38,.45),9,.65)
                add("round",(x,height*.82-1.8,z),(3.5,.65,3.4),(.73,.54,.28),9,.65)
                for j in range(3):
                    add("cube",(x-.8+j*.8,height*.5-1,z+1.54),(.27,1.6,.09),(.93,.65,.30),0,.65,glow=.8)
            elif i==3:
                for j in range(3):
                    add("cone",(x+j*.9,height*.32,z),(1.5,height*(.8+j*.18),1.5),
                        (.35+j*.13,.65+j*.08,.88),7,.68,rz=(j-1)*.12,glow=.25)
                add("sphere",(x,-.2,z),(4.5,1.3,4),th.top,8,.68)
            else:
                add("cube",(x,height*.35-2,z),(3.6,height,3.8),(.17,.15,.23),3,.67)
                add("cone",(x,height*.9-2,z),(3.,3.,3.),(.24,.17,.26),9,.67)
                add("cube",(x,height*.37,z+1.93),(.35,2.2,.1),(1.,.3,.08),0,.67,glow=3.)
        # Small plants, polished crystals, mechanical props and local detail.
        for ground in level.platforms:
            if ground.kind!="ground": continue
            for _ in range(max(1,int(ground.w*1.8))):
                x=r.uniform(ground.x,ground.x+ground.w)
                z=r.uniform(-1.6,-.55)
                if i==0:
                    add("grass",(x,.015,z),(r.uniform(.3,.8),r.uniform(.18,.55),.5),th.top,4)
                    if r.random()<.14:
                        add("sphere",(x,.38,z),(.22,.18,.22),(1.,.78,.28),0)
                elif i==1:
                    s=r.uniform(.25,.7)
                    add("cylinder",(x,s*.3,z),(.09,s*.6,.09),(.42,.57,.68),0)
                    add("sphere",(x,s*.65,z),(s,s*.4,s),(.25,.77,.84),7,glow=.8)
                elif i==2:
                    if r.random()<.45:
                        add("round",(x,.15,z),(.38,.3,.4),(.58,.48,.28),9,ry=r.random()*6)
                elif i==3:
                    add("cone",(x,.3,z),(.25,r.uniform(.35,1.1),.27),(.51,.82,1.),7,rz=r.uniform(-.25,.25),glow=.3)
                else:
                    add("cone",(x,.22,z),(.36,r.uniform(.2,.7),.36),(.29,.20,.28),1)
        self.level_id=level.index

    def character(self,p,t,menu=False):
        if p.invuln>0 and int(t*16)%2 and not menu: return
        x=p.x+p.w/2;y=p.y
        if menu: x=self.camera+6.;y=.12
        scale=p.h/1.12
        stride=math.sin(t*(11 if abs(p.vx)>7 else 8))*min(1.,abs(p.vx)/3.)
        if menu: stride=math.sin(t*1.5)*.18
        bob=abs(stride)*.035
        squash=1.-p.squash*.55
        def model(mesh,px,py,pz,sx,sy,sz,col,**kw):
            self.add(mesh,(x+px,y+(py+bob)*scale*squash,pz),
                     (sx/max(squash,.8),sy*scale*squash,sz),col,**kw)
        red=(.42,.24,.74);teal=(.12,.57,.63);skin=(1.,.75,.53)
        if p.power==3: red=(.99,.86,.44);teal=(.85,.28,.56)
        model("sphere",0,.47,0,.53,.54,.40,teal,rough=.46)
        model("round",0,.33,.10,.42,.24,.28,teal)
        for side in (-1,1):
            leg=side*stride
            model("sphere",side*.14,.20+leg*.025,.03+leg*.07,.22,.32,.24,teal,rz=leg*.35)
            model("round",side*.16,.075,.11+leg*.10,.27,.15,.38,(.23,.13,.13),rough=.4)
            model("sphere",side*.32,.45-side*stride*.075,.02,.22,.30,.22,red,rz=side*.25+stride*.4)
            model("sphere",side*.34,.34-side*stride*.075,.12,.20,.20,.20,(.96,.93,.87))
            model("sphere",side*.105,.50,.214,.075,.075,.046,(1.,.83,.27),rough=.25)
        model("sphere",0,.82,.01,.58,.53,.50,skin,rough=.65)
        model("sphere",p.facing*.07,.78,.28,.16,.16,.18,skin)
        for side in (-1,1):
            model("sphere",side*.105+p.facing*.035,.87,.239,.105,.13,.06,(.98,.99,1.))
            model("sphere",side*.105+p.facing*.05,.875,.276,.051,.073,.024,(.025,.09,.13),rough=.22)
            model("round",side*.108+p.facing*.02,.953,.255,.11,.032,.03,(.22,.10,.09),rz=-side*.10)
        model("sphere",0,1.04,-.02,.66,.31,.56,red,rough=.48)
        model("round",p.facing*.06,.975,.16,.67,.07,.48,red,rough=.4)
        # Original Lumen badge, not a Nintendo logo.
        model("sphere",0,1.068,.261,.18,.16,.055,(1.,.92,.73))
        model("round",-.02,1.07,.298,.035,.10,.018,teal)
        model("round",.01,1.025,.299,.085,.027,.018,teal)
        model("round",0,.60,.23,.32,.095,.085,(1.,.76,.24),rough=.6)
        model("round",-.23*p.facing,.50,-.10,.36,.14,.07,(1.,.64,.20),rz=p.facing*.2+stride*.2)
        if p.star_time>0:
            for j in range(7):
                a=t*3+j*TAU/7
                self.add("sphere",(x+math.cos(a)*.7,y+.75+math.sin(a)*.6,math.sin(a*1.3)*.5),
                         (.10,.10,.10),(.48,1.,.84),glow=5.)

    def enemy(self,e,t):
        x=e.x+e.w/2;y=e.y
        if e.kind=="boss":
            c=(.20,.19,.29) if e.hurt<=0 or int(t*15)%2 else (.9,.6,.3)
            core=(.25,.93,1.) if e.vulnerable>0 else (1.,.32,.07)
            self.add("round",(x,y+1.2,0),(1.9,1.7,1.2),c,9,rough=.32)
            self.add("sphere",(x,y+1.9,.02),(1.4,1.1,1.2),c,9)
            self.add("torus",(x,y+1.15,.67),(.78,.78,.5),(.69,.41,.17),9)
            self.add("sphere",(x,y+1.15,.72),(.49,.49,.18),core,7,glow=3.)
            for s in (-1,1):
                self.add("round",(x+s*.84,y+.3,.1),(.60,.60,.75),c,9)
                self.add("sphere",(x+s*1.02,y+1.25,.02),(.76,1.,.87),c,9)
                self.add("round",(x+s*.31,y+2.07,.57),(.27,.12,.10),core,glow=4.,rz=-s*.17)
                self.add("cone",(x+s*.49,y+2.65,-.02),(.34,.70,.36),(.87,.61,.22),9,rz=-s*.25)
            self.add("cone",(x,y+2.79,-.02),(.38,.94,.38),(.96,.68,.2),9)
            if e.phase==0 and e.timer>.5:
                self.add("torus",(x,.07,.0),(3.5+.3*math.sin(t*9),.13,3.5),(1.,.31,.07),glow=3.)
            return
        stride=math.sin(t*9+x)*.08
        if e.kind=="flyer":
            self.add("sphere",(x,y+.43,0),(.83,.62,.64),(.30,.28,.63),9,rough=.3)
            self.add("sphere",(x,y+.44,.31),(.28,.22,.1),(.52,.95,1.),glow=2.5)
            for s in (-1,1):
                self.add("sphere",(x+s*.62,y+.54+math.sin(t*18)*.12,-.1),
                         (.72,.09,.40),(.55,.77,.89),7,rz=s*math.sin(t*18)*.25)
            return
        if e.kind=="turtle":
            self.add("sphere",(x,y+(.23 if e.shell else .38),-.05),
                     (.89,.46 if e.shell else .70,.7),(.22,.54,.35),rough=.32)
            self.add("torus",(x,y+.2,.22),(.74,.35,.45),(.82,.75,.40),rough=.5)
            if e.shell: return
            self.add("sphere",(x+(.24 if e.vx>0 else -.24),y+.62,.21),(.41,.48,.42),(.84,.74,.41))
        else:
            col=(.73,.26,.31) if e.kind=="walker" else (.56,.32,.71)
            self.add("sphere",(x,y+.44,0),(.67,.67,.54),(.79,.60,.42))
            self.add("sphere",(x,y+.68,-.02),(.97,.48,.75),col,rough=.44)
            for s in (-1,1):
                self.add("sphere",(x+s*.25,y+.82,.18),(.18,.10,.13),(.97,.85,.65))
        for s in (-1,1):
            self.add("round",(x+s*.25,y+.09,.12+s*stride),(.34,.19,.38),(.26,.15,.18))
            self.add("sphere",(x+s*.14,y+.43,.284),(.115,.16,.067),(.97,.95,.86))
            self.add("sphere",(x+s*.14,y+.43,.319),(.047,.085,.02),(.10,.07,.14))

    def build(self,g):
        self.batches.clear();self.camera=g.cam_x
        l=g.level;th=l.theme;t=g.fx_time;cx=g.cam_x
        if self.level_id!=l.index: self.make_decor(l)
        for par,mesh,data in self.decor:
            x=data[0]+cx*(1-par)
            if abs(x-cx)<36+data[3]*.5:
                self.batches[mesh].append((x,*data[1:]))
        for p in l.platforms:
            if p.x>cx+33 or p.x+p.w<cx-33: continue
            if not p.active:
                if p.kind=="phase":
                    self.add("round",(p.x+p.w/2,p.y+.10,-.25),(p.w,.08,1.4),th.accent,7,glow=.20)
                continue
            y=p.y+p.h/2+math.sin(p.bump/.23*math.pi)*.12 if p.bump else p.y+p.h/2
            if p.kind=="ground":
                self.add("cube",(p.x+p.w/2,y,-.35),(p.w,p.h,3.5),th.ground,1)
                self.add("cube",(p.x+p.w/2,p.top-.10,-.35),(p.w,.22,3.6),th.top,
                         8 if l.index==3 else (9 if l.index==2 else 2))
                self.add("cube",(p.x+p.w/2,p.top-.36,1.42),(p.w,.19,.045),
                         tuple(v*.8 for v in th.top),1)
            elif p.kind in ("question","brick"):
                color=(.41,.34,.28) if p.used else ((.87,.39,.19) if p.kind=="brick" else (1.,.72,.20))
                self.add("round",(p.x+.475,y,0),(.95,.95,.95),color,3 if p.kind=="brick" else 0,rough=.38)
                if p.kind=="question" and not p.used:
                    # Raised, geometric question mark on the front face.
                    for dx,dy in ((-.11,.20),(.02,.26),(.16,.18),(.16,.05),(.04,-.03),(.02,-.23)):
                        self.add("round",(p.x+.475+dx,y+dy,.498),(.12,.12,.045),(.99,.96,.75),glow=.25)
                else:
                    for dx in (-.32,.32):
                        for dy in (-.32,.32):
                            self.add("sphere",(p.x+.475+dx,y+dy,.475),(.075,.075,.045),(.9,.65,.36),9)
            else:
                color=th.top
                if p.kind=="moving": color=(.85,.64,.24)
                elif p.kind=="phase": color=(.33,.72,.90)
                elif p.kind=="crumble":
                    color=(.69,.45,.33) if p.timer<0 else (1.,.38,.23)
                elif p.kind=="spring": color=(.87,.27,.35)
                wobble=math.sin(t*42)*.035 if p.kind=="crumble" and p.timer>0 else 0.
                self.add("round",(p.x+p.w/2+wobble,y,-.1),(p.w,p.h,2.0),color,
                         7 if p.kind=="phase" else (8 if l.index==3 else 0),rough=.48,
                         glow=.15 if p.kind=="phase" else 0.)
                self.add("round",(p.x+p.w/2,y+.20,-.1),(p.w,.10,2.02),
                         tuple(min(1,v*1.13) for v in color),rough=.55)
                if p.kind=="moving":
                    self.add("cube",(p.x+p.w/2,y-.18,1.),(p.w*.82,.075,.09),th.accent,glow=1.6)
                if p.kind=="spring":
                    for j in range(3):
                        self.add("torus",(p.x+p.w/2,p.y-.1-j*.13,0),(.8,.14,.6),(.84,.77,.57),9)
        for pickup in l.pickups:
            if pickup.taken or abs(pickup.x-cx)>30: continue
            x,y=pickup.x,pickup.y+math.sin(t*2.4+pickup.phase)*.10
            a=t*2.+pickup.phase
            if pickup.kind=="coin":
                self.add("torus",(x,y,.15),(.45,.45,.35),(1.,.74,.20),9,ry=a,rough=.2,glow=.25)
                self.add("sphere",(x,y,.15),(.31,.31,.075),(1.,.81,.33),9,ry=a,rough=.21,glow=.1)
            elif pickup.kind=="shard":
                self.add("cone",(x,y+.17,.1),(.68,.60,.68),(.38,1.,.86),7,ry=a,rough=.16,glow=1.5)
                self.add("cone",(x,y-.23,.1),(.68,.42,.68),(.32,.75,1.),7,rz=math.pi,ry=a,rough=.16,glow=1.5)
                self.add("torus",(x,y,.1),(1.18,1.18,.6),(.41,.85,.93),7,ry=a*.4,glow=.65)
            elif pickup.kind=="grow":
                self.add("sphere",(x,y-.15,0),(.35,.40,.35),(.97,.86,.69))
                self.add("sphere",(x,y+.10,0),(.72,.43,.62),(.83,.18,.30),rough=.3,glow=.3)
                for dx in (-.21,.15):
                    self.add("sphere",(x+dx,y+.19,.23),(.15,.13,.10),(1.,.94,.76))
            else:
                self.add("torus",(x,y,.15),(.72,.72,.7),(1.,.42,.08),ry=a,glow=2.)
                self.add("sphere",(x,y,.15),(.39,.39,.27),(1.,.86,.38),glow=3.)
        for j,(x,y) in enumerate(l.checkpoints):
            if abs(x-cx)>32: continue
            activated=j<=l.checkpoint_id
            col=(.3,1.,.78) if activated else (.67,.76,.85)
            self.add("cylinder",(x,y+1.2,-.65),(.11,2.4,.11),(.53,.56,.65),9)
            self.add("sphere",(x,y+2.48,-.65),(.26,.26,.26),col,glow=2. if activated else .4)
            self.add("round",(x+.48,y+1.90,-.65),(.82,.50,.07),col,glow=.4,rz=math.sin(t*3)*.025)
            self.add("round",(x,y+.12,-.65),(.8,.24,.7),(.35,.38,.46),9)
        for h in l.hazards:
            x,y,w,hh,kind,phase=h
            if abs(x+w/2-cx)>33: continue
            if kind=="spikes":
                for j in range(int(w/.35)):
                    self.add("cone",(x+.2+j*.35,y+hh/2,.10),(.34,hh,.7),
                             (.57,.72,.86) if l.index==3 else (.65,.50,.44),9,rough=.3)
            elif kind=="vent":
                self.add("cylinder",(x+w/2,.06,0),(w,.18,w),(.30,.26,.31),9)
                active=l.vent_active(h,g.time)
                height=hh if active else .20+max(0.,(g.time+phase)%3.7-1.5)*.4
                self.add("cone",(x+w/2,height*.46,.0),(w*.8,height,w*.8),
                         (1.,.38,.08),6,glow=2. if active else .5)
                if active:
                    for j in range(6):
                        yy=(t*2+j*.53)%hh
                        self.add("sphere",(x+w/2+math.sin(t*4+j)*.35,yy,.2),
                                 (.21,.42,.21),(1.,.68,.14),glow=4.)
            else:
                self.add("cube",(x+w/2,-.65,-.2),(w,.35,3.6),(1.,.24,.045),6)
        # Water is decorative. Only the defined lava and spike volumes damage you.
        if l.index in (0,1,3):
            self.add("cube",(cx,-3.0,-8.),(85,.10,15.),
                     (.16,.46,.62) if l.index!=1 else (.19,.20,.56),5,rough=.16)
        elif l.index==4:
            self.add("cube",(cx,-3.8,-8.),(85,.3,18.),(1.,.3,.03),6)
        for e in l.enemies:
            if e.alive and abs(e.x-cx)<32: self.enemy(e,t)
        for s in g.projectiles:
            color=(1.,.30,.09) if s.hostile else (1.,.78,.23)
            self.add("sphere",(s.x,s.y,.2),(s.radius*2,)*3,color,glow=5.)
            for j in range(1,4):
                size=s.radius*(1.-j*.2)
                self.add("sphere",(s.x-s.vx*.017*j,s.y,.20),(size*2,)*3,color,glow=2.)
        for q in g.particles:
            size=q.size*min(1.,q.life/q.maxlife*2)
            self.add("sphere",(q.x,q.y,q.z),(size,)*3,q.color,glow=q.glow)
        # Ambient floating pollen, snow or embers: deterministic animation, no allocations in physics.
        for j in range(65 if l.index in (1,3,4) else 28):
            x=cx-21+(j*7.123+t*(.32 if l.index!=3 else -.8))%43
            y=(j*3.71+t*(.65 if l.index==4 else -.24))%13-.5
            z=-1.5-(j%7)*.9
            size=.04 if l.index!=3 else .055
            col=(1.,.62,.20) if l.index==4 else ((.79,.93,1.) if l.index==3 else th.accent)
            self.add("sphere",(x,y,z),(size,)*3,col,glow=2. if l.index in (1,4) else .7)
        # Portal: visible exit, closed while the final guardian is alive.
        x=l.exit_x+.7
        locked=bool(l.boss and l.boss.alive)
        col=(.54,.18,.18) if locked else th.accent
        self.add("round",(x,.10,-.2),(3.4,.23,2.6),(.47,.44,.48),9)
        self.add("torus",(x,1.8,-.45),(2.75,3.4,1.8),(.76,.65,.39),9,rough=.3)
        self.add("torus",(x,1.8,-.29),(2.32,2.96,.7),col,glow=3. if not locked else .2)
        if not locked:
            for j in range(8):
                a=t+j*TAU/8
                self.add("sphere",(x+math.cos(a)*1.1,1.8+math.sin(a)*1.42,-.05),
                         (.09,)*3,col,glow=4.)
        self.character(g.player,t,menu=g.state=="menu")
        return self.batches

# ---------------------------------------------------------------------------
# Batched, resolution-independent interface. The font is rasterized just once.
# ---------------------------------------------------------------------------
class Interface:
    def __init__(self,gl):
        try:
            from PIL import Image,ImageDraw,ImageFont
        except ImportError as exc:
            raise RuntimeError("Manca Pillow. Ubuntu: sudo apt install python3-pil") from exc
        fontpaths=["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
        font=None
        for path in fontpaths:
            try: font=ImageFont.truetype(path,52);break
            except OSError: pass
        if font is None: font=ImageFont.load_default(size=52)
        chars="".join(dict.fromkeys("".join(chr(i) for i in range(32,127))+"àèéìòùÀÈÌÒÙ×•—"))
        atlas=Image.new("L",(1024,1024),0);draw=ImageDraw.Draw(atlas)
        draw.rectangle((0,0,7,7),fill=255)
        self.glyphs={};self.advances={};self.base_size=52.
        for i,ch in enumerate(chars):
            cx=(i%14)*72+2;cy=(i//14)*86+14
            left,top,right,bottom=font.getbbox(ch)
            w=max(1,right-left);h=max(1,bottom-top)
            draw.text((cx-left,cy-top),ch,font=font,fill=255)
            self.glyphs[ch]=(cx/1024,cy/1024,(cx+w)/1024,(cy+h)/1024,w,h,left,top)
            self.advances[ch]=float(font.getlength(ch))
        self.texture=Texture(gl,1024,1024,"font",np.asarray(atlas))
        self.gl=gl;self.program=Program(gl,UI_VERT,UI_FRAG,"interface")
        self.vao=gl.gen("VertexArrays");self.vbo=gl.gen("Buffers")
        gl.BindVertexArray(self.vao);gl.BindBuffer(GL_ARRAY_BUFFER,self.vbo)
        gl.BufferData(GL_ARRAY_BUFFER,32,None,GL_DYNAMIC_DRAW)
        for loc,size,offset in ((0,2,0),(1,2,8),(2,4,16)):
            gl.EnableVertexAttribArray(loc)
            gl.VertexAttribPointer(loc,size,GL_FLOAT,0,32,C.c_void_p(offset))
        self.vertices=[];self.width=1920.;self.height=1080.
        self.ink=(.96,.98,1.,1.);self.muted=(.63,.72,.79,1.)
        self.gold=(1.,.79,.37,1.);self.mint=(.38,1.,.82,1.)

    def quad(self,x,y,w,h,uv,color):
        u0,v0,u1,v1=uv
        a=(x,y,u0,v0,*color);b=(x+w,y,u1,v0,*color)
        c=(x+w,y+h,u1,v1,*color);d=(x,y+h,u0,v1,*color)
        self.vertices.extend((a,b,c,a,c,d))

    def rect(self,x,y,w,h,color):
        self.quad(x,y,w,h,(.003,.003,.003,.003),color)

    def roundrect(self,x,y,w,h,r,color):
        points=[]
        for cx,cy,start in ((x+w-r,y+r,-math.pi/2),(x+w-r,y+h-r,0),
                            (x+r,y+h-r,math.pi/2),(x+r,y+r,math.pi)):
            for j in range(7):
                a=start+j/6*math.pi/2
                points.append((cx+math.cos(a)*r,cy+math.sin(a)*r))
        mid=(x+w/2,y+h/2,.003,.003,*color)
        for a,b in zip(points,points[1:]+points[:1]):
            self.vertices.extend((mid,(*a,.003,.003,*color),(*b,.003,.003,*color)))

    def text_width(self,text,size,spacing=0.):
        return sum(self.advances.get(ch,30)*size/52+spacing for ch in text)

    def text(self,text,x,y,size=26,color=None,spacing=0.,align="left"):
        color=color or self.ink
        if align=="center": x-=self.text_width(text,size,spacing)/2
        elif align=="right": x-=self.text_width(text,size,spacing)
        s=size/52
        for ch in text:
            glyph=self.glyphs.get(ch,self.glyphs["?"])
            u0,v0,u1,v1,w,h,left,top=glyph
            if ch!=" ": self.quad(x+left*s,y+(top-10)*s,w*s,h*s,(u0,v0,u1,v1),color)
            x+=self.advances.get(ch,30)*s+spacing

    def panel(self,x,y,w,h):
        self.roundrect(x,y,w,h,18,(.023,.037,.061,.86))
        self.rect(x+23,y+1,w-46,1,(.65,.80,.91,.18))

    def build(self,g,renderer,fps):
        self.vertices=[]
        self.width=1920.;self.height=1080.
        self.pixel_width=renderer.width;self.pixel_height=renderer.height
        h=self.height;p=g.player;l=g.level
        th=l.theme;accent=(*th.accent,1.)
        if g.state=="menu":
            self.panel(80,110,930,min(680,h-350))
            self.rect(122,150,70,5,self.mint)
            self.text("A U R O R A   W O R L D S",123,183,24,self.mint)
            self.text("SUPER",116,243,83,spacing=2)
            self.text("LUMEN",112,326,142,spacing=-3)
            self.text("CINQUE MONDI. UNA SCINTILLA DA SALVARE.",124,496,23,self.gold)
            self.text("Un platform originale, costruito interamente in Python.",125,541,22,self.muted)
            self.roundrect(123,597,404,68,11,self.mint)
            self.text("INVIO  /  INIZIA",325,616,26,(.025,.09,.10,1.),align="center")
            self.text("F1  comandi      F2  qualita' grafica",123,704,21,self.muted)
            card_y=h-240
            for i,theme in enumerate(THEMES):
                x=80+i*354;selected=i==g.selected;unlocked=i<g.unlocked
                self.panel(x,card_y,338,133)
                if selected: self.rect(x+18,card_y+1,302,3,(*theme.accent,1.))
                self.text(f"0{i+1}",x+19,card_y+20,40,(*theme.accent,1.) if unlocked else self.muted)
                names=["GIARDINI","LUCELUNA","OFFICINE","BRINA","ECLISSE"]
                self.text(names[i],x+83,card_y+24,22,self.ink if unlocked else self.muted)
                status="SELEZIONATO" if selected else ("DISPONIBILE" if unlocked else "DA SBLOCCARE")
                self.text(status,x+84,card_y+61,15,self.mint if selected else self.muted)
                best=g.best.get(str(i),{})
                if best: self.text(f"AURORA {best.get('shards',0)}/3",x+84,card_y+88,15,self.gold)
            self.text("A / D  seleziona mondo       INVIO  gioca       ESC  esci",80,h-70,22,self.muted)
            self.text("SINGLE-FILE EDITION  /  2026",1840,h-66,18,self.muted,align="right")
        else:
            self.panel(36,28,610,106)
            self.text(f"{l.index+1:02d}  /  {th.name}",62,51,23,self.ink)
            self.text(f"VITE  {g.lives:02d}",62,94,19,self.muted)
            self.text("LUMEN",210,94,18,self.muted)
            for j in range(2):
                col=self.mint if j<(1 if p.power==1 else 2) else (.22,.29,.33,1.)
                self.roundrect(306+j*25,94,16,16,5,col)
            power="FIAMMA" if p.power==3 else ("AURA STELLARE" if p.star_time>0 else "")
            self.text(power,385,94,18,self.gold)
            self.panel(1312,28,572,106)
            self.text(f"MONETE  {g.coins:03d}",1338,51,24,self.gold)
            self.text(f"AURORA  {g.level_shards}/3",1588,51,24,self.mint)
            self.text(f"PUNTI  {g.score:07d}",1338,95,17,self.muted)
            self.text(f"{int(g.elapsed)//60:02d}:{int(g.elapsed)%60:02d}",1825,91,23,self.muted,align="right")
            self.rect(40,h-24,1840,3,(.63,.73,.81,.22))
            self.rect(40,h-24,1840*clamp(p.x/l.exit_x,0,1),3,accent)
            for j,(x,_) in enumerate(l.checkpoints):
                self.rect(40+1840*x/l.exit_x,h-27,6,9,self.mint if j<=l.checkpoint_id else self.muted)
            if g.banner>0 and g.state=="play":
                self.panel(448,163,1024,93)
                self.text(th.subtitle,960,187,27,align="center")
                tip=["Tieni premuto SALTO per salire di piu'. Cerca i percorsi alti.",
                     "I ponti azzurri sono intermittenti. Le molle aprono nuove strade.",
                     "Gli ascensori dorati si muovono. Tra le torri c'e' una corrente d'aria.",
                     "Sul ghiaccio si frena lentamente. Le piattaforme fragili ricompaiono.",
                     "La lava e' letale. Il guardiano espone il nucleo dopo ogni schianto."][l.index]
                self.text(tip,960,229,16,self.muted,align="center")
            if g.toast_time>0:
                self.panel(470,h-119,980,64)
                self.text(g.toast,960,h-98,25,self.mint,align="center")
            elif g.elapsed<14 and l.index==0 and g.state=="play":
                self.panel(495,h-114,930,60)
                self.text("A/D  muovi     SPAZIO  salta     SHIFT  corri     X  fuoco",960,h-94,22,align="center")
            boss=l.boss
            if boss and boss.alive and p.x>193:
                self.panel(601,151,718,99)
                self.text("IL GUARDIANO DELL'ECLISSE",960,169,23,self.gold,align="center")
                self.rect(638,211,644,13,(.17,.20,.24,1.))
                self.rect(638,211,644*boss.hp/7,13,self.mint if boss.vulnerable>0 else (1.,.32,.13,1.))
                if boss.vulnerable>0:
                    self.text("NUCLEO ESPOSTO! COLPISCI ORA",960,265,21,self.mint,align="center")
            if g.state in ("pause","complete","victory","gameover"):
                self.rect(0,0,1920,h,(.006,.013,.024,.65))
                cy=h/2-205
                self.panel(430,cy,1060,424)
                title={"pause":"PAUSA","complete":"MONDO COMPLETATO", "victory":"AURORA RITROVATA",
                       "gameover":"NON FINISCE QUI"}[g.state]
                self.text(title,960,cy+53,48,self.ink,align="center")
                if g.state=="pause":
                    self.text("ESC / P  riprendi      INVIO  menu principale",960,cy+145,27,self.muted,align="center")
                    self.text("R  riparti dal checkpoint      F1  tutti i comandi",960,cy+196,24,self.muted,align="center")
                elif g.state=="complete":
                    self.text(f"Aurora {g.level_shards}/3    Monete {g.coins}    Tempo {g.elapsed:.1f}s",960,cy+143,29,self.gold,align="center")
                    self.text(THEMES[min(4,l.index+1)].name,960,cy+208,26,self.muted,align="center")
                elif g.state=="victory":
                    self.text(f"Punti {g.score}    Frammenti raccolti nella partita {g.total_shards}/15",960,cy+144,27,self.gold,align="center")
                    ending="L'aurora completa illumina tutti i cinque mondi." if g.total_shards==15 else "Il sole e' salvo. I frammenti rimasti ti aspettano sui percorsi alti."
                    self.text(ending,960,cy+210,21,self.muted,align="center")
                else:
                    self.text("Riparti dal checkpoint con cinque vite.",960,cy+148,29,self.muted,align="center")
                    self.text("I frammenti gia' raccolti restano tuoi.",960,cy+205,25,self.gold,align="center")
                if g.state!="pause":
                    self.roundrect(745,cy+295,430,62,10,self.mint)
                    self.text("INVIO  /  CONTINUA",960,cy+316,23,(.03,.10,.10,1.),align="center")
            if g.state=="dead":
                self.text("UNA SCINTILLA NON SI SPEGNE COSI'",960,h*.43,31,self.ink,align="center")
        if g.stats:
            py=h-338
            self.panel(1395,py,490,232)
            self.text(f"{fps:5.1f} FPS   /   GPU {renderer.gpu_ms:5.2f} ms",1418,py+22,22,self.mint)
            self.text(f"{renderer.quality_name.upper()}  /  {renderer.rw} x {renderer.rh}",1418,py+64,19,self.ink)
            self.text(f"{renderer.instance_count} istanze  /  {renderer.triangle_count:,} triangoli",1418,py+102,17,self.muted)
            name=renderer.gpu_name
            if len(name)>49: name=name[:46]+"..."
            self.text(name,1418,py+140,16,self.muted)
            self.text("F2 qualita'   F12 screenshot",1418,py+186,17,self.gold)
        if g.help:
            self.rect(0,0,1920,h,(.008,.016,.028,.87))
            y=max(45,h/2-400)
            self.panel(435,y,1050,790)
            self.text("OGNI SALTO CONTA",960,y+42,44,self.mint,align="center")
            lines=[("A / D  oppure frecce", "Muovi Lumen"),
                   ("SPAZIO / Z / W", "Salta; tieni premuto per un salto alto"),
                   ("SHIFT", "Corri: salti piu' lunghi"),
                   ("X / CTRL", "Lancia fuoco con il potenziamento solare"),
                   ("S / freccia GIU' in aria", "Schianto a terra"),
                   ("R", "Riparti dal checkpoint, consumando una vita"),
                   ("P / ESC", "Pausa; INVIO dalla pausa torna al menu"),
                   ("F2 / F3", "Qualita' grafica / statistiche GPU"),
                   ("F11 / F12 / M", "Schermo intero / screenshot / audio"),
                   ("Controller Xbox-style", "A salta, B corre, X fuoco, Start pausa")]
            for j,(key,meaning) in enumerate(lines):
                yy=y+128+j*46
                self.text(key,483,yy,20,self.gold)
                self.text(meaning,835,yy,19,self.ink)
            self.text("Salta sui nemici. Colpisci i blocchi da sotto. Calcia i gusci.",960,y+634,23,self.muted,align="center")
            self.text("Tre frammenti segreti per mondo. I portali non richiedono tutti i frammenti.",960,y+679,20,self.muted,align="center")
            self.text("F1  /  CHIUDI",960,y+736,23,self.mint,align="center")

    def draw(self):
        if not self.vertices: return
        gl=self.gl
        a=np.asarray(self.vertices,dtype=np.float32)
        gl.BindVertexArray(self.vao);gl.BindBuffer(GL_ARRAY_BUFFER,self.vbo)
        gl.BufferData(GL_ARRAY_BUFFER,a.nbytes,C.c_void_p(a.ctypes.data),GL_DYNAMIC_DRAW)
        gl.Disable(GL_DEPTH_TEST);gl.Enable(0x0BE2)
        gl.BlendFunc(0x0302,0x0303)
        self.program.use()
        scale=min(self.pixel_width/self.width,self.pixel_height/self.height)
        self.program.v2("uSize",self.pixel_width,self.pixel_height)
        self.program.f("uScale",scale)
        self.program.v2("uOffset",(self.pixel_width-self.width*scale)/2,
                         (self.pixel_height-self.height*scale)/2)
        self.texture.bind(0);self.program.i("uFont",0)
        gl.DrawArrays(0x0004,0,len(a))
        gl.Disable(0x0BE2)


class Renderer:
    def __init__(self,window,args):
        self.window=window;self.gl=GL(window.sdl);self.args=args
        gl=self.gl
        self.gpu_name=gl.GetString(0x1F01).decode("utf-8","replace")
        self.gl_version=gl.GetString(0x1F02).decode("utf-8","replace")
        print("GPU:",self.gpu_name,"\nOpenGL:",self.gl_version)
        if any(s in self.gpu_name.lower() for s in ("llvmpipe","softpipe","software")):
            print("ATTENZIONE: renderer software. Sul PC di gioco deve comparire NVIDIA RTX 3090.")
        self.program=Program(gl,MESH_VERT,SCENE_FRAG,"scene")
        self.shadow_program=Program(gl,MESH_VERT,SHADOW_FRAG,"shadow")
        self.sky=Program(gl,FULLSCREEN_VERT,SKY_FRAG,"sky")
        self.effects_program=Program(gl,FULLSCREEN_VERT,EFFECT_FRAG,"ao_and_light")
        self.bloom_program=Program(gl,FULLSCREEN_VERT,BLOOM_FRAG,"bloom")
        self.final_program=Program(gl,FULLSCREEN_VERT,FINAL_FRAG,"tonemap")
        self.fullscreen_vao=gl.gen("VertexArrays")
        self.meshes={"cube":Mesh(gl,mesh_cube()),"round":Mesh(gl,mesh_cube(True)),
                     "sphere":Mesh(gl,mesh_sphere(16,28)),"cylinder":Mesh(gl,mesh_cylinder()),
                     "cone":Mesh(gl,mesh_cylinder(True)),"torus":Mesh(gl,mesh_torus()),
                     "grass":Mesh(gl,mesh_grass())}
        self.interface=Interface(gl);self.scene=Scene()
        self.targets=[];self.quality_name=args.quality
        self.width,self.height=window.size()
        self.gpu_ms=0.;self.gpu_samples=[];self.query_pending=deque()
        self.query_free=[gl.gen("Queries") for _ in range(5)]
        self.instance_count=0;self.triangle_count=0
        self.configure()

    def configure(self):
        gl=self.gl
        for target in self.targets: target.delete()
        self.targets=[]
        self.q=dict(QUALITY[self.quality_name])
        if self.args.smoke_test:
            self.q.update(shadow=512,pcf=1,ao=8,volume=4,cloud=3,scale=1.)
        scale=self.args.scale if self.args.scale is not None else self.q["scale"]
        maximum=C.c_int();gl.GetIntegerv(0x0D33,C.byref(maximum))
        cap=min(8192,maximum.value)
        scale=min(scale,cap/self.width,cap/self.height)
        self.rw=max(160,int(self.width*scale));self.rh=max(90,int(self.height*scale))
        self.hdr=Target(gl,self.rw,self.rh,2,True)
        self.shadow=Target(gl,self.q["shadow"],self.q["shadow"],0,True)
        self.effects=Target(gl,max(80,self.rw//2),max(45,self.rh//2))
        self.bloom=[]
        for i in range(5):
            self.bloom.append(Target(gl,max(8,self.rw//(2**(i+1))),max(8,self.rh//(2**(i+1)))))
        self.targets=[self.hdr,self.shadow,self.effects,*self.bloom]
        gl.BindFramebuffer(GL_FRAMEBUFFER,0)
        err=gl.GetError()
        if err: raise RuntimeError(f"Allocazione GPU fallita (GL {err:#x}); prova --quality high --scale 1.")
        print(f"Profilo {self.quality_name}: output {self.width}x{self.height}, render {self.rw}x{self.rh}, ombre {self.q['shadow']}.")

    def change_quality(self):
        choices=list(QUALITY);self.quality_name=choices[(choices.index(self.quality_name)+1)%len(choices)]
        self.configure()

    def fullquad(self):
        self.gl.BindVertexArray(self.fullscreen_vao);self.gl.DrawArrays(0x0004,0,3)

    def query_start(self):
        gl=self.gl
        while self.query_pending:
            q=self.query_pending[0];available=C.c_int()
            gl.GetQueryObjectiv(q,0x8867,C.byref(available))
            if not available.value: break
            ns=C.c_uint64();gl.GetQueryObjectui64v(q,0x8866,C.byref(ns))
            elapsed=ns.value/1e6
            self.gpu_ms=elapsed if not self.gpu_ms else self.gpu_ms*.9+elapsed*.1
            self.gpu_samples.append(elapsed)
            if len(self.gpu_samples)>30000: del self.gpu_samples[:10000]
            self.query_free.append(self.query_pending.popleft())
        if not self.query_free: return None
        q=self.query_free.pop();gl.BeginQuery(0x88BF,q);return q

    def draw(self,g,fps=0.):
        gl=self.gl
        w,h=self.window.size()
        if (w,h)!=(self.width,self.height):
            self.width,self.height=w,h;self.configure()
        batches=self.scene.build(g)
        self.instance_count=0;self.triangle_count=0
        for name,mesh in self.meshes.items():
            mesh.upload(batches.get(name,[]))
            self.instance_count+=mesh.instances
            self.triangle_count+=mesh.count//3*mesh.instances
        q=self.query_start()
        th=g.level.theme
        sx=math.sin(g.fx_time*65)*g.shake*.12
        sy=math.cos(g.fx_time*49)*g.shake*.06
        centre=(g.cam_x+sx,g.cam_y+sy,0.)
        eye=(centre[0],centre[1]+5.,22.)
        aspect=self.width/self.height
        view=look_at(eye,centre)
        half_y=7.1
        vp=ortho(-half_y*aspect,half_y*aspect,-half_y,half_y,.1,95.)@view
        inv=np.linalg.inv(vp)
        light_dir=vnormalize((-0.45,.79,.43))
        lc=np.array((g.cam_x,3.5,-3),dtype=np.float32)
        lightview=look_at(lc+light_dir*40,lc)
        lightvp=ortho(-31,31,-25,25,1,95)@lightview
        gl.Disable(0x0BE2);gl.Enable(GL_DEPTH_TEST);gl.DepthFunc(0x0203);gl.DepthMask(1)
        self.shadow.use();gl.Clear(GL_DEPTH_BUFFER_BIT)
        sp=self.shadow_program;sp.use();sp.mat("uVP",lightvp);sp.f("uTime",g.fx_time)
        for mesh in self.meshes.values(): mesh.draw()
        self.hdr.use();gl.ClearColor(0,0,0,0);gl.Clear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT)
        gl.Disable(GL_DEPTH_TEST)
        sk=self.sky;sk.use();sk.v3("uTop",th.sky_top);sk.v3("uBottom",th.sky_bottom)
        sk.v3("uSun",th.sun);sk.f("uTime",g.fx_time);sk.f("uCamera",g.cam_x)
        sk.f("uAspect",aspect);sk.i("uTheme",g.level.index);sk.i("uCloud",self.q["cloud"])
        self.fullquad()
        gl.Enable(GL_DEPTH_TEST)
        p=self.program;p.use();p.mat("uVP",vp);p.mat("uLightVP",lightvp)
        p.v3("uEye",eye);p.v3("uSunDir",light_dir);p.v3("uSunColor",th.sun)
        p.v3("uFog",th.fog);p.f("uAmbient",th.ambient);p.f("uTime",g.fx_time)
        p.i("uTheme",g.level.index);p.i("uPCF",self.q["pcf"])
        self.shadow.depth.bind(0);p.i("uShadow",0)
        lamps=[]
        for pickup in g.level.pickups:
            if not pickup.taken and pickup.kind!="coin" and abs(pickup.x-g.cam_x)<22:
                color=(1.,.46,.14) if pickup.kind in ("fire","grow") else th.accent
                lamps.append((abs(pickup.x-g.cam_x),(pickup.x,pickup.y,.9,2.5),(*color,1.)))
        for s in g.projectiles:
            lamps.append((abs(s.x-g.cam_x)-5,(s.x,s.y,.7,2.5),(1.,.40,.07,1.)))
        if g.player.star_time>0:
            lamps.append((-99,(g.player.x,g.player.y+1,1.,4.),(.35,1.,.8,1.)))
        lamps.sort(key=lambda item:item[0]);lamps=lamps[:6]
        while len(lamps)<6: lamps.append((0,(0,0,0,0),(0,0,0,0)))
        p.vec4_array("uLights[0]",[q[1] for q in lamps])
        p.vec4_array("uLightColors[0]",[q[2] for q in lamps])
        for mesh in self.meshes.values(): mesh.draw()
        gl.Disable(GL_DEPTH_TEST)
        self.effects.use();ep=self.effects_program;ep.use()
        self.hdr.depth.bind(0);self.hdr.colors[1].bind(1);self.shadow.depth.bind(2)
        ep.i("uDepth",0);ep.i("uNormal",1);ep.i("uShadow",2)
        ep.mat("uInvVP",inv);ep.mat("uLightVP",lightvp);ep.v3("uEye",eye)
        ep.v3("uSunColor",th.sun);ep.v2("uResolution",self.rw,self.rh)
        ep.i("uAO",self.q["ao"]);ep.i("uVolume",self.q["volume"])
        ep.f("uDensity",.055 if g.level.index in (1,4) else .045)
        self.fullquad()
        bp=self.bloom_program;bp.use();bp.i("uSource",0)
        source=self.hdr.colors[0]
        for i,target in enumerate(self.bloom):
            target.use();source.bind(0);bp.v2("uPixel",1/source.w,1/source.h)
            bp.i("uFirst",int(i==0));self.fullquad();source=target.colors[0]
        gl.BindFramebuffer(GL_FRAMEBUFFER,0);gl.Viewport(0,0,self.width,self.height)
        fp=self.final_program;fp.use()
        self.hdr.colors[0].bind(0);self.effects.colors[0].bind(1)
        fp.i("uScene",0);fp.i("uEffects",1);fp.v2("uPixel",1/self.rw,1/self.rh)
        fp.f("uTime",g.fx_time)
        for i,target in enumerate(self.bloom):
            target.colors[0].bind(i+2);fp.i("uBloom"+str(i),i+2)
        self.fullquad()
        if q is not None:
            gl.EndQuery(0x88BF);self.query_pending.append(q)
        self.interface.build(g,self,fps);self.interface.draw()

    def screenshot(self,path):
        from PIL import Image
        self.gl.PixelStorei(0x0D05,1)
        a=np.empty((self.height,self.width,3),dtype=np.uint8)
        self.gl.ReadBuffer(0x0405)
        self.gl.ReadPixels(0,0,self.width,self.height,0x1907,GL_UNSIGNED_BYTE,C.c_void_p(a.ctypes.data))
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        Image.fromarray(a[::-1]).save(path)
        return str(path)

    def close(self):
        for target in self.targets: target.delete()
        self.targets=[]
        # Remaining GL objects are released with the SDL-owned GL context.

# ---------------------------------------------------------------------------
# Deterministic tests: not a substitute for a full human playtest of difficulty.
# ---------------------------------------------------------------------------
class LogicTests(unittest.TestCase):
    def flat_game(self):
        g=Game(False);g.start(0)
        g.level.platforms=[Platform(-50.,-4.,500.,4.,"ground")]
        g.level.pickups=[];g.level.enemies=[];g.level.hazards=[];g.level.checkpoints=[]
        g.level.exit_x=400.;g.level.boss=None;g.level.length=450.
        for _ in range(30): g.tick(FIXED_DT,Controls())
        return g

    def test_five_worlds_and_three_shards_each(self):
        for i in range(5):
            l=Level(i)
            self.assertEqual(sum(p.kind=="shard" for p in l.pickups),3)
            self.assertGreater(len(l.enemies),5)
            self.assertEqual(len(l.checkpoints),3)
            self.assertGreater(l.exit_x,190)
            self.assertTrue(any(p.kind=="ground" and p.x<l.exit_x<p.x+p.w for p in l.platforms))

    def test_checkpoints_have_safe_ground(self):
        for i in range(5):
            l=Level(i)
            for x,y in l.checkpoints:
                self.assertTrue(any(p.kind=="ground" and p.x<x and x+.66<p.x+p.w and p.top==y for p in l.platforms))
                self.assertFalse(any(overlap(x,y,.66,1.52,*h[:4]) for h in l.hazards))

    def test_main_ground_gaps_within_running_jump_range(self):
        # Necessary reachability condition; does not claim to solve enemy timing.
        reach=RUN_SPEED*(2*JUMP_SPEED/GRAVITY)+Player().w
        for i in range(5):
            floor=sorted((p.x,p.x+p.w) for p in Level(i).platforms if p.kind=="ground")
            for (_,right),(left,_) in zip(floor,floor[1:]):
                self.assertLess(left-right,reach*.80)

    def test_full_jump_height(self):
        g=self.flat_game();peak=0.
        for j in range(150):
            g.tick(FIXED_DT,Controls(jump=True,jump_pressed=j==0))
            peak=max(peak,g.player.y)
        self.assertGreater(peak,2.75);self.assertLess(peak,2.96)
        self.assertAlmostEqual(g.player.y,0.,places=5)
        self.assertTrue(g.player.grounded)

    def test_variable_jump_and_no_auto_bunnyhop(self):
        g=self.flat_game();peak=0.
        for j in range(150):
            g.tick(FIXED_DT,Controls(jump=j<6,jump_pressed=j==0))
            peak=max(peak,g.player.y)
        self.assertLess(peak,1.6);self.assertGreater(peak,.4)
        self.assertAlmostEqual(g.player.y,0.,places=5)

    def test_run_is_faster_and_fixed_step_stable(self):
        slow=self.flat_game();fast=self.flat_game()
        for _ in range(120):
            slow.tick(FIXED_DT,Controls(axis=1))
            fast.tick(FIXED_DT,Controls(axis=1,run=True))
        self.assertGreater(fast.player.x,slow.player.x+2.)
        self.assertLessEqual(fast.player.vx,RUN_SPEED)
        self.assertTrue(math.isfinite(fast.player.y))

    def test_coyote_jump(self):
        g=self.flat_game();p=g.player
        p.grounded=False;p.y=.2;p.coyote=.08
        g.tick(FIXED_DT,Controls(jump=True,jump_pressed=True))
        self.assertGreater(p.vy,12.)

    def test_buffered_jump_on_landing(self):
        g=self.flat_game();p=g.player
        p.grounded=False;p.support=None;p.y=.15;p.vy=-4.;p.coyote=0.
        for j in range(8): g.tick(FIXED_DT,Controls(jump=True,jump_pressed=j==0))
        self.assertGreater(p.vy,10.)

    def test_coin_is_not_collected_twice(self):
        g=self.flat_game();g.level.pickups=[Pickup(g.player.x+.3,.7)]
        for _ in range(10):g.tick(FIXED_DT,Controls())
        self.assertEqual(g.coins,1)
        self.assertEqual(g.score,100)

    def test_damage_and_invulnerability(self):
        g=self.flat_game();g.player.power=3
        g.damage(0.)
        self.assertEqual(g.player.power,1)
        self.assertEqual(g.lives,5)
        g.damage(0.);self.assertEqual(g.lives,5)
        g.player.invuln=0.;g.damage(0.)
        self.assertEqual(g.lives,4);self.assertEqual(g.state,"dead")

    def test_checkpoint_preserves_collected_items(self):
        g=Game(False);g.start(0);g.level.checkpoint_id=1
        g.level.pickups[0].taken=True
        g.respawn()
        self.assertEqual(g.player.x,g.level.checkpoints[1][0])
        self.assertTrue(g.level.pickups[0].taken)
        self.assertEqual(g.state,"play")

    def test_boss_requires_exposure(self):
        g=Game(False);g.start(4);b=g.level.boss
        self.assertFalse(g.hit_boss(b));self.assertEqual(b.hp,7)
        for _ in range(7):
            b.hurt=0.;b.vulnerable=2.
            self.assertTrue(g.hit_boss(b))
        self.assertFalse(b.alive)

    def test_final_portal_is_locked_before_boss(self):
        g=Game(False);g.start(4);g.player.x=g.level.exit_x+1
        g.tick(FIXED_DT,Controls())
        self.assertEqual(g.state,"play")
        g.level.boss.alive=False;g.tick(FIXED_DT,Controls())
        self.assertEqual(g.state,"victory")

    def test_moving_support_transports_player(self):
        g=self.flat_game()
        b=Platform(2.,2.,4.,.48,"moving",amplitude=2.,speed=1.)
        g.level.platforms.append(b)
        p=g.player;p.x=3.;p.y=b.top;p.support=b;p.grounded=True
        start=p.x
        for _ in range(30):g.tick(FIXED_DT,Controls())
        self.assertGreater(p.x,start+.3)
        self.assertAlmostEqual(p.y,b.top,places=3)

    def test_crumbling_platform_recovers(self):
        l=Level(2);b=next(b for b in l.platforms if b.kind=="crumble")
        b.timer=0.;p=Player()
        for j in range(100):l.update(j*FIXED_DT,FIXED_DT,p)
        self.assertFalse(b.active)
        for j in range(500):l.update(j*FIXED_DT,FIXED_DT,p)
        self.assertTrue(b.active);self.assertEqual(b.timer,-1.)

    def test_enemy_stomp_and_shell_kick(self):
        g=self.flat_game();p=g.player
        e=Enemy(p.x,.0,"walker");g.level.enemies=[e]
        p.y=.82;p.last_y=1.05;p.vy=-6.
        g.update_enemies(FIXED_DT)
        self.assertFalse(e.alive);self.assertGreater(p.vy,0.)
        e=Enemy(p.x,0.,"turtle",vx=0.,shell=True)
        g.level.enemies=[e];p.y=0.;p.vy=0.;p.last_y=0.
        g.update_enemies(FIXED_DT)
        self.assertGreater(abs(e.vx),10.)

    def test_question_block_once_and_brick_break(self):
        g=self.flat_game();b=Platform(3,2.5,.95,.95,"question",payload="coin")
        g.hit_block(b);g.hit_block(b)
        self.assertEqual(g.coins,1);self.assertTrue(b.used)
        brick=Platform(3,2.5,.95,.95,"brick")
        g.player.power=2;g.hit_block(brick)
        self.assertFalse(brick.active)

    def test_growth_under_question_block_does_not_clip(self):
        g=self.flat_game()
        b=Platform(3.,2.4,.95,.95,"question",payload="grow")
        g.level.platforms.append(b)
        for j in range(50):
            g.tick(FIXED_DT,Controls(jump=True,jump_pressed=j==0))
        self.assertTrue(b.used)
        self.assertEqual(g.player.power,2)
        p=g.player
        self.assertFalse(overlap(p.x+.001,p.y+.001,p.w-.002,p.h-.002,b.x,b.y,b.w,b.h))

    def test_spring_launch_does_not_need_jump_held(self):
        g=self.flat_game()
        b=Platform(2.,0.,3.,.25,"spring")
        g.level.platforms.append(b)
        g.player.y=.30;g.player.vy=-1.;g.player.grounded=False;g.player.support=None
        peak=0.
        for _ in range(90):
            g.tick(FIXED_DT,Controls())
            peak=max(peak,g.player.y)
        self.assertGreater(peak,5.)

    def test_all_worlds_physics_remain_finite(self):
        rng=random.Random(710)
        for i in range(5):
            g=Game(False);g.start(i)
            for j in range(1200):
                if g.state=="gameover":g.lives=5;g.respawn()
                if g.state in ("complete","victory"):break
                c=Controls(axis=1.,jump=j%95<60,jump_pressed=j%95==0,
                           run=True,fire=True,down=False)
                g.tick(FIXED_DT,c)
                self.assertTrue(math.isfinite(g.player.x))
                self.assertTrue(math.isfinite(g.player.y))

    def test_main_routes_can_be_traversed_without_hazards(self):
        # Tests authored collision geometry with a deterministic runner.
        # Enemy avoidance, vent timing and secret routes still need human playtesting.
        for idx in range(5):
            g=Game(False);g.start(idx)
            g.level.enemies=[];g.level.hazards=[];g.level.pickups=[];g.level.boss=None
            g.player.power=2
            for _ in range(6000):
                p=g.player
                if g.state!="play":break
                jump=False
                if p.grounded and p.support:
                    edge=p.support.x+p.support.w
                    danger=not any(b.active and b.kind=="ground" and
                        b.x<=edge+.4<=b.x+b.w for b in g.level.platforms)
                    jump=danger and edge-(p.x+p.w)<2.
                g.tick(FIXED_DT,Controls(axis=1.,jump=True,jump_pressed=jump,run=True))
            self.assertIn(g.state,("complete","victory"),f"World {idx+1}, x={g.player.x}")

    def test_matrix_roundtrip(self):
        m=ortho(-10,10,-5,5,.1,90)@look_at((0,8,22),(0,3,0))
        p=np.array([3,2,-1,1],dtype=np.float32)
        self.assertTrue(np.allclose(np.linalg.inv(m)@(m@p),p,atol=1e-4))


def run_self_tests():
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(LogicTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def run_smoke_test(renderer,g,args):
    """Real SDL/OpenGL draw calls on all worlds, with optional PNG evidence."""
    for i in range(5):
        g.start(i);g.player.x=17.;g.player.y=0.;g.player.power=3
        g.cam_x=19.;g.banner=0.;g.stats=False
        for j in range(3):
            g.fx_time+=.16;g.time+=.16
            g.level.update(g.time,FIXED_DT,g.player)
            renderer.draw(g,60.)
            renderer.gl.Finish()
            err=renderer.gl.GetError()
            if err: raise RuntimeError(f"Smoke test world {i+1}: OpenGL error {err:#x}")
        if args.smoke_output:
            renderer.screenshot(Path(args.smoke_output)/f"world_{i+1}.png")
        print(f"RENDER OK / mondo {i+1} / {renderer.instance_count} istanze / {renderer.triangle_count} triangoli")
    # Verify menu and all overlays as well, including the final boss model.
    g.start(4);g.cam_x=215;g.player.x=207.;g.player.power=3;g.level.boss.vulnerable=1.
    renderer.draw(g,60.)
    if args.smoke_output: renderer.screenshot(Path(args.smoke_output)/"boss.png")
    g.load_level(0);g.state="menu";g.selected=0
    renderer.draw(g,60.)
    if args.smoke_output: renderer.screenshot(Path(args.smoke_output)/"menu.png")
    for state in ("pause","complete","victory","gameover"):
        g.state=state;renderer.draw(g,60.)
    g.help=True;renderer.draw(g,60.)
    renderer.gl.Finish()
    err=renderer.gl.GetError()
    if err: raise RuntimeError(f"Smoke test overlay: OpenGL error {err:#x}")
    print("SMOKE TEST COMPLETATO: cinque mondi, boss, menu e interfacce.")


def parse_args(argv=None):
    parser=argparse.ArgumentParser(description="SUPER LUMEN / Aurora Worlds - platform 2.5D in un file Python",
                                   formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--width",type=int,default=1920,help="Larghezza finestra")
    parser.add_argument("--height",type=int,default=1080,help="Altezza finestra")
    parser.add_argument("--fullscreen",action="store_true",help="Schermo intero alla risoluzione del desktop")
    parser.add_argument("--quality",choices=QUALITY,default="ultra",help="Profilo grafico")
    parser.add_argument("--scale",type=float,help="Scala render interna: 0.5..2.0; override del preset")
    parser.add_argument("--fps",type=int,default=144,help="Limite FPS CPU, 0 per disattivarlo")
    parser.add_argument("--no-vsync",action="store_true",help="Disattiva il VSync; possibile tearing")
    parser.add_argument("--mute",action="store_true",help="Nessun audio")
    parser.add_argument("--no-save",action="store_true",help="Non legge e non scrive progressi")
    parser.add_argument("--level",type=int,choices=range(1,6),help="Avvia direttamente un mondo (1..5)")
    parser.add_argument("--self-test",action="store_true",help="Test logica senza finestra o audio")
    parser.add_argument("--smoke-test",action="store_true",help="Test rendering OpenGL su tutti i mondi")
    parser.add_argument("--smoke-output",type=Path,help="Directory PNG del test grafico")
    parser.add_argument("--benchmark",type=float,default=0.,metavar="SECONDI",help="Tour grafico automatico, senza scrivere salvataggi")
    args=parser.parse_args(argv)
    if not 320<=args.width<=7680 or not 240<=args.height<=4320:
        parser.error("Dimensioni supportate: larghezza 320..7680; altezza 240..4320.")
    if args.scale is not None and not .5<=args.scale<=2.:
        parser.error("--scale deve essere compreso fra 0.5 e 2.0.")
    if args.fps<0 or args.benchmark<0 or args.benchmark>3600:
        parser.error("FPS non negativi; benchmark fra 0 e 3600 secondi.")
    if args.smoke_test:
        if args.width==1920: args.width=960
        if args.height==1080: args.height=540
        args.mute=True
    return args


def main(argv=None):
    args=parse_args(argv)
    if args.self_test: return 0 if run_self_tests() else 1
    window=None;renderer=None;audio=None
    try:
        window=Window(args)
        renderer=Renderer(window,args)
        audio=SynthAudio(window.sdl,args.mute or args.smoke_test)
        game=Game(not(args.no_save or args.smoke_test or args.benchmark),audio)
        if args.smoke_test:
            run_smoke_test(renderer,game,args);return 0
        if args.level: game.start(args.level-1)
        if args.benchmark: game.start(0);game.stats=True
        clock=time.perf_counter;last=clock();accumulator=0.;pending_jump=False
        fps=60.;frame_samples=[];benchmark_start=last
        help_pause=False
        while not window.closed:
            frame_start=clock();real_dt=frame_start-last;last=frame_start
            real_dt=max(real_dt,1e-6)
            dt=min(real_dt,.10)
            fps=fps*.95+(1/real_dt)*.05
            controls,pressed,lost_focus=window.poll()
            pending_jump=pending_jump or controls.jump_pressed
            screenshot=False
            if 58 in pressed:
                game.help=not game.help
                if game.help and game.state=="play":
                    game.state="pause";help_pause=True
                elif not game.help and help_pause:
                    if game.state=="pause": game.state="play"
                    help_pause=False
            if 59 in pressed:
                renderer.change_quality()
                game.message("GRAFICA / "+renderer.quality_name.upper())
            if 60 in pressed: game.stats=not game.stats
            if 68 in pressed: window.toggle_fullscreen()
            if 69 in pressed: screenshot=True
            if 16 in pressed:
                audio.muted=not audio.muted
                game.message("AUDIO DISATTIVATO" if audio.muted else "AUDIO ATTIVATO")
            if lost_focus and game.state=="play" and not args.benchmark:
                game.state="pause"
            if game.state=="menu":
                move=(-1 if {4,80}&pressed else 0)+(1 if {7,79}&pressed else 0)
                if move:
                    game.selected=clamp(game.selected+move,0,game.unlocked-1)
                    game.load_level(game.selected)
                if 40 in pressed:
                    game.help=False;game.start(game.selected)
                    accumulator=0.;pending_jump=False
                if 41 in pressed:
                    if game.help: game.help=False
                    else: window.closed=True
            elif game.state in ("complete","victory"):
                if 40 in pressed: game.next_level();pending_jump=False
                if 41 in pressed: game.state="menu";game.load_level(game.selected)
            elif game.state=="gameover":
                if 40 in pressed: game.lives=5;game.respawn()
                if 41 in pressed: game.state="menu";game.load_level(game.selected)
            elif game.state in ("play","pause"):
                if 41 in pressed or 19 in pressed:
                    if game.help:
                        game.help=False
                        if help_pause: game.state="play";help_pause=False
                    else: game.state="pause" if game.state=="play" else "play"
                elif game.state=="pause" and 40 in pressed and not game.help:
                    game.selected=game.level.index
                    game.state="menu";game.load_level(game.selected)
                if 21 in pressed:
                    game.help=False;help_pause=False;game.state="play";game.die()
            if args.benchmark:
                elapsed=clock()-benchmark_start
                if elapsed>=args.benchmark: break
                segment=max(.1,args.benchmark/5)
                index=min(4,int(elapsed/segment))
                if game.level.index!=index: game.load_level(index);game.state="play"
                fraction=clamp((elapsed-index*segment)/segment,0,1)
                game.cam_x=9+fraction*(game.level.length-20)
                game.cam_y=4.3
                game.player.x=game.cam_x+2.;game.player.y=0.;game.player.power=3
                game.player.vx=6.;game.banner=0.
                game.fx_time+=dt;game.time+=dt
                game.level.update(game.time,dt,game.player)
                frame_samples.append(real_dt*1000)
            else:
                accumulator=min(accumulator+dt,.2)
                while accumulator>=FIXED_DT:
                    c=Controls(controls.axis,controls.jump,pending_jump,
                               controls.run,controls.fire,controls.down)
                    game.tick(FIXED_DT,c)
                    pending_jump=False;accumulator-=FIXED_DT
                if game.state not in ("play","dead"): pending_jump=False
            audio.update(game.level.index,game.state)
            renderer.draw(game,fps)
            if screenshot:
                path=DATA_DIR/"screenshots"/(time.strftime("lumen_%Y%m%d_%H%M%S")+".png")
                try:
                    renderer.screenshot(path);game.message("SCREENSHOT SALVATO")
                    print("Screenshot:",path)
                except OSError as exc: game.message("SCREENSHOT NON SALVATO");print(exc)
            window.sdl.GL_SwapWindow(window.handle)
            if args.fps and not args.benchmark:
                remaining=1/args.fps-(clock()-frame_start)
                if remaining>.001: time.sleep(remaining-.0005)
        if args.benchmark and frame_samples:
            a=np.asarray(frame_samples[5:] or frame_samples)
            gpu=np.asarray(renderer.gpu_samples[5:] or renderer.gpu_samples or [0.])
            report={"gpu":renderer.gpu_name,"opengl":renderer.gl_version,
                    "quality":renderer.quality_name,"output":[renderer.width,renderer.height],
                    "internal":[renderer.rw,renderer.rh],"seconds":args.benchmark,
                    "frames":len(frame_samples),"average_fps":float(1000/np.mean(a)),
                    "frame_ms_p50":float(np.percentile(a,50)),"frame_ms_p95":float(np.percentile(a,95)),
                    "frame_ms_p99":float(np.percentile(a,99)),"gpu_ms_mean":float(np.mean(gpu)),
                    "note":"Camera tour; includes CPU, asset regeneration at world changes, and GPU work. Not an interactive gameplay benchmark."}
            print(json.dumps(report,indent=2))
            try:
                DATA_DIR.mkdir(parents=True,exist_ok=True)
                path=DATA_DIR/("benchmark_"+time.strftime("%Y%m%d_%H%M%S")+".json")
                path.write_text(json.dumps(report,indent=2));print("Report:",path)
            except OSError as exc: print("Report non salvato:",exc)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("\nAvvio/esecuzione non riusciti:",exc,file=sys.stderr)
        print("Ubuntu: sudo apt install python3-numpy python3-pil libsdl2-2.0-0\n"
              "Usa /usr/bin/python3. Verifica il driver con nvidia-smi.\n"
              "Prova --quality high --scale 1 --width 1280 --height 720.\n"
              "Con problemi Wayland prova: SDL_VIDEODRIVER=x11 /usr/bin/python3 super_lumen.py",
              file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        if audio: audio.close()
        if renderer: renderer.close()
        if window: window.close()


if __name__=="__main__":
    raise SystemExit(main())
