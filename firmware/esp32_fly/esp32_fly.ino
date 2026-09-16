#include <Arduino.h>
#include "esp_heap_caps.h"
#include "esp_partition.h"
#include "esp_timer.h"
#define FAST_CODE IRAM_ATTR
#include "../../runtime/connectome_fast.h"
#include "../../runtime/connectome_simd_test.h"
#include "generated/connectome_build.h"
#include "generated/escape_circuit.h"
#include "escape_ui.h"

// Core 0 runs the first graph range; the Arduino loop on core 1 runs the rest.
// Task notifications hand off immutable job inputs and wait for both outputs
// before state_a is replaced. The UI task never accesses the graph buffers.
extern "C" int64_t connectome_dot_s16(const int16_t *, const int16_t *, uint32_t);
static const esp_partition_t *partition;
static FastGraph graph;
static FastWorkspace workers[2];
static FastProfile profiles[2];
static float *state_a, *state_b, *input_vector;
static void *hot;
static uint8_t *cache;
static uint32_t cache_capacity, cache_used, cached_blocks, psram_used, internal_used;
static bool ready = false, has_state = false, simd_ok = false;
static bool simd_core1_ok = false;
static int mode = 6, worker_error = 0;
// Mode 6: two cores, Q29 states in internal RAM, SIMD dot products, Flash
// mapping and a PSRAM cache of decoded varints. Modes 0..8 remain for regression.
static int cache_staged = 0;
static esp_partition_mmap_handle_t mapping_handle;
static TaskHandle_t worker_h, main_h;
static const float *job_float;
static const int32_t *job_q;
static FastDot job_dot;
static bool job_cache;
static uint64_t prep_us;
static void *allocate(size_t n, bool internal) {
    void *p = heap_caps_aligned_alloc(
        16, n, (internal ? MALLOC_CAP_INTERNAL : MALLOC_CAP_SPIRAM) | MALLOC_CAP_8BIT);
    if (p) {
        if (internal)
            internal_used += n;
        else
            psram_used += n;
    }
    return p;
}
static int read_model(void *, uint32_t offset, void *dst, size_t bytes) {
    return (uint64_t)offset + bytes <= CONNECTOME_BYTES &&
           esp_partition_read(partition, offset, dst, bytes) == ESP_OK;
}
static void cooperate(void *) { delay(1); }
static uint64_t now() { return esp_timer_get_time(); }
static void fatal(const char *reason) {
    ui_error(reason);
    ready = false;
    has_state = false;
    Serial.printf("{\"event\":\"fatal\",\"reason\":\"%s\"}\n", reason);
}
static void info() {
    Serial.printf("{\"event\":\"info\",\"ready\":%s,\"variant\":\"escape-v1\",\"mode\":%d,"
                  "\"mapped_bytes\":%u,\"cache_staged\":%d,\"simd_selftest\":%s,\"neurons\":%u,"
                  "\"edges\":%u,\"model_bytes\":%u,\"model_fnv1a\":\"%08x\",\"psram_bytes\":%u,"
                  "\"explicit_psram_bytes\":%u,\"explicit_internal_bytes\":%u,\"cache_capacity\":%"
                  "u,\"cache_used\":%u,\"cached_blocks\":%u,\"blocks\":%u,\"split\":%u,\"free_"
                  "psram\":%u,\"largest_psram_block\":%u,\"free_internal\":%u,"
                  "\"verification_protocol\":2,\"contract\":%u,\"circuit_bytes\":%u,"
                  "\"circuit_fnv1a\":\"%08x\",\"inputs\":[%u,%u],\"outputs\":[%u,%u],"
                  "\"amplitude\":%.9g,\"threshold\":%.9g,\"simd_core0\":%s,\"simd_core1\":%s}\n",
                  ready ? "true" : "false", mode, (unsigned)(graph.mapped ? CONNECTOME_BYTES : 0u),
                  cache_staged, simd_ok ? "true" : "false", (unsigned)CONNECTOME_N, (unsigned)CONNECTOME_E,
                  (unsigned)CONNECTOME_BYTES, (unsigned)CONNECTOME_HASH, (unsigned)ESP.getPsramSize(),
                  (unsigned)psram_used, (unsigned)(internal_used + sizeof(workers)),
                  (unsigned)cache_capacity, (unsigned)cache_used, (unsigned)cached_blocks,
                  (unsigned)graph.count, (unsigned)graph.split,
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                  (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM),
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL), (unsigned)ESCAPE_CONTRACT,
                  (unsigned)ESCAPE_CIRCUIT_BYTES, (unsigned)ESCAPE_CIRCUIT_HASH, (unsigned)ESCAPE_INPUTS_L,
                  (unsigned)ESCAPE_INPUTS_R, (unsigned)ESCAPE_OUTPUTS_L, (unsigned)ESCAPE_OUTPUTS_R,
                  ESCAPE_AMPLITUDE, ESCAPE_THRESHOLD,
                  simd_ok ? "true" : "false", simd_core1_ok ? "true" : "false");
}
static bool simd_selftest() { return connectome_dot_selftest(connectome_dot_s16); }
static void worker_main(void *) {
    simd_ok = simd_selftest();
    xTaskNotifyGive(main_h);
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        worker_error = fast_range(&graph, &workers[0], 0, graph.split, state_a, input_vector,
                                  state_b, job_float, job_q, job_dot, job_cache, now, &profiles[0]);
        xTaskNotifyGive(main_h);
    }
}
static int run_graph() {
    uint64_t start = now();
    if (mode == 3 || mode == 4 || mode == 6 || mode == 8) {
        if (fast_quantize(state_a, (int32_t *)hot, CONNECTOME_N))
            return 3;
        job_q = (int32_t *)hot;
        job_float = NULL;
    } else {
        memcpy(hot, state_a, CONNECTOME_N * 4);
        job_float = (float *)hot;
        job_q = NULL;
    }
    job_dot = (mode == 4 || mode == 6 || mode == 8) ? connectome_dot_s16 : fast_dot_scalar;
    job_cache = mode >= 2;
    graph.use_mapping = mode >= 5;
    prep_us = now() - start;
    if (mode)
        xTaskNotifyGive(worker_h);
    else
        memset(&profiles[0], 0, sizeof(profiles[0]));
    int error =
        fast_range(&graph, &workers[1], mode ? graph.split : 0, graph.count, state_a, input_vector,
                   state_b, job_float, job_q, job_dot, job_cache, now, &profiles[1]);
    if (mode) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        if (worker_error)
            error = worker_error;
    }
    return error;
}
static void count_cache() {
    cache_used = 0;
    cached_blocks = 0;
    for (uint32_t i = 0; i < graph.count; i++)
        if (graph.blocks[i].cache) {
            cached_blocks++;
            cache_used +=
                ((cache_staged ? graph.blocks[i].edges * 4 : graph.blocks[i].raw) + 15) & ~15u;
        }
}
// Modes 7 and 8 need the packed cache; switching rebuilds it.
static bool set_mode(int value) {
    int staged = value >= 7;
    if (staged != cache_staged) {
        if (fast_prepare(&graph, &workers[0], cache, cache_capacity, staged)) {
            fatal("cache rebuild failed");
            return false;
        }
        cache_staged = staged;
        count_cache();
    }
    mode = value;
    return true;
}
static bool receive(void *dst, size_t length) {
    uint8_t *p = (uint8_t *)dst;
    size_t used = 0;
    uint32_t last = millis();
    while (used < length) {
        int available = Serial.available();
        if (available > 0) {
            size_t amount = min((size_t)available, length - used);
            size_t received = Serial.readBytes(p + used, amount);
            used += received;
            if (received)
                last = millis();
        } else
            delay(1);
        if (millis() - last > 30000)
            return false;
    }
    return true;
}
static bool vector_input(float *destination) {
    Serial.println("{\"event\":\"input_ready\"}");
    uint8_t hash[4];
    if (!receive(hash, 4)) {
        fatal("input checksum timeout");
        return false;
    }
    for (size_t offset = 0; offset < CONNECTOME_N * 4;) {
        size_t bytes = min((size_t)1024, (size_t)CONNECTOME_N * 4 - offset);
        if (!receive((uint8_t *)destination + offset, bytes)) {
            fatal("input timeout");
            return false;
        }
        offset += bytes;
        Serial.write((uint8_t)0x06);
    }
    if (connectome_hash(destination, CONNECTOME_N * 4) != connectome_u32(hash)) {
        fatal("input checksum mismatch");
        return false;
    }
    for (uint32_t i = 0; i < CONNECTOME_N; i++)
        if (!isfinite(destination[i])) {
            fatal("nonfinite input");
            return false;
        }
    return true;
}
static void send_output() {
    uint8_t ack;
    if (!receive(&ack, 1) || ack != 0x06) {
        fatal("output readiness timeout");
        return;
    }
    const uint8_t *p = (const uint8_t *)state_a;
    size_t sent = 0;
    while (sent < CONNECTOME_N * 4) {
        size_t chunk = min((size_t)4096, (size_t)CONNECTOME_N * 4 - sent), written = 0;
        uint32_t last = millis();
        while (written < chunk) {
            size_t n = Serial.write(p + sent + written, chunk - written);
            written += n;
            if (n)
                last = millis();
            else
                delay(1);
            if (millis() - last > 30000) {
                fatal("output write timeout");
                return;
            }
        }
        if (!receive(&ack, 1) || ack != 0x06) {
            fatal("output acknowledgement timeout");
            return;
        }
        sent += chunk;
    }
    Serial.println();
}

