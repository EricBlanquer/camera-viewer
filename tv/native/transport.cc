#include <cstring>
#include <cctype>
#include <cstdio>
#include <deque>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include "video_decoder.h"
#include "tls_client.h"
#include "ppapi/c/pp_errors.h"
#include "ppapi/cpp/instance.h"
#include "ppapi/cpp/module.h"
#include "ppapi/cpp/net_address.h"
#include "ppapi/cpp/udp_socket.h"
#include "ppapi/cpp/tcp_socket.h"
#include "ppapi/cpp/host_resolver.h"
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

class TcpChannel {
 public:
  TcpChannel(Transport* owner, int id);
  ~TcpChannel() { Close(); }
  void Connect(const std::string& host, int port, const std::string& pin, const std::string& seed);
  void Send(const std::string& data);
  void Close();
 private:
  void OnResolve(int32_t code);
  void OnConnect(int32_t code);
  void ProcessTls();
  void Enqueue(const std::string& data);
  void Receive();
  void OnReceive(int32_t count);
  void Flush();
  void OnSend(int32_t count);
  void Fail(const std::string& operation, int32_t code);
  Transport* owner_;
  int id_;
  pp::TCPSocket socket_;
  pp::HostResolver resolver_;
  pp::CompletionCallbackFactory<TcpChannel> callbacks_;
  char buffer_[65536];
  std::deque<std::string> queue_;
  size_t queued_ = 0;
  size_t sent_ = 0;
  bool connected_ = false;
  bool sending_ = false;
  std::unique_ptr<TlsClient> tls_;
};

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
    } else if (command == "tcp-open" && tcp_channels_.count(id) == 0 && tcp_channels_.size() < 8) {
      pp::Var host = message.Get("host");
      pp::Var port = message.Get("port");
      if (!host.is_string() || !port.is_int()) return;
      pp::Var pin = message.Get("certificate_sha256");
      if (!pin.is_undefined() && !pin.is_string()) return;
      tcp_channels_[id].reset(new TcpChannel(this, id));
      tcp_channels_[id]->Connect(host.AsString(), port.AsInt(), pin.is_string() ? pin.AsString() : "", BufferBytes(message.Get("seed")));
    } else if (command == "tcp-close") {
      tcp_channels_.erase(id);
    } else if (command == "tcp-send" && tcp_channels_.count(id)) {
      tcp_channels_[id]->Send(BufferBytes(message.Get("data")));
    } else if (command == "digest-hash") {
      pp::Var data = message.Get("data");
      pp::Var algorithm = message.Get("algorithm");
      pp::Var request = message.Get("request");
      if (!data.is_string() || data.AsString().size() > 8192 || !algorithm.is_string() || !request.is_int()) return;
      if (algorithm.AsString() != "MD5" && algorithm.AsString() != "SHA-256") return;
      pp::VarDictionary event;
      event.Set("type", "digest-hash");
      event.Set("id", id);
      event.Set("request", request);
      event.Set("value", DigestHex(data.AsString(), algorithm.AsString()));
      PostMessage(event);
    } else if (command == "decode-video") {
      pp::Var codec = message.Get("codec");
      pp::Var generation = message.Get("generation");
      pp::Var key = message.Get("key");
      pp::Var burst = message.Get("burst");
      if (!codec.is_int() || (codec.AsInt() != 27 && codec.AsInt() != 36) || !generation.is_int() || !key.is_bool()) return;
      std::string data = BufferBytes(message.Get("data"));
      if (data.empty() || data.size() > MAX_FRAME_BYTES) return;
      if (!decoders_.count(stream)) decoders_[stream].reset(new VideoDecoder(this, stream));
      decoders_[stream]->Submit(data, codec.AsInt(), generation.AsInt(), key.AsBool(), burst.is_bool() && burst.AsBool());
    }
  }
  void TcpData(int id, const char* bytes, size_t size) {
    pp::VarDictionary event;
    event.Set("type", "tcp-data");
    event.Set("id", id);
    event.Set("data", ArrayBuffer(bytes, size));
    PostMessage(event);
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
  std::map<int, std::unique_ptr<TcpChannel>> tcp_channels_;
  std::map<int, std::unique_ptr<VideoDecoder>> decoders_;
};

