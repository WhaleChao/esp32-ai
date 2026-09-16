// Escape world regression: determinism, loom geometry, escape direction, the
// spider's approach/catch/retreat cycle and arena bounds over a long session.
// Synthetic decisions exercise the world, not the brain.
// cc -std=c11 -O1 -ffp-contract=off -Wall -Wextra -Werror -fsanitize=address,undefined \
//   runtime/host_verify/escape_world_test.c -lm -o /tmp/escape_world_test
// /tmp/escape_world_test
// Assertions must remain enabled. No model assets or ESP32 required.

#include "../../firmware/esp32_fly/escape_world.h"

#include <assert.h>
#include <stdio.h>

static void place(EscapeWorld *w, double fly_x, double fly_y, double heading, double spider_x, double spider_y) {
    w->fly_x = fly_x;
    w->fly_y = fly_y;
    w->fly_heading = heading;
    w->spider_x = spider_x;
    w->spider_y = spider_y;
    w->spider_state = SPIDER_APPROACH;
}

static void test_determinism(void) {
    EscapeWorld a, b;
    world_init(&a, 61000);
    world_init(&b, 61000);
    for (int t = 0; t < WORLD_HZ * 600; t++) {
        world_tick(&a);
        world_tick(&b);
    }
    assert(memcmp(&a, &b, sizeof(a)) == 0);
    world_init(&b, 61001);
    for (int t = 0; t < WORLD_HZ * 600; t++)
        world_tick(&b);
    assert(a.fly_x != b.fly_x || a.fly_y != b.fly_y);
}

static void test_looms(void) {
    EscapeWorld w;
    float looms[2], near[2], far[2];
    world_init(&w, 1);
    // Facing +x with y pointing down, a spider above on the screen is on the fly's left.
    place(&w, 60, 36, 0, 60, 26);
    world_looms(&w, looms);
    assert(looms[0] > 0.3f && looms[1] < 1e-6f);
    place(&w, 60, 36, 0, 60, 46);
    world_looms(&w, looms);
    assert(looms[1] > 0.3f && looms[0] < 1e-6f);
    place(&w, 60, 36, 0, 70, 36);
    world_looms(&w, looms);
    assert(looms[0] > 0 && fabs(looms[0] - looms[1]) < 1e-6f);
    place(&w, 60, 36, 0, 60 + LOOM_FAR_DISTANCE + 1, 36);
    world_looms(&w, looms);
    assert(looms[0] == 0 && looms[1] == 0);
    place(&w, 60, 36, 0, 60 + SPIDER_CATCH_DISTANCE, 36);
    world_looms(&w, looms);
    assert(fabs(looms[0] + looms[1] - 1) < 1e-6f);
    place(&w, 60, 36, 0, 60 + LOOM_NEAR_DISTANCE, 36);
    world_looms(&w, looms);
    assert(fabs(looms[0] + looms[1] - 1) < 1e-5f);
    place(&w, 60, 36, 0, 60 + (LOOM_NEAR_DISTANCE + LOOM_FAR_DISTANCE) / 2, 36);
    world_looms(&w, looms);
    assert(looms[0] + looms[1] > 0.05f && looms[0] + looms[1] < 0.95f);
    // Between the near and far loom distances, a closer spider looms larger.
    double span = LOOM_FAR_DISTANCE - LOOM_NEAR_DISTANCE;
    place(&w, 60, 36, 0, 60, 36 - (LOOM_NEAR_DISTANCE + 0.75 * span));
    world_looms(&w, far);
    place(&w, 60, 36, 0, 60, 36 - (LOOM_NEAR_DISTANCE + 0.25 * span));
    world_looms(&w, near);
    assert(far[0] > 0 && near[0] > far[0] && near[0] < 1);
    // Turning the fly around moves the same spider to the other eye.
    place(&w, 60, 36, WORLD_PI, 60, 26);
    world_looms(&w, looms);
    assert(looms[1] > 0.3f && looms[0] < 1e-6f);
    w.spider_state = SPIDER_ABSENT;
    world_looms(&w, looms);
    assert(looms[0] == 0 && looms[1] == 0);
}

