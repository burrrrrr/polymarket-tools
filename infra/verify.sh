#!/bin/bash
# The check a reviewer reruns:   bash infra/verify.sh
#
# It needs Docker and git, and about 8 minutes, or 20 where arm64 is emulated.
# It reaches the Ubuntu mirrors, PyPI and, for probe.sh, Polymarket's public
# endpoints. It calls no cloud API and installs nothing on this machine beyond
# Docker images. No live farmer and no keyed worker ever starts: a stub stands
# in for them on the key path, and the console, the farmer's dry run and the
# keyless worker run only after the container's network is cut.
#
# The working tree is tested as a clone would hold it, uncommitted files
# included, so the line endings and the missing executable bits are the real ones.
set -uo pipefail
# Git Bash on Windows rewrites arguments that look like paths before docker sees them.
export MSYS_NO_PATHCONV=1

IMAGE=polymarket-verify
ORIGIN=/srv/origin
CHECKOUT=/opt/polymarket-tools
DATA=/var/lib/polymarket
DISK=/dev/verify-data
DISK_IMAGE=/root/verify-data.img
STUB=/usr/local/lib/verify-key-stub
UNITS="polymarket-console polymarket-farmer polymarket-worker"
KEYED="polymarket-farmer polymarket-worker"
BARE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; }
skip() { echo "SKIP: $*"; }

check() {
    local what=$1
    shift
    if "$@"; then pass "$what"; else fail "$what"; fi
}

same() {
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: got '$2', wanted '$3'"; fi
}

has() {
    if grep -qF -- "$3" <<<"$2"; then pass "$1"; else fail "$1: '$3' is not in '$2'"; fi
}

logged() {
    local log=/root/verify-$1.log
    shift
    "$@" >"$log" 2>&1 && return 0
    tail -n 60 "$log"
    return 1
}

# ---------------------------------------------------------------- this machine

host() {
    ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && { pwd -W 2>/dev/null || pwd; })
    TMP=$(cd "$(mktemp -d)" && { pwd -W 2>/dev/null || pwd; })
    RUN=polymarket-verify-$$
    trap cleanup EXIT
    cd "$ROOT" || exit 1

    {
        static_checks
        pulumi_checks
        native=$(docker version --format '{{.Server.Arch}}')
        vm_checks "$native"
        if [ "$native" = arm64 ]; then
            pass "arm64: the checks above ran on arm64"
        else
            arm64_checks
        fi
    } 2>&1 | tee "$TMP/verify.log"

    local passed failed skipped
    passed=$(grep -c '^PASS' "$TMP/verify.log")
    failed=$(grep -c '^FAIL' "$TMP/verify.log")
    skipped=$(grep -c '^SKIP' "$TMP/verify.log")
    echo
    echo "verify: $passed passed, $failed failed, $skipped skipped"
    [ "$failed" -eq 0 ] && [ "$passed" -gt 0 ]
}

cleanup() {
    local container
    for container in $(docker ps -aq --filter "name=$RUN"); do
        docker exec "$container" bash "$ORIGIN/infra/verify.sh" inside-release-disk >/dev/null 2>&1
        docker rm -f "$container" >/dev/null 2>&1
    done
    rm -rf "$TMP"
}

# The tree as git would commit it right now, without writing to the repository:
# a throwaway index and object directory hold the uncommitted files.
tree_tar() {
    mkdir -p "$TMP/git/objects"
    (
        export GIT_INDEX_FILE=$TMP/git/index GIT_OBJECT_DIRECTORY=$TMP/git/objects
        export GIT_ALTERNATE_OBJECT_DIRECTORIES=$ROOT/.git/objects
        git read-tree HEAD && git add -A 2>/dev/null && git archive "$(git write-tree)" "$@"
    )
}