TcpChannel::TcpChannel(Transport* owner, int id) : owner_(owner), id_(id), socket_(owner), resolver_(owner), callbacks_(this) {}
void TcpChannel::Connect(const std::string& host, int port, const std::string& pin, const std::string& seed) {
  if (!pp::TCPSocket::IsAvailable() || !pp::HostResolver::IsAvailable()) { Fail("unavailable", PP_ERROR_NOTSUPPORTED); return; }
  if (host.empty() || host.size() > 253 || port < 1 || port > 65535) { Fail("address", PP_ERROR_BADARGUMENT); return; }
  for (unsigned char character : host) {
    if (!std::isalnum(character) && character != '.' && character != '-') { Fail("address", PP_ERROR_BADARGUMENT); return; }
  }
  if (!pin.empty()) {
    tls_.reset(new TlsClient());
    if (!tls_->Initialize(pin, seed)) { Fail("TLS configuration", PP_ERROR_BADARGUMENT); return; }
  }
  PP_HostResolver_Hint hint = {PP_NETADDRESS_FAMILY_IPV4, 0};
  int32_t code = resolver_.Resolve(host.c_str(), static_cast<uint16_t>(port), hint, callbacks_.NewCallback(&TcpChannel::OnResolve));
  if (code != PP_OK_COMPLETIONPENDING) OnResolve(code);
}
void TcpChannel::OnResolve(int32_t code) {
  if (code != PP_OK || !resolver_.GetNetAddressCount()) { Fail("resolve", code); return; }
  code = socket_.Connect(resolver_.GetNetAddress(0), callbacks_.NewCallback(&TcpChannel::OnConnect));
  if (code != PP_OK_COMPLETIONPENDING) OnConnect(code);
}
void TcpChannel::OnConnect(int32_t code) {
  if (code != PP_OK) { Fail("connect", code); return; }
  if (tls_) ProcessTls();
  else { connected_ = true; owner_->Event("tcp-ready", "ready", id_); }
  if (!socket_.is_null()) Receive();
}
void TcpChannel::ProcessTls() {
  if (!connected_) {
    int code = tls_->Handshake();
    Enqueue(tls_->Output());
    if (socket_.is_null()) return;
    if (code == 0) { connected_ = true; owner_->Event("tcp-ready", "TLS certificate verified", id_); }
    else if (!TlsClient::Pending(code)) { Fail("TLS authentication", code); return; }
  }
  while (connected_) {
    char plaintext[65536];
    int code = tls_->Read(plaintext, sizeof(plaintext));
    Enqueue(tls_->Output());
    if (socket_.is_null()) return;
    if (code > 0) owner_->TcpData(id_, plaintext, code);
    else if (TlsClient::Pending(code)) return;
    else { Fail("TLS receive", code); return; }
  }
}
void TcpChannel::Receive() {
  int32_t code = socket_.Read(buffer_, sizeof(buffer_), callbacks_.NewCallback(&TcpChannel::OnReceive));
  if (code != PP_OK_COMPLETIONPENDING) OnReceive(code);
}
void TcpChannel::OnReceive(int32_t count) {
  if (count <= 0) { Fail("receive", count); return; }
  if (tls_) { tls_->Feed(buffer_, count); ProcessTls(); }
  else owner_->TcpData(id_, buffer_, count);
  if (!socket_.is_null()) Receive();
}
void TcpChannel::Send(const std::string& data) {
  if (!connected_ || data.empty()) return;
  if (!tls_) { Enqueue(data); return; }
  size_t offset = 0;
  while (offset < data.size()) {
    int code = tls_->Write(data.data() + offset, data.size() - offset);
    Enqueue(tls_->Output());
    if (!connected_) return;
    if (code <= 0) { Fail("TLS send", code); return; }
    offset += code;
  }
}
void TcpChannel::Enqueue(const std::string& data) {
  if (data.empty()) return;
  if (data.size() > 65536 || queued_ + data.size() > 262144) { Fail("send buffer", PP_ERROR_NOMEMORY); return; }
  queue_.push_back(data);
  queued_ += data.size();
  if (!sending_) Flush();
}
void TcpChannel::Flush() {
  if (queue_.empty()) { sending_ = false; return; }
  sending_ = true;
  const std::string& data = queue_.front();
  int32_t code = socket_.Write(data.data() + sent_, data.size() - sent_, callbacks_.NewCallback(&TcpChannel::OnSend));
  if (code != PP_OK_COMPLETIONPENDING) OnSend(code);
}
void TcpChannel::OnSend(int32_t count) {
  if (count <= 0 || static_cast<size_t>(count) > queue_.front().size() - sent_) { Fail("send", count); return; }
  sent_ += count;
  if (sent_ == queue_.front().size()) {
    queued_ -= sent_;
    sent_ = 0;
    queue_.pop_front();
  }
  Flush();
}
void TcpChannel::Fail(const std::string& operation, int32_t code) {
  Close();
  owner_->Event("tcp-error", "TCP " + operation + ": " + std::to_string(code), id_);
}
void TcpChannel::Close() { callbacks_.CancelAll(); socket_.Close(); connected_ = false; socket_ = pp::TCPSocket(); }

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
