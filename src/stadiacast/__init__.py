"""Stadiacast: a local server for the Stadia Controller's Wi-Fi mode.

A LAN-side stand-in for the shut-down Stadia cloud endpoints, so a Stadia Controller in
Wi-Fi mode (stock firmware) keeps working with the owner's own PC.
"""

__version__ = "0.1.0"

#: Name the receiver reports to the controller (HTTP ``Server`` header, SDP build number).  Wire-visible and
#: proven live; it predates the project name and is kept on purpose.
SERVER_NAME = "stadia-local-receiver"
