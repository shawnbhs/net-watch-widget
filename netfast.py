"""Fast local network-liveness signals for Windows.

The desktop widget this supports used to learn about a dropped link only after a
monolithic refresh cycle joined six workers, the slowest of which were two HTTP
public-IP lookups with six second timeouts and a `ping -n 2` guarded by a fifteen
second subprocess timeout. Every one of those blocks for its full timeout exactly
when the network is down, so the user waited 15-20 seconds to be told something
he already suspected.

Everything here is deliberately local and cheap: the routing table and the LAN
gateway can answer "is there a path off this machine" in microseconds to a few
hundred milliseconds, with no DNS, no TCP handshake, and no process spawn. The
expensive public-IP enrichment is expected to run separately and asynchronously;
this module only tells the caller when it is worth kicking that work off.

Pure standard library plus ctypes. No third-party dependencies.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import socket
import struct
import subprocess
import threading
import time

__all__ = [
    "link_state",
    "gateway_alive",
    "should_refresh_now",
    "NetWatcher",
]

# Any routable, non-local destination works: the UDP connect() below never emits
# a packet, so the address is only a question put to the routing table and is
# never contacted. 8.8.8.8 is used purely because it is guaranteed to be a public
# unicast address that no sane routing table treats as on-link.
_PROBE_DEST = "8.8.8.8"
_PROBE_PORT = 53

_ERROR_SUCCESS = 0
_IP_SUCCESS = 0

# Measured on Windows 11: IcmpSendEcho rounds its timeout down to a 500 ms
# granularity and never returns sooner, so anything below this must be bounded by
# the caller rather than by the API.
_ICMP_TIMEOUT_FLOOR_MS = 500

_iphlpapi = None
_iphlpapi_error: str | None = None
try:
    _iphlpapi = ctypes.WinDLL("iphlpapi.dll")
except OSError as exc:  # pragma: no cover - only on a broken Windows install
    _iphlpapi_error = str(exc)


class _MIB_IPFORWARDROW(ctypes.Structure):
    """Layout of MIB_IPFORWARDROW as consumed by GetBestRoute."""

    _fields_ = [
        ("dwForwardDest", wintypes.DWORD),
        ("dwForwardMask", wintypes.DWORD),
        ("dwForwardPolicy", wintypes.DWORD),
        ("dwForwardNextHop", wintypes.DWORD),
        ("dwForwardIfIndex", wintypes.DWORD),
        ("dwForwardType", wintypes.DWORD),
        ("dwForwardProto", wintypes.DWORD),
        ("dwForwardAge", wintypes.DWORD),
        ("dwForwardNextHopAS", wintypes.DWORD),
        ("dwForwardMetric1", wintypes.DWORD),
        ("dwForwardMetric2", wintypes.DWORD),
        ("dwForwardMetric3", wintypes.DWORD),
        ("dwForwardMetric4", wintypes.DWORD),
        ("dwForwardMetric5", wintypes.DWORD),
    ]


class _IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ("Address", wintypes.DWORD),
        ("Status", wintypes.DWORD),
        ("RoundTripTime", wintypes.DWORD),
        ("DataSize", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("Data", ctypes.c_void_p),
        ("Options", _IP_OPTION_INFORMATION),
    ]


def _declare_prototypes() -> None:
    """Pin argument and return types for every iphlpapi call used here.

    Without this ctypes assumes a C int return and int-sized arguments. On 64-bit
    Windows that silently truncates the ICMP handle from IcmpCreateFile, and
    passing the truncated value back into IcmpSendEcho dereferences a bogus
    pointer and raises an access violation. The surrounding try/except would turn
    that into a plain False, so every gateway on earth would look dead. Declaring
    the signatures is what makes gateway_alive able to report a real answer.
    """
    if _iphlpapi is None:
        return
    _iphlpapi.IcmpCreateFile.argtypes = []
    _iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
    _iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    _iphlpapi.IcmpCloseHandle.restype = wintypes.BOOL
    _iphlpapi.IcmpSendEcho.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.WORD,
        ctypes.POINTER(_IP_OPTION_INFORMATION),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _iphlpapi.IcmpSendEcho.restype = wintypes.DWORD
    _iphlpapi.GetBestRoute.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_MIB_IPFORWARDROW),
    ]
    _iphlpapi.GetBestRoute.restype = wintypes.DWORD
    _iphlpapi.ConvertInterfaceIndexToLuid.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    _iphlpapi.ConvertInterfaceIndexToLuid.restype = wintypes.DWORD
    _iphlpapi.ConvertInterfaceLuidToAlias.argtypes = [
        ctypes.POINTER(ctypes.c_uint64),
        wintypes.LPWSTR,
        ctypes.c_size_t,
    ]
    _iphlpapi.ConvertInterfaceLuidToAlias.restype = wintypes.DWORD


try:
    _declare_prototypes()
except Exception as exc:  # pragma: no cover - defensive
    _iphlpapi_error = f"prototype setup failed: {exc}"
    _iphlpapi = None


def _ipv4_from_dword(value: int) -> str:
    """Render a Windows in_addr DWORD (network byte order) as dotted quad."""
    return socket.inet_ntoa(struct.pack("<I", value & 0xFFFFFFFF))


def _dword_from_ipv4(text: str) -> int:
    """Parse a dotted quad into the network-byte-order DWORD the Win32 API wants."""
    return struct.unpack("<I", socket.inet_aton(text))[0]


def _adapter_alias(if_index: int) -> str | None:
    """Resolve an interface index to its friendly alias, e.g. "Wi-Fi".

    ConvertInterfaceIndexToLuid/ConvertInterfaceLuidToAlias are used in place of
    GetAdaptersAddresses because they answer this one question without allocating
    and walking a whole linked list of adapters, and in place of any PowerShell
    call because spawning a shell costs hundreds of milliseconds. Returns None on
    any failure so a missing cosmetic label can never break liveness detection.
    """
    if _iphlpapi is None:
        return None
    try:
        luid = ctypes.c_uint64(0)
        if _iphlpapi.ConvertInterfaceIndexToLuid(
            wintypes.ULONG(if_index), ctypes.byref(luid)
        ) != _ERROR_SUCCESS:
            return None
        buf = ctypes.create_unicode_buffer(256)
        if _iphlpapi.ConvertInterfaceLuidToAlias(
            ctypes.byref(luid), buf, ctypes.c_size_t(len(buf))
        ) != _ERROR_SUCCESS:
            return None
        return buf.value or None
    except Exception:
        return None


def _best_route(dest: str = _PROBE_DEST) -> tuple[str | None, int | None]:
    """Ask the kernel which next hop and interface index serve `dest`.

    GetBestRoute is a pure routing-table lookup, so it costs microseconds and
    needs no elevation. The gateway is reported as None when the route is on-link
    (next hop 0.0.0.0), which is what a loopback-only or point-to-point route
    looks like. Any ctypes or Win32 failure yields (None, None) rather than
    raising, leaving the caller to fall back.
    """
    if _iphlpapi is None:
        return (None, None)
    try:
        row = _MIB_IPFORWARDROW()
        rc = _iphlpapi.GetBestRoute(
            wintypes.DWORD(_dword_from_ipv4(dest)),
            wintypes.DWORD(0),
            ctypes.byref(row),
        )
        if rc != _ERROR_SUCCESS:
            return (None, None)
        next_hop = _ipv4_from_dword(row.dwForwardNextHop)
        if next_hop == "0.0.0.0":
            next_hop = None
        return (next_hop, int(row.dwForwardIfIndex))
    except Exception:
        return (None, None)


def _gateway_from_route_print() -> str | None:
    """Last-resort default-gateway parse from `route print -4`.

    This exists only for the case where iphlpapi cannot be loaded or GetBestRoute
    is unavailable. It spawns a process and therefore costs tens of milliseconds,
    which is why it is never on the normal path. `route` is preferred over
    PowerShell because starting powershell.exe alone costs several hundred
    milliseconds. Returns None on any failure, including timeout.
    """
    try:
        out = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            candidate = parts[2]
            try:
                socket.inet_aton(candidate)
            except OSError:
                continue
            return candidate
    return None


def link_state() -> dict:
    """Report whether this machine currently has a usable route off-box.

    The primary signal is a UDP socket connect() to a public address. UDP
    connect() exchanges no packets at all; it only binds the socket to whichever
    local interface the routing table says would carry the traffic. That makes it
    a microsecond-scale question that fails immediately with no route to host
    when the link drops, unlike ping or an HTTP request which both burn their
    full timeout in exactly that situation.

    Returns a dict with keys: up, local_ip, gateway, adapter, checked_ms, error.
    `up` is True only when a local source address was selected, which is the
    thing that actually goes away when the adapter loses its address or the
    default route disappears. Never raises: any unexpected failure is reported as
    up=False with the exception text in `error`, because a monitor that throws is
    worse than a monitor that says "down".
    """
    started = time.perf_counter()
    local_ip: str | None = None
    gateway: str | None = None
    adapter: str | None = None
    error: str | None = None
    up = False

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # A timeout is set even though connect() cannot block on UDP, purely
            # to guarantee this call can never wedge on an exotic LSP or filter
            # driver that hooks the winsock path.
            sock.settimeout(0.25)
            sock.connect((_PROBE_DEST, _PROBE_PORT))
            local_ip = sock.getsockname()[0]
        finally:
            sock.close()
        # 0.0.0.0 means winsock accepted the call but selected no interface,
        # which is indistinguishable from having no route for our purposes.
        if local_ip in (None, "", "0.0.0.0"):
            local_ip = None
        up = local_ip is not None
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        error = f"{type(exc).__name__}: {exc}"

    if up:
        gateway, if_index = _best_route()
        if if_index is not None:
            adapter = _adapter_alias(if_index)
        if gateway is None and if_index is None:
            gateway = _gateway_from_route_print()

    return {
        "up": up,
        "local_ip": local_ip,
        "gateway": gateway,
        "adapter": adapter,
        "checked_ms": (time.perf_counter() - started) * 1000.0,
        "error": error,
    }


def _icmp_probe(gw: str | None, timeout_ms: int) -> tuple[bool, str]:
    """Single ICMP echo returning both the verdict and why, for diagnosis.

    gateway_alive collapses this to a bool, but a bare False cannot distinguish
    "the gateway did not answer" from "the ctypes binding is broken", and that
    ambiguity is exactly how a silently truncated handle can masquerade as a dead
    network. Tests and troubleshooting use the detail string; production callers
    do not need it.
    """
    if not gw:
        return (False, "no gateway")
    if _iphlpapi is None:
        return (False, f"iphlpapi unavailable: {_iphlpapi_error}")
    try:
        dest = _dword_from_ipv4(gw)
    except OSError:
        return (False, "malformed address")

    handle = None
    try:
        handle = _iphlpapi.IcmpCreateFile()
        # IcmpCreateFile signals failure with INVALID_HANDLE_VALUE, which arrives
        # here as the all-ones pointer value rather than as a null.
        if not handle or handle == ctypes.c_void_p(-1).value:
            return (False, "IcmpCreateFile failed")

        payload = b"netfast"
        # Room for the reply header, the echoed payload, and slack for any IP
        # options the responder attaches; the API rejects a buffer that is tight.
        reply_size = ctypes.sizeof(_ICMP_ECHO_REPLY) + len(payload) + 32
        reply_buf = ctypes.create_string_buffer(reply_size)
        req_buf = ctypes.create_string_buffer(payload, len(payload))

        count = _iphlpapi.IcmpSendEcho(
            handle,
            wintypes.DWORD(dest),
            ctypes.cast(req_buf, ctypes.c_void_p),
            wintypes.WORD(len(payload)),
            None,
            ctypes.cast(reply_buf, ctypes.c_void_p),
            wintypes.DWORD(reply_size),
            wintypes.DWORD(int(timeout_ms)),
        )
        if not count:
            return (False, f"no reply (win32 error {ctypes.GetLastError()})")
        reply = ctypes.cast(reply_buf, ctypes.POINTER(_ICMP_ECHO_REPLY)).contents
        if reply.Status != _IP_SUCCESS:
            return (False, f"reply status {reply.Status}")
        return (True, f"rtt {reply.RoundTripTime} ms")
    except Exception as exc:
        return (False, f"{type(exc).__name__}: {exc}")
    finally:
        if handle:
            try:
                _iphlpapi.IcmpCloseHandle(handle)
            except Exception:
                pass


def gateway_alive(gw: str | None, timeout_ms: int = 400) -> bool:
    """Probe the LAN gateway with one ICMP echo bounded by an explicit timeout.

    The Win32 ICMP helper API is used rather than `ping.exe` for two reasons:
    ping cannot be told to wait less than roughly a second, and each call costs a
    process spawn on top of that.

    IcmpSendEcho does not fully honour short timeouts: measured on Windows 11 it
    quantises to a 500 ms floor, so a request for 150 ms still blocks for about
    500 ms. Since the point of this module is sub-500 ms reaction, budgets under
    that floor are enforced here instead, by running the blocking call on a
    throwaway daemon thread and abandoning it when the deadline passes. The
    orphaned thread costs nothing and exits on its own at the OS timeout.

    Returns False, never raises, for every failure mode: gw is None or malformed,
    iphlpapi missing, handle creation failed, the request timed out, or a reply
    arrived carrying a non-success status such as destination unreachable. Note
    that a False here is weaker evidence than link_state()'s, since plenty of
    routers drop ICMP by policy while forwarding traffic perfectly well.
    """
    if not gw:
        return False
    timeout_ms = int(timeout_ms)
    if timeout_ms >= _ICMP_TIMEOUT_FLOOR_MS:
        return _icmp_probe(gw, timeout_ms)[0]

    result: list[bool] = []

    def worker() -> None:
        try:
            result.append(_icmp_probe(gw, timeout_ms)[0])
        except Exception:
            result.append(False)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_ms / 1000.0)
    # An unfinished probe is treated as a miss: waiting longer is precisely the
    # behaviour the caller asked not to have.
    return bool(result and result[0])


def should_refresh_now(old: dict, new: dict) -> bool:
    """Decide whether a transition justifies an immediate public-IP refresh.

    The expensive enrichment (two HTTP lookups) is worth paying for only when the
    public IP plausibly moved. Coming back up after an outage and changing local
    address or gateway all mean the machine is likely on a different path than it
    was, e.g. a Wi-Fi to hotspot switch or a router-issued new lease after a
    reconnect. Going down is deliberately excluded: there is no internet to ask.

    Tolerates missing keys and non-dict inputs by returning False, since a
    spurious refresh is cheap but an exception in a monitor callback is not.
    """
    try:
        if not new.get("up"):
            return False
        if not old.get("up"):
            return True
        if old.get("local_ip") != new.get("local_ip"):
            return True
        return old.get("gateway") != new.get("gateway")
    except Exception:
        return False


def _state_key(state: dict) -> tuple:
    """Reduce a state dict to the fields whose change is worth telling anyone about."""
    return (
        bool(state.get("up")),
        state.get("local_ip"),
        state.get("gateway"),
    )


class NetWatcher:
    """Poll link_state() on a daemon thread and report only meaningful flips.

    Meaningful means up->down, down->up, a changed local IP, or a changed
    gateway; everything else, including timing jitter in checked_ms, is noise the
    UI must not see. Transitions are debounced: `debounce` consecutive samples
    must agree on the new value before it is announced, so a single dropped
    sample during, say, a Wi-Fi roam cannot flash the widget red.

    The polling thread swallows every exception, including one raised by the
    injected state function, because a monitor thread that dies silently leaves
    the UI frozen on stale data forever, which is the exact failure this module
    exists to prevent.
    """

    def __init__(self, on_change=None, interval: float = 1.0, debounce: int = 2,
                 state_fn=None):
        """Configure the watcher.

        on_change is called as on_change(old_state, new_state) from the polling
        thread; the caller is responsible for marshalling to its UI thread.
        state_fn exists so tests can feed a scripted sequence of states instead of
        touching the real network, which is the only safe way to exercise the
        down path on a live machine.
        """
        self.on_change = on_change
        self.interval = float(interval)
        self.debounce = max(1, int(debounce))
        self.state_fn = state_fn or link_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._pending: dict | None = None
        self._pending_count = 0

    @property
    def state(self) -> dict | None:
        """Most recently announced state, or None before the first sample."""
        with self._lock:
            return self._state

    def start(self) -> None:
        """Begin polling. Idempotent: a second call while running does nothing."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="NetWatcher", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Signal the thread and wait briefly for it to exit.

        The loop waits on an Event rather than sleeping, so shutdown is bounded by
        how long one state_fn call takes, not by the poll interval.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout if timeout is not None else self.interval + 1.0)
        self._thread = None

    def poll_once(self) -> dict | None:
        """Take one sample and emit an event if the debounce threshold is met.

        Exposed separately from the thread loop so tests can drive the state
        machine deterministically without sleeping. Returns the sampled state, or
        None if state_fn failed.
        """
        try:
            sample = self.state_fn()
        except Exception:
            # A failing sampler is itself a symptom, but treating it as "down"
            # would let a bug in the sampler paint the UI red. Skipping the
            # sample keeps the last known good state until the sampler recovers.
            return None
        if not isinstance(sample, dict):
            return None
        self._observe(sample)
        return sample

    def _observe(self, sample: dict) -> None:
        with self._lock:
            if self._state is None:
                # The first sample is the baseline, not a transition; announcing
                # it would fire a bogus event at every application start.
                self._state = sample
                return
            if _state_key(sample) == _state_key(self._state):
                self._pending = None
                self._pending_count = 0
                return
            if self._pending is not None and _state_key(sample) == _state_key(self._pending):
                self._pending_count += 1
            else:
                self._pending = sample
                self._pending_count = 1
            if self._pending_count < self.debounce:
                return
            old, new = self._state, self._pending
            self._state = new
            self._pending = None
            self._pending_count = 0
        callback = self.on_change
        if callback is not None:
            # Called outside the lock so a slow or reentrant callback cannot
            # stall or deadlock the polling thread.
            try:
                callback(old, new)
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            if self._stop.wait(self.interval):
                break
