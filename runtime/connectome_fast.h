#ifndef CONNECTOME_FAST_H
#define CONNECTOME_FAST_H

#include "connectome_stream.h"
#include "lz4_block_fast.h"

#define FAST_CHUNK 128

typedef struct {
    uint32_t indices, weights; // Byte offsets within a decoded varint block.
    uint16_t degree, reserved;
    float scale;
} FastRow;

typedef struct {
    uint32_t offset, raw, compressed, row, rows, edges, staged;
    uint8_t *cache;
} FastBlock;

// Caller owns all storage. rows has model.n entries; blocks has room for
// ceil(model.n / model.block_rows) entries. mapped, when used, spans model.bytes
// and must represent the same immutable image returned by model.read.
typedef struct {
    ConnectomeModel model;
    FastRow *rows;
    FastBlock *blocks;
    uint32_t count, split;
    const uint8_t *mapped;
    int use_mapping;
} FastGraph;

// A dot hook sums signed int16 products exactly into int64. Its inputs are
// 16-byte aligned; count is a multiple of 8 and never exceeds FAST_CHUNK.
typedef int64_t (*FastDot)(const int16_t *, const int16_t *, uint32_t);
typedef uint64_t (*FastClock)(void);

typedef struct {
    uint64_t read, decode, compute; // Clock units supplied by the caller.
    uint32_t blocks, cache_hits;
} FastProfile;

// Each worker needs its own workspace, decoded buffers and profile. Prepared
// metadata, cache and input state may be shared between disjoint block ranges.
typedef struct {
    ConnectomeWorkspace stream;
    int16_t weights[FAST_CHUNK] __attribute__((aligned(16)));
    int16_t high[FAST_CHUNK] __attribute__((aligned(16)));
    int16_t low[FAST_CHUNK] __attribute__((aligned(16)));
} FastWorkspace;

// Internal fast path, only after fast_prepare validates the immutable stream.
// Index deltas and contact counts occupy at most three bytes.
static inline uint32_t fast_value(const uint8_t **cursor) {
    const uint8_t *data = *cursor;
    uint32_t value = *data++;
    if (value & 128) {
        value = (value & 127) | ((uint32_t)(*data & 127) << 7);
        if (*data++ & 128) {
            value |= (uint32_t)*data++ << 14;
        }
    }
    *cursor = data;
    return value;
}

