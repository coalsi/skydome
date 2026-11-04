// Live stacking for the night sky (DECISIONS.md D-026).
//
// Each camera's new frames are averaged into float planes at the camera's
// full resolution, before stitching: a running mean of the last `depth`
// frames (exactly the mean until it has that many, then an exponential
// average of about that length). Noise falls with the square root of the
// frames stacked, and the floats keep the faint end the stretch lifts.
// Stars drift ~0.25°/min (~8 camera pixels), so a 16 s stack keeps them
// points; longer needs derotating to the stars (alignment, Phase 2).
#pragma once

#include <cuda_runtime.h>
#include <vector>

#include "stitch.h"

class Stacker {
public:
    explicit Stacker(int ncams);
    ~Stacker();
    Stacker(const Stacker&) = delete;
    Stacker& operator=(const Stacker&) = delete;

    // Blend camera c's new frame in (synchronous on `stream`)
    void add(int c, const PlanarImage& frame, int depth, cudaStream_t stream);
    // What the stitch should read for camera c: the frame with its stack
    // attached as float planes, or the frame alone if nothing is stacked yet
    PlanarImage view(int c, const PlanarImage& frame) const;
    int count(int c) const { return cams_[c].n; }
    void reset();   // start every camera's stack again (new settings, new depth)

private:
    struct Cam {
        float* p[3] = {nullptr, nullptr, nullptr};
        int w[3] = {0, 0, 0}, h[3] = {0, 0, 0};
        int n = 0;   // frames in the stack
    };
    std::vector<Cam> cams_;
};
