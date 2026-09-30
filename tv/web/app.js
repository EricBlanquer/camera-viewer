"use strict";
const transport = document.getElementById("transport");
const debugPanel = document.getElementById("debug-panel");
const logElement = document.getElementById("log");
const STORAGE_KEY = "camera-viewer-config-v1";
const GRID_SIZE = 4;
const events = [];
let nativeReady = false;
let cameras = [];
let selected = 0;
function log(message) {
  events.push({ time: Date.now(), message });
  if (events.length > 100) events.shift();
  logElement.textContent = events.slice(-8).map((event) => event.message).join(
    "\n",
  );
  console.log(message);
}
function refreshOverlay() {
  debugPanel.hidden = panes.some((pane) => pane.hasImage);
}
function validateConfiguration(configuration) {
  if (
    !Array.isArray(configuration) || !configuration.length ||
    configuration.length > GRID_SIZE
  ) throw new Error("Configure between one and four cameras");
  configuration.forEach((camera) => {
    if (
      !camera || !["icam365", "okam", "imou"].includes(camera.type) ||
      typeof camera.name !== "string" || !camera.name ||
      camera.name.length > 128
    ) throw new Error("Invalid camera configuration");
    createSession(camera, {});
  });
}
function createSession(camera, callbacks) {
  const Session = camera.type === "imou"
    ? ImouVideo.Session
    : CameraProtocol.NativeSession;
  return new Session(camera, transport, callbacks);
}
function configure(configuration) {
  validateConfiguration(configuration);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(configuration));
  cameras = configuration;
  connect();
  return cameras.length;
}
async function refreshCredential(camera) {
  const cloud = camera.type === "icam365" && camera.cloud_session;
  if (!cloud) return camera;
  if (cloud.origin !== "https://api-we01.tange365.com") {
    throw new Error("Invalid account service");
  }
  const body = Object.assign({}, cloud.query, {
    token: cloud.token,
    appid: cloud.appid,
    uuid: cloud.uuid,
    page: "1",
    limit: "1",
  });
  const response = await new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", cloud.origin + "/app/device/list/detail", true);
    request.timeout = 15000;
    request.setRequestHeader("Authorization", cloud.token);
    request.setRequestHeader("Content-Type", "application/json");
    request.setRequestHeader("X-Tg-App-Id", cloud.appid);
    request.setRequestHeader("X-Tg-App-Pkgname", "com.tange365.icam365");
    request.setRequestHeader("X-Tg-App-Platform", "android");
    request.onload = () => {
      try {
        if (request.status !== 200 || request.responseText.length > 1048576) {
          throw new Error();
        }
        resolve(JSON.parse(request.responseText));
      } catch (error) {
        reject(new Error("Account credential refresh failed"));
      }
    };
    request.onerror = request.ontimeout = () =>
      reject(new Error("Account credential refresh failed"));
    request.send(JSON.stringify(body));
  });
  const items = response.data && response.data.items;
  if (
    response.code !== 200 || !Array.isArray(items) || items.length !== 1 ||
    items[0].uuid !== cloud.uuid || items[0].p2p_id !== camera.p2p_id
  ) throw new Error("Account returned an unexpected camera");
  return Object.assign({}, camera, { password: items[0].password });
}
class CameraPane {
  constructor(index) {
    this.index = index;
    this.generation = 0;
    this.session = null;
    this.playing = false;
    this.hasImage = false;
    this.reconnectTimer = null;
    this.lastProgress = null;
    this.element = document.createElement("section");
    this.element.className = "camera";
    this.element.innerHTML = '<canvas></canvas><p class="status"></p>';
    document.getElementById("grid").appendChild(this.element);
    this.canvas = this.element.querySelector("canvas");
    this.context = this.canvas.getContext("2d");
    this.statusElement = this.element.querySelector(".status");
    this.metrics = { frames: 0, bytes: 0, playtime: 0, state: "empty" };
  }
  status(message) {
    this.statusElement.textContent = message;
    log(
      (this.camera ? this.camera.name : "Camera " + (this.index + 1)) + ": " +
        message,
    );
  }
  rectangle() {
    return {
      x: this.index % 2 * 960,
      y: Math.floor(this.index / 2) * 540,
      width: 960,
      height: 540,
    };
  }
  sameCamera(camera) {
    if (!camera || !this.camera || camera.type !== this.camera.type) {
      return false;
    }
    if (camera.type === "imou") {
      return camera.device_id === this.camera.device_id &&
        camera.channel_id === this.camera.channel_id &&
        camera.account.app_id === this.camera.account.app_id;
    }
    return camera.type === "okam"
      ? camera.uid === this.camera.uid
      : camera.p2p_id === this.camera.p2p_id;
  }
  async connect(camera) {
    this.stop({ preserveImage: this.sameCamera(camera) });
    this.camera = camera;
    const current = this.generation;
    if (!camera || !nativeReady || document.hidden) {
      this.metrics = {
        frames: 0,
        bytes: 0,
        playtime: 0,
        state: camera ? "waiting" : "empty",
      };
      this.statusElement.textContent = camera ? "Waiting for TV transport" : "";
      return;
    }
    this.status(this.hasImage ? "Reconnecting" : "Connecting");
    this.metrics = { frames: 0, bytes: 0, playtime: 0, state: "connecting" };
    let config = camera;
    try {
      config = await refreshCredential(camera);
    } catch (error) {
      log(camera.name + ": using saved camera credential");
    }
    if (current !== this.generation) return;
    try {
      config = Object.assign({}, config, {
        local_host: webapis.network.getIp(),
      });
      this.session = createSession(config, {
        status: (message) => this.status(message),
        video: (payload, codec) => this.video(payload, codec),
        error: (message, relay) => this.failed(message, relay),
      });
      this.session.open();
    } catch (error) {
      this.failed(error.message);
    }
  }
  video(payload, codec) {
    if (this.lastProgress === null) this.lastProgress = performance.now();
    this.metrics.frames++;
    this.metrics.bytes += payload.length;
    transport.postMessage({
      command: "decode-video",
      stream: this.index,
      generation: this.generation,
      codec,
      key: CameraProtocol.isKeyframe(payload, codec),
      burst: this.camera.type === "imou",
      data: payload.buffer.slice(
        payload.byteOffset,
        payload.byteOffset + payload.byteLength,
      ),
    });
  }
  render(frame) {
    if (frame.generation !== this.generation || !this.session) return;
    if (
      this.canvas.width !== frame.width || this.canvas.height !== frame.height
    ) {
      this.canvas.width = frame.width;
      this.canvas.height = frame.height;
    }
    this.context.putImageData(
      new ImageData(
        new Uint8ClampedArray(frame.data),
        frame.width,
        frame.height,
      ),
      0,
      0,
    );
    this.hasImage = true;
    this.element.classList.remove("reconnecting");
    const now = performance.now();
    if (!this.playing) {
      this.playing = true;
      this.firstFrame = now;
      this.metrics.decoded = 0;
      this.element.classList.add("playing");
      this.metrics.state = "playing";
      this.status("Live");
      refreshOverlay();
    }
    this.lastProgress = now;
    this.metrics.lastFrame = Date.now();
    this.metrics.playtime = Math.round(now - this.firstFrame);
    this.metrics.decoded++;
  }
  failed(message, retryRelay = false) {
    if (retryRelay && this.camera) this.camera.prefer_relay = true;
    this.stop({ preserveImage: true });
    this.metrics.state = "error";
    this.status(message);
    this.reconnectTimer = setTimeout(() => this.connect(this.camera), 10000);
  }
  stop({ preserveImage = false } = {}) {
    this.generation++;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.session) {
      this.session.close();
      this.session = null;
    }
    if (!preserveImage) {
      this.context.clearRect(0, 0, this.canvas.width, this.canvas.height);
      this.hasImage = false;
    }
    this.playing = false;
    this.lastProgress = null;
    this.element.classList.remove("playing");
    this.element.classList.toggle("reconnecting", this.hasImage);
    refreshOverlay();
  }
  check() {
    if (
      this.lastProgress !== null &&
      performance.now() - this.lastProgress > 12000
    ) this.failed("Live playback stalled, reconnecting");
  }
}
const panes = Array.from(
  { length: GRID_SIZE },
  (_, index) => new CameraPane(index),
);
function connect() {
  panes.forEach((pane, index) => pane.connect(cameras[index]));
}
function stop() {
  panes.forEach((pane) => pane.stop());
}
function state() {
  return panes.map((pane) =>
    Object.assign({
      name: pane.camera ? pane.camera.name : null,
      rectangle: pane.rectangle(),
      connected: Boolean(pane.session && pane.session.peer),
    }, pane.metrics)
  );
}
window.cameraViewer = { events, configure, connect, stop, state };
document.getElementById("native-listener").addEventListener(
  "message",
  (event) => {
    const message = event.data;
    if (message.type === "transport-ready") {
      nativeReady = true;
      connect();
    } else if (message.type === "error") log(message.detail);
    if (message.type === "video-frame" && panes[message.stream]) {
      panes[
        message.stream
      ].render(message);
    }
    if (
      message.type === "decoder-error" && panes[message.stream] &&
      message.generation === panes[message.stream].generation
    ) panes[message.stream].failed(message.detail);
    panes.forEach((pane) => {
      if (pane.session) pane.session.event(message);
    });
  },
  true,
);
transport.addEventListener("load", () => log("Native transport loaded"));
transport.addEventListener("error", () => log("Native transport failed"));
transport.addEventListener("crash", () => {
  stop();
  log("Native transport crashed");
});
function select(index) {
  selected = index;
  panes.forEach((pane, position) =>
    pane.element.classList.toggle("selected", position === selected)
  );
}
document.addEventListener("keydown", (event) => {
  if (event.keyCode === 10009) {
    stop();
    tizen.application.getCurrentApplication().exit();
  } else if (event.keyCode === 13) panes[selected].connect(cameras[selected]);
  else if ([37, 39].includes(event.keyCode)) select(selected ^ 1);
  else if ([38, 40].includes(event.keyCode)) select(selected ^ 2);
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stop();
  else connect();
});
setInterval(() => panes.forEach((pane) => pane.check()), 1000);
try {
  cameras = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  if (cameras.length) validateConfiguration(cameras);
} catch (error) {
  cameras = [];
  log("Camera setup required");
}
select(0);
