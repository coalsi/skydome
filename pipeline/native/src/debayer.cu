#include "debayer.h"

#include "stitch.h"   // cuda_check

namespace {

__device__ __forceinline__ float px(const uint16_t* raw, int pitch, int w, int h, int x, int y) {
    x = min(max(x, 0), w - 1);
    y = min(max(y, 0), h - 1);
    return __ldg(raw + static_cast<size_t>(y) * pitch + x);
}

// Bilinear demosaic at one pixel: the colour it has, the others averaged from its neighbours
__device__ float3 rgb_at(const uint16_t* raw, int pitch, int w, int h, int x, int y, int rx, int ry) {
    const bool xr = (x & 1) == rx, yr = (y & 1) == ry;
    const float c = px(raw, pitch, w, h, x, y);
    const float left = px(raw, pitch, w, h, x - 1, y), right = px(raw, pitch, w, h, x + 1, y);
    const float up = px(raw, pitch, w, h, x, y - 1), down = px(raw, pitch, w, h, x, y + 1);
    if (xr == yr) {   // red or blue: green is all around it, the other colour on the diagonals
        const float cross = 0.25f * (left + right + up + down);
        const float diag = 0.25f * (px(raw, pitch, w, h, x - 1, y - 1) + px(raw, pitch, w, h, x + 1, y - 1) +
                                    px(raw, pitch, w, h, x - 1, y + 1) + px(raw, pitch, w, h, x + 1, y + 1));
        return xr ? make_float3(c, cross, diag) : make_float3(diag, cross, c);
    }
    const float horiz = 0.5f * (left + right), vert = 0.5f * (up + down);
    // Green on a red row has red beside it and blue above and below; on a blue row the other way
    return yr ? make_float3(horiz, c, vert) : make_float3(vert, c, horiz);
}

__device__ __forceinline__ float tone(float c, float inv_gamma) {
    c = fminf(fmaxf(c, 0.f), 1.f);
    return 255.f * (inv_gamma == 1.f ? c : __powf(c, inv_gamma));
}

__device__ __forceinline__ uint8_t to8(float v) {
    return static_cast<uint8_t>(fminf(fmaxf(v + 0.5f, 0.f), 255.f));
}

// One thread per 2x2 cell: four Y values and the cell's U and V
__global__ void debayer_kernel(const uint16_t* raw, int w, int h, int pitch, DebayerParams p, float inv_gamma,
                               float* yp, float* up, float* vp, uint8_t* y8, uint8_t* u8, uint8_t* v8) {
    const int cx = blockIdx.x * blockDim.x + threadIdx.x;
    const int cy = blockIdx.y * blockDim.y + threadIdx.y;
    const int cw = w / 2, ch = h / 2;
    if (cx >= cw || cy >= ch) return;
    constexpr float kNorm = 1.f / 65535.f;   // ZWO's RAW16 fills 16 bits whatever the ADC depth
    float sr = 0.f, sg = 0.f, sb = 0.f;
    for (int dy = 0; dy < 2; ++dy) {
        for (int dx = 0; dx < 2; ++dx) {
            const int x = 2 * cx + dx, y = 2 * cy + dy;
            const float3 c = rgb_at(raw, pitch, w, h, x, y, p.rx, p.ry);
            const float r = tone(c.x * kNorm * p.wb_r, inv_gamma);
            const float g = tone(c.y * kNorm, inv_gamma);
            const float b = tone(c.z * kNorm * p.wb_b, inv_gamma);
            const float yy = 0.299f * r + 0.587f * g + 0.114f * b;
            const size_t i = static_cast<size_t>(y) * w + x;
            yp[i] = yy;
            y8[i] = to8(yy);
            sr += r;
            sg += g;
            sb += b;
        }
    }
    sr *= 0.25f;
    sg *= 0.25f;
    sb *= 0.25f;
    const float uu = 128.f - 0.168736f * sr - 0.331264f * sg + 0.5f * sb;
    const float vv = 128.f + 0.5f * sr - 0.418688f * sg - 0.081312f * sb;
    const size_t j = static_cast<size_t>(cy) * cw + cx;
    up[j] = uu;
    vp[j] = vv;
    u8[j] = to8(uu);
    v8[j] = to8(vv);
}

}  // namespace

void debayer_to_yuv(const uint16_t* raw, int w, int h, int pitch, const DebayerParams& p, float* y, float* u,
                    float* v, uint8_t* y8, uint8_t* u8, uint8_t* v8, cudaStream_t stream) {
    const dim3 block(32, 8);
    const dim3 grid((w / 2 + block.x - 1) / block.x, (h / 2 + block.y - 1) / block.y);
    const float inv_gamma = p.gamma > 0.f ? 1.f / p.gamma : 1.f;
    debayer_kernel<<<grid, block, 0, stream>>>(raw, w, h, pitch, p, inv_gamma, y, u, v, y8, u8, v8);
    cuda_check(cudaGetLastError(), "debayer kernel launch");
}
