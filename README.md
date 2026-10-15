# pyamigamon

A crash monitor and debugger for Amigas running Kickstart 3.x, driven from
another computer over the serial port.

It uses SAD, the debugger built into the Kickstart 3.x ROM, through
[pyamigadebug](https://github.com/rvalles/pyamigadebug), so nothing needs to be
installed on the Amiga. When the Amiga gurus, pyamigamon catches it before it
reboots and reports the alert, the crashing task and (where it survived) the
CPU exception frame with the faulting PC. It then drops into an interactive
monitor where you can inspect memory, tasks, libraries, devices, interrupts,
memory lists and more. It was written for debugging drivers, where a crash
usually takes out everything you'd normally use to investigate.

## Requirements

- An Amiga with Kickstart 3.x (V39+; SAD first appeared in V39).
- A serial connection to the Amiga's serial port (null-modem cable, or an
  emulator's serial port; see [Testing with Copperline](#testing-with-copperline)).
  SAD always runs at 9600 baud.
- Python 3.8+ and [pyserial](https://pypi.org/project/pyserial/) (`pip install pyserial`).
- A checkout of [pyamigadebug](https://github.com/rvalles/pyamigadebug). By
  default pyamigamon looks for it next to this repository (`../pyamigadebug`);
  use `--pyamigadebug PATH` or `$PYAMIGADEBUG` to point elsewhere.

## Usage

```
python3 pyamigamon.py [--attach] [--interval SECS] [--debug] [--pyamigadebug PATH] PORT
```

| Option | |
|---|---|
| `PORT` | Serial device, e.g. `/dev/cu.usbserial-XXXX` or an emulator's pty |
| `--attach` | Don't wait for a crash; connect to SAD entered via `Debug()` or NMI |
| `--interval` | Seconds between DEL probes while waiting for a crash (default 0.3) |
| `--debug` | Verbose pyamigadebug output |
| `--pyamigadebug` | Path to the pyamigadebug checkout |

### Catching a crash

```
python3 pyamigamon.py /dev/cu.usbserial-XXXX
```

When a dead-end alert happens, Kickstart 3.x blinks the power LED for a few
seconds before rebooting, and a DEL character on the serial port during that
window drops it into SAD. pyamigamon keeps sending DEL until the Amiga answers,
so start it before the crash and leave it waiting.

```
*** GURU MEDITATION ***
  Alert: #8000.0005  ($80000005)
  Task:  $4000BE60
  SAD entry PC: $00F80440 (inside exec's alert handling)
  Exception: Zero divide (vector 5, format $2) in supervisor mode
  Fault PC:  $400308C4
  Fault SR:  $2000 (S IPL0 -----)
  Instruction address: $400308C2
  Frame at $40002274, stack before exception $40002280
Monitor ready. Type ? for help. Up/down for history, tab completes commands.
>
```

**About the exception frame.** For a CPU exception that happened in
*supervisor* mode (interrupt code, `Supervisor()`, exec internals), the
crash's own exception frame sits right after SAD's, and pyamigamon decodes it:
the faulting PC and SR, plus the fault address, SSW etc. for bus and address
errors (68000 group 0 frames and 68010–68060 formats $0–$C). It only accepts a
frame whose vector matches the alert number.

For an exception in *user* mode (most task code, including a device's
`BeginIO` and unit tasks), exec first shows the "Software Failure"
requester. Choosing Reboot re-raises the alert from the requester's own code,
and the original frame is gone; pyamigamon says so rather than guessing.

### Using it as a general monitor

Put the Amiga into SAD on a healthy system and connect with `--attach`:

```
python3 pyamigamon.py --attach /dev/cu.usbserial-XXXX
```

Ways to enter SAD:

- Call `exec.library/Debug()`. A three-instruction program is enough, and
  calling `Debug()` from your own code works as a breakpoint:
  ```
          move.l  4.w,a6
          jsr     -114(a6)        ; Debug()
          rts
  ```
- Start Workbench with `LoadWB -debug` and use the hidden debug menu
  (to the right of the normal menus).
- An NMI, if your hardware can generate one.

`resume` hands control back; all registers are restored, so the caller of
`Debug()` returns normally.

## Commands

Numbers are hex by default; `$` and `0x` prefixes are accepted. Command
history is kept in `~/.pyamigamon_history`, and tab completes command names.

### Registers and memory

| Command | |
|---|---|
| `r` | Registers saved on SAD entry (D0–D7, A0–A6, USP, SSP, SR, PC, VBR, CPU) and the crash's exception frame |
| `m [addr] [len]` | Hex/ASCII dump, default `$80` bytes; `m` alone continues where the last one ended |
| `wb addr val ...` | Write bytes |
| `ww addr val ...` | Write words (even address) |
| `wl addr val ...` | Write longs (even address) |
| `upload file addr` | Copy a host file into Amiga memory in one transfer |

`wb`/`ww`/`wl` do one correctly sized access per value, so they are safe on
custom chip and CIA registers (e.g. `ww DFF180 0F00` sets COLOR00). They don't
read back, because reading some registers has side effects. `upload` is a bulk
transfer and is much faster (roughly 10 KB in under 30 s), but it should only
be used for RAM.

### System lists

| Command | |
|---|---|
| `t` | Tasks: running, ready and waiting, with priority, state and wait mask; `*` marks the task that caused the guru, and shell processes show the command they're running |
| `task [name\|addr]` | One task in detail (see below) |
| `l` | Libraries, with version and open count |
| `d` | Devices, with version and open count |
| `i` | Interrupt vectors: enable/request state from INTENA/INTREQ, handlers, and every server on the shared chains |
| `a` | Autoconfig (Zorro) boards: address, size, manufacturer/product, flags and bound driver |
| `f` | Filesystems registered in FileSystem.resource |
| `p` | Public message ports, with queued message counts and the task they signal |
| `s` | Public semaphores, their owner, and tasks waiting on them |
| `mem [v]` | Memory regions with free/largest block, checking each free list for corruption; `v` lists every chunk |
| `res` | Resident modules (ROM and KickTags) in init order |
| `dos` | DOS devices (with device/unit, geometry, MaxTransfer, Mask and DosType), volumes and assigns |

The list walkers expect to run on a crashed machine: a bad pointer or an
over-long list stops the walk with a warning instead of hanging, and a bad
memory read prints an error and leaves the session running.

`task` shows every field of the Task structure (signals as bit lists, trap and
exception handlers, nesting counts), checks the stack pointer against the
stack bounds, and for a process adds its message port, SegList, current
directory, file handles and CLI details. For a task that isn't running, it
decodes the registers exec saved when switching it out, including the PC and
the first values on its stack, which show where it called `Wait()` from:

```
  Saved     PC $00F80C0C  SR $0018 (U IPL0 XN---)  (where it was switched out)
            D0 400009B8  D1 000000FF  D2 00000001  D3 40000810
  ...
  Stack top 00F828DA 40017636 00000000 ...
```

### KickTags

| Command | |
|---|---|
| `kick` | Show `KickMemPtr`, `KickTagPtr`, `KickCheckSum` and their entries |
| `kick file [chip]` | Load a resident module into the KickTags so it starts at the next reboot |
| `kick clear` | Clear the KickTags (takes effect at the next reboot) |

`kick file` loads an Amiga executable, relocates it into a single
`AllocMem(MEMF_KICK)` block (or chip RAM with `chip`), finds its ROM tags,
chains them onto any existing KickMem/KickTag entries and sets `KickCheckSum`
using exec's `SumKickData()`. Because it calls exec functions it needs a
healthy machine: it has the same requirements as `dread`/`dwrite` (see
[Device I/O](#device-io)), so use `--attach`.

A module started from the KickTags has no expansion.library binding, so
drivers that call `GetCurrentBinding()` to find their board won't work this
way.

### Device I/O

| Command | |
|---|---|
| `dread dev unit block count file` | Read `count` 512-byte blocks starting at `block` into a host file |
| `dwrite dev unit block file` | Write a host file (a multiple of 512 bytes) to blocks starting at `block` |

For example `dread lide.device 0 0 4 rdb.bin` reads the first four blocks
(the RDB) of unit 0. Unit, block and count are hex like everything else.
`dwrite` asks for confirmation when run interactively.

These open the device and use `CMD_READ`/`CMD_WRITE` through `DoIO()`, in
8 KB chunks, so they exercise the driver exactly as the system would.
Offsets are 32-bit, so only the first 4 GB of a unit can be reached.

Like `kick`, they call exec functions, so they only run when that's safe:
SAD entered via `Debug()` (not NMI or a crash), called from user mode, not
inside `Disable()`, with a valid running task. Otherwise they refuse and say
why. The running system keeps going while the I/O is in progress, which is
how the device's interrupts and unit task get to complete the request.

### File transfer

| Command | |
|---|---|
| `get amigapath file` | Copy a file from the Amiga to the host |
| `put file amigapath` | Copy a host file to the Amiga |
| `ls [amigapath]` | List a directory (default: the caller's current directory): name, size, protection bits, date and comment |

These use dos.library (`Open`/`Read`/`Write`/`Close`) from the process that
called `Debug()`, so they work on any filesystem the Amiga can see, e.g.
`get DH0:S/Startup-Sequence startup.txt` or `put driver.device RAM:`. They
have the same requirements as device I/O, and additionally the caller must
be a DOS process (a shell command, for example), not a plain task. "Insert
volume" requesters are suppressed during the transfer, so a missing volume
returns an error instead. `put` asks before overwriting when run
interactively. Expect roughly 1 KB/s at 9600 baud. `ls` sorts entries by
name and shows dates as the Amiga stores them (local time, no time zone).

### Session

| Command | |
|---|---|
| `reboot` | Reboot the Amiga and end the session |
| `resume` | Return to the system and end the session (not possible after a crash) |
| `?` | Help |
| `q` | Quit, leaving the Amiga in SAD |

## Testing with Copperline

The [Copperline](https://copperline.dev/) emulator can put the Amiga's serial
port on a pseudo-terminal. Add this to a config:

```toml
[serial]
mode = "pty"
```

or pass `--serial pty`. Copperline logs the device it created, which can
change on every launch:

```
INFO copperline::serial] serial: pty at /dev/ttys005 ...
```

Then run `python3 pyamigamon.py /dev/ttys005`, or add `--attach`. To get test
programs onto the Amiga, either boot straight into one with
`copperline --config ... --run PROGRAM` or mount a host folder with a
`[[filesys]]` entry.

Things to know:

- With a control client attached (`--control`/`--control-gui`), Copperline
  pauses when a program loads. Resume it with
  `copperline-ctl --info FILE continue`.
- `copperline-ctl ... capture.screenshot '{"path": "..."}'` captures one row
  per scanline, so double the height for the real aspect ratio.
- At the time of writing, Copperline builds some exception frames
  differently from real CPUs: 68000 bus/address error frames come out with
  their fields in reverse order, and 68020/030 address errors use format $0
  instead of $A/$B. Don't use those cases to check frame decoding.

## Limitations

- Kickstart 3.x (SAD) only. Kickstart 1.x/2.x use RomWack, which pyamigadebug
  supports but pyamigamon doesn't.
- The serial link runs at 9600 baud, so large lists and memory dumps take a
  few seconds.
- After a guru, the system's own structures may be corrupt; treat what the
  monitor shows as evidence, not as guaranteed truth.
- A task that was using the FPU when it was switched out has a larger saved
  state whose layout isn't decoded; `task` says so instead of showing
  registers.

## Acknowledgements

pyamigamon is built on top of
[pyamigadebug](https://github.com/rvalles/pyamigadebug) by
[Roc Vallès i Domènech](https://rvalles.net/). Its implementation of the SAD
protocol, and of calling Amiga system functions through it, is what makes
pyamigamon possible. pyamigadebug also powers
[amigaXfer](https://github.com/rvalles/pyamigadebug), and its approach to
safely driving a live system from SAD (such as disabling interrupts while
in control) informed several design decisions here. Many thanks to Roc for
creating it and releasing it as open source.

## License

Copyright (C) 2026 Matthew Harlum

pyamigamon is licensed under the GNU General Public License, version 2 only
(SPDX: `GPL-2.0-only`). See [LICENSE](LICENSE).
