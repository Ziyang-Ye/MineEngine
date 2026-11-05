"""Free port block for one capture instance when several instances share a host network (scheduler jobs with host networking,
or a GPU box running a few instances at once). A block of 160 ports at P: game P+k, rcon P+10+k, act-recorder P+40, coord
P+50, pose link (UDP) P+60, the last three + 30k + {0, 1} inside run_windows.py (NATIVE_PORT_STEP = 30), k < 4. Blocks lie
below the kernel's ephemeral range when it leaves room (outgoing connections take ephemeral ports at random), else above it.
A block is taken only if no TCP / UDP socket of the host (/proc/net/*) uses any of its ports, each TCP port binds with
SO_REUSEADDR and each UDP port binds. Prints "P display" (display = :100 + block index) or exits 1. Linux only.
  python -I linux/pick_ports.py [SEED]
"""
import random
import socket
import sys

BLOCK, K = 160, 4


def used_ports():
    out = set()
    for f in ("tcp", "tcp6", "udp", "udp6"):
        try:
            for line in open(f"/proc/net/{f}").readlines()[1:]:
                out.add(int(line.split()[1].rsplit(":", 1)[1], 16))
        except OSError:
            pass
    return out


def block(P):
    tcp = [P + k for k in range(K)] + [P + 10 + k for k in range(K)]
    tcp += [P + b + 30 * k + j for b in (40, 50) for k in range(K) for j in (0, 1)]
    udp = [P + 60 + 30 * k + j for k in range(K) for j in (0, 1)]
    return tcp, udp


def binds(tcp, udp):
    for p in tcp + udp:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM if p in tcp else socket.SOCK_DGRAM)
        try:
            if p in tcp:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", p))
        except OSError:
            return False
        finally:
            s.close()
    return True


def main():
    lo, hi = 32768, 60999
    try:
        lo, hi = map(int, open("/proc/sys/net/ipv4/ip_local_port_range").read().split())
    except OSError:
        pass
    ranges = [r for r in ((10000, lo), (hi + 1, 65536)) if r[1] - r[0] >= 20 * BLOCK] or [(10000, 65536)]
    starts = [a + BLOCK * i for a, b in ranges for i in range((b - a) // BLOCK)]
    rng = random.Random(sys.argv[1] if len(sys.argv) > 1 else None); rng.shuffle(starts)
    used = used_ports()
    for P in starts[:200]:
        tcp, udp = block(P)
        if used.isdisjoint(tcp + udp) and binds(tcp, udp):
            print(P, 100 + (P // BLOCK) % 400)
            return
    sys.exit(1)


if __name__ == "__main__":
    main()
