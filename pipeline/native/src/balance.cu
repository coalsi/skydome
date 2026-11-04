#include "balance.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "sample.cuh"

namespace {

constexpr int kMaxCams = 8;
constexpr int kMaxPairs = kMaxCams * (kMaxCams - 1) / 2;
constexpr int kFields = 6;              // n, Σ(ln Ya − ln Yb), Σ Ua', Σ Ub', Σ Va', Σ Vb' (' = − 128)
constexpr float kMargin = 0.01f;        // skip the outermost 1% of each picture
constexpr float kMaxZenithDeg = 88.f;   // sky only: the ground near the rig sits at different parallax
constexpr float kYLow = 10.f, kYHigh = 240.f;   // too dark to trust, or clipped
constexpr float kNightLow = 2.f;   // with a black point: this much above it is usable
constexpr int kMinPairSamples = 200;    // a pair with fewer usable points says nothing
constexpr float kRegularise = 0.02f;    // pulls each correction toward none
constexpr float kMinGain = 0.5f, kMaxGain = 2.f, kMaxOffset = 40.f;

struct Plane {
    const uint8_t* p;
    int pitch, w, h;
    float sx, sy;   // plane size / table camera size
};

struct CamFrame {
    Plane y, u, v;
};

}  // namespace

struct PhotoBalance::Sample {
    uint8_t a, b, pair, pad;
    float ua, va, ub, vb;   // table camera pixels
};

namespace {

__device__ __forceinline__ float at(const Plane& pl, float u, float v) {
    const float x = fminf(fmaxf((u + 0.5f) * pl.sx - 0.5f, 0.f), pl.w - 1.f);
    const float y = fminf(fmaxf((v + 0.5f) * pl.sy - 0.5f, 0.f), pl.h - 1.f);
    return sample(pl.p, pl.pitch, pl.w, pl.h, x, y);
}

struct Frames {
    CamFrame cam[kMaxCams];
};

__global__ void measure_kernel(const PhotoBalance::Sample* samples, int n, Frames f, int npairs, float* acc,
                               float black, float low) {
    __shared__ float block[kMaxPairs * kFields];
    for (int i = threadIdx.x; i < npairs * kFields; i += blockDim.x) block[i] = 0.f;
    __syncthreads();

    const int k = blockIdx.x * blockDim.x + threadIdx.x;
    if (k < n) {
        const PhotoBalance::Sample s = samples[k];
        const CamFrame& A = f.cam[s.a];
        const CamFrame& B = f.cam[s.b];
        if (A.y.p && B.y.p) {
            const float ya = at(A.y, s.ua, s.va) - black, yb = at(B.y, s.ub, s.vb) - black;
            if (ya > low && ya < kYHigh - black && yb > low && yb < kYHigh - black) {
                float* b = block + s.pair * kFields;
                atomicAdd(b + 0, 1.f);
                atomicAdd(b + 1, __logf(ya) - __logf(yb));
                atomicAdd(b + 2, at(A.u, s.ua, s.va) - 128.f);
                atomicAdd(b + 3, at(B.u, s.ub, s.vb) - 128.f);
                atomicAdd(b + 4, at(A.v, s.ua, s.va) - 128.f);
                atomicAdd(b + 5, at(B.v, s.ub, s.vb) - 128.f);
            }
        }
    }
    __syncthreads();
    for (int i = threadIdx.x; i < npairs * kFields; i += blockDim.x)
        if (block[i] != 0.f) atomicAdd(acc + i, block[i]);
}

// Solve (A + λI) x = rhs for a small dense system (Gaussian elimination, partial pivoting)
std::vector<double> solve(std::vector<double> A, std::vector<double> rhs, int n) {
    for (int i = 0; i < n; ++i) A[i * n + i] += kRegularise;
    for (int col = 0; col < n; ++col) {
        int piv = col;
        for (int r = col + 1; r < n; ++r)
            if (std::fabs(A[r * n + col]) > std::fabs(A[piv * n + col])) piv = r;
        for (int c = 0; c < n; ++c) std::swap(A[col * n + c], A[piv * n + c]);
        std::swap(rhs[col], rhs[piv]);
        for (int r = col + 1; r < n; ++r) {
            const double f = A[r * n + col] / A[col * n + col];
            for (int c = col; c < n; ++c) A[r * n + c] -= f * A[col * n + c];
            rhs[r] -= f * rhs[col];
        }
    }
    std::vector<double> x(n);
    for (int r = n - 1; r >= 0; --r) {
        double s = rhs[r];
        for (int c = r + 1; c < n; ++c) s -= A[r * n + c] * x[c];
        x[r] = s / A[r * n + r];
    }
    return x;
}

}  // namespace

