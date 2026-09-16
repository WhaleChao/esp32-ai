/* ESP32-S3 signed int16 dot product for the Q29 graph executor.
 *
 * C interface (use extern "C" from C++):
 *   int64_t connectome_dot_s16(const int16_t *a, const int16_t *b, uint32_t n);
 *
 * Both arrays must be 16-byte aligned and contain n elements. n must be a
 * multiple of eight in [0, 128]; the caller pads a short chunk with zeros.
 * These preconditions are checked by the caller, not by this kernel.
 *
 * The 40-bit accumulator fits all 128 signed int16 products, including the
 * extreme inputs. The result is exact, with no rounding or saturation.
 * q0, q1 and ACCX are scratch registers. A zero count returns zero without
 * loading either array. Keep this function in IRAM.
 */

.section .iram1,"ax"
.align 4
.global connectome_dot_s16
.type connectome_dot_s16,@function
connectome_dot_s16:
    entry a1,16

    /* Clear both parts of the accumulator before every dot product. */
    movi a8,0
    wur.accx_0 a8
    wur.accx_1 a8

    /* Each iteration loads eight values from each array and advances 16 B. */
    srli a4,a4,3
    loopnez a4,.Lconnectome_dot_end
    ee.vld.128.ip q0,a2,16
    ee.vld.128.ip q1,a3,16
    ee.vmulas.s16.accx q0,q1
.Lconnectome_dot_end:
    /* Windowed ABI: low 32 bits in a2; sign-extended upper 8 bits in a3. */
    rur.accx_0 a2
    rur.accx_1 a3
    sext a3,a3,7
    retw.n
.size connectome_dot_s16,.-connectome_dot_s16
