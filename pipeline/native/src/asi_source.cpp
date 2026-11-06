#include "asi_source.h"

#include <ASICamera2.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <sys/stat.h>

#include <cuda_runtime.h>

namespace {

constexpr auto kBackoff = std::chrono::seconds(3);
constexpr int kWaitMs = 500;   // short waits for a frame, so stopping and new settings aren't held up

time_t file_mtime(const std::string& path) {
    struct stat st{};
    return stat(path.c_str(), &st) == 0 ? st.st_mtime : 0;
}

std::string json_str(const char* s) {
    std::string out = "\"";
    for (; *s; ++s) {
        if (*s == '"' || *s == '\\') out += '\\';
        if (static_cast<unsigned char>(*s) >= 0x20) out += *s;
    }
    return out + "\"";
}

}  // namespace

AsiSettings AsiSettings::load(const std::string& path) {
    AsiSettings s;
    std::ifstream f(path);
    std::string line;
    while (std::getline(f, line)) {
        line = line.substr(0, line.find('#'));
        const auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string key = line.substr(0, colon);
        key.erase(std::remove_if(key.begin(), key.end(), [](unsigned char c) { return std::isspace(c); }), key.end());
        long v = 0;
        std::istringstream vs(line.substr(colon + 1));
        if (!(vs >> v)) continue;
        if (key == "auto_exposure") s.auto_exposure = v != 0;
        else if (key == "exposure_us") s.exposure_us = v;
        else if (key == "gain") s.gain = static_cast<int>(v);
        else if (key == "offset") s.offset = static_cast<int>(v);
        else if (key == "wb_red") s.wb_red = static_cast<int>(v);
        else if (key == "wb_blue") s.wb_blue = static_cast<int>(v);
        else if (key == "gamma") s.gamma = static_cast<int>(v);
        else if (key == "auto_max_exposure_ms") s.auto_max_exposure_ms = v;
        else if (key == "auto_max_gain") s.auto_max_gain = static_cast<int>(v);
        else if (key == "auto_target") s.auto_target = static_cast<int>(v);
        else if (key == "bandwidth") s.bandwidth = static_cast<int>(v);
        else if (key == "flip") s.flip = static_cast<int>(v);
    }
    return s;
}

AsiSource::AsiSource(std::string name, std::string which, std::string settings_file, std::string caps_file)
    : name_(std::move(name)), which_(std::move(which)), settings_file_(std::move(settings_file)),
      caps_file_(std::move(caps_file)) {
    thread_ = std::thread([this] { run(); });
}

AsiSource::~AsiSource() {
    stop_.store(true);
    if (thread_.joinable()) thread_.join();
    cudaFree(d_raw_);
    cudaFree(d_tap_);
    for (int k = 0; k < 3; ++k) {
        cudaFree(d_f_[k]);
        cudaFree(d_8_[k]);
    }
}

const char* AsiSource::state() const {
    switch (state_.load()) {
        case 1: return "live";
        case 2: return "restarting";
        default: return "waiting";
    }
}

void AsiSource::sleep_for(std::chrono::milliseconds d) {
    const auto until = Clock::now() + d;
    while (!stop_.load() && Clock::now() < until) std::this_thread::sleep_for(std::chrono::milliseconds(100));
}

// ---- capture thread ---------------------------------------------------------------

