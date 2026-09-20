"""Deterministic fixed-tick CCD. No graphics, sound, or wall-clock dependency."""
from dataclasses import dataclass, field
import math

TAU = math.tau
EPS = 1e-4


def delta(a, b):
    return (a - b + math.pi) % TAU - math.pi


def circle_times(x, y, vx, vy, radius, duration):
    """Exact times when a linear trajectory intersects a circle at the origin."""
    a = vx * vx + vy * vy
    if a < 1e-20:
        return ()
    b = x * vx + y * vy
    c = x * x + y * y - radius * radius
    discriminant = b * b - a * c
    if discriminant < 0:
        return ()
    root = math.sqrt(max(0, discriminant))
    return tuple(t for t in ((-b - root) / a, (-b + root) / a)
                 if 1e-10 < t <= duration + 1e-10)


@dataclass
class SimulationStats:
    seed: int
    elapsed: float = 0.0
    collisions: int = 0
    near_misses: int = 0
    passes: list = field(default_factory=list)
    events: list = field(default_factory=list)
    completed_at: float | None = None
    last_near: float = -100.0
    solver_exhaustions: int = 0

    def quality(self, minimum, maximum):
        end = self.completed_at or self.elapsed
        times = [0.0] + [e[0] for e in self.events] + [end]
        idle = max((b - a for a, b in zip(times, times[1:])), default=end)
        passage_times = [0.0] + [e[0] for e in self.passes]
        intervals = [b - a for a, b in zip(passage_times, passage_times[1:])]
        spread = (max(intervals) - min(intervals)) if intervals else end
        climax = sum(e[1] == 'bounce' and e[0] >= end - 4 for e in self.events)
        duration_penalty = max(minimum - end, 0, end - maximum)
        score = (100 - duration_penalty * 3 - idle * 2 - spread * .6
                 + min(self.near_misses, 8) * 2 + min(climax, 12))
        if self.completed_at is None or self.solver_exhaustions:
            score -= 200
        return round(score, 2), round(idle, 3)


