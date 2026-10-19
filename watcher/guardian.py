"""Pipe-lifetime guardian: a killed controller must not leave its process groups running."""
import json
import os
import signal
import subprocess
import sys
import time


def terminate_group(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


class Guardian:
    def __init__(self):
        self.process = subprocess.Popen([sys.executable, "-m", "watcher.guardian"], stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, start_new_session=True)

    def send(self, action, pid=0):
        self.process.stdin.write((json.dumps([action, pid]) + "\n").encode())
        self.process.stdin.flush()

    def add(self, pid):
        self.send("add", pid)

    def remove(self, pid):
        self.send("remove", pid)

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


def main():
    groups = set()
    for line in sys.stdin:
        action, pid = json.loads(line)
        if action == "add":
            groups.add(pid)
        elif action == "remove":
            # Flow children are part of its lifetime, including children left
            # behind when a flow function returns normally.
            terminate_group(pid)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            groups.discard(pid)
    for pid in groups:
        terminate_group(pid)
    time.sleep(1)
    for pid in groups:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    main()
