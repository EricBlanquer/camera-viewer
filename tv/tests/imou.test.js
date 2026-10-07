"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { concat, bytes, text } = require("../web/protocol.js");
const imou = require("../web/imou.js");
const account = {
  app_id: "testApp",
  app_secret: "testSecret",
  region: "Europe",
};
const camera = {
  type: "imou",
  name: "Kitchen",
  device_id: "testDevice",
  channel_id: "0",
  account,
};
const address =
  "rtsp://liveopenrtspproxy-test.imoulife.com:8554/live?expire=123&digest=test";
const description = { codec: 36, payload: 98, nals: [] };
function rtp(payload, sequence, marker = true, timestamp = 1) {
  const packet = new Uint8Array(12 + payload.length);
  const header = new DataView(packet.buffer);
  packet[0] = 128;
  packet[1] = 98 | (marker ? 128 : 0);
  header.setUint16(2, sequence);
  header.setUint32(4, timestamp);
  packet.set(payload, 12);
  return packet;
}
function interleaved(channel, packet) {
  return concat(
    Uint8Array.of(36, channel, packet.length >> 8, packet.length & 255),
    packet,
  );
}
function response(sequence, body = "", headers = "") {
  return bytes(
    "RTSP/1.0 200 OK\r\nCSeq: " + sequence + "\r\n" + headers +
      "Content-Length: " + bytes(body).length + "\r\n\r\n" + body,
  );
}
test("Imou signature matches the official HMAC-SHA256 vector", async () => {
  assert.equal(
    await imou.signature(
      "test123456789test123456789",
      1706511734,
      "f5a1ae2d-c09c-4d39-a744-83a5c2c653c2",
    ),
    "xjhCQBoJ9hRDsCjyDcHjtDNzRZ3ZJezcawsfWeiaoxU=",
  );
});
test("account shares authentication, refreshes an expired token, and signs each request", async () => {
  const calls = [];
  const streams = [];
  let expired = true;
  const client = new imou.Account(account, async (host, method, body) => {
    assert.equal(host, "openapi-fk.easy4ip.com");
    assert.equal(
      body.system.sign,
      await imou.signature(
        account.app_secret,
        body.system.time,
        body.system.nonce,
      ),
    );
    calls.push(method);
    if (method === "accessToken") {
      return {
        result: {
          code: "0",
          data: {
            accessToken: "testToken" + calls.length,
            expireTime: 3600,
            currentDomain: "https://openapi-fk.easy4ip.com:443",
          },
        },
      };
    }
    assert.ok(body.params.token.startsWith("testToken"));
    assert.equal(body.params.deviceId, camera.device_id);
    streams.push(body.params.streamId);
    if (expired) {
      expired = false;
      return { result: { code: "TK1002" } };
    }
    return { result: { code: "0", data: { url: address } } };
  });
  await Promise.all([client.authenticate(), client.authenticate()]);
  assert.equal(calls.length, 1);
  assert.equal(await client.stream(camera), address);
  assert.deepEqual(calls, [
    "accessToken",
    "getStreamUrl",
    "accessToken",
    "getStreamUrl",
  ]);
  await client.stream(camera);
  assert.equal(calls.filter((method) => method === "accessToken").length, 2);
  await client.stream(camera, true);
  assert.deepEqual(streams, [1, 1, 1, 0]);
});
test("account rejects unknown gateways and hides vendor response messages", async () => {
  const client = new imou.Account(
    account,
    async () => ({
      result: {
        code: "0",
        data: {
          accessToken: "testToken",
          expireTime: 3600,
          currentDomain: "https://untrusted.invalid",
        },
      },
    }),
  );
  await assert.rejects(client.authenticate(), /Invalid Imou account gateway/);
  const rejected = new imou.Account(account, async () => ({
    result: {
      code: "SN1001",
      msg: "sensitive server detail",
    },
  }));
  await assert.rejects(
    rejected.authenticate(),
    /^Error: Imou account request rejected \(SN1001\)$/,
  );
});
test("camera and stream validation reject credentials in URLs and untrusted hosts", () => {
  imou.validate(camera);
  assert.equal(
    imou.streamUrl(address).hostname,
    "liveopenrtspproxy-test.imoulife.com",
  );
  for (
    const url of [
      "rtsp://untrusted.invalid/live",
      address + "\r\nHeader: bad",
      address.replace("rtsp://", "rtsp://user:password@"),
      address + "#fragment",
    ]
  ) {
    assert.throws(() => imou.streamUrl(url), /Invalid Imou stream address/);
  }
  assert.throws(
    () => imou.validate({ ...camera, channel_id: "0\r\n" }),
    /Invalid Imou/,
  );
  assert.throws(
    () =>
      imou.validate({ ...camera, account: { ...account, region: "unknown" } }),
    /Invalid Imou/,
  );
});
test("legacy TV URL parsing still resolves RTSP endpoints and relative tracks", (t) => {
  const OriginalURL = global.URL;
  global.URL = class extends OriginalURL {
    constructor(value, base) {
      if (String(value).startsWith("rtsp:")) {
        throw new Error("Unsupported URL authority");
      }
      super(value, base);
    }
  };
  t.after(() => {
    global.URL = OriginalURL;
  });
  const endpoint = imou.streamUrl(address);
  assert.equal(endpoint.hostname, "liveopenrtspproxy-test.imoulife.com");
  assert.equal(endpoint.port, "8554");
  const video = imou.videoDescription(
    "m=video 0 RTP/AVP 98\r\na=rtpmap:98 H265/90000\r\na=control:trackID=0\r\n",
    endpoint,
  );
  assert.equal(
    video.control,
    "rtsp://liveopenrtspproxy-test.imoulife.com:8554/trackID=0",
  );
});
test("SDP selects video, resolves its control, and rejects unsupported ordering", () => {
  const base = new URL(address);
  const sdp = "v=0\r\na=control:*\r\nm=video 0 RTP/AVP 98\r\n" +
    "a=rtpmap:98 H265/90000\r\na=control:trackID=0\r\n" +
    "a=fmtp:98 sprop-vps=QAEB;sprop-sps=QgEC;sprop-pps=RAED\r\n" +
    "m=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/16000\r\n";
  const video = imou.videoDescription(sdp, base);
  assert.equal(video.codec, 36);
  assert.equal(video.payload, 98);
  assert.equal(
    video.control,
    "rtsp://liveopenrtspproxy-test.imoulife.com:8554/trackID=0",
  );
  assert.deepEqual(video.nals.map((nal) => Array.from(nal)), [[64, 1, 1], [
    66,
    1,
    2,
  ], [68, 1, 3]]);
  assert.throws(
    () =>
      imou.videoDescription(
        sdp.replace("sprop-vps=", "sprop-max-don-diff=1;sprop-vps="),
        base,
      ),
    /Unsupported/,
  );
  assert.throws(
    () =>
      imou.videoDescription(
        sdp.replace("trackID=0", "rtsp://untrusted.invalid/track"),
        base,
      ),
    /Invalid RTSP control/,
  );
});
test("RTSP framing handles byte fragmentation and mixed RTP, RTCP, and responses", () => {
  const received = [];
  const reader = new imou.RtspReader(
    (status, headers, body) => received.push([status, headers.cseq, body]),
    (channel, packet) => received.push([channel, Array.from(packet)]),
  );
  const data = concat(
    response(1, "v=0\r\n"),
    interleaved(0, Uint8Array.of(1, 2, 3)),
    interleaved(1, Uint8Array.of(4)),
    response(2),
  );
  for (const byte of data) reader.feed(Uint8Array.of(byte));
  assert.deepEqual(received, [[200, "1", "v=0\r\n"], [0, [1, 2, 3]], [1, [4]], [
    200,
    "2",
    "",
  ]]);
  assert.equal(reader.buffer.length, 0);
});
test("RTSP framing rejects duplicate, invalid, and oversized lengths", () => {
  for (
    const headers of [
      "Content-Length: 65537",
      "Content-Length: -1",
      "CSeq: 1\r\nCSeq: 2",
    ]
  ) {
    const reader = new imou.RtspReader(() => {}, () => {});
    assert.throws(
      () => reader.feed(bytes("RTSP/1.0 200 OK\r\n" + headers + "\r\n\r\n")),
      /RTSP/,
    );
  }
  const reader = new imou.RtspReader(() => {}, () => {});
  assert.throws(() => reader.feed(new Uint8Array(65536).fill(65)), /exceeded/);
});
test("HEVC fragments reconstruct an access unit across sequence wrap with parameters", () => {
  const output = [];
  const video = new imou.RtpVideo({
    ...description,
    nals: [Uint8Array.of(64, 1, 9)],
  }, (frame, codec) => output.push([Array.from(frame), codec]));
  video.feed(rtp(Uint8Array.of(98, 1, 128 | 19, 7, 8), 65535, false));
  assert.equal(output.length, 0);
  video.feed(rtp(Uint8Array.of(98, 1, 64 | 19, 9, 10), 0));
  assert.deepEqual(output, [[[
    0,
    0,
    0,
    1,
    64,
    1,
    9,
    0,
    0,
    0,
    1,
    38,
    1,
    7,
    8,
    9,
    10,
  ], 36]]);
});
test("H264 aggregation and fragments produce complete Annex B frames", () => {
  const output = [];
  const video = new imou.RtpVideo(
    { codec: 27, payload: 98, nals: [] },
    (frame) => output.push(Array.from(frame)),
  );
  video.feed(rtp(Uint8Array.of(24, 0, 2, 7, 1, 0, 2, 8, 2), 1, false));
  video.feed(rtp(Uint8Array.of(28, 128 | 5, 3), 2, false));
  video.feed(rtp(Uint8Array.of(28, 64 | 5, 4), 3));
  assert.equal(output.length, 1);
  assert.deepEqual(output[0].slice(-7), [0, 0, 0, 1, 5, 3, 4]);
});
test("RTP source, extension, and padding headers leave the video payload intact", () => {
  const output = [];
  const video = new imou.RtpVideo(
    description,
    (frame) => output.push(Array.from(frame)),
  );
  const header = rtp(Uint8Array.of(), 1).slice(0, 12);
  header[0] = 128 | 32 | 16 | 1;
  const packet = concat(
    header,
    Uint8Array.of(0, 0, 0, 1),
    Uint8Array.of(0, 0, 0, 1, 5, 6, 7, 8),
    Uint8Array.of(2, 1, 7, 0, 2),
  );
  video.feed(packet);
  assert.deepEqual(output, [[0, 0, 0, 1, 2, 1, 7]]);
});
test("RTP rejects gaps, missing fragments, malformed aggregation and excessive frame sizes", () => {
  const video = () =>
    new imou.RtpVideo(description, () => assert.fail("Invalid frame emitted"));
  const broken = video();
  broken.feed(rtp(Uint8Array.of(98, 1, 128 | 19, 1), 1, false));
  assert.throws(
    () => broken.feed(rtp(Uint8Array.of(98, 1, 64 | 19, 2), 3)),
    /sequence interrupted/,
  );
  assert.throws(
    () => video().feed(rtp(Uint8Array.of(98, 1, 64 | 19, 2), 1)),
    /Missing RTP/,
  );
  assert.throws(
    () => video().feed(rtp(Uint8Array.of(96, 1, 0, 3, 2, 1), 1)),
    /aggregation/,
  );
  assert.throws(
    () => video().unit(new Uint8Array(8 * 1024 * 1024 + 1)),
    /exceeded/,
  );
});
test("session negotiates video-only TCP playback, keeps it alive and acknowledges stop", async (t) => {
  const original = global.XMLHttpRequest;
  global.XMLHttpRequest = class {
    open(method, url) {
      this.url = url;
    }
    setRequestHeader() {}
    send() {
      const data = this.url.endsWith("accessToken")
        ? { accessToken: "testToken", expireTime: 3600 }
        : { url: address };
      this.status = 200;
      this.responseText = JSON.stringify({ result: { code: "0", data } });
      queueMicrotask(() => this.onload());
    }
  };
  t.after(() => {
    global.XMLHttpRequest = original;
  });
  const messages = [];
  const listeners = new Set();
  const native = {
    postMessage: (message) => messages.push(message),
    addEventListener: (name, callback) => listeners.add(callback),
    removeEventListener: (name, callback) => listeners.delete(callback),
  };
  const frames = [];
  const errors = [];
  const session = new imou.Session(camera, native, {
    status() {},
    video: (frame) => frames.push(frame),
    error: (message) => errors.push(message),
  });
  t.after(() => session.finishClose());
  await session.open();
  assert.equal(messages[0].command, "tcp-open");
  assert.equal(messages[0].port, 8554);
  function event(data) {
    session.event({ id: session.id, type: "tcp-data", data: data.buffer });
  }
  session.event({ id: session.id, type: "tcp-ready" });
  assert.ok(text(new Uint8Array(messages.at(-1).data)).startsWith("DESCRIBE "));
  const sdp =
    "v=0\r\nm=video 0 RTP/AVP 98\r\na=rtpmap:98 H265/90000\r\na=control:trackID=0\r\n" +
    "m=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/16000\r\na=control:trackID=1\r\n";
  event(response(1, sdp, "Content-Base: " + address + "/\r\n"));
  const setup = text(new Uint8Array(messages.at(-1).data));
  assert.ok(setup.startsWith("SETUP "));
  assert.ok(setup.includes("trackID=0"));
  assert.ok(setup.includes("interleaved=0-1"));
  assert.ok(!setup.includes("trackID=1"));
  event(
    response(
      2,
      "",
      "Session: testSession;timeout=60\r\nTransport: RTP/AVP/TCP;unicast;interleaved=2-3\r\n",
    ),
  );
  assert.ok(text(new Uint8Array(messages.at(-1).data)).startsWith("PLAY "));
  event(response(3));
  assert.equal(session.state, "playing");
  assert.equal(session.peer, true);
  event(interleaved(3, Uint8Array.of(1, 2, 3)));
  assert.equal(frames.length, 0);
  event(interleaved(2, rtp(Uint8Array.of(38, 1, 7), 1)));
  assert.equal(frames.length, 1);
  session.lastKeepalive -= 26000;
  session.tick();
  assert.ok(text(new Uint8Array(messages.at(-1).data)).startsWith("OPTIONS "));
  event(response(4));
  session.lastVideo -= 16000;
  assert.throws(() => session.tick(), /video stalled/);
  session.close();
  assert.equal(session.state, "closing");
  assert.equal(listeners.size, 1);
  assert.ok(text(new Uint8Array(messages.at(-1).data)).startsWith("TEARDOWN "));
  event(response(5));
  assert.equal(session.state, "closed");
  assert.equal(listeners.size, 0);
  assert.equal(messages.at(-1).command, "tcp-close");
  assert.deepEqual(errors, []);
});
test("stopping during authentication prevents a late socket connection", async (t) => {
  const original = global.XMLHttpRequest;
  const requests = [];
  global.XMLHttpRequest = class {
    open() {}
    setRequestHeader() {}
    send() {
      requests.push(this);
    }
  };
  t.after(() => {
    global.XMLHttpRequest = original;
  });
  const messages = [];
  const session = new imou.Session(
    { ...camera, account: { ...account, app_id: "stoppedApp" } },
    {
      postMessage: (message) => messages.push(message),
    },
    {
      status() {},
      error() {
        assert.fail("Late error");
      },
    },
  );
  t.after(() => session.finishClose());
  const opening = session.open();
  while (!requests.length) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  session.close();
  let xhr = requests[0];
  xhr.status = 200;
  xhr.responseText = JSON.stringify({
    result: { code: "0", data: { accessToken: "testToken", expireTime: 3600 } },
  });
  xhr.onload();
  while (requests.length < 2) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  xhr = requests[1];
  xhr.status = 200;
  xhr.responseText = JSON.stringify({
    result: { code: "0", data: { url: address } },
  });
  xhr.onload();
  await opening;
  assert.equal(session.state, "closed");
  assert.deepEqual(messages, []);
});

