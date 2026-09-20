"""Neon MIDI Rings - a vertical, collision-driven musical Pygame simulation.

Bounce = next MIDI event. Gap crossing = independent pass sound.
All physics and drawing use 1080x1920 logical coordinates; only the final frame
is scaled for the development window.
"""

from __future__ import annotations

import heapq
import argparse
import json
import math
import os
import random
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue, Full
from typing import Deque, Iterable, Optional

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

BASE_DIR = Path(__file__).resolve().parent
_dll_handles = []
if sys.platform == "win32":
    # Prefer the optional project-local official FluidSynth binary, so users do
    # not need administrator rights or a machine-wide PATH modification.
    vendor_root = BASE_DIR / "vendor" / "fluidsynth"
    native_bin = next(vendor_root.glob("*/bin"), None) if vendor_root.is_dir() else None
    if native_bin is not None:
        os.environ["PATH"] = str(native_bin) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            _dll_handles.append(os.add_dll_directory(str(native_bin)))

import mido
import pygame
import pygame.gfxdraw
import imageio_ffmpeg
from physics import PhysicsEngine, SimulationStats, delta
from recording_audio import soundtrack, mux
import numpy as np

try:
    import fluidsynth
    FLUIDSYNTH_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # Missing module or native DLL; report both clearly later.
    fluidsynth = None
    FLUIDSYNTH_IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Central configuration
# ---------------------------------------------------------------------------
WIDTH, HEIGHT =  1080,1920
CENTER_X, CENTER_Y = WIDTH / 2, HEIGHT / 2
SCALE = 0.5
FPS = 30                         # 120 is supported.
GAME_MODE = "classic"           # "classic", "race", or "prediction"
SHARED_MIDI = True

NUM_RINGS = 13               # Keep between 12 and 25.
INNER_RING_RADIUS = 95
RING_SPACING = 35
RING_THICKNESS = 10
GAP_SIZE_DEGREES = 30            # Smaller than the original 50°, but playable.
ROTATION_SPEED_MIN = 1.5
ROTATION_SPEED_MAX = 1.90

BALL_RADIUS = 12
BALL_SPEED = 400
MAX_BALL_SPEED = 3000
SPEED_BOOST_PER_RING = 1.22332
GRAVITY = 500
BOUNCE_RESTITUTION = 1.0
PHYSICS_SUBSTEPS = 4
MAX_PHYSICS_SUBSTEPS = 16
PHYSICS_HZ = 240
SIMULATION_SEED = 89
RANDOMIZE_SEED_ON_RESTART = False
SEAMLESS_LOOP = True
AUTO_SEARCH_RUNS = 0             # 100 searches seeds without rendering/audio.
TARGET_DURATION_MIN = 15
TARGET_DURATION_MAX = 35
SEARCH_TIMEOUT = 90
SEARCH_STALL_TIMEOUT = 18
NEAR_MISS_COOLDOWN = 1.1
NEAR_MISS_ANGLE = math.radians(10)
SPEED_RAMP = True

TRAIL_LENGTH = 10
PARTICLE_COUNT = 35
NOTE_COOLDOWN_MS = 60
CHORD_THRESHOLD_MS = 30
MIN_NOTE_DURATION = 0.0000005
MAX_NOTE_DURATION = 0.0000010

MIDI_FILE = "music/music.mid"
SOUNDFONT_FILE = "assets/soundfont.sf2"
PASS_SOUND = "sounds/pass.wav"  # .wav and .ogg are accepted by pygame.
PASS_SOUND_VOLUME = 0.8
MIDI_VOLUME = 0.8
MIDI_LOOP = True

FLASH_DURATION = 0.08
SHAKE_DURATION = 0.15
SHAKE_STRENGTH = 8
AUTO_RESTART = True
RESTART_DELAY = 0.85
PREDICTION_DELAY = 2.0
RECORD_ON_START = False             # True starts recording immediately.
RECORDING_FPS = 20
RECORDING_CRF = 18                  # Lower = better quality/larger file.
DEBUG = False

BG = (4, 5, 12)
WHITE = (240, 248, 255)
BLUE = (40, 175, 255)
RED = (255, 55, 105)
NEON_PALETTE = [
    (0, 235, 255), (116, 80, 255), (255, 55, 190),
    (255, 145, 45), (65, 255, 145), (40, 130, 255),
]

TAU = math.tau


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def angle_delta(a: float, b: float) -> float:
    """Smallest signed angular delta a-b, robust across the 0/2pi seam."""
    return (a - b + math.pi) % TAU - math.pi


def angle_in_gap(ball_angle: float, gap_angle: float, gap_size: float,
                 angular_margin: float = 0.0) -> bool:
    half_gap = max(0.0, gap_size * 0.5 - angular_margin)
    return abs(angle_delta(ball_angle, gap_angle)) <= half_gap


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def alpha_color(color: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return color[0], color[1], color[2], max(0, min(255, alpha))


@dataclass(slots=True)
class MidiEvent:
    notes: list[int]
    velocities: list[int]
    duration: float
    channels: list[int] = field(default_factory=list)
    programs: list[int] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)


