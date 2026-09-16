#ifndef FLY_ESCAPE_WORLD_H
#define FLY_ESCAPE_WORLD_H

#include <math.h>
#include <stdint.h>
#include <string.h>

// The escape demo's world: a fly walking in the arena and a spider that creeps
// toward it. Positions are display pixels with y pointing down, velocities are
// pixels/second and headings are radians, where heading h moves along
// (cos h, sin h). The brain never sees positions; it only receives the two loom
// levels from world_looms() and answers through world_escape().

#define WORLD_PI 3.14159265358979323846
#define WORLD_HZ 120
#define WORLD_LEFT 3.0
#define WORLD_RIGHT 124.0
#define WORLD_TOP 13.0      // rows above are the status line
#define WORLD_BOTTOM 60.0
#define SPIDER_RADIUS 4.0
#define SPIDER_CATCH_DISTANCE 6.0
#define SPIDER_SPAWN_MARGIN 8.0
// Tunable at compile time, for example by research/fly/escape_sim.py --define.
#ifndef FLY_WALK_SPEED
#define FLY_WALK_SPEED 9.0
#endif
#ifndef FLY_JUMP_SPEED
#define FLY_JUMP_SPEED 80.0
#endif
#ifndef FLY_JUMP_SECONDS
#define FLY_JUMP_SECONDS 0.45
#endif
#ifndef SPIDER_SPEED
#define SPIDER_SPEED 3.0
#endif
#ifndef SPIDER_RETREAT_SPEED
#define SPIDER_RETREAT_SPEED 24.0
#endif
#ifndef LOOM_FAR_DISTANCE
#define LOOM_FAR_DISTANCE 70.0  // loom 0 at this distance or farther
#endif
#ifndef LOOM_NEAR_DISTANCE
#define LOOM_NEAR_DISTANCE 30.0  // loom 1 at this distance or closer
#endif
// Escape activity takes several neural steps to fade after the spider leaves.
#ifndef ESCAPE_REFRACTORY_SECONDS
#define ESCAPE_REFRACTORY_SECONDS 6.0
#endif

enum { FLY_WALK, FLY_JUMP };
enum { SPIDER_ABSENT, SPIDER_APPROACH, SPIDER_RETREAT };

typedef struct {
    double fly_x, fly_y, fly_heading, fly_timer, turn_timer, refractory;
    int fly_state;
    double spider_x, spider_y, spider_timer;
    int spider_state;
    uint32_t rng, escapes, caught, encounters;
    int last_escape_side;  // -1 none, 0 danger seen on the left, 1 on the right
    uint64_t ticks;
} EscapeWorld;

// Keep the generator and draw order stable so a seed reproduces a session.
static inline uint32_t world_random(EscapeWorld *w) {
    uint32_t value = w->rng;
    value ^= value << 13;
    value ^= value >> 17;
    value ^= value << 5;
    return w->rng = value;
}

static inline double world_uniform(EscapeWorld *w) {
    return (double)world_random(w) / 4294967296.0;
}

static inline double world_clip(double value, double low, double high) {
    return value < low ? low : (value > high ? high : value);
}

static inline double world_wrap(double angle) {
    while (angle > WORLD_PI)
        angle -= 2 * WORLD_PI;
    while (angle <= -WORLD_PI)
        angle += 2 * WORLD_PI;
    return angle;
}

static inline void world_spider_wait(EscapeWorld *w) {
    w->spider_state = SPIDER_ABSENT;
    w->spider_timer = 4.0 + 5.0 * world_uniform(w);
}

// A zero seed uses 1 to avoid the xorshift zero state.
static inline void world_init(EscapeWorld *w, uint32_t seed) {
    memset(w, 0, sizeof(*w));
    w->rng = seed ? seed : 1;
    w->fly_x = 64;
    w->fly_y = 37;
    w->fly_heading = 2 * WORLD_PI * world_uniform(w);
    w->turn_timer = 1.0;
    w->last_escape_side = -1;
    world_spider_wait(w);
}

static inline void world_spawn_spider(EscapeWorld *w) {
    double t = world_uniform(w);
    switch (world_random(w) % 4) {
    case 0: w->spider_x = WORLD_LEFT - SPIDER_SPAWN_MARGIN; w->spider_y = WORLD_TOP + t * (WORLD_BOTTOM - WORLD_TOP); break;
    case 1: w->spider_x = WORLD_RIGHT + SPIDER_SPAWN_MARGIN; w->spider_y = WORLD_TOP + t * (WORLD_BOTTOM - WORLD_TOP); break;
    case 2: w->spider_x = WORLD_LEFT + t * (WORLD_RIGHT - WORLD_LEFT); w->spider_y = WORLD_TOP - SPIDER_SPAWN_MARGIN; break;
    default: w->spider_x = WORLD_LEFT + t * (WORLD_RIGHT - WORLD_LEFT); w->spider_y = WORLD_BOTTOM + SPIDER_SPAWN_MARGIN; break;
    }
    w->spider_state = SPIDER_APPROACH;
    w->encounters++;
}

static inline double world_spider_distance(const EscapeWorld *w) {
    return hypot(w->spider_x - w->fly_x, w->spider_y - w->fly_y);
}

// Move one step along a heading and bounce off the arena walls.
static inline void world_move_fly(EscapeWorld *w, double speed, double dt) {
    double x = w->fly_x + cos(w->fly_heading) * speed * dt;
    double y = w->fly_y + sin(w->fly_heading) * speed * dt;
    if (x < WORLD_LEFT || x > WORLD_RIGHT) {
        w->fly_heading = world_wrap(WORLD_PI - w->fly_heading);
        x = world_clip(x, WORLD_LEFT, WORLD_RIGHT);
    }
    if (y < WORLD_TOP || y > WORLD_BOTTOM) {
        w->fly_heading = world_wrap(-w->fly_heading);
        y = world_clip(y, WORLD_TOP, WORLD_BOTTOM);
    }
    w->fly_x = x;
    w->fly_y = y;
}

