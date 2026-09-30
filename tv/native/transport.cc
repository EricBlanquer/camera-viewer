#include <cstring>
#include <cstdio>
#include <deque>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include "video_decoder.h"
#include "ppapi/c/pp_errors.h"
#include "ppapi/cpp/instance.h"
#include "ppapi/cpp/module.h"
#include "ppapi/cpp/net_address.h"
#include "ppapi/cpp/udp_socket.h"
#include "ppapi/cpp/var_array_buffer.h"
#include "ppapi/cpp/var_dictionary.h"
#include "ppapi/utility/completion_callback_factory.h"

namespace {
const size_t MAX_FRAME_BYTES = 8 * 1024 * 1024;
uint16_t NetworkOrder(uint16_t value) { return static_cast<uint16_t>((value >> 8) | (value << 8)); }
std::string BufferBytes(const pp::Var& value) {
  if (!value.is_array_buffer()) return std::string();
  pp::VarArrayBuffer buffer(value);
  const char* bytes = static_cast<const char*>(buffer.Map());
  std::string data(bytes, buffer.ByteLength());
  buffer.Unmap();
  return data;
}
pp::VarArrayBuffer ArrayBuffer(const char* data, size_t size) {
  pp::VarArrayBuffer buffer(static_cast<uint32_t>(size));
  if (size) std::memcpy(buffer.Map(), data, size);
  buffer.Unmap();
  return buffer;
}
}

class Transport;

class UdpChannel {
 public:
  UdpChannel(Transport* owner, int id);
  ~UdpChannel() { Close(); }
  void Bind();
  void Send(const std::string& host, int port, const std::string& data);
  void Close();
 private:
  void OnBind(int32_t code);
  void Receive();
  void OnReceive(int32_t count, pp::NetAddress address);
  void Flush();
  void OnSend(int32_t count);
  struct Datagram { pp::NetAddress address; std::string data; };
  Transport* owner_;
  int id_;
  pp::UDPSocket socket_;
  pp::CompletionCallbackFactory<UdpChannel> callbacks_;
  char buffer_[65536];
  std::deque<Datagram> queue_;
  bool sending_ = false;
  bool bound_ = false;
};

class Transport : public pp::Instance {
 public:
  explicit Transport(PP_Instance instance) : pp::Instance(instance) {}
  bool Init(uint32_t, const char*[], const char*[]) override {
    if (!pp::UDPSocket::IsAvailable()) { Event("error", "Native UDP sockets unavailable"); return false; }
    Event("transport-ready", "ready");
    return true;
  }
  void HandleMessage(const pp::Var& value) override {
    if (!value.is_dictionary()) return;
    pp::VarDictionary message(value);
    std::string command = message.Get("command").is_string() ? message.Get("command").AsString() : "";
    int id = message.Get("id").is_int() ? message.Get("id").AsInt() : 0;
    int stream = message.Get("stream").is_int() ? message.Get("stream").AsInt() : 0;
    if (stream < 0 || stream > 3) return;
    if (command == "udp-open" && channels_.count(id) == 0 && channels_.size() < 8) {
      channels_[id].reset(new UdpChannel(this, id));
      channels_[id]->Bind();
    } else if (command == "udp-close") {
      channels_.erase(id);
    } else if (command == "udp-send" && channels_.count(id)) {
      pp::Var host = message.Get("host");
      pp::Var port = message.Get("port");
      if (host.is_string() && port.is_int()) channels_[id]->Send(host.AsString(), port.AsInt(), BufferBytes(message.Get("data")));
    } else if (command == "decode-video") {
      pp::Var codec = message.Get("codec");
      pp::Var generation = message.Get("generation");
      pp::Var key = message.Get("key");
      if (!codec.is_int() || (codec.AsInt() != 27 && codec.AsInt() != 36) || !generation.is_int() || !key.is_bool()) return;
      std::string data = BufferBytes(message.Get("data"));
      if (data.empty() || data.size() > MAX_FRAME_BYTES) return;
      if (!decoders_.count(stream)) decoders_[stream].reset(new VideoDecoder(this, stream));
      decoders_[stream]->Submit(data, codec.AsInt(), generation.AsInt(), key.AsBool());
    }
  }
  void Event(const std::string& type, const std::string& detail, int id = 0) {
    pp::VarDictionary event;
    event.Set("type", type);
    event.Set("detail", detail);
    event.Set("id", id);
    PostMessage(event);
  }
  void Datagram(int id, const char* bytes, size_t size, const pp::NetAddress& address) {
    PP_NetAddress_IPv4 ip;
    if (!address.DescribeAsIPv4Address(&ip)) return;
    std::ostringstream host;
    host << static_cast<int>(ip.addr[0]) << '.' << static_cast<int>(ip.addr[1]) << '.' << static_cast<int>(ip.addr[2]) << '.' << static_cast<int>(ip.addr[3]);
    pp::VarDictionary event;
    event.Set("type", "udp-data");
    event.Set("id", id);
    event.Set("host", host.str());
    event.Set("port", static_cast<int>(NetworkOrder(ip.port)));
    event.Set("data", ArrayBuffer(bytes, size));
    PostMessage(event);
  }
 private:
  std::map<int, std::unique_ptr<UdpChannel>> channels_;
  std::map<int, std::unique_ptr<VideoDecoder>> decoders_;
};