bool AsiSource::open_camera() {
    const int n = ASIGetNumOfConnectedCameras();
    ASI_CAMERA_INFO info{};
    int index = -1;
    for (int i = 0; i < n && index < 0; ++i) {
        if (ASIGetCameraProperty(&info, i) != ASI_SUCCESS) continue;
        if (which_.empty() || std::strstr(info.Name, which_.c_str())) index = i;
    }
    if (index < 0) return false;
    const int id = info.CameraID;
    if (ASIOpenCamera(id) != ASI_SUCCESS || ASIInitCamera(id) != ASI_SUCCESS) {
        ASICloseCamera(id);
        std::fprintf(stderr, "[%s] %s: couldn't open it (udev rule installed? see tools/get_zwo_sdk.sh)\n",
                     name_.c_str(), info.Name);
        return false;
    }
    const int w = static_cast<int>(info.MaxWidth) & ~1, h = static_cast<int>(info.MaxHeight) & ~1;
    if (ASISetROIFormat(id, w, h, 1, ASI_IMG_RAW16) != ASI_SUCCESS) {
        std::fprintf(stderr, "[%s] %s: no 16-bit raw at %dx%d\n", name_.c_str(), info.Name, w, h);
        ASICloseCamera(id);
        return false;
    }
    int nc = 0;
    ASIGetNumOfControls(id, &nc);
    for (int i = 0; i < nc; ++i) {
        ASI_CONTROL_CAPS c{};
        if (ASIGetControlCaps(id, i, &c) != ASI_SUCCESS) continue;
        if (c.ControlType == ASI_EXPOSURE) {
            exp_min_ = c.MinValue;
            exp_max_ = c.MaxValue;
        } else if (c.ControlType == ASI_GAIN) {
            gain_min_ = static_cast<int>(c.MinValue);
            gain_max_ = static_cast<int>(c.MaxValue);
        } else if (c.ControlType == ASI_OFFSET) {
            offset_default_ = static_cast<int>(c.DefaultValue);
        }
    }
    id_ = id;
    host_.assign(static_cast<size_t>(w) * h, 0);
    {
        std::lock_guard<std::mutex> lock(m_);
        camera_name_ = info.Name;
        width_ = w;
        height_ = h;
        bayer_ = info.IsColorCam ? info.BayerPattern : -1;
        ready_.assign(host_.size(), 0);
        open_ = true;
    }
    write_caps();
    ae_.min_us = static_cast<double>(exp_min_);
    controls_dirty_ = true;
    settings_mtime_ = -1;   // apply everything to the fresh camera
    load_settings();
    apply_controls();
    if (ASIStartVideoCapture(id) != ASI_SUCCESS) {
        close_camera();
        return false;
    }
    ASIGetDroppedFrames(id, &last_dropped_total_);
    last_frame_ = Clock::now();
    std::fprintf(stderr, "[%s] %s: %dx%d 16-bit raw, %s\n", name_.c_str(), info.Name, w, h,
                 info.IsUSB3Host ? "USB 3" : "USB 2 (slow: use a USB 3 port)");
    return true;
}

void AsiSource::close_camera() {
    if (id_ < 0) return;
    ASIStopVideoCapture(id_);
    ASICloseCamera(id_);
    id_ = -1;
    std::lock_guard<std::mutex> lock(m_);
    open_ = false;
}

void AsiSource::write_caps() {
    if (caps_file_.empty() || id_ < 0) return;
    std::string json = "{\"camera\":";
    {
        std::lock_guard<std::mutex> lock(m_);
        json += json_str(camera_name_.c_str()) + ",\"width\":" + std::to_string(width_) +
                ",\"height\":" + std::to_string(height_) + ",\"bayer\":" + std::to_string(bayer_);
    }
    json += ",\"controls\":{";
    int nc = 0;
    ASIGetNumOfControls(id_, &nc);
    bool first = true;
    for (int i = 0; i < nc; ++i) {
        ASI_CONTROL_CAPS c{};
        if (ASIGetControlCaps(id_, i, &c) != ASI_SUCCESS) continue;
        json += (first ? "" : ",") + json_str(c.Name) + ":{\"min\":" + std::to_string(c.MinValue) +
                ",\"max\":" + std::to_string(c.MaxValue) + ",\"default\":" + std::to_string(c.DefaultValue) +
                ",\"auto\":" + (c.IsAutoSupported ? "true" : "false") +
                ",\"writable\":" + (c.IsWritable ? "true" : "false") + "}";
        first = false;
    }
    json += "}";
    int hdr_offset = 0, unity_offset = 0, lowest_rn_gain = 0, lowest_rn_offset = 0;
    if (ASIGetGainOffset(id_, &hdr_offset, &unity_offset, &lowest_rn_gain, &lowest_rn_offset) == ASI_SUCCESS)
        json += ",\"gain_offset\":{\"highest_dr_offset\":" + std::to_string(hdr_offset) +
                ",\"unity_gain_offset\":" + std::to_string(unity_offset) +
                ",\"lowest_rn_gain\":" + std::to_string(lowest_rn_gain) +
                ",\"lowest_rn_offset\":" + std::to_string(lowest_rn_offset) + "}";
    json += ",\"sdk\":" + json_str(ASIGetSDKVersion()) + "}\n";
    const std::string tmp = caps_file_ + ".tmp";
    std::ofstream(tmp) << json;
    std::rename(tmp.c_str(), caps_file_.c_str());
}

