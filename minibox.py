import ctypes
import os
import socket
import sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)

MS_REC = 0x4000
MS_PRIVATE = 1 << 18


def mount(source, target, fstype, flags=0, data=None):
    ret = libc.mount(
        source.encode() if source else None,
        target.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), f"mount {target}")


def run(rootfs: str, hostname: str, command: list[str]) -> int:
    # New mount, hostname and PID namespaces. The PID namespace applies to
    # children, so we fork: the child becomes PID 1 inside it.
    os.unshare(os.CLONE_NEWNS | os.CLONE_NEWUTS | os.CLONE_NEWPID)
    pid = os.fork()

    if pid == 0:
        socket.sethostname(hostname)
        # Stop our mounts from leaking back to the host.
        mount(None, "/", None, MS_REC | MS_PRIVATE)
        os.chroot(rootfs)
        os.chdir("/")
        mount("proc", "/proc", "proc")
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TERM": os.environ.get("TERM", "xterm"),
            "HOME": "/root",
        }
        os.execvpe(command[0], command, env)

    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def main():
    if len(sys.argv) < 5 or sys.argv[1] != "run":
        print("usage: sudo python3 minibox.py run <rootfs> <hostname> <command...>")
        sys.exit(2)
    if os.geteuid() != 0:
        print("minibox needs root (use sudo)")
        sys.exit(1)
    sys.exit(run(sys.argv[2], sys.argv[3], sys.argv[4:]))


if __name__ == "__main__":
    main()