static_checks() {
    local files file script with_cr=
    files=$(git ls-files -co --exclude-standard -- infra docs/deploy.md .gitattributes)
    for file in $files; do
        [ "$(tr -cd '\r' <"$file" | wc -c)" -eq 0 ] || with_cr+="$file "
    done
    same "no carriage return in infra/, docs/deploy.md or .gitattributes" "$with_cr" ""
    same "infra/host names no provider" \
        "$(grep -rniwE 'aws|amazon|ec2|ssm|s3|nvme|digitalocean|hetzner|gcp|azure' infra/host)" ""
    same "no account id, email address, wallet address or access key in those files" \
        "$(grep -nE '[0-9]{12}|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}|0x[0-9a-fA-F]{40}|AKIA[0-9A-Z]{16}' $files)" ""
    for script in infra/host/bootstrap.sh infra/host/polymarket-start infra/host/probe.sh infra/verify.sh; do
        check "bash parses $script" bash -n "$script"
    done
}

pulumi_checks() {
    local tests=$RUN-pulumi log=$TMP/pulumi.log
    docker run -d --name "$tests" python:3.12-slim sleep infinity >/dev/null &&
        tree_tar infra | docker exec -i "$tests" tar -x -C /srv &&
        docker exec -w /srv/infra "$tests" pip install -q --disable-pip-version-check \
            --root-user-action=ignore -r requirements.txt ||
        { fail "pulumi: install the pinned SDKs in a container"; return; }

    docker exec -w /srv/infra "$tests" python -W ignore::DeprecationWarning -m unittest -v test_aws \
        >"$log" 2>&1
    local status=$?
    sed -n -E 's/^(test_[a-z0-9_]+) .* \.\.\. ok$/PASS: pulumi: \1/p; s/^(test_[a-z0-9_]+) .* \.\.\. (.*)$/FAIL: pulumi: \1: \2/p' "$log"
    [ $status -eq 0 ] || sed -n '/^=====/,$p' "$log"
    check "pulumi: the unit tests exit 0" [ $status -eq 0 ]

    docker exec -w /srv/infra "$tests" python -c "
import aws, userdata
print(userdata.render('$ORIGIN', '$DISK', first_commands=[aws.APT_OVER_HTTPS]), end='')" \
        >"$TMP/user-data"
    check "pulumi: the AWS module's user data renders for the container run" [ -s "$TMP/user-data" ]
}

# The packages are what an Ubuntu cloud image has and the container image lacks.
#
# systemd needs a privileged container, and a privileged container shares its
# kernel settings with this machine. Left alone, systemd would apply Ubuntu's
# sysctl values to that kernel and replace its binfmt_misc handlers, which are
# what Docker's emulators and WSL's Windows interop run on. A read-only
# /proc/sys makes systemd skip both, as it does in an unprivileged container.
build_image() {
    docker build -q --platform "linux/$1" -t "$IMAGE:$1" - >/dev/null <<'EOF'
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        systemd systemd-sysv dbus sudo curl ca-certificates git openssh-server needrestart \
 && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/bin/sh", "-c", "mount --bind /proc/sys /proc/sys && mount -o remount,bind,ro /proc/sys && exec /sbin/init"]
EOF
}

kernel_handlers() {
    docker run --rm --privileged "$IMAGE:$1" sh -c '
        mount -t binfmt_misc none /proc/sys/fs/binfmt_misc 2>/dev/null
        ls /proc/sys/fs/binfmt_misc | sort | tr "\n" " "'
}

# Start a container from the image and give it what a new VM has: the repo to
# clone at $ORIGIN, and the user data.
start_vm() {
    local vm=$1 arch=$2
    shift 2
    docker run -d --name "$vm" --platform "linux/$arch" "$@" >/dev/null &&
        tree_tar | docker exec -i "$vm" bash -c "
            mkdir -p $ORIGIN && tar -x -C $ORIGIN && cd $ORIGIN && git init -q -b master &&
            git add -A && git -c user.name=verify -c user.email=verify@invalid commit -qm tree" &&
        docker exec -i "$vm" bash -c 'cat > /root/user-data' <"$TMP/user-data"
}

