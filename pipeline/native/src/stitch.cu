#include "stitch.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>

#include "sample.cuh"

void cuda_check(cudaError_t e, const char* what) {
    if (e != cudaSuccess) throw std::runtime_error(std::string(what) + ": " + cudaGetErrorString(e));
}

namespace {

constexpr int kMaxCams = 8;
// Full-range (JPEG) black, matching the decoder's output
constexpr uint8_t kBlackY = 0;
constexpr uint8_t kBlackC = 128;

struct CamPlanes {
    const uint8_t* a;   // the plane sampled (Y, or U for the chroma pass)
    const uint8_t* b;   // V for the chroma pass, unused for luma
    int pitch;          // shared by a and b
    int w, h;           // plane size
    float sx, sy;       // plane size / table camera size
    float feather;      // in plane pixels
};

struct Params {
    CamPlanes cam[kMaxCams];
    // Per-camera brightness/colour correction (PhotoBalance): Y x gain;
    // chroma 128 + gain x (c - 128) + offset
    float gain[kMaxCams], off_a[kMaxCams], off_b[kMaxCams];
    // Night stretch on Y after blending (D-025): black point, then
    // 255 * asinh(k * x) / asinh(k) over x = (Y - black) / (255 - black). k = 0: off
    float black, stretch_k, stretch_norm;
    int ncams;
    const float* gu;
    const float* gv;
    const uint8_t* mask;
    // Ground mask: altitude of the horizon line per degree of azimuth (360),
    // nullptr = none. Below it the output is fill, like outside the dome
    const float* horizon;
    float edge_deg;     // zenith angle at the dome circle (table header)
    int nodes, step, size;
    int out_div;        // 1 = luma, 2 = chroma (output is half size)
    int ow, oh;         // output plane size
    float x0, y0, scale;   // canvas pixel at the output's corner, canvas pixels per output (luma) pixel
    uint8_t* outa;
    uint8_t* outb;
    uint8_t fill_a, fill_b;
};

__device__ __forceinline__ uint8_t to_u8(float x) {
    return static_cast<uint8_t>(fminf(fmaxf(x + 0.5f, 0.f), 255.f));
}

template <bool kTwo>
__global__ void stitch_kernel(Params p) {
    const int ox = blockIdx.x * blockDim.x + threadIdx.x;
    const int oy = blockIdx.y * blockDim.y + threadIdx.y;
    if (ox >= p.ow || oy >= p.oh) return;
    const size_t o = static_cast<size_t>(oy) * p.ow + ox;

    // Output pixel centre in canvas pixels (the whole dome: x0 = y0 = 0, scale 1)
    const float px = p.x0 + (ox + 0.5f) * p.out_div * p.scale - 0.5f;
    const float py = p.y0 + (oy + 0.5f) * p.out_div * p.scale - 0.5f;
    const float half = 0.5f * p.size;
    const float dx = (px + 0.5f - half) / half;
    const float dy = (py + 0.5f - half) / half;
    const float rho2 = dx * dx + dy * dy;
    bool hide = rho2 > 1.f;
    if (!hide && p.horizon) {
        // Same layout as AllSkyProjection (north up, east on the left)
        const float alt = 90.f - sqrtf(rho2) * p.edge_deg;
        float az = atan2f(-dx, -dy) * 57.29578f;
        if (az < 0.f) az += 360.f;
        hide = alt < __ldg(p.horizon + min(static_cast<int>(az), 359));
    }
    if (hide) {
        p.outa[o] = p.fill_a;
        if (kTwo) p.outb[o] = p.fill_b;
        return;
    }

    const float gx = px / p.step, gy = py / p.step;
    const int i = min(max(static_cast<int>(floorf(gx)), 0), p.nodes - 2);
    const int j = min(max(static_cast<int>(floorf(gy)), 0), p.nodes - 2);
    const float fx = gx - i, fy = gy - j;
    const uint8_t m = __ldg(p.mask + static_cast<size_t>(j) * (p.nodes - 1) + i);

    float acc_a = 0.f, acc_b = 0.f, wsum = 0.f, fb_a = 0.f, fb_b = 0.f, fb_n = 0.f;
    for (int c = 0; c < p.ncams; ++c) {
        if (!((m >> c) & 1)) continue;
        const CamPlanes& cp = p.cam[c];
        if (!cp.a) continue;
        const size_t k = static_cast<size_t>(c) * p.nodes * p.nodes + static_cast<size_t>(j) * p.nodes + i;
        const float u00 = __ldg(p.gu + k), u01 = __ldg(p.gu + k + 1);
        const float u10 = __ldg(p.gu + k + p.nodes), u11 = __ldg(p.gu + k + p.nodes + 1);
        const float v00 = __ldg(p.gv + k), v01 = __ldg(p.gv + k + 1);
        const float v10 = __ldg(p.gv + k + p.nodes), v11 = __ldg(p.gv + k + p.nodes + 1);
        float u = (u00 * (1.f - fx) + u01 * fx) * (1.f - fy) + (u10 * (1.f - fx) + u11 * fx) * fy;
        float v = (v00 * (1.f - fx) + v01 * fx) * (1.f - fy) + (v10 * (1.f - fx) + v11 * fx) * fy;
        u = (u + 0.5f) * cp.sx - 0.5f;
        v = (v + 0.5f) * cp.sy - 0.5f;
        // NaN fails every comparison, so rays outside the lens model drop out here
        if (!(u >= 0.f && u <= cp.w - 1 && v >= 0.f && v <= cp.h - 1)) continue;
        const float d = fminf(fminf(u, cp.w - 1 - u), fminf(v, cp.h - 1 - v));
        const float t = fminf(fmaxf(d / cp.feather, 0.f), 1.f);
        const float w = t * t * (3.f - 2.f * t);
        float sa = sample(cp.a, cp.pitch, cp.w, cp.h, u, v);
        float sb = kTwo ? sample(cp.b, cp.pitch, cp.w, cp.h, u, v) : 0.f;
        if (kTwo) {
            sa = 128.f + p.gain[c] * (sa - 128.f) + p.off_a[c];
            sb = 128.f + p.gain[c] * (sb - 128.f) + p.off_b[c];
        } else {
            // About the black point: at night the cameras' black level (~35) is
            // most of the signal, and scaling it would leave each camera a different grey
            sa = p.black + (sa - p.black) * p.gain[c];
        }
        if (w > 0.f) {
            acc_a += w * sa;
            acc_b += w * sb;
            wsum += w;
        } else {
            // Seen only on a camera's outermost pixel row: use it unweighted
            fb_a += sa;
            fb_b += sb;
            fb_n += 1.f;
        }
    }
    float ra, rb;
    if (wsum > 0.f) {
        ra = acc_a / wsum;
        rb = acc_b / wsum;
    } else if (fb_n > 0.f) {
        ra = fb_a / fb_n;
        rb = fb_b / fb_n;
    } else {
        ra = p.fill_a;
        rb = p.fill_b;
    }
    if (!kTwo && p.stretch_k > 0.f) {
        const float x = fmaxf(ra - p.black, 0.f) / (255.f - p.black);
        ra = 255.f * asinhf(p.stretch_k * x) * p.stretch_norm;
    }
    p.outa[o] = to_u8(ra);
    if (kTwo) p.outb[o] = to_u8(rb);
}

}  // namespace

