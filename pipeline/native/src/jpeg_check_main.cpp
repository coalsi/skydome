// skydome-jpeg-check FILE...: run the capture path's frame check on JPEG
// files and print the verdict for each (to compare with djpeg).

#include <cstdio>
#include <fstream>
#include <iterator>
#include <vector>

#include "jpeg_check.h"

int main(int argc, char** argv) {
    int bad = 0;
    for (int i = 1; i < argc; ++i) {
        std::ifstream f(argv[i], std::ios::binary);
        std::vector<uint8_t> d((std::istreambuf_iterator<char>(f)), {});
        const JpegCheck c = check_jpeg(d.data(), d.size());
        bad += c != JpegCheck::Ok;
        std::printf("%s %s\n", argv[i], jpeg_check_name(c));
    }
    return bad ? 1 : 0;
}
