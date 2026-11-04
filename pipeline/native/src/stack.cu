#include "stack.h"

#include <algorithm>

namespace {

// acc += alpha * (frame - acc); alpha = 1 copies (the first frame). The
// frame is 8-bit from the UVC cameras, float from the ZWO camera (its 12 bits)
template <typename T>
__global__ void blend_kernel(float* acc, int w, int h, const T* src, int spitch, float alpha) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= w || y >= h) return;
    float& a = acc[static_cast<size_t>(y) * w + x];
    const float s = __ldg(src + static_cast<size_t>(y) * spitch + x);
    a = alpha >= 1.f ? s : a + alpha * (s - a);
}

}  // namespace

Stacker::Stacker(int ncams) : cams_(ncams) {}

Stacker::~Stacker() {
    for (Cam& c : cams_)
        for (float* p : c.p) cudaFree(p);
}

void Stacker::reset() {
    for (Cam& c : cams_) c.n = 0;
}

void Stacker::add(int ci, const PlanarImage& frame, int depth, cudaStream_t stream) {
    Cam& c = cams_[ci];
    for (int k = 0; k < 3; ++k) {
        if (c.w[k] != frame.width[k] || c.h[k] != frame.height[k]) {   // first frame, or a new camera mode
            cudaFree(c.p[k]);
            cuda_check(cudaMalloc(&c.p[k], sizeof(float) * frame.width[k] * frame.height[k]), "cudaMalloc stack");
            c.w[k] = frame.width[k];
            c.h[k] = frame.height[k];
            c.n = 0;
        }
    }
    // The exact mean of what's there until `depth` frames, then a running average of about that many
    const float alpha = 1.f / static_cast<float>(std::min(c.n + 1, std::max(depth, 1)));
    const dim3 block(32, 8);
    for (int k = 0; k < 3; ++k) {
        const dim3 grid((c.w[k] + block.x - 1) / block.x, (c.h[k] + block.y - 1) / block.y);
        if (frame.fplane[k])
            blend_kernel<<<grid, block, 0, stream>>>(c.p[k], c.w[k], c.h[k], frame.fplane[k], frame.fpitch[k], alpha);
        else
            blend_kernel<<<grid, block, 0, stream>>>(c.p[k], c.w[k], c.h[k], frame.plane[k], frame.pitch[k], alpha);
    }
    cuda_check(cudaGetLastError(), "stack kernel launch");
    cuda_check(cudaStreamSynchronize(stream), "stack");   // the camera's frame is released at its next poll
    ++c.n;
}

PlanarImage Stacker::view(int ci, const PlanarImage& frame) const {
    const Cam& c = cams_[ci];
    PlanarImage v = frame;
    if (c.n == 0 || c.w[0] != frame.width[0] || c.h[0] != frame.height[0]) return v;
    for (int k = 0; k < 3; ++k) {
        v.fplane[k] = c.p[k];
        v.fpitch[k] = c.w[k];
    }
    return v;
}
