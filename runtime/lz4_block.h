#ifndef LZ4_BLOCK_H
#define LZ4_BLOCK_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Decode one raw LZ4 graph block, without a frame header or external dictionary.
// capacity is the exact expected decoded size, not just the buffer allocation.
// Source and destination must be separate buffers of length and capacity bytes.
// Returns 1 on an exact-size decode, or 0 on failure. A failed decode may have
// written a partial result; callers must discard it. No allocation is performed.
static int lz4_block(const uint8_t *src, size_t length,
                       uint8_t *dst, size_t capacity) {
    if (!src || !dst) {
        return 0;
    }
    size_t input = 0, output = 0;
    while (input < length) {
        uint8_t token = src[input++];
        size_t literals = token >> 4;
        if (literals == 15) {
            uint8_t extension;
            do {
                if (input == length) {
                    return 0;
                }
                extension = src[input++];
                if (literals > capacity - output ||
                    extension > capacity - output - literals) {
                    return 0;
                }
                literals += extension;
            } while (extension == 255);
        }
        if (literals > length - input || literals > capacity - output) {
            return 0;
        }
        memcpy(dst + output, src + input, literals);
        input += literals;
        output += literals;
        if (input == length) {
            return output == capacity;
        }

        if (length - input < 2) {
            return 0;
        }
        size_t offset = (size_t)src[input] | ((size_t)src[input + 1] << 8);
        input += 2;
        if (!offset || offset > output) {
            return 0;
        }
        size_t match = (token & 15) + 4;
        if ((token & 15) == 15) {
            uint8_t extension;
            do {
                if (input == length) {
                    return 0;
                }
                extension = src[input++];
                if (match > capacity - output ||
                    extension > capacity - output - match) {
                    return 0;
                }
                match += extension;
            } while (extension == 255);
        }
        if (match > capacity - output) {
            return 0;
        }

        // A match can refer to bytes emitted earlier in the same match. Copy
        // forwards byte by byte; memcpy or memmove would change that behavior.
        for (size_t i = 0; i < match; i++) {
            dst[output] = dst[output - offset];
            output++;
        }
    }
    return 0;
}

#endif
