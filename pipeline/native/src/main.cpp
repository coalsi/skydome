// skydome-live: the cameras -> one live dome stream (handoff §4, Phase 1).
//
//   skydome-live --lut var/stitch_5120.lut --camera sky=asi [options]
//   skydome-live --lut var/stitch_5120.lut --camera zenith=/dev/skycam-zenith ... [options]
//
// Normally started by tools/run_live.py, which builds the table and finds
// the cameras: one ZWO all-sky camera (DECISIONS.md D-029; the "stitch" is
// then just the fisheye turned into the dome) or UVC cameras (the 5-camera
// rig). Each output tick takes the newest frame from every camera, stitches
// them on the GPU and hands the dome to the encoder. A camera without a
// frame is left out; the dome keeps running on the rest.
//
// On the side (DECISIONS.md D-023): full-detail stills from a bigger table
// on request, a zoom stream of whatever part of the dome the web page is
// zoomed into (rendered straight from the cameras), and a ground mask.

#include <algorithm>
#include <atomic>
#include <csignal>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <sstream>
#include <sys/stat.h>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <cuda_runtime.h>
#include <gst/gst.h>

#include "capture.h"
#include "encoder.h"
#include "balance.h"
#include "lut.h"
#include "snapshot.h"
#include "stack.h"
#include "stitch.h"
#ifdef SKYDOME_ASI
#include "asi_source.h"
#endif

