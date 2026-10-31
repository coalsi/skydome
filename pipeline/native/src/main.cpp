// skydome-live: five cameras -> one live dome stream (handoff §4, Phase 1).
//
//   skydome-live --lut var/stitch_4096.lut --camera zenith=/dev/skycam-zenith ... [options]
//
// Normally started by tools/run_live.py, which builds the table and finds
// the cameras. Each output tick takes the newest frame from every camera,
// stitches them on the GPU and hands the dome to the encoder. A camera
// without a frame is left out; the dome keeps running on the rest.

#include <algorithm>
#include <atomic>
#include <csignal>
#include <cstdio>
#include <fstream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <cuda_runtime.h>
#include <gst/gst.h>

#include "capture.h"
#include "encoder.h"
#include "lut.h"
#include "stitch.h"

namespace {

std::atomic<bool> g_stop{false};

struct Options {
    std::string lut;
    std::vector<std::pair<std::string, std::string>> cameras;   // name, device
    CaptureFormat capture;
    EncoderConfig enc;
    std::string stats_file;
    long max_frames = 0;   // 0 = run until stopped
};

void usage() {
    std::fprintf(stderr,
                 "usage: skydome-live --lut FILE --camera NAME=DEVICE [--camera ...]\n"
                 "  --fps N              output frame rate (default 15)\n"
                 "  --capture WxH@FPS    camera mode (default 3840x2160@15)\n"
                 "  --format mjpeg|yuyv  camera format (default mjpeg; yuyv only up to 1080p)\n"
                 "  --bitrate KBPS       H.264 bitrate (default 20000)\n"
                 "  --sink DESC          GStreamer sink after h264parse (default RTSP to mediamtx /dome)\n"
                 "  --no-encode          stitch only (benchmark)\n"
                 "  --stats-file PATH    write JSON stats every 2 s\n"
                 "  --frames N           stop after N output frames\n");
}

Options parse(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) throw std::runtime_error(a + " needs a value");
            return argv[++i];
        };
        if (a == "--lut") o.lut = next();
        else if (a == "--camera") {
            const std::string v = next();
            const auto eq = v.find('=');
            if (eq == std::string::npos) throw std::runtime_error("--camera wants NAME=DEVICE");
            o.cameras.emplace_back(v.substr(0, eq), v.substr(eq + 1));
        } else if (a == "--fps") o.enc.fps = std::stoi(next());
        else if (a == "--capture") {
            const std::string v = next();
            if (std::sscanf(v.c_str(), "%dx%d@%d", &o.capture.width, &o.capture.height, &o.capture.fps) != 3)
                throw std::runtime_error("--capture wants WxH@FPS");
        } else if (a == "--format") {
            const std::string v = next();
            if (v == "mjpeg") o.capture.kind = CaptureFormat::Kind::MJPEG;
            else if (v == "yuyv") o.capture.kind = CaptureFormat::Kind::YUYV;
            else throw std::runtime_error("--format is mjpeg or yuyv");
        } else if (a == "--bitrate") o.enc.bitrate_kbps = std::stoi(next());
        else if (a == "--sink") o.enc.sink = next();
        else if (a == "--no-encode") o.enc.encode = false;
        else if (a == "--stats-file") o.stats_file = next();
        else if (a == "--frames") o.max_frames = std::stol(next());
        else if (a == "-h" || a == "--help") {
            usage();
            std::exit(0);
        } else throw std::runtime_error("unknown argument " + a);
    }
    if (o.lut.empty() || o.cameras.empty()) {
        usage();
        throw std::runtime_error("--lut and at least one --camera are required");
    }
    // Handoff §2.1 / §9: never YUY2 above 1080p
    if (o.capture.kind == CaptureFormat::Kind::YUYV && o.capture.width * o.capture.height > 1920 * 1080)
        throw std::runtime_error("YUYV is only allowed up to 1920x1080; use mjpeg for 4K");
    return o;
}

struct Stats {
    Clock::time_point since = Clock::now();
    uint64_t out_frames = 0, skipped = 0, push_failed = 0;
    double stitch_ms = 0, stitch_ms_max = 0;
};

