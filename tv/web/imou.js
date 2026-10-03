"use strict";
(function (root) {
  const protocol = typeof module !== "undefined"
    ? require("./protocol.js")
    : root.CameraProtocol;
  const { bytes, text, concat, view, ByteQueue, isKeyframe } = protocol;
  const MAX_FRAME = 8 * 1024 * 1024;
  const MAX_RESPONSE = 65536;
  const LOCAL_HD_SUBTYPE = 0;
  const LOCAL_SD_SUBTYPE = 1;
  const CLOUD_HD_STREAM = 0;
  const CLOUD_SD_STREAM = 1;
  const REGIONS = {
    Europe: "openapi-fk.easy4ip.com",
    Singapore: "openapi-sg.easy4ip.com",
    "North America": "openapi-or.easy4ip.com",
  };
  const accounts = new Map();
  let nextId = 1;
  function uuid() {
    const values = crypto.getRandomValues(new Uint8Array(16));
    values[6] = values[6] & 15 | 64;
    values[8] = values[8] & 63 | 128;
    const hex = Array.from(
      values,
      (value) => value.toString(16).padStart(2, "0"),
    ).join("");
    return [
      hex.slice(0, 8),
      hex.slice(8, 12),
      hex.slice(12, 16),
      hex.slice(16, 20),
      hex.slice(20),
    ].join("-");
  }
  function base64(data) {
    return btoa(
      Array.from(data, (value) => String.fromCharCode(value)).join(""),
    );
  }
  async function signature(secret, timestamp, nonce) {
    const digest = new Uint8Array(
      await crypto.subtle.digest("SHA-256", bytes(secret)),
    );
    const hex = Array.from(
      digest,
      (value) => value.toString(16).padStart(2, "0"),
    ).join("");
    const key = await crypto.subtle.importKey(
      "raw",
      bytes(hex),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    return base64(
      new Uint8Array(
        await crypto.subtle.sign(
          "HMAC",
          key,
          bytes(
            "time:" + timestamp + ",nonce:" + nonce +
              ",appSecret:" + secret,
          ),
        ),
      ),
    );
  }
  function validate(camera) {
    if (camera.local !== undefined) {
      const local = camera.local;
      if (
        camera.type !== "imou" || !local || !privateHost(local.host) ||
        !Number.isInteger(local.port) || local.port < 1 || local.port > 65535 ||
        !Number.isInteger(local.channel) || local.channel < 1 ||
        local.channel > 65535 ||
        typeof local.username !== "string" || !local.username ||
        local.username.length > 128 ||
        typeof local.password !== "string" || !local.password ||
        local.password.length > 512 ||
        /[\r\n\0]/.test(local.username + local.password) ||
        (local.certificate_sha256 !== undefined &&
          (typeof local.certificate_sha256 !== "string" ||
            !/^[a-f0-9]{64}$/.test(local.certificate_sha256)))
      ) throw new Error("Invalid local Imou camera configuration");
      return;
    }
    const account = camera.account;
    if (
      camera.type !== "imou" || !account ||
      !REGIONS[account.region] ||
      typeof account.app_id !== "string" ||
      !/^[A-Za-z0-9]{1,128}$/.test(account.app_id) ||
      typeof account.app_secret !== "string" ||
      !account.app_secret || account.app_secret.length > 512 ||
      typeof camera.device_id !== "string" ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(camera.device_id) ||
      typeof camera.channel_id !== "string" ||
      !/^\d{1,5}$/.test(camera.channel_id) ||
      (camera.product_id !== undefined &&
        (typeof camera.product_id !== "string" ||
          camera.product_id.length > 128))
    ) throw new Error("Invalid Imou camera configuration");
  }
  function privateHost(host) {
    if (typeof host !== "string" || !/^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) {
      return false;
    }
    const parts = host.split(".").map(Number);
    if (
      parts.some((part, index) =>
        part > 255 || String(part) !== host.split(".")[index]
      )
    ) return false;
    return parts[0] === 10 || parts[0] === 192 && parts[1] === 168 ||
      parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31;
  }
  function localUrl(camera, hd = false) {
    validate(camera);
    const local = camera.local;
    return streamUrl(
      "rtsp://" + local.host + ":" + local.port +
        "/cam/realmonitor?channel=" + local.channel + "&subtype=" +
        (hd ? LOCAL_HD_SUBTYPE : LOCAL_SD_SUBTYPE),
      true,
    );
  }
  function challenge(value) {
    if (
      typeof value !== "string" || !value.startsWith("Digest ") ||
      value.length > 4096 || /[\r\n\0]/.test(value)
    ) {
      throw new Error("Unsupported camera authentication");
    }
    const parameters = Object.create(null);
    const pattern =
      /([A-Za-z_-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))\s*(?:,|$)/g;
    let match;
    let offset = 7;
    while ((match = pattern.exec(value))) {
      if (
        value.slice(offset, match.index).trim() ||
        parameters[match[1].toLowerCase()] !== undefined
      ) {
        throw new Error("Invalid camera authentication challenge");
      }
      parameters[match[1].toLowerCase()] = match[2] === undefined
        ? match[3]
        : match[2].replace(/\\(.)/g, "$1");
      offset = pattern.lastIndex;
    }
    const algorithm = parameters.algorithm || "MD5";
    if (
      value.slice(offset).trim() || !parameters.realm || !parameters.nonce ||
      !["MD5", "MD5-sess", "SHA-256", "SHA-256-sess"].includes(algorithm) ||
      parameters.qop &&
        !parameters.qop.split(",").map((item) => item.trim()).includes("auth")
    ) {
      throw new Error("Unsupported camera authentication");
    }
    return Object.assign(parameters, { algorithm });
  }
  function quoted(value) {
    return '"' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  }
  function request(host, method, body) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "https://" + host + "/openapi/" + method, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.timeout = 15000;
      xhr.onprogress = (event) => {
        if (event.loaded > MAX_RESPONSE) {
          xhr.abort();
          reject(new Error("Imou response exceeded the limit"));
        }
      };
      xhr.onerror = xhr.ontimeout = () =>
        reject(new Error("Imou account service unavailable"));
      xhr.onload = () => {
        try {
          if (xhr.status !== 200 || xhr.responseText.length > MAX_RESPONSE) {
            throw new Error();
          }
          resolve(JSON.parse(xhr.responseText));
        } catch (error) {
          reject(new Error("Invalid Imou account response"));
        }
      };
      xhr.send(JSON.stringify(body));
    });
  }
  class Account {
    constructor(config, send = request) {
      this.config = config;
      this.send = send;
      this.host = REGIONS[config.region];
      this.token = "";
      this.expires = 0;
      this.authentication = null;
    }
    async call(method, params = {}, retry = true) {
      if (method !== "accessToken") await this.authenticate();
      const time = Math.floor(Date.now() / 1000);
      const nonce = uuid();
      const response = await this.send(this.host, method, {
        system: {
          ver: "1.0",
          appId: this.config.app_id,
          time,
          nonce,
          sign: await signature(this.config.app_secret, time, nonce),
        },
        params: Object.assign(
          {},
          params,
          method === "accessToken" ? {} : { token: this.token },
        ),
        id: uuid(),
      });
      const result = response && response.result;
      if (!result || typeof result.code !== "string") {
        throw new Error("Invalid Imou account response");
      }
      if (result.code === "TK1002" && method !== "accessToken" && retry) {
        this.token = "";
        return this.call(method, params, false);
      }
      if (result.code !== "0") {
        throw new Error(
          "Imou account request rejected (" +
            (/^[A-Z0-9]{1,12}$/.test(result.code) ? result.code : "unknown") +
            ")",
        );
      }
      if (!result.data || typeof result.data !== "object") {
        throw new Error("Invalid Imou account response");
      }
      return result.data;
    }
    async authenticate() {
      if (this.token && Date.now() < this.expires) return;
      if (!this.authentication) {
        this.authentication = this.call("accessToken").then((data) => {
          const expiry = Number(data.expireTime);
          if (
            typeof data.accessToken !== "string" || !data.accessToken ||
            !Number.isFinite(expiry) || expiry <= 0
          ) {
            throw new Error("Invalid Imou account token");
          }
          if (data.currentDomain) {
            let url;
            try {
              url = new URL(
                data.currentDomain.includes("://")
                  ? data.currentDomain
                  : "https://" + data.currentDomain,
              );
            } catch (error) {
              throw new Error("Invalid Imou account gateway");
            }
            if (
              url.protocol !== "https:" || url.username || url.password ||
              url.port && url.port !== "443" || url.pathname !== "/" ||
              url.search || url.hash ||
              !Object.values(REGIONS).includes(url.hostname)
            ) {
              throw new Error("Invalid Imou account gateway");
            }
            this.host = url.hostname;
          }
          this.token = data.accessToken;
          this.expires = Date.now() + Math.max(1, expiry - 60) * 1000;
        });
      }
      try {
        await this.authentication;
      } finally {
        this.authentication = null;
      }
    }
    async stream(camera, hd = false) {
      const params = {
        deviceId: camera.device_id,
        channelId: camera.channel_id,
        streamId: hd ? CLOUD_HD_STREAM : CLOUD_SD_STREAM,
      };
      if (camera.product_id) params.productId = camera.product_id;
      return (await this.call("getStreamUrl", params)).url;
    }
  }
  function streamUrl(value, local = false) {
    if (
      typeof value !== "string" || value.length > 8192 || /[\r\n\0]/.test(value)
    ) {
      throw new Error("Invalid Imou stream address");
    }
    const authority = /^rtsp:\/\/([A-Za-z0-9.-]+)(?::(\d{1,5}))?(\/[^#]*)?$/
      .exec(value);
    if (!authority) throw new Error("Invalid Imou stream address");
    const port = authority[2] ? Number(authority[2]) : 554;
    let url;
    try {
      url = new URL(
        "http://" + authority[1] + ":" + port + (authority[3] || "/"),
      );
    } catch (error) {
      throw new Error("Invalid Imou stream address");
    }
    if (
      url.protocol !== "http:" || url.username || url.password ||
      url.hash ||
      !(local
        ? privateHost(url.hostname)
        : /^[A-Za-z0-9.-]+\.(imoulife\.com|easy4ip\.com)$/.test(
          url.hostname,
        )) ||
      port < 1 || port > 65535
    ) {
      throw new Error("Invalid Imou stream address");
    }
    return {
      href: "rtsp://" + url.hostname + ":" + port + url.pathname + url.search,
      hostname: url.hostname,
      port: String(port),
      http: url,
      local,
    };
  }
  function controlUrl(value, base) {
    let url;
    const endpoint = base.http ? base : streamUrl(base.href);
    try {
      url = value.startsWith("rtsp://")
        ? streamUrl(value, endpoint.local).http
        : new URL(value === "*" ? endpoint.http.href : value, endpoint.http);
    } catch (error) {
      throw new Error("Invalid RTSP control address");
    }
    if (
      /[\r\n\0]/.test(value) || url.protocol !== "http:" ||
      url.hostname !== endpoint.hostname || url.port !== endpoint.http.port ||
      url.username || url.password || url.hash
    ) {
      throw new Error("Invalid RTSP control address");
    }
    return "rtsp://" + endpoint.hostname + ":" + endpoint.port + url.pathname +
      url.search;
  }
  function videoDescription(sdp, base) {
    let video = false;
    let codec = 0;
    let payload = -1;
    let control = "";
    let parameters = "";
    const lines = sdp.split(/\r?\n/);
    for (const line of lines) {
      if (line.startsWith("m=")) {
        if (video) break;
        video = line.startsWith("m=video ");
      }
      if (!video) continue;
      const mapping = /^a=rtpmap:(\d+) (H264|H265)\/90000$/i.exec(line);
      if (mapping) {
        codec = mapping[2].toUpperCase() === "H265" ? 36 : 27;
        payload = Number(mapping[1]);
      }
      if (line.startsWith("a=control:")) control = line.slice(10);
      if (line.startsWith("a=fmtp:")) parameters = line;
    }
    if (
      !video || !codec || payload > 127 || !control ||
      /(?:sprop-max-don-diff|sprop-interleaving-depth)=[1-9]/.test(
        parameters,
      ) ||
      /packetization-mode=2/.test(parameters)
    ) {
      throw new Error("Unsupported Imou video description");
    }
    const nals = [];
    const pattern =
      /(?:sprop-parameter-sets|sprop-vps|sprop-sps|sprop-pps)=([^;\s]+)/g;
    let match;
    while ((match = pattern.exec(parameters))) {
      for (const encoded of match[1].split(",")) {
        let decoded;
        try {
          decoded = atob(encoded);
        } catch (error) {
          throw new Error("Invalid RTSP video parameters");
        }
        if (!decoded.length || decoded.length > MAX_RESPONSE) {
          throw new Error("Invalid RTSP video parameters");
        }
        nals.push(Uint8Array.from(decoded, (char) => char.charCodeAt(0)));
      }
    }
    return { codec, payload, control: controlUrl(control, base), nals };
  }
  class RtspReader {
    constructor(response, packet) {
      this.buffer = new ByteQueue();
      this.response = response;
      this.packet = packet;
    }
    feed(data) {
      this.buffer.append(data);
      while (this.buffer.length) {
        if (this.buffer.peek(1)[0] === 36) {
          if (this.buffer.length < 4) return;
          const header = this.buffer.peek(4);
          const size = view(header).getUint16(2);
          if (!size) throw new Error("Invalid RTSP interleaved packet");
          if (this.buffer.length < size + 4) return;
          this.buffer.read(4);
          this.packet(header[1], this.buffer.read(size));
          continue;
        }
        const prefix = text(
          this.buffer.peek(Math.min(this.buffer.length, MAX_RESPONSE)),
        );
        const end = prefix.indexOf("\r\n\r\n");
        if (end < 0) {
          if (this.buffer.length >= MAX_RESPONSE) {
            throw new Error("RTSP response exceeded the limit");
          }
          return;
        }
        const lines = prefix.slice(0, end).split("\r\n");
        const status = /^RTSP\/1\.0 (\d{3}) /.exec(lines.shift());
        if (!status) throw new Error("Invalid RTSP response");
        const headers = Object.create(null);
        for (const line of lines) {
          const separator = line.indexOf(":");
          if (separator < 1) throw new Error("Invalid RTSP response headers");
          const name = line.slice(0, separator).toLowerCase();
          if (Object.prototype.hasOwnProperty.call(headers, name)) {
            throw new Error("Duplicate RTSP response header");
          }
          headers[name] = line.slice(separator + 1).trim();
        }
        const length = headers["content-length"] || "0";
        if (!/^\d+$/.test(length) || +length > MAX_RESPONSE) {
          throw new Error("Invalid RTSP response length");
        }
        if (this.buffer.length < end + 4 + +length) return;
        this.buffer.read(end + 4);
        this.response(+status[1], headers, text(this.buffer.read(+length)));
      }
    }
  }
  class RtpVideo {
    constructor(description, output) {
      this.codec = description.codec;
      this.payload = description.payload;
      this.output = output;
      this.parameters = new Map();
      description.nals.forEach((nal) => this.remember(nal));
      this.sequence = null;
      this.timestamp = null;
      this.reset();
    }
    reset() {
      this.nals = [];
      this.size = 0;
      this.fragment = null;
    }
    kind(nal) {
      return this.codec === 36 ? nal[0] >> 1 & 63 : nal[0] & 31;
    }
    remember(nal) {
      const kind = this.kind(nal);
      if (
        this.codec === 36 ? [32, 33, 34].includes(kind) : [7, 8].includes(kind)
      ) {
        this.parameters.set(kind, nal.slice());
      }
    }
    add(nal) {
      if (nal.length < (this.codec === 36 ? 2 : 1)) {
        throw new Error("Invalid RTP video unit");
      }
      this.size += nal.length + 4;
      if (this.size > MAX_FRAME) {
        throw new Error("RTP video frame exceeded the limit");
      }
      this.remember(nal);
      this.nals.push(nal);
    }
    feed(packet) {
      if (packet.length < 12 || packet[0] >> 6 !== 2) {
        throw new Error("Invalid RTP packet");
      }
      if ((packet[1] & 127) !== this.payload) return;
      const header = view(packet);
      let offset = 12 + (packet[0] & 15) * 4;
      if (offset > packet.length) throw new Error("Invalid RTP sources");
      if (packet[0] & 16) {
        if (offset + 4 > packet.length) {
          throw new Error("Invalid RTP extension");
        }
        offset += 4 + header.getUint16(offset + 2) * 4;
      }
      const padding = packet[0] & 32 ? packet[packet.length - 1] : 0;
      const end = packet.length - padding;
      if ((packet[0] & 32 && !padding) || offset >= end) {
        throw new Error("Invalid RTP payload");
      }
      const sequence = header.getUint16(2);
      const timestamp = header.getUint32(4);
      if (this.sequence !== null && sequence !== (this.sequence + 1 & 65535)) {
        throw new Error("RTP video packet sequence interrupted");
      }
      this.sequence = sequence;
      if (this.timestamp !== timestamp) {
        if (this.fragment) throw new Error("Incomplete RTP video unit");
        this.reset();
        this.timestamp = timestamp;
      }
      this.unit(packet.subarray(offset, end));
      if (packet[1] & 128) {
        if (this.fragment) throw new Error("Incomplete RTP video unit");
        const start = new Uint8Array([0, 0, 0, 1]);
        let frame = concat(...this.nals.map((nal) => concat(start, nal)));
        if (isKeyframe(frame, this.codec)) {
          frame = concat(
            ...Array.from(
              this.parameters.values(),
              (nal) => concat(start, nal),
            ),
            frame,
          );
        }
        if (frame.length > MAX_FRAME) {
          throw new Error("RTP video frame exceeded the limit");
        }
        if (frame.length) this.output(frame, this.codec);
        this.reset();
      }
    }
    unit(data) {
      const hevc = this.codec === 36;
      if (data.length < (hevc ? 2 : 1)) {
        throw new Error("Invalid RTP video unit");
      }
      const kind = this.kind(data);
      if (kind === (hevc ? 48 : 24)) {
        if (this.fragment) throw new Error("Incomplete RTP video unit");
        let offset = hevc ? 2 : 1;
        while (offset < data.length) {
          if (offset + 2 > data.length) {
            throw new Error("Invalid RTP aggregation");
          }
          const size = view(data).getUint16(offset);
          offset += 2;
          if (!size || offset + size > data.length) {
            throw new Error("Invalid RTP aggregation");
          }
          this.add(data.subarray(offset, offset + size));
          offset += size;
        }
      } else if (kind === (hevc ? 49 : 28)) {
        const offset = hevc ? 3 : 2;
        if (data.length <= offset) throw new Error("Invalid RTP fragment");
        const flags = data[offset - 1];
        const start = Boolean(flags & 128);
        const end = Boolean(flags & 64);
        const type = flags & (hevc ? 63 : 31);
        if (start && end || type >= (hevc ? 48 : 24)) {
          throw new Error("Invalid RTP fragment flags");
        }
        if (start) {
          if (this.fragment) throw new Error("Incomplete RTP video unit");
          const header = hevc
            ? new Uint8Array([data[0] & 129 | type << 1, data[1]])
            : new Uint8Array([data[0] & 224 | type]);
          this.fragment = { parts: [header], size: header.length, type };
        }
        if (!this.fragment || this.fragment.type !== type) {
          throw new Error("Missing RTP video fragment");
        }
        this.fragment.parts.push(data.subarray(offset));
        this.fragment.size += data.length - offset;
        if (this.fragment.size + this.size > MAX_FRAME) {
          throw new Error("RTP video frame exceeded the limit");
        }
        if (end) {
          this.add(concat(...this.fragment.parts));
          this.fragment = null;
        }
      } else if (kind < (hevc ? 48 : 24)) {
        if (this.fragment) throw new Error("Incomplete RTP video unit");
        this.add(data);
      } else throw new Error("Unsupported RTP video packetization");
    }
  }
  class Session {
    constructor(config, native, callbacks) {
      validate(config);
      this.config = config;
      this.native = native;
      this.callbacks = callbacks;
      this.id = nextId++;
      this.state = "closed";
      this.peer = null;
      this.opened = false;
      this.pending = new Map();
      this.sequence = 0;
      this.session = "";
      this.hashes = new Map();
      this.hashSequence = 0;
      this.nonceCount = 0;
      this.reader = new RtspReader(
        (status, headers, body) => this.response(status, headers, body),
        (channel, packet) => {
          if (this.state === "playing" && channel === this.rtpChannel) {
            this.video.feed(packet);
          }
        },
      );
    }
    async open() {
      if (this.state !== "closed") return;
      this.state = "account";
      this.started = performance.now();
      this.callbacks.status("Connecting to Imou Life");
      this.timer = setInterval(() => {
        try {
          this.tick();
        } catch (error) {
          this.fail(error);
        }
      }, 1000);
      try {
        const hd = Boolean(this.config.hd);
        if (this.config.local) this.url = localUrl(this.config, hd);
        else {
          const config = this.config.account;
          let account = accounts.get(config.app_id);
          if (
            !account || account.config.region !== config.region ||
            account.config.app_secret !== config.app_secret
          ) {
            account = new Account(config);
            accounts.set(config.app_id, account);
          }
          const value = await account.stream(this.config, hd);
          if (this.state !== "account") return;
          this.url = streamUrl(value);
        }
        this.aggregate = this.url.href;
        this.state = "connecting";
        this.opened = true;
        this.native.postMessage({
          command: "tcp-open",
          id: this.id,
          host: this.url.hostname,
          port: Number(this.url.port) || 554,
          ...(this.config.local && this.config.local.certificate_sha256
            ? {
              certificate_sha256: this.config.local.certificate_sha256,
              seed: crypto.getRandomValues(new Uint8Array(48)).buffer,
            }
            : {}),
        });
      } catch (error) {
        if (this.state !== "closed" && this.state !== "closing") {
          this.fail(error);
        }
      }
    }
    hash(data, algorithm) {
      return new Promise((resolve, reject) => {
        const request = ++this.hashSequence;
        this.hashes.set(request, { resolve, reject });
        this.native.postMessage({
          command: "digest-hash",
          id: this.id,
          request,
          data,
          algorithm,
        });
      });
    }
    async authorization(method, address) {
      const auth = this.authentication;
      const local = this.config.local;
      const algorithm = auth.algorithm.replace(/-sess$/, "");
      const cnonce = uuid().replace(/-/g, "");
      const nc = (++this.nonceCount).toString(16).padStart(8, "0");
      let first = await this.hash(
        local.username + ":" + auth.realm + ":" + local.password,
        algorithm,
      );
      if (auth.algorithm.endsWith("-sess")) {
        first = await this.hash(
          first + ":" + auth.nonce + ":" + cnonce,
          algorithm,
        );
      }
      const second = await this.hash(method + ":" + address, algorithm);
      const response = await this.hash(
        first + ":" + auth.nonce + ":" +
          (auth.qop ? nc + ":" + cnonce + ":auth:" : "") + second,
        algorithm,
      );
      const fields = {
        username: quoted(local.username),
        realm: quoted(auth.realm),
        nonce: quoted(auth.nonce),
        uri: quoted(address),
        response: quoted(response),
        algorithm: auth.algorithm,
      };
      if (auth.opaque !== undefined) fields.opaque = quoted(auth.opaque);
      if (auth.qop) {
        Object.assign(fields, { qop: "auth", nc, cnonce: quoted(cnonce) });
      } else if (auth.algorithm.endsWith("-sess")) {
        fields.cnonce = quoted(cnonce);
      }
      return "Digest " +
        Object.keys(fields).map((key) => key + "=" + fields[key]).join(", ");
    }
    send(method, address = this.aggregate, headers = {}, retried = false) {
      if (this.pending.size >= 4) {
        throw new Error("Too many pending RTSP requests");
      }
      const sequence = ++this.sequence;
      const fields = Object.assign(
        {
          CSeq: sequence,
          "User-Agent": "CameraViewer",
        },
        this.session ? { Session: this.session } : {},
        headers,
      );
      this.pending.set(sequence, {
        method,
        address,
        headers,
        retried,
        sent: performance.now(),
      });
      if (this.authentication) {
        this.authorization(method, address).then((authorization) => {
          if (this.pending.has(sequence)) {
            this.transmit(
              sequence,
              method,
              address,
              Object.assign(fields, { Authorization: authorization }),
            );
          }
        }).catch((error) => this.fail(error));
      } else this.transmit(sequence, method, address, fields);
    }
    transmit(sequence, method, address, fields) {
      const message = bytes(
        method + " " + address + " RTSP/1.0\r\n" +
          Object.keys(fields).map((key) => key + ": " + fields[key]).join(
            "\r\n",
          ) +
          "\r\n\r\n",
      );
      this.native.postMessage({
        command: "tcp-send",
        id: this.id,
        data: message.buffer,
      });
    }
    response(status, headers, body) {
      if (!/^\d+$/.test(headers.cseq || "")) {
        throw new Error("Invalid RTSP sequence");
      }
      const sequence = Number(headers.cseq);
      const pending = this.pending.get(sequence);
      if (!pending) throw new Error("Unexpected RTSP response");
      this.pending.delete(sequence);
      if (this.state === "closing") {
        if (pending.method === "TEARDOWN") this.finishClose();
        return;
      }
      if (status !== 200) {
        if (status === 401 && this.config.local && !pending.retried) {
          this.authentication = challenge(headers["www-authenticate"]);
          this.nonceCount = 0;
          this.send(pending.method, pending.address, pending.headers, true);
          return;
        }
        throw new Error("Imou stream rejected (RTSP " + status + ")");
      }
      if (pending.method === "DESCRIBE") {
        const base = headers["content-base"]
          ? streamUrl(
            controlUrl(headers["content-base"], this.url),
            this.url.local,
          )
          : this.url;
        const description = videoDescription(body, base);
        this.video = new RtpVideo(description, (payload, codec) => {
          this.lastVideo = performance.now();
          this.callbacks.video(payload, codec);
        });
        this.state = "setup";
        this.send("SETUP", description.control, {
          Transport: "RTP/AVP/TCP;unicast;interleaved=0-1",
        });
      } else if (pending.method === "SETUP") {
        const session = /^([A-Za-z0-9_.-]{1,256})(?:;.*)?$/.exec(
          headers.session || "",
        );
        const channel = /(?:^|;)\s*interleaved=(\d+)-(\d+)/i.exec(
          headers.transport || "",
        );
        if (
          !session || !/^RTP\/AVP\/TCP(?:;|$)/i.test(headers.transport || "") ||
          !channel || +channel[1] > 254 || +channel[2] !== +channel[1] + 1
        ) {
          throw new Error("Invalid RTSP session transport");
        }
        this.session = session[1];
        this.rtpChannel = Number(channel[1]);
        const timeout = /;\s*timeout=(\d+)/.exec(headers.session);
        this.keepalive = Math.max(
          5000,
          Math.min(25000, timeout ? +timeout[1] * 500 : 25000),
        );
        this.state = "starting";
        this.send("PLAY", this.aggregate, { Range: "npt=0.000-" });
      } else if (pending.method === "PLAY") {
        this.state = "playing";
        this.peer = true;
        this.lastKeepalive = performance.now();
        this.lastVideo = this.lastKeepalive;
        this.callbacks.status("Imou Life video connected");
      }
    }
    event(event) {
      if (event.id !== this.id || this.state === "closed") return;
      try {
        if (event.type === "digest-hash") {
          const pending = this.hashes.get(event.request);
          if (!pending) return;
          this.hashes.delete(event.request);
          if (
            typeof event.value !== "string" ||
            !/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/.test(event.value)
          ) pending.reject(new Error("Camera authentication failed"));
          else pending.resolve(event.value);
        } else if (event.type === "tcp-ready" && this.state === "connecting") {
          this.state = "describe";
          this.send("DESCRIBE", this.url.href, { Accept: "application/sdp" });
        } else if (event.type === "tcp-data") {
          this.reader.feed(new Uint8Array(event.data));
        } else if (event.type === "tcp-error") {
          throw new Error(event.detail);
        }
      } catch (error) {
        if (this.state === "closing") this.finishClose();
        else this.fail(error);
      }
    }
    tick() {
      const now = performance.now();
      if (this.state !== "playing" && now - this.started > 45000) {
        throw new Error("Imou connection timed out");
      }
      for (const request of this.pending.values()) {
        if (now - request.sent > 15000) {
          throw new Error("Imou RTSP request timed out");
        }
      }
      if (
        this.state === "playing" && now - this.lastKeepalive >= this.keepalive
      ) {
        this.lastKeepalive = now;
        this.send("OPTIONS");
      }
      if (this.state === "playing" && now - this.lastVideo > 15000) {
        throw new Error("Imou video stalled, reconnecting");
      }
    }
    cancelAuthentication() {
      this.hashes.forEach((pending) =>
        pending.reject(new Error("Camera authentication stopped"))
      );
      this.hashes.clear();
    }
    close() {
      if (this.state === "closed" || this.state === "closing") return;
      clearInterval(this.timer);
      this.state = "closing";
      this.peer = null;
      if (!this.opened || !this.session) {
        this.finishClose();
        return;
      }
      this.closeListener = (event) => this.event(event.data);
      this.native.addEventListener("message", this.closeListener);
      this.closeTimer = setTimeout(() => this.finishClose(), 2000);
      this.pending.clear();
      this.cancelAuthentication();
      this.send("TEARDOWN");
    }
    finishClose() {
      clearInterval(this.timer);
      clearTimeout(this.closeTimer);
      if (this.closeListener) {
        this.native.removeEventListener("message", this.closeListener);
      }
      if (this.opened) {
        this.native.postMessage({ command: "tcp-close", id: this.id });
      }
      this.opened = false;
      this.pending.clear();
      this.cancelAuthentication();
      this.state = "closed";
      this.reader.buffer = new ByteQueue();
      this.video = null;
    }
    fail(error) {
      if (this.state === "closed" || this.state === "closing") return;
      this.close();
      this.callbacks.error(error.message);
    }
  }
  const api = {
    validate,
    signature,
    Account,
    streamUrl,
    localUrl,
    challenge,
    videoDescription,
    RtspReader,
    RtpVideo,
    Session,
  };
  root.ImouVideo = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
