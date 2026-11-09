# rrweb Live Browser Streaming

A small, consent-based live browser session demo. The recorder captures the page where `record.html` is running and streams rrweb events through an authenticated Socket.IO room to `viewer.html`.

## Requirements

- Node.js 20 or newer
- A modern browser with JavaScript modules
- A private network or HTTPS deployment for use outside localhost

## Install

```bash
cd examples/rrweb-live
npm install
cp .env.example .env
```

Set `ROOM_TOKEN` in `.env` to a long, random secret (at least 24 characters). Keep `.env` private and never commit it.

Start the server:

```bash
npm start
```

Open:

- Recorder: http://127.0.0.1:8787/record.html
- Viewer: http://127.0.0.1:8787/viewer.html

Enter the same room name and `ROOM_TOKEN` on both pages. Start the viewer first, then start recording.

## Use with a page you own

This demo records the recorder page itself. To record another application you own, integrate the rrweb recorder module into that application's frontend and send its emitted events to the authenticated Socket.IO room using the same `join-room` and `rrweb-event` messages. Do not inject it into sites or sessions without the owner's permission.

## Privacy and security

- Record only your own pages or sessions for which every relevant participant has given permission.
- The demo masks all input fields by default and supports `data-rrweb-mask`, `rr-block`, and `rr-ignore` selectors.
- Do not record passwords, payment details, private messages, authentication codes, or other sensitive information.
- The server checks a shared room token and room-name format, relays events only to the authenticated room, and does not persist recordings.
- Use HTTPS/WSS in production, set `ALLOWED_ORIGINS` to the exact allowed origin(s), and use a unique high-entropy token. The empty origin list is convenient for local development and should not be used as a production restriction.
- A shared token is a simple demo gate, not a full user identity or per-room authorization system. For production, use short-lived signed room credentials and proper user authentication.
- rrweb event streams can contain page content. Treat access tokens and recordings as sensitive.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8787` | HTTP port |
| `HOST` | `127.0.0.1` | Bind address; use `0.0.0.0` only behind a secured reverse proxy |
| `ROOM_TOKEN` | required | Shared secret for joining rooms |
| `ALLOWED_ORIGINS` | empty | Comma-separated browser origins; set explicitly in production |

## Notes

The browser loads rrweb 2.2.1 ESM assets from the official rrweb CDN. The Socket.IO client is served by the local server. This example is intentionally a relay rather than a recording database: viewers joining after recording starts will wait for the next rrweb full snapshot.
