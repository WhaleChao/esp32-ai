// Host-only ctypes bridge. Compile with -O3 -ffp-contract=off -shared -fPIC.
// Owns state/workspaces; borrows the immutable, verified graph byte buffer.
#include "../connectome_fast.h"
#include <stdlib.h>

typedef struct {
    FastGraph graph;
    FastWorkspace workspace;
    float *state, *next;
    int32_t *quantized;
    uint8_t *cache;
    int staged;
} FlyModel;

static int read_graph(void *context, uint32_t offset, void *dst, size_t bytes) {
    FlyModel *m = context;
    if ((uint64_t)offset + bytes > m->graph.model.bytes)
        return 0;
    memcpy(dst, m->graph.mapped + offset, bytes);
    return 1;
}
void fly_destroy(FlyModel *m) {
    if (!m)
        return;
    free(m->graph.rows);
    free(m->graph.blocks);
    free(m->workspace.stream.raw);
    free(m->workspace.stream.compressed);
    free(m->workspace.stream.degrees);
    free(m->state);
    free(m->next);
    free(m->quantized);
    free(m->cache);
    free(m);
}
FlyModel *fly_create(const uint8_t *graph, size_t bytes) {
    if (!graph || bytes < 20 || bytes > 0xDE0000 || memcmp(graph, "FCL1", 4) ||
        connectome_u32(graph + 4) != 1 || connectome_u32(graph + 8) != 48311 ||
        connectome_u32(graph + 16) != 32)
        return NULL;
    uint32_t n = 48311, block_rows = 32, max_raw = 0, max_compressed = 0;
    size_t offset = 20 + n * 4;
    uint32_t rows = 0;
    while (rows < n) {
        if (offset + 16 > bytes)
            return NULL;
        uint32_t count = connectome_u32(graph + offset), raw = connectome_u32(graph + offset + 8),
                 packed = connectome_u32(graph + offset + 12);
        if (!count || count > block_rows || count > n - rows || !raw || raw > 16 * 1024 * 1024 ||
            !packed || offset + 16 + (uint64_t)packed > bytes)
            return NULL;
        if (raw > max_raw)
            max_raw = raw;
        if (packed > max_compressed)
            max_compressed = packed;
        rows += count;
        offset += 16 + packed;
    }
    if (offset != bytes)
        return NULL;
    FlyModel *m = calloc(1, sizeof(*m));
    if (!m)
        return NULL;
    m->graph.model = (ConnectomeModel){n,
                                    connectome_u32(graph + 12),
                                    (uint32_t)bytes,
                                    block_rows,
                                    max_raw,
                                    max_compressed,
                                    read_graph,
                                    NULL,
                                    NULL,
                                    m,
                                    1};
    m->graph.mapped = graph;
    m->graph.use_mapping = 1;
    m->graph.rows = calloc(n, sizeof(FastRow));
    m->graph.blocks = calloc((n + 31) / 32, sizeof(FastBlock));
    m->workspace.stream.raw = malloc(max_raw);
    m->workspace.stream.compressed = malloc(max_compressed);
    m->workspace.stream.degrees = calloc(block_rows, 4);
    m->state = calloc(n, 4);
    m->next = calloc(n, 4);
    m->quantized = calloc(n, 4);
    m->cache = aligned_alloc(16, 6 * 1024 * 1024);
    if (!m->graph.rows || !m->graph.blocks || !m->workspace.stream.raw ||
        !m->workspace.stream.compressed || !m->workspace.stream.degrees || !m->state || !m->next ||
        !m->quantized || !m->cache)
        goto fail;
    if (fast_prepare(&m->graph, &m->workspace, m->cache, 6 * 1024 * 1024, 0))
        goto fail;
    return m;
fail:
    fly_destroy(m);
    return NULL;
}
int fly_reset(FlyModel *m, const float *state) {
    if (!m)
        return 1;
    uint32_t n = m->graph.model.n;
    if (state) {
        for (uint32_t i = 0; i < n; i++)
            if (!isfinite(state[i]) || fabsf(state[i]) > 1)
                return 2;
        memcpy(m->state, state, n * 4);
    } else
        memset(m->state, 0, n * 4);
    memset(m->next, 0, n * 4);
    return 0;
}
// 0: scalar stream; 1/2: cached FP32/Q29; 3/4: packed-cache FP32/Q29.
int fly_step(FlyModel *m, const float *input, float *output, int mode) {
    if (!m || !input || !output || mode < 0 || mode > 4)
        return 1;
    uint32_t n = m->graph.model.n;
    for (uint32_t i = 0; i < n; i++)
        if (!isfinite(input[i]))
            return 2;
    int error = 0;
    if (mode == 0)
        error = connectome_step(&m->graph.model, &m->workspace.stream, m->state, input, m->next);
    else {
        int staged = mode >= 3;
        if (staged != m->staged) {
            error = fast_prepare(&m->graph, &m->workspace, m->cache, 6 * 1024 * 1024, staged);
            if (error)
                return error;
            m->staged = staged;
        }
        int q = (mode == 2 || mode == 4);
        if (q && fast_quantize(m->state, m->quantized, n))
            return 3;
        FastProfile profile;
        error = fast_range(&m->graph, &m->workspace, 0, m->graph.split, m->state, input, m->next,
                           m->state, q ? m->quantized : NULL, fast_dot_scalar, 1, NULL, &profile);
        if (!error)
            error = fast_range(&m->graph, &m->workspace, m->graph.split, m->graph.count, m->state,
                               input, m->next, m->state, q ? m->quantized : NULL, fast_dot_scalar,
                               1, NULL, &profile);
    }
    if (error)
        return error;
    if (output != m->next)
        memcpy(output, m->next, n * 4);
    memcpy(m->state, m->next, n * 4);
    return 0;
}
