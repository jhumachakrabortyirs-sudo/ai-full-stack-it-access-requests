"""Loaded only by explicitly prepared CLI test subprocesses via PYTHONPATH."""
import os
import socket


_original_getitem = type(os.environ).__getitem__


def _forbid_credential(environment, key):
    if key == "API_KEY":
        raise AssertionError("CLI test attempted to read API_KEY")
    return _original_getitem(environment, key)


def _forbid_network(*args, **kwargs):
    raise AssertionError("CLI test attempted network access")


type(os.environ).__getitem__ = _forbid_credential
# Preserve socket's type so SSL and HTTP imports work, while blocking DNS and
# outbound traffic before any connected transport can be used.
socket.getaddrinfo = _forbid_network
socket.create_connection = _forbid_network
socket.socket.connect = _forbid_network
socket.socket.connect_ex = _forbid_network
socket.socket.sendto = _forbid_network