// Validate before using unchecked inner loops, then build row/block metadata.
// arena is an optional 16-byte-aligned cache, borrowed for the graph's lifetime.
// Capacity zero disables caching. staged selects exact uint32 index/weight
// pairs instead of decoded varint bytes. Neither representation drops edges or
// changes integer contact counts. Failure leaves the graph unusable.
// Returns 0 on success; nonzero values identify preparation failures.
static inline int fast_prepare(FastGraph *graph, FastWorkspace *workspace,
                               uint8_t *arena, size_t capacity, int staged) {
    if (!graph || !workspace || !graph->rows || !graph->blocks ||
        capacity > UINT32_MAX || (capacity && (!arena || ((uintptr_t)arena & 15)))) {
        return 11;
    }
    ConnectomeModel *model = &graph->model;
    if (!model->lz4 || model->n > 65535 || model->block_rows > 64) {
        return 1;
    }
    if (connectome_step(model, &workspace->stream, NULL, NULL, NULL)) {
        return 2;
    }

    uint64_t raw_total = 0, raw_seen = 0;
    for (uint32_t scan = 20 + model->n * 4; scan < model->bytes;) {
        uint8_t header[16];
        if (!model->read(model->context, scan, header, 16)) {
            return 3;
        }
        raw_total += staged ? (uint64_t)connectome_u32(header + 4) * 4
                            : connectome_u32(header + 8);
        scan += 16 + connectome_u32(header + 12);
    }
    if (capacity && raw_total > UINT64_MAX / capacity) {
        return 11;
    }

    uint32_t offset = 20 + model->n * 4, row = 0;
    graph->count = 0;
    uint64_t total_edges = 0;
    size_t used = 0;
    while (row < model->n) {
        uint8_t header[16];
        if (!model->read(model->context, offset, header, 16)) {
            return 3;
        }
        if (graph->count >= (model->n + model->block_rows - 1) / model->block_rows) {
            return 10;
        }
        FastBlock *block = &graph->blocks[graph->count++];
        block->staged = staged;
        block->row = row;
        block->rows = connectome_u32(header);
        block->edges = connectome_u32(header + 4);
        block->raw = connectome_u32(header + 8);
        block->compressed = connectome_u32(header + 12);
        block->offset = offset + 16;
        block->cache = NULL;
        if (!model->read(model->context, block->offset,
                         workspace->stream.compressed, block->compressed) ||
            !lz4_block(workspace->stream.compressed, block->compressed,
                         workspace->stream.raw, block->raw)) {
            return 4;
        }

        const uint8_t *raw = workspace->stream.raw;
        const uint8_t *cursor = raw, *end = raw + block->raw;
        for (uint32_t r = 0; r < block->rows; r++) {
            uint32_t degree;
            if (!connectome_varint_read(&cursor, end, &degree) || degree > 65535) {
                return 5;
            }
            graph->rows[row + r].degree = (uint16_t)degree;
        }
        for (uint32_t r = 0; r < block->rows; r++) {
            FastRow *meta = &graph->rows[row + r];
            meta->indices = (uint32_t)(cursor - raw);
            uint32_t index = 0;
            for (uint32_t j = 0; j < meta->degree; j++) {
                const uint8_t *start = cursor;
                uint32_t delta;
                if (!connectome_varint_read(&cursor, end, &delta) ||
                    cursor - start > 3 || delta > 65535 ||
                    (j && !delta) || index + delta >= model->n) {
                    return 6;
                }
                index += delta;
            }
        }
        for (uint32_t r = 0; r < block->rows; r++) {
            FastRow *meta = &graph->rows[row + r];
            meta->weights = (uint32_t)(cursor - raw);
            uint32_t sum = 0;
            for (uint32_t j = 0; j < meta->degree; j++) {
                const uint8_t *start = cursor;
                uint32_t weight;
                if (!connectome_varint_read(&cursor, end, &weight) ||
                    cursor - start > 3 || !weight || weight > 32767 ||
                    sum > 16777216 - weight) {
                    return 7;
                }
                sum += weight;
            }
            meta->scale = 0.7f / (float)(sum ? sum : 1);
        }
        if (cursor != end) {
            return 8;
        }

        // Distribute whole cached blocks along the graph using a proportional
        // budget. Their placement is independent of runtime mode.
        size_t bytes = staged ? (size_t)block->edges * 4 : block->raw;
        size_t aligned = (bytes + 15u) & ~(size_t)15u;
        raw_seen += bytes;
        size_t allowed = raw_total ? (size_t)((uint64_t)capacity * raw_seen / raw_total) : 0;
        if (bytes && used <= allowed && aligned <= allowed - used) {
            block->cache = arena + used;
            used += aligned;
            if (!staged) {
                memcpy(block->cache, raw, block->raw);
            } else {
                uint32_t *dst = (uint32_t *)block->cache;
                for (uint32_t r = 0; r < block->rows; r++) {
                    const FastRow *meta = &graph->rows[row + r];
                    const uint8_t *indices = raw + meta->indices;
                    const uint8_t *weights = raw + meta->weights;
                    uint32_t index = 0;
                    for (uint32_t j = 0; j < meta->degree; j++) {
                        index += fast_value(&indices);
                        *dst++ = index | (fast_value(&weights) << 16);
                    }
                }
            }
        }
        row += block->rows;
        offset = block->offset + block->compressed;
        total_edges += block->edges;
        if (model->yield) {
            model->yield(model->context);
        }
    }
    if (offset != model->bytes || total_edges != model->edges) {
        return 9;
    }
    // A scheduling hint: the split balances edge counts, not measured duration.
    uint64_t running = 0;
    graph->split = 0;
    while (graph->split < graph->count && running < total_edges / 2) {
        running += graph->blocks[graph->split++].edges;
    }
    return 0;
}

// Q29 quantizes neural state only. Contact counts stay exact. Use the default
// round-to-nearest floating-point environment, as in the firmware and host gold.
static inline int fast_quantize(const float *state, int32_t *quantized, uint32_t n) {
    if (!state || !quantized) {
        return 1;
    }
    for (uint32_t i = 0; i < n; i++) {
        if (!isfinite(state[i]) || fabsf(state[i]) > 1.f) {
            return 1;
        }
        quantized[i] = (int32_t)lrintf(state[i] * 536870912.f);
    }
    return 0;
}

static inline int64_t fast_dot_scalar(const int16_t *a, const int16_t *b, uint32_t n) {
    int64_t sum = 0;
    for (uint32_t i = 0; i < n; i++) {
        sum += (int32_t)a[i] * b[i];
    }
    return sum;
}

