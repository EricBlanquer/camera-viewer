#!/usr/bin/env bash
set -euo pipefail
tv_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_cache=${CAMERA_TV_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/camera-viewer-tv}
sdk_path=${NACL_SDK_ROOT:-$build_cache/sdk/pepper_63}
ffmpeg_path=${FFMPEG_SRC:-$build_cache/FFmpeg}
tizen_cli=${TIZEN_CLI:-$HOME/tizen-studio/tools/ide/bin/tizen}
ffmpeg_revision=241fe649406143735cb559b53b48044c933801d5
if [[ ! -x "$sdk_path/toolchain/linux_arm_glibc/bin/arm-nacl-g++" ]]; then
  printf '%s\n' 'Set NACL_SDK_ROOT to an extracted Samsung Pepper 63 SDK.' >&2
  exit 1
fi
if [[ ! -d "$ffmpeg_path" ]]; then
  git clone --filter=blob:none --no-checkout https://github.com/FFmpeg/FFmpeg.git "$ffmpeg_path"
  git -C "$ffmpeg_path" fetch --depth=1 origin "$ffmpeg_revision"
  git -C "$ffmpeg_path" checkout --detach "$ffmpeg_revision"
fi
mkdir -p "$tv_root/web/native" "$tv_root/dist"
chmod -R u+w "$tv_root/web/native"
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$sdk_path:/sdk:ro" -v "$ffmpeg_path:/ffmpeg" -v "$tv_root:/app" \
  -w /ffmpeg python:2.7 bash -lc '
  set -euo pipefail
  export PATH=/sdk/toolchain/linux_arm_glibc/bin:$PATH
  if [[ ! -f libavcodec/libavcodec.a || ! -f libswscale/libswscale.a ]]; then
    ./configure --enable-cross-compile --cross-prefix=arm-nacl- --target-os=linux --arch=arm --cpu=generic --disable-asm --disable-runtime-cpudetect --disable-everything --enable-decoder=h264,hevc --enable-parser=h264,hevc --enable-swscale --enable-avcodec --enable-avutil --disable-programs --disable-doc --disable-avdevice --disable-avfilter --disable-avformat --disable-postproc --disable-network --disable-hwaccels --disable-autodetect --enable-small --enable-pthreads --disable-shared --enable-static --extra-cflags=-D_GNU_SOURCE
    make -j"$(nproc)"
  fi
  cd /app
  arm-nacl-g++ -std=gnu++11 -O2 -pthread -I/sdk/include -I/ffmpeg -c native/transport.cc -o dist/transport_armv7.o
  arm-nacl-g++ -pthread dist/transport_armv7.o /ffmpeg/libavcodec/libavcodec.a /ffmpeg/libswscale/libswscale.a /ffmpeg/libavutil/libavutil.a -L/sdk/lib/glibc_arm/Release -lppapi_cpp -lppapi -lm -o web/native/transport_armv7.nexe
  python /sdk/tools/create_nmf.py -D /sdk/toolchain/linux_arm_glibc/bin/arm-nacl-objdump -s web/native -o web/native/transport.nmf web/native/transport_armv7.nexe
  '
"$tizen_cli" build-web -- "$tv_root/web"
"$tizen_cli" package -t wgt -s "${TIZEN_PROFILE:-Blanquer}" -- "$tv_root/web/.buildResult"
cp "$tv_root/web/.buildResult/Camera Viewer.wgt" "$tv_root/dist/camera-viewer.wgt"
printf 'Package: %s\n' "$tv_root/dist/camera-viewer.wgt"