#include "escape_app.h"

void setup() {
    Serial.setRxBufferSize(8192);
    Serial.setTxBufferSize(8192);
    Serial.begin(921600);
    unsigned long started = millis();
    while (!Serial && millis() - started < 4000)
        delay(10);
    Serial.println("FLY_V1");
    ui_begin();
    if (!game_mutex) {
        fatal("UI mutex allocation failed");
        return;
    }
    if (!psramFound() || ESP.getPsramSize() < 8 * 1024 * 1024) {
        fatal("8 MiB PSRAM required");
        return;
    }
    partition =
        esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "flymodel");
    if (!partition || partition->size < CONNECTOME_BYTES) {
        fatal("model partition too small");
        return;
    }
    hot = allocate(CONNECTOME_N * 4, true);
    state_a = (float *)allocate(CONNECTOME_N * 4, false);
    state_b = (float *)allocate(CONNECTOME_N * 4, false);
    input_vector = (float *)allocate(CONNECTOME_N * 4, false);
    graph.rows = (FastRow *)allocate(CONNECTOME_N * sizeof(FastRow), false);
    graph.blocks = (FastBlock *)allocate(
        ((CONNECTOME_N + CONNECTOME_BLOCK_ROWS - 1) / CONNECTOME_BLOCK_ROWS) * sizeof(FastBlock), false);
    if (!hot || !state_a || !state_b || !input_vector || !graph.rows || !graph.blocks) {
        fatal("state allocation failed");
        return;
    }
    for (unsigned i = 0; i < 2; i++) {
        workers[i].stream.raw = (uint8_t *)allocate(CONNECTOME_RAW, false);
        workers[i].stream.compressed = (uint8_t *)allocate(CONNECTOME_COMPRESSED, false);
        workers[i].stream.degrees = (uint32_t *)allocate(CONNECTOME_BLOCK_ROWS * 4, true);
        if (!workers[i].stream.raw || !workers[i].stream.compressed || !workers[i].stream.degrees) {
            fatal("decoder allocation failed");
            return;
        }
    }
    uint32_t free_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    cache_capacity =
        min<uint32_t>(6u * 1024 * 1024, free_ps > 512u * 1024 ? free_ps - 512u * 1024 : 0u) & ~15u;
    cache = (uint8_t *)allocate(cache_capacity, false);
    if (!cache || !cache_capacity) {
        fatal("cache allocation failed");
        return;
    }
    uint32_t hash = 2166136261u;
    for (uint32_t offset = 0; offset < CONNECTOME_BYTES;) {
        size_t bytes = min((uint32_t)CONNECTOME_COMPRESSED, CONNECTOME_BYTES - offset);
        if (!read_model(NULL, offset, workers[0].stream.compressed, bytes)) {
            fatal("flash read failed");
            return;
        }
        hash = connectome_hash_more(hash, workers[0].stream.compressed, bytes);
        offset += bytes;
        delay(1);
    }
    if (hash != CONNECTOME_HASH) {
        fatal("model checksum mismatch");
        return;
    }
    graph.model = {CONNECTOME_N,
                   CONNECTOME_E,
                   CONNECTOME_BYTES,
                   CONNECTOME_BLOCK_ROWS,
                   CONNECTOME_RAW,
                   CONNECTOME_COMPRESSED,
                   read_model,
                   NULL,
                   cooperate,
                   NULL,
                   1};
    const void *mapped = NULL;
    if (esp_partition_mmap(partition, 0, CONNECTOME_BYTES, ESP_PARTITION_MMAP_DATA, &mapped,
                           &mapping_handle) != ESP_OK) {
        fatal("Flash mapping failed");
        return;
    }
    graph.mapped = (const uint8_t *)mapped;
    int error = fast_prepare(&graph, &workers[0], cache, cache_capacity, cache_staged);
    if (error) {
        Serial.printf("prepare error %d\n", error);
        fatal("model validation failed");
        return;
    }
    count_cache();
    simd_core1_ok = simd_selftest();
    if (!simd_core1_ok) {
        fatal("SIMD selftest core 1 failed");
        return;
    }
    main_h = xTaskGetCurrentTaskHandle();
    if (xTaskCreatePinnedToCore(worker_main, "graph-worker", 8192, NULL, 1, &worker_h, 0) !=
        pdPASS) {
        fatal("worker allocation failed");
        return;
    }
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (!simd_ok) {
        fatal("SIMD selftest core 0 failed");
        return;
    }
    ready = true;
    info();
    Serial.println("READY>");
    escape_start(61000);
    if (xTaskCreatePinnedToCore(ui_task, "escape-display", 6144, (void *)&escape_threshold, 2, NULL,
                                1) != pdPASS) {
        fatal("display task failed");
        return;
    }
}
void loop() {
    static char command[64];
    static size_t used = 0;
    static bool invalid_line = false;
    while (Serial.available()) {
        char ch = Serial.read();
        if (ch == '\n') {
            command[used] = 0;
            used = 0;
            if (invalid_line) {
                invalid_line = false;
                Serial.println("Invalid command line");
                continue;
            }
            float looms[2];
            uint32_t seed;
            if (!strcmp(command, "INFO"))
                info();
            else if (!ready)
                fatal("not ready");
            else if (!strcmp(command, "STOP")) {
                ui_stop();
                ui_status();
            } else if (!strcmp(command, "STATUS"))
                ui_status();
            else if (!strncmp(command, "PLAY", 4)) {
                if (parse_play(command, &seed))
                    escape_start(seed);
                else
                    Serial.println("Use PLAY or PLAY seed");
            } else if (!strcmp(command, "ZERO"))
                zero_command();
            else if (!strncmp(command, "LOOM", 4)) {
                if (parse_loom(command, looms))
                    loom_command(looms);
                else
                    Serial.println("Use LOOM with two 8-digit lowercase hex float32 loom levels in [0, 1]");
            } else if (!strncmp(command, "MODE ", 5)) {
                int value = command[5] - '0';
                if (strlen(command) == 6 && value >= 0 && value <= 8) {
                    ui_stop();
                    has_state = false;
                    if (set_mode(value))
                        info();
                } else
                    Serial.println("Invalid mode");
            } else if (!strcmp(command, "INIT")) {
                ui_stop();
                if (vector_input(state_a)) {
                    has_state = true;
                    Serial.println("{\"event\":\"initialized\"}");
                }
            } else if (!strcmp(command, "STEP")) {
                ui_stop();
                if (!has_state) {
                    fatal("initial state missing");
                    continue;
                }
                if (!vector_input(input_vector))
                    continue;
                int64_t start = esp_timer_get_time();
                int error = run_graph();
                if (error) {
                    fatal("graph update failed");
                    continue;
                }
                memcpy(state_a, state_b, CONNECTOME_N * 4);
                int64_t duration = esp_timer_get_time() - start;
                Serial.printf("{\"event\":\"output\",\"bytes\":%u,\"fnv1a\":\"%08x\",\"compute_"
                              "us\":%lld,\"mode\":%d,\"prep_us\":%llu,\"read_cpu_us\":%llu,"
                              "\"decode_cpu_us\":%llu,\"kernel_cpu_us\":%llu,\"cache_hits\":%u}\n",
                              (unsigned)(CONNECTOME_N * 4), (unsigned)connectome_hash(state_a, CONNECTOME_N * 4),
                              (long long)duration, mode, (unsigned long long)prep_us,
                              (unsigned long long)(profiles[0].read + profiles[1].read),
                              (unsigned long long)(profiles[0].decode + profiles[1].decode),
                              (unsigned long long)(profiles[0].compute + profiles[1].compute),
                              (unsigned)(profiles[0].cache_hits + profiles[1].cache_hits));
                send_output();
            } else
                Serial.println("Commands: INFO STATUS PLAY [seed] STOP ZERO LOOM l r MODE 0..8 INIT STEP");
        } else if (ch != '\r') {
            if (!ch || used == sizeof(command) - 1)
                invalid_line = true;
            else if (!invalid_line)
                command[used++] = ch;
        }
    }
    if (ready && ui_active())
        escape_update();
    delay(1);
}