// Advance exactly one tick at WORLD_HZ.
static inline void world_tick(EscapeWorld *w) {
    const double dt = 1.0 / WORLD_HZ;
    if (w->refractory > 0)
        w->refractory -= dt;
    if (w->fly_state == FLY_JUMP) {
        world_move_fly(w, FLY_JUMP_SPEED, dt);
        if ((w->fly_timer -= dt) <= 0)
            w->fly_state = FLY_WALK;
    } else {
        // Random walk: small heading jitter and an occasional new direction.
        w->fly_heading = world_wrap(w->fly_heading + (world_uniform(w) - 0.5) * 0.08);
        if ((w->turn_timer -= dt) <= 0) {
            w->fly_heading = world_wrap(w->fly_heading + (world_uniform(w) - 0.5) * 2.4);
            w->turn_timer = 1.0 + 2.0 * world_uniform(w);
        }
        world_move_fly(w, FLY_WALK_SPEED, dt);
    }

    if (w->spider_state == SPIDER_ABSENT) {
        if ((w->spider_timer -= dt) <= 0)
            world_spawn_spider(w);
    } else {
        double dx = w->fly_x - w->spider_x, dy = w->fly_y - w->spider_y;
        double distance = hypot(dx, dy);
        if (w->spider_state == SPIDER_APPROACH) {
            if (distance <= SPIDER_CATCH_DISTANCE) {
                w->caught++;
                w->spider_state = SPIDER_RETREAT;
                w->fly_x = WORLD_LEFT + (WORLD_RIGHT - WORLD_LEFT) * (0.2 + 0.6 * world_uniform(w));
                w->fly_y = WORLD_TOP + (WORLD_BOTTOM - WORLD_TOP) * (0.2 + 0.6 * world_uniform(w));
            } else if (distance > 0) {
                w->spider_x += dx / distance * SPIDER_SPEED * dt;
                w->spider_y += dy / distance * SPIDER_SPEED * dt;
            }
        } else if (distance > 0) {
            w->spider_x -= dx / distance * SPIDER_RETREAT_SPEED * dt;
            w->spider_y -= dy / distance * SPIDER_RETREAT_SPEED * dt;
            if (w->spider_x < WORLD_LEFT - 2 * SPIDER_SPAWN_MARGIN || w->spider_x > WORLD_RIGHT + 2 * SPIDER_SPAWN_MARGIN ||
                w->spider_y < WORLD_TOP - 2 * SPIDER_SPAWN_MARGIN || w->spider_y > WORLD_BOTTOM + 2 * SPIDER_SPAWN_MARGIN)
                world_spider_wait(w);
        } else {
            world_spider_wait(w);
        }
    }
    w->ticks++;
}

// Loom level per eye in [0, 1]. The spider's angular size grows as it approaches:
// 0 at LOOM_FAR_DISTANCE, 1 at LOOM_NEAR_DISTANCE or closer. The brain needs a few
// neural steps of strong looming to cross the escape threshold, so full looming
// starts well before the catch distance. It is split between the eyes by its
// bearing: fully on one side at 90 degrees, shared equally ahead and behind.
static inline void world_looms(const EscapeWorld *w, float looms[2]) {
    looms[0] = looms[1] = 0;
    if (w->spider_state != SPIDER_APPROACH)
        return;
    double dx = w->spider_x - w->fly_x, dy = w->spider_y - w->fly_y;
    double distance = hypot(dx, dy);
    double size = 2 * atan(SPIDER_RADIUS / fmax(distance, SPIDER_CATCH_DISTANCE));
    double far = 2 * atan(SPIDER_RADIUS / LOOM_FAR_DISTANCE);
    double near = 2 * atan(SPIDER_RADIUS / LOOM_NEAR_DISTANCE);
    double level = world_clip((size - far) / (near - far), 0, 1);
    // With y pointing down, a positive cross product puts the spider on the fly's right.
    double hx = cos(w->fly_heading), hy = sin(w->fly_heading);
    double bearing = atan2(hx * dy - hy * dx, hx * dx + hy * dy);
    double right = 0.5 * (1 + sin(bearing));
    looms[0] = (float)(level * (1 - right));
    looms[1] = (float)(level * right);
}

// Apply one neural decision. drive_left/right are the mean escape-neuron states
// for each side. An escape turns the fly 90 degrees away from the side with the
// larger drive and jumps. The spider gives up. Returns the side the danger was
// seen on, or -1 when there is no escape.
static inline int world_escape(EscapeWorld *w, float drive_left, float drive_right, float threshold) {
    if (!(drive_left > threshold || drive_right > threshold) || w->fly_state != FLY_WALK || w->refractory > 0)
        return -1;
    int side = drive_left > drive_right ? 0 : 1;
    // Turning right is +90 degrees with y pointing down.
    w->fly_heading = world_wrap(w->fly_heading + (side == 0 ? (WORLD_PI / 2) : -(WORLD_PI / 2)));
    w->fly_state = FLY_JUMP;
    w->fly_timer = FLY_JUMP_SECONDS;
    w->refractory = ESCAPE_REFRACTORY_SECONDS;
    w->last_escape_side = side;
    if (w->spider_state == SPIDER_APPROACH) {
        w->escapes++;
        w->spider_state = SPIDER_RETREAT;
    }
    return side;
}

#endif
