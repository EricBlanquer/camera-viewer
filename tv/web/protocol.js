(function (root) {
  "use strict";
  const MAX_BUFFER = 8 * 1024 * 1024;
  const MAX_FRAME = 1024 * 1024;
  const DIRECTORY_PORT = 32100;
  const OKAM_STATUS_REQUEST = "get_status.cgi?name=admin&";
  const OKAM_KEEP_ALIVE_MS = 45000;
  const SHUFFLE_HEX =
    "7c9ce84a13dedcb22f2123e4307b3d8cbc0b270c3cf79ae7087196009785efc11fc4dba1c2ebd901faba3b05b81587832872d18b5ad6da9358feaacc6e1bf0a388ab43c00db545384f502266207f075b14981d9ba72ab9a8cbf1fc4947063eb10e043a945eee541134dd4df9ecc7c9e3781a6f706ba4bda95dd5f8e5bb26af4237d8e1020aae5f1cc573094e6924906d12b319ad748a2940f52dbea559e0f479d24bce8982488425c6912ba2fb8fe9a6b09e3f65f603312eac0f952c5ced39b7336c567eb4a0fd7a815351868d9f77ff6a80dfe2bf10d775645776f355cdd0c818e6364162cf99f2324c67606192cad3ea637d16b68ed46835c3529d46441e17";
  const LOOKUP_HEX =
    "4959433db5bf6da347534f6165e371e9677f02030badb3892b2f35c16b8b959711e5a70deff1050783fb9d3bc5c713171d1f2529d3df";
  const fromHex = (value) =>
    Uint8Array.from(value.match(/../g).map((byte) => parseInt(byte, 16)));
  const SHUFFLE = fromHex(SHUFFLE_HEX);
  const LOOKUP = fromHex(LOOKUP_HEX);
  const bytes = (value) => new TextEncoder().encode(value);
  const text = (value) => new TextDecoder().decode(value);
  const view = (value) =>
    new DataView(value.buffer, value.byteOffset, value.byteLength);
  const concat = (...arrays) => {
    const output = new Uint8Array(
      arrays.reduce((sum, array) => sum + array.length, 0),
    );
    let offset = 0;
    arrays.forEach((array) => {
      output.set(array, offset);
      offset += array.length;
    });
    return output;
  };
  const word32 = (value) => {
    const output = new Uint8Array(4);
    view(output).setUint32(0, value, true);
    return output;
  };
  function packet(type, body = new Uint8Array()) {
    if (body.length > 65535) throw new Error("Packet is too large");
    return concat(
      Uint8Array.of(0xf1, type, body.length >> 8, body.length & 255),
      body,
    );
  }
  function uidBytes(did) {
    const parts = did.split("-");
    if (
      parts.length !== 3 || !/^[A-Za-z0-9]{1,8}$/.test(parts[0]) ||
      !/^[A-Za-z0-9]{1,8}$/.test(parts[2]) || !/^\d+$/.test(parts[1]) ||
      Number(parts[1]) > 0xffffffff
    ) throw new Error("Invalid camera identifier");
    const output = new Uint8Array(20);
    output.set(bytes(parts[0]), 0);
    view(output).setUint32(8, Number(parts[1]));
    output.set(bytes(parts[2]), 12);
    return output;
  }
  function networkAddress(host, port) {
    const octets = host.split(".").map(Number);
    if (
      octets.length !== 4 ||
      octets.some((octet) =>
        !Number.isInteger(octet) || octet < 0 || octet > 255
      ) || !Number.isInteger(port) || port < 1 || port > 65535
    ) throw new Error("Invalid network address");
    const output = new Uint8Array(16);
    output[1] = 2;
    view(output).setUint16(2, port, true);
    output.set(octets.reverse(), 4);
    return output;
  }
  function parseAddress(data) {
    if (data.length < 16 || data[0] !== 0 || data[1] !== 2) {
      throw new Error("Invalid directory response");
    }
    return {
      host: Array.from(data.subarray(4, 8)).reverse().join("."),
      port: view(data).getUint16(2, true),
    };
  }
  function decodeService(value) {
    const parts = value.split(":");
    const encoded = parts[0].toUpperCase();
    if (
      !encoded.length || encoded.length > 4096 || encoded.length % 2 ||
      !/^[A-P]+$/.test(encoded)
    ) throw new Error("Invalid directory configuration");
    const output = new Uint8Array(encoded.length / 2);
    let chained = 57;
    for (let i = 0; i < output.length; i++) {
      output[i] = chained ^ LOOKUP[i % LOOKUP.length] ^
        ((encoded.charCodeAt(i * 2) - 65) << 4 |
          (encoded.charCodeAt(i * 2 + 1) - 65));
      chained ^= output[i];
    }
    const servers = text(output).replace(/\0+$/, "").split(",").filter(Boolean);
    if (!servers.length || servers.length > 16) {
      throw new Error("Invalid directory configuration");
    }
    servers.forEach((server) => networkAddress(server, DIRECTORY_PORT));
    return { servers, key: parts[1] || "" };
  }
  function cipher(data, key, decrypt = false, session = false) {
    const states = [0, 0, 0, 0];
    for (const byte of bytes(key)) {
      states[0] = (states[0] + byte) & 255;
      states[1] = (states[1] - byte) & 255;
      states[2] = (states[2] +
        (session ? Math.floor(byte * 0xab / 512) : Math.floor(byte / 3))) &
        255;
      states[3] ^= byte;
    }
    const output = new Uint8Array(data.length);
    let previous = 0;
    for (let i = 0; i < data.length; i++) {
      output[i] = data[i] ^ SHUFFLE[(states[previous & 3] + previous) & 255];
      previous = decrypt ? data[i] : output[i];
    }
    return output;
  }
  function isKeyframe(data, codec) {
    if (![27, 36].includes(codec)) return false;
    for (let i = 0; i + 3 < data.length; i++) {
      if (data[i] || data[i + 1]) continue;
      const offset = data[i + 2] === 1
        ? i + 3
        : data[i + 2] === 0 && data[i + 3] === 1
        ? i + 4
        : -1;
      if (offset < 0 || offset >= data.length) continue;
      const kind = codec === 36 ? (data[offset] >> 1) & 63 : data[offset] & 31;
      if (codec === 36 ? kind >= 16 && kind <= 21 : kind === 5) return true;
    }
    return false;
  }
  class ByteQueue {
    constructor() {
      this.chunks = [];
      this.offset = 0;
      this.length = 0;
    }
    append(data) {
      if (!data.length) return;
      if (this.length + data.length > MAX_BUFFER) {
        throw new Error("Receive buffer exceeded");
      }
      this.chunks.push(data);
      this.length += data.length;
    }
    peek(size) {
      if (size > this.length) return null;
      const output = new Uint8Array(size);
      let count = 0;
      let offset = this.offset;
      for (const chunk of this.chunks) {
        const available = Math.min(size - count, chunk.length - offset);
        output.set(chunk.subarray(offset, offset + available), count);
        count += available;
        offset = 0;
        if (count === size) break;
      }
      return output;
    }
    read(size) {
      const output = this.peek(size);
      if (output === null) return null;
      this.length -= size;
      while (size > 0) {
        const available = this.chunks[0].length - this.offset;
        if (size < available) {
          this.offset += size;
          break;
        }
        size -= available;
        this.chunks.shift();
        this.offset = 0;
      }
      return output;
    }
  }
  class OrderedChannel {
    constructor(expected = 0) {
      this.expected = expected;
      this.pending = new Map();
      this.size = 0;
      this.gap = null;
    }
    feed(index, payload, now = performance.now()) {
      const distance = (index - this.expected) & 65535;
      if (distance >= 32768 || this.pending.has(index)) return [];
      if (distance > 4096 || this.size + payload.length > MAX_BUFFER) {
        throw new Error("Packet ordering buffer exceeded");
      }
      this.pending.set(index, payload);
      this.size += payload.length;
      const output = [];
      while (this.pending.has(this.expected)) {
        const chunk = this.pending.get(this.expected);
        output.push(chunk);
        this.size -= chunk.length;
        this.pending.delete(this.expected);
        this.expected = (this.expected + 1) & 65535;
      }
      if (!this.pending.size) this.gap = null;
      else if (output.length || this.gap === null) this.gap = now;
      return output;
    }
    check(now = performance.now()) {
      if (this.gap !== null && now - this.gap > 8000) {
        throw new Error("Camera packets were lost");
      }
    }
  }
  class MediaFrames {
    constructor(kind) {
      this.kind = kind;
      this.buffer = new ByteQueue();
    }
    feed(data) {
      this.buffer.append(data);
      const output = [];
      const size = this.kind === "okam" ? 32 : 16;
      while (this.buffer.length >= size) {
        const header = this.buffer.peek(size);
        const length = view(header).getUint32(
          this.kind === "okam" ? 16 : 8,
          true,
        );
        if (length > (this.kind === "okam" ? MAX_BUFFER : MAX_FRAME)) {
          throw new Error("Invalid camera frame size");
        }
        if (this.kind === "okam") {
          if (view(header).getUint32(0) !== 0x55aa15a8 || !length) {
            throw new Error("Invalid O-KAM video frame");
          }
        } else if (
          ![0, 80, 138].includes(header[0]) || (!header[0] && length)
        ) throw new Error("Invalid iCam365 video frame");
        if (this.buffer.length < size + length) break;
        this.buffer.read(size);
        const payload = this.buffer.read(length);
        if (length) {
          output.push({
            payload,
            codec: this.kind === "okam" ? 27 : header[0],
            flags: this.kind === "okam" ? header[4] : header[2],
          });
        }
      }
      return output;
    }
  }
  let nextSessionId = 1;
  class NativeSession {
    constructor(config, native, callbacks) {
      this.config = config;
      this.native = native;
      this.callbacks = callbacks;
      this.okam = config.type === "okam";
      if (!["okam", "icam365"].includes(config.type)) {
        throw new Error("Unsupported camera type");
      }
      let did = config.p2p_id;
      if (this.okam) {
        if (!/^[A-Za-z0-9]{4}\d{6}[A-Za-z]{5}$/.test(config.uid)) {
          throw new Error("Invalid camera identifier");
        }
        did = config.uid.slice(0, 4) + "-" + config.uid.slice(4, 10) + "-" +
          config.uid.slice(10);
      }
      if (
        typeof did !== "string" || typeof config.password !== "string" ||
        !config.password ||
        bytes(config.password).length > (this.okam ? 512 : 48)
      ) throw new Error("Invalid camera configuration");
      this.uid = uidBytes(did.split(",")[0]);
      const service = this.okam
        ? config.service_parameter
        : config.p2p_platform;
      if (
        typeof service !== "string" ||
        (!this.okam && !service.startsWith("ppcs:"))
      ) throw new Error("Invalid directory configuration");
      const decoded = decodeService(service.replace(/^ppcs:/, ""));
      this.servers = decoded.servers;
      this.key = this.okam ? decoded.key : "";
      if (this.okam && !this.key) throw new Error("Missing transport key");
      this.channels = Array.from({ length: 8 }, () => new OrderedChannel());
      this.commandBuffer = new ByteQueue();
      this.video = new MediaFrames(this.okam ? "okam" : "icam");
      this.audio = new MediaFrames("icam");
      this.sequence = 0;
      this.pending = new Map();
      this.targets = [];
      this.relays = [];
      this.state = "closed";
      this.peer = null;
      this.id = nextSessionId++;
      this.lastAlive =
        this.lastKeepAlive =
        this.lastPunch =
        this.lastLookup =
        this.lastRelay =
        this.lastRelayList =
          0;
      this.authenticated = false;
      this.videoReceived = false;
      this.relay = this.okam && Boolean(config.prefer_relay);
    }
    open() {
      this.state = "binding";
      this.started = performance.now();
      this.native.postMessage({ command: "udp-open", id: this.id });
      this.timer = setInterval(() => {
        try {
          this.tick();
        } catch (error) {
          this.fail(error);
        }
      }, 100);
    }
    send(data, address = this.peer) {
      if (!address) return;
      const post = (payload) =>
        this.native.postMessage({
          command: "udp-send",
          id: this.id,
          host: address.host,
          port: address.port,
          data: payload.buffer.slice(
            payload.byteOffset,
            payload.byteOffset + payload.byteLength,
          ),
        });
      if (!this.okam) {
        post(data);
        return;
      }
      const dual = [0x41, 0x42, 0x43, 0x80, 0x83, 0xd1, 0xe1].includes(data[1]);
      const encrypted = [
        0,
        0x20,
        0x67,
        0x70,
        0x72,
        0xd0,
        0xd1,
        0xe0,
        0xe1,
        0xf0,
      ].includes(data[1]);
      if (dual || !encrypted) post(data);
      if (dual || encrypted) post(cipher(data, this.key));
    }
    directory(data) {
      this.servers.forEach((host) =>
        this.send(data, { host, port: DIRECTORY_PORT })
      );
    }
    lookup() {
      const address = this.okam
        ? { host: this.config.local_host || "0.0.0.0", port: this.port }
        : this.wan || { host: "0.0.0.0", port: this.port };
      this.directory(
        packet(
          0x20,
          concat(this.uid, networkAddress(address.host, address.port)),
        ),
      );
      this.lastLookup = performance.now();
    }
    punch() {
      this.targets.forEach((target) => {
        for (let offset = -3; offset <= 3; offset++) {
          if (target.port + offset > 0 && target.port + offset <= 65535) {
            this.send(packet(0x41, this.uid), {
              host: target.host,
              port: target.port + offset,
            });
          }
        }
      });
      this.lastPunch = performance.now();
    }
    event(event) {
      if (event.id !== this.id || this.state === "closed") return;
      try {
        if (event.type === "udp-ready") {
          this.port = Number(event.detail);
          this.state = "connecting";
          this.directory(packet(0));
          this.lookup();
          this.callbacks.status(
            this.relay
              ? "Connecting from TV through camera P2P service"
              : "Connecting directly to camera",
          );
        } else if (event.type === "udp-data") {
          this.receive(new Uint8Array(event.data), {
            host: event.host,
            port: event.port,
          });
        } else if (event.type === "error") this.fail(new Error(event.detail));
      } catch (error) {
        this.fail(error);
      }
    }
    negotiate(data, address, directory) {
      const type = data[1];
      if (directory && type === 1 && data.length >= 20) {
        this.wan = parseAddress(data.subarray(4));
        this.lookup();
      }
      if (
        !this.okam && directory && type === 0x21 && data.length >= 5 && data[4]
      ) throw new Error("Camera directory reported offline");
      if (directory && type === 0x40 && data.length >= 20) {
        const target = parseAddress(data.subarray(4));
        if (
          !this.targets.some((item) =>
            item.host === target.host && item.port === target.port
          ) && this.targets.length < 32
        ) this.targets.push(target);
        this.punch();
      }
      if (this.relay) this.negotiateRelay(data, address, directory);
      const matching = data.length === 24 &&
        this.uid.every((byte, index) => data[index + 4] === byte);
      if (!directory && type === 0x42 && this.relay && matching) {
        this.send(packet(0x43), address);
      }
      if (
        this.peer || directory || !matching ||
        !(this.relay ? type === 0x84 : [0x41, 0x42].includes(type))
      ) return;
      this.peer = address;
      this.connected = performance.now();
      this.state = "handshake";
      if (type === 0x42) this.send(packet(0x43));
      else if (type === 0x41) this.send(packet(0x42, this.uid));
      if (!this.okam) {
        const report = new Uint8Array(84);
        report.set(this.uid);
        if (this.wan) {
          report.set(networkAddress(this.wan.host, this.wan.port), 40);
        }
        report.set(networkAddress(address.host, address.port), 56);
        view(report).setUint32(80, Math.floor(Date.now() / 1000));
        this.report = packet(
          0xf9,
          cipher(report, "SSD@cs2-network.", false, true),
        );
        [0x84, 0x42, 0x41].forEach((kind) => this.send(packet(kind, this.uid)));
        this.send(this.report);
      }
      for (let i = 0; i < (this.okam ? 1 : 8); i++) this.send(packet(0xe0));
      this.callbacks.status(
        this.relay
          ? "TV camera P2P connection established"
          : "Direct camera connection established",
      );
    }
    negotiateRelay(data, address, directory) {
      const type = data[1];
      const relay = this.relays.some((item) =>
        item.host === address.host && item.port === address.port
      );
      if (
        directory && type === 0x69 && data.length >= 8 && data[4] <= 32 &&
        data.length === 8 + data[4] * 16
      ) {
        for (let i = 0; i < data[4]; i++) {
          const target = parseAddress(data.subarray(8 + i * 16));
          if (
            !this.relays.some((item) =>
              item.host === target.host && item.port === target.port
            )
          ) this.relays.push(target);
        }
      } else if (relay && type === 0x71 && !this.relayEndpoint) {
        this.relayEndpoint = address;
        this.send(packet(0x72), address);
      } else if (
        relay && type === 0x73 && data.length === 12 && this.relayEndpoint &&
        address.host === this.relayEndpoint.host &&
        address.port === this.relayEndpoint.port
      ) {
        const token = data.subarray(4, 8);
        const allocated = networkAddress(address.host, view(data).getUint16(8));
        this.relayRequest = packet(0x80, concat(this.uid, allocated, token));
        this.directory(this.relayRequest);
      } else if (directory && type === 0x82 && data.length === 24) {
        const target = parseAddress(data.subarray(4));
        const response = packet(
          0x83,
          concat(data.subarray(20), this.uid, new Uint8Array(4)),
        );
        this.send(response, target);
        this.send(response, address);
      } else if (type === 2 && data.length === 36 && (directory || relay)) {
        [4, 20].forEach((offset) => {
          try {
            this.send(packet(3), parseAddress(data.subarray(offset)));
          } catch (error) {
            this.callbacks.status("P2P service returned an unavailable route");
          }
        });
      }
    }
    receive(data, address) {
      if (this.okam && data[0] !== 0xf1) data = cipher(data, this.key, true);
      if (
        data.length < 4 || data[0] !== 0xf1 ||
        data.length !== 4 + view(data).getUint16(2)
      ) return;
      const type = data[1];
      const directory = this.servers.includes(address.host) &&
        address.port === DIRECTORY_PORT;
      if (!this.peer) this.negotiate(data, address, directory);
      if (
        !this.peer || address.host !== this.peer.host ||
        (!this.okam && address.port !== this.peer.port)
      ) return;
      if (type === 0xe0) this.send(packet(0xe1), address);
      else if (type === 0x41) this.send(packet(0x42, this.uid), address);
      else if (type === 0x42) this.send(packet(0x43), address);
      else if (type === 0xf9 && this.report && this.state === "handshake") {
        this.send(this.report);
      } else if (type === 0xf0) {
        if (this.state === "closing") {
          this.finishClose();
          return;
        }
        throw new Error("Camera closed its connection");
      } else if (
        type === 0xd1 && data.length >= 8 && data[4] === 0xd1 && data[5] === 0
      ) {
        const count = view(data).getUint16(6);
        if (data.length === 8 + count * 2) {
          for (let i = 0; i < count; i++) {
            this.pending.delete(view(data).getUint16(8 + i * 2));
          }
        }
        if (this.state === "closing" && !this.pending.size) this.finishClose();
      } else if (
        type === 0xd0 && data.length >= 8 && data[4] === 0xd1 &&
        data[5] < this.channels.length
      ) {
        const channel = data[5];
        const index = view(data).getUint16(6);
        this.send(
          packet(
            0xd1,
            Uint8Array.of(0xd1, channel, 0, 1, index >> 8, index & 255),
          ),
        );
        if (this.state === "closing") return;
        for (
          const chunk of this.channels[channel].feed(index, data.subarray(8))
        ) {
          if (channel === 0) {
            this.commandBuffer.append(chunk);
            this.commands();
          } else if (channel === (this.okam ? 1 : 2)) {
            for (const frame of this.video.feed(chunk)) {
              if (this.okam && [12, 16, 17].includes(frame.flags)) {
                continue;
              }
              this.lastVideo = performance.now();
              this.videoReceived = true;
              this.callbacks.video(frame.payload, this.okam ? 27 : 36);
            }
          } else if (!this.okam && channel === 1 && this.callbacks.audio) {
            for (const frame of this.audio.feed(chunk)) {
              this.callbacks.audio(frame.payload);
            }
          }
        }
      }
    }
    write(payload) {
      for (let offset = 0; offset < payload.length; offset += 1024) {
        const index = this.sequence;
        this.sequence = (index + 1) & 65535;
        const data = packet(
          0xd0,
          concat(
            Uint8Array.of(0xd1, 0, index >> 8, index & 255),
            payload.subarray(offset, offset + 1024),
          ),
        );
        this.pending.set(index, { data, sent: performance.now(), attempts: 0 });
        this.send(data);
      }
    }
    command(command, body = new Uint8Array()) {
      this.write(concat(word32(command), word32(body.length), body));
    }
    cgi(path) {
      const request = bytes(
        "GET /" + path + "loginuse=admin&loginpas=" + this.config.password +
          "&user=admin&pwd=888888&",
      );
      if (
        request.length > 65535 ||
        /[\x00-\x1f\x7f-\uffff]/.test(this.config.password)
      ) throw new Error("Invalid camera credential");
      const header = new Uint8Array(8);
      view(header).setUint16(0, 0x0a01, true);
      view(header).setUint16(4, request.length, true);
      this.write(concat(header, request));
    }
    commands() {
      while (this.commandBuffer.length >= 8) {
        const header = view(this.commandBuffer.peek(8));
        if (this.okam && header.getUint16(0, true) !== 0x0a01) {
          throw new Error("Invalid camera command framing");
        }
        const command = this.okam
          ? header.getUint16(2, true)
          : header.getUint32(0, true);
        const size = this.okam
          ? header.getUint16(4, true)
          : header.getUint32(4, true);
        if (size > MAX_FRAME) throw new Error("Invalid camera command");
        if (this.commandBuffer.length < size + 8) break;
        this.commandBuffer.read(8);
        const payload = this.commandBuffer.read(size);
        const result = this.okam
          ? text(payload).match(/result\s*=\s*["']?(-?\d+)/)
          : null;
        if (command === (this.okam ? 0x6001 : 0x8003)) {
          if (
            this.okam
              ? !result || Number(result[1]) !== 0
              : payload.length < 4 || view(payload).getInt32(0, true) !== 0
          ) throw new Error("Camera rejected authentication");
          if (this.authenticated) continue;
          this.authenticated = true;
          this.state = "streaming";
          this.lastVideo = performance.now();
          this.lastKeepAlive = this.lastVideo;
          if (this.okam) this.cgi("livestream.cgi?streamid=10&substream=2&");
          else {
            this.command(0x8024);
            this.command(0x8012, new Uint8Array(8));
            this.command(0x1ff, concat(word32(2), word32(0)));
            this.command(0x320, concat(word32(0), word32(1)));
          }
          this.callbacks.status("Authenticated, waiting for live video");
        } else if (
          this.okam && [0x6037, 0x60d1].includes(command) && result &&
          Number(result[1]) !== 0
        ) throw new Error("Camera rejected live video");
      }
    }
    tick() {
      const now = performance.now();
      if (!this.peer && this.state !== "closed") {
        if (now - this.started > (this.relay ? 55000 : 20000)) {
          throw new Error("Camera P2P connection timed out");
        }
        if (this.state === "connecting" && now - this.lastLookup > 1000) {
          this.lookup();
        }
        if (this.targets.length && now - this.lastPunch > 250) this.punch();
        if (this.relay && now - this.lastRelayList > 3000) {
          this.directory(packet(0x67, this.uid));
          this.lastRelayList = now;
        }
        if (this.relay && now - this.lastRelay > 1000) {
          this.relays.forEach((target) => this.send(packet(0x70), target));
          if (this.relayEndpoint && !this.relayRequest) {
            this.send(packet(0x72), this.relayEndpoint);
          }
          if (this.relayRequest) this.directory(this.relayRequest);
          this.lastRelay = now;
        }
      }
      if (this.peer && now - this.lastAlive > 1000) {
        this.send(packet(0xe0));
        this.lastAlive = now;
      }
      if (
        this.state === "handshake" &&
        now - this.connected > (this.okam ? 0 : 6000)
      ) {
        this.state = "authenticating";
        if (this.okam) this.cgi(OKAM_STATUS_REQUEST);
        else {
          const credential = new Uint8Array(60);
          credential.set(bytes(this.config.password), 8);
          this.command(0x8002, credential);
        }
        this.authAt = now;
      }
      if (
        this.okam && this.authenticated &&
        now - this.lastKeepAlive > OKAM_KEEP_ALIVE_MS
      ) {
        this.cgi(OKAM_STATUS_REQUEST);
        this.lastKeepAlive = now;
      }
      if (this.state === "authenticating" && now - this.authAt > 15000) {
        throw new Error("Camera authentication timed out");
      }
      for (const entry of this.pending.values()) {
        if (now - entry.sent >= 500) {
          if (entry.attempts >= 20) {
            throw new Error("Camera did not acknowledge a command");
          }
          this.send(entry.data);
          entry.attempts++;
          entry.sent = now;
        }
      }
      this.channels.forEach((channel) => channel.check(now));
      if (this.authenticated && now - this.lastVideo > 12000) {
        throw new Error("Camera stopped sending video");
      }
    }
    close() {
      if (["closed", "closing"].includes(this.state)) return;
      clearInterval(this.timer);
      this.state = "closing";
      this.pending.clear();
      this.closeStarted = performance.now();
      this.closeListener = (event) => this.event(event.data);
      this.native.addEventListener("message", this.closeListener);
      if (this.peer && this.authenticated) {
        if (this.okam) this.cgi("livestream.cgi?streamid=16&substream=0&");
        else this.command(0x2ff, concat(word32(2), word32(0)));
      }
      if (!this.pending.size) {
        this.finishClose();
        return;
      }
      this.timer = setInterval(() => {
        const now = performance.now();
        if (now - this.closeStarted >= 5000) {
          this.finishClose();
          return;
        }
        for (const entry of this.pending.values()) {
          if (now - entry.sent >= 500) {
            this.send(entry.data);
            entry.sent = now;
          }
        }
      }, 100);
    }
    finishClose() {
      if (this.state === "closed") return;
      clearInterval(this.timer);
      this.native.removeEventListener("message", this.closeListener);
      if (this.peer) this.send(packet(0xf0));
      this.targets.forEach((target) => this.send(packet(0xf0), target));
      const id = this.id;
      const native = this.native;
      setTimeout(() => native.postMessage({ command: "udp-close", id }), 500);
      this.state = "closed";
    }
    fail(error) {
      if (this.state === "closed") return;
      if (this.state === "closing") {
        this.finishClose();
        return;
      }
      const retryRelay = this.okam && !this.relay && !this.videoReceived;
      this.close();
      this.callbacks.error(error.message, retryRelay);
    }
  }
  const api = {
    concat,
    word32,
    bytes,
    text,
    view,
    packet,
    uidBytes,
    networkAddress,
    parseAddress,
    decodeService,
    cipher,
    isKeyframe,
    ByteQueue,
    OrderedChannel,
    MediaFrames,
    NativeSession,
  };
  root.CameraProtocol = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
