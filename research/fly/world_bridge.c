// Host-only ctypes bridge to the firmware's escape world.
// Compile with -std=c11 -O3 -ffp-contract=off -shared -fPIC.
#include "../../firmware/esp32_fly/escape_world.h"

#include <stdlib.h>

// The world and the sample of the current neural cycle, as the board keeps them.
typedef struct {
    EscapeWorld world;
    WorldSample sample;
} Bridge;

Bridge *escape_world_create(uint32_t seed) {
    Bridge *b = malloc(sizeof(*b));
    if (b) {
        world_init(&b->world, seed);
        world_sample(&b->world, &b->sample);
    }
    return b;
}
void escape_world_destroy(Bridge *b) { free(b); }
void escape_world_advance(Bridge *b, uint32_t ticks) {
    while (ticks--)
        world_tick(&b->world);
}
// Starts a neural cycle: samples the world and returns the loom levels.
void escape_world_sample(Bridge *b, float *looms) {
    world_sample(&b->world, &b->sample);
    looms[0] = b->sample.looms[0];
    looms[1] = b->sample.looms[1];
}
// Ends the cycle: applies the decision against the latest sample.
int escape_world_decide(Bridge *b, float drive_left, float drive_right, float threshold) {
    return world_escape(&b->world, &b->sample, drive_left, drive_right, threshold);
}
// escapes, caught, encounters, spider_state, fly_state
void escape_world_stats(Bridge *b, uint32_t *stats) {
    stats[0] = b->world.escapes;
    stats[1] = b->world.caught;
    stats[2] = b->world.encounters;
    stats[3] = (uint32_t)b->world.spider_state;
    stats[4] = (uint32_t)b->world.fly_state;
}
// fly_x, fly_y, fly_heading, spider_x, spider_y
void escape_world_positions(Bridge *b, double *positions) {
    positions[0] = b->world.fly_x;
    positions[1] = b->world.fly_y;
    positions[2] = b->world.fly_heading;
    positions[3] = b->world.spider_x;
    positions[4] = b->world.spider_y;
}
