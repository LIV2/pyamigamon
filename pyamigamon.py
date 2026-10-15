#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Matthew Harlum
"""pyamigamon - crash monitor for an Amiga running Kickstart 3.x (SAD).

Waits on the serial port for the Amiga to crash. When a dead-end alert
("guru") happens, exec blinks the power LED for a few seconds before
rebooting; during that window a DEL (0x7F) on the serial port drops it into
SAD. We keep sending DEL until the Amiga answers, connect to SAD with
pyamigadebug, report the guru number and PC, then drop into a small
interactive monitor for inspecting the crashed machine.
"""
import argparse
import contextlib
import datetime
import os
import struct
import sys
import time
import types

import serial

HISTORY_FILE = os.path.expanduser("~/.pyamigamon_history")
HISTORY_LENGTH = 1000
DEFAULT_PYAMIGADEBUG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pyamigadebug")

# Exec stashes the last dead-end alert in low memory so it survives a reset
# and can be shown as the guru after reboot.
HELP_ADDR = 0x000    # 'HELP' marker when an alert is pending
ALERT_ADDR = 0x100   # alert number
TASK_ADDR = 0x104    # task that caused it
HELP_MAGIC = 0x48454C50  # 'HELP'

# SAD context frame (SAD/GET_CONTEXT_FRAME autodoc). Everything from SAD_SR on is
# the exception frame SAD was entered with; when a CPU exception in supervisor mode
# led to the guru, the crash's own exception frame follows directly after it.
SAD_VBR = 0
SAD_ATTNFLAGS = 4
SAD_USP = 12
SAD_D0 = 16
SAD_A0 = 48
SAD_SR = 80
SAD_PC = 82
SAD_FRAME_SIZE = 8  # SR, PC, format word (zero filler on a 68000)
SAD_READ = SAD_SR + SAD_FRAME_SIZE + 92  # plus the largest (format $B) exception frame

# ExecBase->AttnFlags
AFF_68010 = 0x01
CPU_FLAGS = [(0x80, "68060"), (0x08, "68040"), (0x04, "68030"), (0x02, "68020"), (0x01, "68010")]
FPU_FLAGS = [(0x40, "68040 FPU"), (0x20, "68882"), (0x10, "68881")]

# Exception stack frame sizes by format (68010-68060 / CPU32)
FRAME_SIZES = {0x0: 8, 0x1: 8, 0x2: 12, 0x3: 12, 0x4: 16, 0x7: 60, 0x8: 58, 0x9: 20, 0xA: 32, 0xB: 92, 0xC: 24}
EXCEPTION_NAMES = {
    2: "Bus error / access fault", 3: "Address error", 4: "Illegal instruction",
    5: "Zero divide", 6: "CHK instruction", 7: "TRAPV / TRAPcc", 8: "Privilege violation",
    9: "Trace", 10: "Line-A emulator", 11: "Line-F emulator", 13: "Coprocessor protocol violation",
    14: "Format error", 15: "Uninitialized interrupt", 24: "Spurious interrupt",
    48: "FPU branch/set on unordered", 49: "FPU inexact result", 50: "FPU divide by zero",
    51: "FPU underflow", 52: "FPU operand error", 53: "FPU overflow", 54: "FPU signaling NaN",
    55: "FPU unimplemented data type", 56: "MMU configuration error", 57: "MMU illegal operation",
    58: "MMU access level violation", 60: "Unimplemented effective address",
    61: "Unimplemented integer instruction",
}
EXCEPTION_NAMES.update({25 + i: f"Level {i + 1} autovector interrupt" for i in range(7)})
EXCEPTION_NAMES.update({32 + i: f"TRAP #{i}" for i in range(16)})

# ExecBase / Task / Process offsets (exec/execbase.i, exec/tasks.i, dos/dosextens.i)
EXEC_INTVECTS = 84
EXEC_THISTASK = 276
EXEC_IDNESTCNT = 294  # followed by TDNestCnt
EXEC_DEVICELIST = 350
EXEC_RESOURCELIST = 336
EXEC_LIBLIST = 378
EXEC_TASKREADY = 406
EXEC_TASKWAIT = 420
EXEC_PORTLIST = 392
EXEC_SEMAPHORELIST = 532
EXEC_RESMODULES = 300
EXEC_MEMLIST = 322
EXEC_KICKMEMPTR = 546
EXEC_KICKTAGPTR = 550
EXEC_KICKCHECKSUM = 554
LN_TYPE = 8
LN_PRI = 9
LN_NAME = 10
TC_STATE = 15
TC_SIGWAIT = 22
# Full Task structure (exec/tasks.i)
TC_FLAGS = 14
TC_IDNESTCNT = 16
TC_TDNESTCNT = 17
TC_SIGALLOC = 18
TC_SIGRECVD = 26
TC_SIGEXCEPT = 30
TC_TRAPALLOC = 34
TC_TRAPABLE = 36
TC_EXCEPTDATA = 38
TC_EXCEPTCODE = 42
TC_TRAPDATA = 46
TC_TRAPCODE = 50
TC_SPREG = 54
TC_SPLOWER = 58
TC_SPUPPER = 62
TC_SWITCH = 66
TC_LAUNCH = 70
TC_MEMENTRY = 74
TC_USERDATA = 88
TC_SIZE = 92
TC_FLAG_NAMES = [(0x01, "proctime"), (0x08, "etask"), (0x10, "stackchk"), (0x20, "except"),
                 (0x40, "switch"), (0x80, "launch")]
# Process (dos/dosextens.i), after the embedded Task
PR_MSGPORT = 92
PR_SEGLIST = 128
PR_STACKSIZE = 132
PR_RESULT2 = 148
PR_CURRENTDIR = 152
PR_CIS = 156
PR_COS = 160
PR_CONSOLETASK = 164
PR_FILESYSTEMTASK = 168
PR_WINDOWPTR = 184
PR_HOMEDIR = 188
PR_EXITCODE = 196
PR_ARGUMENTS = 204
PR_SIZE = 228
CLI_SETNAME = 4
CLI_PROMPT = 24
CLI_INTERACTIVE = 40
CLI_BACKGROUND = 44
CLI_DEFAULTSTACK = 52
CLI_SIZE = 64
# Registers exec's task switch leaves at tc_SPReg (checked on Kickstart 3.1):
# [FPU fsave frame, if there is an FPU][PC.l][SR.w][D0-D7/A0-A6]
SWITCH_FRAME = 4 + 2 + 15 * 4
AFF_FPU = 0x70  # 68881, 68882 or 68040 FPU
STACK_LOW_WARN = 256
PR_TASKNUM = 140
PR_CLI = 172
CLI_COMMANDNAME = 16
CLI_MODULE = 60
NT_TASK = 1
NT_PROCESS = 13
# Registers saved around exec calls made through SAD ("sp" is the user stack pointer)
CALL_REGS = ["d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7",
             "a0", "a1", "a2", "a3", "a4", "a5", "a6", "sp", "pc"]
TASK_STATES = {0: "inval", 1: "added", 2: "run", 3: "ready", 4: "wait", 5: "except", 6: "removed"}
MAX_LIST_NODES = 256  # stop walking a corrupt list instead of looping forever

# MsgPort / SignalSemaphore offsets (exec/ports.i, exec/semaphores.i)
MP_FLAGS = 14
MP_SIGBIT = 15
MP_SIGTASK = 16
MP_MSGLIST = 20
PORT_ACTIONS = {0: "signal", 1: "softint", 2: "ignore"}
SS_NESTCOUNT = 14
SS_WAITQUEUE = 16
SS_OWNER = 40
SS_QUEUECOUNT = 44
SR_WAITER = 8
SM_SHARED = 1  # low bit of sr_Waiter marks a shared request

# Library / Interrupt offsets (exec/libraries.i, exec/interrupts.i)
LIB_VERSION = 20
LIB_REVISION = 22
LIB_OPENCNT = 32
IS_DATA = 14
IS_CODE = 18
IV_SIZE = 12  # IntVector: iv_Data, iv_Code, iv_Node
IV_DATA = 0
IV_CODE = 4
IV_NODE = 8

INTENAR = 0xDFF01C
INTREQR = 0xDFF01E
# Paula interrupt bits; the ones in SERVER_CHAINS run a list of servers, the rest a single handler.
INT_NAMES = ["TBE", "DSKBLK", "SOFTINT", "PORTS", "COPER", "VERTB", "BLIT", "AUD0",
             "AUD1", "AUD2", "AUD3", "RBF", "DSKSYNC", "EXTER", "INTEN", "NMI"]
SERVER_CHAINS = {3, 4, 5, 13, 15}

# Expansion offsets (libraries/expansionbase.h, libraries/configvars.h, libraries/configregs.h)
EB_BOARDLIST = 60  # eb_Private05 in 2.0+ headers; the list of ConfigDevs
CD_FLAGS = 14
CD_ROM = 16        # struct ExpansionRom, 16 bytes
CD_BOARDADDR = 32
CD_BOARDSIZE = 36
CD_DRIVER = 44
CD_SIZE = 68
ER_TYPE = 0
ER_PRODUCT = 1
ER_FLAGS = 2
ER_MANUFACTURER = 4
ER_SERIALNUMBER = 6
ERT_TYPEMASK = 0xC0
ERT_ZORROII = 0xC0
ERT_ZORROIII = 0x80
ERTF_MEMLIST = 0x20
ERTF_DIAGVALID = 0x10
CD_FLAG_NAMES = [(0x01, "shutup"), (0x02, "configme"), (0x04, "badmem"), (0x08, "processed")]

# FileSystem.resource offsets (resources/filesysres.h)
FSR_CREATOR = 14
FSR_FILESYSENTRIES = 18
FSE_DOSTYPE = 14
FSE_VERSION = 18
FSE_PATCHFLAGS = 22
FSE_HANDLER = 38
FSE_STACKSIZE = 42
FSE_PRIORITY = 46
FSE_SEGLIST = 54
FSE_SIZE = 62
# fse_PatchFlags bits: which fields of the entry are set
FSE_PATCH_HANDLER = 0x08
FSE_PATCH_STACKSIZE = 0x10
FSE_PATCH_PRIORITY = 0x20
FSE_PATCH_SEGLIST = 0x80

# MemHeader / MemChunk (exec/memory.i)
MH_ATTRIBUTES = 14
MH_FIRST = 16
MH_LOWER = 20
MH_UPPER = 24
MH_FREE = 28
MH_SIZE = 32
MEM_BLOCKMASK = 7
MAX_MEM_CHUNKS = 4096
MEMF_NAMES = [(0x1, "public"), (0x2, "chip"), (0x4, "fast"), (0x100, "local"),
              (0x200, "24bitdma"), (0x400, "kick")]

# Resident (exec/resident.i)
RTC_MATCHWORD = 0x4AFC
RT_MATCHTAG = 2
RT_FLAGS = 10
RT_VERSION = 11
RT_TYPE = 12
RT_PRI = 13
RT_NAME = 14
RT_IDSTRING = 18
RT_INIT = 22
RT_SIZE = 26
RT_FLAG_NAMES = [(0x01, "cold"), (0x02, "single"), (0x04, "afterdos"), (0x80, "auto")]
NT_NAMES = {0: "unknown", 1: "task", 3: "device", 8: "resource", 9: "library", 13: "process"}

# DOS (dos/dosextens.i, dos/filehandler.i)
DL_ROOT = 34
RN_INFO = 24
DI_DEVINFO = 4
DOL_NEXT = 0
DOL_TYPE = 4
DOL_TASK = 8
DOL_LOCK = 12
DOL_HANDLER = 16       # device
DOL_STACKSIZE = 20
DOL_PRIORITY = 24
DOL_STARTUP = 28
DOL_SEGLIST = 32
DOL_ASSIGNNAME = 16    # late / non-binding assign
DOL_DISKTYPE = 32      # volume
DOL_NAME = 40
DOL_SIZE = 44
DLT_NAMES = {0: "device", 1: "assign", 2: "volume", 3: "late", 4: "nonbind"}
FL_VOLUME = 16
FSSM_UNIT = 0
FSSM_DEVICE = 4
FSSM_ENVIRON = 8
DE_TABLESIZE = 0
DE_SIZEBLOCK = 4
DE_SURFACES = 12
DE_BLOCKSPERTRACK = 20
DE_LOWCYL = 36
DE_HIGHCYL = 40
DE_NUMBUFFERS = 44
DE_BUFMEMTYPE = 48
DE_MAXTRANSFER = 52
DE_MASK = 56
DE_BOOTPRI = 60
DE_DOSTYPE = 64
DE_READ = 68
MAX_DOSLIST = 256

