"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const p = require("../web/protocol.js");
const hex = (bytes) => Buffer.from(bytes).toString("hex");
function session(type = "icam365") {
  const sent = [];
  const listeners = new Set();
  const native = {
    postMessage: (message) => sent.push(message),
    addEventListener: (_, listener) => listeners.add(listener),
    removeEventListener: (_, listener) => listeners.delete(listener),
  };
  const config = type === "okam"
    ? {
      type,
      uid: "TEST000001ABCDE",
      service_parameter: "EBGIEABAKIIMGMIMFJ:ExampleTransport",
      password: "test",
    }
    : {
      type,
      p2p_id: "TEST-1-ABCDE",
      p2p_platform: "ppcs:EBGIEABAKIIMGMIMFJ",
      password: "test",
    };
  const video = [];
  const connection = new p.NativeSession(config, native, {
    status: () => {},
    video: (...frame) => video.push(frame),
    error: () => {},
  });
  connection.state = "connecting";
  return { connection, sent, video, listeners };
}
test("keyframe detection accepts both Annex B prefixes for H.264 and HEVC", () => {
  assert.equal(p.isKeyframe(Uint8Array.of(0, 0, 1, 0x65), 27), true);
  assert.equal(p.isKeyframe(Uint8Array.of(0, 0, 0, 1, 0x26, 1), 36), true);
  assert.equal(p.isKeyframe(Uint8Array.of(0, 0, 1, 0x41, 1), 27), false);
  assert.equal(p.isKeyframe(Uint8Array.of(0, 0, 0, 1), 36), false);
  assert.equal(p.isKeyframe(Uint8Array.of(0, 0, 1, 0x65), 138), false);
});
test("directory status semantics remain specific to the camera protocol", () => {
  const directory = { host: "192.0.2.1", port: 32100 };
  const response = p.packet(0x21, Uint8Array.of(1));
  assert.throws(
    () => session().connection.receive(response, directory),
    /offline/,
  );
  assert.doesNotThrow(() =>
    session("okam").connection.receive(response, directory)
  );
});
test("handshake checks the camera UID and only accepts peer media", () => {
  const { connection, sent, video } = session();
  const peer = { host: "192.0.2.2", port: 40000 };
  connection.receive(p.packet(0x41, p.uidBytes("TEST-2-ABCDE")), peer);
  assert.equal(connection.peer, null);
  connection.receive(p.packet(0x41, connection.uid), peer);
  assert.deepEqual(connection.peer, peer);
  const header = new Uint8Array(16);
  header[0] = 80;
  p.view(header).setUint32(8, 5, true);
  const data = p.packet(
    0xd0,
    p.concat(
      Uint8Array.of(0xd1, 2, 0, 0),
      header,
      Uint8Array.of(0, 0, 1, 0x26, 1),
    ),
  );
  connection.receive(data, { host: "192.0.2.3", port: peer.port });
  assert.equal(video.length, 0);
  connection.receive(data, peer);
  connection.receive(data, peer);
  assert.equal(video.length, 1);
  assert.equal(video[0][1], 36);
  assert.equal(
    sent.filter((item) => new Uint8Array(item.data)[1] === 0xd1).length,
    2,
  );
  connection.close();
});
test("closing acknowledges media without waiting for gaps and drains the stop command", () => {
  const { connection, listeners } = session();
  const peer = { host: "192.0.2.2", port: 40000 };
  connection.peer = peer;
  connection.authenticated = true;
  connection.state = "streaming";
  connection.close();
  assert.equal(connection.state, "closing");
  assert.equal(listeners.size, 1);
  connection.receive(p.packet(0xd0, Uint8Array.of(0xd1, 2, 0, 10, 1, 2)), peer);
  assert.equal(connection.channels[2].pending.size, 0);
  const index = Array.from(connection.pending.keys())[0];
  connection.receive(
    p.packet(0xd1, Uint8Array.of(0xd1, 0, 0, 1, index >> 8, index & 255)),
    peer,
  );
  assert.equal(connection.state, "closed");
  assert.equal(listeners.size, 0);
});
test("transport and readiness encryption match the Linux transport vectors", () => {
  const clear = Buffer.from("f1d00008d100000001020304", "hex");
  assert.equal(
    hex(p.cipher(clear, "ExampleTransport")),
    "dc747b57ebd4c7df6688c7db",
  );
  assert.equal(
    hex(
      p.cipher(
        Buffer.from("dc747b57ebd4c7df6688c7db", "hex"),
        "ExampleTransport",
        true,
      ),
    ),
    hex(clear),
  );
  assert.equal(
    hex(p.cipher(
      Uint8Array.from({ length: 20 }, (_, i) => i),
      "SSD@cs2-network.",
      false,
      true,
    )),
    "4074e65f915739c9d94a15a4c0a5339f4219f314",
  );
});
test("UID and endpoint framing preserve protocol byte order", () => {
  assert.equal(
    hex(p.uidBytes("TEST-1-ABCDE")),
    "5445535400000000000000014142434445000000",
  );
  assert.equal(
    hex(p.networkAddress("192.0.2.11", 32100)),
    "0002647d0b0200c00000000000000000",
  );
  assert.deepEqual(p.parseAddress(p.networkAddress("192.0.2.11", 32100)), {
    host: "192.0.2.11",
    port: 32100,
  });
  assert.throws(() => p.uidBytes("TEST-4294967296-ABCDE"));
  assert.throws(() => p.networkAddress("192.0.2.256", 1));
});
test("packet ordering handles duplicates and sequence wrap", () => {
  const channel = new p.OrderedChannel(65535);
  assert.deepEqual(channel.feed(0, Uint8Array.of(2)), []);
  assert.deepEqual(channel.feed(65535, Uint8Array.of(1)).map(hex), [
    "01",
    "02",
  ]);
  assert.equal(channel.expected, 1);
  assert.deepEqual(channel.feed(65535, Uint8Array.of(3)), []);
});
test("each recovered packet gap gets its own deadline", () => {
  const channel = new p.OrderedChannel();
  channel.feed(2, Uint8Array.of(2), 0);
  channel.feed(4, Uint8Array.of(4), 1000);
  channel.feed(0, Uint8Array.of(0), 7000);
  assert.doesNotThrow(() => channel.check(9000));
  assert.throws(() => channel.check(15001), /lost/);
});
test("fragmented camera frames are emitted only when complete", () => {
  const header = new Uint8Array(16);
  header[0] = 80;
  p.view(header).setUint32(8, 4, true);
  const frames = new p.MediaFrames("icam");
  assert.deepEqual(frames.feed(header.subarray(0, 7)), []);
  assert.deepEqual(
    frames.feed(p.concat(header.subarray(7), Uint8Array.of(1, 2))),
    [],
  );
  assert.equal(hex(frames.feed(Uint8Array.of(3, 4))[0].payload), "01020304");
  header[0] = 1;
  assert.throws(() => new p.MediaFrames("icam").feed(header), /Invalid/);
});
test("O-KAM media frame types are retained for audio filtering", () => {
  const header = new Uint8Array(32);
  p.view(header).setUint32(0, 0x55aa15a8);
  header[4] = 12;
  p.view(header).setUint32(16, 1, true);
  const frame =
    new p.MediaFrames("okam").feed(p.concat(header, Uint8Array.of(42)))[0];
  assert.equal(frame.flags, 12);
  assert.equal(hex(frame.payload), "2a");
});
test("queue bounds reject oversized or distant packets", () => {
  assert.throws(
    () => new p.OrderedChannel().feed(4097, Uint8Array.of(1)),
    /buffer/,
  );
  const buffer = new p.ByteQueue();
  buffer.append(new Uint8Array(8 * 1024 * 1024));
  assert.throws(() => buffer.append(Uint8Array.of(1)), /buffer/);
});
test("iCam365 authentication starts video and selects the desktop HD stream", () => {
  const { connection } = session();
  const commands = [];
  connection.command = (command, body) => commands.push([command, body]);
  connection.commandBuffer.append(
    p.concat(p.word32(0x8003), p.word32(4), p.word32(0)),
  );
  connection.commands();
  assert.deepEqual(commands.map(([command]) => command), [
    0x8024,
    0x8012,
    0x1ff,
    0x320,
  ]);
  assert.deepEqual(Array.from(commands.at(-1)[1]), [0, 0, 0, 0, 1, 0, 0, 0]);
  connection.commands();
  assert.equal(commands.length, 4);
});
