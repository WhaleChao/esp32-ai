// Host-only ctypes bridge to the firmware's escape world.
// Compile with -std=c11 -O3 -ffp-contract=off -shared -fPIC.
#include "../../firmware/esp32_fly/escape_world.h"

#include <stdlib.h>

EscapeWorld *escape_world_create(uint32_t seed) {
    EscapeWorld *w = malloc(sizeof(*w));
    if (w)
        world_init(w, seed);
    return w;
}
void escape_world_destroy(EscapeWorld *w) { free(w); }
void escape_world_advance(EscapeWorld *w, uint32_t ticks) {
    while (ticks--)
        world_tick(w);
}
void escape_world_looms(EscapeWorld *w, float *looms) { world_looms(w, looms); }
int escape_world_decide(EscapeWorld *w, float drive_left, float drive_right, float threshold) {
    return world_escape(w, drive_left, drive_right, threshold);
}
// escapes, caught, encounters, spider_state, fly_state
void escape_world_stats(EscapeWorld *w, uint32_t *stats) {
    stats[0] = w->escapes;
    stats[1] = w->caught;
    stats[2] = w->encounters;
    stats[3] = (uint32_t)w->spider_state;
    stats[4] = (uint32_t)w->fly_state;
}
// fly_x, fly_y, fly_heading, spider_x, spider_y
void escape_world_positions(EscapeWorld *w, double *positions) {
    positions[0] = w->fly_x;
    positions[1] = w->fly_y;
    positions[2] = w->fly_heading;
    positions[3] = w->spider_x;
    positions[4] = w->spider_y;
}