PhotoBalance::PhotoBalance(const GridLut& lut)
    : ncams_(static_cast<int>(lut.cams.size())), npairs_(0), corr_(lut.cams.size()) {
    if (ncams_ > kMaxCams) throw std::runtime_error("too many cameras in the stitch table");
    int pair_of[kMaxCams][kMaxCams];
    for (int a = 0; a < ncams_; ++a) {
        cam_w_[a] = lut.cams[a].width;
        cam_h_[a] = lut.cams[a].height;
        for (int b = a + 1; b < ncams_; ++b) {
            pair_a_[npairs_] = a;
            pair_b_[npairs_] = b;
            pair_of[a][b] = npairs_++;
        }
    }

    // Every grid node in the sky that two cameras both see, away from their edges
    std::vector<Sample> samples;
    const float half = 0.5f * lut.size;
    const size_t per_cam = size_t(lut.nodes) * lut.nodes;
    for (int j = 0; j < lut.nodes; ++j) {
        for (int i = 0; i < lut.nodes; ++i) {
            const float dx = (i * lut.step + 0.5f - half) / half, dy = (j * lut.step + 0.5f - half) / half;
            if (std::sqrt(dx * dx + dy * dy) * lut.edge_zenith_deg > kMaxZenithDeg) continue;
            int seen[kMaxCams], m = 0;
            float us[kMaxCams], vs[kMaxCams];
            for (int c = 0; c < ncams_; ++c) {
                const float u = lut.u[c * per_cam + size_t(j) * lut.nodes + i];
                const float v = lut.v[c * per_cam + size_t(j) * lut.nodes + i];
                const float mx = kMargin * cam_w_[c], my = kMargin * cam_h_[c];
                if (u >= mx && u <= cam_w_[c] - 1 - mx && v >= my && v <= cam_h_[c] - 1 - my) {
                    seen[m] = c;
                    us[m] = u;
                    vs[m++] = v;
                }
            }
            for (int x = 0; x < m; ++x)
                for (int y = x + 1; y < m; ++y)
                    samples.push_back({static_cast<uint8_t>(seen[x]), static_cast<uint8_t>(seen[y]),
                                       static_cast<uint8_t>(pair_of[seen[x]][seen[y]]), 0, us[x], vs[x], us[y],
                                       vs[y]});
        }
    }
    nsamples_ = static_cast<int>(samples.size());
    if (nsamples_) {
        cuda_check(cudaMalloc(&d_samples_, samples.size() * sizeof(Sample)), "cudaMalloc balance samples");
        cuda_check(cudaMemcpy(d_samples_, samples.data(), samples.size() * sizeof(Sample), cudaMemcpyHostToDevice),
                   "upload balance samples");
    }
    cuda_check(cudaMalloc(&d_acc_, kMaxPairs * kFields * sizeof(float)), "cudaMalloc balance sums");
}

PhotoBalance::~PhotoBalance() {
    cudaFree(d_samples_);
    cudaFree(d_acc_);
}

