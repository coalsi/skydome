// Bilinear sample of one 8-bit plane, shared by the stitch and balance kernels
#pragma once

#include <cstdint>

__device__ __forceinline__ float sample(const uint8_t* p, int pitch, int w, int h, float x, float y) {
    // x, y are inside [0, w-1] x [0, h-1]; clamp the 2x2 footprint at the border
    int x0 = min(static_cast<int>(x), w - 1);
    int y0 = min(static_cast<int>(y), h - 1);
    int x1 = min(x0 + 1, w - 1);
    int y1 = min(y0 + 1, h - 1);
    float fx = x - x0, fy = y - y0;
    const uint8_t* r0 = p + static_cast<size_t>(y0) * pitch;
    const uint8_t* r1 = p + static_cast<size_t>(y1) * pitch;
    float top = __ldg(r0 + x0) * (1.f - fx) + __ldg(r0 + x1) * fx;
    float bot = __ldg(r1 + x0) * (1.f - fx) + __ldg(r1 + x1) * fx;
    return top * (1.f - fy) + bot * fy;
}

// The same on a float plane (a stacked camera, stack.h); pitch in elements
__device__ __forceinline__ float sample_f(const float* p, int pitch, int w, int h, float x, float y) {
    int x0 = min(static_cast<int>(x), w - 1);
    int y0 = min(static_cast<int>(y), h - 1);
    int x1 = min(x0 + 1, w - 1);
    int y1 = min(y0 + 1, h - 1);
    float fx = x - x0, fy = y - y0;
    const float* r0 = p + static_cast<size_t>(y0) * pitch;
    const float* r1 = p + static_cast<size_t>(y1) * pitch;
    float top = __ldg(r0 + x0) * (1.f - fx) + __ldg(r0 + x1) * fx;
    float bot = __ldg(r1 + x0) * (1.f - fx) + __ldg(r1 + x1) * fx;
    return top * (1.f - fy) + bot * fy;
}