inside() {
    local vm=$1 phase=$2
    docker exec "$vm" bash "$ORIGIN/infra/verify.sh" "inside-$phase" ||
        fail "$phase: the phase stopped before its end"
}

vm_checks() {
    local vm=$RUN-vm handlers
    build_image "$1" || { fail "build the ubuntu:24.04 image with systemd"; return; }
    handlers=$(kernel_handlers "$1")
    start_vm "$vm" "$1" --privileged --tmpfs /run --tmpfs /run/lock "$IMAGE:$1" ||
        { fail "start an ubuntu:24.04 container with systemd as PID 1"; return; }
    inside "$vm" first-boot
    inside "$vm" probe
    check "the container is cut off from the network before anything of the app runs" \
        docker network disconnect bridge "$vm"
    inside "$vm" offline
    same "this machine: the kernel's binfmt_misc handlers are the ones it had before the run" \
        "$(kernel_handlers "$1")" "$handlers"
}

arm64_checks() {
    local vm=$RUN-arm64
    if ! build_image arm64 2>"$TMP/arm64.log"; then
        skip "arm64: this Docker cannot build a linux/arm64 image: $(tail -n 1 "$TMP/arm64.log")"
        return
    fi
    start_vm "$vm" arm64 "$IMAGE:arm64" sleep infinity ||
        { fail "arm64: start an ubuntu:24.04 arm64 container"; return; }
    inside "$vm" arm64
}

# ------------------------------------------------------- inside the containers