# exec LVOs used by kick
LVO_ALLOCMEM = -198
LVO_SUMKICKDATA = -612
LVO_CACHECLEARU = -636
LVO_FREEMEM = -210
LVO_DISABLE = -120
LVO_ENABLE = -126
LVO_OPENDEVICE = -444
LVO_CLOSEDEVICE = -450
LVO_DOIO = -456
LVO_CREATEIOREQUEST = -654
LVO_DELETEIOREQUEST = -660
LVO_CREATEMSGPORT = -666
LVO_DELETEMSGPORT = -672
MEMF_PUBLIC = 0x1

# IOStdReq (exec/io.i)
IO_COMMAND = 28
IO_FLAGS = 30
IO_ERROR = 31
IO_ACTUAL = 32
IO_LENGTH = 36
IO_DATA = 40
IO_OFFSET = 44
IOSTDREQ_SIZE = 48
CMD_READ = 2
CMD_WRITE = 3
BLOCK_SIZE = 512
IO_CHUNK_BLOCKS = 16       # 8 KB per DoIO
# dos.library
LVO_OPENLIBRARY = -552
LVO_CLOSELIBRARY = -414
LVO_OPEN = -30
LVO_CLOSE = -36
LVO_READ = -42
LVO_WRITE = -48
LVO_LOCK = -84
LVO_UNLOCK = -90
LVO_IOERR = -132
LVO_EXAMINE = -102
LVO_EXNEXT = -108
ERROR_NO_MORE_ENTRIES = 232
# FileInfoBlock (dos/dos.h)
FIB_DIRENTRYTYPE = 4
FIB_FILENAME = 8       # C string, 108 bytes
FIB_PROTECTION = 116
FIB_SIZE = 124
FIB_DATE = 132         # DateStamp: days since 1978-01-01, minutes, ticks
FIB_COMMENT = 144      # C string, 80 bytes
FIB_READ = 224
MODE_OLDFILE = 1005
MODE_NEWFILE = 1006
SHARED_LOCK = -2
DOS_CHUNK = 8192
# Return path for non-exec library calls made through SAD:
#   move.l 4.w,a6 ; jmp _LVODebug(a6)
DEBUG_STUB = bytes.fromhex("2C780004" "4EEEFF8E")
DOS_ERRORS = {103: "ERROR_NO_FREE_STORE", 202: "ERROR_OBJECT_IN_USE", 203: "ERROR_OBJECT_EXISTS",
              204: "ERROR_DIR_NOT_FOUND", 205: "ERROR_OBJECT_NOT_FOUND", 209: "ERROR_ACTION_NOT_KNOWN",
              210: "ERROR_INVALID_COMPONENT_NAME", 212: "ERROR_OBJECT_WRONG_TYPE",
              213: "ERROR_DISK_NOT_VALIDATED", 214: "ERROR_DISK_WRITE_PROTECTED",
              218: "ERROR_DEVICE_NOT_MOUNTED", 221: "ERROR_DISK_FULL", 222: "ERROR_DELETE_PROTECTED",
              223: "ERROR_WRITE_PROTECTED", 224: "ERROR_READ_PROTECTED", 225: "ERROR_NOT_A_DOS_DISK",
              226: "ERROR_NO_DISK", 232: "ERROR_NO_MORE_ENTRIES"}
IO_ERRORS = {-1: "IOERR_OPENFAIL", -2: "IOERR_ABORTED", -3: "IOERR_NOCMD", -4: "IOERR_BADLENGTH",
             -5: "IOERR_BADADDRESS", -6: "IOERR_UNITBUSY", -7: "IOERR_SELFTEST",
             21: "TDERR_NotSpecified", 22: "TDERR_NoSecHdr", 25: "TDERR_BadSecSum",
             28: "TDERR_WriteProt", 29: "TDERR_DiskChanged", 30: "TDERR_SeekError",
             32: "TDERR_NoMem", 33: "TDERR_BadUnitNum", 34: "TDERR_BadDriveType",
             35: "TDERR_DriveInUse", 36: "TDERR_PostReset"}
MEMF_CHIP = 0x2
MEMF_CLEAR = 0x10000
MEMF_KICK = 0x400

# KickMem block layout: MemList, then the KickTag table, then the hunks
ML_NUMENTRIES = 14
ML_ME = 16
ML_SIZE = 24          # MemList with a single MemEntry
NT_MEMORY = 10
KICK_TABLE = ML_SIZE
KICK_MAX_TAGS = 8
KICK_HUNKS = KICK_TABLE + (KICK_MAX_TAGS + 2) * 4  # tags + link to old table + terminator
KICK_HUNKS = (KICK_HUNKS + 7) & ~7

# Hunk file block types (dos/doshunks.h)
HUNK_NAME = 0x3E8
HUNK_CODE = 0x3E9
HUNK_DATA = 0x3EA
HUNK_BSS = 0x3EB
HUNK_RELOC32 = 0x3EC
HUNK_SYMBOL = 0x3F0
HUNK_DEBUG = 0x3F1
HUNK_END = 0x3F2
HUNK_HEADER = 0x3F3
HUNK_DREL32 = 0x3F7        # RELOC32SHORT in executables (what LoadSeg does since V37)
HUNK_RELOC32SHORT = 0x3FC


def wait_for_crash(ser, interval):
    """Send DEL until the Amiga responds, i.e. it is in the alert blink loop."""
    print("Waiting for Amiga to crash (guru blink)...")
    while not ser.in_waiting:
        ser.write(b"\x7F")
        ser.flush()
        time.sleep(interval)
    print("Amiga responded, entering SAD.")


def be16(buf, offset):
    return int.from_bytes(buf[offset:offset + 2], "big")


def be32(buf, offset):
    return int.from_bytes(buf[offset:offset + 4], "big")


def cpu_name(attn):
    cpu = next((name for bit, name in CPU_FLAGS if attn & bit), "68000")
    fpu = next((name for bit, name in FPU_FLAGS if attn & bit), None)
    return f"{cpu}/{fpu}" if fpu else cpu


def format_bits(value):
    """Set bit numbers, high to low, with runs collapsed: '31-28,8'."""
    bits = [b for b in range(31, -1, -1) if value & (1 << b)]
    runs, start = [], None
    for i, b in enumerate(bits):
        if start is None:
            start = b
        if i + 1 == len(bits) or bits[i + 1] != b - 1:
            runs.append(f"{start}-{b}" if start != b else f"{b}")
            start = None
    return ",".join(runs)


def format_sr(sr):
    trace = {0: "", 1: "T0 ", 2: "T1 ", 3: "T1 T0 "}[sr >> 14]
    mode = ("S" if sr & 0x2000 else "U") + ("M" if sr & 0x1000 else "")
    ccr = "".join(c if sr & bit else "-" for c, bit in zip("XNZVC", (16, 8, 4, 2, 1)))
    return f"{trace}{mode} IPL{(sr >> 8) & 7} {ccr}"


def exception_name(vector):
    if 64 <= vector <= 255:
        return f"User interrupt vector {vector}"
    return EXCEPTION_NAMES.get(vector, f"Vector {vector}")


def decode_frame(buf, offset, has_format, vector_hint):
    """Decode a CPU exception frame at buf[offset:].

    Returns a dict with sr, pc, vector, format, size and a list of extra
    (label, text) details, or None if it doesn't look like a frame.
    On a 68000 there is no format word, so the vector must come from the alert.
    """
    if not has_format:
        if vector_hint in (2, 3):
            # 68000 group 0 (bus/address error): access info, address, IR, then SR/PC
            info, addr, ir = be16(buf, offset), be32(buf, offset + 2), be16(buf, offset + 6)
            sr, pc = be16(buf, offset + 8), be32(buf, offset + 10)
            details = [("Fault address", f"${addr:08X} ({'read' if info & 0x10 else 'write'}, "
                                         f"{'not ' if info & 0x08 else ''}instruction, FC{info & 7})"),
                       ("Instruction", f"${ir:04X} (PC is 2-10 bytes past it)")]
            frame = dict(sr=sr, pc=pc, vector=vector_hint, format=None, size=14, details=details)
        else:
            sr, pc = be16(buf, offset), be32(buf, offset + 2)
            frame = dict(sr=sr, pc=pc, vector=vector_hint, format=None, size=6, details=[])
        # Bits that are always zero in a 68000 SR
        return frame if not frame["sr"] & 0x58E0 else None

    sr, pc, fv = be16(buf, offset), be32(buf, offset + 2), be16(buf, offset + 6)
    fmt, vector = fv >> 12, (fv & 0xFFF) >> 2
    if fmt not in FRAME_SIZES:
        return None
    f = offset  # frame-relative offsets below
    details = []
    if fmt == 0x2:
        # 020/030: address of the faulting instruction (PC is the next one).
        # 040 address error: the misaligned address.
        label = "Fault address" if vector == 3 else "Instruction address"
        details.append((label, f"${be32(buf, f + 8):08X}"))
    elif fmt == 0x3:
        details.append(("Effective address", f"${be32(buf, f + 8):08X}"))
    elif fmt == 0x4:
        if vector == 2:  # 68060 access fault
            details.append(("Fault address", f"${be32(buf, f + 8):08X}"))
            details.append(("FSLW", f"${be32(buf, f + 12):08X}"))
        else:  # 68LC040/68060 FPU emulation
            details.append(("Effective address", f"${be32(buf, f + 8):08X}"))
            details.append(("FPU instruction PC", f"${be32(buf, f + 12):08X}"))
    elif fmt == 0x7:  # 68040 access error
        details.append(("Fault address", f"${be32(buf, f + 20):08X}"))
        details.append(("Effective address", f"${be32(buf, f + 8):08X}"))
        ssw = be16(buf, f + 12)
        details.append(("SSW", f"${ssw:04X} ({'read' if ssw & 0x100 else 'write'})"))
    elif fmt == 0x8:  # 68010 bus/address error
        ssw = be16(buf, f + 8)
        details.append(("Fault address", f"${be32(buf, f + 10):08X}"))
        details.append(("SSW", f"${ssw:04X} ({'read' if ssw & 0x100 else 'write'})"))
    elif fmt == 0x9:
        details.append(("Instruction address", f"${be32(buf, f + 8):08X}"))
    elif fmt in (0xA, 0xB):  # 68020/68030 short/long bus fault
        ssw = be16(buf, f + 10)
        if ssw & 0x0100:  # DF: data cycle fault
            details.append(("Fault address", f"${be32(buf, f + 16):08X} "
                                             f"({'read' if ssw & 0x40 else 'write'}, data)"))
        if ssw & 0xC000:  # FC/FB: instruction pipe stage C/B fault
            stage = "C" if ssw & 0x8000 else "B"
            if fmt == 0xB:
                stage_b = be32(buf, f + 36)
                addr = stage_b - 2 if stage == "C" else stage_b
                details.append(("Fault address", f"${addr:08X} (instruction fetch, stage {stage})"))
            else:
                details.append(("Fault", f"instruction fetch, stage {stage}"))
        details.append(("SSW", f"${ssw:04X}"))
    return dict(sr=sr, pc=pc, vector=vector, format=fmt, size=FRAME_SIZES[fmt], details=details)


