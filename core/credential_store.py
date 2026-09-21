"""Encrypt Google profile credentials for the current Windows user (DPAPI)."""
import ctypes
import os
from ctypes import wintypes

PREFIX = b'AGENTIC-DPAPI-1\0'


def _crypt(payload, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Google account credential storage currently requires Windows DPAPI')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    dll = ctypes.WinDLL('crypt32', use_last_error=True)
    function = dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(result.data)


def protect(payload):
    return PREFIX + _crypt(payload)


def unprotect(payload):
    # Backward compatible read; activation migrates legacy plaintext atomically.
    return _crypt(payload[len(PREFIX):], True) if payload.startswith(PREFIX) else payload


def save(path, payload):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(protect(payload))
    temporary.replace(path)