class MidiMelody:
    """Parses note timing, durations, programs, and near-simultaneous chords."""

    def __init__(self, path: Path, chord_threshold_ms: int = CHORD_THRESHOLD_MS):
        self.path = path
        self.chord_threshold = chord_threshold_ms / 1000.0
        self.events: list[MidiEvent] = []
        self.index = 0
        self.load()

    def load(self) -> None:
        midi = mido.MidiFile(self.path)
        tempo = 500_000
        absolute_seconds = 0.0
        programs = defaultdict(int)
        active: dict[tuple[int, int], list[tuple[float, int, int]]] = defaultdict(list)
        completed: list[tuple[float, int, int, int, float, int]] = []

        for message in mido.merge_tracks(midi.tracks):
            absolute_seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
            if message.type == "set_tempo":
                tempo = message.tempo
            elif message.type == "program_change":
                programs[message.channel] = message.program
            elif message.type == "note_on" and message.velocity > 0:
                active[(message.channel, message.note)].append(
                    (absolute_seconds, message.velocity, programs[message.channel])
                )
            elif message.type == "note_off" or (
                message.type == "note_on" and message.velocity == 0
            ):
                key = (message.channel, message.note)
                if active[key]:
                    start, velocity, program = active[key].pop(0)
                    duration = clamp(absolute_seconds - start, MIN_NOTE_DURATION, MAX_NOTE_DURATION)
                    completed.append((start, message.note, velocity, message.channel, duration, program))

        # Gracefully close notes that have no note_off in malformed/simple files.
        for (channel, note), starts in active.items():
            for start, velocity, program in starts:
                completed.append((start, note, velocity, channel, 0.25, program))
        completed.sort(key=lambda item: item[0])
        if not completed:
            raise ValueError(f"Le MIDI ne contient aucune note jouable : {self.path}")

        groups: list[list[tuple[float, int, int, int, float, int]]] = []
        for note_data in completed:
            if not groups or note_data[0] - groups[-1][0][0] > self.chord_threshold:
                groups.append([note_data])
            else:
                groups[-1].append(note_data)

        self.events = [
            MidiEvent(
                notes=[n[1] for n in group],
                velocities=[n[2] for n in group],
                duration=max(n[4] for n in group),
                channels=[n[3] for n in group],
                programs=[n[5] for n in group],
                durations=[n[4] for n in group],
            )
            for group in groups
        ]
        self.index = 0

    def next_event(self) -> Optional[MidiEvent]:
        if not self.events:
            return None
        if self.index >= len(self.events):
            if not MIDI_LOOP:
                return None
            self.index = 0
        event = self.events[self.index]
        self.index += 1
        return event

    def reset(self) -> None:
        self.index = 0


class MidiSynth:
    """Low-latency FluidSynth wrapper with scheduled note-offs."""

    def __init__(self, soundfont_path: Path, volume: float):
        if fluidsynth is None:
            raise RuntimeError(
                "FluidSynth/pyfluidsynth est indisponible. Installez requirements.txt "
                "et la bibliothèque native FluidSynth (voir README.md). Détail : "
                f"{FLUIDSYNTH_IMPORT_ERROR}"
            )
        self.synth = fluidsynth.Synth(gain=volume, samplerate=44100)
        self._start_driver()
        self.sfid = self.synth.sfload(str(soundfont_path), update_midi_preset=0)
        if self.sfid < 0:
            raise RuntimeError(f"Impossible de charger le SoundFont : {soundfont_path}")
        self.channel_program: dict[int, int] = {}
        self.pending_offs: list[tuple[float, int, int]] = []
        self.note_tokens = defaultdict(int)
        self.muted = False

    def _start_driver(self) -> None:
        errors = []
        # dsound is normally the lowest-friction Windows driver; default works elsewhere.
        drivers: Iterable[Optional[str]] = ("dsound", None) if sys.platform == "win32" else (None,)
        for driver in drivers:
            try:
                selected = driver or self.synth.get_setting("audio.driver")
                self.synth.setting("audio.driver", selected)
                # pyfluidsynth.Synth.start() also opens a physical MIDI-input
                # device. Gameplay sends noteon/noteoff directly, so creating
                # only the audio driver avoids misleading "no MIDI devices"
                # errors on machines without a MIDI keyboard.
                audio_driver = fluidsynth.new_fluid_audio_driver(
                    self.synth.settings, self.synth.synth
                )
                if not audio_driver:
                    raise RuntimeError(f"pilote audio '{selected}' indisponible")
                self.synth.audio_driver = audio_driver
                return
            except Exception as exc:  # Try the platform fallback before reporting.
                errors.append(str(exc))
        raise RuntimeError("FluidSynth ne peut pas ouvrir la sortie audio : " + " | ".join(errors))

    def play_chord(self, event: MidiEvent) -> None:
        if self.muted:
            return
        now = time.monotonic()
        for note, velocity, channel, program, duration in zip(
            event.notes, event.velocities, event.channels, event.programs,
            event.durations or [event.duration] * len(event.notes)
        ):
            channel = channel % 16
            if self.channel_program.get(channel) != program:
                bank = 128 if channel == 9 else 0
                self.synth.program_select(channel, self.sfid, bank, program)
                self.channel_program[channel] = program
            # A previous note-off must not cut a newer repetition short.
            self.synth.noteoff(channel, note)
            self.note_tokens[channel, note] += 1
            self.synth.noteon(channel, note, max(1, min(127, velocity)))
            heapq.heappush(self.pending_offs, (now + duration, channel, note,
                                             self.note_tokens[channel, note]))

    def update(self) -> None:
        now = time.monotonic()
        while self.pending_offs and self.pending_offs[0][0] <= now:
            _, channel, note, token = heapq.heappop(self.pending_offs)
            if self.note_tokens[channel, note] == token:
                self.synth.noteoff(channel, note)

    def all_notes_off(self) -> None:
        for channel in range(16):
            self.synth.cc(channel, 123, 0)
        self.pending_offs.clear()
        self.note_tokens.clear()

    def close(self) -> None:
        self.all_notes_off()
        self.synth.delete()


