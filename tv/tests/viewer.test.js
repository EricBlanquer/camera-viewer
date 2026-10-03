"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const protocol = require("../web/protocol.js");
const imou = require("../web/imou.js");
function viewer() {
  const listeners = {};
  const intervals = [];
  const sessions = [];
  const messages = [];
  let exits = 0;
  let now = 0;
  function element() {
    const classes = new Set();
    return {
      textContent: "",
      classList: {
        add: (value) => classes.add(value),
        remove: (value) => classes.delete(value),
        contains: (value) => classes.has(value),
        toggle: (value, enabled) =>
          enabled ? classes.add(value) : classes.delete(value),
      },
      addEventListener: (name, callback) => {
        listeners[name] = callback;
      },
      appendChild: () => {},
    };
  }
  const nodes = Object.fromEntries(
    ["transport", "debug-panel", "log", "grid", "native-listener"].map(
      (id) => [id, element()],
    ),
  );
  nodes.transport.postMessage = (message) => messages.push(message);
  const document = {
    hidden: false,
    getElementById: (id) => nodes[id],
    addEventListener: (name, callback) => {
      listeners[name] = callback;
    },
    createElement: () => {
      const section = element();
      const canvas = element();
      canvas.width = canvas.height = 2;
      canvas.image = null;
      canvas.getContext = () => ({
        clearRect: () => {
          canvas.image = null;
        },
        putImageData: (image) => {
          canvas.image = image;
        },
      });
      const children = { canvas, h2: element(), ".status": element() };
      section.querySelector = (selector) =>
        selector === "h2" && !section.innerHTML.includes("<h2")
          ? null
          : children[selector];
      return section;
    },
  };
  class Session {
    constructor(config, native, callbacks) {
      new protocol.NativeSession(config, native, callbacks);
      this.callbacks = callbacks;
    }
    open() {
      this.peer = true;
      sessions.push(this);
    }
    close() {
      this.peer = null;
    }
    event() {}
  }
  class ImouSession extends Session {
    constructor(config, native, callbacks) {
      super(
        {
          type: "icam365",
          p2p_id: "TEST-9-ABCDE",
          p2p_platform: "ppcs:EBGIEABAKIIMGMIMFJ",
          password: "test",
        },
        native,
        callbacks,
      );
      imou.validate(config);
    }
  }
  const context = vm.createContext({
    document,
    window: {},
    console: { log: () => {} },
    performance: { now: () => now },
    localStorage: { getItem: () => null, setItem: () => {} },
    CameraProtocol: Object.assign({}, protocol, { NativeSession: Session }),
    ImouVideo: Object.assign({}, imou, { Session: ImouSession }),
    webapis: { network: { getIp: () => "192.0.2.10" } },
    tizen: {
      application: {
        getCurrentApplication: () => ({
          exit: () => {
            exits++;
          },
        }),
      },
    },
    setInterval: (callback) => intervals.push(callback),
    clearInterval: () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    ImageData: class {
      constructor(data) {
        this.data = data;
      }
    },
    Uint8ClampedArray,
  });
  vm.runInContext(
    fs.readFileSync(require.resolve("../web/app.js"), "utf8"),
    context,
  );
  const camera = {
    type: "icam365",
    name: "Entrance",
    p2p_id: "TEST-1-ABCDE",
    p2p_platform: "ppcs:EBGIEABAKIIMGMIMFJ",
    password: "test",
  };
  function emit(data) {
    listeners.message({ data });
  }
  function render(
    index,
    generation = vm.runInContext("panes[" + index + "].generation", context),
  ) {
    emit({
      type: "video-frame",
      stream: index,
      generation,
      width: 2,
      height: 2,
      data: new ArrayBuffer(16),
    });
  }
  return {
    context,
    camera,
    emit,
    render,
    nodes,
    sessions,
    intervals,
    messages,
    exits: () => exits,
    press: (keyCode) => listeners.keydown({ keyCode }),
    pane: (index) => vm.runInContext("panes[" + index + "]", context),
    setTime: (value) => {
      now = value;
    },
  };
}
test("four fixed cells and the debug overlay depend on decoded video", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera, {
    ...app.camera,
    name: "Garden",
  }]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  const state = JSON.parse(
    JSON.stringify(app.context.window.cameraViewer.state()),
  );
  assert.deepEqual(state.map((pane) => pane.rectangle), [
    { x: 0, y: 0, width: 960, height: 540 },
    { x: 960, y: 0, width: 960, height: 540 },
    { x: 0, y: 540, width: 960, height: 540 },
    { x: 960, y: 540, width: 960, height: 540 },
  ]);
  assert.equal(app.nodes["debug-panel"].hidden, false);
  app.render(0, -1);
  assert.equal(app.nodes["debug-panel"].hidden, false);
  app.render(0);
  assert.equal(app.nodes["debug-panel"].hidden, true);
  assert.equal(app.context.window.cameraViewer.state()[0].decoded, 1);
  app.context.window.cameraViewer.stop();
  app.render(0);
  assert.equal(app.nodes["debug-panel"].hidden, false);
});
test("reconnection retains the last image until a new decoded frame arrives", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  app.render(0);
  const pane = app.pane(0);
  const image = pane.canvas.image;
  const previousGeneration = pane.generation;
  app.setTime(13001);
  app.intervals[0]();
  assert.equal(pane.canvas.image, image);
  assert.equal(pane.playing, false);
  assert.equal(pane.element.classList.contains("reconnecting"), true);
  assert.equal(app.nodes["debug-panel"].hidden, true);
  app.render(0, previousGeneration);
  assert.equal(pane.canvas.image, image);
  await pane.connect({ ...app.camera });
  assert.equal(pane.canvas.image, image);
  app.render(0);
  assert.notEqual(pane.canvas.image, image);
  assert.equal(pane.element.classList.contains("reconnecting"), false);
  assert.equal(pane.playing, true);
});
test("changing or removing a camera clears the retained image", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  app.render(0);
  const pane = app.pane(0);
  await pane.connect({ ...app.camera, p2p_id: "TEST-2-ABCDE" });
  assert.equal(pane.canvas.image, null);
  app.render(0);
  await pane.connect(undefined);
  assert.equal(pane.canvas.image, null);
  assert.equal(pane.statusElement.textContent, "");
});
test("camera names and empty-cell labels are absent from the video grid", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  assert.equal(app.pane(0).element.querySelector("h2"), null);
  for (let index = 1; index < 4; index++) {
    assert.equal(app.pane(index).statusElement.textContent, "");
  }
});
test("an Imou pane plays independently and retains only its own camera image", async () => {
  const app = viewer();
  const kitchen = {
    type: "imou",
    name: "Kitchen",
    device_id: "testKitchen",
    channel_id: "0",
    account: { app_id: "testApp", app_secret: "testSecret", region: "Europe" },
  };
  app.context.window.cameraViewer.configure([app.camera, {
    ...app.camera,
    p2p_id: "TEST-2-ABCDE",
  }, kitchen]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  [0, 1, 2].forEach((index) => app.render(index));
  const pane = app.pane(2);
  const image = pane.canvas.image;
  const firstSession = app.pane(0).session;
  const secondSession = app.pane(1).session;
  await pane.connect({ ...kitchen });
  assert.equal(pane.canvas.image, image);
  assert.equal(app.pane(0).session, firstSession);
  assert.equal(app.pane(1).session, secondSession);
  await pane.connect({ ...kitchen, channel_id: "1" });
  assert.equal(pane.canvas.image, null);
  app.render(2);
  assert.equal(app.context.window.cameraViewer.state()[2].state, "playing");
  assert.equal(app.pane(3).statusElement.textContent, "");
});
test("stalled rendering closes only the affected connection", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera, {
    ...app.camera,
    name: "Garden",
  }]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  app.render(0);
  app.render(1);
  app.setTime(13001);
  app.render(1);
  app.intervals[0]();
  const state = app.context.window.cameraViewer.state();
  assert.equal(state[0].state, "error");
  assert.equal(state[0].connected, false);
  assert.equal(state[1].state, "playing");
  assert.equal(state[1].connected, true);
  assert.equal(app.nodes["debug-panel"].hidden, true);
});
test("local Imou reconnects retain images only for the same local camera and channel", async () => {
  const app = viewer();
  const camera = {
    type: "imou",
    name: "Kitchen",
    local: {
      host: "192.168.1.210",
      port: 554,
      channel: 1,
      username: "admin",
      password: "test",
    },
  };
  app.context.window.cameraViewer.configure([camera]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  app.render(0);
  const pane = app.pane(0);
  const image = pane.canvas.image;
  await pane.connect({
    ...camera,
    local: { ...camera.local, password: "updated" },
  });
  assert.equal(pane.canvas.image, image);
  for (
    const change of [{ host: "192.168.1.244" }, { port: 8554 }, { channel: 2 }]
  ) {
    app.render(0);
    await pane.connect({ ...camera, local: { ...camera.local, ...change } });
    assert.equal(pane.canvas.image, null);
  }
});
test("OK shows the selected camera in full screen and Back returns to the grid", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera, {
    ...app.camera,
    name: "Garden",
    p2p_id: "TEST-2-ABCDE",
  }]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  const garden = app.pane(1);
  const session = garden.session;
  const generation = garden.generation;
  const decodeSize = () => {
    session.callbacks.video(new Uint8Array([0, 0, 0, 1, 0x65, 0]), 27);
    const message = app.messages.at(-1);
    return [message.stream, message.max_width, message.max_height];
  };
  assert.deepEqual(decodeSize(), [1, 960, 540]);
  app.press(39);
  app.press(13);
  assert.equal(app.nodes.grid.classList.contains("full-screen"), true);
  assert.equal(garden.element.classList.contains("full-screen"), true);
  assert.equal(app.pane(0).element.classList.contains("full-screen"), false);
  assert.equal(garden.session, session);
  assert.equal(garden.generation, generation);
  assert.deepEqual(
    JSON.parse(JSON.stringify(app.context.window.cameraViewer.state()[1]))
      .rectangle,
    { x: 0, y: 0, width: 1920, height: 1080 },
  );
  assert.deepEqual(decodeSize(), [1, 1920, 1080]);
  app.press(37);
  assert.equal(garden.element.classList.contains("selected"), true);
  assert.equal(garden.element.classList.contains("full-screen"), true);
  app.press(10009);
  assert.equal(app.nodes.grid.classList.contains("full-screen"), false);
  assert.equal(garden.element.classList.contains("full-screen"), false);
  assert.equal(app.exits(), 0);
  assert.equal(garden.session, session);
  assert.deepEqual(decodeSize(), [1, 960, 540]);
  app.press(10009);
  assert.equal(app.exits(), 1);
});
test("OK on an unused cell keeps the grid", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  app.press(40);
  app.press(13);
  assert.equal(app.nodes.grid.classList.contains("full-screen"), false);
  assert.equal(app.pane(2).element.classList.contains("full-screen"), false);
});
test("a new camera configuration returns to the grid", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  app.press(13);
  assert.equal(app.pane(0).element.classList.contains("full-screen"), true);
  app.context.window.cameraViewer.configure([app.camera]);
  assert.equal(app.nodes.grid.classList.contains("full-screen"), false);
  assert.equal(app.pane(0).element.classList.contains("full-screen"), false);
});
