import argparse
import ctypes
import os
import shutil
import socket
import stat
import sys
import uuid

libc = ctypes.CDLL("libc.so.6", use_errno=True)

MS_REC = 0x4000
MS_PRIVATE = 1 << 18
MNT_DETACH = 2
SYS_PIVOT_ROOT = 155  # x86_64 only

CONTAINERS_DIR = "/var/lib/minibox/containers"
CGROUP_ROOT = "/sys/fs/cgroup"
CGROUP_BASE = os.path.join(CGROUP_ROOT, "minibox")


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


# ---- cgroups v2 ----


def write_file(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def enable_controllers(path: str, wanted: list[str]) -> None:
    available = read_file(os.path.join(path, "cgroup.controllers")).split()
    missing = [c for c in wanted if c not in available]
    if missing:
        raise RuntimeError(f"cgroup controllers not available: {', '.join(missing)}")
    write_file(
        os.path.join(path, "cgroup.subtree_control"),
        " ".join(f"+{c}" for c in wanted),
    )


def setup_cgroup(name: str, memory_mb: int | None, cpus: float | None) -> str | None:
    wanted = []
    if memory_mb:
        wanted.append("memory")
    if cpus:
        wanted.append("cpu")
    if not wanted:
        return None

    enable_controllers(CGROUP_ROOT, wanted)
    os.makedirs(CGROUP_BASE, exist_ok=True)
    enable_controllers(CGROUP_BASE, wanted)

    path = os.path.join(CGROUP_BASE, name)
    os.makedirs(path)
    if memory_mb:
        write_file(os.path.join(path, "memory.max"), str(memory_mb * 1024 * 1024))
        try:  # no swap, so hitting the limit means an OOM kill, not slowness
            write_file(os.path.join(path, "memory.swap.max"), "0")
        except OSError:
            pass
    if cpus:
        write_file(os.path.join(path, "cpu.max"), f"{int(cpus * 100000)} 100000")
    return path


def oom_kills(path: str) -> int:
    try:
        events = read_file(os.path.join(path, "memory.events"))
    except OSError:
        return 0
    for line in events.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            return int(value)
    return 0


# ---- container ----
def setup_dev(merged: str) -> None:
    dev = os.path.join(merged, "dev")
    os.makedirs(dev, exist_ok=True)
    mount("tmpfs", dev, "tmpfs", 0, "mode=755,size=1m")
    devices = {
        "null": (1, 3),
        "zero": (1, 5),
        "full": (1, 7),
        "random": (1, 8),
        "urandom": (1, 9),
        "tty": (5, 0),
    }
    for name, (major, minor) in devices.items():
        path = os.path.join(dev, name)
        os.mknod(path, 0o666 | stat.S_IFCHR, os.makedev(major, minor))
        os.chmod(path, 0o666)

def child(rootfs, container_dir, hostname, command, cgroup_path):
    # Join the cgroup first, so everything below counts against the limits.
    # Writing 0 means "the process doing the writing".
    if cgroup_path:
        write_file(os.path.join(cgroup_path, "cgroup.procs"), "0")

    os.unshare(os.CLONE_NEWNS | os.CLONE_NEWUTS | os.CLONE_NEWNET)
    socket.sethostname(hostname)
    mount(None, "/", None, MS_REC | MS_PRIVATE)

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

    setup_dev(merged)
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


def run(rootfs, hostname, command, memory_mb=None, cpus=None) -> int:
    name = uuid.uuid4().hex[:8]
    container_dir = os.path.join(CONTAINERS_DIR, name)
    for sub in ("upper", "work", "merged"):
        os.makedirs(os.path.join(container_dir, sub))

    try:
        cgroup_path = setup_cgroup(name, memory_mb, cpus)
    except (RuntimeError, OSError) as e:
        print(f"minibox: cgroup setup failed: {e}", file=sys.stderr)
        shutil.rmtree(container_dir, ignore_errors=True)
        return 1

    os.unshare(os.CLONE_NEWPID)
    pid = os.fork()
    if pid == 0:
        try:
            child(rootfs, container_dir, hostname, command, cgroup_path)
        except Exception as e:
            print(f"minibox: {e}", file=sys.stderr)
            os._exit(1)

    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code < 0:  # killed by a signal: report it the way shells do
        code = 128 - code

    if cgroup_path:
        kills = oom_kills(cgroup_path) if memory_mb else 0
        if kills:
            print(
                f"minibox: container hit its {memory_mb} MB memory limit "
                f"and was OOM-killed (oom_kill={kills})",
                file=sys.stderr,
            )
        try:
            os.rmdir(cgroup_path)
        except OSError:
            pass
    shutil.rmtree(container_dir, ignore_errors=True)
    return code


def main():
    parser = argparse.ArgumentParser(prog="minibox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--memory", type=int, help="memory limit in MB")
    p.add_argument("--cpus", type=float, help="CPU limit, for example 0.5")
    p.add_argument("rootfs")
    p.add_argument("hostname")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if not args.command:
        parser.error("missing command to run")
    if os.geteuid() != 0:
        print("minibox needs root (use sudo)")
        sys.exit(1)
    sys.exit(run(args.rootfs, args.hostname, args.command, args.memory, args.cpus))


if __name__ == "__main__":
    main()