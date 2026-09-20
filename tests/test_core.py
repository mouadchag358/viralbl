"""Regression tests: actual CCD trajectories, pixels, MIDI, and determinism."""
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import mido
import pygame
import main
from physics import circle_times


class FakeAudio(main.SilentAudio):
    def __init__(self):
        self.notes = self.passes = 0
    def play_next_midi_event(self, source="BALL"):
        self.notes += 1
    def play_pass_sound(self):
        self.passes += 1


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def scenario(self, angle=math.pi, radius=100):
        g = main.Game(seed=42, headless=True)
        g.audio = FakeAudio()
        ring = main.Ring(0, radius, main.BLUE)
        ring.angle, ring.rotation_speed = angle, 0
        g.rings = [ring]
        ball = g.balls[0]
        ball.x, ball.y = main.CENTER_X, main.CENTER_Y
        ball.vx, ball.vy = 500, 0
        return g, ring, ball

    def test_analytic_intersection_oblique_and_tangent(self):
        roots = circle_times(-200, 60, 1000, 0, 100, 1)
        self.assertAlmostEqual(roots[0], .12)
        self.assertAlmostEqual(roots[1], .28)
        self.assertAlmostEqual(circle_times(-200, 100, 1000, 0, 100, 1)[0], .2)

    def test_gap_wraps_across_zero(self):
        self.assertTrue(main.angle_in_gap(math.radians(1), math.radians(359), math.radians(20)))
        self.assertFalse(main.angle_in_gap(math.radians(40), math.radians(359), math.radians(20)))

    def test_visible_gap_matches_full_clearance_pass(self):
        surface = pygame.Surface((main.WIDTH, main.HEIGHT))
        effects = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        for degrees in (1, 45, 90, 135, 225, 270, 315, 359):
            with self.subTest(angle=degrees):
                g, ring, ball = self.scenario(math.radians(degrees))
                surface.fill((0, 0, 0))
                effects.fill((0, 0, 0, 0))
                ring.draw(surface, effects)
                nx, ny = math.cos(ring.angle), math.sin(ring.angle)
                gap = (round(main.CENTER_X + nx * 100), round(main.CENTER_Y + ny * 100))
                solid = (round(main.CENTER_X - nx * 100), round(main.CENTER_Y - ny * 100))
                self.assertEqual(tuple(surface.get_at(gap))[:3], (0, 0, 0))
                self.assertNotEqual(tuple(surface.get_at(solid))[:3], (0, 0, 0))
                ball.vx, ball.vy = nx * 500, ny * 500
                g.physics.move(ball, g.rings, .22)
                self.assertEqual(g.audio.passes, 0)  # Center outside, back still inside.
                g.physics.move(ball, g.rings, .03)
                self.assertEqual(g.audio.passes, 1)
                self.assertEqual(g.audio.notes, 0)
                self.assertTrue(ring.destroyed)
                self.assertAlmostEqual(math.hypot(ball.vx, ball.vy), 525)

    def test_high_speed_solid_cannot_tunnel(self):
        g, ring, ball = self.scenario()
        ball.vx = 20000
        with patch.object(main, "MAX_BALL_SPEED", 20000):
            g.physics.move(ball, g.rings, .005)
        self.assertEqual(g.audio.notes, 1)
        self.assertLess(ball.vx, 0)
        self.assertLess(math.hypot(ball.x-main.CENTER_X, ball.y-main.CENTER_Y), 83)
        self.assertEqual(g.audio.passes, 0)

    def test_multiple_impacts_consume_remaining_time(self):
        g, ring, ball = self.scenario()
        ring.gap_size = .01
        ring.angle = math.pi / 2
        ball.vx = 1000
        g.physics.move(ball, g.rings, .5)
        self.assertEqual(g.audio.notes, 3)
        self.assertEqual(g.stats.collisions, 3)
        self.assertLess(math.hypot(ball.x-main.CENTER_X, ball.y-main.CENTER_Y), 83.001)

    def test_fast_legitimate_impacts_are_not_cooled_down(self):
        g, ring, ball = self.scenario()
        ring.gap_size = .01
        ring.angle = math.pi / 2
        ball.vx = 20000
        with patch.object(main, "MAX_BALL_SPEED", 20000):
            g.physics.move(ball, g.rings, .02)
        self.assertGreaterEqual(g.audio.notes, 2)
        times = [event[0] for event in g.stats.events if event[1] == "bounce"]
        self.assertLess(times[1] - times[0], .06)

    def test_rounded_gap_edge_blocks_partial_ball(self):
        g, ring, ball = self.scenario(0)
        angle = ring.gap_size / 2 - math.radians(3)
        ball.vx, ball.vy = 500 * math.cos(angle), 500 * math.sin(angle)
        g.physics.move(ball, g.rings, .22)
        self.assertGreater(g.audio.notes, 0)
        self.assertEqual(g.audio.passes, 0)

    def test_rotating_cap_sweeps_stationary_ball(self):
        g, ring, ball = self.scenario(0)
        ring.rotation_speed = 2
        angle = ring.gap_size / 2 + .22
        ball.x = main.CENTER_X + 100 * math.cos(angle)
        ball.y = main.CENTER_Y + 100 * math.sin(angle)
        # Test the endpoint solver directly, with no radial body ambiguity.
        ball.vx = ball.vy = 0
        hit = g.physics.cap_hit(ball, ring, 1, 0, .1)
        self.assertIsNotNone(hit)
        self.assertGreater(hit[0], 0)
        self.assertLess(hit[0], .1)

    def test_frame_rate_and_rendering_do_not_change_seed(self):
        snapshots = []
        for fps in (60, 120):
            g = main.Game(seed=7, headless=True)
            for _ in range(fps * 8):
                g.update_physics(1 / fps)
            snapshots.append((g.balls[0].x, g.balls[0].y, g.balls[0].vx,
                              g.balls[0].vy, g.stats.events, [r.angle for r in g.rings]))
        self.assertEqual(snapshots[0], snapshots[1])

    def test_midi_chords_keep_individual_durations_and_programs(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        midi.tracks.append(track)
        track.append(mido.Message("program_change", channel=2, program=11))
        track.append(mido.Message("note_on", channel=2, note=60, velocity=90))
        track.append(mido.Message("note_on", channel=2, note=64, velocity=80, time=10))
        track.append(mido.Message("note_off", channel=2, note=60, time=100))
        track.append(mido.MetaMessage("set_tempo", tempo=1000000, time=0))
        track.append(mido.Message("note_on", channel=2, note=64, velocity=0, time=240))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.mid"
            midi.save(path)
            melody = main.MidiMelody(path)
        event = melody.events[0]
        self.assertEqual(event.notes, [60, 64])
        self.assertEqual(event.programs, [11, 11])
        self.assertEqual(event.channels, [2, 2])
        self.assertLess(event.durations[0], event.durations[1])
        self.assertEqual(melody.index, 0)


if __name__ == "__main__":
    unittest.main()
