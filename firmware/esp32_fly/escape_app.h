#ifndef FLY_ESCAPE_APP_H
#define FLY_ESCAPE_APP_H
#include "generated/escape_circuit.h"
#include <errno.h>

// Included after the sketch's graph runner and serial helpers. Nothing here is
// trained: the spider's looming drives the fly's LC4/LPLC2 neurons, and the mean
// activity of the escape descending neurons on each side decides an escape.
static_assert(CONNECTOME_N == 48311u, "The circuit's indices require the selected graph");
static_assert(ESCAPE_CONTRACT == 1u, "Regenerate assets for the supported escape contract");
static const float escape_threshold = ESCAPE_THRESHOLD;
#define ESCAPE_OUTPUTS (ESCAPE_OUTPUTS_L + ESCAPE_OUTPUTS_R)
// Room for one JSON event carrying every output value.
#define ESCAPE_LINE_BYTES (384 + 18 * ESCAPE_OUTPUTS)
// The world keeps moving at 120 Hz while a neural cycle runs; a short cycle is
// padded so escape timing does not depend on cache hits.
#define ESCAPE_MIN_CYCLE_US 1600000u

// PLAY uses the default seed or one unsigned 32-bit seed.
static bool parse_play(const char *command, uint32_t *seed) {
    *seed = 61000;
    if (!strcmp(command, "PLAY"))
        return true;
    if (strncmp(command, "PLAY ", 5) || command[5] < '0' || command[5] > '9')
        return false;
    char *end;
    errno = 0;
    unsigned long value = strtoul(command + 5, &end, 10);
    if (errno || value > UINT32_MAX || *end)
        return false;
    *seed = (uint32_t)value;
    return true;
}

// LOOM takes the exact float32 bits of both loom levels as eight hex digits each,
// for example "LOOM 3f800000 00000000" for a spider fully on the left.
static bool parse_loom(const char *command, float looms[2]) {
    if (strlen(command) != 22 || strncmp(command, "LOOM ", 5) || command[13] != ' ')
        return false;
    uint32_t bits[2] = {0, 0};
    for (int field = 0; field < 2; field++)
        for (int k = 0; k < 8; k++) {
            char c = command[5 + field * 9 + k];
            int digit = c >= '0' && c <= '9' ? c - '0' : (c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1);
            if (digit < 0)
                return false;
            bits[field] = bits[field] << 4 | (uint32_t)digit;
        }
    memcpy(looms, bits, sizeof(bits));
    return looms[0] >= 0 && looms[0] <= 1 && looms[1] >= 0 && looms[1] <= 1;
}

static void escape_encode(const float looms[2]) {
    memset(input_vector, 0, CONNECTOME_N * 4);
    const float left = ESCAPE_AMPLITUDE * looms[0], right = ESCAPE_AMPLITUDE * looms[1];
    for (uint32_t i = 0; i < ESCAPE_INPUTS_L; i++)
        input_vector[escape_inputs_left[i]] = left;
    for (uint32_t i = 0; i < ESCAPE_INPUTS_R; i++)
        input_vector[escape_inputs_right[i]] = right;
}

// Output-neuron states, left then right, and the float32 mean per side.
static void escape_read(const float *state, float drives[2], float outputs[ESCAPE_OUTPUTS]) {
    float left = 0, right = 0;
    for (uint32_t i = 0; i < ESCAPE_OUTPUTS_L; i++)
        left += outputs[i] = state[escape_outputs_left[i]];
    for (uint32_t i = 0; i < ESCAPE_OUTPUTS_R; i++)
        right += outputs[ESCAPE_OUTPUTS_L + i] = state[escape_outputs_right[i]];
    drives[0] = left / ESCAPE_OUTPUTS_L;
    drives[1] = right / ESCAPE_OUTPUTS_R;
}

// Appends "name":[v,...] and returns the new length, or the buffer size on overflow.
static size_t append_values(char *line, size_t size, size_t used, const char *name, const float *values,
                            unsigned count) {
    int n = snprintf(line + used, size - used, "\"%s\":[", name);
    for (unsigned i = 0; n >= 0 && used + n < size && i < count; i++) {
        int more = snprintf(line + used + n, size - used - n, i ? ",%.9g" : "%.9g", values[i]);
        n = more < 0 ? -1 : n + more;
    }
    if (n < 0 || used + n + 1 >= size)
        return size;
    line[used + n] = ']';
    line[used + n + 1] = 0;
    return used + n + 1;
}

// Telemetry is dropped rather than blocking a neural cycle when the host is not reading.
static void write_line_if_room(const char *line, size_t length) {
    if (length > 0 && Serial && Serial.availableForWrite() >= (int)length)
        Serial.write((const uint8_t *)line, length);
}

static void escape_start(uint32_t seed) {
    ui_stop();
    if (!set_mode(6))
        return;
    memset(state_a, 0, CONNECTOME_N * 4);
    memset(state_b, 0, CONNECTOME_N * 4);
    memset(input_vector, 0, CONNECTOME_N * 4);
    has_state = true;
    xSemaphoreTake(game_mutex, portMAX_DELAY);
    world_init(&world, seed);
    neural_steps = 0;
    last_graph_us = last_cycle_us = 0;
    last_looms[0] = last_looms[1] = last_drives[0] = last_drives[1] = 0;
    game_error[0] = 0;
    game_epoch_us = now();
    game_active = true;
    xSemaphoreGive(game_mutex);
    ui_status();
}

