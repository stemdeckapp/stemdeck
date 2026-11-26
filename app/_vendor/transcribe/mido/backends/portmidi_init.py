# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Low-level wrapper for PortMidi library

Copied straight from Grant Yoshida's portmidizero, with slight
modifications.
"""
import ctypes
import ctypes.util
import sys

dll_name = ''
if sys.platform == 'darwin':
    dll_name = ctypes.util.find_library('libportmidi.dylib')
elif sys.platform in ('win32', 'cygwin'):
    dll_name = 'portmidi.dll'
else:
    dll_name = 'libportmidi.so'

lib = ctypes.CDLL(dll_name)

null = None
false = 0
true = 1

# portmidi.h

# From portmidi.h
PM_HOST_ERROR_MSG_LEN = 256


def get_host_error_message():
    """Return host error message."""
    buf = ctypes.create_string_buffer(PM_HOST_ERROR_MSG_LEN)
    lib.Pm_GetHostErrorText(buf, PM_HOST_ERROR_MSG_LEN)
    return buf.raw.decode().rstrip('\0')


PmError = ctypes.c_int
# PmError enum
pmNoError = 0
pmHostError = -10000
pmInvalidDeviceId = -9999
pmInsufficientMemory = -9989
pmBufferTooSmall = -9979
pmBufferOverflow = -9969
pmBadPtr = -9959
pmBadData = -9994
pmInternalError = -9993
pmBufferMaxSize = -9992

lib.Pm_Initialize.restype = PmError
lib.Pm_Terminate.restype = PmError

PmDeviceID = ctypes.c_int

PortMidiStreamPtr = ctypes.c_void_p
PmStreamPtr = PortMidiStreamPtr
PortMidiStreamPtrPtr = ctypes.POINTER(PortMidiStreamPtr)

lib.Pm_HasHostError.restype = ctypes.c_int
lib.Pm_HasHostError.argtypes = [PortMidiStreamPtr]

lib.Pm_GetErrorText.restype = ctypes.c_char_p
lib.Pm_GetErrorText.argtypes = [PmError]

lib.Pm_GetHostErrorText.argtypes = [ctypes.c_char_p, ctypes.c_uint]

pmNoDevice = -1


class PmDeviceInfo(ctypes.Structure):
    _fields_ = [("structVersion", ctypes.c_int),
                ("interface", ctypes.c_char_p),
                ("name", ctypes.c_char_p),
                ("is_input", ctypes.c_int),
                ("is_output", ctypes.c_int),
                ("opened", ctypes.c_int)]


PmDeviceInfoPtr = ctypes.POINTER(PmDeviceInfo)

lib.Pm_CountDevices.restype = ctypes.c_int
lib.Pm_GetDefaultOutputDeviceID.restype = PmDeviceID
lib.Pm_GetDefaultInputDeviceID.restype = PmDeviceID

PmTimestamp = ctypes.c_long
PmTimeProcPtr = ctypes.CFUNCTYPE(PmTimestamp, ctypes.c_void_p)
NullTimeProcPtr = ctypes.cast(null, PmTimeProcPtr)

# PmBefore is not defined

lib.Pm_GetDeviceInfo.argtypes = [PmDeviceID]
lib.Pm_GetDeviceInfo.restype = PmDeviceInfoPtr

lib.Pm_OpenInput.restype = PmError
lib.Pm_OpenInput.argtypes = [PortMidiStreamPtrPtr,
                             PmDeviceID,
                             ctypes.c_void_p,
                             ctypes.c_long,
                             PmTimeProcPtr,
                             ctypes.c_void_p]

lib.Pm_OpenOutput.restype = PmError
lib.Pm_OpenOutput.argtypes = [PortMidiStreamPtrPtr,
                              PmDeviceID,
                              ctypes.c_void_p,
                              ctypes.c_long,
                              PmTimeProcPtr,
                              ctypes.c_void_p,
                              ctypes.c_long]

lib.Pm_SetFilter.restype = PmError
lib.Pm_SetFilter.argtypes = [PortMidiStreamPtr, ctypes.c_long]

lib.Pm_SetChannelMask.restype = PmError
lib.Pm_SetChannelMask.argtypes = [PortMidiStreamPtr, ctypes.c_int]

lib.Pm_Abort.restype = PmError
lib.Pm_Abort.argtypes = [PortMidiStreamPtr]

lib.Pm_Close.restype = PmError
lib.Pm_Close.argtypes = [PortMidiStreamPtr]

PmMessage = ctypes.c_long


class PmEvent(ctypes.Structure):
    _fields_ = [("message", PmMessage),
                ("timestamp", PmTimestamp)]


PmEventPtr = ctypes.POINTER(PmEvent)

lib.Pm_Read.restype = PmError
lib.Pm_Read.argtypes = [PortMidiStreamPtr, PmEventPtr, ctypes.c_long]

lib.Pm_Poll.restype = PmError
lib.Pm_Poll.argtypes = [PortMidiStreamPtr]

lib.Pm_Write.restype = PmError
lib.Pm_Write.argtypes = [PortMidiStreamPtr, PmEventPtr, ctypes.c_long]

lib.Pm_WriteShort.restype = PmError
lib.Pm_WriteShort.argtypes = [PortMidiStreamPtr, PmTimestamp, ctypes.c_long]

lib.Pm_WriteSysEx.restype = PmError
lib.Pm_WriteSysEx.argtypes = [PortMidiStreamPtr, PmTimestamp, ctypes.c_char_p]

# porttime.h

# PtError enum
PtError = ctypes.c_int
ptNoError = 0
ptHostError = -10000
ptAlreadyStarted = -9999
ptAlreadyStopped = -9998
ptInsufficientMemory = -9997

PtTimestamp = ctypes.c_long
PtCallback = ctypes.CFUNCTYPE(PmTimestamp, ctypes.c_void_p)

lib.Pt_Start.restype = PtError
lib.Pt_Start.argtypes = [ctypes.c_int, PtCallback, ctypes.c_void_p]

lib.Pt_Stop.restype = PtError
lib.Pt_Started.restype = ctypes.c_int
lib.Pt_Time.restype = PtTimestamp
