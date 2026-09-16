#ifndef LZ4_BLOCK_FAST_H
#define LZ4_BLOCK_FAST_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

// The ESP32 caller defines FAST_CODE as IRAM_ATTR before including this file.
// Keep a separate function so that attribute applies to the decoder itself.
#ifndef FAST_CODE
#define FAST_CODE
#endif

// Decode one raw LZ4 graph block with the same bounds and return convention as
// lz4_block. Source and destination must be separate buffers; capacity is the
// exact decoded size. Discard any partial output on failure. No allocation or
// external dictionary is used. GCC/Clang attributes match the ESP32 toolchain.
static FAST_CODE __attribute__((noinline)) int lz4_block_fast(
    const uint8_t *src, size_t length, uint8_t *dst, size_t capacity) {
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

        if (offset == 1) {
            // A run of one repeated byte needs no forward-copy dependency.
            memset(dst + output, dst[output - 1], match);
            output += match;
        } else if (offset >= match && match >= 16) {
            // The entire source and destination ranges are disjoint.
            memcpy(dst + output, dst + output - offset, match);
            output += match;
        } else {
            // With offset >= 16 each individual copy is disjoint, even when
            // the full match overlaps. Later chunks can read earlier output.
            while (offset >= 16 && match >= 16) {
                memcpy(dst + output, dst + output - offset, 16);
                output += 16;
                match -= 16;
            }
            // Short offsets and the remaining tail require forward copying.
            while (match--) {
                dst[output] = dst[output - offset];
                output++;
            }
        }
    }
    return 0;
}

#endif
