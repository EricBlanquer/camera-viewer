#ifndef CAMERA_VIEWER_TLS_CLIENT_H
#define CAMERA_VIEWER_TLS_CLIENT_H
#include <algorithm>
#include <cstring>
#include <string>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/md.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>

inline std::string DigestHex(const std::string& data, const std::string& algorithm) {
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(algorithm == "SHA-256" ? MBEDTLS_MD_SHA256 : MBEDTLS_MD_MD5);
  unsigned char digest[32];
  if (!info || mbedtls_md(info, reinterpret_cast<const unsigned char*>(data.data()), data.size(), digest)) return "";
  static const char HEX[] = "0123456789abcdef";
  std::string output;
  for (size_t i = 0; i < mbedtls_md_get_size(info); ++i) { output += HEX[digest[i] >> 4]; output += HEX[digest[i] & 15]; }
  return output;
}

class TlsClient {
 public:
  TlsClient() { mbedtls_ssl_init(&ssl_); mbedtls_ssl_config_init(&config_); mbedtls_ctr_drbg_init(&random_); }
  ~TlsClient() { mbedtls_ssl_free(&ssl_); mbedtls_ssl_config_free(&config_); mbedtls_ctr_drbg_free(&random_); }
  bool Initialize(const std::string& pin, const std::string& seed) {
    if (pin.size() != 64 || seed.size() != 48) return false;
    for (char value : pin) if (!(value >= '0' && value <= '9') && !(value >= 'a' && value <= 'f')) return false;
    pin_ = pin;
    seed_ = seed;
    int code = mbedtls_ctr_drbg_seed(&random_, Entropy, this, nullptr, 0);
    std::fill(seed_.begin(), seed_.end(), 0);
    seed_.clear();
    if (code || mbedtls_ssl_config_defaults(&config_, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM, MBEDTLS_SSL_PRESET_DEFAULT)) return false;
    mbedtls_ssl_conf_rng(&config_, mbedtls_ctr_drbg_random, &random_);
    mbedtls_ssl_conf_authmode(&config_, MBEDTLS_SSL_VERIFY_OPTIONAL);
    mbedtls_ssl_conf_verify(&config_, Verify, this);
    mbedtls_ssl_conf_min_tls_version(&config_, MBEDTLS_SSL_VERSION_TLS1_2);
    mbedtls_ssl_conf_max_tls_version(&config_, MBEDTLS_SSL_VERSION_TLS1_2);
    if (mbedtls_ssl_setup(&ssl_, &config_)) return false;
    mbedtls_ssl_set_bio(&ssl_, this, Send, Receive, nullptr);
    return true;
  }
  int Handshake() {
    int code = mbedtls_ssl_handshake(&ssl_);
    return code == 0 && (!verified_ || mbedtls_ssl_get_verify_result(&ssl_) != 0) ? MBEDTLS_ERR_X509_CERT_VERIFY_FAILED : code;
  }
  void Feed(const char* data, size_t size) { input_.append(data, size); }
  std::string Output() { std::string output; output.swap(output_); return output; }
  int Read(char* data, size_t size) { return mbedtls_ssl_read(&ssl_, reinterpret_cast<unsigned char*>(data), size); }
  int Write(const char* data, size_t size) { return mbedtls_ssl_write(&ssl_, reinterpret_cast<const unsigned char*>(data), size); }
  static bool Pending(int code) { return code == MBEDTLS_ERR_SSL_WANT_READ || code == MBEDTLS_ERR_SSL_WANT_WRITE; }
 private:
  static int Entropy(void* context, unsigned char* data, size_t size) {
    TlsClient* client = static_cast<TlsClient*>(context);
    if (size > client->seed_.size()) return MBEDTLS_ERR_CTR_DRBG_ENTROPY_SOURCE_FAILED;
    std::memcpy(data, client->seed_.data(), size);
    client->seed_.erase(0, size);
    return 0;
  }
  static int Verify(void* context, mbedtls_x509_crt* certificate, int depth, uint32_t* flags) {
    TlsClient* client = static_cast<TlsClient*>(context);
    if (depth != 0) { *flags = 0; return 0; }
    std::string raw(reinterpret_cast<const char*>(certificate->raw.p), certificate->raw.len);
    std::string digest = DigestHex(raw, "SHA-256");
    unsigned char difference = digest.size() == client->pin_.size() ? 0 : 1;
    for (size_t i = 0; i < std::min(digest.size(), client->pin_.size()); ++i) difference |= digest[i] ^ client->pin_[i];
    client->verified_ = difference == 0;
    *flags = client->verified_ ? 0 : MBEDTLS_X509_BADCERT_OTHER;
    return 0;
  }
  static int Send(void* context, const unsigned char* data, size_t size) {
    TlsClient* client = static_cast<TlsClient*>(context);
    if (client->output_.size() + size > 262144) return MBEDTLS_ERR_SSL_WANT_WRITE;
    client->output_.append(reinterpret_cast<const char*>(data), size);
    return static_cast<int>(size);
  }
  static int Receive(void* context, unsigned char* data, size_t size) {
    TlsClient* client = static_cast<TlsClient*>(context);
    if (client->input_.empty()) return MBEDTLS_ERR_SSL_WANT_READ;
    size = std::min(size, client->input_.size());
    std::memcpy(data, client->input_.data(), size);
    client->input_.erase(0, size);
    return static_cast<int>(size);
  }
  mbedtls_ssl_context ssl_;
  mbedtls_ssl_config config_;
  mbedtls_ctr_drbg_context random_;
  std::string pin_;
  std::string seed_;
  std::string input_;
  std::string output_;
  bool verified_ = false;
};
#endif