class SadContext:
    """Registers SAD saved on entry, plus the crash's exception frame if it survived."""

    def __init__(self, amiga, alert=None):
        self.addr = amiga._ctxframe
        buf = amiga.readmem(self.addr, SAD_READ)
        self.vbr = be32(buf, SAD_VBR)
        self.attn = be32(buf, SAD_ATTNFLAGS)
        self.usp = be32(buf, SAD_USP)
        self.d = [be32(buf, SAD_D0 + 4 * i) for i in range(8)]
        self.a = [be32(buf, SAD_A0 + 4 * i) for i in range(7)]
        self.has_format = bool(self.attn & AFF_68010)
        # SAD's own frame is always SR, PC and a format word: a real format 0 frame on
        # 68010+, and a zero filler word on a 68000 (checked on Kickstart 3.1).
        self.sr, self.pc = be16(buf, SAD_SR), be32(buf, SAD_PC)
        # Supervisor stack pointer at the moment SAD was entered
        self.ssp = self.addr + SAD_SR + SAD_FRAME_SIZE
        self.crash = None
        if alert is not None and (alert & 0x7FFFFF00) == 0:
            # CPU exception alerts are $800000vv / $000000vv, vv = vector number.
            vector = alert & 0xFF
            frame = decode_frame(buf, SAD_SR + SAD_FRAME_SIZE, self.has_format, vector)
            if frame and frame["vector"] == vector:
                frame["addr"] = self.ssp
                self.crash = frame

    def print_registers(self):
        for name, regs in (("D", self.d), ("A", self.a)):
            for base in (0, 4):
                print("  " + "  ".join(f"{name}{i} {regs[i]:08X}" for i in range(base, min(base + 4, len(regs)))))
        print(f"  USP {self.usp:08X}  SSP {self.ssp:08X}  VBR {self.vbr:08X}  CPU {cpu_name(self.attn)}")
        print(f"  SR  {self.sr:04X} ({format_sr(self.sr)})  PC {self.pc:08X}")


def print_crash_frame(ctx, alert):
    frame = ctx.crash
    if frame is None:
        if (alert & 0x7FFFFF00) == 0:
            print("  Exception frame: not found. If the task got a 'Software Failure' requester "
                  "first, the original frame is gone.")
        return
    mode = "supervisor" if frame["sr"] & 0x2000 else "user"
    fmt = f", format ${frame['format']:X}" if frame["format"] is not None else ""
    print(f"  Exception: {exception_name(frame['vector'])} (vector {frame['vector']}{fmt}) in {mode} mode")
    print(f"  Fault PC:  ${frame['pc']:08X}")
    print(f"  Fault SR:  ${frame['sr']:04X} ({format_sr(frame['sr'])})")
    for label, text in frame["details"]:
        print(f"  {label}: {text}")
    print(f"  Frame at ${frame['addr']:08X}, stack before exception ${frame['addr'] + frame['size']:08X}")


def load_hunks(path):
    """Parse an Amiga executable into hunks: dicts with size, data and relocs.

    relocs is a list of (target hunk, [offsets]); each offset holds a long to
    which the target hunk's load address must be added.
    """
    with open(path, "rb") as f:
        raw = f.read()
    pos = 0

    def u32():
        nonlocal pos
        if pos + 4 > len(raw):
            raise ValueError("truncated hunk file")
        pos += 4
        return int.from_bytes(raw[pos - 4:pos], "big")

    def u16():
        nonlocal pos
        pos += 2
        return int.from_bytes(raw[pos - 2:pos], "big")

    if u32() != HUNK_HEADER:
        raise ValueError(f"{path} is not an Amiga executable (no HUNK_HEADER)")
    while (n := u32()):  # resident library names, unused in executables
        pos += n * 4
    count, first, last = u32(), u32(), u32()
    if count != last - first + 1:
        raise ValueError("overlay executables are not supported")
    hunks = []
    for _ in range(count):
        word = u32()
        if word >> 30 == 3:
            u32()  # extended memory attributes
        hunks.append(dict(size=(word & 0x3FFFFFFF) * 4, data=b"", relocs=[]))
    for hunk in hunks:
        while True:
            btype = u32() & 0x3FFFFFFF
            if btype in (HUNK_CODE, HUNK_DATA):
                n = u32() & 0x3FFFFFFF
                hunk["data"] = raw[pos:pos + n * 4]
                pos += n * 4
            elif btype == HUNK_BSS:
                u32()
            elif btype == HUNK_RELOC32:
                while (n := u32()):
                    target = u32()
                    hunk["relocs"].append((target, [u32() for _ in range(n)]))
            elif btype in (HUNK_DREL32, HUNK_RELOC32SHORT):
                while (n := u16()):
                    target = u16()
                    hunk["relocs"].append((target, [u16() for _ in range(n)]))
                pos = (pos + 3) & ~3
            elif btype == HUNK_SYMBOL:
                while (n := u32()):
                    pos += (n + 1) * 4
            elif btype in (HUNK_DEBUG, HUNK_NAME):
                pos += u32() * 4
            elif btype == HUNK_END:
                break
            else:
                raise ValueError(f"unsupported hunk type ${btype:X} at file offset {pos - 4}")
    return hunks


def report_guru(amiga):
    print(f"SAD entry: {amiga.entry}")
    if amiga.entry != "Crash":
        print("Not a crash entry (SAD was entered via NMI or Debug()), no guru to report.")
        return None, None
    marker = amiga.peek32(HELP_ADDR)
    alert = amiga.peek32(ALERT_ADDR)
    task = amiga.peek32(TASK_ADDR)
    pc = amiga.getreg("pc")
    d7 = amiga.getreg("d7")
    if marker != HELP_MAGIC:
        # No 'HELP' marker; Alert() still has the alert number in D7.
        print(f"Warning: no 'HELP' marker at $0 (found ${marker:08X}), using D7 as alert number.")
        alert = d7
    ctx = SadContext(amiga, alert)
    print("*** GURU MEDITATION ***")
    print(f"  Alert: #{alert >> 16:04X}.{alert & 0xFFFF:04X}  (${alert:08X})")
    print(f"  Task:  ${task:08X}")
    print(f"  SAD entry PC: ${pc:08X} (inside exec's alert handling)")
    print_crash_frame(ctx, alert)
    return task, alert


def parse_hex(text):
    """Monitor-style number: hex by default, optional $ or 0x prefix."""
    text = text.strip().lower()
    if text.startswith("$"):
        text = text[1:]
    return int(text, 16)


def parse_fib(fib):
    cstr = lambda off, n: fib[off:off + n].split(b"\0", 1)[0].decode("latin-1")
    return dict(type=int.from_bytes(fib[FIB_DIRENTRYTYPE:FIB_DIRENTRYTYPE + 4], "big", signed=True),
                name=cstr(FIB_FILENAME, 108), protection=be32(fib, FIB_PROTECTION),
                size=be32(fib, FIB_SIZE), days=be32(fib, FIB_DATE),
                minutes=be32(fib, FIB_DATE + 4), comment=cstr(FIB_COMMENT, 80))


def format_protection(bits):
    """List-style hsparwed: hspa are set when on, rwed when their bit is clear."""
    flags = "".join(c if bits & (1 << b) else "-" for c, b in zip("hspa", (7, 6, 5, 4)))
    return flags + "".join("-" if bits & (1 << b) else c for c, b in zip("rwed", (3, 2, 1, 0)))


def format_fib(e):
    when = datetime.datetime(1978, 1, 1) + datetime.timedelta(days=e["days"], minutes=e["minutes"])
    size = "<dir>" if e["type"] > 0 else str(e["size"])
    line = f"  {e['name']:<30} {size:>9}  {format_protection(e['protection'])}  {when:%Y-%m-%d %H:%M}"
    return line + (f"  : {e['comment']}" if e["comment"] else "")


def dos_error_name(error):
    return f"{DOS_ERRORS.get(error, 'error')} ({error})"


def io_error_name(error):
    return f"{IO_ERRORS.get(error, 'error')} ({error})"


def hexdump(addr, data):
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        words = " ".join(chunk[i:i + 4].hex().upper() for i in range(0, len(chunk), 4))
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        print(f"{addr + offset:08X}  {words:<35}  {text}")


def valid_ptr(addr):
    return addr != 0 and not addr & 1


def read_cstr(amiga, addr, maxlen=64):
    if not addr:
        return "<null>"
    return amiga.readmem(addr, maxlen).split(b"\0", 1)[0].decode("latin-1")


def read_bstr(amiga, bptr):
    addr = (bptr << 2) & 0xFFFFFFFF
    if not addr:
        return ""
    length = amiga.peek8(addr)
    # Mountlist BSTRs often count a trailing NUL in their length
    return amiga.readmem(addr + 1, length).decode("latin-1").rstrip("\0") if length else ""


def list_nodes(amiga, header):
    """Yield node addresses of an exec List, bailing out if it looks corrupt."""
    node = amiga.peek32(header)
    for _ in range(MAX_LIST_NODES):
        if not valid_ptr(node):
            raise ValueError(f"bad node pointer ${node:08X} in list ${header:08X}")
        succ = amiga.peek32(node)
        if succ == 0:  # reached the list's tail sentinel
            return
        yield node
        node = succ
    raise ValueError(f"list ${header:08X} has more than {MAX_LIST_NODES} nodes, probably corrupt")


def format_approx(size):
    """Size rounded to one decimal, for regions that aren't an exact power of two."""
    for unit, shift in (("G", 30), ("M", 20), ("K", 10)):
        if size >= 1 << shift:
            return f"{size / (1 << shift):.1f}".rstrip("0").rstrip(".") + unit
    return str(size)


def format_dostype(dostype):
    """'DOS\\3' style: printable bytes as characters, others as \\n."""
    return "".join(chr(b) if 0x20 < b < 0x7F else f"\\{b}" for b in dostype.to_bytes(4, "big"))


def format_size(size):
    for unit, shift in (("M", 20), ("K", 10)):
        if size >= 1 << shift and size % (1 << shift) == 0:
            return f"{size >> shift}{unit}"
    return f"${size:X}"


def task_info(amiga, task):
    hdr = amiga.readmem(task, TC_SIGWAIT + 4)
    ntype = hdr[LN_TYPE]
    pri = int.from_bytes(hdr[LN_PRI:LN_PRI + 1], "big", signed=True)
    name = read_cstr(amiga, int.from_bytes(hdr[LN_NAME:LN_NAME + 4], "big"))
    state = TASK_STATES.get(hdr[TC_STATE], f"?{hdr[TC_STATE]}")
    sigwait = int.from_bytes(hdr[TC_SIGWAIT:TC_SIGWAIT + 4], "big")
    kind = {NT_TASK: "task", NT_PROCESS: "proc"}.get(ntype, f"?{ntype}")
    if ntype == NT_PROCESS:
        tasknum = amiga.peek32(task + PR_TASKNUM)
        cli = amiga.peek32(task + PR_CLI) << 2
        # A shell process running a command: show the command, it's what you want to see.
        if cli and amiga.peek32(cli + CLI_MODULE):
            name += f" [{tasknum}: {read_bstr(amiga, amiga.peek32(cli + CLI_COMMANDNAME))}]"
    return kind, pri, state, sigwait, name