bool PhotoBalance::update(const PlanarImage* inputs, cudaStream_t stream, float step, float black) {
    used_ = 0;
    if (!nsamples_ || !npairs_) return false;
    Frames f{};
    for (int c = 0; c < ncams_; ++c) {
        const PlanarImage& im = inputs[c];
        if (!im.plane[0]) continue;
        for (int p = 0; p < 3; ++p) {
            Plane& pl = p == 0 ? f.cam[c].y : p == 1 ? f.cam[c].u : f.cam[c].v;
            pl = {im.plane[p], im.pitch[p], im.width[p], im.height[p],
                  static_cast<float>(im.width[p]) / cam_w_[c], static_cast<float>(im.height[p]) / cam_h_[c]};
        }
    }
    cuda_check(cudaMemsetAsync(d_acc_, 0, npairs_ * kFields * sizeof(float), stream), "clear balance sums");
    const int threads = 256;
    measure_kernel<<<(nsamples_ + threads - 1) / threads, threads, 0, stream>>>(
        d_samples_, nsamples_, f, npairs_, d_acc_, black, black > 0.f ? kNightLow : kYLow);
    cuda_check(cudaGetLastError(), "balance kernel launch");
    float acc[kMaxPairs * kFields];
    cuda_check(cudaMemcpyAsync(acc, d_acc_, npairs_ * kFields * sizeof(float), cudaMemcpyDeviceToHost, stream),
               "download balance sums");
    cuda_check(cudaStreamSynchronize(stream), "balance");

    // Least squares over the pairs, weighted by how much each one saw:
    //   brightness  x_a − x_b = −mean(ln Ya − ln Yb)          (x = ln gain)
    //   colour      o_a − o_b = −(g_a·mean Ua' − g_b·mean Ub')  (same for V)
    const int n = ncams_;
    double total = 0;
    for (int p = 0; p < npairs_; ++p)
        if (acc[p * kFields] >= kMinPairSamples) total += acc[p * kFields];
    if (total <= 0) return false;
    std::vector<double> A(n * n, 0.0), rx(n, 0.0);
    for (int p = 0; p < npairs_; ++p) {
        const float* s = acc + p * kFields;
        if (s[0] < kMinPairSamples) continue;
        const int a = pair_a_[p], b = pair_b_[p];
        const double w = s[0] / total, m = s[1] / s[0];
        A[a * n + a] += w;
        A[b * n + b] += w;
        A[a * n + b] -= w;
        A[b * n + a] -= w;
        rx[a] -= w * m;
        rx[b] += w * m;
        used_ += static_cast<int>(s[0]);
    }
    const std::vector<double> x = solve(A, rx, n);
    std::vector<double> gain(n), ru(n, 0.0), rv(n, 0.0);
    for (int c = 0; c < n; ++c) gain[c] = std::clamp(std::exp(x[c]), double(kMinGain), double(kMaxGain));
    for (int p = 0; p < npairs_; ++p) {
        const float* s = acc + p * kFields;
        if (s[0] < kMinPairSamples) continue;
        const int a = pair_a_[p], b = pair_b_[p];
        const double w = s[0] / total;
        const double du = gain[a] * s[2] / s[0] - gain[b] * s[3] / s[0];
        const double dv = gain[a] * s[4] / s[0] - gain[b] * s[5] / s[0];
        ru[a] -= w * du;
        ru[b] += w * du;
        rv[a] -= w * dv;
        rv[b] += w * dv;
    }
    const std::vector<double> ou = solve(A, ru, n), ov = solve(A, rv, n);

    for (int c = 0; c < n; ++c) {
        if (!inputs[c].plane[0]) continue;
        CamCorrection& k = corr_[c];
        const double lg = std::log(k.gain) + step * (std::log(gain[c]) - std::log(k.gain));
        k.gain = static_cast<float>(std::exp(lg));
        k.u = std::clamp(static_cast<float>(k.u + step * (ou[c] - k.u)), -kMaxOffset, kMaxOffset);
        k.v = std::clamp(static_cast<float>(k.v + step * (ov[c] - k.v)), -kMaxOffset, kMaxOffset);
    }
    return true;
}
