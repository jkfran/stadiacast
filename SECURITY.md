# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's **Security → Report a vulnerability**
form on this repository rather than in a public issue. You should get an acknowledgement within
a week. Fixes land on `main` and go out with the next release; the latest release is the one
that is supported.

## What the receiver exposes

The receiver is designed for a trusted network, typically a hotspot hosted by the machine it
runs on. It trusts every device on that network. Keep the following in mind before putting it
anywhere else:

- **Controllers are not authenticated.** Any device on the network that speaks the protocol is
  taken for a controller, and can take the place of one that is connected.
- **The control endpoints have no login.** `/_status`, `/_input`, `/_events`, `/_rumble`,
  `/_start_game` and `/_audio` answer on every listener (443, 80 and 8008) to anyone who can
  reach it. `/_status` shows each controller's serial number, device ids, address and session
  state, the credentials of its WebRTC sessions included, and the run directory. `/_input` and
  `/_events` show what is pressed on each controller as it happens. `/_rumble` and
  `/_start_game` act on a controller. `/_audio` starts and stops the stream to a controller's
  headset from a sound file of this computer, from what the computer plays or from one of its
  capture devices, a microphone included. `/_shutdown` stops the receiver and is taken from its
  own computer only. What a browser sends on behalf of a web page is refused (any request other
  than `GET` that carries an `Origin` header).
- **It impersonates Google hostnames** for the clients that resolve names through its DNS sink,
  and the sink answers whoever asks it. Only point the devices at it that you want to talk to
  the receiver; names it does not redirect are forwarded to your resolver unchanged.
- **The run directory is private data.** `captures/<stamp>/` holds every request body the
  controller sent and a log with its serial number, device ids, addresses and the certificates
  and tokens it presents, and `dns.jsonl` lists every name looked up through the sink. Captures
  are ignored by git; cut them down before you share one.
- **The built-in certificate proves nothing.** Its private key ships with the package, so TLS
  on this server satisfies the controller's check and protects no one. A certificate of your
  own under `certs/` is different: its key is a real key for a domain you own. Keep it
  `chmod 600`, never commit it (the directory is ignored by git and the pre-commit
  configuration includes a private-key detector), and revoke it if it leaks.
- **What leaves your network.** The receiver itself connects to nothing, except for the
  `stadiaconfig` passthrough to `clients3.google.com`, which runs only when that name is
  redirected to the receiver and the `passthrough` extra is installed. The DNS sink forwards
  the names it does not redirect to the resolver you give it, and through it the controller
  makes two requests to Google at boot, its connectivity check and its configuration.

Input hardening: an HTTP request has the idle timeout (30 s) for its head, as long again for
its body and for its response to be taken; heads are limited to 64 KiB, bodies to 4 MiB and
connections to 128 at a time, and a malformed request is answered with an error and its
connection closed. What arrives on the UDP ports is parsed defensively, and a decoding error in
one controller's traffic does not reach the others.