void AsiSource::load_settings() {
    const time_t m = settings_file_.empty() ? 0 : file_mtime(settings_file_);
    if (m == settings_mtime_) return;
    settings_mtime_ = m;
    const AsiSettings s = m ? AsiSettings::load(settings_file_) : AsiSettings{};
    bool to_auto;
    {
        std::lock_guard<std::mutex> lock(m_);
        to_auto = s.auto_exposure && !settings_.auto_exposure;
        settings_ = s;
    }
    if (to_auto)   // carry on from where manual left the picture
        ae_.set_index(static_cast<double>(active_.exposure_us) * AutoExposure::linear(active_.gain));
    controls_dirty_ = true;
}

void AsiSource::apply_controls() {
    AsiSettings s;
    {
        std::lock_guard<std::mutex> lock(m_);
        s = settings_;
    }
    ae_.max_us = std::clamp(static_cast<double>(s.auto_max_exposure_ms) * 1000.0, static_cast<double>(exp_min_),
                            static_cast<double>(exp_max_));
    ae_.max_gain = std::clamp(s.auto_max_gain, gain_min_, gain_max_);
    ae_.target = std::clamp(s.auto_target, 1, 90) / 100.0;
    AutoExposure::Setting want = s.auto_exposure
        ? ae_.setting()
        : AutoExposure::Setting{std::clamp(s.exposure_us, exp_min_, exp_max_), std::clamp(s.gain, gain_min_, gain_max_)};
    if (controls_dirty_) {
        ASISetControlValue(id_, ASI_OFFSET, s.offset >= 0 ? s.offset : offset_default_, ASI_FALSE);
        ASISetControlValue(id_, ASI_BANDWIDTHOVERLOAD, std::clamp(s.bandwidth, 40, 100), ASI_FALSE);
        ASISetControlValue(id_, ASI_FLIP, std::clamp(s.flip, 0, 3), ASI_FALSE);
        ASISetControlValue(id_, ASI_HIGH_SPEED_MODE, 0, ASI_FALSE);   // full bit depth
        controls_dirty_ = false;
        active_ = {-1, -1};   // set both below
    }
    if (want.exposure_us == active_.exposure_us && want.gain == active_.gain) return;
    // A long frame on its way would hold up a much shorter one (dawn, or a
    // manual change): start the video again rather than wait it out
    const bool restart = active_.exposure_us >= 2'000'000 && want.exposure_us < active_.exposure_us / 4;
    if (restart) ASIStopVideoCapture(id_);
    ASISetControlValue(id_, ASI_EXPOSURE, want.exposure_us, ASI_FALSE);
    ASISetControlValue(id_, ASI_GAIN, want.gain, ASI_FALSE);
    if (restart) ASIStartVideoCapture(id_);
    ae_.changed(want);
    // The frame already exposing was taken with the old values
    older_pending_ = !restart && active_.exposure_us > 0;
    older_ = active_;
    active_ = want;
}