void report(const Options& o, std::vector<std::unique_ptr<CameraSource>>& cams, const Encoder& enc,
            Stats& st, Clock::time_point now) {
    const double secs = std::chrono::duration<double>(now - st.since).count();
    const double out_fps = st.out_frames / secs;
    const double avg_ms = st.out_frames ? st.stitch_ms / st.out_frames : 0;
    std::string json = "{\"output_fps\":" + std::to_string(out_fps) + ",\"stitch_ms\":" + std::to_string(avg_ms) +
                       ",\"stitch_ms_max\":" + std::to_string(st.stitch_ms_max) +
                       ",\"skipped\":" + std::to_string(st.skipped) +
                       ",\"push_failed\":" + std::to_string(st.push_failed) +
                       ",\"encoder\":\"" + (enc.running() ? "running" : "down") + "\"" +
                       ",\"encoder_restarts\":" + std::to_string(enc.restarts()) + ",\"cameras\":{";
    std::string line;
    char buf[160];
    std::snprintf(buf, sizeof buf, "out %.1f fps, stitch %.1f ms (max %.1f), skipped %lu |", out_fps, avg_ms,
                  st.stitch_ms_max, static_cast<unsigned long>(st.skipped));
    line = buf;
    bool first = true;
    for (auto& cp : cams) {
        if (!cp) continue;
        CameraSource& c = *cp;
        const double fps = c.take_frame_count() / secs;
        const uint64_t dropped = c.take_dropped();
        std::snprintf(buf, sizeof buf, " %s %.1f %s%s", c.name().c_str(), fps, c.state(),
                      dropped ? (" (" + std::to_string(dropped) + " damaged dropped)").c_str() : "");
        line += buf;
        json += (first ? "" : ",") + std::string("\"") + c.name() + "\":{\"fps\":" + std::to_string(fps) +
                ",\"state\":\"" + c.state() + "\",\"restarts\":" + std::to_string(c.restarts()) +
                ",\"damaged_dropped\":" + std::to_string(dropped) + "}";
        first = false;
    }
    json += "}}\n";
    std::fprintf(stderr, "%s\n", line.c_str());
    if (!o.stats_file.empty()) {
        const std::string tmp = o.stats_file + ".tmp";
        std::ofstream(tmp) << json;
        std::rename(tmp.c_str(), o.stats_file.c_str());
    }
    st = Stats{};
    st.since = now;
}

int run(int argc, char** argv) {
    const Options o = parse(argc, argv);
    gst_init(nullptr, nullptr);
    cuda_check(cudaSetDeviceFlags(cudaDeviceMapHost), "cudaSetDeviceFlags");
    cuda_check(cudaFree(nullptr), "CUDA init");

    const GridLut lut = GridLut::load(o.lut);
    GpuStitcher stitcher(lut);
    EncoderConfig ec = o.enc;
    ec.size = lut.size;

    // Table order; cameras not given on the command line stay null (missing)
    std::vector<std::unique_ptr<CameraSource>> cams(lut.cams.size());
    for (const auto& [name, dev] : o.cameras) {
        const int c = lut.camera_index(name);
        if (c < 0) throw std::runtime_error("camera " + name + " is not in " + o.lut);
        cams[c] = std::make_unique<CameraSource>(name, dev, o.capture);
    }
    FramePool pool(lut.size, 4);
    Encoder enc(ec);
    cudaStream_t stream;
    cuda_check(cudaStreamCreate(&stream), "cudaStreamCreate");

    std::fprintf(stderr, "skydome-live: %dx%d dome at %d fps from %zu camera(s), table %s\n", lut.size, lut.size,
                 ec.fps, o.cameras.size(), o.lut.c_str());

    const auto period = std::chrono::nanoseconds(1'000'000'000LL / ec.fps);
    auto next_tick = Clock::now();
    auto next_report = next_tick + std::chrono::seconds(2);
    Stats st;
    std::vector<PlanarImage> inputs(lut.cams.size());
    long produced = 0;

    while (!g_stop.load()) {
        const auto now = Clock::now();
        for (auto& c : cams)
            if (c) c->poll(now);
        enc.poll(now);

        bool any = false;
        for (size_t c = 0; c < cams.size(); ++c) {
            inputs[c] = (cams[c] && cams[c]->has_frame()) ? cams[c]->image() : PlanarImage{};
            any |= inputs[c].plane[0] != nullptr;
        }
        if (any) {
            if (FramePool::Frame* f = pool.acquire()) {
                const auto t0 = Clock::now();
                stitcher.run(inputs.data(), f->dev, stream);
                cuda_check(cudaStreamSynchronize(stream), "stitch");
                const double ms = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
                st.stitch_ms += ms;
                st.stitch_ms_max = std::max(st.stitch_ms_max, ms);
                if (!enc.push(f)) ++st.push_failed;
                ++st.out_frames;
                if (o.max_frames && ++produced >= o.max_frames) break;
            } else {
                ++st.skipped;   // the encoder is behind: all output frames still in use
            }
        }

        if (now >= next_report) {
            report(o, cams, enc, st, now);
            next_report = now + std::chrono::seconds(2);
        }

        next_tick += period;
        const auto after = Clock::now();
        if (next_tick > after) std::this_thread::sleep_until(next_tick);
        else if (after - next_tick > period * 4) next_tick = after;   // fell far behind: don't burst
    }

    cudaStreamDestroy(stream);
    std::fprintf(stderr, "skydome-live: stopped\n");
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, [](int) { g_stop.store(true); });
    std::signal(SIGTERM, [](int) { g_stop.store(true); });
    try {
        return run(argc, argv);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "skydome-live: %s\n", e.what());
        return 1;
    }
}
