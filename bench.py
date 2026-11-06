import statistics
import subprocess
import sys
import time

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 50
ROOTFS = "/opt/minibox/images/alpine"

COMMANDS = {
    "minibox": ["sudo", "python3", "minibox.py", "run", ROOTFS, "bench", "/bin/true"],
    "docker run": ["docker", "run", "--rm", "alpine", "true"],
    "baseline: sudo python3 -c pass": ["sudo", "python3", "-c", "pass"],
}


def timed(cmd: list[str]) -> float:
    start = time.perf_counter()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return (time.perf_counter() - start) * 1000


def main():
    subprocess.run(["sudo", "-v"], check=True)  # cache the sudo password first
    for cmd in COMMANDS.values():  # warm-up, not measured
        for _ in range(3):
            timed(cmd)

    samples = {name: [] for name in COMMANDS}
    for _ in range(RUNS):  # interleaved so system drift hits all equally
        for name, cmd in COMMANDS.items():
            samples[name].append(timed(cmd))

    lines = ["| Command | Median (ms) | p95 (ms) | Min (ms) |", "| --- | --- | --- | --- |"]
    for name, values in samples.items():
        values.sort()
        p95 = values[int(len(values) * 0.95) - 1]
        lines.append(
            f"| {name} | {statistics.median(values):.0f} | {p95:.0f} | {values[0]:.0f} |"
        )
    report = "\n".join(lines)
    print(f"\n{RUNS} runs each\n\n{report}")
    with open("bench_results.md", "w") as f:
        f.write(f"{RUNS} runs each\n\n{report}\n")


if __name__ == "__main__":
    main()