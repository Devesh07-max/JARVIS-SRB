import asyncio
import re
import sys


def _parse_simple(output: str):
    ping = download = upload = None
    for raw in output.splitlines():
        line = raw.strip().lower()
        if line.startswith("ping:"):
            m = re.search(r"ping:\s*([0-9.]+)", raw, re.I)
            if m:
                ping = float(m.group(1))
        elif line.startswith("download:"):
            m = re.search(r"download:\s*([0-9.]+)\s*(Mbit/s|Gbit/s|Kbit/s|bit/s)", raw, re.I)
            if m:
                value, unit = float(m.group(1)), m.group(2).lower()
                factor = {"bit/s": 1e-6, "kbit/s": 1e-3, "mbit/s": 1, "gbit/s": 1000}[unit]
                download = value * factor
        elif line.startswith("upload:"):
            m = re.search(r"upload:\s*([0-9.]+)\s*(Mbit/s|Gbit/s|Kbit/s|bit/s)", raw, re.I)
            if m:
                value, unit = float(m.group(1)), m.group(2).lower()
                factor = {"bit/s": 1e-6, "kbit/s": 1e-3, "mbit/s": 1, "gbit/s": 1000}[unit]
                upload = value * factor
    if ping is None or download is None or upload is None:
        raise RuntimeError("Speedtest returned an incomplete result")
    return ping, download, upload


async def _run_cli(timeout=90):
    commands = [
        [sys.executable, "-m", "speedtest", "--simple"],
        ["speedtest-cli", "--simple"],
    ]
    last = None
    for cmd in commands:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(f"Speed test timed out after {timeout} seconds")
            if proc.returncode != 0:
                raise RuntimeError((stderr or stdout).decode(errors="ignore")[-800:] or "Speed test command failed")
            return _parse_simple(stdout.decode(errors="ignore"))
        except Exception as e:
            last = e
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill(); await proc.communicate()
                except Exception:
                    pass
    raise last or RuntimeError("Speed test command is unavailable")


async def run_speedtest(timeout=90):
    """Bounded speed test for JustrunMyApp; never leaves the button waiting forever."""
    return await _run_cli(timeout=min(90, int(timeout or 90)))