inside_first_boot() {
    local state=
    for _ in $(seq 60); do
        state=$(systemctl is-system-running 2>/dev/null)
        [ "$state" = running ] || [ "$state" = degraded ] && break
        sleep 1
    done
    same "systemd is PID 1 in the container" "$(ps -o comm= -p 1) $state" "systemd running"
    same "this machine: the container cannot write the kernel settings it shares with it" \
        "$(findmnt -no OPTIONS /proc/sys | cut -d, -f1) $(ls /proc/sys/fs/binfmt_misc | wc -l)" "ro 0"

    mkdir -p /srv/verify-open && chmod 0777 /srv/verify-open
    touch /tmp/verify-host-marker
    # An image whose own sshd settings allow passwords, which bootstrap must outrank.
    echo 'PasswordAuthentication yes' >/etc/ssh/sshd_config.d/50-cloud-init.conf

    local loop=
    if truncate -s 256M $DISK_IMAGE && loop=$(losetup -f --show $DISK_IMAGE); then
        # bootstrap makes the user just before it looks for the disk, so the
        # disk turns up while bootstrap is already waiting for it.
        ( until id polymarket >/dev/null 2>&1; do sleep 1; done; sleep 8; ln -s "$loop" $DISK ) &
    else
        skip "data disk: this Docker cannot make a loop device, so the checks use a plain directory"
        sed -i "s|DATA_DEVICE=$DISK|DATA_DEVICE=''|" /root/user-data
    fi

    # The image's own apt run, which holds the lists lock when the user data begins.
    apt-get update >/dev/null 2>&1 &
    sleep 1
    check "first boot: the user data runs to its end in cloud-init's bare environment" \
        logged first-boot env -i PATH=$BARE_PATH bash /root/user-data
    has "first boot: apt was busy when the user data began, and the user data waited for it" \
        "$(grep -m 1 'Could not get lock' /root/verify-first-boot.log)" "/var/lib/apt/lists/lock"
    same "first boot: apt has no plain-http source left" \
        "$(grep -cE '^URIs:.*http://' /etc/apt/sources.list.d/ubuntu.sources)" 0
    same "first boot: nothing is enabled" \
        "$(systemctl is-enabled $UNITS 2>&1 | tr '\n' ' ')" "disabled static static "
    same "first boot: nothing is running" \
        "$(systemctl is-active $UNITS 2>&1 | tr '\n' ' ')" "inactive inactive inactive "
    same "first boot: the env file is the example, 0640 root:polymarket" \
        "$(stat -c '%a %U:%G' /etc/polymarket/env) $(cmp $CHECKOUT/infra/host/env.example /etc/polymarket/env 2>&1)" \
        "640 root:polymarket "
    same "first boot: the data directory is 0770 polymarket:polymarket, with an empty 0660 file for each database" \
        "$(stat -c '%a %U:%G' $DATA) $(stat -c '%a %U:%G %s' $DATA/farm.db $DATA/farm_books.db $DATA/tools.db | sort -u)" \
        "770 polymarket:polymarket 660 polymarket:polymarket 0"
    same "first boot: the console has a user of its own, in the group of the other" \
        "$(id -un polymarket-console) $(id -gn polymarket-console) $(id -u polymarket-console | grep -cx "$(id -u polymarket)")" \
        "polymarket-console polymarket 0"
    same "first boot: root owns every file of the checkout and the venv" \
        "$(find $CHECKOUT ! -user root | head -n 3)" ""
    check "first boot: neither service user can write to the code" \
        bash -c "for user in polymarket polymarket-console; do
            sudo -u \$user test ! -w $CHECKOUT/farm/reward_farmer.py -a ! -w $CHECKOUT/farm || exit 1; done"
    mkdir -p /run/sshd
    same "first boot: sshd takes keys only, over an image file that allows passwords" \
        "$(sshd -T 2>&1 | grep -E '^(passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) ' | sort | tr '\n' ' ')" \
        "kbdinteractiveauthentication no passwordauthentication no pubkeyauthentication yes "

    if [ -n "$loop" ]; then
        same "data disk: bootstrap formatted the empty disk and mounted it" \
            "$(findmnt -no SOURCE,FSTYPE $DATA)" "$loop ext4"
        same "data disk: one fstab line, by UUID, with nofail" \
            "$(grep -c "^UUID=$(blkid -p -s UUID -o value "$loop") $DATA ext4 defaults,nofail " /etc/fstab) $(grep -c " $DATA " /etc/fstab)" \
            "1 1"
    fi

    # The operator's own edits and data, which every later run must leave alone.
    sed -i -e "s|^POLYMARKET_FUNDER_ADDRESS=.*|POLYMARKET_FUNDER_ADDRESS=0x$(printf '0%.0s' $(seq 40))|" \
        -e 's|^FARMER_ARGS=.*|FARMER_ARGS=--interval 7|' /etc/polymarket/env
    sudo -u polymarket bash -c "echo database > $DATA/verify-marker"

    fingerprint >/root/verify-before
    check "second run: bootstrap by hand, with no DATA_DEVICE, exits 0" \
        logged second-run bash $CHECKOUT/infra/host/bootstrap.sh
    fingerprint >/root/verify-after
    same "second run: nothing changed (env file, fstab, units, users, mount, databases, packages, unit states)" \
        "$(diff /root/verify-before /root/verify-after)" ""

    if [ -n "$loop" ]; then
        umount $DATA
        check "data disk: bootstrap runs again on the disk that now holds data" \
            logged disk-again env DATA_DEVICE=$DISK bash $CHECKOUT/infra/host/bootstrap.sh
        same "data disk: it was mounted again and not formatted" \
            "$(findmnt -no SOURCE $DATA) $(cat $DATA/verify-marker 2>&1) $(grep -c " $DATA " /etc/fstab)" \
            "$loop database 1"
        umount $DATA
        logged disk-by-hand bash $CHECKOUT/infra/host/bootstrap.sh
        same "data disk: a run by hand mounts the disk again before it writes under the directory" \
            "$(findmnt -no SOURCE $DATA) $(cat $DATA/verify-marker 2>&1)" "$loop database"
    fi

    echo 'echo verify: the pulled bootstrap ran' >>$ORIGIN/infra/host/bootstrap.sh
    git -C $ORIGIN -c user.name=verify -c user.email=verify@invalid commit -qam update
    logged update bash $CHECKOUT/infra/host/bootstrap.sh
    same "update: bootstrap pulls the new commit and carries on from the new copy of itself" \
        "$(git -C $CHECKOUT rev-parse HEAD) $(grep -c '^verify: the pulled bootstrap ran$' /root/verify-update.log)" \
        "$(git -C $ORIGIN rev-parse HEAD) 1"
    same "update: nothing is enabled or running afterwards either" \
        "$(systemctl is-enabled $UNITS 2>&1 | tr '\n' ' ')$(systemctl is-active $UNITS 2>&1 | tr '\n' ' ')" \
        "disabled static static inactive inactive inactive "
}