double AsiSource::sky_level(const std::vector<uint16_t>& raw) const {
    // The median of the middle of the picture (the sky circle), every 7th
    // pixel each way: odd, so all four Bayer colours are in it
    std::vector<uint16_t> v;
    int w, h;
    {
        std::lock_guard<std::mutex> lock(m_);
        w = width_;
        h = height_;
    }
    const double cx = w / 2.0, cy = h / 2.0, r = 0.45 * std::min(w, h);
    v.reserve(static_cast<size_t>(3.2 * (r / 7) * (r / 7)));
    for (int y = static_cast<int>(cy - r); y < cy + r; y += 7)
        for (int x = static_cast<int>(cx - r); x < cx + r; x += 7)
            if ((x - cx) * (x - cx) + (y - cy) * (y - cy) < r * r) v.push_back(raw[static_cast<size_t>(y) * w + x]);
    if (v.empty()) return 0;
    std::nth_element(v.begin(), v.begin() + v.size() / 2, v.end());
    return v[v.size() / 2] / 65535.0;
}

void AsiSource::on_frame() {
    const AutoExposure::Setting taken = older_pending_ ? older_ : active_;
    older_pending_ = false;
    // Changing a long exposure spoils the next frame: nearly black, and long
    // before any exposure could have ended. It would darken the picture, and
    // the automatic exposure would chase it, change again, and spoil another
    const auto arrived = Clock::now();
    const bool spoiled = taken.exposure_us >= 1'000'000 &&
                         std::chrono::duration<double>(arrived - last_frame_).count() < taken.exposure_us / 2e6;
    if (spoiled) {
        last_frame_ = arrived;
        return;
    }
    const double level = sky_level(host_);
    // The frame's middle: it arrived as its exposure ended
    const double t_mid = std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count() -
                         taken.exposure_us / 2e6;
    bool auto_on;
    {
        std::lock_guard<std::mutex> lock(m_);
        auto_on = settings_.auto_exposure;
        std::swap(host_, ready_);
        ++ready_seq_;
        // poll() trades ready_ for its upload buffer, empty at first: keep ours whole
        if (host_.size() != ready_.size()) host_.assign(ready_.size(), 0);
        shown_ = taken;
        shown_time_ = t_mid;
        level_ = level;
    }
    if (auto_on) ae_.update(level, taken);
    frames_.fetch_add(1);
    last_frame_ = Clock::now();
}

void AsiSource::run() {
    while (!stop_.load()) {
        if (id_ < 0) {
            state_.store(0);
            if (!open_camera()) {
                sleep_for(kBackoff);
                continue;
            }
            state_.store(1);
        }
        load_settings();
        apply_controls();
        const ASI_ERROR_CODE err =
            ASIGetVideoData(id_, reinterpret_cast<unsigned char*>(host_.data()), static_cast<long>(host_.size() * 2), kWaitMs);
        const auto now = Clock::now();
        if (err == ASI_SUCCESS) {
            on_frame();
        } else if (err == ASI_ERROR_TIMEOUT) {
            // Nothing for twice the exposure and 15 s more: start the camera again
            const auto limit = std::chrono::microseconds(2 * std::max(active_.exposure_us, 0L)) + std::chrono::seconds(15);
            if (now - last_frame_ > limit) {
                std::fprintf(stderr, "[%s] no frames for %.0f s: reopening\n", name_.c_str(),
                             std::chrono::duration<double>(now - last_frame_).count());
                close_camera();
                restarts_.fetch_add(1);
                state_.store(2);
                sleep_for(kBackoff);
            }
        } else {
            std::fprintf(stderr, "[%s] camera error %d (unplugged?): reopening\n", name_.c_str(), static_cast<int>(err));
            close_camera();
            restarts_.fetch_add(1);
            state_.store(2);
            sleep_for(kBackoff);
            continue;
        }
        if (id_ >= 0 && now >= next_housekeeping_) {
            next_housekeeping_ = now + std::chrono::seconds(5);
            // No sky is white at the camera's shortest: it hasn't got the
            // setting (seen at dawn on the ASI678MC). Sending everything again cures it
            {
                std::lock_guard<std::mutex> lock(m_);
                if (settings_.auto_exposure && active_.exposure_us <= exp_min_ && active_.gain <= gain_min_ && level_ > 0.95) {
                    std::fprintf(stderr, "[%s] white at the shortest exposure: sending the settings again\n", name_.c_str());
                    controls_dirty_ = true;
                }
            }
            long t = 0;
            ASI_BOOL a = ASI_FALSE;
            if (ASIGetControlValue(id_, ASI_TEMPERATURE, &t, &a) == ASI_SUCCESS) {
                std::lock_guard<std::mutex> lock(m_);
                temperature_c_ = t / 10.0;
            }
            int total = 0;
            if (ASIGetDroppedFrames(id_, &total) == ASI_SUCCESS) {
                if (total > last_dropped_total_) dropped_.fetch_add(total - last_dropped_total_);
                last_dropped_total_ = total;
            }
        }
    }
    close_camera();
}

