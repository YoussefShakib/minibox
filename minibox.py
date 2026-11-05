import ctypes
import os
import shutil
import socket
import sys
import uuid

libc = ctypes.CDLL("libc.so.6", use_errno=True)

MS_REC = 0x4000
MS_PRIVATE = 1 << 18
MNT_DETACH = 2
SYS_PIVOT_ROOT = 155  # x86_64 only

CONTAINERS_DIR = "/var/lib/minibox/containers"


def _check(ret: int, what: str) -> None:
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), what)


def mount(source, target, fstype, flags=0, data=None):
    ret = libc.mount(
        source.encode() if source else None,
        target.encode(),
        fstype.encode() if fstype else None,
        ctypes.c_ulong(flags),
        data.encode() if data else None,
    )
    _check(ret, f"mount {target}")


def pivot_root(new_root: str, put_old: str) -> None:
    ret = libc.syscall(
        ctypes.c_long(SYS_PIVOT_ROOT), new_root.encode(), put_old.encode()
    )
    _check(ret, "pivot_root")


def umount_detach(target: str) -> None:
    _check(libc.umount2(target.encode(), MNT_DETACH), f"umount {target}")


def child(rootfs: str, container_dir: str, hostname: str, command: list[str]):
    # Own mount, hostname and network namespaces for this process only.
    os.unshare(os.CLONE_NEWNS | os.CLONE_NEWUTS | os.CLONE_NEWNET)
    socket.sethostname(hostname)

    # Stop our mounts from leaking back to the host.
    mount(None, "/", None, MS_REC | MS_PRIVATE)

    # Overlay: the image is the read-only lower layer; writes go to "upper".
    upper = os.path.join(container_dir, "upper")
    work = os.path.join(container_dir, "work")
    merged = os.path.join(container_dir, "merged")
    mount(
        "overlay",
        merged,
        "overlay",
        0,
        f"lowerdir={rootfs},upperdir={upper},workdir={work}",
    )

    # Swap the root: new root = merged, old root parked in /oldroot, then gone.
    os.chdir(merged)
    os.makedirs("oldroot", exist_ok=True)
    pivot_root(".", "oldroot")
    os.chdir("/")
    mount("proc", "/proc", "proc")
    umount_detach("/oldroot")
    os.rmdir("/oldroot")

    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TERM": os.environ.get("TERM", "xterm"),
        "HOME": "/root",
    }
    os.execvpe(command[0], command, env)


def run(rootfs: str, hostname: str, command: list[str]) -> int:
    container_dir = os.path.join(CONTAINERS_DIR, uuid.uuid4().hex[:8])
    for sub in ("upper", "work", "merged"):
        os.makedirs(os.path.join(container_dir, sub))

    # The PID namespace applies to children, so unshare then fork:
    # the child becomes PID 1 inside it.
    os.unshare(os.CLONE_NEWPID)
    pid = os.fork()
    if pid == 0:
        try:
            child(rootfs, container_dir, hostname, command)
        except Exception as e:
            print(f"minibox: {e}", file=sys.stderr)
            os._exit(1)

    _, status = os.waitpid(pid, 0)
    shutil.rmtree(container_dir, ignore_errors=True)
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