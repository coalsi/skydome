// The all-sky camera: a ZWO ASI camera (ASI676MC) through ZWO's SDK
// (DECISIONS.md D-029). 16-bit raw frames in video mode on a thread of its
// own (a night frame takes 20 s), demosaicked on the GPU into the planar
// YUV the stitch reads, with float planes that keep the sensor's 12 bits.
//
// Exposure is the camera's own business here (exposure.h): automatic from
// daylight video to long night frames, or manual. Picture settings come
// from a file the web server writes (`key: value` lines, reread when it
// changes); the camera's control ranges go to a JSON file when it opens,
// for the server to offer. Recovers on its own like the UVC cameras: a
// camera that drops off USB is reopened when it's back.
#pragma once

#include <atomic>
#include <cstdint>
#include <ctime>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "debayer.h"
#include "exposure.h"
#include "source.h"

// What the web server can set (var/asi_settings.yaml); units in the names
struct AsiSettings {
    bool auto_exposure = true;
    long exposure_us = 10000;        // manual
    int gain = 0;                    // manual, 0.1 dB
    int offset = -1;                 // black level pedestal; -1 = the camera's default
    int wb_red = 180;                // red and blue gains against green, percent
    int wb_blue = 160;               // (guesses until first light; set from the page)
    int gamma = 220;                 // display gamma x100: 220 for daylight, 100 linear for the night stretch
    long auto_max_exposure_ms = 20000;
    int auto_max_gain = 200;
    int auto_target = 18;            // the sky's median, percent of full scale
    int bandwidth = 80;              // ZWO's USB bandwidth limit, percent
    int flip = 0;                    // 0 none, 1 horizontal, 2 vertical, 3 both

    // Unknown keys and bad lines are ignored; missing keys keep their defaults
    static AsiSettings load(const std::string& path);
};

class AsiSource : public FrameSource {
public:
    // which: "" for the first ZWO camera found, else part of its name (e.g. "676")
    AsiSource(std::string name, std::string which, std::string settings_file, std::string caps_file);
    ~AsiSource() override;
    AsiSource(const AsiSource&) = delete;
    AsiSource& operator=(const AsiSource&) = delete;

    void poll(Clock::time_point now) override;

    const std::string& name() const override { return name_; }
    bool has_frame() const override { return has_; }
    const PlanarImage& image() const override { return image_; }
    const char* state() const override;

    uint64_t take_frame_count() override { return frames_.exchange(0); }
    uint64_t take_dropped() override { return dropped_.exchange(0); }
    uint64_t restarts() const override { return restarts_.load(); }
    uint64_t frame_seq() const override { return seq_; }

    // The next frame's luminance (linear when gamma is 100), 16-bit PGM: base + ".pgm"
    void request_raw(const std::string& base) override;
    std::string stats_json() const override;

private:
    void run();                      // the capture thread
    bool open_camera();
    void close_camera();
    void load_settings();
    void apply_controls();
    void on_frame();
    double sky_level(const std::vector<uint16_t>& raw) const;
    void write_caps();
    void sleep_for(std::chrono::milliseconds d);
    void write_pgm(const std::string& path);
    void ensure_buffers(int w, int h);

    std::string name_, which_, settings_file_, caps_file_;
    std::thread thread_;
    std::atomic<bool> stop_{false};

    // Capture thread only
    int id_ = -1;
    long exp_min_ = 32, exp_max_ = 2000LL * 1000 * 1000;
    int gain_min_ = 0, gain_max_ = 600, offset_default_ = 0;
    AutoExposure ae_;
    AutoExposure::Setting active_{}, older_{};
    bool older_pending_ = false;
    bool controls_dirty_ = true;
    time_t settings_mtime_ = -1;
    Clock::time_point last_frame_{};
    int last_dropped_total_ = 0;
    Clock::time_point next_housekeeping_{};
    std::vector<uint16_t> host_;     // being filled by the SDK

    // Shared with poll(), under m_
    mutable std::mutex m_;
    AsiSettings settings_;
    std::vector<uint16_t> ready_;    // the newest complete frame
    uint64_t ready_seq_ = 0;
    std::string camera_name_;
    int width_ = 0, height_ = 0, bayer_ = 0;
    AutoExposure::Setting shown_{};  // what the newest frame was taken with
    double level_ = 0;               // its sky level
    double temperature_c_ = 0;
    bool open_ = false;

    std::atomic<int> state_{0};      // 0 waiting for a camera, 1 running, 2 backing off
    std::atomic<uint64_t> frames_{0}, dropped_{0}, restarts_{0};

    // Main thread only
    std::vector<uint16_t> upload_;
    uint64_t uploaded_seq_ = 0;
    uint64_t seq_ = 0;
    bool has_ = false;
    int dev_w_ = 0, dev_h_ = 0;
    uint16_t* d_raw_ = nullptr;
    float* d_f_[3] = {nullptr, nullptr, nullptr};
    uint8_t* d_8_[3] = {nullptr, nullptr, nullptr};
    PlanarImage image_{};
    std::string raw_base_;
    bool raw_wanted_ = false;
};
