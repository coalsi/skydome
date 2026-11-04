// skydome-stitch-files: run the GPU stitch on raw planar frames from disk.
// Used by pipeline/tests/test_native_stitch.py to check the kernel against
// the NumPy twin, and to time the kernel on its own.
//
//   skydome-stitch-files --lut var/stitch_5120.lut --out dome.i420 \
//       --input zenith=zenith.yuv:3840x2160:y42b --input north=... [--repeat 50] [--balance]
//
// --balance matches brightness and colour between the cameras first
// (PhotoBalance, as skydome-live does), and prints the corrections.
// --float hands the stitch float planes, as a stacked camera does (stack.h).
//
// Input files are tightly packed planar YUV: y42b (4:2:2) or i420 (4:2:0).
// Cameras in the table without an --input are treated as missing.

#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

#include "balance.h"
#include "lut.h"
#include "stitch.h"

namespace {

struct Input {
    std::string name, path, fmt;
    int w = 0, h = 0;
};

Input parse_input(const std::string& s) {
    // name=path:WxH:fmt
    Input in;
    const auto eq = s.find('='), c2 = s.rfind(':');
    const auto c1 = s.rfind(':', c2 - 1);
    if (eq == std::string::npos || c1 == std::string::npos || c2 == std::string::npos || c1 <= eq)
        throw std::runtime_error("bad --input " + s);
    in.name = s.substr(0, eq);
    in.path = s.substr(eq + 1, c1 - eq - 1);
    if (std::sscanf(s.substr(c1 + 1, c2 - c1 - 1).c_str(), "%dx%d", &in.w, &in.h) != 2)
        throw std::runtime_error("bad size in --input " + s);
    in.fmt = s.substr(c2 + 1);
    if (in.fmt != "y42b" && in.fmt != "i420") throw std::runtime_error("format must be y42b or i420: " + s);
    return in;
}

int run(int argc, char** argv) {
    std::string lut_path, out_path;
    std::vector<Input> inputs;
    int repeat = 1;
    bool balance = false;
    bool as_float = false;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) throw std::runtime_error(a + " needs a value");
            return argv[++i];
        };
        if (a == "--lut") lut_path = next();
        else if (a == "--out") out_path = next();
        else if (a == "--input") inputs.push_back(parse_input(next()));
        else if (a == "--repeat") repeat = std::stoi(next());
        else if (a == "--balance") balance = true;
        else if (a == "--float") as_float = true;
        else throw std::runtime_error("unknown argument " + a);
    }
    if (lut_path.empty()) throw std::runtime_error("--lut is required");

    const GridLut lut = GridLut::load(lut_path);
    GpuStitcher st(lut);
    std::vector<PlanarImage> imgs(lut.cams.size());
    std::vector<uint8_t*> owned;
    std::vector<float*> owned_f;
    for (const Input& in : inputs) {
        const int c = lut.camera_index(in.name);
        if (c < 0) throw std::runtime_error("camera " + in.name + " is not in the table");
        std::ifstream f(in.path, std::ios::binary);
        std::vector<uint8_t> data((std::istreambuf_iterator<char>(f)), {});
        const int cw = in.w / 2, ch = in.fmt == "y42b" ? in.h : in.h / 2;
        const size_t ysz = size_t(in.w) * in.h, csz = size_t(cw) * ch;
        if (data.size() != ysz + 2 * csz) throw std::runtime_error(in.path + ": wrong size for " + in.fmt);
        uint8_t* d = nullptr;
        cuda_check(cudaMalloc(&d, data.size()), "cudaMalloc input");
        cuda_check(cudaMemcpy(d, data.data(), data.size(), cudaMemcpyHostToDevice), "upload input");
        owned.push_back(d);
        PlanarImage& im = imgs[c];
        im.plane[0] = d;
        im.plane[1] = d + ysz;
        im.plane[2] = d + ysz + csz;
        im.pitch[0] = in.w;
        im.pitch[1] = im.pitch[2] = cw;
        im.width[0] = in.w;
        im.height[0] = in.h;
        im.width[1] = im.width[2] = cw;
        im.height[1] = im.height[2] = ch;
        if (as_float) {
            const size_t off[3] = {0, ysz, ysz + csz}, n[3] = {ysz, csz, csz};
            for (int k = 0; k < 3; ++k) {
                std::vector<float> fl(data.begin() + off[k], data.begin() + off[k] + n[k]);
                float* fd = nullptr;
                cuda_check(cudaMalloc(&fd, n[k] * sizeof(float)), "cudaMalloc float input");
                cuda_check(cudaMemcpy(fd, fl.data(), n[k] * sizeof(float), cudaMemcpyHostToDevice), "upload float");
                owned_f.push_back(fd);
                im.fplane[k] = fd;
                im.fpitch[k] = k ? cw : in.w;
            }
        }
    }

    const int s = st.size();
    const size_t ysz = size_t(s) * s, csz = ysz / 4;
    uint8_t* d_out = nullptr;
    cuda_check(cudaMalloc(&d_out, ysz + 2 * csz), "cudaMalloc output");
    const I420Out out{d_out, d_out + ysz, d_out + ysz + csz};
    cudaStream_t stream;
    cuda_check(cudaStreamCreate(&stream), "stream");
    if (balance) {
        PhotoBalance pb(lut);
        if (!pb.update(imgs.data(), stream, 1.f)) std::printf("balance: too little usable overlap; none applied\n");
        st.set_corrections(pb.corrections());
        for (size_t c = 0; c < lut.cams.size(); ++c)
            std::printf("balance %-7s gain %.3f  u %+6.2f  v %+6.2f\n", lut.cams[c].name.c_str(),
                        pb.corrections()[c].gain, pb.corrections()[c].u, pb.corrections()[c].v);
        std::printf("balance: %d of %d overlap points used\n", pb.used(), pb.samples());
    }
    st.run(imgs.data(), out, stream);   // warm-up
    cuda_check(cudaStreamSynchronize(stream), "warm-up");
    const auto t0 = std::chrono::steady_clock::now();
    for (int r = 0; r < repeat; ++r) st.run(imgs.data(), out, stream);
    cuda_check(cudaStreamSynchronize(stream), "stitch");
    const double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    std::printf("stitch %dx%d from %zu camera(s): %.2f ms per frame (%d runs)\n", s, s, inputs.size(),
                ms / repeat, repeat);

    if (!out_path.empty()) {
        std::vector<uint8_t> host(ysz + 2 * csz);
        cuda_check(cudaMemcpy(host.data(), d_out, host.size(), cudaMemcpyDeviceToHost), "download");
        std::ofstream(out_path, std::ios::binary).write(reinterpret_cast<const char*>(host.data()),
                                                         static_cast<std::streamsize>(host.size()));
    }
    for (uint8_t* d : owned) cudaFree(d);
    for (float* d : owned_f) cudaFree(d);
    cudaFree(d_out);
    cudaStreamDestroy(stream);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        return run(argc, argv);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "skydome-stitch-files: %s\n", e.what());
        return 1;
    }
}
