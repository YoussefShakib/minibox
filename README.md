# minibox

A minimal Linux container runtime in Python, built from scratch to learn what
Docker does under the hood. It uses namespaces, cgroups v2, overlayfs and
`pivot_root` directly through system calls, with no container library.

## What it does

- Runs a command in an isolated environment built on an Alpine root filesystem
- **Namespaces:** PID, mount, UTS (hostname) and network
- **Overlay filesystem:** the image stays read-only; writes go to a per-container
  layer that is deleted on exit
- **`pivot_root`:** swaps the root filesystem and unmounts the old one, which
  `chroot` does not do
- **cgroups v2:** `--memory` (MB) and `--cpus` limits, with OOM-kill detection
- A minimal `/dev` (null, zero, random, urandom, tty) on a private tmpfs

## Run it

Needs Linux (tested on WSL2, kernel 6.18, x86_64), Python 3.12 and root.

```
sudo mkdir -p /opt/minibox/images/alpine
# unpack an Alpine "mini root filesystem" tarball into that folder
sudo python3 minibox.py run /opt/minibox/images/alpine mybox /bin/sh
sudo python3 minibox.py run --memory 64 --cpus 0.5 /opt/minibox/images/alpine mybox /bin/sh
```

Inside the container, `hostname` prints `mybox`, `ps` shows the shell as PID 1,
`ip addr` shows only a loopback interface, and the host's files are not reachable.

## What each namespace isolates

| Namespace | Flag | What the container sees |
| --- | --- | --- |
| PID | `CLONE_NEWPID` | Its own process tree; the shell is PID 1 |
| Mount | `CLONE_NEWNS` | Its own mount table; mounts do not leak to the host |
| UTS | `CLONE_NEWUTS` | Its own hostname |
| Network | `CLONE_NEWNET` | An empty network stack (loopback only) |

Not isolated: users, IPC and time namespaces. See Limitations.

## How it works

1. The parent creates a PID namespace and forks, so the child becomes PID 1.
2. The child joins a cgroup (if limits were requested) before doing anything else.
3. It creates mount, UTS and network namespaces and makes `/` private so its
   mounts do not propagate to the host.
4. It mounts an overlay (image as lower layer, a fresh directory as upper),
   mounts a tmpfs `/dev`, then calls `pivot_root` and unmounts the old root.
5. It mounts `/proc` and runs the command with a clean environment.
6. After the container exits, the parent reads the cgroup's OOM counter,
   reports a kill if there was one, and removes the cgroup and overlay layers.

## Benchmark: startup time

`bench.py` runs `/bin/true` in each runtime 50 times (after warm-up),
interleaved so that machine noise affects all of them equally.

| Command | Median (ms) | p95 (ms) | Min (ms) |
| --- | --- | --- | --- |
| minibox | 91 | 115 | 63 |
| docker run | 747 | 810 | 564 |
| baseline: `sudo python3 -c pass` | 38 | 51 | 22 |

Read this carefully:

- The baseline is the cost of `sudo` plus starting Python. Subtracting it,
  minibox adds about 53 ms at the median.
- This is not a fair fight, and it does not show that minibox is a better
  Docker. `docker run` goes through a client, a daemon and (here) Docker
  Desktop's VM boundary, and does image, network and security setup that
  minibox skips.
- The numbers moved between runs (a 5-run test gave 65 ms for minibox), so treat
  them as rough. One machine, one image, one command.
- A [study of isolation platforms](https://arxiv.org/abs/2110.11462) reports
  that going through the Docker daemon adds around 250 ms compared with
  invoking the container runtime directly, which is consistent with most of the
  gap coming from layers above the kernel.

## Limitations

- Requires root. No user namespaces or rootless mode.
- x86_64 only (the `pivot_root` syscall number is hard-coded).
- No image format, no pulling, no layers beyond one overlay.
- No real networking: the container has only a loopback interface.
- No security hardening: no seccomp, no dropped capabilities, no AppArmor.
  Do not run untrusted images with it.
- IPC and time namespaces are not used.
- Tested only on WSL2 with Alpine and BusyBox.

## docker-lab/

A small Dockerfile and Compose setup (a Python app plus Redis) used to practise
layer caching, volumes and health checks.