// Internal kernel. Keep its out-of-line IRAM placement and accumulation order;
// compile without fast-math or floating-point contraction.
__attribute__((noinline)) static FAST_CODE void fast_rows(
    const FastGraph *graph, const FastBlock *block, const uint8_t *raw,
    FastWorkspace *workspace, const float *state, const float *input, float *next,
    const float *hot_float, const int32_t *hot_q, FastDot dot, int staged) {
    const uint32_t *packed = staged ? (const uint32_t *)raw : NULL;
    for (uint32_t row = block->row; row < block->row + block->rows; row++) {
        const FastRow *meta = &graph->rows[row];
        // Varint offsets do not apply to the packed cache representation.
        const uint8_t *indices = staged ? NULL : raw + meta->indices;
        const uint8_t *weights = staged ? NULL : raw + meta->weights;
        uint32_t index = 0;
        float accumulator = 0;
        if (!hot_q) {
            for (uint32_t j = 0; j < meta->degree; j++) {
                uint32_t weight;
                if (staged) {
                    uint32_t pair = *packed++;
                    index = pair & 65535;
                    weight = pair >> 16;
                } else {
                    index += fast_value(&indices);
                    weight = fast_value(&weights);
                }
                accumulator += ((float)weight * meta->scale) * hot_float[index];
            }
        } else {
            int64_t high_sum = 0, low_sum = 0;
            for (uint32_t j = 0; j < meta->degree;) {
                uint32_t count = meta->degree - j;
                if (count > FAST_CHUNK) {
                    count = FAST_CHUNK;
                }
                for (uint32_t k = 0; k < count; k++, j++) {
                    if (staged) {
                        uint32_t pair = *packed++;
                        index = pair & 65535;
                        workspace->weights[k] = (int16_t)(pair >> 16);
                    } else {
                        index += fast_value(&indices);
                        workspace->weights[k] = (int16_t)fast_value(&weights);
                    }
                    int32_t value = hot_q[index];
                    // Arithmetic right shift gives floor division by 32768 on
                    // Xtensa and supported hosts; the remainder is nonnegative.
                    int32_t high = value >> 15;
                    workspace->high[k] = (int16_t)high;
                    workspace->low[k] = (int16_t)(value - high * 32768);
                }
                uint32_t padded = (count + 7) & ~7u;
                for (uint32_t k = count; k < padded; k++) {
                    workspace->weights[k] = workspace->high[k] = workspace->low[k] = 0;
                }
                high_sum += dot(workspace->weights, workspace->high, padded);
                low_sum += dot(workspace->weights, workspace->low, padded);
            }
            accumulator = (float)(high_sum * 32768 + low_sum) * 0x1p-29f * meta->scale;
        }
        next[row] = 0.5f * state[row] + 0.5f * tanhf(accumulator + input[row]);
    }
}

// Execute [begin, end) block indices of a successfully prepared graph.
// state/input/next are n-node vectors; next must not alias any input or cache.
// Supply either hot_float (a copy of state) or hot_q from fast_quantize(state).
// A non-NULL hot_q selects Q29 and requires a dot hook. cached enables cache
// hits; otherwise the same blocks are read and decoded again. No allocation.
// Profile is reset per call. Failure leaves partial output that must be discarded.
// Return codes: 0 success, 1 read failure, 2 decode failure, 3 invalid arguments.
static inline int fast_range(
    FastGraph *graph, FastWorkspace *workspace, uint32_t begin, uint32_t end,
    const float *state, const float *input, float *next,
    const float *hot_float, const int32_t *hot_q, FastDot dot,
    int cached, FastClock clock, FastProfile *profile) {
    if (!graph || !workspace || !profile || !state || !input || !next ||
        begin > end || end > graph->count ||
        (hot_q ? !dot : !hot_float)) {
        return 3;
    }
    memset(profile, 0, sizeof(*profile));
    for (uint32_t i = begin; i < end; i++) {
        FastBlock *block = &graph->blocks[i];
        const uint8_t *raw = block->cache;
        int staged = cached && raw && block->staged;
        if (!cached || !raw) {
            uint64_t start = clock ? clock() : 0;
            const uint8_t *compressed = graph->mapped && graph->use_mapping
                ? graph->mapped + block->offset : NULL;
            if (!compressed) {
                if (!graph->model.read(graph->model.context, block->offset,
                                       workspace->stream.compressed, block->compressed)) {
                    return 1;
                }
                compressed = workspace->stream.compressed;
            }
            if (clock) {
                profile->read += clock() - start;
            }
            start = clock ? clock() : 0;
            if (!lz4_block_fast(compressed, block->compressed,
                                  workspace->stream.raw, block->raw)) {
                return 2;
            }
            if (clock) {
                profile->decode += clock() - start;
            }
            raw = workspace->stream.raw;
        } else {
            profile->cache_hits++;
        }
        uint64_t start = clock ? clock() : 0;
        fast_rows(graph, block, raw, workspace, state, input, next,
                  hot_float, hot_q, dot, staged);
        if (clock) {
            profile->compute += clock() - start;
        }
        profile->blocks++;
        if (graph->model.yield && (i & 15u) == 15u) {
            graph->model.yield(graph->model.context);
        }
    }
    return 0;
}

#endif