// ---- main thread ------------------------------------------------------------------

void AsiSource::ensure_buffers(int w, int h) {
    if (w == dev_w_ && h == dev_h_) return;
    cudaFree(d_raw_);
    for (int k = 0; k < 3; ++k) {
        cudaFree(d_f_[k]);
        cudaFree(d_8_[k]);
    }
    const size_t full = static_cast<size_t>(w) * h, half = full / 4;
    cuda_check(cudaMalloc(&d_raw_, full * sizeof(uint16_t)), "cudaMalloc raw");
    for (int k = 0; k < 3; ++k) {
        const size_t n = k ? half : full;
        cuda_check(cudaMalloc(&d_f_[k], n * sizeof(float)), "cudaMalloc planes");
        cuda_check(cudaMalloc(&d_8_[k], n), "cudaMalloc planes");
    }
    dev_w_ = w;
    dev_h_ = h;
    image_ = PlanarImage{};
    for (int k = 0; k < 3; ++k) {
        const int pw = k ? w / 2 : w, ph = k ? h / 2 : h;
        image_.plane[k] = d_8_[k];
        image_.pitch[k] = pw;
        image_.width[k] = pw;
        image_.height[k] = ph;
        image_.fplane[k] = d_f_[k];
        image_.fpitch[k] = pw;
    }
}

void AsiSource::poll(Clock::time_point) {
    int w, h, bayer;
    DebayerParams p;
    AutoExposure::Setting taken;
    double t_mid;
    {
        std::lock_guard<std::mutex> lock(m_);
        if (!open_) has_ = false;   // the camera's gone: leave it out of the dome until it's back
        if (ready_seq_ == uploaded_seq_) return;
        std::swap(ready_, upload_);
        uploaded_seq_ = ready_seq_;
        w = width_;
        h = height_;
        bayer = bayer_;
        p.wb_r = settings_.wb_red / 100.f;
        p.wb_b = settings_.wb_blue / 100.f;
        p.gamma = std::max(settings_.gamma, 10) / 100.f;
        taken = shown_;
        t_mid = shown_time_;
    }
    if (upload_.size() != static_cast<size_t>(w) * h) return;   // reopened at another size meanwhile
    ensure_buffers(w, h);
    // A mono camera would want a plain copy, not a demosaic: not done (the ASI676MC is colour)
    if (bayer >= 0) bayer_red(bayer, p.rx, p.ry);
    cuda_check(cudaMemcpy(d_raw_, upload_.data(), upload_.size() * sizeof(uint16_t), cudaMemcpyHostToDevice),
               "upload raw");
    debayer_to_yuv(d_raw_, w, h, w, p, d_f_[0], d_f_[1], d_f_[2], d_8_[0], d_8_[1], d_8_[2], nullptr);
    cuda_check(cudaStreamSynchronize(nullptr), "debayer");
    ++seq_;
    has_ = true;
    if (raw_wanted_) {
        raw_wanted_ = false;
        write_pgm(raw_base_ + ".pgm");
    }
    const auto now = Clock::now();
    if (!tap_dir_.empty() && now >= next_tap_) {
        next_tap_ = now + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(tap_period_s_));
        write_tap(taken, t_mid);
    }
}

void AsiSource::set_tap(const std::string& dir, double max_fps) {
    tap_dir_ = dir;
    tap_period_s_ = max_fps > 0 ? 1.0 / max_fps : 0;
}

