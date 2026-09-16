#ifndef CONNECTOME_SIMD_TEST_H
#define CONNECTOME_SIMD_TEST_H
#include <stdint.h>

// Run the same fixtures against a real Xtensa dot kernel or a host test hook.
// Returns zero on any mismatch. Boot invokes this separately on both cores.
static inline int connectome_dot_selftest(int64_t (*dot)(const int16_t *, const int16_t *, uint32_t)) {
    if (!dot)
        return 0;
    int16_t a[128] __attribute__((aligned(16)));
    int16_t b[128] __attribute__((aligned(16)));
    uint32_t rng = 0x81f47231;
    for (unsigned test = 0; test < 64; test++) {
        for (unsigned i = 0; i < 128; i++) {
            rng = rng * 1664525u + 1013904223u;
            a[i] = (int16_t)(rng >> 16);
            rng = rng * 1664525u + 1013904223u;
            b[i] = (int16_t)(rng >> 16);
            if (test == 0)
                a[i] = b[i] = 32767;
            if (test == 1) {
                a[i] = -32768;
                b[i] = 32767;
            }
            if (test == 2)
                a[i] = b[i] = -32768;
        }
        for (unsigned n = 0; n <= 128; n += 8) {
            int64_t expected = 0;
            for (unsigned i = 0; i < n; i++)
                expected += (int32_t)a[i] * b[i];
            if (dot(a, b, n) != expected)
                return 0;
        }
    }
    return 1;
}
#endif