namespace {

std::atomic<bool> g_stop{false};

struct Options {
    std::string lut;
    std::vector<std::pair<std::string, std::string>> cameras;   // name, device
    CaptureFormat capture;
    EncoderConfig enc;
    std::string stats_file;
    std::string snapshot;   // JPEG stills on request (snapshot.h)
    long max_frames = 0;   // 0 = run until stopped
    bool balance = true;   // match brightness and colour between cameras (D-022)
    std::string full_lut, full_snapshot;   // full-detail stills (both needed)
    std::string zoom_file;                 // zoom requests from the web page
    int zoom_w = 1600, zoom_h = 1200, zoom_bitrate = 8000;
    std::string zoom_sink = "rtspclientsink location=rtsp://127.0.0.1:8554/dome-zoom protocols=tcp latency=0";
    std::string horizon;                   // ground mask file
    std::string raw_dir;                   // raw camera frames on request
    std::string stretch;                   // night stretch file: "black k"
    std::string stack;                     // stacking depth file: "frames" (0 or 1 = off)
    std::string asi_settings, asi_caps;    // ZWO camera: picture settings in, control ranges out
    std::string watch_dir;                 // the sky watcher's feed (D-030)
    double watch_fps = 5;
};

// A request older than this means nobody is zoomed in any more; the zoom
// encoder then stops after kZoomLinger (so a quick zoom out and in again
// doesn't restart it)
constexpr int kZoomFreshS = 4;
constexpr auto kZoomLinger = std::chrono::seconds(15);

// File modification time (seconds since the epoch), 0 if it isn't there
time_t mtime(const std::string& path) {
    struct stat st{};
    return stat(path.c_str(), &st) == 0 ? st.st_mtime : 0;
}

// The web page writes "x0 y0 x1 y1": the zoomed rectangle in dome
// coordinates (0..1 across the dome square), refreshed while it's zoomed
bool read_zoom(const std::string& path, float r[4]) {
    std::ifstream f(path);
    return static_cast<bool>(f >> r[0] >> r[1] >> r[2] >> r[3]) && r[2] > r[0] && r[3] > r[1];
}

// 360 horizon altitudes (one per degree of azimuth, from north through
// east), one or more per line, '#' starts a comment. Anything else: no mask
std::vector<float> read_horizon(const std::string& path) {
    std::ifstream f(path);
    std::vector<float> alt;
    std::string line;
    while (std::getline(f, line)) {
        line = line.substr(0, line.find('#'));
        std::istringstream ls(line);
        float v;
        while (ls >> v) alt.push_back(v);
    }
    if (alt.size() != 360) alt.clear();
    return alt;
}

void usage() {
    std::fprintf(stderr,
                 "usage: skydome-live --lut FILE --camera NAME=DEVICE [--camera ...]\n"
                 "  DEVICE               a V4L2 device (UVC camera), or asi / asi:PART-OF-NAME for a ZWO camera\n"
                 "  --fps N              output frame rate (default 15)\n"
                 "  --capture WxH@FPS    camera mode (default 3840x2160@15)\n"
                 "  --format mjpeg|yuyv  camera format (default mjpeg; yuyv only up to 1080p)\n"
                 "  --bitrate KBPS       H.264 bitrate (default 20000)\n"
                 "  --sink DESC          GStreamer sink after h264parse (default RTSP to mediamtx /dome)\n"
                 "  --no-encode          stitch only (benchmark)\n"
                 "  --stats-file PATH    write JSON stats every 2 s\n"
                 "  --snapshot PATH      write a JPEG of the dome to PATH whenever PATH.request appears\n"
                 "  --frames N           stop after N output frames\n"
                 "  --no-balance         don't match brightness and colour between cameras\n"
                 "  --full-lut FILE --full-snapshot PATH\n"
                 "                       full-detail JPEG from the bigger table FILE whenever PATH.request appears\n"
                 "  --zoom-file PATH     zoom rectangle requests (\"x0 y0 x1 y1\", dome 0..1) from the web page\n"
                 "  --zoom-size WxH      zoom stream size (default 1600x1200)\n"
                 "  --zoom-bitrate KBPS  zoom stream bitrate (default 8000)\n"
                 "  --zoom-sink DESC     zoom stream sink (default RTSP to mediamtx /dome-zoom)\n"
                 "  --horizon FILE       ground mask: 360 horizon altitudes; reread when it changes\n"
                 "  --stretch FILE       night stretch \"black k\" (k 0 = off); reread when it changes\n"
                 "  --stack FILE         stack each camera's last N frames (\"N\"; 0/1 = off); reread when it changes\n"
                 "  --raw-dir DIR        when DIR/request appears, save each camera's next frame as sent\n"
                 "                       (DIR/<camera>.jpg, or .pgm from a ZWO camera)\n"
                 "  --asi-settings FILE  ZWO camera picture settings (key: value lines); reread when it changes\n"
                 "  --asi-caps FILE      write the ZWO camera's control ranges here when it opens (JSON)\n"
                 "  --watch-dir DIR      feed the sky watcher: each ZWO camera's luma at half size, DIR/<camera>.y16\n"
                 "                       and .json (16-bit, x256)\n"
                 "  --watch-fps N        at most N frames a second to it (default 5)\n");
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
        else if (a == "--snapshot") o.snapshot = next();
        else if (a == "--frames") o.max_frames = std::stol(next());
        else if (a == "--no-balance") o.balance = false;
        else if (a == "--full-lut") o.full_lut = next();
        else if (a == "--full-snapshot") o.full_snapshot = next();
        else if (a == "--zoom-file") o.zoom_file = next();
        else if (a == "--zoom-size") {
            if (std::sscanf(next().c_str(), "%dx%d", &o.zoom_w, &o.zoom_h) != 2 || o.zoom_w % 2 || o.zoom_h % 2)
                throw std::runtime_error("--zoom-size wants an even WxH");
        } else if (a == "--zoom-bitrate") o.zoom_bitrate = std::stoi(next());
        else if (a == "--zoom-sink") o.zoom_sink = next();
        else if (a == "--horizon") o.horizon = next();
        else if (a == "--raw-dir") o.raw_dir = next();
        else if (a == "--stretch") o.stretch = next();
        else if (a == "--stack") o.stack = next();
        else if (a == "--asi-settings") o.asi_settings = next();
        else if (a == "--asi-caps") o.asi_caps = next();
        else if (a == "--watch-dir") o.watch_dir = next();
        else if (a == "--watch-fps") o.watch_fps = std::stod(next());
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

bool is_asi(const std::string& device) { return device == "asi" || device.rfind("asi:", 0) == 0; }

std::unique_ptr<FrameSource> make_source(const Options& o, const std::string& name, const std::string& dev) {
    if (!is_asi(dev)) return std::make_unique<CameraSource>(name, dev, o.capture);
#ifdef SKYDOME_ASI
    auto src = std::make_unique<AsiSource>(name, dev.size() > 4 ? dev.substr(4) : "", o.asi_settings, o.asi_caps);
    if (!o.watch_dir.empty()) src->set_tap(o.watch_dir, o.watch_fps);
    return src;
#else
    throw std::runtime_error("built without ZWO's SDK: run tools/get_zwo_sdk.sh, then rebuild");
#endif
}

void report(const Options& o, std::vector<std::unique_ptr<FrameSource>>& cams, const Encoder& enc,
            const PhotoBalance* balance, const std::string& extra, Stats& st, Clock::time_point now) {
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
        FrameSource& c = *cp;
        const double fps = c.take_frame_count() / secs;
        const uint64_t dropped = c.take_dropped();
        std::snprintf(buf, sizeof buf, " %s %.1f %s%s", c.name().c_str(), fps, c.state(),
                      dropped ? (" (" + std::to_string(dropped) + " damaged dropped)").c_str() : "");
        line += buf;
        json += (first ? "" : ",") + std::string("\"") + c.name() + "\":{\"fps\":" + std::to_string(fps) +
                ",\"state\":\"" + c.state() + "\",\"restarts\":" + std::to_string(c.restarts()) +
                ",\"damaged_dropped\":" + std::to_string(dropped) + c.stats_json();
        if (balance) {
            const CamCorrection& k = balance->corrections()[&cp - cams.data()];
            json += ",\"balance\":{\"gain\":" + std::to_string(k.gain) + ",\"u\":" + std::to_string(k.u) +
                    ",\"v\":" + std::to_string(k.v) + "}";
        }
        json += "}";
        first = false;
    }
    json += "}";
    if (balance) json += ",\"balance_points\":" + std::to_string(balance->used());
    json += extra + "}\n";
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
    std::unique_ptr<PhotoBalance> balance;
    if (o.balance && lut.cams.size() > 1) {   // one camera has nothing to match
        balance = std::make_unique<PhotoBalance>(lut);
        std::fprintf(stderr, "balance: %d overlap points\n", balance->samples());
    }
    EncoderConfig ec = o.enc;
    ec.width = ec.height = lut.size;

    // Table order; cameras not given on the command line stay null (missing)
    std::vector<std::unique_ptr<FrameSource>> cams(lut.cams.size());
    for (const auto& [name, dev] : o.cameras) {
        const int c = lut.camera_index(name);
        if (c < 0) throw std::runtime_error("camera " + name + " is not in " + o.lut);
        cams[c] = make_source(o, name, dev);
    }
    FramePool pool(lut.size, lut.size, 4);
    Encoder enc(ec);
    std::unique_ptr<Snapshot> snap;
    if (!o.snapshot.empty()) snap = std::make_unique<Snapshot>(o.snapshot, lut.size, lut.size);

    // Full-detail stills: the same cameras through a bigger table, on request only
    std::unique_ptr<GpuStitcher> full;
    std::unique_ptr<FramePool> full_pool;
    std::unique_ptr<Snapshot> full_snap;
    if (!o.full_lut.empty() && !o.full_snapshot.empty()) {
        const GridLut fl = GridLut::load(o.full_lut);
        for (size_t c = 0; c < lut.cams.size(); ++c)
            if (fl.camera_index(lut.cams[c].name) != static_cast<int>(c))
                throw std::runtime_error(o.full_lut + " doesn't list the cameras like " + o.lut);
        full = std::make_unique<GpuStitcher>(fl);
        full_pool = std::make_unique<FramePool>(fl.size, fl.size, 1);
        full_snap = std::make_unique<Snapshot>(o.full_snapshot, fl.size, fl.size);
        std::fprintf(stderr, "full-detail stills: %dx%d\n", fl.size, fl.size);
    }

    // Zoom stream: started when the page asks, stopped a while after it stops asking
    std::unique_ptr<Encoder> zoom_enc;
    std::unique_ptr<FramePool> zoom_pool;
    View zoom_view{o.zoom_w, o.zoom_h, 0.f, 0.f, 1.f};
    Clock::time_point zoom_last{};
    time_t zoom_mtime = 0;

    // Ground mask
    time_t horizon_mtime = -1;
    bool horizon_on = false;

    // Night stretch
    time_t stretch_mtime = -1;
    float stretch_black = 0.f, stretch_k = 0.f;

    // Stacking (D-026): the stitch reads `views`, each camera's stack when it's on
    Stacker stacker(static_cast<int>(lut.cams.size()));
    time_t stack_mtime = -1;
    int stack_depth = 0;
    std::vector<uint64_t> stacked_seq(lut.cams.size(), 0);
    std::vector<PlanarImage> views(lut.cams.size());
    cudaStream_t stream;
    cuda_check(cudaStreamCreate(&stream), "cudaStreamCreate");

    std::fprintf(stderr, "skydome-live: %dx%d dome at %d fps from %zu camera(s), table %s\n", lut.size, lut.size,
                 ec.fps, o.cameras.size(), o.lut.c_str());

    const auto period = std::chrono::nanoseconds(1'000'000'000LL / ec.fps);
    auto next_tick = Clock::now();
    auto next_report = next_tick + std::chrono::seconds(2);
    auto next_balance = next_tick;
    auto next_files = next_tick;
    Stats st;
    std::vector<PlanarImage> inputs(lut.cams.size());
    long produced = 0;

    while (!g_stop.load()) {
        const auto now = Clock::now();
        for (auto& c : cams)
            if (c) c->poll(now);
        enc.poll(now);

        if (now >= next_files) {
            next_files = now + std::chrono::milliseconds(250);
            if (!o.horizon.empty()) {
                const time_t m = mtime(o.horizon);
                if (m != horizon_mtime) {
                    horizon_mtime = m;
                    const std::vector<float> h = m ? read_horizon(o.horizon) : std::vector<float>{};
                    stitcher.set_horizon(h);
                    if (full) full->set_horizon(h);
                    if (horizon_on != !h.empty())
                        std::fprintf(stderr, "ground mask %s\n", h.empty() ? "off" : "on");
                    horizon_on = !h.empty();
                }
            }
            if (!o.stretch.empty()) {
                const time_t m = mtime(o.stretch);
                if (m != stretch_mtime) {
                    stretch_mtime = m;
                    float b = 0.f, k = 0.f;
                    std::ifstream f(o.stretch);
                    if (!(m && f >> b >> k)) b = k = 0.f;
                    stretch_black = b;
                    stretch_k = k;
                    stitcher.set_stretch(b, k);
                    if (full) full->set_stretch(b, k);
                    std::fprintf(stderr, "night stretch: black %.0f, k %.1f%s\n", b, k, k > 0.f ? "" : " (off)");
                }
            }
            if (!o.stack.empty()) {
                const time_t m = mtime(o.stack);
                if (m != stack_mtime) {
                    stack_mtime = m;
                    int n = 0;
                    std::ifstream f(o.stack);
                    if (!(m && f >> n)) n = 0;
                    stack_depth = std::max(0, std::min(n, 600));
                    stacker.reset();   // new depth or new settings: start again
                    std::fill(stacked_seq.begin(), stacked_seq.end(), 0);
                    std::fprintf(stderr, "stacking: %d frames%s\n", stack_depth, stack_depth > 1 ? "" : " (off)");
                }
            }
            if (!o.raw_dir.empty() && mtime(o.raw_dir + "/request")) {
                std::remove((o.raw_dir + "/request").c_str());
                for (auto& c : cams)
                    if (c) c->request_raw(o.raw_dir + "/" + c->name());
            }
            if (!o.zoom_file.empty()) {
                const time_t m = mtime(o.zoom_file);
                float r[4];
                if (m && std::time(nullptr) - m <= kZoomFreshS) {
                    if (m != zoom_mtime && read_zoom(o.zoom_file, r)) {
                        zoom_view.x0 = r[0] * lut.size;
                        zoom_view.y0 = r[1] * lut.size;
                        zoom_view.scale = (r[2] - r[0]) * lut.size / o.zoom_w;
                    }
                    zoom_mtime = m;
                    zoom_last = now;
                    if (!zoom_enc) {
                        EncoderConfig zc;
                        zc.width = o.zoom_w;
                        zc.height = o.zoom_h;
                        zc.fps = ec.fps;
                        zc.bitrate_kbps = o.zoom_bitrate;
                        zc.sink = o.zoom_sink;
                        zc.encode = ec.encode;
                        zoom_enc = std::make_unique<Encoder>(zc);
                        if (!zoom_pool) zoom_pool = std::make_unique<FramePool>(o.zoom_w, o.zoom_h, 3);
                    }
                } else if (zoom_enc && now - zoom_last > kZoomLinger) {
                    zoom_enc.reset();
                    std::fprintf(stderr, "[zoom] stopped\n");
                }
            }
        }
        if (zoom_enc) zoom_enc->poll(now);

        bool any = false;
        for (size_t c = 0; c < cams.size(); ++c) {
            inputs[c] = (cams[c] && cams[c]->has_frame()) ? cams[c]->image() : PlanarImage{};
            any |= inputs[c].plane[0] != nullptr;
        }
        if (any) {
            for (size_t c = 0; c < cams.size(); ++c) {
                views[c] = inputs[c];
                if (stack_depth < 2 || !inputs[c].plane[0]) continue;
                const uint64_t seq = cams[c]->frame_seq();
                if (seq != stacked_seq[c]) {   // a new picture from this camera: blend it in
                    stacker.add(static_cast<int>(c), inputs[c], stack_depth, stream);
                    stacked_seq[c] = seq;
                }
                views[c] = stacker.view(static_cast<int>(c), inputs[c]);
            }
            if (balance && now >= next_balance) {
                // Once a second, a quarter of the way: a few seconds to settle, no flicker
                if (balance->update(inputs.data(), stream, 0.25f, stretch_k > 0.f ? stretch_black : 0.f)) {
                    stitcher.set_corrections(balance->corrections());
                    if (full) full->set_corrections(balance->corrections());
                }
                next_balance = now + std::chrono::seconds(1);
            }
            if (FramePool::Frame* f = pool.acquire()) {
                const auto t0 = Clock::now();
                stitcher.run(views.data(), f->dev, stream);
                cuda_check(cudaStreamSynchronize(stream), "stitch");
                const double ms = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
                st.stitch_ms += ms;
                st.stitch_ms_max = std::max(st.stitch_ms_max, ms);
                if (snap && snap->requested()) snap->take(f->host, f->bytes);
                if (full_snap && full_snap->requested()) {
                    // Rare and slow (~40 ms): only when someone asks
                    FramePool::Frame* ff = full_pool->acquire();
                    full->run(views.data(), ff->dev, stream);
                    cuda_check(cudaStreamSynchronize(stream), "full-detail stitch");
                    full_snap->take(ff->host, ff->bytes);
                    FramePool::release(ff);
                }
                if (!enc.push(f)) ++st.push_failed;
                ++st.out_frames;
                if (o.max_frames && ++produced >= o.max_frames) break;
            } else {
                ++st.skipped;   // the encoder is behind: all output frames still in use
            }
            if (zoom_enc) {
                if (FramePool::Frame* zf = zoom_pool->acquire()) {
                    stitcher.run_view(views.data(), zf->dev, zoom_view, stream);
                    cuda_check(cudaStreamSynchronize(stream), "zoom stitch");
                    zoom_enc->push(zf);
                }
            }
        }

        if (now >= next_report) {
            const std::string extra = std::string(",\"zoom\":") + (zoom_enc ? "true" : "false") +
                                      ",\"ground_mask\":" + (horizon_on ? "true" : "false") +
                                      ",\"stretch\":{\"black\":" + std::to_string(stretch_black) +
                                      ",\"k\":" + std::to_string(stretch_k) + "}";
            std::string stack_json = ",\"stack\":{\"depth\":" + std::to_string(stack_depth) + ",\"frames\":{";
            for (size_t c = 0; c < lut.cams.size(); ++c)
                stack_json += (c ? ",\"" : "\"") + lut.cams[c].name + "\":" + std::to_string(stacker.count(static_cast<int>(c)));
            stack_json += "}}";
            report(o, cams, enc, balance.get(), extra + stack_json, st, now);
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