void AsiSource::write_tap(const AutoExposure::Setting& s, double t_mid) {
    const int hw = dev_w_ / 2, hh = dev_h_ / 2;
    const size_t n = static_cast<size_t>(hw) * hh;
    if (!d_tap_ || tap_host_.size() != n) {
        cudaFree(d_tap_);
        cuda_check(cudaMalloc(&d_tap_, n * sizeof(uint16_t)), "cudaMalloc tap");
        tap_host_.assign(n, 0);
    }
    luma_half_u16(d_f_[0], dev_w_, dev_h_, d_tap_, nullptr);
    cuda_check(cudaMemcpy(tap_host_.data(), d_tap_, n * sizeof(uint16_t), cudaMemcpyDeviceToHost), "download tap");
    // The picture first, then its description: the watcher reads the JSON to know a new frame is there
    const std::string base = tap_dir_ + "/" + name_;
    if (FILE* f = std::fopen((base + ".y16.tmp").c_str(), "wb")) {
        const bool ok = std::fwrite(tap_host_.data(), sizeof(uint16_t), n, f) == n;
        if (std::fclose(f) != 0 || !ok) return;
        std::rename((base + ".y16.tmp").c_str(), (base + ".y16").c_str());
    } else {
        return;
    }
    float gamma;
    {
        std::lock_guard<std::mutex> lock(m_);
        gamma = settings_.gamma / 100.f;
    }
    char json[320];
    std::snprintf(json, sizeof json,
                  "{\"seq\":%lu,\"time\":%.3f,\"exposure_us\":%ld,\"gain\":%d,\"width\":%d,\"height\":%d,"
                  "\"scale\":256,\"gamma\":%.2f,\"full_width\":%d,\"full_height\":%d}\n",
                  static_cast<unsigned long>(seq_), t_mid, s.exposure_us, s.gain, hw, hh, gamma, dev_w_, dev_h_);
    const std::string tmp = base + ".json.tmp";
    std::ofstream(tmp) << json;
    std::rename(tmp.c_str(), (base + ".json").c_str());
}

void AsiSource::request_raw(const std::string& base) {
    raw_base_ = base;
    raw_wanted_ = true;
}

void AsiSource::write_pgm(const std::string& path) {
    const size_t n = static_cast<size_t>(dev_w_) * dev_h_;
    std::vector<float> y(n);
    cuda_check(cudaMemcpy(y.data(), d_f_[0], n * sizeof(float), cudaMemcpyDeviceToHost), "download luma");
    std::vector<uint8_t> be(2 * n);   // PGM's 16 bits are big-endian
    for (size_t i = 0; i < n; ++i) {
        const float v = std::min(std::max(y[i] * (65535.f / 255.f) + 0.5f, 0.f), 65535.f);
        const auto q = static_cast<uint16_t>(v);
        be[2 * i] = static_cast<uint8_t>(q >> 8);
        be[2 * i + 1] = static_cast<uint8_t>(q & 0xff);
    }
    const std::string tmp = path + ".tmp";
    if (FILE* f = std::fopen(tmp.c_str(), "wb")) {
        std::fprintf(f, "P5\n%d %d\n65535\n", dev_w_, dev_h_);
        const bool ok = std::fwrite(be.data(), 1, be.size(), f) == be.size();
        if (std::fclose(f) == 0 && ok) std::rename(tmp.c_str(), path.c_str());
    }
}

std::string AsiSource::stats_json() const {
    std::lock_guard<std::mutex> lock(m_);
    char buf[320];
    std::snprintf(buf, sizeof buf,
                  ",\"camera\":%s,\"exposure_us\":%ld,\"gain\":%d,\"auto_exposure\":%s,\"sky_level\":%.4f,"
                  "\"temperature_c\":%.1f,\"width\":%d,\"height\":%d",
                  json_str(camera_name_.c_str()).c_str(), shown_.exposure_us, shown_.gain,
                  settings_.auto_exposure ? "true" : "false", level_, temperature_c_, width_, height_);
    return buf;
}
