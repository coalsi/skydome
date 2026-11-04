// Brightness and colour matching between cameras (DECISIONS.md D-022).
//
// Each camera runs its own auto exposure and white balance, so neighbours
// disagree where they overlap. PhotoBalance compares them there, in the live
// frames, and finds one gain (brightness) and one colour offset per camera
// that make the overlaps agree; GpuStitcher applies them. The dome's overall
// brightness stays where the cameras put it (the corrections are as small as
// they can be, so they average out to none).
#pragma once

#include <cuda_runtime.h>
#include <vector>

#include "lut.h"
#include "stitch.h"

class PhotoBalance {
public:
    explicit PhotoBalance(const GridLut& lut);
    ~PhotoBalance();
    PhotoBalance(const PhotoBalance&) = delete;
    PhotoBalance& operator=(const PhotoBalance&) = delete;

    // Measure the overlaps in these frames (one GPU pass, synchronous) and
    // move each correction `step` of the way (0..1] to the new solution.
    // Cameras without a frame keep their correction. Returns false, changing
    // nothing, when too little of the overlap was usable (night, fog, cap on).
    bool update(const PlanarImage* inputs, cudaStream_t stream, float step);

    const std::vector<CamCorrection>& corrections() const { return corr_; }
    int samples() const { return nsamples_; }   // overlap points in the table
    int used() const { return used_; }           // of those, usable last update

    struct Sample;   // one overlap point: two cameras and where each sees it

private:
    int ncams_, npairs_, nsamples_ = 0, used_ = 0;
    int cam_w_[8], cam_h_[8];
    int pair_a_[28], pair_b_[28];
    Sample* d_samples_ = nullptr;
    float* d_acc_ = nullptr;
    std::vector<CamCorrection> corr_;
};