// One neural cycle: sample the spider, run the graph, apply the decision at the end.
static void escape_update() {
    if (!ui_active())
        return;
    uint64_t start = now();
    float looms[2];
    xSemaphoreTake(game_mutex, portMAX_DELAY);
    world_looms(&world, looms);
    uint64_t sample_tick = world.ticks;
    xSemaphoreGive(game_mutex);
    escape_encode(looms);
    uint64_t graph_start = now();
    int error = run_graph();
    last_graph_us = now() - graph_start;
    if (error) {
        ui_stop();
        fatal("escape graph update failed");
        return;
    }
    float drives[2], outputs[ESCAPE_OUTPUTS];
    escape_read(state_b, drives, outputs);
    memcpy(state_a, state_b, CONNECTOME_N * 4);
    while (now() - start < ESCAPE_MIN_CYCLE_US)
        delay(1);
    xSemaphoreTake(game_mutex, portMAX_DELAY);
    bool still_active = game_active;
    int side = still_active ? world_escape(&world, drives[0], drives[1], escape_threshold) : -1;
    memcpy(last_looms, looms, sizeof(looms));
    memcpy(last_drives, drives, sizeof(drives));
    uint32_t escapes = world.escapes, caught = world.caught, encounters = world.encounters;
    xSemaphoreGive(game_mutex);
    if (!still_active)
        return;
    last_cycle_us = now() - start;
    neural_steps++;

    char line[ESCAPE_LINE_BYTES];
    uint32_t bits[2];
    memcpy(bits, looms, sizeof(bits));
    int head = snprintf(line, sizeof(line),
                        "{\"event\":\"escape_step\",\"step\":%u,\"sample_tick\":%llu,\"loom_bits\":[%u,%u],",
                        (unsigned)neural_steps, (unsigned long long)sample_tick, (unsigned)bits[0],
                        (unsigned)bits[1]);
    size_t used = head < 0 ? sizeof(line) : (size_t)head;
    if (used < sizeof(line))
        used = append_values(line, sizeof(line), used, "drives", drives, 2);
    if (used + 1 < sizeof(line)) {
        line[used++] = ',';
        line[used] = 0;
        used = append_values(line, sizeof(line), used, "outputs", outputs, ESCAPE_OUTPUTS);
    }
    if (used < sizeof(line)) {
        int tail = snprintf(line + used, sizeof(line) - used,
                            ",\"escape_side\":%d,\"escapes\":%u,\"caught\":%u,\"encounters\":%u,"
                            "\"graph_us\":%llu,\"cycle_us\":%llu}\n",
                            side, (unsigned)escapes, (unsigned)caught, (unsigned)encounters,
                            (unsigned long long)last_graph_us, (unsigned long long)last_cycle_us);
        if (tail > 0 && used + tail < sizeof(line))
            write_line_if_room(line, used + tail);
    }
    char status[UI_STATUS_BYTES];
    int length = ui_format_status(status, sizeof(status));
    if (length > 0 && (size_t)length < sizeof(status))
        write_line_if_room(status, (size_t)length);
}

// Verification: ZERO clears graph state; LOOM then runs single steps with exact inputs.
static void zero_command() {
    ui_stop();
    memset(state_a, 0, CONNECTOME_N * 4);
    has_state = true;
    Serial.println("{\"event\":\"zeroed\"}");
}

static void loom_command(const float looms[2]) {
    ui_stop();
    if (!has_state) {
        // Not fatal: MODE clears the state, and the verifier recovers with ZERO.
        Serial.println("{\"event\":\"loom_error\",\"reason\":\"state not initialized; send ZERO\"}");
        return;
    }
    escape_encode(looms);
    int64_t start = esp_timer_get_time();
    int error = run_graph();
    if (error) {
        fatal("graph update failed");
        return;
    }
    memcpy(state_a, state_b, CONNECTOME_N * 4);
    float drives[2], outputs[ESCAPE_OUTPUTS];
    escape_read(state_a, drives, outputs);
    char line[ESCAPE_LINE_BYTES];
    int head = snprintf(line, sizeof(line), "{\"event\":\"loom\",\"mode\":%d,\"compute_us\":%lld,", mode,
                        (long long)(esp_timer_get_time() - start));
    size_t used = head < 0 ? sizeof(line) : (size_t)head;
    if (used < sizeof(line))
        used = append_values(line, sizeof(line), used, "drives", drives, 2);
    if (used + 1 < sizeof(line)) {
        line[used++] = ',';
        line[used] = 0;
        used = append_values(line, sizeof(line), used, "outputs", outputs, ESCAPE_OUTPUTS);
    }
    if (used + 2 >= sizeof(line)) {
        fatal("loom response overflow");
        return;
    }
    line[used++] = '}';
    line[used++] = '\n';
    Serial.write((const uint8_t *)line, used);
}
#endif
