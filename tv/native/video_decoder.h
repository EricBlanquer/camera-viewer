#ifndef CAMERA_VIEWER_VIDEO_DECODER_H
#define CAMERA_VIEWER_VIDEO_DECODER_H
#include <atomic>
#include <cstring>
#include <string>
#include "ppapi/c/pp_errors.h"
#include "ppapi/cpp/instance.h"
#include "ppapi/cpp/var_array_buffer.h"
#include "ppapi/cpp/var_dictionary.h"
#include "ppapi/utility/completion_callback_factory.h"
#include "ppapi/utility/threading/simple_thread.h"
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>
}
class VideoDecoder {
 public:
  VideoDecoder(pp::Instance* owner, int stream) : owner_(owner), stream_(stream), worker_(owner), callbacks_(this) { running_ = worker_.Start(); }
  ~VideoDecoder() {
    if (running_) { worker_.message_loop().PostQuit(true); worker_.Join(); }
    callbacks_.CancelAll();
    Release();
  }
  void Submit(const std::string& bytes, int codec, int generation, bool key, bool burst) {
    if (!running_) { Error("Decoder worker unavailable", generation); return; }
    if (generation != input_generation_) { input_generation_ = generation; waiting_key_ = true; }
    if (pending_.load() >= (burst ? MAX_BURST_PENDING : MAX_PENDING) || pending_bytes_.load() + bytes.size() > MAX_PENDING_BYTES) { waiting_key_ = true; return; }
    if (waiting_key_ && !key) return;
    bool reset = waiting_key_;
    waiting_key_ = false;
    pending_++;
    pending_bytes_ += bytes.size();
    auto callback = callbacks_.NewCallback(&VideoDecoder::Decode, DecodeRequest{bytes, codec, generation, reset});
    if (worker_.message_loop().PostWork(callback) != PP_OK) { Complete(bytes.size()); Error("Decoder worker stopped", generation); }
  }
 private:
  static const int MAX_PENDING = 4;
  static const int MAX_BURST_PENDING = 32;
  static const size_t MAX_PENDING_BYTES = 16 * 1024 * 1024;
  struct DecodeRequest { std::string bytes; int codec; int generation; bool reset; };
  void Complete(size_t size) { pending_bytes_ -= size; pending_--; }
  void Release() {
    avcodec_free_context(&context_);
    av_frame_free(&frame_);
    sws_freeContext(scaler_);
    scaler_ = nullptr;
  }
  void Error(const std::string& message, int generation) {
    pp::VarDictionary event;
    event.Set("type", "decoder-error");
    event.Set("stream", stream_);
    event.Set("generation", generation);
    event.Set("detail", message);
    owner_->PostMessage(event);
  }
  bool Initialize(int codec, int generation) {
    Release();
    av_log_set_level(AV_LOG_QUIET);
    const AVCodec* decoder = avcodec_find_decoder(codec == 36 ? AV_CODEC_ID_HEVC : AV_CODEC_ID_H264);
    if (!decoder) return false;
    context_ = avcodec_alloc_context3(decoder);
    frame_ = av_frame_alloc();
    if (!context_ || !frame_) return false;
    context_->thread_count = 1;
    context_->flags |= AV_CODEC_FLAG_LOW_DELAY;
    if (avcodec_open2(context_, decoder, nullptr) < 0) return false;
    codec_ = codec;
    generation_ = generation;
    return true;
  }
  void Decode(int32_t status, DecodeRequest request) {
    const std::string& bytes = request.bytes;
    int codec = request.codec;
    int generation = request.generation;
    bool reset = request.reset;
    if (status != PP_OK) { Complete(bytes.size()); return; }
    if ((!context_ || codec != codec_ || generation != generation_) && !Initialize(codec, generation)) { Error("Video decoder initialization failed", generation); Complete(bytes.size()); return; }
    else if (reset) avcodec_flush_buffers(context_);
    AVPacket* packet = av_packet_alloc();
    if (!packet || av_new_packet(packet, bytes.size()) < 0) { av_packet_free(&packet); Error("Video decoder allocation failed", generation); Complete(bytes.size()); return; }
    std::memcpy(packet->data, bytes.data(), bytes.size());
    int code = avcodec_send_packet(context_, packet);
    if (code == AVERROR(EAGAIN)) { Drain(generation); code = avcodec_send_packet(context_, packet); }
    av_packet_free(&packet);
    if (code < 0 && code != AVERROR(EAGAIN)) { Error("Camera video could not be decoded", generation); Complete(bytes.size()); return; }
    Drain(generation);
    Complete(bytes.size());
  }
  void Drain(int generation) { while (avcodec_receive_frame(context_, frame_) == 0) Render(generation); }
  void Render(int generation) {
    if (frame_->width < 1 || frame_->height < 1 || frame_->width > 4096 || frame_->height > 4096) { Error("Camera image dimensions are unsupported", generation); return; }
    int width = frame_->width > 960 ? 960 : frame_->width;
    int height = frame_->height * width / frame_->width;
    if (height > 540) { height = 540; width = frame_->width * height / frame_->height; }
    scaler_ = sws_getCachedContext(scaler_, frame_->width, frame_->height, static_cast<AVPixelFormat>(frame_->format), width, height, AV_PIX_FMT_RGBA, SWS_FAST_BILINEAR, nullptr, nullptr, nullptr);
    if (!scaler_) { Error("Camera image conversion failed", generation); return; }
    pp::VarArrayBuffer pixels(width * height * 4);
    uint8_t* output[4] = {static_cast<uint8_t*>(pixels.Map()), nullptr, nullptr, nullptr};
    int stride[4] = {width * 4, 0, 0, 0};
    sws_scale(scaler_, frame_->data, frame_->linesize, 0, frame_->height, output, stride);
    pixels.Unmap();
    pp::VarDictionary event;
    event.Set("type", "video-frame");
    event.Set("stream", stream_);
    event.Set("generation", generation);
    event.Set("width", width);
    event.Set("height", height);
    event.Set("data", pixels);
    owner_->PostMessage(event);
  }
  pp::Instance* owner_;
  int stream_;
  pp::SimpleThread worker_;
  pp::CompletionCallbackFactory<VideoDecoder> callbacks_;
  std::atomic<int> pending_{0};
  std::atomic<size_t> pending_bytes_{0};
  bool waiting_key_ = true;
  bool running_ = false;
  AVCodecContext* context_ = nullptr;
  AVFrame* frame_ = nullptr;
  SwsContext* scaler_ = nullptr;
  int codec_ = -1;
  int generation_ = -1;
  int input_generation_ = -1;
};
#endif
