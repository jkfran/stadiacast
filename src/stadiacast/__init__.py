"""Stadiacast: a local server for the Stadia Controller's Wi-Fi mode.

A LAN-side stand-in for the shut-down Stadia cloud endpoints, so a Stadia Controller in
Wi-Fi mode (stock firmware) keeps working with the owner's own PC.
"""

__version__ = "1.1.0"

#: Name the receiver reports to the controller (HTTP ``Server`` header, SDP build number).  It is what the
#: controller sees on the wire, so it is kept on purpose although it predates the project name.
SERVER_NAME = "stadia-local-receiver"