class AudioManager:
    def __init__(self, midi_path: Path, soundfont_path: Path, pass_path: Path):
        self.melody = MidiMelody(midi_path)
        self.synth = MidiSynth(soundfont_path, MIDI_VOLUME)
        self.pass_sound = pygame.mixer.Sound(str(pass_path))
        self.pass_sound.set_volume(PASS_SOUND_VOLUME)
        self.muted = False
        self.per_ball_indices: dict[str, int] = defaultdict(int)
        self.recording_events = None
        self.event_time = 0.0
        self.pass_pcm = (pygame.sndarray.array(self.pass_sound).astype(np.float64)
                         * PASS_SOUND_VOLUME).astype(np.int32)

    def play_next_midi_event(self, source: str = "BALL") -> None:
        if SHARED_MIDI or GAME_MODE == "classic":
            event = self.melody.next_event()
        else:
            index = self.per_ball_indices[source]
            if index >= len(self.melody.events):
                if not MIDI_LOOP:
                    return
                index = 0
            event = self.melody.events[index]
            self.per_ball_indices[source] = index + 1
        if event is not None and not self.muted:
            self.synth.play_chord(event)
            if self.recording_events is not None:
                self.recording_events.append((self.event_time, 'midi', event))

    def play_pass_sound(self) -> None:
        if not self.muted:
            self.pass_sound.play()
            if self.recording_events is not None:
                self.recording_events.append((self.event_time, 'pass', None))

    def reset_music(self) -> None:
        self.synth.all_notes_off()
        self.melody.reset()
        self.per_ball_indices.clear()
        if self.recording_events is not None:
            self.recording_events.append((self.event_time, 'reset', None))

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        self.synth.muted = self.muted
        if self.muted:
            self.synth.all_notes_off()
            if self.recording_events is not None:
                self.recording_events.append((self.event_time, 'reset', None))

    def update(self) -> None:
        self.synth.update()

    def close(self) -> None:
        self.synth.close()


class VideoRecorder:
    """Asynchronous 1080x1920 MP4 recorder backed by bundled FFmpeg."""

    def __init__(self, audio=None) -> None:
        self.audio = audio
        self.frame_count = 0
        self.events = []
        self.active = False
        self.output_path: Optional[Path] = None
        self.error: Optional[str] = None
        self._queue: Optional[Queue] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> Path:
        if self.active:
            return self.output_path  # type: ignore[return-value]
        output_dir = BASE_DIR / "recordings"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000:06d}"
        self.output_path = output_dir / f"neon_rings_{stamp}.mp4"
        self.error = None
        self.frame_count = 0
        self.events = []
        if self.audio is not None:
            self.audio.event_time = 0.0
            self.audio.recording_events = self.events
        self._queue = Queue(maxsize=8)
        self.active = True
        self._thread = threading.Thread(target=self._encode, daemon=True)
        self._thread.start()
        print(f"REC démarré : {self.output_path}")
        return self.output_path

    def _encode(self) -> None:
        writer = None
        video_path = self.output_path.with_suffix('.silent.mp4') if self.audio else self.output_path
        try:
            writer = imageio_ffmpeg.write_frames(
                str(video_path), (WIDTH, HEIGHT), fps=RECORDING_FPS,
                codec="libx264", pix_fmt_in="rgb24", pix_fmt_out="yuv420p",
                macro_block_size=1,
                output_params=[
                    "-preset", "veryfast", "-crf", str(RECORDING_CRF),
                    "-movflags", "+faststart",
                ],
            )
            writer.send(None)
            while True:
                frame = self._queue.get()  # type: ignore[union-attr]
                if frame is None:
                    break
                writer.send(frame)
        except Exception as exc:
            self.error = str(exc)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception as exc:
                    self.error = self.error or str(exc)
        if self.audio is not None and self.frame_count and not self.error:
            wav_path = self.output_path.with_suffix('.wav')
            try:
                soundtrack(wav_path, BASE_DIR / SOUNDFONT_FILE, self.audio.pass_pcm,
                           self.events, self.frame_count, RECORDING_FPS, fluidsynth, MIDI_VOLUME)
                mux(video_path, wav_path, self.output_path)
                # Only encoder-created intermediates are removed after success.
                video_path.unlink()
                wav_path.unlink()
            except Exception as exc:
                self.error = str(exc)

    def capture(self, surface: pygame.Surface) -> None:
        if self.active and self._thread is not None and not self._thread.is_alive():
            self.active = False
            print(f"ERREUR ENREGISTREMENT : {self.error or 'encodeur arrêté'}", file=sys.stderr)
            return
        if self.active and self._queue is not None:
            # Blocking briefly is intentional: it preserves every simulation
            # frame and therefore produces a true fixed-rate 60 FPS video.
            self._enqueue(pygame.image.tobytes(surface, "RGB"))
            self.frame_count += 1

    def _enqueue(self, frame):
        while self._thread is not None and self._thread.is_alive():
            try:
                self._queue.put(frame, timeout=.1)
                return
            except Full:
                if pygame.get_init():
                    pygame.event.pump()
        self.active = False
        if self.audio is not None:
            self.audio.recording_events = None
        raise RuntimeError(f"Encodeur arrêté : {self.error or 'échec FFmpeg'}")

    def stop(self) -> Optional[Path]:
        if not self.active:
            return self.output_path
        self.active = False
        if self._queue is not None:
            self._enqueue(None)
        if self._thread is not None:
            self._thread.join()
        if self.error:
            print(f"ERREUR ENREGISTREMENT : {self.error}", file=sys.stderr)
        elif self.output_path:
            print(f"REC sauvegardé : {self.output_path}")
        return self.output_path

    def toggle(self) -> None:
        if self.active:
            self.stop()
        else:
            self.start()


