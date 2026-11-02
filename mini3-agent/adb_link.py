"""One owned persistent Mini3 shell; sent commands are never retried.

The device supports one active shell. Keep its task and USB buffers alive,
and delimit responses with the existing version command.
"""
import os
from pathlib import Path
import re
import subprocess
import threading
import time


class ADBLinkError(RuntimeError):
    pass


class ADBInUse(RuntimeError):
    pass


class Lease:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        if self.file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b', buffering=0)
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise ADBInUse('Mini3 USB is owned by another process; stop its service before diagnostics') from None
        self.file = stream

    def close(self):
        if self.file is None:
            return
        stream, self.file = self.file, None
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


CSI = rb'\x1b\[[0-?]*[ -/]*[@-~]'
PREFIX = rb'(?:' + CSI + rb'|(?:ListenAI|boot):/\$[ \t]*)*'
VERSION = re.compile(rb'(?:^|[\r\n])' + PREFIX +
                     rb'(\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9_.-]+)?)'
                     rb'(?:' + CSI + rb')*\r*\n')
ANSI = re.compile(CSI)


def split_frame(data, version=None):
    pattern = VERSION if version is None else re.compile(
        rb'(?:^|[\r\n])' + PREFIX + re.escape(version.encode('ascii')) +
        rb'(?:' + CSI + rb')*\r*\n')
    match = pattern.search(data)
    if match is None:
        return None
    found = match.group(1).decode('ascii') if version is None else version
    return bytes(data[:match.start()]), match.end(), found


def clean_response(raw):
    text = ANSI.sub(b'', raw).decode('utf-8', errors='replace')
    lines = []
    for line in text.split('\n'):
        # A log redraw can place the prompt before a real reply. Preserve the
        # reply; dropping the entire prompt-prefixed line loses valid ACKs.
        line = line.rstrip('\r').rsplit('\r', 1)[-1]
        line = re.sub(r'^(?:(?:ListenAI|boot):/\$[ \t]*)+', '', line)
        if line and not line.startswith(('local_agent ', 'wakeup_uac_record ', 'version')):
            lines.append(line)
    return '\n'.join(lines) + '\n'


def select_device(listing, requested=None):
    rows = [line.split() for line in listing.splitlines()]
    if requested:
        matches = [row for row in rows if len(row) >= 2 and row[0] == requested]
    else:
        labels = {'model:listenai', 'model:arcs_mini3', 'model:arcs-mini3'}
        matches = [row for row in rows if len(row) >= 2 and not row[0].startswith('BOOT-')
                   and labels.intersection(row[2:])]
    if len(matches) > 1:
        raise ADBInUse('Multiple compatible devices; set MINI3_ADB_SERIAL explicitly')
    if not matches or matches[0][1] != 'device':
        raise ADBLinkError('Mini3 ADB is not online; wait for USB reconnect')
    return matches[0][0]


