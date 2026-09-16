#ifndef CONNECTOME_CODEC_H
#define CONNECTOME_CODEC_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>

// Read an unsigned 32-bit base-128 varint. cursor and end must belong to the
// same buffer, with cursor <= end. Failure may consume part of the input.
static inline int connectome_varint_read(const uint8_t **cursor,
                                      const uint8_t *end, uint32_t *value) {
    *value = 0;
    for (unsigned shift = 0; shift < 35; shift += 7) {
        if (*cursor == end) {
            return 0;
        }
        unsigned byte = *(*cursor)++;
        if (shift == 28 && (byte & 240)) {
            return 0;
        }
        *value |= (uint32_t)(byte & 127) << shift;
        if (!(byte & 128)) {
            return 1;
        }
    }
    return 0;
}

enum ConnectomeDecodeResult {
    CONNECTOME_DECODE_OK = 0,
    CONNECTOME_DECODE_DEGREE = 1,
    CONNECTOME_DECODE_EDGE_COUNT = 2,
    CONNECTOME_DECODE_INDEX_STREAM = 3,
    CONNECTOME_DECODE_WEIGHT = 4,
    CONNECTOME_DECODE_WEIGHT_SUM = 5,
    CONNECTOME_DECODE_ROW_STREAM = 6,
    CONNECTOME_DECODE_INDEX_RANGE = 7,
    CONNECTOME_DECODE_TRAILING_DATA = 8,
    CONNECTOME_DECODE_ARGUMENT = 9
};

// Decode a graph block after decompression. Its three varint streams contain:
//   1. Incoming edge counts for each row.
//   2. Sorted source-node indices, delta encoded from zero within each row.
//   3. Positive integer contact counts in the same edge order.
// No edges or contact counts are approximated during decoding.
//
// degrees has room for rows values. Optional out_indices and out_weights each
// have room for expected_edges values. All output buffers must be separate
// from the input data. On failure, discard partially written outputs.
//
// With state == NULL this only validates/decodes the block. Otherwise state is
// a full n-node vector; input and next are rows-long slices starting at
// row_start. next must not overlap state, input or the other buffers. The
// optional scalar step uses a fixed FP32 accumulation order:
//   next[r] = 0.5 * state[row_start+r] + 0.5 * tanh(weighted_sum + input[r])
// Each row's contact weights are normalized to sum to 0.7. This step is the
// scalar reference path; the Q29/SIMD executor is implemented separately.
static inline int connectome_decode_block(
    uint32_t rows, uint32_t row_start, uint32_t n, uint32_t expected_edges,
    const uint8_t *data, size_t length, uint32_t *degrees,
    uint32_t *out_indices, uint32_t *out_weights,
    const float *state, const float *input, float *next) {
    if (!rows || !data || !degrees || row_start > n || rows > n - row_start ||
        (state && (!input || !next))) {
        return CONNECTOME_DECODE_ARGUMENT;
    }

    const uint8_t *cursor = data, *end = data + length;
    uint64_t edges = 0;
    for (uint32_t row = 0; row < rows; row++) {
        if (!connectome_varint_read(&cursor, end, &degrees[row])) {
            return CONNECTOME_DECODE_DEGREE;
        }
        edges += degrees[row];
    }
    if (edges != expected_edges) {
        return CONNECTOME_DECODE_EDGE_COUNT;
    }

    // Locate the weight stream without allocating an intermediate edge array.
    const uint8_t *indices = cursor;
    for (uint64_t edge = 0; edge < edges; edge++) {
        uint32_t delta;
        if (!connectome_varint_read(&cursor, end, &delta)) {
            return CONNECTOME_DECODE_INDEX_STREAM;
        }
    }
    const uint8_t *index_end = cursor, *weights = cursor;
    uint32_t edge = 0;
    for (uint32_t row = 0; row < rows; row++) {
        const uint8_t *row_weights = weights;
        uint64_t total = 0;
        for (uint32_t j = 0; j < degrees[row]; j++) {
            uint32_t weight;
            if (!connectome_varint_read(&weights, end, &weight) || !weight) {
                return CONNECTOME_DECODE_WEIGHT;
            }
            total += weight;
        }
        // Integer row totals must be exactly representable in FP32 for the
        // frozen normalization convention used by the independent graph gold.
        if (total > 16777216) {
            return CONNECTOME_DECODE_WEIGHT_SUM;
        }
        float scale = 0.7f / (float)(total ? total : 1);
        float accumulator = 0;
        uint32_t index = 0;
        for (uint32_t j = 0; j < degrees[row]; j++, edge++) {
            uint32_t delta, weight;
            if (!connectome_varint_read(&indices, index_end, &delta) ||
                !connectome_varint_read(&row_weights, weights, &weight)) {
                return CONNECTOME_DECODE_ROW_STREAM;
            }
            if ((j && !delta) || (uint64_t)index + delta >= n) {
                return CONNECTOME_DECODE_INDEX_RANGE;
            }
            index += delta;
            if (out_indices) {
                out_indices[edge] = index;
            }
            if (out_weights) {
                out_weights[edge] = weight;
            }
            if (state) {
                accumulator += ((float)weight * scale) * state[index];
            }
        }
        if (state) {
            next[row] = 0.5f * state[row_start + row] +
                        0.5f * tanhf(accumulator + input[row]);
        }
    }
    return weights == end && indices == index_end
        ? CONNECTOME_DECODE_OK : CONNECTOME_DECODE_TRAILING_DATA;
}

#endif
