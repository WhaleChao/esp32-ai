#ifndef CONNECTOME_STREAM_H
#define CONNECTOME_STREAM_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "connectome_codec.h"
#include "lz4_block.h"

// Callbacks return nonzero only after reading/decoding the entire requested
// range. They must respect the supplied buffer sizes. context is caller-owned.
typedef int (*ConnectomeRead)(void *context, uint32_t offset, void *dst, size_t bytes);
typedef int (*ConnectomeInflate)(void *context, const uint8_t *src, size_t bytes,
                              uint8_t *dst, size_t decoded_bytes);
typedef void (*ConnectomeYield)(void *context);

typedef struct {
    uint32_t n, edges, bytes, block_rows, max_raw, max_compressed;
    ConnectomeRead read;
    ConnectomeInflate inflate;
    ConnectomeYield yield;
    void *context;
    int lz4; // Nonzero: FCL1 raw LZ4 blocks. Zero: FCZ1 zlib via inflate callback.
} ConnectomeModel;

// Caller allocates max_compressed bytes, max_raw bytes and block_rows uint32s.
// The three buffers must be separate. One workspace is needed per active call.
typedef struct {
    uint8_t *compressed, *raw;
    uint32_t *degrees;
} ConnectomeWorkspace;

static inline uint32_t connectome_u32(const uint8_t *data) {
    return (uint32_t)data[0] | ((uint32_t)data[1] << 8) |
           ((uint32_t)data[2] << 16) | ((uint32_t)data[3] << 24);
}

// FNV-1a is a diagnostic fingerprint, not a cryptographic check.
// Release assets must also be verified against their pinned SHA-256 hashes.
static inline uint32_t connectome_hash_more(uint32_t hash, const void *data,
                                        size_t length) {
    const uint8_t *cursor = (const uint8_t *)data;
    while (length--) {
        hash ^= *cursor++;
        hash *= 16777619u;
    }
    return hash;
}

static inline uint32_t connectome_hash(const void *data, size_t length) {
    return connectome_hash_more(2166136261u, data, length);
}

enum ConnectomeStepResult {
    CONNECTOME_STEP_OK = 0,
    CONNECTOME_STEP_HEADER_READ = 10,
    CONNECTOME_STEP_HEADER = 11,
    CONNECTOME_STEP_BLOCK_READ = 12,
    CONNECTOME_STEP_BLOCK_SIZE = 13,
    CONNECTOME_STEP_PAYLOAD_READ = 14,
    CONNECTOME_STEP_INFLATE = 15,
    CONNECTOME_STEP_EDGE_COUNT = 16,
    CONNECTOME_STEP_FINAL_SIZE = 17,
    CONNECTOME_STEP_ARGUMENT = 18,
    CONNECTOME_STEP_CODEC_BASE = 100
};

// Stream one complete graph without materializing its edge matrix. A model has
// a 20-byte header, n uint32 node-order entries, then blocks with 16-byte headers
// (rows, edges, decoded bytes, compressed bytes), each followed by its payload.
// Node-order entries are metadata: the executor uses the stored row/index order.
// The caller verifies the asset identity and binds matching input/output maps.
//
// state == NULL validates the complete stream without updating neural state.
// Otherwise state, input and next each have n floats; next must not overlap
// state or input. All vectors must be separate from the workspace and model.
// Successful execution returns 0. Codec failures return 100 + ConnectomeDecodeResult.
// On failure, discard any partial next vector. read must see an immutable image
// for the duration of the call. The optional yield callback runs after each block.
static inline int connectome_step(const ConnectomeModel *model, ConnectomeWorkspace *workspace,
                                const float *state, const float *input, float *next) {
    if (!model || !workspace || !model->read || !model->n ||
        !model->block_rows || !model->max_raw || !model->max_compressed ||
        !workspace->compressed || !workspace->raw || !workspace->degrees ||
        (!model->lz4 && !model->inflate) || (state && (!input || !next))) {
        return CONNECTOME_STEP_ARGUMENT;
    }

    uint8_t header[20];
    if (model->bytes < sizeof(header) ||
        !model->read(model->context, 0, header, sizeof(header))) {
        return CONNECTOME_STEP_HEADER_READ;
    }
    if (memcmp(header, model->lz4 ? "FCL1" : "FCZ1", 4) ||
        connectome_u32(header + 4) != 1 || connectome_u32(header + 8) != model->n ||
        connectome_u32(header + 12) != model->edges ||
        connectome_u32(header + 16) != model->block_rows) {
        return CONNECTOME_STEP_HEADER;
    }

    uint64_t offset = 20 + (uint64_t)model->n * 4;
    uint32_t row = 0;
    uint64_t edges = 0;
    while (row < model->n) {
        if (offset + 16 > model->bytes ||
            !model->read(model->context, (uint32_t)offset, header, 16)) {
            return CONNECTOME_STEP_BLOCK_READ;
        }
        uint32_t rows = connectome_u32(header);
        uint32_t block_edges = connectome_u32(header + 4);
        uint32_t raw = connectome_u32(header + 8);
        uint32_t compressed = connectome_u32(header + 12);
        offset += 16;
        if (!rows || rows > model->block_rows || rows > model->n - row ||
            !raw || raw > model->max_raw ||
            !compressed || compressed > model->max_compressed ||
            offset + compressed > model->bytes) {
            return CONNECTOME_STEP_BLOCK_SIZE;
        }
        if (!model->read(model->context, (uint32_t)offset,
                         workspace->compressed, compressed)) {
            return CONNECTOME_STEP_PAYLOAD_READ;
        }
        if (model->lz4) {
            if (!lz4_block(workspace->compressed, compressed, workspace->raw, raw)) {
                return CONNECTOME_STEP_INFLATE;
            }
        } else if (!model->inflate(model->context, workspace->compressed, compressed,
                                    workspace->raw, raw)) {
            return CONNECTOME_STEP_INFLATE;
        }

        int result = connectome_decode_block(
            rows, row, model->n, block_edges, workspace->raw, raw,
            workspace->degrees, NULL, NULL, state,
            state ? input + row : NULL, state ? next + row : NULL);
        if (result) {
            return CONNECTOME_STEP_CODEC_BASE + result;
        }
        offset += compressed;
        row += rows;
        edges += block_edges;
        if (edges > model->edges) {
            return CONNECTOME_STEP_EDGE_COUNT;
        }
        if (model->yield) {
            model->yield(model->context);
        }
    }
    return offset == model->bytes && edges == model->edges
        ? CONNECTOME_STEP_OK : CONNECTOME_STEP_FINAL_SIZE;
}

#endif
