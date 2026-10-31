// GPU remap + feathered blend of up to 8 planar YUV cameras into one I420
// dome (DECISIONS.md D-016). Same arithmetic as gpu_lut.grid_stitch.
#pragma once

#include <cstdint>
#include <cuda_runtime.h>

#include "lut.h"

// One camera frame in GPU-visible memory: 3 planes (Y, U, V), any chroma
// subsampling (Y42B from the MJPEG decoder, I420 from nvvidconv).
// plane[0] == nullptr means the camera has no frame: it's left out and the
// others are renormalised, so the dome keeps running on fewer cameras.
struct PlanarImage {
    const uint8_t* plane[3] = {nullptr, nullptr, nullptr};
    int pitch[3] = {0, 0, 0};
    int width[3] = {0, 0, 0};
    int height[3] = {0, 0, 0};
};

// I420 output: Y is size x size, U and V are size/2 x size/2, tightly packed.
struct I420Out {
    uint8_t* y = nullptr;
    uint8_t* u = nullptr;
    uint8_t* v = nullptr;
};

class GpuStitcher {
public:
    explicit GpuStitcher(const GridLut& lut);
    ~GpuStitcher();
    GpuStitcher(const GpuStitcher&) = delete;
    GpuStitcher& operator=(const GpuStitcher&) = delete;

    int size() const { return size_; }
    int camera_count() const { return ncams_; }

    // inputs: one per table camera, in table order. Outside the dome circle,
    // and where no live camera sees the sky, the output is black.
    void run(const PlanarImage* inputs, const I420Out& out, cudaStream_t stream) const;

private:
    int size_, step_, nodes_, ncams_;
    int cam_w_[8], cam_h_[8];
    float feather_[8];
    float* d_u_ = nullptr;
    float* d_v_ = nullptr;
    uint8_t* d_mask_ = nullptr;
};

// Throws std::runtime_error with the CUDA message on failure
void cuda_check(cudaError_t e, const char* what);
