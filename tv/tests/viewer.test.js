"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const protocol = require("../web/protocol.js");
const imou = require("../web/imou.js");
function viewer(options = {}) {
  const listeners = {};
  const intervals = [];
  const sessions = [];
  const messages = [];
  const screenSaverStates = [];
  const accountRequests = [];
  const storage = new Map();
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
      this.config = config;
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
  class NativeSession extends Session {
    constructor(config, native, callbacks) {
      super(config, native, callbacks);
      this.qualities = [];
    }
    setQuality(hd) {
      this.qualities.push(hd);
    }
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
      this.config = config;
    }
  }
  class XMLHttpRequest {
    constructor() {
      this.headers = {};
    }
    open(method, url) {
      this.url = url;
    }
    setRequestHeader(name, value) {
      this.headers[name] = value;
    }
    send(body) {
      const request = {
        url: this.url,
        headers: this.headers,
        body: JSON.parse(body),
      };
      accountRequests.push(request);
      this.status = 200;
      this.responseText = JSON.stringify(
        options.account ? options.account(request) : { code: 500 },
      );
      Promise.resolve().then(() => this.onload());
    }
  }
  const context = vm.createContext({
    document,
    window: {},
    console: { log: () => {} },
    performance: { now: () => now },
    localStorage: {
      getItem: (key) => storage.has(key) ? storage.get(key) : null,
      setItem: (key, value) => storage.set(key, value),
    },
    XMLHttpRequest,
    CameraProtocol: Object.assign({}, protocol, { NativeSession }),
    ImouVideo: Object.assign({}, imou, { Session: ImouSession }),
    webapis: {
      network: { getIp: () => "192.0.2.10" },
      appcommon: "appcommon" in options ? options.appcommon : {
        AppCommonScreenSaverState: { SCREEN_SAVER_OFF: 0, SCREEN_SAVER_ON: 1 },
        setScreenSaver: (state, onsuccess) => {
          screenSaverStates.push(state);
          onsuccess(state);
        },
      },
    },
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
    accountRequests,
    stored: () => JSON.parse(storage.get("camera-viewer-config-v1")),
    screenSaver: () => screenSaverStates,
    setHidden: (hidden) => {
      document.hidden = hidden;
      listeners.visibilitychange();
    },
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
  assert.equal(session.config.hd, false);
  app.press(39);
  app.press(13);
  assert.deepEqual(session.qualities, [true]);
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
  assert.deepEqual(session.qualities, [true, false]);
  assert.deepEqual(decodeSize(), [1, 960, 540]);
  app.press(10009);
  assert.equal(app.exits(), 1);
});
test("the TV screen saver stays off while the viewer is displayed", () => {
  const app = viewer();
  assert.deepEqual(app.screenSaver(), [0]);
  app.setHidden(true);
  assert.deepEqual(app.screenSaver(), [0, 1]);
  app.setHidden(false);
  assert.deepEqual(app.screenSaver(), [0, 1, 0]);
  app.press(10009);
  assert.deepEqual(app.screenSaver(), [0, 1, 0, 1]);
  assert.equal(app.exits(), 1);
});
test("an unavailable screen saver setting is logged without blocking the viewer", () => {
  const app = viewer({ appcommon: undefined });
  const failures = () =>
    app.context.window.cameraViewer.events.filter((event) =>
      event.message === "Screen saver setting failed"
    ).length;
  assert.equal(failures(), 1);
  app.press(10009);
  assert.equal(failures(), 2);
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
test("an Imou camera reconnects on its main stream in full screen and on its secondary stream in the grid", async () => {
  const app = viewer();
  const kitchen = {
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
  app.context.window.cameraViewer.configure([kitchen]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  const pane = app.pane(0);
  const grid = pane.session;
  assert.equal(grid.config.hd, false);
  app.render(0);
  const image = pane.canvas.image;
  app.press(13);
  await Promise.resolve();
  await Promise.resolve();
  const full = pane.session;
  assert.notEqual(full, grid);
  assert.equal(grid.peer, null);
  assert.equal(full.config.hd, true);
  assert.equal(pane.canvas.image, image);
  app.press(10009);
  await Promise.resolve();
  await Promise.resolve();
  assert.notEqual(pane.session, full);
  assert.equal(full.peer, null);
  assert.equal(pane.session.config.hd, false);
});
test("a camera marked always_hd stays on its main stream in the grid", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([
    { ...app.camera, always_hd: true },
    { ...app.camera, name: "Garden", p2p_id: "TEST-2-ABCDE" },
  ]);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  assert.equal(app.pane(0).session.config.hd, true);
  assert.equal(app.pane(1).session.config.hd, false);
  app.press(13);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(app.pane(0).session.config.hd, true);
  app.press(10009);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(app.pane(0).session.config.hd, true);
  assert.ok(!app.pane(0).session.qualities.includes(false));
});
test("a camera connected while it is in full screen starts on its main stream", async () => {
  const app = viewer();
  app.context.window.cameraViewer.configure([app.camera]);
  app.press(13);
  app.emit({ type: "transport-ready" });
  await Promise.resolve();
  assert.equal(app.pane(0).session.config.hd, true);
  assert.deepEqual(app.pane(0).session.qualities, []);
});
function accountCamera(app) {
  return {
    ...app.camera,
    cloud_session: {
      origin: "https://api-we01.tange365.com",
      token: "old-token",
      appid: "123",
      uuid: "device-uuid",
      query: { platform: "android" },
      username: "600000000",
      area_code: "33",
    },
    account_password: "account-secret",
  };
}
function deviceDetail(password) {
  return {
    code: 200,
    data: {
      items: [{ uuid: "device-uuid", p2p_id: "TEST-1-ABCDE", password }],
    },
  };
}
function accountPath(request) {
  return new URL(request.url).pathname;
}
async function rejectAndReconnect(app) {
  const pane = app.pane(0);
  pane.session.callbacks.error(protocol.AUTHENTICATION_REJECTED);
  await pane.connect(pane.camera);
  return pane;
}
async function startAccountViewer(account) {
  const app = viewer({ account });
  app.context.window.cameraViewer.configure([accountCamera(app)]);
  app.emit({ type: "transport-ready" });
  await new Promise((resolve) => setImmediate(resolve));
  return app;
}
test("an iCam365 camera connects with its saved credential without contacting its account", async () => {
  const app = await startAccountViewer(() => deviceDetail("fresh"));
  assert.equal(app.pane(0).session.config.password, "test");
  assert.equal(app.accountRequests.length, 0);
});
test("a credential rejected by the camera is refreshed from the account and saved", async () => {
  const app = await startAccountViewer((request) =>
    request.headers.Authorization === "old-token"
      ? deviceDetail("fresh")
      : { code: 51023 }
  );
  const pane = await rejectAndReconnect(app);
  assert.deepEqual(app.accountRequests.map(accountPath), [
    "/app/device/list/detail",
  ]);
  assert.equal(app.accountRequests[0].body.platform, "android");
  assert.equal(app.accountRequests[0].body.uuid, "device-uuid");
  assert.equal(pane.session.config.password, "fresh");
  assert.equal(app.stored()[0].password, "fresh");
  assert.equal(app.stored()[0].cloud_session.token, "old-token");
});
test("an expired account session signs in again before refreshing the camera credential", async () => {
  const app = await startAccountViewer((request) => {
    if (accountPath(request) === "/app/user/login") {
      return { code: 200, data: { token: "new-token" } };
    }
    return request.headers.Authorization === "new-token"
      ? deviceDetail("fresh")
      : { code: 51023 };
  });
  const pane = await rejectAndReconnect(app);
  assert.deepEqual(app.accountRequests.map(accountPath), [
    "/app/device/list/detail",
    "/app/user/login",
    "/app/device/list/detail",
  ]);
  const login = app.accountRequests[1];
  assert.equal(login.headers.Authorization, undefined);
  assert.equal(login.body.username, "600000000");
  assert.equal(login.body.pwd, "account-secret");
  assert.equal(login.body.area_code, "33");
  assert.equal(login.body.appid, "123");
  assert.equal(login.body.platform, "android");
  assert.equal(pane.session.config.password, "fresh");
  assert.equal(app.stored()[0].password, "fresh");
  assert.equal(app.stored()[0].cloud_session.token, "new-token");
  assert.ok(
    app.context.window.cameraViewer.events.some((event) =>
      event.message === "Entrance: account session renewed"
    ),
  );
});
test("a refused account password is not submitted again", async () => {
  const app = await startAccountViewer((request) =>
    accountPath(request) === "/app/user/login"
      ? { code: 51021 }
      : { code: 51023 }
  );
  await rejectAndReconnect(app);
  app.setTime(300001);
  const pane = await rejectAndReconnect(app);
  assert.deepEqual(app.accountRequests.map(accountPath), [
    "/app/device/list/detail",
    "/app/user/login",
    "/app/device/list/detail",
  ]);
  assert.equal(pane.session.config.password, "test");
  assert.ok(
    app.context.window.cameraViewer.events.some((event) =>
      event.message === "Entrance: Account password rejected"
    ),
  );
});
test("a failed account sign-in waits five minutes before the next attempt", async () => {
  const app = await startAccountViewer(() => ({ code: 500 }));
  await rejectAndReconnect(app);
  await rejectAndReconnect(app);
  assert.deepEqual(app.accountRequests.map(accountPath), [
    "/app/device/list/detail",
    "/app/user/login",
    "/app/device/list/detail",
  ]);
  app.setTime(300001);
  await rejectAndReconnect(app);
  assert.deepEqual(app.accountRequests.map(accountPath).slice(3), [
    "/app/device/list/detail",
    "/app/user/login",
  ]);
});
test("an account password requires the account username and area code", () => {
  const app = viewer();
  const camera = accountCamera(app);
  delete camera.cloud_session.area_code;
  assert.throws(
    () => app.context.window.cameraViewer.configure([camera]),
    /Invalid camera configuration/,
  );
});
test("viewer visibility changes are logged", () => {
  const app = viewer();
  app.setHidden(true);
  app.setHidden(false);
  assert.deepEqual(
    Array.from(
      app.context.window.cameraViewer.events,
      (event) => event.message,
    ).filter((message) => message.startsWith("Viewer ")),
    ["Viewer hidden", "Viewer displayed"],
  );
});
