// GPU remap + feathered blend of up to 8 planar YUV cameras into one I420
// dome (DECISIONS.md D-016). Same arithmetic as gpu_lut.grid_stitch.
#pragma once

#include <cstdint>
#include <cuda_runtime.h>
#include <vector>

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

// I420 output: Y is width x height, U and V half that each way, tightly packed.
struct I420Out {
    uint8_t* y = nullptr;
    uint8_t* u = nullptr;
    uint8_t* v = nullptr;
};

// Brightness and colour correction for one camera (PhotoBalance, balance.h).
// Full-range YUV: Y x gain; U, V: 128 + gain x (c - 128) + u / v.
struct CamCorrection {
    float gain = 1.f;
    float u = 0.f;
    float v = 0.f;
};

// Part of the dome canvas rendered at its own resolution: output pixel (x, y)
// is canvas pixel (x0 + x * scale, y0 + y * scale). The whole dome is
// {size, size, 0, 0, 1}; a zoomed view is a small rectangle at scale < 1,
// sampled straight from the cameras (the table is a smooth grid, so the
// finer sampling is real detail, not an enlarged dome).
struct View {
    int width, height;     // output pixels, even
    float x0, y0, scale;
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
    void run_view(const PlanarImage* inputs, const I420Out& out, const View& view, cudaStream_t stream) const;

    // Hide the ground: horizon altitude (degrees) for each degree of azimuth
    // from north through east, 360 values; anything else turns the mask off
    void set_horizon(const std::vector<float>& alt_by_az);

    // Used from the next run() on; one per table camera. Default: none
    void set_corrections(const std::vector<CamCorrection>& corr);

private:
    int size_, step_, nodes_, ncams_;
    int cam_w_[8], cam_h_[8];
    float feather_[8];
    CamCorrection corr_[8];
    float edge_deg_;
    float* d_horizon_ = nullptr;
    bool has_horizon_ = false;
    float* d_u_ = nullptr;
    float* d_v_ = nullptr;
    uint8_t* d_mask_ = nullptr;
};

// Throws std::runtime_error with the CUDA message on failure
void cuda_check(cudaError_t e, const char* what);
