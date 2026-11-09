import { record } from "https://cdn.rrweb.com/record/2.2.1/dist/record.js";

const form = document.querySelector("#connect-form");
const status = document.querySelector("#status");
const stopButton = document.querySelector("#stop");
let socket;
let stopRecording;

function setStatus(message) {
  status.textContent = message;
}

function stop() {
  if (stopRecording) {
    stopRecording();
    stopRecording = undefined;
  }
  if (socket?.connected) socket.disconnect();
  socket = undefined;
  stopButton.disabled = true;
  setStatus("Recording stopped.");
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  stop();

  const room = form.elements.room.value.trim();
  const token = form.elements.token.value;
  socket = io();

  socket.on("connect", () => {
    socket.emit("join-room", { room, token, role: "recorder" }, (result) => {
      if (!result?.ok) {
        setStatus(result?.error || "Could not join room.");
        socket.disconnect();
        return;
      }

      stopRecording = record({
        emit(rrwebEvent) {
          socket?.emit("rrweb-event", rrwebEvent);
        },
        maskAllInputs: true,
        maskTextSelector: "[data-rrweb-mask]",
        blockClass: "rr-block",
        ignoreClass: "rr-ignore"
      });

      stopButton.disabled = false;
      setStatus(`Recording and streaming to room “${room}”.`);
    });
  });

  socket.on("connect_error", () => setStatus("Connection failed. Check the server and try again."));
  socket.on("disconnect", () => {
    if (stopRecording) {
      stopRecording();
      stopRecording = undefined;
    }
    stopButton.disabled = true;
    setStatus("Disconnected.");
  });
});

stopButton.addEventListener("click", stop);
window.addEventListener("beforeunload", () => stopRecording?.());