class Monitor:
    DEFAULT_LENGTH = 0x80

    def __init__(self, amiga, guru_task=None, alert=None):
        self.amiga = amiga
        self.guru_task = guru_task
        self.alert = alert
        self.left_in_sad = True
        self.next_addr = 0

    def setup_readline(self):
        """Line editing, persistent history and tab completion, when interactive."""
        if not sys.stdin.isatty():
            return None
        try:
            import readline
        except ImportError:  # e.g. Windows without pyreadline
            return None
        try:
            readline.read_history_file(HISTORY_FILE)
        except OSError:
            pass
        readline.set_history_length(HISTORY_LENGTH)
        names = sorted(self.commands)

        def complete(text, state):
            if readline.get_begidx() > 0:  # only complete the command word
                return None
            matches = [n for n in names if n.startswith(text.lower())]
            return matches[state] + " " if state < len(matches) else None

        readline.set_completer(complete)
        readline.set_completer_delims(" ")
        # macOS's system Python links libedit, which uses a different binding syntax
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        return readline

    def run(self):
        readline = self.setup_readline()
        try:
            self.loop()
        finally:
            if readline:
                try:
                    readline.write_history_file(HISTORY_FILE)
                except OSError as e:
                    print(f"Couldn't save history to {HISTORY_FILE}: {e.strerror}")

    def loop(self):
        print("Monitor ready. Type ? for help. Up/down for history, tab completes commands.")
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                print()
                return
            if not line:
                continue
            cmd, *args = line.split()
            handler = self.commands.get(cmd.lower())
            if handler is None:
                print(f"Unknown command '{cmd}'. Type ? for help.")
                continue
            try:
                if handler(self, args) is False:
                    return
            except ValueError as e:
                print(f"Bad argument: {e}")
            except (struct.error, OverflowError, IndexError) as e:
                # Garbage pointers are expected on a crashed machine; keep the session alive.
                print(f"Error reading target memory: {e}")

    def cmd_memory(self, args):
        """m [addr] [len]  display memory (hex, default len $80; no addr continues)"""
        addr = parse_hex(args[0]) if args else self.next_addr
        length = parse_hex(args[1]) if len(args) > 1 else self.DEFAULT_LENGTH
        if length <= 0:
            raise ValueError("length must be > 0")
        hexdump(addr, self.amiga.readmem(addr, length))
        self.next_addr = addr + length

    def cmd_tasks(self, args):
        """t               list tasks (* = task that caused the guru)"""
        execbase = self.amiga.peek32(4)
        groups = [("run", [self.amiga.peek32(execbase + EXEC_THISTASK)])]
        for label, offset in (("ready", EXEC_TASKREADY), ("wait", EXEC_TASKWAIT)):
            groups.append((label, self.collect(label, execbase + offset)))
        print(f"  {'Address':<9} {'Type':<4} {'Pri':>4} {'State':<6} {'SigWait':<8}  Name")
        for label, nodes in groups:
            for task in nodes:
                if not valid_ptr(task):
                    continue
                kind, pri, state, sigwait, name = task_info(self.amiga, task)
                mark = "*" if task == self.guru_task else " "
                print(f"{mark} ${task:08X} {kind:<4} {pri:>4} {state:<6} {sigwait:08X}  {name}")

    def all_tasks(self):
        """(state label, task) for the running task and the ready and wait queues."""
        execbase = self.amiga.peek32(4)
        tasks = [("run", self.amiga.peek32(execbase + EXEC_THISTASK))]
        for label, offset in (("ready", EXEC_TASKREADY), ("wait", EXEC_TASKWAIT)):
            tasks += [(label, t) for t in self.collect(label, execbase + offset)]
        return [(label, t) for label, t in tasks if valid_ptr(t)]

    def find_task(self, arg):
        """A task by (case-insensitive) name or CLI command name, else by address."""
        tasks = self.all_tasks()
        wanted = arg.lower()
        for _, task in tasks:
            name = task_info(self.amiga, task)[4].lower()
            # task_info appends " [n: command]" for shell processes
            command = name.split(": ", 1)[1].rstrip("]") if " [" in name else ""
            if name.split(" [")[0] == wanted or command == wanted or command.endswith(":" + wanted):
                return task
        try:
            addr = parse_hex(arg)
        except ValueError:
            raise ValueError(f"no task named '{arg}'") from None
        if not any(t == addr for _, t in tasks):
            print(f"Note: ${addr:08X} is not in exec's task lists; showing it anyway.")
        return addr

    def cmd_task(self, args):
        """task [name|addr]  show a task's fields, stack and saved registers (default: guru/current task)"""
        execbase = self.amiga.peek32(4)
        this_task = self.amiga.peek32(execbase + EXEC_THISTASK)
        task = self.find_task(" ".join(args)) if args else (self.guru_task or this_task)
        tc = self.amiga.readmem(task, TC_SIZE)
        ntype = tc[LN_TYPE]
        name = read_cstr(self.amiga, be32(tc, LN_NAME))
        kind = {NT_TASK: "task", NT_PROCESS: "process"}.get(ntype, f"type {ntype}?")
        sbyte = lambda off: int.from_bytes(tc[off:off + 1], "big", signed=True)
        print(f"{kind.capitalize()} ${task:08X} \"{name}\"" + ("  (caused the guru)" if task == self.guru_task else ""))
        print(f"  Node      succ ${be32(tc, 0):08X}  pred ${be32(tc, 4):08X}  pri {sbyte(LN_PRI)}")
        flags = tc[TC_FLAGS]
        flag_text = ",".join(n for bit, n in TC_FLAG_NAMES if flags & bit) or "-"
        print(f"  State     {TASK_STATES.get(tc[TC_STATE], f'?{tc[TC_STATE]}')}   flags ${flags:02X} ({flag_text})")
        running = task == this_task
        if running:
            # The running task's counts live in ExecBase; tc_ copies are only saved at switch
            nest = self.amiga.readmem(execbase + EXEC_IDNESTCNT, 2)
            idn, tdn = (int.from_bytes(nest[i:i + 1], "big", signed=True) for i in (0, 1))
            print(f"  Nesting   IDNestCnt {idn} ({'enabled' if idn < 0 else 'Disable()d'})  "
                  f"TDNestCnt {tdn} ({'multitasking' if tdn < 0 else 'Forbid()den'})  (live, ExecBase)")
        else:
            print(f"  Nesting   IDNestCnt {sbyte(TC_IDNESTCNT)}  TDNestCnt {sbyte(TC_TDNESTCNT)}  "
                  f"(saved at task switch; -1 = not nested)")
        for label, off in (("alloc", TC_SIGALLOC), ("wait", TC_SIGWAIT), ("recvd", TC_SIGRECVD),
                           ("except", TC_SIGEXCEPT)):
            value = be32(tc, off)
            print(f"  Sig{label:<7}${value:08X}  {format_bits(value)}")
        print(f"  Traps     alloc ${be16(tc, TC_TRAPALLOC):04X}  able ${be16(tc, TC_TRAPABLE):04X}  "
              f"code ${be32(tc, TC_TRAPCODE):08X}  data ${be32(tc, TC_TRAPDATA):08X}")
        print(f"  Except    code ${be32(tc, TC_EXCEPTCODE):08X}  data ${be32(tc, TC_EXCEPTDATA):08X}")
        print(f"  Switch    ${be32(tc, TC_SWITCH):08X}  Launch ${be32(tc, TC_LAUNCH):08X}  "
              f"UserData ${be32(tc, TC_USERDATA):08X}  MemEntry lists {self.count_mementry(task)}")
        self.print_task_stack(tc, running)
        if ntype == NT_PROCESS:
            self.print_process(task)

    def count_mementry(self, task):
        # Tasks set up without NewList() (legal for old-style tasks) leave the list all zero
        if self.amiga.peek32(task + TC_MEMENTRY) == 0:
            return "0 (list not initialised)"
        return len(self.collect("MemEntry", task + TC_MEMENTRY))

    def print_task_stack(self, tc, running):
        lower, upper, spreg = be32(tc, TC_SPLOWER), be32(tc, TC_SPUPPER), be32(tc, TC_SPREG)
        print(f"  Stack     ${lower:08X}-${upper:08X} ({upper - lower} bytes)")
        if running:
            # Its live registers are in SAD's context, not at tc_SPReg
            ctx = SadContext(self.amiga)
            sp = ctx.usp if not ctx.sr & 0x2000 else None
            print("  Running   registers are in SAD's context (see r)")
            if sp is None:
                return
        else:
            sp = spreg
        used, free = upper - sp, sp - lower
        warn = ""
        if not lower <= sp <= upper:
            warn = "  ** SP OUTSIDE STACK: overflow or corruption"
        elif free < STACK_LOW_WARN:
            warn = "  ** nearly full"
        print(f"  SP        ${sp:08X}  used {used}  free {free}{warn}")
        if running or not valid_ptr(spreg):
            return
        attn = self.amiga.peek16(self.amiga.peek32(4) + 296)
        frame = self.amiga.readmem(spreg, 4 + SWITCH_FRAME + 32)
        off = 0
        if attn & AFF_FPU:
            if frame[0] != 0:
                print("  Saved     FPU was in use (non-null fsave frame); registers not decoded")
                return
            off = 4  # null fsave frame
        pc, sr = be32(frame, off), be16(frame, off + 4)
        regs = [be32(frame, off + 6 + 4 * i) for i in range(15)]
        print(f"  Saved     PC ${pc:08X}  SR ${sr:04X} ({format_sr(sr)})  (where it was switched out)")
        print("            " + "  ".join(f"D{i} {regs[i]:08X}" for i in range(4)))
        print("            " + "  ".join(f"D{i} {regs[i]:08X}" for i in range(4, 8)))
        print("            " + "  ".join(f"A{i} {regs[8 + i]:08X}" for i in range(4)))
        print("            " + "  ".join(f"A{i} {regs[8 + i]:08X}" for i in range(4, 7))
              + f"  A7 {spreg + off + SWITCH_FRAME:08X}")
        top = frame[off + SWITCH_FRAME:off + SWITCH_FRAME + 32]
        print("  Stack top " + " ".join(f"{be32(top, i):08X}" for i in range(0, len(top), 4)))

    def print_process(self, proc):
        pr = self.amiga.readmem(proc, PR_SIZE)
        bptr = lambda off: (be32(pr, off) << 2) & 0xFFFFFFFF
        port = proc + PR_MSGPORT
        msgs = len(self.collect("process port", port + MP_MSGLIST))
        print(f"  MsgPort   ${port:08X}  sigbit {pr[PR_MSGPORT + MP_SIGBIT]}  {msgs} message(s) queued")
        print(f"  SegList   ${bptr(PR_SEGLIST):08X}  StackSize {be32(pr, PR_STACKSIZE)}  "
              f"TaskNum {be32(pr, PR_TASKNUM)}  Result2 {int.from_bytes(pr[PR_RESULT2:PR_RESULT2 + 4], 'big', signed=True)}")
        print(f"  CurrentDir {self.describe_lock(bptr(PR_CURRENTDIR))}  HomeDir {self.describe_lock(bptr(PR_HOMEDIR))}")
        print(f"  CIS ${bptr(PR_CIS):08X}  COS ${bptr(PR_COS):08X}  ConsoleTask ${be32(pr, PR_CONSOLETASK):08X}  "
              f"FileSystemTask ${be32(pr, PR_FILESYSTEMTASK):08X}")
        print(f"  WindowPtr ${be32(pr, PR_WINDOWPTR):08X}  ExitCode ${be32(pr, PR_EXITCODE):08X}")
        args = be32(pr, PR_ARGUMENTS)
        if args:
            print(f"  Arguments \"{read_cstr(self.amiga, args, 128).strip()}\"")
        cli = bptr(PR_CLI)
        if valid_ptr(cli):
            c = self.amiga.readmem(cli, CLI_SIZE)
            cbptr = lambda off: be32(c, off)
            print(f"  CLI       ${cli:08X}  command \"{read_bstr(self.amiga, cbptr(CLI_COMMANDNAME))}\"  "
                  f"module ${(cbptr(CLI_MODULE) << 2) & 0xFFFFFFFF:08X}")
            print(f"            dir \"{read_bstr(self.amiga, cbptr(CLI_SETNAME))}\"  "
                  f"prompt \"{read_bstr(self.amiga, cbptr(CLI_PROMPT))}\"  "
                  f"{'interactive' if cbptr(CLI_INTERACTIVE) else 'batch'}"
                  f"{', background' if cbptr(CLI_BACKGROUND) else ''}  "
                  f"default stack {cbptr(CLI_DEFAULTSTACK) * 4}")

    def describe_lock(self, lock):
        if not lock:
            return "-"
        if not valid_ptr(lock):
            return f"${lock:08X}?"
        volume = (self.amiga.peek32(lock + FL_VOLUME) << 2) & 0xFFFFFFFF
        volname = read_bstr(self.amiga, self.amiga.peek32(volume + DOL_NAME)) if valid_ptr(volume) else "?"
        return f"${lock:08X} on {volname}:"

    def collect(self, label, header):
        """All nodes of a list; on corruption, warn and return what was readable."""
        nodes = []
        try:
            for node in list_nodes(self.amiga, header):
                nodes.append(node)
        except ValueError as e:
            print(f"Warning: {label} list: {e}")
        return nodes

    def print_libs(self, label, offset):
        nodes = self.collect(label, self.amiga.peek32(4) + offset)
        print(f"  {'Address':<9} {'Version':>9} {'Open':>4} {'Pri':>4}  Name")
        for lib in nodes:
            hdr = self.amiga.readmem(lib, LIB_OPENCNT + 2)
            pri = int.from_bytes(hdr[LN_PRI:LN_PRI + 1], "big", signed=True)
            name = read_cstr(self.amiga, int.from_bytes(hdr[LN_NAME:LN_NAME + 4], "big"))
            version = int.from_bytes(hdr[LIB_VERSION:LIB_VERSION + 2], "big")
            revision = int.from_bytes(hdr[LIB_REVISION:LIB_REVISION + 2], "big")
            opencnt = int.from_bytes(hdr[LIB_OPENCNT:LIB_OPENCNT + 2], "big")
            print(f"  ${lib:08X} {f'{version}.{revision}':>9} {opencnt:>4} {pri:>4}  {name}")

    def find_node(self, label, list_offset, name):
        execbase = self.amiga.peek32(4)
        for node in self.collect(label, execbase + list_offset):
            if read_cstr(self.amiga, self.amiga.peek32(node + LN_NAME)) == name:
                return node
        return None

    def find_library(self, name):
        return self.find_node("library", EXEC_LIBLIST, name)

    def cmd_filesystems(self, args):
        """f               list filesystems in FileSystem.resource"""
        fsres = self.find_node("resource", EXEC_RESOURCELIST, "FileSystem.resource")
        if fsres is None:
            print("FileSystem.resource not found.")
            return
        creator = read_cstr(self.amiga, self.amiga.peek32(fsres + FSR_CREATOR)).strip()
        print(f"FileSystem.resource at ${fsres:08X}, created by {creator}")
        entries = self.collect("filesystem", fsres + FSR_FILESYSENTRIES)
        print(f"  {'Address':<9} {'DosType':<9} {'Hex':<9} {'Version':>8} {'Patch':<5} "
              f"{'Stack':>6} {'Pri':>4}  {'SegList':<9} Name")
        for fse in entries:
            hdr = self.amiga.readmem(fse, FSE_SIZE)
            def ulong(offset):
                return int.from_bytes(hdr[offset:offset + 4], "big")
            dostype = ulong(FSE_DOSTYPE)
            version = ulong(FSE_VERSION)
            patch = ulong(FSE_PATCHFLAGS)
            # Fields after PatchFlags are only valid when their PatchFlags bit is set;
            # entries may even be allocated short, so unflagged fields can be garbage.
            stack = f"{ulong(FSE_STACKSIZE)}" if patch & FSE_PATCH_STACKSIZE else "-"
            pri = (f"{int.from_bytes(hdr[FSE_PRIORITY:FSE_PRIORITY + 4], 'big', signed=True)}"
                   if patch & FSE_PATCH_PRIORITY else "-")
            seglist = f"${(ulong(FSE_SEGLIST) << 2) & 0xFFFFFFFF:08X}" if patch & FSE_PATCH_SEGLIST else "-"
            name = read_cstr(self.amiga, ulong(LN_NAME)).strip() if ulong(LN_NAME) else ""
            handler = read_bstr(self.amiga, ulong(FSE_HANDLER)) if patch & FSE_PATCH_HANDLER else ""
            if handler:
                name += f" (handler {handler})"
            print(f"  ${fse:08X} {format_dostype(dostype):<9} ${dostype:08X} "
                  f"{f'{version >> 16}.{version & 0xFFFF}':>8} ${patch:03X}  "
                  f"{stack:>6} {pri:>4}  {seglist:<9} {name or '-'}")

    def cmd_autoconfig(self, args):
        """a               list autoconfig (Zorro) boards"""
        expbase = self.find_library("expansion.library")
        if expbase is None:
            print("expansion.library not found.")
            return
        boards = self.collect("board", expbase + EB_BOARDLIST)
        print(f"  {'Address':<9} {'Bus':<4} {'Board':<9} {'Size':>6}  {'Manuf':>5}/{'Prod':<4} "
              f"{'Serial':<8}  {'Type':<9} {'Flags':<18} Driver")
        for cd in boards:
            hdr = self.amiga.readmem(cd, CD_SIZE)
            rom = hdr[CD_ROM:CD_ROM + 16]
            ertype = rom[ER_TYPE]
            bus = {ERT_ZORROII: "Z2", ERT_ZORROIII: "Z3"}.get(ertype & ERT_TYPEMASK, "?")
            manuf = int.from_bytes(rom[ER_MANUFACTURER:ER_MANUFACTURER + 2], "big")
            serial_no = int.from_bytes(rom[ER_SERIALNUMBER:ER_SERIALNUMBER + 4], "big")
            boardaddr = int.from_bytes(hdr[CD_BOARDADDR:CD_BOARDADDR + 4], "big")
            boardsize = int.from_bytes(hdr[CD_BOARDSIZE:CD_BOARDSIZE + 4], "big")
            driver = int.from_bytes(hdr[CD_DRIVER:CD_DRIVER + 4], "big")
            kind = "mem" if ertype & ERTF_MEMLIST else "io"
            if ertype & ERTF_DIAGVALID:
                kind += "+rom"  # has a boot/diag ROM (e.g. a disk controller)
            flags = ",".join(n for bit, n in CD_FLAG_NAMES if hdr[CD_FLAGS] & bit) or "-"
            drivername = read_cstr(self.amiga, self.amiga.peek32(driver + LN_NAME)) if valid_ptr(driver) else "-"
            print(f"  ${cd:08X} {bus:<4} ${boardaddr:08X} {format_size(boardsize):>6}  {manuf:>5}/{rom[ER_PRODUCT]:<4} "
                  f"{serial_no:08X}  {kind:<9} {flags:<18} {drivername}")

    def cmd_libraries(self, args):
        """l               list libraries"""
        self.print_libs("library", EXEC_LIBLIST)

    def cmd_devices(self, args):
        """d               list devices"""
        self.print_libs("device", EXEC_DEVICELIST)

    def cmd_interrupts(self, args):
        """i               list interrupt vectors, handlers and servers"""
        execbase = self.amiga.peek32(4)
        intena = self.amiga.peek16(INTENAR)
        intreq = self.amiga.peek16(INTREQR)
        vectors = self.amiga.readmem(execbase + EXEC_INTVECTS, IV_SIZE * len(INT_NAMES))
        print(f"INTENA ${intena:04X}  INTREQ ${intreq:04X}  (master enable {'on' if intena & 0x4000 else 'OFF'})")
        print(f"  {'Bit':>3} {'Vector':<8} {'Ena':<3} {'Req':<3}  {'Node':<9} {'Code':<9} {'Data':<9} {'Pri':>4}  Name")
        for bit, vname in enumerate(INT_NAMES):
            if bit == 14:  # INTEN is the master enable bit, not a vector
                continue
            iv = vectors[bit * IV_SIZE:(bit + 1) * IV_SIZE]
            iv_data = int.from_bytes(iv[IV_DATA:IV_DATA + 4], "big")
            iv_code = int.from_bytes(iv[IV_CODE:IV_CODE + 4], "big")
            iv_node = int.from_bytes(iv[IV_NODE:IV_NODE + 4], "big")
            ena = "on" if intena & (1 << bit) else "-"
            req = "REQ" if intreq & (1 << bit) else "-"
            prefix = f"  {bit:>3} {vname:<8} {ena:<3} {req:<3}"
            if bit in SERVER_CHAINS:
                # iv_Data is the server List; each server is an Interrupt node.
                servers = self.collect(f"{vname} server", iv_data) if valid_ptr(iv_data) else []
                if not servers:
                    print(f"{prefix}  (no servers)")
                for n, server in enumerate(servers):
                    lead = prefix if n == 0 else " " * len(prefix)
                    print(f"{lead}  {self.format_interrupt(server)}")
            elif iv_node:
                print(f"{prefix}  {self.format_interrupt(iv_node)}")
            else:
                print(f"{prefix}  {'-':<9} ${iv_code:08X} ${iv_data:08X}       (exec default)")

    def format_interrupt(self, node):
        hdr = self.amiga.readmem(node, IS_CODE + 4)
        pri = int.from_bytes(hdr[LN_PRI:LN_PRI + 1], "big", signed=True)
        name = read_cstr(self.amiga, int.from_bytes(hdr[LN_NAME:LN_NAME + 4], "big"))
        is_data = int.from_bytes(hdr[IS_DATA:IS_DATA + 4], "big")
        is_code = int.from_bytes(hdr[IS_CODE:IS_CODE + 4], "big")
        return f"${node:08X} ${is_code:08X} ${is_data:08X} {pri:>4}  {name}"

    def node_name(self, node):
        if not valid_ptr(node):
            return "-"
        return read_cstr(self.amiga, self.amiga.peek32(node + LN_NAME))

    def cmd_ports(self, args):
        """p               list public message ports"""
        ports = self.collect("port", self.amiga.peek32(4) + EXEC_PORTLIST)
        print(f"  {'Address':<9} {'Action':<7} {'Sig':>3} {'Msgs':>4}  {'Name':<24} SigTask")
        for port in ports:
            hdr = self.amiga.readmem(port, MP_MSGLIST)
            flags = hdr[MP_FLAGS]
            sigtask = int.from_bytes(hdr[MP_SIGTASK:MP_SIGTASK + 4], "big")
            name = read_cstr(self.amiga, int.from_bytes(hdr[LN_NAME:LN_NAME + 4], "big"))
            msgs = len(self.collect(f"{name} message", port + MP_MSGLIST))
            action = PORT_ACTIONS.get(flags & 3, f"?{flags & 3}")
            # For PA_SOFTINT, mp_SigTask points at an Interrupt, not a Task; both start with a Node.
            target = f"${sigtask:08X} {self.node_name(sigtask)}" if sigtask else "-"
            print(f"  ${port:08X} {action:<7} {hdr[MP_SIGBIT]:>3} {msgs:>4}  {name:<24} {target}")

    def cmd_semaphores(self, args):
        """s               list public semaphores, owners and waiters"""
        sems = self.collect("semaphore", self.amiga.peek32(4) + EXEC_SEMAPHORELIST)
        print(f"  {'Address':<9} {'Nest':>4} {'Queue':>5}  {'Name':<24} Owner")
        for sem in sems:
            hdr = self.amiga.readmem(sem, SS_QUEUECOUNT + 2)
            name = read_cstr(self.amiga, int.from_bytes(hdr[LN_NAME:LN_NAME + 4], "big"))
            nest = int.from_bytes(hdr[SS_NESTCOUNT:SS_NESTCOUNT + 2], "big", signed=True)
            queue = int.from_bytes(hdr[SS_QUEUECOUNT:SS_QUEUECOUNT + 2], "big", signed=True)
            owner = int.from_bytes(hdr[SS_OWNER:SS_OWNER + 4], "big")
            if queue < 0:
                holder = "free"
            elif owner:
                holder = f"${owner:08X} {self.node_name(owner)}"
            else:
                holder = "shared"
            print(f"  ${sem:08X} {nest:>4} {queue:>5}  {name:<24} {holder}")
            # Waiters are what you want when hunting a deadlock.
            for req in self.collect(f"{name} wait queue", sem + SS_WAITQUEUE):
                waiter = self.amiga.peek32(req + SR_WAITER)
                task = waiter & ~SM_SHARED
                mode = "shared" if waiter & SM_SHARED else "exclusive"
                print(f"{'':>48}waiting: ${task:08X} {self.node_name(task)} ({mode})")

    def write_values(self, args, size):
        if len(args) < 2:
            raise ValueError("need an address and at least one value")
        addr = parse_hex(args[0])
        if size > 1 and addr & 1:
            raise ValueError(f"${addr:08X} is odd; word/long writes must be even (use wb)")
        values = [parse_hex(v) for v in args[1:]]
        limit = 1 << (8 * size)
        for v in values:
            if not 0 <= v < limit:
                raise ValueError(f"${v:X} doesn't fit in {size} byte(s)")
        # One sized access per value: a bulk array write would hit hardware
        # registers with the wrong access size.
        poke = {1: self.amiga.poke8, 2: self.amiga.poke16, 4: self.amiga.poke32}[size]
        for i, v in enumerate(values):
            poke(addr + i * size, v)
        print(f"Wrote {len(values)} {['byte', 'word', '', 'long'][size - 1]}"
              f"{'s' if len(values) > 1 else ''} at ${addr:08X}")
        self.next_addr = addr

    def cmd_write_byte(self, args):
        """wb addr val ... write bytes"""
        self.write_values(args, 1)

    def cmd_write_word(self, args):
        """ww addr val ... write words (even address)"""
        self.write_values(args, 2)

    def cmd_write_long(self, args):
        """wl addr val ... write longs (even address)"""
        self.write_values(args, 4)

    def cmd_upload(self, args):
        """upload file addr   copy a host file to Amiga memory in one transfer (RAM, not registers)"""
        if len(args) != 2:
            raise ValueError("usage: upload file addr")
        addr = parse_hex(args[1])
        try:
            with open(args[0], "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"Can't read {args[0]}: {e.strerror}")
            return
        start = time.monotonic()
        self.amiga.writemem(addr, data)
        print(f"Uploaded {len(data)} bytes to ${addr:08X}-${addr + len(data) - 1:08X} "
              f"in {time.monotonic() - start:.1f}s")
        self.next_addr = addr

    def cmd_registers(self, args):
        """r               show registers saved on SAD entry, and the crash's exception frame"""
        ctx = SadContext(self.amiga, self.alert)
        ctx.print_registers()
        if self.alert is not None:
            print_crash_frame(ctx, self.alert)

    def cmd_memlist(self, args):
        """mem [v]         list memory regions and check their free lists (v: list chunks)"""
        verbose = bool(args) and args[0].lower().startswith("v")
        headers = self.collect("memory", self.amiga.peek32(4) + EXEC_MEMLIST)
        print(f"  {'Address':<9} {'Lower':<9} {'Upper':<9} {'Size':>6} {'Free':>9} {'Largest':>9} "
              f"{'Chunks':>6} {'Pri':>4}  {'Name':<18} Attributes")
        for mh in headers:
            hdr = self.amiga.readmem(mh, MH_SIZE)
            attrs = be16(hdr, MH_ATTRIBUTES)
            lower, upper, free = be32(hdr, MH_LOWER), be32(hdr, MH_UPPER), be32(hdr, MH_FREE)
            pri = int.from_bytes(hdr[LN_PRI:LN_PRI + 1], "big", signed=True)
            name = read_cstr(self.amiga, be32(hdr, LN_NAME))
            attr_text = ",".join(n for bit, n in MEMF_NAMES if attrs & bit) or f"${attrs:04X}"
            chunks, problems = self.walk_chunks(be32(hdr, MH_FIRST), lower, upper)
            total = sum(size for _, size in chunks)
            largest = max((size for _, size in chunks), default=0)
            if total != free:
                problems.append(f"mh_Free is {free} but the chunks add up to {total}")
            print(f"  ${mh:08X} ${lower:08X} ${upper:08X} {format_approx(upper - lower):>6} {free:>9} "
                  f"{largest:>9} {len(chunks):>6} {pri:>4}  {name:<18} {attr_text}")
            for problem in problems:
                print(f"    ** {problem}")
            if verbose:
                for addr, size in chunks:
                    print(f"      chunk ${addr:08X}-${addr + size - 1:08X} {size:>9}")

    def walk_chunks(self, chunk, lower, upper):
        """Walk a MemHeader's free list, returning (chunks, problems found)."""
        chunks, problems = [], []
        prev_end = lower
        while chunk:
            if len(chunks) >= MAX_MEM_CHUNKS:
                problems.append(f"more than {MAX_MEM_CHUNKS} chunks, stopped (loop?)")
                break
            if chunk & MEM_BLOCKMASK or not lower <= chunk < upper:
                problems.append(f"chunk pointer ${chunk:08X} is misaligned or outside the region")
                break
            if chunk < prev_end:
                problems.append(f"chunk ${chunk:08X} overlaps or is out of order (previous ends ${prev_end:08X})")
                break
            if chunk == prev_end and chunks:
                problems.append(f"chunk ${chunk:08X} touches the previous one (should have been merged)")
            mc = self.amiga.readmem(chunk, 8)
            nxt, size = be32(mc, 0), be32(mc, 4)
            if size == 0 or size & MEM_BLOCKMASK or chunk + size > upper:
                problems.append(f"chunk ${chunk:08X} has a bad size {size}")
                break
            chunks.append((chunk, size))
            prev_end = chunk + size
            chunk = nxt
        return chunks, problems

    def cmd_residents(self, args):
        """res             list resident modules (ROM and KickTags)"""
        table = self.amiga.peek32(self.amiga.peek32(4) + EXEC_RESMODULES)
        print(f"  {'Address':<9} {'Pri':>4} {'Ver':>3} {'Type':<8} {'Flags':<16} {'Init':<9}  Name / IdString")
        count = 0
        while valid_ptr(table) and count < MAX_LIST_NODES:
            entry = self.amiga.peek32(table)
            if entry == 0:
                break
            if entry & 0x80000000:  # link to a continuation table
                table = entry & 0x7FFFFFFF
                continue
            table += 4
            count += 1
            rt = self.amiga.readmem(entry, RT_SIZE)
            bad = be16(rt, 0) != RTC_MATCHWORD or be32(rt, RT_MATCHTAG) != entry
            flags = rt[RT_FLAGS]
            flag_text = ",".join(n for bit, n in RT_FLAG_NAMES if flags & bit) or "-"
            pri = int.from_bytes(rt[RT_PRI:RT_PRI + 1], "big", signed=True)
            name = read_cstr(self.amiga, be32(rt, RT_NAME)).strip()
            idstr = read_cstr(self.amiga, be32(rt, RT_IDSTRING)).strip() if be32(rt, RT_IDSTRING) else ""
            mark = "!" if bad else " "
            print(f"{mark} ${entry:08X} {pri:>4} {rt[RT_VERSION]:>3} {NT_NAMES.get(rt[RT_TYPE], rt[RT_TYPE]):<8} "
                  f"{flag_text:<16} ${be32(rt, RT_INIT):08X}  {name}" + (f"  [{idstr}]" if idstr and idstr != name else ""))
        if count >= MAX_LIST_NODES:
            print(f"Warning: more than {MAX_LIST_NODES} residents, stopped (corrupt table?)")

    def cmd_dos(self, args):
        """dos             list DOS devices, volumes and assigns"""
        dosbase = self.find_library("dos.library")
        if dosbase is None:
            print("dos.library not found (DOS not up yet?).")
            return
        root = self.amiga.peek32(dosbase + DL_ROOT)
        info = self.amiga.peek32(root + RN_INFO) << 2 if valid_ptr(root) else 0
        dol = (self.amiga.peek32(info + DI_DEVINFO) << 2) & 0xFFFFFFFF if valid_ptr(info) else 0
        print(f"  {'Address':<9} {'Type':<7} {'Name':<12} {'Task':<9}  Details")
        for _ in range(MAX_DOSLIST):
            if not dol:
                return
            if not valid_ptr(dol):
                print(f"Warning: bad DosList pointer ${dol:08X}")
                return
            d = self.amiga.readmem(dol, DOL_SIZE)
            dtype = be32(d, DOL_TYPE)
            task = be32(d, DOL_TASK)
            name = read_bstr(self.amiga, be32(d, DOL_NAME)) + ":"
            task_text = f"${task:08X}" if task else "-"
            print(f"  ${dol:08X} {DLT_NAMES.get(dtype, f'?{dtype}'):<7} {name:<12} {task_text:<9}  "
                  f"{self.dos_details(dtype, d)}")
            dol = (be32(d, DOL_NEXT) << 2) & 0xFFFFFFFF
        print(f"Warning: more than {MAX_DOSLIST} DosList entries, stopped (loop?)")

    def dos_details(self, dtype, d):
        if dtype == 0:  # device
            text = []
            handler = read_bstr(self.amiga, be32(d, DOL_HANDLER))
            if handler:
                text.append(f"handler {handler}")
            seglist = (be32(d, DOL_SEGLIST) << 2) & 0xFFFFFFFF
            if seglist:
                text.append(f"seglist ${seglist:08X}")
            fssm = self.read_fssm(be32(d, DOL_STARTUP))
            if fssm:
                text.append(fssm)
            text.append(f"pri {int.from_bytes(d[DOL_PRIORITY:DOL_PRIORITY + 4], 'big', signed=True)} "
                        f"stack {be32(d, DOL_STACKSIZE)}")
            return ", ".join(text)
        if dtype == 2:  # volume
            disktype = be32(d, DOL_DISKTYPE)
            return f"disk type {format_dostype(disktype)} (${disktype:08X})"
        if dtype == 1:  # assign
            lock = (be32(d, DOL_LOCK) << 2) & 0xFFFFFFFF
            if not valid_ptr(lock):
                return "no lock"
            volume = (self.amiga.peek32(lock + FL_VOLUME) << 2) & 0xFFFFFFFF
            volname = read_bstr(self.amiga, self.amiga.peek32(volume + DOL_NAME)) if valid_ptr(volume) else "?"
            return f"lock ${lock:08X} on {volname}:"
        if dtype in (3, 4):  # late / non-binding assign: target is a C string
            return f"-> {read_cstr(self.amiga, be32(d, DOL_ASSIGNNAME))}"
        return ""

    def read_fssm(self, startup):
        """Describe a FileSysStartupMsg, or return None if dol_Startup isn't one.

        dol_Startup is handler-specific: often a FileSysStartupMsg BPTR, but it
        can also be a plain number (e.g. for CON:), so check before trusting it.
        """
        fssm = (startup << 2) & 0xFFFFFFFF
        if startup < 0x400 or not valid_ptr(fssm):
            return None
        msg = self.amiga.readmem(fssm, 16)
        device = read_bstr(self.amiga, be32(msg, FSSM_DEVICE))
        environ = (be32(msg, FSSM_ENVIRON) << 2) & 0xFFFFFFFF
        if not device or not device.isprintable() or not valid_ptr(environ):
            return None
        text = f"{device} unit {be32(msg, FSSM_UNIT)}"
        de = self.amiga.readmem(environ, DE_READ)
        size = be32(de, DE_TABLESIZE)
        if size < 10:  # need up to de_HighCyl (index 10)
            return text
        low, high = be32(de, DE_LOWCYL), be32(de, DE_HIGHCYL)
        blocks = (high - low + 1) * be32(de, DE_SURFACES) * be32(de, DE_BLOCKSPERTRACK)
        text += f", cyl {low}-{high} ({format_approx(blocks * be32(de, DE_SIZEBLOCK) * 4)})"
        if size >= 13:
            text += f", maxtransfer ${be32(de, DE_MAXTRANSFER):X}"
        if size >= 14:
            text += f", mask ${be32(de, DE_MASK):08X}"
        if size >= 15:
            text += f", bootpri {int.from_bytes(de[DE_BOOTPRI:DE_BOOTPRI + 4], 'big', signed=True)}"
        if size >= 16:
            text += f", {format_dostype(be32(de, DE_DOSTYPE))}"
        return text

    def cmd_kick(self, args):
        """kick [file [chip] | clear]  show KickTags, load a resident module into them, or clear them"""
        execbase = self.amiga.peek32(4)
        if not args:
            self.print_kick(execbase)
            return
        if not self.system_callable("kick"):
            return
        saved = self.begin_calls()
        try:
            if args[0].lower() == "clear":
                self.amiga.poke32(execbase + EXEC_KICKMEMPTR, 0)
                self.amiga.poke32(execbase + EXEC_KICKTAGPTR, 0)
                self.amiga.poke32(execbase + EXEC_KICKCHECKSUM, 0)
                print("KickMemPtr/KickTagPtr cleared; takes effect at the next reboot.")
            else:
                chip = len(args) > 1 and args[1].lower() == "chip"
                self.kick_load(execbase, args[0], chip)
        finally:
            self.end_calls(saved)

    def kick_load(self, execbase, path, chip):
        try:
            hunks = load_hunks(path)
        except OSError as e:
            print(f"Can't read {path}: {e.strerror}")
            return
        offsets, total = [], KICK_HUNKS
        for hunk in hunks:
            offsets.append(total)
            total += (hunk["size"] + 7) & ~7
        flags = MEMF_CLEAR | (MEMF_CHIP if chip else MEMF_KICK)
        block = self.amiga.libcall(base=execbase, lvo=LVO_ALLOCMEM, d0=total, d1=flags, result="d0")
        if not block:
            print(f"AllocMem({total}, ${flags:X}) failed.")
            return
        bases = [block + o for o in offsets]
        print(f"Allocated {total} bytes at ${block:08X} ({'chip' if chip else 'MEMF_KICK'})")

        images, tags = [], []
        for n, hunk in enumerate(hunks):
            image = bytearray(hunk["data"].ljust(hunk["size"], b"\0"))
            for target, offs in hunk["relocs"]:
                for off in offs:
                    value = (be32(image, off) + bases[target]) & 0xFFFFFFFF
                    image[off:off + 4] = value.to_bytes(4, "big")
            for off in range(0, len(image) - RT_SIZE + 1, 2):
                if be16(image, off) == RTC_MATCHWORD and be32(image, off + RT_MATCHTAG) == bases[n] + off:
                    tags.append((bases[n] + off, image, off))
            images.append(image)
        if not tags:
            print("No ROM tag ($4AFC pointing to itself) found; nothing to add. Memory left allocated.")
            return
        if len(tags) > KICK_MAX_TAGS:
            print(f"More than {KICK_MAX_TAGS} ROM tags found; only the first {KICK_MAX_TAGS} are added.")
            tags = tags[:KICK_MAX_TAGS]

        old_mem = self.amiga.peek32(execbase + EXEC_KICKMEMPTR)
        old_tags = self.amiga.peek32(execbase + EXEC_KICKTAGPTR)
        # MemList so exec reserves this block again at reset; chained to any existing KickMem
        memlist = bytearray(ML_SIZE)
        memlist[0:4] = old_mem.to_bytes(4, "big")
        memlist[LN_TYPE] = NT_MEMORY
        memlist[ML_NUMENTRIES:ML_NUMENTRIES + 2] = (1).to_bytes(2, "big")
        memlist[ML_ME:ML_ME + 4] = block.to_bytes(4, "big")
        memlist[ML_ME + 4:ML_ME + 8] = total.to_bytes(4, "big")
        # Tag table: our residents, then a link (bit 31 set) to the previous table
        table = b"".join(addr.to_bytes(4, "big") for addr, _, _ in tags)
        if old_tags:
            table += (old_tags | 0x80000000).to_bytes(4, "big")
        table += bytes(4)
        self.amiga.writemem(block, bytes(memlist) + table)
        for base, image in zip(bases, images):
            print(f"Uploading {len(image)} bytes to ${base:08X}...")
            self.amiga.writemem(base, bytes(image))
        self.amiga.libcall(base=execbase, lvo=LVO_CACHECLEARU)
        self.amiga.sync()

        self.amiga.poke32(execbase + EXEC_KICKMEMPTR, block)
        self.amiga.poke32(execbase + EXEC_KICKTAGPTR, block + KICK_TABLE)
        checksum = self.amiga.libcall(base=execbase, lvo=LVO_SUMKICKDATA, result="d0")
        self.amiga.poke32(execbase + EXEC_KICKCHECKSUM, checksum)
        for addr, image, off in tags:
            name = read_cstr(self.amiga, be32(image, off + RT_NAME)).strip()
            print(f"Added resident ${addr:08X} {name} (version {image[off + RT_VERSION]}, "
                  f"pri {int.from_bytes(image[off + RT_PRI:off + RT_PRI + 1], 'big', signed=True)})")
        print(f"KickCheckSum ${checksum:08X}. Reboot (reboot command) to start the module.")

    def print_kick(self, execbase):
        mem = self.amiga.peek32(execbase + EXEC_KICKMEMPTR)
        tags = self.amiga.peek32(execbase + EXEC_KICKTAGPTR)
        checksum = self.amiga.peek32(execbase + EXEC_KICKCHECKSUM)
        print(f"KickMemPtr ${mem:08X}  KickTagPtr ${tags:08X}  KickCheckSum ${checksum:08X}")
        for _ in range(MAX_LIST_NODES):
            if not valid_ptr(mem):
                break
            entries = self.amiga.peek16(mem + ML_NUMENTRIES)
            for i in range(min(entries, 16)):
                addr = self.amiga.peek32(mem + ML_ME + 8 * i)
                length = self.amiga.peek32(mem + ML_ME + 8 * i + 4)
                print(f"  KickMem ${addr:08X}-${addr + length - 1:08X} ({length} bytes)")
            mem = self.amiga.peek32(mem)
        for _ in range(MAX_LIST_NODES):
            if not valid_ptr(tags):
                break
            entry = self.amiga.peek32(tags)
            if entry == 0:
                break
            if entry & 0x80000000:
                tags = entry & 0x7FFFFFFF
                continue
            name = read_cstr(self.amiga, self.amiga.peek32(entry + RT_NAME)).strip()
            print(f"  KickTag ${entry:08X} {name}")
            tags += 4

    def system_callable(self, what):
        """Can exec functions be called right now? Explain and return False if not.

        Calls run in the context saved by SAD, as if that code had made them, so
        they need a task in user mode with interrupts on: exactly what a call to
        Debug() from a normal task gives, and what NMI and crash entries don't.
        """
        problems = []
        if self.amiga.entry != "Debug()":
            problems.append(f"SAD was entered via {self.amiga.entry}, not Debug()")
        else:
            ctx = SadContext(self.amiga)
            execbase = self.amiga.peek32(4)
            nest = self.amiga.readmem(execbase + EXEC_IDNESTCNT, 2)
            idn, tdn = (int.from_bytes(nest[i:i + 1], "big", signed=True) for i in (0, 1))
            task = self.amiga.peek32(execbase + EXEC_THISTASK)
            if ctx.sr & 0x2000:
                problems.append("Debug() was called from supervisor mode")
            if idn >= 0:
                problems.append(f"the caller is inside Disable() (IDNestCnt {idn}), so I/O can't complete")
            if not valid_ptr(task) or self.amiga.peek8(task + TC_STATE) != 2:
                problems.append("there is no valid running task")
            if not problems and tdn >= 0:
                print(f"Warning: the caller is inside Forbid() (TDNestCnt {tdn}); "
                      "anything that waits will let other tasks run.")
        if problems:
            print(f"Refusing {what}: " + "; ".join(problems) + ".")
            print("It needs SAD entered via Debug() from a normal task (use --attach).")
            return False
        return True

    def begin_calls(self):
        """Start a sequence of exec calls: save the context and Disable().

        The calls clobber SAD's saved registers, so they're put back afterwards.
        Interrupts are kept off whenever control is back in SAD between calls, as
        amigaXfer does: SAD's serial handling is unreliable with interrupts on.
        I/O still completes, because Wait() switches to other tasks, which run
        with their own interrupt state.
        """
        saved = self.amiga.getregs(CALL_REGS)
        self.exec_call(LVO_DISABLE)
        return saved

    def end_calls(self, saved):
        self.exec_call(LVO_ENABLE)
        self.amiga.setregs(saved)

    def lib_call(self, base, lvo, stub, **regs):
        """Call a non-exec library function through SAD and return D0.

        pyamigadebug makes the called function return straight into exec's
        Debug(), which needs A6 = ExecBase. Other libraries return with their
        own base in A6, so they return through stub (DEBUG_STUB, in Amiga
        memory), which reloads ExecBase first.
        """
        old = self.amiga.execdebug
        self.amiga.execdebug = stub
        try:
            return self.amiga.libcall(base=base, lvo=lvo, result="d0", **regs)
        finally:
            self.amiga.execdebug = old

    def exec_call(self, lvo, **regs):
        """Call an exec function through SAD and return D0."""
        return self.amiga.libcall(base=self.amiga.peek32(4), lvo=lvo, result="d0", **regs)

    def cmd_dread(self, args):
        """dread dev unit block count file  read blocks from an exec device to a host file"""
        if len(args) != 5:
            raise ValueError("usage: dread device unit block count file")
        device, unit, block, count = args[0], parse_hex(args[1]), parse_hex(args[2]), parse_hex(args[3])
        if count <= 0:
            raise ValueError("count must be > 0")
        data = self.device_io(CMD_READ, device, unit, block, count)
        if data is None:
            return
        with open(args[4], "wb") as f:
            f.write(data)
        print(f"Wrote {len(data)} bytes to {args[4]}")

    def cmd_dwrite(self, args):
        """dwrite dev unit block file  write a host file (multiple of 512 bytes) to device blocks"""
        if len(args) != 4:
            raise ValueError("usage: dwrite device unit block file")
        device, unit, block = args[0], parse_hex(args[1]), parse_hex(args[2])
        try:
            with open(args[3], "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"Can't read {args[3]}: {e.strerror}")
            return
        if not data or len(data) % BLOCK_SIZE:
            raise ValueError(f"file is {len(data)} bytes; it must be a non-zero multiple of {BLOCK_SIZE}")
        count = len(data) // BLOCK_SIZE
        if sys.stdin.isatty():
            answer = input(f"Overwrite {count} block(s) at ${block:X} on {device} unit {unit}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
        self.device_io(CMD_WRITE, device, unit, block, count, data)

    def device_io(self, command, device, unit, block, count, data=None):
        """CMD_READ/CMD_WRITE count blocks at block through exec's DoIO. Returns read data."""
        what = "dread" if command == CMD_READ else "dwrite"
        if (block + count) * BLOCK_SIZE > 0xFFFFFFFF:
            raise ValueError("beyond 4 GB; 64-bit (TD64/NSD) commands aren't supported")
        if not self.system_callable(what):
            return None
        saved = self.begin_calls()
        port = ioreq = mem = 0
        opened = False
        chunk = IO_CHUNK_BLOCKS * BLOCK_SIZE
        name = device.encode("latin-1") + b"\0"
        memsize = chunk + ((len(name) + 3) & ~3)
        result = bytearray()
        try:
            port = self.exec_call(LVO_CREATEMSGPORT)
            if port:
                ioreq = self.exec_call(LVO_CREATEIOREQUEST, a0=port, d0=IOSTDREQ_SIZE)
            mem = self.exec_call(LVO_ALLOCMEM, d0=memsize, d1=MEMF_PUBLIC | MEMF_CLEAR)
            if not (port and ioreq and mem):
                print("Out of memory setting up the I/O request.")
                return None
            buf, name_addr = mem, mem + chunk
            self.amiga.writemem(name_addr, name)
            # OpenDevice returns io_Error sign-extended in D0
            err = int.from_bytes(self.exec_call(LVO_OPENDEVICE, a0=name_addr, d0=unit, a1=ioreq, d1=0)
                                 .to_bytes(4, "big"), "big", signed=True)
            if err:
                print(f"OpenDevice(\"{device}\", {unit}) failed: {io_error_name(err)}")
                return None
            opened = True
            done = 0
            start = time.monotonic()
            while done < count:
                n = min(IO_CHUNK_BLOCKS, count - done)
                length = n * BLOCK_SIZE
                if command == CMD_WRITE:
                    self.amiga.writemem(buf, data[done * BLOCK_SIZE:done * BLOCK_SIZE + length])
                self.amiga.poke16(ioreq + IO_COMMAND, command)
                self.amiga.poke8(ioreq + IO_FLAGS, 0)
                self.amiga.poke32(ioreq + IO_LENGTH, length)
                self.amiga.poke32(ioreq + IO_DATA, buf)
                self.amiga.poke32(ioreq + IO_OFFSET, (block + done) * BLOCK_SIZE)
                self.exec_call(LVO_DOIO, a1=ioreq)
                error = int.from_bytes(bytes([self.amiga.peek8(ioreq + IO_ERROR)]), "big", signed=True)
                actual = self.amiga.peek32(ioreq + IO_ACTUAL)
                if error or actual != length:
                    print(f"{'CMD_READ' if command == CMD_READ else 'CMD_WRITE'} at block ${block + done:X} "
                          f"failed: {io_error_name(error)}, io_Actual {actual} of {length}")
                    return None
                if command == CMD_READ:
                    result += self.amiga.readmem(buf, length)
                done += n
            verb = "Read" if command == CMD_READ else "Wrote"
            print(f"{verb} {count} block(s) at ${block:X} on {device} unit {unit} "
                  f"in {time.monotonic() - start:.1f}s")
            return bytes(result)
        finally:
            if opened:
                self.exec_call(LVO_CLOSEDEVICE, a1=ioreq)
            if ioreq:
                self.exec_call(LVO_DELETEIOREQUEST, a0=ioreq)
            if port:
                self.exec_call(LVO_DELETEMSGPORT, a0=port)
            if mem:
                self.exec_call(LVO_FREEMEM, a1=mem, d0=memsize)
            self.end_calls(saved)

    def cmd_get(self, args):
        """get amigapath file   copy a file from the Amiga to the host (via dos.library)"""
        if len(args) != 2:
            raise ValueError("usage: get amigapath hostfile")
        data = self.dos_transfer(args[0])
        if data is not None:
            with open(args[1], "wb") as f:
                f.write(data)
            print(f"Saved {len(data)} bytes to {args[1]}")

    def cmd_put(self, args):
        """put file amigapath   copy a host file to the Amiga (via dos.library)"""
        if len(args) != 2:
            raise ValueError("usage: put hostfile amigapath")
        try:
            with open(args[0], "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"Can't read {args[0]}: {e.strerror}")
            return
        self.dos_transfer(args[1], data)

    @contextlib.contextmanager
    def dos_session(self, what, path):
        """Set up for dos.library calls from the Debug() caller; yields None if not possible.

        Yields an object with dos(lvo, **regs) -> D0, buf (DOS_CHUNK bytes of
        Amiga memory) and path (the address of path as a C string). Cleans up
        everything, including the saved registers, on exit.
        """
        if not self.system_callable(what):
            yield None
            return
        execbase = self.amiga.peek32(4)
        proc = self.amiga.peek32(execbase + EXEC_THISTASK)
        if self.amiga.peek8(proc + LN_TYPE) != NT_PROCESS:
            print(f"Refusing {what}: Debug() was called from a plain task; dos.library needs a process.")
            yield None
            return
        saved = self.begin_calls()
        old_window = self.amiga.peek32(proc + PR_WINDOWPTR)
        dosbase = mem = 0
        pathname = path.encode("latin-1") + b"\0"
        memsize = DOS_CHUNK + 16 + len(DEBUG_STUB) + ((len(pathname) + 3) & ~3)
        try:
            mem = self.exec_call(LVO_ALLOCMEM, d0=memsize, d1=MEMF_PUBLIC | MEMF_CLEAR)
            if not mem:
                print("Out of memory.")
                yield None
                return
            dosname_addr = mem + DOS_CHUNK
            stub = dosname_addr + 16
            path_addr = stub + len(DEBUG_STUB)
            self.amiga.writemem(dosname_addr, b"dos.library\0")
            self.amiga.writemem(path_addr, pathname)
            self.amiga.writemem(stub, DEBUG_STUB)
            self.exec_call(LVO_CACHECLEARU)
            dosbase = self.exec_call(LVO_OPENLIBRARY, a1=dosname_addr, d0=36)
            if not dosbase:
                print("Can't open dos.library V36+.")
                yield None
                return
            # No "insert volume" requesters while we drive the machine from here
            self.amiga.poke32(proc + PR_WINDOWPTR, 0xFFFFFFFF)
            yield types.SimpleNamespace(
                dos=lambda lvo, **regs: self.lib_call(dosbase, lvo, stub, **regs),
                buf=mem, path=path_addr)
        finally:
            self.amiga.poke32(proc + PR_WINDOWPTR, old_window)
            if dosbase:
                self.exec_call(LVO_CLOSELIBRARY, a1=dosbase)
            if mem:
                self.exec_call(LVO_FREEMEM, a1=mem, d0=memsize)
            self.end_calls(saved)

    def dos_transfer(self, path, data=None):
        """Read the Amiga file at path (data None, returns its contents) or write data to it."""
        with self.dos_session("get" if data is None else "put", path) as d:
            if d is None:
                return None
            if data is not None and sys.stdin.isatty():
                lock = d.dos(LVO_LOCK, d1=d.path, d2=SHARED_LOCK & 0xFFFFFFFF)
                if lock:
                    d.dos(LVO_UNLOCK, d1=lock)
                    if input(f"{path} exists. Overwrite? [y/N] ").strip().lower() not in ("y", "yes"):
                        print("Cancelled.")
                        return None
            fh = d.dos(LVO_OPEN, d1=d.path, d2=MODE_OLDFILE if data is None else MODE_NEWFILE)
            if not fh:
                print(f"Can't open {path}: {dos_error_name(d.dos(LVO_IOERR))}")
                return None
            try:
                start = time.monotonic()
                result = bytearray()
                if data is None:
                    while True:
                        n = d.dos(LVO_READ, d1=fh, d2=d.buf, d3=DOS_CHUNK)
                        if n == 0xFFFFFFFF:
                            print(f"Read error after {len(result)} bytes: {dos_error_name(d.dos(LVO_IOERR))}")
                            return None
                        if n == 0:
                            break
                        result += self.amiga.readmem(d.buf, n)
                else:
                    for off in range(0, len(data), DOS_CHUNK):
                        piece = data[off:off + DOS_CHUNK]
                        self.amiga.writemem(d.buf, piece)
                        n = d.dos(LVO_WRITE, d1=fh, d2=d.buf, d3=len(piece))
                        if n != len(piece):
                            print(f"Write error after {off} bytes: {dos_error_name(d.dos(LVO_IOERR))}")
                            return None
                size = len(result) if data is None else len(data)
                print(f"{'Read' if data is None else 'Wrote'} {size} bytes "
                      f"{'from' if data is None else 'to'} {path} in {time.monotonic() - start:.1f}s")
                return bytes(result)
            finally:
                d.dos(LVO_CLOSE, d1=fh)

    def cmd_ls(self, args):
        """ls [amigapath]  list a directory on the Amiga (default: the caller's current dir)"""
        path = " ".join(args)
        with self.dos_session("ls", path) as d:
            if d is None:
                return
            lock = d.dos(LVO_LOCK, d1=d.path, d2=SHARED_LOCK & 0xFFFFFFFF)
            if not lock:
                print(f"Can't lock {path or 'current directory'}: {dos_error_name(d.dos(LVO_IOERR))}")
                return
            try:
                fib = d.buf  # AllocMem memory, so longword aligned as Examine() requires
                if not d.dos(LVO_EXAMINE, d1=lock, d2=fib):
                    print(f"Examine failed: {dos_error_name(d.dos(LVO_IOERR))}")
                    return
                entry = parse_fib(self.amiga.readmem(fib, FIB_READ))
                if entry["type"] < 0:  # a file: just show it
                    print(format_fib(entry))
                    return
                print(f"Directory of {path or entry['name'] + ' (current)'}")
                entries = []
                while d.dos(LVO_EXNEXT, d1=lock, d2=fib):
                    entries.append(parse_fib(self.amiga.readmem(fib, FIB_READ)))
                err = d.dos(LVO_IOERR)
                # ExNext returns entries in hash order; sort for readability
                for entry in sorted(entries, key=lambda e: e["name"].lower()):
                    print(format_fib(entry))
                if err != ERROR_NO_MORE_ENTRIES:
                    print(f"Listing stopped early: {dos_error_name(err)}")
                files = [e for e in entries if e["type"] < 0]
                print(f"{len(files)} file(s), {len(entries) - len(files)} dir(s), "
                      f"{sum(e['size'] for e in files)} bytes")
            finally:
                d.dos(LVO_UNLOCK, d1=lock)

    def cmd_reboot(self, args):
        """reboot          reboot the Amiga (ends the session)"""
        self.amiga.reboot()
        self.left_in_sad = False
        print("Amiga rebooting.")
        return False

    def cmd_resume(self, args):
        """resume          leave SAD and let the Amiga carry on (ends the session)"""
        if self.amiga.entry == "Crash":
            print("Can't resume from a crash; use reboot.")
            return
        self.amiga.resume()
        self.left_in_sad = False
        print("Amiga resumed.")
        return False

    def cmd_help(self, args):
        """?               show this help"""
        for doc in dict.fromkeys(f.__doc__ for f in self.commands.values()):
            print("  " + doc)

    def cmd_quit(self, args):
        """q               quit monitor (Amiga stays in SAD)"""
        return False

    commands = {
        "m": cmd_memory,
        "r": cmd_registers,
        "wb": cmd_write_byte,
        "ww": cmd_write_word,
        "wl": cmd_write_long,
        "upload": cmd_upload,
        "t": cmd_tasks,
        "task": cmd_task,
        "l": cmd_libraries,
        "d": cmd_devices,
        "i": cmd_interrupts,
        "a": cmd_autoconfig,
        "f": cmd_filesystems,
        "p": cmd_ports,
        "s": cmd_semaphores,
        "mem": cmd_memlist,
        "res": cmd_residents,
        "dos": cmd_dos,
        "kick": cmd_kick,
        "dread": cmd_dread,
        "dwrite": cmd_dwrite,
        "get": cmd_get,
        "put": cmd_put,
        "ls": cmd_ls,
        "reboot": cmd_reboot,
        "resume": cmd_resume,
        "?": cmd_help,
        "h": cmd_help,
        "help": cmd_help,
        "q": cmd_quit,
        "quit": cmd_quit,
    }


def main():
    parser = argparse.ArgumentParser(description="Report Amiga gurus via SAD over serial.")
    parser.add_argument("port", help="serial device, e.g. /dev/cu.usbserial-XXXX")
    parser.add_argument("--pyamigadebug", default=os.environ.get("PYAMIGADEBUG", DEFAULT_PYAMIGADEBUG),
                        help="path to pyamigadebug checkout (default: ../pyamigadebug or $PYAMIGADEBUG)")
    parser.add_argument("--interval", type=float, default=0.3, help="seconds between DEL probes")
    parser.add_argument("--debug", action="store_true", help="verbose pyamigadebug output")
    parser.add_argument("--attach", action="store_true",
                        help="don't wait for a crash; connect to SAD entered via Debug() or NMI")
    args = parser.parse_args()

    sys.path.insert(0, os.path.abspath(args.pyamigadebug))
    from SAD import SAD

    # SAD talks at 9600 baud; pyamigadebug sets this too, but the DEL probe goes out first.
    with serial.Serial(args.port, 9600, timeout=None, exclusive=True) as ser:
        if args.attach:
            print("Waiting for SAD (enter it with Debug(), NMI or the Workbench debug menu)...")
        else:
            wait_for_crash(ser, args.interval)
        amiga = SAD(serial=ser, Debug=args.debug)
        guru_task, alert = report_guru(amiga)
        monitor = Monitor(amiga, guru_task, alert)
        monitor.run()
        if monitor.left_in_sad:
            print("Amiga left in SAD.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