fingerprint() {
    sha256sum /etc/polymarket/env /etc/fstab /etc/systemd/system/polymarket-*.service \
        /usr/local/sbin/polymarket-start /etc/ssh/sshd_config.d/* /etc/needrestart/conf.d/*
    stat -c '%n %a %U:%G' /etc/polymarket/env $DATA $CHECKOUT /usr/local/sbin/polymarket-start
    stat -c '%n modified %Y' /etc/polymarket/env
    getent passwd polymarket polymarket-console
    getent group polymarket
    stat -c '%n %a %U:%G %s' $DATA/*.db
    findmnt -no SOURCE,FSTYPE,UUID $DATA
    cat $DATA/verify-marker
    git -C $CHECKOUT rev-parse HEAD
    $CHECKOUT/.venv/bin/pip freeze
    systemctl is-enabled $UNITS
    systemctl is-active $UNITS
}

inside_probe() {
    local out
    out=$(bash <$CHECKOUT/infra/host/probe.sh 2>&1)
    same "probe.sh: piped into bash on Ubuntu, it exits 0" $? 0
    echo "$out" | sed 's/^/    /'
    has "probe.sh: it names the edge" "$out" "colo="
    has "probe.sh: it names the country" "$out" "loc="
    has "probe.sh: it reports the timings" "$out" "median"
}

inside_offline() {
    check "offline: the exchange cannot be reached from here on" \
        bash -c '! curl -s -m 5 -o /dev/null https://clob.polymarket.com/time'

    cd $CHECKOUT || return 1
    .venv/bin/python run_tests.py >/root/verify-tests.log 2>&1
    local status=$?
    [ $status -eq 0 ] || cat /root/verify-tests.log
    same "test suite: python run_tests.py exits 0 on $(.venv/bin/python -V 2>&1), $(uname -sm): $(tail -n 1 /root/verify-tests.log)" \
        $status 0

    local out
    out=$(systemd-analyze verify /etc/systemd/system/polymarket-*.service 2>&1)
    same "units: systemd-analyze verify accepts the three units${out:+ ($out)}" $? 0

    systemctl enable --now polymarket-console >/dev/null 2>&1
    local code=
    for _ in $(seq 30); do
        code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/)
        [ "$code" = 200 ] && break
        sleep 1
    done
    same "console: the unit answers GET http://127.0.0.1:8787/" "$code" 200
    same "console: it runs as polymarket-console" \
        "$(stat -c %U "/proc/$(systemctl show -p MainPID --value polymarket-console)")" polymarket-console
    same "console: it filled the two databases it uses, which stay 0660 for the other user" \
        "$(stat -c '%a %U:%G' $DATA/farm.db $DATA/tools.db | sort -u) $(find $DATA -name '*.db' -size +0 | wc -l)" \
        "660 polymarket:polymarket 2"
    same "console: it is enabled for boot" "$(systemctl is-enabled polymarket-console)" enabled

    own_code
    key_path
    restart_hook
    missing_disk
}

# A drop-in that replaces only what a unit runs. The user, the sandbox, the
# environment and, unless one is named here, the standard input stay the unit's.
swap_exec() {
    mkdir -p "/etc/systemd/system/$1.service.d"
    {
        printf '[Service]\nExecStart=\nExecStart=%s\n' "$2"
        [ -z "${3:-}" ] || printf 'StandardInput=%s\n' "$3"
    } >"/etc/systemd/system/$1.service.d/verify.conf"
}

# The farmer's and the worker's own code under their units, with the network
# cut and no key anywhere: one dry-run cycle, and a worker started with --no-key.
own_code() {
    local since
    swap_exec polymarket-farmer "$CHECKOUT/.venv/bin/python -m farm.reward_farmer --once \$FARMER_ARGS" null
    swap_exec polymarket-worker "$CHECKOUT/.venv/bin/python -m tools --no-key" null
    systemctl daemon-reload
    since=$(date +%s)
    systemctl start $KEYED
    for _ in $(seq 60); do
        [ "$(systemctl is-active polymarket-farmer)" != active ] &&
            journalctl -u polymarket-worker --since "@$since" -o cat | grep -q '^Worker on ' && break
        sleep 1
    done
    has "units: the farmer's own code runs one dry cycle under its unit, with FARMER_ARGS applied" \
        "$(journalctl -u polymarket-farmer --since "@$since" --no-pager -o cat)" \
        "DRY RUN (no orders are sent); db $DATA/farm.db, every 7s"
    same "units: that dry cycle exits 0" \
        "$(systemctl show -p Result --value polymarket-farmer) $(systemctl show -p ExecMainStatus --value polymarket-farmer)" \
        "success 0"
    has "units: the worker's own code runs under its unit, with no key" \
        "$(journalctl -u polymarket-worker --since "@$since" --no-pager -o cat)" \
        "Worker on $DATA/tools.db, no key"
    systemctl stop polymarket-worker
    same "units: the worker ends cleanly on the SIGTERM that systemctl stop sends" \
        "$(systemctl show -p Result --value polymarket-worker) $(systemctl show -p ExecMainStatus --value polymarket-worker)" \
        "success 0"
}

# The live farmer and the keyed worker are never started here. A stub in their
# place reports what the unit gave it.
key_path() {
    local unit name
    cat >$STUB <<'EOF'
IFS= read -r key
name=$1
shift
writable() { touch "$1" 2>/dev/null && echo writable || echo read-only; }
echo "stub $name key_chars=${#key} args=$# user=$(id -un)" \
    "no_new_privs=$(awk '/^NoNewPrivs/ { print $2 }' /proc/self/status)" \
    "system=$(writable /srv/verify-open/$name) data=$(writable /var/lib/polymarket/verify-$name)" \
    "home=$([ -r /home ] && echo visible || echo hidden)" \
    "tmp=$([ -e /tmp/verify-host-marker ] && echo shared || echo private)" \
    "umask=$(umask) core=$(ulimit -c) dump_filter=$(cat /proc/self/coredump_filter)"
exec sleep 600
EOF
    for unit in $KEYED; do
        name=${unit#polymarket-}
        swap_exec $unit "/bin/bash $STUB $name$([ "$name" = farmer ] && echo ' $FARMER_ARGS')"
    done
    cat >/etc/systemd/system/verify-slow.service <<'EOF'
[Service]
Type=oneshot
ExecStart=/bin/sleep 600
EOF
    systemctl daemon-reload

    systemctl start polymarket-farmer >/dev/null 2>&1
    same "key: a keyed unit started without polymarket-start fails, and its process never runs" \
        "$? $(journalctl -u polymarket-farmer --no-pager -o cat | grep -c '^stub ')" "1 0"
    systemctl reset-failed polymarket-farmer

    local key since
    key=verify-not-a-key-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    since=$(date +%s)
    ( sleep 3; printf '%s\n' "$key" ) |
        script -qec "sudo polymarket-start" /root/verify-terminal >/dev/null 2>&1
    same "key: sudo polymarket-start, typed into a terminal, exits 0" $? 0
    sed 's/^/    /' /root/verify-terminal

    local sandbox="user=polymarket no_new_privs=1 system=read-only data=writable home=hidden tmp=private"
    sandbox+=" umask=0007 core=0 dump_filter=00000000"
    same "key: the farmer got the whole key on stdin, as polymarket, in the unit's sandbox, with FARMER_ARGS split in two" \
        "$(journalctl -u polymarket-farmer --since "@$since" --no-pager -o cat | grep '^stub ')" \
        "stub farmer key_chars=${#key} args=2 $sandbox"
    same "key: the worker got the whole key on stdin, as polymarket, in the unit's sandbox" \
        "$(journalctl -u polymarket-worker --since "@$since" --no-pager -o cat | grep '^stub ')" \
        "stub worker key_chars=${#key} args=0 $sandbox"
    check "key: /run/polymarket/key is gone afterwards" [ ! -e /run/polymarket/key ]
    same "key: /run/polymarket is memory, 0700 polymarket" \
        "$(findmnt -no FSTYPE -T /run/polymarket) $(stat -c '%a %U' /run/polymarket)" "tmpfs 700 polymarket"
    has "key: the operator is shown the farmer's state and first lines" \
        "$(cat /root/verify-terminal)" "== polymarket-farmer: active"
    has "key: and the worker's" "$(cat /root/verify-terminal)" "stub worker key_chars=${#key}"

    local leaks=0 pid
    grep -qF "$key" /root/verify-terminal && leaks=$((leaks + 1))
    journalctl --no-pager -o cat | grep -qF "$key" && leaks=$((leaks + 1))
    for unit in $KEYED; do
        pid=$(systemctl show -p MainPID --value $unit)
        tr '\0' '\n' </proc/"$pid"/cmdline | grep -qF "$key" && leaks=$((leaks + 1))
        tr '\0' '\n' </proc/"$pid"/environ | grep -qF "$key" && leaks=$((leaks + 1))
    done
    same "key: it is not on the terminal, in the journal, or in either process's arguments or environment" \
        $leaks 0
    # The stub keeps its standard input open, as the tools did before
    # keys.read_stdin_key: the first number is the key's length, read back by
    # the unit's own user long after the file was deleted.
    same "key: a process that keeps the key file open leaves the key readable to its own user, and not to the console's user" \
        "$(sudo -u polymarket cat "/proc/$pid/fd/0" 2>/dev/null | wc -c) $(sudo -u polymarket-console cat "/proc/$pid/fd/0" 2>&1 | grep -c 'Permission denied')" \
        "$((${#key} + 1)) 1"

    # The read the farmer and the worker do, under a copy of the worker's unit.
    grep -v '^ExecStart=' /etc/systemd/system/polymarket-worker.service \
        >/etc/systemd/system/verify-read.service
    cat >>/etc/systemd/system/verify-read.service <<EOF
ExecStart=$CHECKOUT/.venv/bin/python -c "import sys, time; from polymarket import keys; key = keys.read_stdin_key(sys.stdin); print('read key_chars=' + str(len(key)), flush=True); time.sleep(600)"
EOF
    systemctl daemon-reload
    since=$(date +%s)
    printf '%s\n' "$key" | polymarket-start verify-read >/dev/null 2>&1
    pid=$(systemctl show -p MainPID --value verify-read)
    same "key: the tools' own read takes the whole key, then standard input is the null device and the unit's own user reads nothing back" \
        "$(journalctl -u verify-read --since "@$since" --no-pager -o cat | grep '^read ') $(sudo -u polymarket readlink "/proc/$pid/fd/0") $(sudo -u polymarket cat "/proc/$pid/fd/0" 2>/dev/null | wc -c)" \
        "read key_chars=${#key} /dev/null 0"
    systemctl stop verify-read

    printf '%s\n' "$key" | polymarket-start verify-no-such-unit >/dev/null 2>&1
    same "key: a start that fails exits 1 and leaves no key file" \
        "$? $(ls /run/polymarket)" "1 "

    # A start that is still waiting holds the key file, which shows what the
    # units are given, and then how an interrupted start leaves it.
    setsid bash -c "printf '%s\n' '$key' | polymarket-start verify-slow" >/dev/null 2>&1 &
    local starter=$!
    for _ in $(seq 20); do [ -s /run/polymarket/key ] && break; sleep 0.5; done
    same "key: while a start runs, the file is 0400 polymarket and holds the key and a newline" \
        "$(stat -c '%a %U:%G %s' /run/polymarket/key 2>&1)" "400 polymarket:polymarket $((${#key} + 1))"
    same "key: and the console's user cannot read it" \
        "$(sudo -u polymarket-console cat /run/polymarket/key 2>&1 | grep -c 'Permission denied')" 1
    kill -TERM -- -$starter
    wait $starter 2>/dev/null
    for _ in $(seq 20); do [ -e /run/polymarket/key ] || break; sleep 0.5; done
    check "key: an interrupted start removes the file too" [ ! -e /run/polymarket/key ]
    systemctl stop verify-slow 2>/dev/null
}

# apt restarts services that map a library it has just replaced. Both keyed
# stubs and the console are made to look that way, then the hook's own mode runs.
restart_hook() {
    local libc before after
    libc=$(ldd /bin/sleep | awk '/libc\.so/ { print $3 }')
    before="$(systemctl show -p MainPID --value $UNITS | tr '\n' ' ')"
    cp -p "$libc" "$libc.verify" && mv "$libc.verify" "$libc"
    needrestart -r a -l >/root/verify-needrestart.log 2>&1
    after="$(systemctl show -p MainPID --value $UNITS | tr '\n' ' ')"
    set -- $before $after
    same "restart hook: after a library upgrade the console is restarted, and the farmer and worker are left alone" \
        "console $([ "$1" != "$4" ] && echo restarted || echo kept), farmer $([ "$2" = "$5" ] && echo kept || echo restarted), worker $([ "$3" = "$6" ] && echo kept || echo restarted)" \
        "console restarted, farmer kept, worker kept"
}

missing_disk() {
    findmnt -no SOURCE $DATA | grep -q '^/dev/loop' || return 0
    systemctl stop $UNITS
    umount $DATA && losetup -d "$(losetup -j $DISK_IMAGE -O NAME -n)"
    echo "    waiting for systemd to give up on the missing disk, about 90 s"
    systemctl start polymarket-console >/dev/null 2>&1
    same "missing disk: the console refuses to start, and makes no database on the root disk" \
        "$? $(ls $DATA)" "1 "
}

inside_release_disk() {
    systemctl stop $UNITS 2>/dev/null
    umount $DATA 2>/dev/null
    local loop
    for loop in $(losetup -j $DISK_IMAGE -O NAME -n 2>/dev/null); do losetup -d "$loop"; done
}

inside_arm64() {
    sed -i "s|DATA_DEVICE=$DISK|DATA_DEVICE=''|" /root/user-data
    check "arm64: the user data and bootstrap run to their end on $(uname -m)" \
        logged arm64 env -i PATH=$BARE_PATH bash /root/user-data
    same "arm64: apt has no plain-http source left" \
        "$(grep -cE '^URIs:.*http://' /etc/apt/sources.list.d/ubuntu.sources)" 0
    cd $CHECKOUT || return 1
    same "arm64: every dependency installed from a wheel and imports on aarch64" \
        "$(.venv/bin/python -c 'import platform, py_clob_client_v2, fastapi, uvicorn, websockets, httpx, jinja2, dotenv; print(platform.machine())' 2>&1)" \
        aarch64
}

case ${1:-host} in
    host) host ;;
    inside-first-boot) inside_first_boot ;;
    inside-probe) inside_probe ;;
    inside-offline) inside_offline ;;
    inside-arm64) inside_arm64 ;;
    inside-release-disk) inside_release_disk ;;
    *) echo "usage: bash infra/verify.sh" >&2; exit 2 ;;
esac
