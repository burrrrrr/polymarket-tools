#!/bin/bash
# Host setup on Ubuntu 24.04: packages, the service users, the data disk, the
# checkout's venv, the env file, the units. It runs as root at first boot and
# again by hand after every code update, so each step has to converge from
# whatever an earlier run left behind.
#
#   DATA_DEVICE   block device for /var/lib/polymarket. Unset or empty keeps
#                 the directory on the root disk.
set -euo pipefail

REPO=/opt/polymarket-tools
HOST=$REPO/infra/host
DATA=/var/lib/polymarket

# The pull replaces this file under the running shell, which would carry on
# with the old steps. Start again from the new copy instead.
if [ -d "$REPO/.git" ] && [ -z "${BOOTSTRAP_PULLED:-}" ]; then
    git -C "$REPO" pull --ff-only
    BOOTSTRAP_PULLED=1 exec bash "$HOST/bootstrap.sh"
fi

export DEBIAN_FRONTEND=noninteractive
# apt's hook restarts every service that maps an upgraded library, and a
# restarted farmer or worker has lost its key.
export NEEDRESTART_SUSPEND=1
# The image's own apt timers run in the first minutes after boot. The timeout
# waits for the dpkg lock, and the loop for the lists lock, which has no timeout.
apt_get() {
    for _ in $(seq 60); do
        apt-get -o DPkg::Lock::Timeout=600 "$@" && return
        sleep 10
    done
    return 1
}
apt_get update
apt_get install -y git python3 python3-venv

# The farmer and the worker hold the wallet key, and the console is the one
# process that serves requests. While polymarket-start runs, the key file is
# readable by the user of the keyed units, so the console gets a user of its
# own. The shared group is what lets both users write the databases.
id -u polymarket >/dev/null 2>&1 ||
    useradd --system --user-group --home-dir "$DATA" --no-create-home \
        --shell /usr/sbin/nologin polymarket
id -u polymarket-console >/dev/null 2>&1 ||
    useradd --system --gid polymarket --home-dir "$DATA" --no-create-home \
        --shell /usr/sbin/nologin polymarket-console

mkdir -p "$DATA"
if [ -n "${DATA_DEVICE:-}" ]; then
    # A provider attaches the disk after the VM has started booting.
    for _ in $(seq 120); do
        [ -b "$DATA_DEVICE" ] && break
        sleep 5
    done
    [ -b "$DATA_DEVICE" ] || { echo "Data disk $DATA_DEVICE did not appear." >&2; exit 1; }

    # This disk holds the databases. blkid -p reads the device itself and
    # exits 2 only when it carries no signature at all, so that is the one
    # answer that formats it. A read error must not look like an empty disk.
    probe=0
    blkid -p "$DATA_DEVICE" >/dev/null || probe=$?
    case $probe in
        0) ;;
        2) mkfs.ext4 -q "$DATA_DEVICE" ;;
        *) echo "blkid failed on $DATA_DEVICE (exit $probe). Left untouched." >&2; exit 1 ;;
    esac

    uuid=$(blkid -p -s UUID -o value "$DATA_DEVICE")
    fstype=$(blkid -p -s TYPE -o value "$DATA_DEVICE")
    [ -n "$uuid" ] && [ -n "$fstype" ] ||
        { echo "$DATA_DEVICE holds something that is not a filesystem. Left untouched." >&2; exit 1; }
    grep -q "^UUID=$uuid " /etc/fstab ||
        echo "UUID=$uuid $DATA $fstype defaults,nofail 0 2" >> /etc/fstab
fi
# A run by hand names no device. The disk an earlier run put in fstab still
# has to be mounted before anything below writes under the directory.
if grep -q " $DATA " /etc/fstab && ! mountpoint -q "$DATA"; then
    mount "$DATA"
fi
chown polymarket:polymarket "$DATA"
chmod 0770 "$DATA"
# SQLite makes a new database 0644 whatever the umask, so a database that one
# user made would be read-only to the other. An empty file is an empty database.
for database in farm.db farm_books.db tools.db; do
    [ -e "$DATA/$database" ] ||
        install -m 0660 -o polymarket -g polymarket /dev/null "$DATA/$database"
done

# Root keeps the code and the venv, so neither service user can rewrite what it runs.
chown -R root:root "$REPO"
python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install --disable-pip-version-check "$REPO"

install -d -m 0755 /etc/polymarket
if [ ! -e /etc/polymarket/env ]; then
    install -m 0640 -o root -g polymarket "$HOST/env.example" /etc/polymarket/env.new
    mv /etc/polymarket/env.new /etc/polymarket/env
fi

if [ -d /etc/needrestart/conf.d ]; then
    install -m 0644 "$HOST/needrestart.conf" /etc/needrestart/conf.d/polymarket.conf
fi

install -m 0644 "$HOST"/systemd/polymarket-*.service /etc/systemd/system/
install -m 0755 "$HOST/polymarket-start" /usr/local/sbin/polymarket-start
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
fi

if [ -d /etc/ssh/sshd_config.d ]; then
    # sshd keeps the first value it reads, and an image's own files sort later.
    printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' \
        > /etc/ssh/sshd_config.d/10-polymarket.conf
    if [ -d /run/systemd/system ]; then
        systemctl try-reload-or-restart ssh.service
    fi
fi

cat <<'EOF'

Host is set up. Nothing is enabled and nothing was started or restarted.

  1. Copy your farm.db, farm_books.db and tools.db over the empty ones in
     /var/lib/polymarket before anything starts, and keep them as they are:
     owner and group polymarket, mode 0660. docs/deploy.md has the commands.
  2. Edit /etc/polymarket/env.
  3. sudo systemctl enable --now polymarket-console
  4. sudo polymarket-start

After a code update, restart what should run the new code:
  sudo systemctl restart polymarket-console
  sudo systemctl stop polymarket-farmer polymarket-worker
  sudo polymarket-start        (asks for the key again)
EOF