UdpChannel::UdpChannel(Transport* owner, int id) : owner_(owner), id_(id), socket_(owner), callbacks_(this) {}
void UdpChannel::Bind() {
  PP_NetAddress_IPv4 ip = {0, {0}};
  int32_t code = socket_.Bind(pp::NetAddress(owner_, ip), callbacks_.NewCallback(&UdpChannel::OnBind));
  if (code != PP_OK_COMPLETIONPENDING) OnBind(code);
}
void UdpChannel::OnBind(int32_t code) {
  if (code != PP_OK) { owner_->Event("error", "UDP bind: " + std::to_string(code), id_); return; }
  bound_ = true;
  PP_NetAddress_IPv4 ip;
  socket_.GetBoundAddress().DescribeAsIPv4Address(&ip);
  owner_->Event("udp-ready", std::to_string(NetworkOrder(ip.port)), id_);
  Receive();
}
void UdpChannel::Receive() {
  auto callback = callbacks_.NewCallbackWithOutput(&UdpChannel::OnReceive);
  socket_.RecvFrom(buffer_, sizeof(buffer_), callback);
}
void UdpChannel::OnReceive(int32_t count, pp::NetAddress address) {
  if (count < 0) { owner_->Event("error", "UDP receive: " + std::to_string(count), id_); return; }
  owner_->Datagram(id_, buffer_, count, address);
  Receive();
}
void UdpChannel::Send(const std::string& host, int port, const std::string& data) {
  if (!bound_ || port < 1 || port > 65535 || data.empty() || data.size() > 65507 || queue_.size() >= 512) return;
  PP_NetAddress_IPv4 ip = {NetworkOrder(static_cast<uint16_t>(port)), {0}};
  unsigned int octets[4];
  char extra;
  if (std::sscanf(host.c_str(), "%u.%u.%u.%u%c", &octets[0], &octets[1], &octets[2], &octets[3], &extra) != 4) return;
  for (int i = 0; i < 4; ++i) { if (octets[i] > 255) return; ip.addr[i] = static_cast<uint8_t>(octets[i]); }
  queue_.push_back({pp::NetAddress(owner_, ip), data});
  if (!sending_) Flush();
}
void UdpChannel::Flush() {
  if (queue_.empty()) { sending_ = false; return; }
  sending_ = true;
  const Datagram& packet = queue_.front();
  int32_t code = socket_.SendTo(packet.data.data(), packet.data.size(), packet.address, callbacks_.NewCallback(&UdpChannel::OnSend));
  if (code != PP_OK_COMPLETIONPENDING) OnSend(code);
}
void UdpChannel::OnSend(int32_t count) {
  if (count < 0) owner_->Event("error", "UDP send: " + std::to_string(count), id_);
  queue_.pop_front();
  Flush();
}
void UdpChannel::Close() { callbacks_.CancelAll(); socket_.Close(); bound_ = false; }

class TransportModule : public pp::Module {
 public:
  pp::Instance* CreateInstance(PP_Instance instance) override { return new Transport(instance); }
};
namespace pp { Module* CreateModule() { return new TransportModule(); } }