class Ball:
    def __init__(self, name: str, color: tuple[int, int, int], direction: float):
        offset = -18 if name == "BLUE" else 18 if name == "RED" else 0
        self.name = name
        self.color = color
        self.x = CENTER_X + offset
        self.y = CENTER_Y
        self.vx = math.cos(direction) * BALL_SPEED
        self.vy = math.sin(direction) * BALL_SPEED
        self.radius = BALL_RADIUS
        self.trail: Deque[tuple[float, float]] = deque(maxlen=TRAIL_LENGTH * 4)
        self.last_ring = None
        self.last_collision_time = -10_000
        self.contacts: set[int] = set()
        self.passed_rings: set[int] = set()
        self.score = 0
        self.last_normal: Optional[tuple[float, float, float, float]] = None
        self.glow = pygame.Surface((self.radius * 8, self.radius * 8), pygame.SRCALPHA)
        glow_center = self.glow.get_width() // 2
        for factor, alpha in ((3.2, 20), (2.3, 35), (1.55, 65)):
            pygame.draw.circle(
                self.glow, alpha_color(self.color, alpha),
                (glow_center, glow_center), int(self.radius * factor)
            )

    def integrate(self, dt: float) -> tuple[float, float, float]:
        old_x, old_y = self.x, self.y
        old_distance = math.hypot(old_x - CENTER_X, old_y - CENTER_Y)
        self.vy += GRAVITY * dt
        speed = math.hypot(self.vx, self.vy)
        if speed > MAX_BALL_SPEED:
            scale = MAX_BALL_SPEED / speed
            self.vx *= scale
            self.vy *= scale
        self.x += self.vx * dt
        self.y += self.vy * dt
        return old_x, old_y, old_distance

    def draw(self, surface: pygame.Surface, effects: pygame.Surface) -> None:
        length = int(TRAIL_LENGTH * (1.5 + 2 * math.hypot(self.vx, self.vy) / MAX_BALL_SPEED))
        points = list(self.trail)[-length:]
        if points:
            count = len(points)
            for i, (x, y) in enumerate(points):
                ratio = (i + 1) / count
                radius = max(2, int(self.radius * ratio * 0.85))
                pygame.draw.circle(effects, alpha_color(self.color, int(90 * ratio)), (int(x), int(y)), radius)
        center = self.glow.get_width() // 2
        surface.blit(self.glow, (self.x - center, self.y - center))
        pygame.draw.circle(surface, self.color, (int(self.x), int(self.y)), self.radius)
        pygame.draw.circle(surface, WHITE, (int(self.x - self.radius * .25), int(self.y - self.radius * .25)), max(2, self.radius // 3))


class Ring:
    def __init__(self, ring_id: int, radius: float, color: tuple[int, int, int], rng=None):
        rng = rng or random
        self.id = ring_id
        self.radius = radius
        self.thickness = RING_THICKNESS
        self.color = color
        self.angle = rng.uniform(0, TAU)
        speed = rng.uniform(ROTATION_SPEED_MIN, ROTATION_SPEED_MAX)
        self.rotation_speed = speed if ring_id % 2 == 0 else -speed
        self.gap_size = math.radians(GAP_SIZE_DEGREES)
        self.active = True
        self.destroyed = False
        self.passed_by: set[str] = set()
        self.render_layer = None  # Lazy: seed search never allocates ring images.

    def update(self, dt: float) -> None:
        self.angle = (self.angle + self.rotation_speed * dt) % TAU

    def gap_contains(self, angle: float, ball_radius: float) -> bool:
        margin = math.asin(min(0.95, (ball_radius + self.thickness * 0.5) / self.radius))
        return angle_in_gap(angle, self.angle, self.gap_size, margin)

    def draw(self, surface: pygame.Surface, effects: pygame.Surface,
             intensity: float = 1.0) -> None:
        if self.destroyed:
            return
        center = int(self.radius + self.thickness + 10)
        if self.render_layer is None:
            self.render_layer = pygame.Surface((center * 2, center * 2), pygame.SRCALPHA)
        layer = self.render_layer
        layer.fill((0, 0, 0, 0))
        dim = tuple(min(255, int(c * intensity)) for c in self.color)
        for width, alpha in ((self.thickness + 12, 22), (self.thickness + 6, 42), (self.thickness, 255)):
            pygame.draw.circle(layer, alpha_color(dim, alpha), (center, center),
                               int(self.radius + width / 2), width)
        pygame.gfxdraw.aacircle(layer, center, center, int(self.radius + self.thickness / 2), dim)
        pygame.gfxdraw.aacircle(layer, center, center, int(self.radius - self.thickness / 2), dim)
        # Erase the gap on this ring's private layer, using screen-space angles.
        # A small sector fan also supports large configurable openings.
        angles = [self.angle - self.gap_size / 2 + self.gap_size * i / 12 for i in range(13)]
        wedge = [(center, center)] + [(center + math.cos(a) * center * 3,
                                      center + math.sin(a) * center * 3) for a in angles]
        pygame.draw.polygon(layer, (0, 0, 0, 0), wedge)
        for a in (angles[0], angles[-1]):
            px = int(center + math.cos(a) * self.radius)
            py = int(center + math.sin(a) * self.radius)
            pygame.draw.circle(layer, dim, (px, py), self.thickness // 2)
        surface.blit(layer, (CENTER_X - center, CENTER_Y - center))


@dataclass(slots=True)
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    size: float
    color: tuple[int, int, int]
    lifetime: float
    age: float = 0.0

    def update(self, dt: float) -> bool:
        self.age += dt
        drag = pow(0.08, dt)
        self.vx *= drag
        self.vy = self.vy * drag + 90 * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        return self.age < self.lifetime

    def draw(self, surface: pygame.Surface) -> None:
        ratio = max(0.0, 1.0 - self.age / self.lifetime)
        alpha = int(255 * ratio)
        radius = max(1, int(self.size * ratio))
        pygame.draw.circle(surface, alpha_color(self.color, alpha), (int(self.x), int(self.y)), radius)


class ParticleSystem:
    def __init__(self, rng):
        self.rng = rng
        self.items = []

    def burst(self, x, y, color, count):
        for _ in range(min(count, 450 - len(self.items))):
            angle = self.rng.uniform(0, TAU)
            speed = self.rng.uniform(100, 550)
            self.items.append(Particle(x, y, math.cos(angle) * speed, math.sin(angle) * speed,
                                       self.rng.uniform(2, 7), color, self.rng.uniform(.3, .85)))

    def update(self, dt):
        self.items[:] = [p for p in self.items if p.update(dt)]


class SilentAudio:
    """Seed search executes the same event callbacks without loading media."""
    def play_next_midi_event(self, source='BALL'): pass
    def play_pass_sound(self): pass
    def reset_music(self): pass
    def update(self): pass
    def close(self): pass


class Game:
    def __init__(self, seed=None, headless=False):
        self.headless = headless
        self.seed = SIMULATION_SEED if seed is None else seed
        self._validate_config()
        self.running = True
        self.paused = False
        self.debug = DEBUG
        self.last_substeps = 0
        if headless:
            self.audio = SilentAudio()
            self.restart(initial=True)
            return
        pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=256)
        pygame.init()
        self._validate_assets()
        self.window = pygame.display.set_mode((int(WIDTH * SCALE), int(HEIGHT * SCALE)))
        pygame.display.set_caption("Neon MIDI Rings")
        self.canvas = pygame.Surface((WIDTH, HEIGHT))
        self.fx_layer = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        self.presentation = pygame.Surface((WIDTH, HEIGHT))
        self.flash_surface = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        self.preview = pygame.Surface(self.window.get_size())
        self.clock = pygame.time.Clock()
        self.font_small = pygame.font.Font(None, 34)
        self.font_medium = pygame.font.Font(None, 62)
        self.font_large = pygame.font.Font(None, 118)
        self.audio = AudioManager(
            BASE_DIR / MIDI_FILE, BASE_DIR / SOUNDFONT_FILE, BASE_DIR / PASS_SOUND
        )
        self.recorder = VideoRecorder(self.audio)
        self.running = True
        self.paused = False
        self.debug = DEBUG
        self.last_substeps = PHYSICS_SUBSTEPS
        self.restart(initial=True)
        if RECORD_ON_START:
            self.recorder.start()

    @staticmethod
    def _validate_config() -> None:
        if not 12 <= NUM_RINGS <= 25:
            raise ValueError("NUM_RINGS doit être compris entre 12 et 25.")
        if GAME_MODE not in {"classic", "race", "prediction"}:
            raise ValueError("GAME_MODE doit être 'classic', 'race' ou 'prediction'.")
        outer = INNER_RING_RADIUS + (NUM_RINGS - 1) * RING_SPACING
        if outer + BALL_RADIUS + RING_THICKNESS > WIDTH * 0.5:
            raise ValueError("Les anneaux dépassent la largeur logique; réduisez leur nombre ou espacement.")
        clearance = math.degrees(math.asin(
            min(0.95, (BALL_RADIUS + RING_THICKNESS * 0.5) / INNER_RING_RADIUS)
        ))
        if GAP_SIZE_DEGREES - 2 * clearance < 8:
            raise ValueError(
                "L'ouverture est trop étroite pour la balle sur le premier anneau. "
                "Augmentez GAP_SIZE_DEGREES ou INNER_RING_RADIUS."
            )

    @staticmethod
    def _validate_assets() -> None:
        required = [BASE_DIR / MIDI_FILE, BASE_DIR / SOUNDFONT_FILE, BASE_DIR / PASS_SOUND]
        missing = [str(path.relative_to(BASE_DIR)) for path in required if not path.is_file()]
        if missing:
            details = "\n".join(f"  - {item}" for item in missing)
            raise FileNotFoundError(f"Fichier(s) requis manquant(s) :\n{details}\nVoir README.md.")

    def restart(self, initial=False) -> None:
        if not initial and RANDOMIZE_SEED_ON_RESTART:
            self.seed += 1
        rng = random.Random(self.seed)
        self.fx_rng = random.Random(self.seed ^ 0xC0FFEE)
        self.stats = SimulationStats(self.seed)
        self.physics = PhysicsEngine(sys.modules[__name__], self.bounced, self.ring_passed)
        self.rings = [
            Ring(i, INNER_RING_RADIUS + i * RING_SPACING, NEON_PALETTE[i % len(NEON_PALETTE)], rng)
            for i in range(NUM_RINGS)
        ]
        if GAME_MODE == "classic":
            self.balls = [Ball("BALL", WHITE, rng.uniform(-1.2, -0.25))]
        else:
            self.balls = [
                Ball("BLUE", BLUE, rng.uniform(-1.20, -0.35)),
                Ball("RED", RED, rng.uniform(-2.80, -1.95)),
            ]
        self.particle_system = ParticleSystem(self.fx_rng)
        self.particles = self.particle_system.items
        self.flash_left = 0.0
        self.flash_strength = 0
        self.shake_left = 0.0
        self.end_time: Optional[float] = None
        self.winner: Optional[str] = None
        self.presentation_time = 0.0
        self.frame_physics_start = 0.0
        self.frame_time_scale = 1.0
        self.finish_elapsed = 0.0
        self.ramp_left = 0.0
        self.final_anticipation_used = False
        self.notice = ''
        self.notice_left = 0.0
        self.shockwave = None
        self.prediction_left = PREDICTION_DELAY if GAME_MODE == "prediction" else 0.0
        self.audio.reset_music()
        if not self.headless:
            print(f'SIMULATION SEED: {self.seed}')

    def handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key == pygame.K_SPACE:
                    self.paused = not self.paused
                elif event.key == pygame.K_r:
                    self.restart()
                elif event.key == pygame.K_d:
                    self.debug = not self.debug
                elif event.key == pygame.K_m:
                    self.audio.toggle_mute()
                elif event.key == pygame.K_F9:
                    self.recorder.toggle()

    def update_physics(self, dt: float) -> None:
        self.physics.advance(dt, self.balls, self.rings)
        self.last_substeps = self.physics.last_substeps
        self.stats.elapsed = self.physics.time
        self.stats.solver_exhaustions = self.physics.exhaustions

    def bounced(self, ball, ring, timestamp):
        # CCD emits approaching contacts only. No global cooldown may discard
        # a distinct legitimate impact, including impacts less than 60 ms apart.
        # Reject only an identical event delivered twice, never a later impact.
        event_key = (ring.id, timestamp)
        if getattr(ball, '_audio_impact_key', None) == event_key:
            return
        ball._audio_impact_key = event_key
        self.set_audio_event_time(timestamp)
        self.audio.play_next_midi_event(ball.name)
        ball.last_collision_time = timestamp * 1000
        self.stats.collisions += 1
        self.stats.events.append((timestamp, "bounce", ball.name, ring.id))
        angle = math.atan2(ball.y - CENTER_Y, ball.x - CENTER_X)
        gap = ring.angle + ring.rotation_speed * (timestamp - self.physics.time)
        edge_distance = abs(abs(delta(angle, gap)) - ring.gap_size / 2)
        if edge_distance < NEAR_MISS_ANGLE and timestamp - self.stats.last_near >= NEAR_MISS_COOLDOWN:
            self.stats.last_near = timestamp
            self.stats.near_misses += 1
            self.stats.events.append((timestamp, "near", ball.name, ring.id))
            if not self.headless:
                self.flash_left = .035
                self.flash_strength = 16
                self.shake_left = .035
                self.spawn_particles(ball.x, ball.y, ring.color, 6)

    def set_audio_event_time(self, timestamp):
        if not self.headless and hasattr(self, 'recorder'):
            self.audio.event_time = (self.recorder.frame_count / RECORDING_FPS
                                     + max(0, timestamp - self.frame_physics_start) / self.frame_time_scale)

    def ring_passed(self, ball: Ball, ring: Ring, x: float, y: float, timestamp=None) -> None:
        if ball.name in ring.passed_by:
            return
        ring.passed_by.add(ball.name)
        ball.passed_rings.add(ring.id)
        ball.score += 1
        timestamp = self.physics.time if timestamp is None else timestamp
        self.set_audio_event_time(timestamp)
        self.stats.passes.append((timestamp, ball.name, ring.id))
        self.stats.events.append((timestamp, 'pass', ball.name, ring.id))
        # Reward every successful escape with +5% speed, without exceeding the
        # safety cap used by the adaptive collision substeps.
        speed = math.hypot(ball.vx, ball.vy)
        if speed > 1e-7:
            boosted_speed = min(MAX_BALL_SPEED, speed * SPEED_BOOST_PER_RING)
            boost = boosted_speed / speed
            ball.vx *= boost
            ball.vy *= boost
        if GAME_MODE == "classic" or len(ring.passed_by) == len(self.balls):
            ring.destroyed = True
            ring.active = False
        self.audio.play_pass_sound()  # Deliberately does not touch the MIDI index.
        final = ball.score == NUM_RINGS
        self.spawn_particles(x, y, ring.color, int(PARTICLE_COUNT * (.65 + ball.score / NUM_RINGS)))
        self.flash_left = FLASH_DURATION * (1.8 if final else 1.0)
        self.flash_strength = 130 if final else 70
        self.shake_left = SHAKE_DURATION * (1.5 if final else 1.0)
        if final and self.end_time is None:
            self.winner = ball.name
            self.end_time = timestamp
            self.stats.completed_at = timestamp
            self.finish_elapsed = 0.0
            self.shockwave = (x, y)
            self.spawn_particles(x, y, ball.color, 100)
        remaining = NUM_RINGS - ball.score
        if remaining in (1, 2, 3):
            self.notice = 'FINAL RING' if remaining == 1 else f'{remaining} LEFT'
            self.notice_left = 1.0
        angle = math.atan2(y - CENTER_Y, x - CENTER_X)
        gap = ring.angle + ring.rotation_speed * (timestamp - self.physics.time)
        if SPEED_RAMP and not self.headless and abs(delta(angle, gap)) > ring.gap_size * .25:
            self.ramp_left = .10

    def spawn_particles(self, x, y, color, count) -> None:
        if not self.headless:
            self.particle_system.burst(x, y, color, count)

    def update(self, dt: float) -> None:
        self.audio.update()
        if self.paused:
            return
        self.presentation_time += dt
        if self.prediction_left > 0:
            self.prediction_left = max(0.0, self.prediction_left - dt)
            return
        if SPEED_RAMP and not self.headless and not self.final_anticipation_used:
            for ball in self.balls:
                if ball.score == NUM_RINGS - 1:
                    ring = next((r for r in self.rings if ball.name not in r.passed_by), None)
                    if ring is not None:
                        dx, dy = ball.x - CENTER_X, ball.y - CENTER_Y
                        distance = math.hypot(dx, dy)
                        if (ring.radius - 55 < distance < ring.radius
                                and dx * ball.vx + dy * ball.vy > 0
                                and ring.gap_contains(math.atan2(dy, dx), ball.radius)):
                            self.ramp_left = .12
                            self.final_anticipation_used = True
        time_scale = .65 if self.ramp_left > 0 else 1.0
        if self.end_time is not None:
            time_scale = .55
            self.finish_elapsed += dt
        self.frame_physics_start = self.physics.time
        self.frame_time_scale = time_scale
        if not self.headless:
            self.audio.event_time = self.recorder.frame_count / RECORDING_FPS
        self.update_physics(dt * time_scale)
        self.particle_system.update(dt)
        self.flash_left = max(0.0, self.flash_left - dt)
        self.shake_left = max(0.0, self.shake_left - dt)
        self.ramp_left = max(0.0, self.ramp_left - dt)
        self.notice_left = max(0.0, self.notice_left - dt)
        if self.end_time is not None and AUTO_RESTART and not self.headless:
            if self.finish_elapsed >= RESTART_DELAY:
                self.restart()
                if SEAMLESS_LOOP:
                    self.flash_left = .08
                    self.flash_strength = 24

    def _draw_text(self, text: str, font: pygame.font.Font, color: tuple[int, int, int],
                   center: tuple[float, float]) -> None:
        shadow = font.render(text, True, (15, 15, 25))
        image = font.render(text, True, color)
        rect = image.get_rect(center=(int(center[0]), int(center[1])))
        self.canvas.blit(shadow, rect.move(4, 5))
        self.canvas.blit(image, rect)

    def draw(self) -> None:
        self.canvas.fill(BG)
        self.fx_layer.fill((0, 0, 0, 0))
        remaining = min(NUM_RINGS - ball.score for ball in self.balls)
        intensity = 1.0 if remaining > 3 else 1.12
        targets = {next((r.id for r in self.rings if b.name not in r.passed_by), -1)
                   for b in self.balls}
        for ring in reversed(self.rings):
            ring.draw(self.canvas, self.fx_layer, intensity if ring.id in targets else .72)

        for particle in self.particles:
            particle.draw(self.fx_layer)
        for ball in self.balls:
            # Trails belong to the shared alpha layer; ball bodies stay crisp.
            ball.draw(self.canvas, self.fx_layer)
        self.canvas.blit(self.fx_layer, (0, 0))
        for ball in self.balls:
            # draw() already emitted the trail and body; redraw the body above FX.
            pygame.draw.circle(self.canvas, ball.color, (int(ball.x), int(ball.y)), ball.radius)
            pygame.draw.circle(self.canvas, WHITE,
                               (int(ball.x - ball.radius * .25), int(ball.y - ball.radius * .25)),
                               max(2, ball.radius // 3))

        if GAME_MODE == "classic":
            self._draw_text(f"RINGS LEFT: {NUM_RINGS - self.balls[0].score}", self.font_small, WHITE, (CENTER_X, 90))
        else:
            score = f"BLUE {self.balls[0].score}  -  {self.balls[1].score} RED"
            self._draw_text(score, self.font_small, WHITE, (CENTER_X, 90))

        if self.notice_left > 0 and self.end_time is None:
            self._draw_text(self.notice, self.font_medium, (255, 170, 140), (CENTER_X, 205))
        elif self.presentation_time < 1.0 and GAME_MODE == 'classic':
            self._draw_text('CAN IT ESCAPE?', self.font_medium, WHITE, (CENTER_X, 205))

        if self.prediction_left > 0:
            self._draw_text("WHO ESCAPES FIRST?", self.font_medium, WHITE, (CENTER_X, 300))
            self._draw_text("BLUE   VS   RED", self.font_medium, (120, 210, 255), (CENTER_X, 390))
        if self.end_time is not None:
            message = "ESCAPED!" if GAME_MODE == "classic" else f"{self.winner} WINS!"
            color = WHITE if GAME_MODE == "classic" else (BLUE if self.winner == "BLUE" else RED)
            self._draw_text(message, self.font_large, color, (CENTER_X, 290))
            if self.shockwave and self.finish_elapsed < .6:
                x, y = self.shockwave
                self.fx_layer.fill((0, 0, 0, 0))
                pygame.draw.circle(self.fx_layer, (180, 245, 255, int(150 * (1 - self.finish_elapsed / .6))),
                                   (int(x), int(y)), int(20 + self.finish_elapsed * 450), 3)
                self.canvas.blit(self.fx_layer, (0, 0))
        if self.paused:
            self._draw_text("PAUSED", self.font_large, WHITE, (CENTER_X, CENTER_Y - 180))
        if self.flash_left > 0:
            ratio = self.flash_left / max(FLASH_DURATION, 1e-6)
            self.flash_surface.fill((255, 255, 255, min(85, int(self.flash_strength * ratio))))
            self.canvas.blit(self.flash_surface, (0, 0))

        shake_x = shake_y = 0
        if self.shake_left > 0:
            ratio = self.shake_left / SHAKE_DURATION
            shake_x = math.sin(self.presentation_time * 173) * SHAKE_STRENGTH * ratio
            shake_y = math.sin(self.presentation_time * 139) * SHAKE_STRENGTH * ratio
        self.presentation.fill(BG)
        self.presentation.blit(self.canvas, (int(shake_x), int(shake_y)))
        self.recorder.capture(self.presentation)
        # Monitoring overlays never appear in the exported movie.
        self.canvas.blit(self.presentation, (0, 0))
        if self.debug:
            self.draw_debug()
        if self.recorder.active:
            pygame.draw.circle(self.canvas, RED, (55, 55), 13)
            self.canvas.blit(self.font_small.render('REC', True, WHITE), (80, 40))
        pygame.transform.scale(self.canvas, self.window.get_size(), self.preview)
        self.window.blit(self.preview, (0, 0))
        pygame.display.flip()

    def draw_debug(self) -> None:
        ball = self.balls[0]
        speed = math.hypot(ball.vx, ball.vy)
        lines = [
            f"FPS {self.clock.get_fps():5.1f}",
            f"BALL x={ball.x:.1f} y={ball.y:.1f}",
            f"v=({ball.vx:.1f}, {ball.vy:.1f}) speed={speed:.1f}",
            f"RINGS remaining={NUM_RINGS - ball.score}",
            f"AUDIO MIDI {self.audio.melody.index} / {len(self.audio.melody.events)}",
            f"PHYSICS substeps={self.last_substeps}",
            f"MUTED {self.audio.muted}",
            f"SEED {self.seed} / collisions {self.stats.collisions}",
            f"NEAR {self.stats.near_misses} / ring {ball.last_ring}",
        ]
        panel = pygame.Surface((550, 32 * len(lines) + 20), pygame.SRCALPHA)
        panel.fill((0, 0, 0, 180))
        for i, line in enumerate(lines):
            panel.blit(self.font_small.render(line, True, (160, 255, 190)), (10, 8 + i * 32))
        self.canvas.blit(panel, (18, HEIGHT - panel.get_height() - 18))
        pygame.draw.line(self.canvas, (100, 100, 130), (CENTER_X, CENTER_Y), (ball.x, ball.y), 2)
        if ball.last_normal:
            x, y, nx, ny = ball.last_normal
            pygame.draw.circle(self.canvas, (255, 255, 0), (int(x), int(y)), 7)
            pygame.draw.line(self.canvas, (255, 255, 0), (x, y), (x + nx * 80, y + ny * 80), 4)
        for ring in self.rings:
            if ring.destroyed:
                continue
            for radius in (ring.radius - ball.radius - ring.thickness / 2,
                           ring.radius + ball.radius + ring.thickness / 2):
                pygame.draw.circle(self.canvas, (35, 45, 60), (int(CENTER_X), int(CENTER_Y)), int(radius), 1)
            for a in (ring.angle - ring.gap_size / 2, ring.angle + ring.gap_size / 2):
                end = (CENTER_X + math.cos(a) * ring.radius, CENTER_Y + math.sin(a) * ring.radius)
                pygame.draw.line(self.canvas, (255, 220, 0), (CENTER_X, CENTER_Y), end, 1)
            margin = math.asin((ball.radius + ring.thickness / 2) / ring.radius)
            for a in (ring.angle - ring.gap_size / 2 + margin,
                      ring.angle + ring.gap_size / 2 - margin):
                end = (CENTER_X + math.cos(a) * ring.radius, CENTER_Y + math.sin(a) * ring.radius)
                pygame.draw.line(self.canvas, (40, 220, 130), (CENTER_X, CENTER_Y), end, 1)

    def run(self) -> None:
        try:
            while self.running:
                dt = self.clock.tick(RECORDING_FPS if self.recorder.active else FPS) / 1000.0
                self.handle_events()
                if self.recorder.active:
                    dt = 1.0 / RECORDING_FPS
                self.update(dt)
                self.draw()
        finally:
            self.recorder.stop()
            self.audio.close()
            pygame.quit()


def search_seeds(count, start_seed):
    """Rank unmodified Classic runs using measurable event statistics."""
    results = []
    for seed in range(start_seed, start_seed + count):
        game = Game(seed=seed, headless=True)
        while game.stats.elapsed < SEARCH_TIMEOUT and game.stats.completed_at is None:
            game.update_physics(1 / 30)
            last_event = game.stats.events[-1][0] if game.stats.events else 0
            if game.stats.elapsed - last_event > SEARCH_STALL_TIMEOUT:
                break
        score, idle = game.stats.quality(TARGET_DURATION_MIN, TARGET_DURATION_MAX)
        results.append(dict(seed=seed, score=score, duration=round(game.stats.completed_at or game.stats.elapsed, 3),
                            completed=game.stats.completed_at is not None, bounces=game.stats.collisions,
                            near_misses=game.stats.near_misses, longest_idle=idle,
                            passages=game.stats.passes, events=game.stats.events))
        if (seed - start_seed + 1) % 10 == 0:
            print(f'Search {seed - start_seed + 1}/{count}', flush=True)
    results.sort(key=lambda r: r['score'], reverse=True)
    output = BASE_DIR / 'recordings'
    output.mkdir(exist_ok=True)
    report = output / f'seed_search_{start_seed}_{count}_{time.time_ns()}.json'
    report.write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('SEED   SCORE   DURATION   COMPLETE   BOUNCES   NEAR   MAX IDLE')
    for r in results[:10]:
        print(f"{r['seed']:6} {r['score']:7.2f} {r['duration']:8.2f}s {str(r['completed']):8} "
              f"{r['bounces']:7} {r['near_misses']:6} {r['longest_idle']:8.2f}s")
    print(f'Report: {report}')
    return results


def main() -> int:
    global GAME_MODE, RECORD_ON_START, AUTO_RESTART
    parser = argparse.ArgumentParser(description='Neon MIDI Rings')
    parser.add_argument('--seed', type=int, default=SIMULATION_SEED)
    parser.add_argument('--search', type=int, default=AUTO_SEARCH_RUNS)
    parser.add_argument('--mode', choices=('classic', 'race', 'prediction'), default=GAME_MODE)
    parser.add_argument('--record', action='store_true')
    parser.add_argument('--no-restart', action='store_true')
    args = parser.parse_args()
    GAME_MODE = args.mode
    RECORD_ON_START = RECORD_ON_START or args.record
    AUTO_RESTART = AUTO_RESTART and not args.no_restart
    try:
        if args.search:
            if args.search < 0:
                raise ValueError('--search doit être positif')
            search_seeds(args.search, args.seed)
        else:
            Game(seed=args.seed).run()
        return 0
    except (FileNotFoundError, OSError, EOFError, ValueError, RuntimeError, pygame.error) as exc:
        print(f"\nERREUR: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