const nodeCrypto = require("node:crypto");
const localCamera = {
  type: "imou",
  name: "Kitchen",
  local: {
    host: "192.168.1.210",
    port: 554,
    channel: 1,
    username: "admin",
    password: "test-camera-password",
    certificate_sha256: "a".repeat(64),
  },
};
function reply(sequence, status, headers = "", body = "") {
  return bytes(
    "RTSP/1.0 " + status + " Status\r\nCSeq: " + sequence + "\r\n" + headers +
      "Content-Length: " + bytes(body).length + "\r\n\r\n" + body,
  );
}
function fixture(config = localCamera, delayed = false) {
  const messages = [], errors = [], hashes = [];
  let session;
  const native = {
    postMessage(message) {
      messages.push(message);
      if (message.command !== "digest-hash") return;
      const hash = () =>
        session.event({
          type: "digest-hash",
          id: session.id,
          request: message.request,
          value: nodeCrypto.createHash(
            message.algorithm === "MD5" ? "md5" : "sha256",
          ).update(message.data).digest("hex"),
        });
      if (delayed) hashes.push(hash);
      else queueMicrotask(hash);
    },
    addEventListener() {},
    removeEventListener() {},
  };
  session = new imou.Session(config, native, {
    status() {},
    video() {},
    error(message) {
      errors.push(message);
    },
  });
  const event = (data) =>
    session.event({ type: "tcp-data", id: session.id, data: data.buffer });
  return { session, messages, errors, hashes, event };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
test("local Imou configuration permits private addresses and enforces camera credentials and certificate pins", () => {
  imou.validate(localCamera);
  const endpoint = imou.localUrl(localCamera);
  assert.equal(
    endpoint.href,
    "rtsp://192.168.1.210:554/cam/realmonitor?channel=1&subtype=1",
  );
  assert.equal(endpoint.local, true);
  assert.equal(
    imou.localUrl(localCamera, true).href,
    "rtsp://192.168.1.210:554/cam/realmonitor?channel=1&subtype=0",
  );
  for (
    const change of [
      { host: "127.0.0.1" },
      { host: "192.168.01.210" },
      { host: "8.8.8.8" },
      { host: "192.168.1.256" },
      { host: "192.168.1.210\r\n" },
      { password: "" },
      { username: "admin\r\n" },
      { port: 0 },
      { channel: 0 },
      { certificate_sha256: "" },
      { certificate_sha256: ["a".repeat(64)] },
    ]
  ) {
    assert.throws(
      () =>
        imou.validate({
          ...localCamera,
          local: { ...localCamera.local, ...change },
        }),
      /Invalid local Imou/,
    );
  }
  const description = imou.videoDescription(
    "m=video 0 RTP/AVP 98\r\na=rtpmap:98 H265/90000\r\na=control:rtsp://192.168.1.210:554/trackID=0\r\n",
    endpoint,
  );
  assert.equal(description.control, "rtsp://192.168.1.210:554/trackID=0");
  assert.throws(
    () =>
      imou.videoDescription(
        "m=video 0 RTP/AVP 98\r\na=rtpmap:98 H265/90000\r\na=control:rtsp://192.168.1.211:554/trackID=0\r\n",
        endpoint,
      ),
    /Invalid RTSP control/,
  );
});
test("local video connects without account requests and authenticates DESCRIBE, SETUP, PLAY and stop", async (t) => {
  const original = global.XMLHttpRequest;
  global.XMLHttpRequest = class {
    constructor() {
      assert.fail("Local video requested cloud access");
    }
  };
  t.after(() => {
    global.XMLHttpRequest = original;
  });
  const f = fixture();
  t.after(() => f.session.finishClose());
  await f.session.open();
  assert.equal(f.messages[0].host, localCamera.local.host);
  assert.equal(
    f.messages[0].certificate_sha256,
    localCamera.local.certificate_sha256,
  );
  assert.equal(f.messages[0].seed.byteLength, 48);
  f.session.event({ type: "tcp-ready", id: f.session.id });
  f.event(
    reply(
      1,
      401,
      'WWW-Authenticate: Digest realm="Login", nonce="testnonce", qop="auth", algorithm=MD5\r\n',
    ),
  );
  await settle();
  const authenticated = text(new Uint8Array(f.messages.at(-1).data));
  assert.match(authenticated, /Authorization: Digest username="admin"/);
  assert.ok(!authenticated.includes(localCamera.local.password));
  const auth = imou.challenge(
    authenticated.match(/Authorization: (.*)\r\n/)[1],
  );
  const md5 = (value) =>
    nodeCrypto.createHash("md5").update(value).digest("hex");
  const uri = imou.localUrl(localCamera).href;
  assert.equal(
    auth.response,
    md5(
      md5("admin:Login:" + localCamera.local.password) + ":testnonce:" +
        auth.nc + ":" + auth.cnonce + ":auth:" + md5("DESCRIBE:" + uri),
    ),
  );
  f.event(
    reply(
      2,
      200,
      "Content-Base: " + uri + "/\r\n",
      "m=video 0 RTP/AVP 98\r\na=rtpmap:98 H265/90000\r\na=control:trackID=0\r\n",
    ),
  );
  await settle();
  assert.match(
    text(new Uint8Array(f.messages.at(-1).data)),
    /^SETUP .*\r\n(?:.*\r\n)*Authorization: Digest /,
  );
  f.event(
    reply(
      3,
      200,
      "Session: testSession;timeout=60\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
    ),
  );
  await settle();
  f.event(reply(4, 200));
  assert.equal(f.session.state, "playing");
  f.session.close();
  await settle();
  assert.ok(
    text(new Uint8Array(f.messages.at(-1).data)).startsWith("TEARDOWN "),
  );
  f.event(reply(5, 200));
  assert.equal(f.session.state, "closed");
  assert.deepEqual(f.errors, []);
});
test("local authentication stops after rejected credentials without cloud fallback", async (t) => {
  const f = fixture();
  t.after(() => f.session.finishClose());
  await f.session.open();
  f.session.event({ type: "tcp-ready", id: f.session.id });
  f.event(
    reply(
      1,
      401,
      'WWW-Authenticate: Digest realm="Login", nonce="testnonce"\r\n',
    ),
  );
  await settle();
  f.event(
    reply(
      2,
      401,
      'WWW-Authenticate: Digest realm="Login", nonce="testnonce"\r\n',
    ),
  );
  assert.equal(f.session.state, "closed");
  assert.deepEqual(f.errors, ["Imou stream rejected (RTSP 401)"]);
});
test("closing during local authentication discards late digests", async () => {
  const f = fixture(localCamera, true);
  await f.session.open();
  f.session.event({ type: "tcp-ready", id: f.session.id });
  f.event(
    reply(
      1,
      401,
      'WWW-Authenticate: Digest realm="Login", nonce="testnonce"\r\n',
    ),
  );
  const sent =
    f.messages.filter((message) => message.command === "tcp-send").length;
  f.session.close();
  f.hashes.forEach((hash) => hash());
  await settle();
  assert.equal(f.session.state, "closed");
  assert.equal(
    f.messages.filter((message) => message.command === "tcp-send").length,
    sent,
  );
  assert.deepEqual(f.errors, []);
});
test("Digest responses support MD5 and SHA-256 session algorithms with and without auth qop", async (t) => {
  const f = fixture();
  t.after(() => f.session.finishClose());
  await f.session.open();
  const uri = imou.localUrl(localCamera).href;
  for (const algorithm of ["MD5", "MD5-sess", "SHA-256", "SHA-256-sess"]) {
    for (const qop of ["", ', qop="auth-int, auth"']) {
      f.session.authentication = imou.challenge(
        'Digest realm="Login", nonce="testnonce", opaque="a\\"b", algorithm=' +
          algorithm + qop,
      );
      const value = imou.challenge(
        await f.session.authorization("DESCRIBE", uri),
      );
      const hash = (data) =>
        nodeCrypto.createHash(algorithm.startsWith("MD5") ? "md5" : "sha256")
          .update(data).digest("hex");
      let first = hash("admin:Login:" + localCamera.local.password);
      if (algorithm.endsWith("-sess")) {
        first = hash(first + ":testnonce:" + value.cnonce);
      }
      const second = hash("DESCRIBE:" + uri);
      assert.equal(
        value.response,
        hash(
          first + ":testnonce:" +
            (qop ? value.nc + ":" + value.cnonce + ":auth:" : "") + second,
        ),
      );
      assert.equal(value.opaque, 'a"b');
    }
  }
  for (
    const value of [
      'Basic realm="Login"',
      'Digest realm="Login", nonce="n", algorithm=SHA-1',
      'Digest realm="Login", nonce="n", qop="auth-int"',
      'Digest realm="Login", nonce="n", nonce="other"',
      'Digest realm="Login", nonce="n"\r\nInjected: value',
    ]
  ) assert.throws(() => imou.challenge(value), /authentication/);
});
test("local sessions open the secondary stream for the grid and the main stream for full screen", async (t) => {
  for (const [hd, subtype] of [[undefined, 1], [false, 1], [true, 0]]) {
    const f = fixture({ ...localCamera, hd });
    t.after(() => f.session.finishClose());
    await f.session.open();
    assert.equal(
      f.session.url.href,
      "rtsp://192.168.1.210:554/cam/realmonitor?channel=1&subtype=" + subtype,
    );
  }
});
test("a local Imou camera with its account moves through the account API", async (t) => {
  const original = global.XMLHttpRequest;
  const calls = [];
  global.XMLHttpRequest = class {
    open(method, url) {
      this.url = url;
    }
    setRequestHeader() {}
    send(body) {
      calls.push({ url: this.url, body: JSON.parse(body) });
      const data = this.url.endsWith("accessToken")
        ? { accessToken: "moveToken", expireTime: 3600 }
        : {};
      this.status = 200;
      this.responseText = JSON.stringify({
        result: this.url.endsWith("accessToken")
          ? { code: "0", data }
          : { code: "0" },
      });
      queueMicrotask(() => this.onload());
    }
  };
  t.after(() => {
    global.XMLHttpRequest = original;
  });
  const native = {
    postMessage: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  const statuses = [];
  const callbacks = {
    status: (message) => statuses.push(message),
    video: () => {},
    error: () => {},
  };
  const movable = {
    ...localCamera,
    account: { ...account, app_id: "moveApp" },
    device_id: "testDevice",
    channel_id: "0",
  };
  const session = new imou.Session(movable, native, callbacks);
  assert.equal(session.move("left"), false);
  session.state = "playing";
  assert.equal(session.move("diagonal"), false);
  assert.equal(session.move("left"), true);
  assert.equal(session.move("left"), true);
  await session.moveRequest;
  assert.deepEqual(statuses, []);
  const moves = calls.filter((call) => call.url.endsWith("/controlMovePTZ"));
  assert.equal(moves.length, 1);
  assert.equal(
    moves[0].url,
    "https://openapi-fk.easy4ip.com/openapi/controlMovePTZ",
  );
  assert.deepEqual(moves[0].body.params, {
    deviceId: "testDevice",
    channelId: "0",
    operation: "2",
    duration: 500,
    token: "moveToken",
  });
  const local = new imou.Session(localCamera, native, callbacks);
  local.state = "playing";
  assert.equal(local.move("left"), false);
  assert.throws(
    () =>
      imou.validate({
        ...localCamera,
        account: { ...account, region: "Mars" },
        device_id: "testDevice",
        channel_id: "0",
      }),
    /Invalid Imou camera configuration/,
  );
});
