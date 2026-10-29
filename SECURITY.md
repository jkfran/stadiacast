# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's **Security → Report a vulnerability**
form on this repository rather than in a public issue. You should get an acknowledgement within
a week. Fixes are released on `main`; there are no maintained release branches yet.

## What the receiver exposes

The receiver is designed for a trusted network, typically a hotspot hosted by the machine it
runs on. Keep the following in mind before putting it anywhere else:

- **No authentication.** The HTTP endpoints `/_status`, `/_rumble` and `/_start_game` on ports
  80 and 8008 accept requests from anyone who can reach them. `/_status` includes the session
  state and the log directory; `/_rumble` moves the controller's motors.
- **It impersonates Google hostnames** for the clients that resolve names through its DNS sink.
  Only point devices at the sink that you want to talk to the receiver; other names are
  forwarded upstream unchanged.
- **Request bodies are written to disk** in the run directory (`captures/<stamp>/bodies/`) for
  protocol work. They can contain device identifiers. Captures are ignored by git; treat them
  like logs.
- **The certificate private key** in `certs/privkey.pem` is a real key for a domain you own.
  Keep it `chmod 600`, never commit it (the directory is ignored by git and the pre-commit
  configuration includes a private-key detector), and revoke it if it leaks.
- The receiver makes **no outbound connections** except the optional `stadiaconfig`
  passthrough to `clients3.google.com` (`--stadiaconfig passthrough`, which only works when the
  `passthrough` extra is installed).

Input hardening: HTTP request heads are limited to 64 KiB and bodies to 4 MiB; malformed
requests close the connection; application callbacks are isolated from the datagram path so a
decoding error cannot take the receiver down.
