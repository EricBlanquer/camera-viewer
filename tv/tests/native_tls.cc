#include "tls_client.h"
#include <cassert>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>

int main(int argc, char** argv) {
  assert(DigestHex("abc", "MD5") == "900150983cd24fb0d6963f7d28e17f72");
  assert(DigestHex("abc", "SHA-256") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  assert(argc == 4);
  std::ifstream entropy("/dev/urandom", std::ios::binary);
  std::string seed(48, '\0');
  entropy.read(&seed[0], seed.size());
  assert(entropy.gcount() == 48);
  TlsClient invalid;
  assert(!invalid.Initialize(argv[2], ""));
  TlsClient client;
  assert(client.Initialize(argv[2], seed));
  int descriptor = socket(AF_INET, SOCK_STREAM, 0);
  assert(descriptor >= 0);
  timeval timeout = {10, 0};
  assert(setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) == 0);
  sockaddr_in address = {};
  address.sin_family = AF_INET;
  address.sin_port = htons(std::atoi(argv[1]));
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  assert(connect(descriptor, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
  int code;
  auto flush = [&]() {
    std::string output = client.Output();
    size_t offset = 0;
    while (offset < output.size()) {
      ssize_t count = send(descriptor, output.data() + offset, output.size() - offset, MSG_NOSIGNAL);
      assert(count > 0);
      offset += count;
    }
  };
  auto receive = [&]() {
    char encrypted[65536];
    ssize_t count = recv(descriptor, encrypted, sizeof(encrypted), 0);
    assert(count > 0);
    for (ssize_t index = 0; index < count; ++index) client.Feed(encrypted + index, 1);
  };
  while ((code = client.Handshake()) != 0) {
    flush();
    if (!TlsClient::Pending(code)) {
      close(descriptor);
      assert(std::string(argv[3]) == "reject");
      std::cout << "Certificate mismatch rejected before application data\n";
      return 0;
    }
    receive();
  }
  assert(std::string(argv[3]) == "accept");
  flush();
  std::string data("video\0binary", 12);
  assert(client.Write(data.data(), data.size()) == static_cast<int>(data.size()));
  flush();
  std::string reply;
  while (reply.size() < data.size()) {
    char plaintext[65536];
    code = client.Read(plaintext, sizeof(plaintext));
    flush();
    if (TlsClient::Pending(code)) receive();
    else { assert(code > 0); reply.append(plaintext, code); }
  }
  assert(reply == data);
  close(descriptor);
  std::cout << "Pinned TLS handshake, fragmented encrypted input and binary round trip passed\n";
}