static void test_escape_direction(void) {
    EscapeWorld w;
    world_init(&w, 1);
    place(&w, 60, 36, 0, 60, 26);
    assert(world_escape(&w, 0.01f, 0.02f, 0.05f) == -1);
    assert(world_escape(&w, NAN, NAN, 0.05f) == -1);
    assert(w.fly_state == FLY_WALK && w.escapes == 0);
    assert(world_escape(&w, 0.2f, 0.01f, 0.05f) == 0);
    assert(fabs(w.fly_heading - WORLD_PI / 2) < 1e-12);  // danger on the left: turn right
    assert(w.fly_state == FLY_JUMP && w.spider_state == SPIDER_RETREAT);
    assert(w.escapes == 1 && w.last_escape_side == 0);
    assert(world_escape(&w, 0.3f, 0.01f, 0.05f) == -1);  // already jumping, then refractory
    for (int t = 0; t < (int)(ESCAPE_REFRACTORY_SECONDS * WORLD_HZ) + 2; t++)
        world_tick(&w);
    assert(w.fly_state == FLY_WALK && w.refractory <= 0);
    w.fly_heading = 0;
    assert(world_escape(&w, 0.01f, 0.2f, 0.05f) == 1);
    assert(fabs(w.fly_heading + WORLD_PI / 2) < 1e-12);  // danger on the right: turn left
    assert(w.escapes == 1);  // no spider was approaching this time

    // Walking again but still refractory: no escape.
    w.fly_state = FLY_WALK;
    w.refractory = 1.0;
    assert(world_escape(&w, 0.9f, 0.01f, 0.05f) == -1 && w.fly_state == FLY_WALK);

    // Equal drives count as danger on the right, as MODEL_FORMATS.md documents.
    world_init(&w, 3);
    place(&w, 60, 36, 0, 60, 26);
    assert(world_escape(&w, 0.2f, 0.2f, 0.05f) == 1);
    assert(fabs(w.fly_heading + WORLD_PI / 2) < 1e-12);
}

static void test_corner_bounce(void) {
    EscapeWorld w;
    world_init(&w, 5);
    w.spider_state = SPIDER_ABSENT;
    w.spider_timer = 1e9;
    for (int corner = 0; corner < 4; corner++) {
        double x = corner % 2 ? WORLD_RIGHT : WORLD_LEFT, y = corner / 2 ? WORLD_BOTTOM : WORLD_TOP;
        w.fly_x = x;
        w.fly_y = y;
        w.fly_heading = atan2(y - 36, x - 64);  // straight into the corner
        w.fly_state = FLY_JUMP;
        w.fly_timer = 10;
        for (int t = 0; t < WORLD_HZ; t++) {
            world_tick(&w);
            assert(w.fly_x >= WORLD_LEFT && w.fly_x <= WORLD_RIGHT && w.fly_y >= WORLD_TOP && w.fly_y <= WORLD_BOTTOM);
            assert(isfinite(w.fly_heading) && fabs(w.fly_heading) <= WORLD_PI);
        }
        assert(hypot(w.fly_x - x, w.fly_y - y) > 10);  // it bounced away instead of sticking
    }
}

static void test_catch_and_retreat(void) {
    EscapeWorld w;
    world_init(&w, 7);
    place(&w, 60, 36, 0, 60 + SPIDER_CATCH_DISTANCE - 1, 36);
    world_tick(&w);
    assert(w.caught == 1 && w.spider_state == SPIDER_RETREAT);
    assert(w.fly_x >= WORLD_LEFT && w.fly_x <= WORLD_RIGHT && w.fly_y >= WORLD_TOP && w.fly_y <= WORLD_BOTTOM);
    int ticks = 0;
    while (w.spider_state == SPIDER_RETREAT && ticks++ < WORLD_HZ * 60)
        world_tick(&w);
    assert(w.spider_state == SPIDER_ABSENT);
    uint32_t encounters = w.encounters;
    ticks = 0;
    while (w.spider_state == SPIDER_ABSENT && ticks++ < WORLD_HZ * 10)
        world_tick(&w);
    assert(w.spider_state == SPIDER_APPROACH && w.encounters == encounters + 1);
}

static unsigned long long test_long_session(uint32_t seed) {
    EscapeWorld w;
    world_init(&w, seed);
    unsigned long long decisions = 0;
    for (int t = 0; t < WORLD_HZ * 3600; t++) {
        world_tick(&w);
        assert(w.fly_x >= WORLD_LEFT && w.fly_x <= WORLD_RIGHT && w.fly_y >= WORLD_TOP && w.fly_y <= WORLD_BOTTOM);
        assert(isfinite(w.spider_x) && isfinite(w.spider_y) && fabs(w.fly_heading) <= WORLD_PI);
        if (t % 204 == 0) {
            float looms[2];
            world_looms(&w, looms);
            assert(looms[0] >= 0 && looms[1] >= 0 && looms[0] + looms[1] <= 1.0f + 1e-6f);
            // Stand-in for the brain: a strong loom on one side triggers an escape.
            decisions += world_escape(&w, looms[0], looms[1], 0.25f) >= 0;
        }
    }
    assert(w.escapes + w.caught <= w.encounters);
    assert(w.encounters > 0);
    return decisions;
}

int main(void) {
    test_determinism();
    test_looms();
    test_escape_direction();
    test_corner_bounce();
    test_catch_and_retreat();
    unsigned long long decisions = test_long_session(61000) + test_long_session(1);
    printf("PASS: determinism, loom geometry, escape direction, catch/retreat, 2 h of bounded sessions (%llu escapes)\n",
           decisions);
    return 0;
}