class Mini3Link:
    def __init__(self, adb, lease_path, serial=None, timeout=5):
        self.adb, self.serial, self.timeout = str(adb), serial or os.environ.get('MINI3_ADB_SERIAL'), timeout
        self._selected_serial = None
        self.lease = Lease(lease_path)
        self._lock = threading.RLock()
        self._condition = threading.Condition()
        self._process = self._reader = None
        self._generation = 0
        self._rx = bytearray()
        self._recent = bytearray()
        self._version = None
        self.last_exchange = {}
        self._eof = self._overflow = self._reading = False
        self.stats = {'sessions': 0, 'commands': 0, 'disconnects': 0, 'timeouts': 0,
                      'max_roundtrip_ms': 0}

    def _read(self, process, generation):
        try:
            while True:
                chunk = process.stdout.read(4096)
                with self._condition:
                    if generation != self._generation:
                        return
                    if not chunk:
                        self._eof = True
                        self._condition.notify_all()
                        return
                    self._rx.extend(chunk)
                    self._recent.extend(chunk)
                    del self._recent[:-32768]
                    if len(self._rx) > 65536:
                        if self._reading:
                            self._overflow = True
                        del self._rx[:-32768]
                    self._condition.notify_all()
        except (OSError, ValueError):
            with self._condition:
                if generation == self._generation:
                    self._eof = True
                    self._condition.notify_all()

    def _collect(self, version, deadline):
        with self._condition:
            while True:
                if self._overflow:
                    raise ADBLinkError('Mini3 USB response overflow; command result is unconfirmed')
                frame = split_frame(self._rx, version)
                if frame:
                    raw, consumed, found = frame
                    del self._rx[:consumed]
                    return raw, found
                if self._eof:
                    raise ADBLinkError('Mini3 USB shell closed; wait for reconnect')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stats['timeouts'] += 1
                    raise ADBLinkError('Mini3 USB response timed out; command was not resent')
                self._condition.wait(remaining)

    def _open(self, deadline):
        if self._process is not None and not self._eof and self._process.poll() is None:
            return
        self.disconnect()
        try:
            devices = subprocess.run([self.adb, 'devices', '-l'], capture_output=True,
                                     timeout=self.timeout, encoding='utf-8', errors='replace')
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ADBLinkError('Mini3 ADB enumeration unavailable') from exc
        self._selected_serial = select_device(devices.stdout, self.serial or self._selected_serial)
        with self._condition:
            self._generation += 1
            generation = self._generation
            self._rx.clear()
            self._eof = self._overflow = False
        self._process = subprocess.Popen([self.adb, '-s', self._selected_serial, 'shell', '-x'],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, bufsize=0,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.stats['sessions'] += 1
        self._reader = threading.Thread(target=self._read, args=(self._process, generation), daemon=True)
        self._reader.start()
        self._process.stdin.write(b'version\r\n')
        self._process.stdin.flush()
        _, self._version = self._collect(None, deadline)

    def execute(self, command):
        if not command or any(char in command for char in '\r\n\0;'):
            raise ValueError('Only one framed Mini3 command is allowed')
        if len(command.encode('utf-8')) > 512:
            raise ValueError('Mini3 command exceeds the shell input budget')
        with self._lock:
            self.lease.acquire()
            deadline = time.monotonic() + self.timeout
            started = time.monotonic()
            with self._condition:
                self._reading = True
            try:
                self._open(deadline)
                if command == 'version':
                    return self._version + '\n'
                with self._condition:
                    self._rx.clear()
                self._process.stdin.write((command + '\r\nversion\r\n').encode('utf-8'))
                self._process.stdin.flush()
                self.stats['commands'] += 1
                raw, _ = self._collect(self._version, deadline)
                self.stats['max_roundtrip_ms'] = max(self.stats['max_roundtrip_ms'],
                                                    round((time.monotonic() - started) * 1000))
                response = clean_response(raw)
                self.last_exchange = {'command': command, 'raw': raw.decode('utf-8', errors='replace'),
                                      'response': response}
                return response
            except (ADBLinkError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self.disconnect()
                if isinstance(exc, ADBLinkError):
                    raise
                raise ADBLinkError('Mini3 USB session failed; command was not resent') from exc
            finally:
                with self._condition:
                    self._reading = False

    def disconnect(self):
        with self._lock:
            process, reader = self._process, self._reader
            self._process = self._reader = None
            self._version = None
            with self._condition:
                self._generation += 1
                self._rx.clear()
                self._eof = True
                self._condition.notify_all()
            if process is not None:
                self.stats['disconnects'] += 1
                try:
                    if process.poll() is None:
                        try:
                            process.stdin.write(b'\x03'); process.stdin.flush()
                            process.wait(timeout=.8)
                        except (OSError, subprocess.TimeoutExpired):
                            process.terminate(); process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=2)
                except OSError:
                    pass
                if reader is not None and reader is not threading.current_thread():
                    reader.join(timeout=.5)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try: stream.close()
                        except OSError: pass

    def close(self):
        with self._lock:
            try:
                self.disconnect()
            finally:
                self.lease.close()

    def diagnostic_tail(self):
        with self._condition:
            return self._recent.decode('utf-8', errors='replace')

    @property
    def connected(self):
        with self._condition:
            return self._process is not None and not self._eof and self._version is not None
