import "dotenv/config";
import express from "express";
import helmet from "helmet";
import http from "node:http";
import { Server } from "socket.io";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 8787);
const HOST = process.env.HOST || "127.0.0.1";
const ROOM_TOKEN = process.env.ROOM_TOKEN;
const allowedOrigins = (process.env.ALLOWED_ORIGINS || "")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);

if (!ROOM_TOKEN || ROOM_TOKEN.length < 24) {
  throw new Error("Set ROOM_TOKEN to a random secret of at least 24 characters.");
}

const app = express();
app.disable("x-powered-by");
app.use(helmet({
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'self'"],
      scriptSrc: ["'self'", "https://cdn.socket.io", "https://cdn.rrweb.com"],
      styleSrc: ["'self'", "'unsafe-inline'", "https://cdn.rrweb.com"],
      connectSrc: ["'self'", "ws:", "wss:", "https://cdn.rrweb.com"],
      imgSrc: ["'self'", "data:", "blob:"],
      fontSrc: ["'self'", "data:", "https://cdn.rrweb.com"]
    }
  }
}));
app.use(express.static(path.join(__dirname, "public"), {
  extensions: ["html"],
  setHeaders(res) {
    res.setHeader("Cache-Control", "no-store");
  }
}));

const server = http.createServer(app);
const io = new Server(server, {
  maxHttpBufferSize: 1e6,
  cors: {
    origin(origin, callback) {
      if (!origin || allowedOrigins.length === 0 || allowedOrigins.includes(origin)) {
        return callback(null, true);
      }
      return callback(new Error("Origin is not allowed."));
    }
  }
});

const roomPattern = /^[a-zA-Z0-9_-]{3,64}$/;

io.on("connection", (socket) => {
  socket.on("join-room", ({ room, token, role } = {}, callback = () => {}) => {
    if (token !== ROOM_TOKEN) {
      callback({ ok: false, error: "Invalid room access token." });
      socket.disconnect(true);
      return;
    }

    if (!roomPattern.test(String(room || ""))) {
      callback({ ok: false, error: "Room names must be 3–64 letters, numbers, underscores, or hyphens." });
      return;
    }

    if (!["recorder", "viewer"].includes(role)) {
      callback({ ok: false, error: "Choose recorder or viewer." });
      return;
    }

    socket.data.room = room;
    socket.data.role = role;
    socket.join(room);
    callback({ ok: true, room, role });
    socket.to(room).emit("peer-status", { role, state: "joined" });
  });

  socket.on("rrweb-event", (event) => {
    if (!socket.data.room || socket.data.role !== "recorder" || !event || typeof event !== "object") {
      return;
    }

    // Relay only to viewers in this authenticated room; do not persist session data.
    socket.to(socket.data.room).emit("rrweb-event", event);
  });

  socket.on("disconnect", () => {
    if (socket.data.room && socket.data.role) {
      socket.to(socket.data.room).emit("peer-status", {
        role: socket.data.role,
        state: "left"
      });
    }
  });
});

server.listen(PORT, HOST, () => {
  console.log(`rrweb live server listening at http://${HOST}:${PORT}`);
});
