#include "stitch.h"

#include <stdexcept>
#include <string>

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
    int ncams;
    const float* gu;
    const float* gv;
    const uint8_t* mask;
    int nodes, step, size;
    int out_div;        // 1 = luma, 2 = chroma (output is size/2)
    int osz;
    uint8_t* outa;
    uint8_t* outb;
    uint8_t fill_a, fill_b;
};

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

__device__ __forceinline__ uint8_t to_u8(float x) {
    return static_cast<uint8_t>(fminf(fmaxf(x + 0.5f, 0.f), 255.f));
}

template <bool kTwo>
__global__ void stitch_kernel(Params p) {
    const int ox = blockIdx.x * blockDim.x + threadIdx.x;
    const int oy = blockIdx.y * blockDim.y + threadIdx.y;
    if (ox >= p.osz || oy >= p.osz) return;
    const size_t o = static_cast<size_t>(oy) * p.osz + ox;

    // Output pixel centre in canvas pixels
    const float px = (ox + 0.5f) * p.out_div - 0.5f;
    const float py = (oy + 0.5f) * p.out_div - 0.5f;
    const float half = 0.5f * p.size;
    const float dx = (px + 0.5f - half) / half;
    const float dy = (py + 0.5f - half) / half;
    if (dx * dx + dy * dy > 1.f) {
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
        const float sa = sample(cp.a, cp.pitch, cp.w, cp.h, u, v);
        const float sb = kTwo ? sample(cp.b, cp.pitch, cp.w, cp.h, u, v) : 0.f;
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
    p.outa[o] = to_u8(ra);
    if (kTwo) p.outb[o] = to_u8(rb);
}

}  // namespace

GpuStitcher::GpuStitcher(const GridLut& lut)
    : size_(lut.size), step_(lut.step), nodes_(lut.nodes), ncams_(static_cast<int>(lut.cams.size())) {
    if (ncams_ > kMaxCams) throw std::runtime_error("too many cameras in the stitch table");
    for (int c = 0; c < ncams_; ++c) {
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

GpuStitcher::~GpuStitcher() {
    cudaFree(d_u_);
    cudaFree(d_v_);
    cudaFree(d_mask_);
}

void GpuStitcher::run(const PlanarImage* inputs, const I420Out& out, cudaStream_t stream) const {
    Params luma{};
    Params chroma{};
    for (Params* p : {&luma, &chroma}) {
        p->ncams = ncams_;
        p->gu = d_u_;
        p->gv = d_v_;
        p->mask = d_mask_;
        p->nodes = nodes_;
        p->step = step_;
        p->size = size_;
    }
    for (int c = 0; c < ncams_; ++c) {
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
    luma.osz = size_;
    luma.outa = out.y;
    luma.fill_a = kBlackY;
    chroma.out_div = 2;
    chroma.osz = size_ / 2;
    chroma.outa = out.u;
    chroma.outb = out.v;
    chroma.fill_a = chroma.fill_b = kBlackC;

    const dim3 block(32, 8);
    const dim3 grid_l((luma.osz + block.x - 1) / block.x, (luma.osz + block.y - 1) / block.y);
    const dim3 grid_c((chroma.osz + block.x - 1) / block.x, (chroma.osz + block.y - 1) / block.y);
    stitch_kernel<false><<<grid_l, block, 0, stream>>>(luma);
    stitch_kernel<true><<<grid_c, block, 0, stream>>>(chroma);
    cuda_check(cudaGetLastError(), "stitch kernel launch");
}
