// A raw 16-bit Bayer frame from the ZWO camera -> the planar YUV the stitch
// reads (DECISIONS.md D-029): bilinear demosaic, white balance, display
// gamma, full-range BT.601 like the JPEG decoder gives. Y at full
// resolution, U and V at half, as floats in 0..255 (the stitch and the
// stack read these, so the sensor's 12 bits reach the night stretch) and
// as 8-bit copies.
#pragma once

#include <cstdint>
#include <cuda_runtime.h>

struct DebayerParams {
    int rx = 0, ry = 0;    // where red sits in each 2x2 cell (column, row)
    float wb_r = 1.f;      // red and blue gains against green
    float wb_b = 1.f;
    float gamma = 1.f;     // 1 = linear (night, the stretch lifts it); ~2.2 for daylight
};

// ZWO's ASI_BAYER_PATTERN (RG, BG, GR, GB) -> red's position in the cell
inline void bayer_red(int pattern, int& rx, int& ry) {
    static const int pos[4][2] = {{0, 0}, {1, 1}, {1, 0}, {0, 1}};
    rx = pos[pattern & 3][0];
    ry = pos[pattern & 3][1];
}

// raw: w x h (both even), pitch in pixels. y/y8: w x h; u, v, u8, v8: w/2 x h/2,
// all tightly packed. Asynchronous on `stream`.
void debayer_to_yuv(const uint16_t* raw, int w, int h, int pitch, const DebayerParams& p, float* y, float* u,
                    float* v, uint8_t* y8, uint8_t* u8, uint8_t* v8, cudaStream_t stream);