class PhysicsEngine:
    def __init__(self, config, on_bounce, on_pass):
        self.c = config
        self.on_bounce = on_bounce
        self.on_pass = on_pass
        self.accumulator = 0.0
        self.ticks = 0
        self.last_substeps = 0
        self.exhaustions = 0

    @property
    def time(self):
        return self.ticks / self.c.PHYSICS_HZ

    def advance(self, dt, balls, rings):
        self.accumulator += max(0.0, dt)
        step = 1.0 / self.c.PHYSICS_HZ
        count = int((self.accumulator + 1e-12) / step)
        # No dropped time: rendering/encoding throughput never changes physics.
        for _ in range(count):
            for ball in balls:
                ball.vy += self.c.GRAVITY * step
                self.limit_speed(ball)
                self.move(ball, rings, step)
                ball.trail.append((ball.x, ball.y))
            for ring in rings:
                ring.update(step)
            self.ticks += 1
        self.accumulator = max(0.0, self.accumulator - count * step)
        self.last_substeps = count

    def limit_speed(self, ball):
        speed = math.hypot(ball.vx, ball.vy)
        if speed > self.c.MAX_BALL_SPEED:
            factor = self.c.MAX_BALL_SPEED / speed
            ball.vx *= factor
            ball.vy *= factor

    def cap_hit(self, ball, ring, sign, elapsed, duration):
        """Conservative advancement against a truly rotating round endpoint.

        Distance / (ball speed + endpoint speed) is a safe lower bound on
        time to contact. This avoids freezing rotating caps or stepping over
        thin contacts. Contact tolerance is 1e-6 logical pixels.
        """
        radius = ball.radius + ring.thickness / 2
        bound = math.hypot(ball.vx, ball.vy) + abs(ring.rotation_speed) * ring.radius
        if bound < 1e-12:
            return None
        t = 0.0
        for _ in range(160):
            angle = ring.angle + ring.rotation_speed * (elapsed + t) + sign * ring.gap_size / 2
            cx = self.c.CENTER_X + math.cos(angle) * ring.radius
            cy = self.c.CENTER_Y + math.sin(angle) * ring.radius
            dx, dy = ball.x + ball.vx * t - cx, ball.y + ball.vy * t - cy
            distance = math.hypot(dx, dy)
            separation = distance - radius
            wx = -math.sin(angle) * ring.radius * ring.rotation_speed
            wy = math.cos(angle) * ring.radius * ring.rotation_speed
            if separation <= 1e-6:
                nx, ny = dx / max(distance, 1e-12), dy / max(distance, 1e-12)
                if (ball.vx - wx) * nx + (ball.vy - wy) * ny < -1e-7:
                    return t, nx, ny, wx, wy
                return None
            t += separation / bound
            if t > duration:
                return None
        # At a grazing asymptote there is no approaching impact to resolve.
        return None

    def first_event(self, ball, rings, elapsed, duration):
        x, y = ball.x - self.c.CENTER_X, ball.y - self.c.CENTER_Y
        candidates = []
        travel = math.hypot(ball.vx, ball.vy) * duration
        distance = math.hypot(x, y)
        for ring in rings:
            if ring.destroyed or ball.name in ring.passed_by:
                continue
            combined = ball.radius + ring.thickness / 2
            if abs(distance - ring.radius) > combined + travel + EPS:
                continue
            for boundary, side in ((ring.radius - combined, -1),
                                   (ring.radius + combined, 1)):
                for t in circle_times(x, y, ball.vx, ball.vy, boundary, duration):
                    px, py = x + ball.vx * t, y + ball.vy * t
                    angle = math.atan2(py, px)
                    gap = ring.angle + ring.rotation_speed * (elapsed + t)
                    radial_dot = (ball.vx * px + ball.vy * py) / boundary
                    if radial_dot * side >= -1e-7:
                        continue
                    if abs(delta(angle, gap)) >= ring.gap_size / 2:
                        candidates.append((t, 0, ring, side * px / boundary,
                                           side * py / boundary, 0.0, 0.0))
            for sign in (-1, 1):
                hit = self.cap_hit(ball, ring, sign, elapsed, duration)
                if hit:
                    t, nx, ny, wx, wy = hit
                    candidates.append((t, 0, ring, nx, ny, wx, wy))
            # Destroy only after the entire ball clears the outer ring surface.
            outer = ring.radius + combined + EPS
            for t in circle_times(x, y, ball.vx, ball.vy, outer, duration):
                px, py = x + ball.vx * t, y + ball.vy * t
                if px * ball.vx + py * ball.vy <= 0:
                    continue
                angle = math.atan2(py, px)
                gap = ring.angle + ring.rotation_speed * (elapsed + t)
                if abs(delta(angle, gap)) < ring.gap_size / 2:
                    candidates.append((t, 1, ring, 0, 0, 0, 0))
        return min(candidates, key=lambda e: (e[0], e[1], e[2].id)) if candidates else None

    def move(self, ball, rings, duration):
        remaining, elapsed = duration, 0.0
        for _ in range(64):
            if remaining <= 1e-10:
                return
            event = self.first_event(ball, rings, elapsed, remaining)
            if event is None:
                ball.x += ball.vx * remaining
                ball.y += ball.vy * remaining
                return
            t, kind, ring, nx, ny, wx, wy = event
            ball.x += ball.vx * t
            ball.y += ball.vy * t
            elapsed += t
            remaining = max(0, remaining - t)
            ball.trail.append((ball.x, ball.y))
            timestamp = self.time + elapsed
            if kind == 1:
                self.on_pass(ball, ring, ball.x, ball.y, timestamp)
                continue
            dot = (ball.vx - wx) * nx + (ball.vy - wy) * ny
            # Restitution applies to the normal component, in the cap's frame.
            ball.vx -= (1 + self.c.BOUNCE_RESTITUTION) * dot * nx
            ball.vy -= (1 + self.c.BOUNCE_RESTITUTION) * dot * ny
            self.limit_speed(ball)
            ball.last_normal = (ball.x, ball.y, nx, ny)
            ball.last_ring = ring.id
            self.on_bounce(ball, ring, timestamp)
            ball.x += nx * EPS
            ball.y += ny * EPS
        self.exhaustions += 1
        raise RuntimeError('CCD: trop de contacts dans un seul pas; vérifier la géométrie.')
