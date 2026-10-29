"""Helper processes the receiver spawns next to itself: a redirecting DNS server and an SNTP server.

Both run as ``python -m stadiacast.helpers.<name>`` and write JSON Lines logs into the run
directory.  They are separate processes because they bind privileged-looking ports (53, 123)
and must keep answering while the receiver's event loop is busy.
"""