GpuStitcher::GpuStitcher(const GridLut& lut)
    : size_(lut.size), step_(lut.step), nodes_(lut.nodes), ncams_(static_cast<int>(lut.cams.size())),
      edge_deg_(lut.edge_zenith_deg) {
    if (ncams_ > kMaxCams) throw std::runtime_error("too many cameras in the stitch table");
    for (int c = 0; c < ncams_; ++c) {
        corr_[c] = CamCorrection{};
        cam_w_[c] = lut.cams[c].width;
        cam_h_[c] = lut.cams[c].height;
        feather_[c] = lut.cams[c].feather;
    }
    const size_t plane = lut.u.size() * sizeof(float);
    cuda_check(cudaMalloc(&d_u_, plane), "cudaMalloc u");
    cuda_check(cudaMalloc(&d_v_, plane), "cudaMalloc v");
    cuda_check(cudaMalloc(&d_mask_, lut.mask.size()), "cudaMalloc mask");
    cuda_check(cudaMemcpy(d_u_, lut.u.data(), plane, cudaMemcpyHostToDevice), "upload u");
    cuda_check(cudaMemcpy(d_v_, lut.v.data(), plane, cudaMemcpyHostToDevice), "upload v");
    cuda_check(cudaMemcpy(d_mask_, lut.mask.data(), lut.mask.size(), cudaMemcpyHostToDevice), "upload mask");
}

