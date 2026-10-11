"""The first-boot script, the same on every provider.

A provider module passes what render() returns to its VM as user data. The
script stays this small because everything else lives in
infra/host/bootstrap.sh, which the operator can run again after a code update
without touching the VM's user data.
"""

import shlex

CHECKOUT = "/opt/polymarket-tools"
# The image's own apt timers run in the first minutes after boot. The timeout
# waits for the dpkg lock, and the loop for the lists lock, which has no timeout.
APT_GET = ("apt_get() { for _ in $(seq 60); do "
           "apt-get -o DPkg::Lock::Timeout=600 \"$@\" && return; sleep 10; done; return 1; }")


def render(repo_url, data_device, extra_packages=(), first_commands=()):
    """The user data for an Ubuntu 24.04 VM.

    repo_url        what to clone into /opt/polymarket-tools
    data_device     block device of the data disk, or "" for none
    extra_packages  apt packages the provider's access path needs
    first_commands  shell lines to run before the first apt call, for a
                    provider whose network cannot reach the image's mirrors
    """
    packages = " ".join(["git", *extra_packages])
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        *first_commands,
        "export DEBIAN_FRONTEND=noninteractive",
        APT_GET,
        "apt_get update",
        f"apt_get install -y {packages}",
        f"[ -d {CHECKOUT}/.git ] || git clone {shlex.quote(repo_url)} {CHECKOUT}",
        f"DATA_DEVICE={shlex.quote(data_device)} bash {CHECKOUT}/infra/host/bootstrap.sh",
    ]
    return "\n".join(lines) + "\n"
