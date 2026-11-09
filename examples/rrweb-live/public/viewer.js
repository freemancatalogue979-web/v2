import { Replayer } from "https://cdn.rrweb.com/replay/2.2.1/dist/replay.js";

const form = document.querySelector("#connect-form");
const status = document.querySelector("#status");
const root = document.querySelector("#replay");
let socket;
let replayer;

function setStatus(message) {
  status.textContent = message;
}

function resetReplay() {
  if (replayer) {
    replayer.destroy();
    replayer = undefined;
  }
  root.replaceChildren();
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  socket?.disconnect();
  resetReplay();

  const room = form.elements.room.value.trim();
  const token = form.elements.token.value;
  socket = io();

  socket.on("connect", () => {
    socket.emit("join-room", { room, token, role: "viewer" }, (result) => {
      if (!result?.ok) {
        setStatus(result?.error || "Could not join room.");
        socket.disconnect();
        return;
      }
      setStatus(`Connected to “${room}”. Waiting for the recorder’s first snapshot…`);
    });
  });

  socket.on("rrweb-event", (rrwebEvent) => {
    try {
      if (!replayer) {
        replayer = new Replayer([], { root, liveMode: true });
        replayer.startLive();
      }
      replayer.addEvent(rrwebEvent);
      setStatus("Live replay is running.");
    } catch (error) {
      console.error(error);
      setStatus("Could not apply a replay event. Refresh and reconnect.");
    }
  });

  socket.on("peer-status", ({ role, state }) => {
    if (role === "recorder") setStatus(state === "joined" ? "Recorder connected." : "Recorder disconnected.");
  });
  socket.on("connect_error", () => setStatus("Connection failed. Check the server and try again."));
  socket.on("disconnect", () => {
    resetReplay();
    setStatus("Disconnected from the live stream.");
  });
});

window.addEventListener("beforeunload", () => {
  socket?.disconnect();
  replayer?.destroy();
});