void GpuStitcher::set_horizon(const std::vector<float>& alt_by_az) {
    if (alt_by_az.size() != 360) {
        has_horizon_ = false;
        return;
    }
    if (!d_horizon_) cuda_check(cudaMalloc(&d_horizon_, 360 * sizeof(float)), "cudaMalloc horizon");
    cuda_check(cudaMemcpy(d_horizon_, alt_by_az.data(), 360 * sizeof(float), cudaMemcpyHostToDevice), "upload horizon");
    has_horizon_ = true;
}

void GpuStitcher::set_stretch(float black, float k) {
    stretch_k_ = std::max(k, 0.f);
    black_ = stretch_k_ > 0.f ? std::min(std::max(black, 0.f), 200.f) : 0.f;   // off: as before
}

void GpuStitcher::set_corrections(const std::vector<CamCorrection>& corr) {
    for (int c = 0; c < ncams_ && c < static_cast<int>(corr.size()); ++c) corr_[c] = corr[c];
}

GpuStitcher::~GpuStitcher() {
    cudaFree(d_u_);
    cudaFree(d_v_);
    cudaFree(d_mask_);
    cudaFree(d_horizon_);
}

void GpuStitcher::run(const PlanarImage* inputs, const I420Out& out, cudaStream_t stream) const {
    run_view(inputs, out, View{size_, size_, 0.f, 0.f, 1.f}, stream);
}

void GpuStitcher::run_view(const PlanarImage* inputs, const I420Out& out, const View& view,
                           cudaStream_t stream) const {
    Params luma{};
    Params chroma{};
    for (Params* p : {&luma, &chroma}) {
        p->ncams = ncams_;
        p->gu = d_u_;
        p->gv = d_v_;
        p->mask = d_mask_;
        p->horizon = has_horizon_ ? d_horizon_ : nullptr;
        p->edge_deg = edge_deg_;
        p->nodes = nodes_;
        p->step = step_;
        p->size = size_;
        p->black = black_;
        p->stretch_k = stretch_k_;
        p->stretch_norm = stretch_k_ > 0.f ? 1.f / asinhf(stretch_k_) : 1.f;
        p->x0 = view.x0;
        p->y0 = view.y0;
        p->scale = view.scale;
    }
    for (int c = 0; c < ncams_; ++c) {
        for (Params* p : {&luma, &chroma}) p->gain[c] = corr_[c].gain;
        chroma.off_a[c] = corr_[c].u;
        chroma.off_b[c] = corr_[c].v;
        const PlanarImage& im = inputs[c];
        if (!im.plane[0]) continue;
        const float sx = static_cast<float>(im.width[0]) / cam_w_[c];
        const float sy = static_cast<float>(im.height[0]) / cam_h_[c];
        luma.cam[c] = {im.plane[0], nullptr, im.pitch[0], im.width[0], im.height[0], sx, sy, feather_[c] * sx};
        if (im.pitch[1] != im.pitch[2] || im.width[1] != im.width[2] || im.height[1] != im.height[2])
            throw std::runtime_error("U and V planes differ in layout");
        const float csx = static_cast<float>(im.width[1]) / cam_w_[c];
        const float csy = static_cast<float>(im.height[1]) / cam_h_[c];
        chroma.cam[c] = {im.plane[1], im.plane[2], im.pitch[1], im.width[1], im.height[1], csx, csy,
                         feather_[c] * csx};
    }
    luma.out_div = 1;
    luma.ow = view.width;
    luma.oh = view.height;
    luma.outa = out.y;
    luma.fill_a = kBlackY;
    chroma.out_div = 2;
    chroma.ow = view.width / 2;
    chroma.oh = view.height / 2;
    chroma.outa = out.u;
    chroma.outb = out.v;
    chroma.fill_a = chroma.fill_b = kBlackC;

    const dim3 block(32, 8);
    const dim3 grid_l((luma.ow + block.x - 1) / block.x, (luma.oh + block.y - 1) / block.y);
    const dim3 grid_c((chroma.ow + block.x - 1) / block.x, (chroma.oh + block.y - 1) / block.y);
    stitch_kernel<false><<<grid_l, block, 0, stream>>>(luma);
    stitch_kernel<true><<<grid_c, block, 0, stream>>>(chroma);
    cuda_check(cudaGetLastError(), "stitch kernel launch");
}
