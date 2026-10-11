# Deploy to a server

This page moves the farmer, the worker, and the console from your desktop to
one small cloud VM. The VM runs Ubuntu 24.04. systemd starts the three
processes, and you reach the VM with `ssh`.

The deployment has two layers:

- `infra/host/` sets up the VM. It is the same on every provider and names
  none of them. `bootstrap.sh` installs the code, the three systemd units, and
  `polymarket-start`, which asks for the wallet key.
- `infra/` is a [Pulumi](https://www.pulumi.com/docs/) program in Python that
  creates the VM. `infra/aws.py` is the only provider module today.

The wallet key is never written to the server's disk, to the provider, or to
Pulumi's state. You type it each time you start the farmer or the worker.

Nobody has deployed this yet. Read
[What is and is not verified](#what-is-and-is-not-verified) before you rely on
any step.

- [Create the server on AWS](#create-the-server-on-aws)
- [Run the server](#run-the-server)
- [Back up and remove the server on AWS](#back-up-and-remove-the-server-on-aws)
- [Try another provider](#try-another-provider)
- [What is and is not verified](#what-is-and-is-not-verified)
- [Security assumptions](#security-assumptions)

## Create the server on AWS

`infra/aws.py` creates these resources:

- A network of its own, with one public subnet.
- A security group with no inbound rule. Outbound, it allows TCP 443 only.
- One arm64 instance, `t4g.small` unless you change it, from Canonical's
  current Ubuntu 24.04 image. The root volume is 16 GB and encrypted.
- An instance role with one policy, `AmazonSSMManagedInstanceCore`.
- An encrypted 20 GB data volume for the databases, mounted at
  `/var/lib/polymarket`.
- A snapshot policy that copies the data volume every day and keeps 7 copies.
- A key pair that holds your SSH public key.

No port is open to the internet. Your `ssh` connects through AWS Systems
Manager Session Manager, which the instance reaches outbound on port 443.

### Install the tools

You need these on the desktop:

- The AWS CLI v2 and the
  [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html),
  with credentials that can create the resources above.
  `aws sts get-caller-identity` shows which account the CLI uses.
- Python 3.11 or later, which the repository already needs.
- The OpenSSH client and an SSH key pair. Windows 10 has `ssh` and
  `ssh-keygen`. If `~/.ssh/id_ed25519.pub` does not exist, make a key pair
  with `ssh-keygen -t ed25519`.
- The Pulumi CLI. In PowerShell:

  ```powershell
  winget install pulumi
  ```

  Open a new terminal afterwards, so that `pulumi` is on the `PATH`.

The Pulumi packages in `infra/requirements.txt` go into a virtual environment
in `infra/.venv`. They are not dependencies of the tools, and the server never
installs them.

### Keep the Pulumi state in a local file

Pulumi records the resources it created in a state file. The commands on this
page keep that file on your desktop:

```powershell
pulumi login --local
```

The state is then under `.pulumi` in your home directory.
`pulumi login file://C:/some/folder` puts it in another folder.

- The state holds resource ids, your SSH public key, and the first-boot
  script. It holds no wallet key.
- Back up the state folder. If you lose it, Pulumi forgets the resources, and
  they keep running and AWS keeps charging for them. You then have to find
  and delete each one in the AWS console.
- Pulumi asks for a passphrase when you create the stack, and again on every
  later command unless `PULUMI_CONFIG_PASSPHRASE` holds it. The passphrase
  encrypts secret settings. This stack has none.

### Create and configure the stack

The instance clones the repository from GitHub when it first boots. Push
`infra/` to the repository that `repoUrl` names before you deploy.

Run these in PowerShell, from the repository root:

```powershell
cd infra
pulumi stack init prod
pulumi config set aws:region eu-west-1
pulumi config set sshPublicKey (Get-Content $HOME\.ssh\id_ed25519.pub)
pulumi install
```

`pulumi install` creates `infra/.venv` and installs the pinned packages.
`pulumi config set` writes `infra/Pulumi.prod.yaml`, which Git ignores.

| Setting | Default | Meaning |
| --- | --- | --- |
| `aws:region` | none, required | The AWS region |
| `sshPublicKey` | none, required | The public key that may log in as `ubuntu` |
| `instanceType` | `t4g.small` | An arm64 instance type |
| `dataVolumeGb` | `20` | The size of the data volume |
| `repoUrl` | `https://github.com/burrrrrr/polymarket-tools.git` | The repository the instance clones |
| `provider` | `aws` | The provider module. `aws` is the only one |

Three things are fixed when the instance is created: `sshPublicKey`,
`repoUrl`, and the Ubuntu image. A later change to one of them does not touch
the running instance, because replacing or restarting the instance would stop
the farmer and the worker. To change your SSH key later, edit
`~/.ssh/authorized_keys` on the server.

Two settings do change a running instance:

- A new `instanceType` makes AWS stop and start the instance. The farmer and
  the worker stop with it, and you have to run `sudo polymarket-start` again.
- A larger `dataVolumeGb` grows the volume while it is in use, but not the
  filesystem on it. Grow the filesystem on the server afterwards. Nobody has
  run this command on a server yet:

  ```bash
  sudo resize2fs "$(findmnt -no SOURCE /var/lib/polymarket)"
  ```

### Deploy

```powershell
pulumi up
```

Pulumi lists what it will create and asks you to confirm. When it finishes it
prints `instanceId`, `publicIp`, and `dataVolumeId`. `pulumi stack output
instanceId` prints one of them again.

The first boot then installs the packages and the code, which takes a few
minutes. [What the first boot sets up](#what-the-first-boot-sets-up) lists
the result.

### Connect with ssh

Add a block to `~/.ssh/config` on the desktop, which is
`C:\Users\<you>\.ssh\config` on Windows. Put your instance id in `HostName`.
The `ProxyCommand` line depends on which `ssh` you run.

For the Windows OpenSSH client, which PowerShell and `cmd` use:

```text
Host polymarket
    HostName i-0123456789abcdef0
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    ProxyCommand C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p --region eu-west-1"
```

For the `ssh` that Git Bash ships:

```text
Host polymarket
    HostName i-0123456789abcdef0
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p' --region eu-west-1"
```

Both clients read the same file. If you use both, give the two blocks
different `Host` names.

Then connect, and check that the first boot finished:

```bash
ssh polymarket
cloud-init status --wait
tail -n 20 /var/log/cloud-init-output.log
```

`cloud-init status --wait` returns when the first boot is over. The log then
ends with the lines `bootstrap.sh` prints, which start with `Host is set up.`
If the log ends with an error, fix the cause and run `bootstrap.sh` again as
[Update the code](#update-the-code) shows. It is safe to run any number of
times.

### Open a shell without an SSH key

Session Manager can also open a shell by itself. Use it when `ssh` does not
work. In PowerShell, from the `infra` directory:

```powershell
aws ssm start-session --target (pulumi stack output instanceId) --region eu-west-1
```

You are then `ssm-user`, which can use `sudo`.

## Run the server

This part is the same on every provider. The commands assume the `Host
polymarket` block above, and that you log in as a user that can use `sudo`.

### What the first boot sets up

The first boot runs `infra/host/bootstrap.sh`, which does this:

- It installs `git`, `python3`, and `python3-venv` from Ubuntu's own packages.
- It creates two system users with no login shell. The farmer and the worker
  run as `polymarket`. The console runs as `polymarket-console`, so that the
  process that serves web requests cannot read the wallet key of the other
  two. Both users are in the group `polymarket`.
- It formats the data disk if the disk is empty, and mounts it at
  `/var/lib/polymarket`. It never formats a disk that already holds a
  filesystem.
- It makes an empty `farm.db`, `farm_books.db`, and `tools.db` there if they
  do not exist, with mode 0660 and group `polymarket`. Both users have to
  write the databases, and a database that one of the programs created would
  be read-only for the other user.
- It keeps the code in `/opt/polymarket-tools` and the dependencies in
  `/opt/polymarket-tools/.venv`. Both belong to `root`, so the processes
  cannot change the code they run.
- It copies `infra/host/env.example` to `/etc/polymarket/env` if that file
  does not exist. It never overwrites the file.
- It installs the units `polymarket-console`, `polymarket-farmer`, and
  `polymarket-worker`, and the command `polymarket-start`.
- It tells `sshd` to refuse passwords.
- It tells Ubuntu not to restart the farmer or the worker after a library
  update (see [Automatic updates](#automatic-updates)).

It enables nothing and starts nothing. Do the next four steps in order.

### Move the databases

The databases on a new server are empty. Copy yours over them before you
start anything.

1. Stop the farmer, the console, and the worker on the desktop. Nothing stops
   two farmers from running on the same account, and they would cancel and
   replace each other's orders.
2. On the desktop, in the repository root, take a consistent copy of each
   database. `data` is the data directory that `POLYMARKET_DATA_DIR` names.
   Change it in the command if yours differs.

   ```powershell
   mkdir transfer
   python -c "import sqlite3; [sqlite3.connect(f'file:data/{n}?mode=ro', uri=True).backup(sqlite3.connect(f'transfer/{n}')) for n in ('farm.db', 'farm_books.db', 'tools.db')]"
   ```

   The command works in PowerShell and in Bash. It reads each database and
   never writes to it. Python 3.11 has no `python -m sqlite3`.
3. Copy the three files to your home directory on the server:

   ```powershell
   scp transfer/farm.db transfer/farm_books.db transfer/tools.db polymarket:
   ```

4. On the server, put them in the data directory. The owner, the group, and
   the mode matter, because two users write these files:

   ```bash
   sudo install -o polymarket -g polymarket -m 0660 -t /var/lib/polymarket farm.db farm_books.db tools.db
   rm farm.db farm_books.db tools.db
   ```

To bring a copy back to the desktop, run the same steps the other way. Stop
the three units first if the desktop is going to take over. On the server:

```bash
mkdir transfer
sudo python3 -c "import sqlite3; [sqlite3.connect(f'file:/var/lib/polymarket/{n}?mode=ro', uri=True).backup(sqlite3.connect(f'transfer/{n}')) for n in ('farm.db', 'farm_books.db', 'tools.db')]"
sudo chown "$USER" transfer/*
```

Then on the desktop:

```powershell
scp "polymarket:transfer/*.db" data/
```

### Edit the env file

`/etc/polymarket/env` replaces `.env` on the server. The three units read it.

```bash
sudoedit /etc/polymarket/env
```

- Set `POLYMARKET_FUNDER_ADDRESS`.
- Leave `POLYMARKET_DATA_DIR=/var/lib/polymarket`.
- `FARMER_ARGS` holds extra flags for the farmer, such as `--interval 10`.
  Leave `--heartbeat` out. See
  [The heartbeat warning](farmer.md#the-heartbeat-warning).

systemd reads this file, not a shell. Write one `NAME=value` on a line, with
no `export` and no `$VARIABLE`. A unit reads the file when it starts, so
restart a unit after you change the file.

### Start the console

```bash
sudo systemctl enable --now polymarket-console
```

The console needs no key. `enable` makes it start again after a reboot, and
systemd restarts it if it crashes.

### Start the farmer and the worker

```bash
sudo polymarket-start
```

`polymarket-start` asks for `Wallet private key:` and does not show what you
type or paste. It then starts `polymarket-farmer`, which runs
`python -m farm.reward_farmer --live --key-stdin` with your `FARMER_ARGS`, and
`polymarket-worker`, which runs `python -m tools --key-stdin`. Ten seconds
later it prints the state of each unit and the first lines of its log. Read
those lines. They show the region check and whether the key was accepted.

To start one unit, name it: `sudo polymarket-start polymarket-worker`.

`polymarket-start` leaves a unit that is already running as it is. To start a
running unit again, stop the unit first.

The key takes this path:

1. `polymarket-start` writes the key to `/run/polymarket/key`. `/run` is kept
   in memory, not on a disk. Only `polymarket` can read the file.
2. systemd opens the file as the standard input of each unit, and the
   program reads one line from it.
3. `polymarket-start` deletes the file as soon as the units have started, and
   also when it fails or you interrupt it.

The key is never a command-line argument or an environment variable, so it
does not appear in a process listing, in the journal, or in the shell history.

A farmer or a worker that stops stays stopped. That covers a crash, a `stop`
command from the console, and a reboot. systemd cannot start either unit by
itself, because it has no key. `sudo systemctl start polymarket-farmer`
fails for the same reason. Run `sudo polymarket-start` again.

### Read the logs

The programs print to standard output, and systemd keeps the output in the
journal:

```bash
sudo journalctl -u polymarket-farmer -f            # follow the farmer
sudo journalctl -u polymarket-worker --since today
sudo journalctl -u polymarket-console -n 50
systemctl status 'polymarket-*'
```

### Stop

```bash
sudo systemctl stop polymarket-farmer
sudo systemctl stop polymarket-worker
```

Stopping the farmer cancels its orders, as Ctrl+C does on the desktop. The
worker finishes its running job first. systemd waits up to 120 seconds for
each of them.

### Open the console in your browser

Forward the console's port over `ssh`, from the desktop:

```powershell
ssh -N -L 8787:127.0.0.1:8787 polymarket
```

Leave that command running and open http://127.0.0.1:8787. Use port 8787 on
both ends. The console answers 400 to a request whose `Host` header is not
`127.0.0.1:8787` or `localhost:8787` (see
[Security](console.md#security)).

### Update the code

Push the change to GitHub, then on the server:

```bash
sudo bash /opt/polymarket-tools/infra/host/bootstrap.sh
sudo systemctl restart polymarket-console
```

`bootstrap.sh` pulls the new commits, installs changed dependencies and
units, and restarts nothing. The pull is fast-forward only, so it stops if the
checkout on the server has commits of its own.

The farmer and the worker keep running the old code until you restart them,
which means typing the key again. Restart them soon after the update. A
running program can still load a module it has not used yet, and it would
load the new version of that module beside the old code. To restart them:

```bash
sudo systemctl stop polymarket-farmer polymarket-worker
sudo polymarket-start
```

### What a reboot does

After a reboot the data disk is mounted again and the console starts. The
farmer and the worker wait for you. Log in and run `sudo polymarket-start`.

A normal shutdown stops the farmer first, so the farmer cancels its orders.
If the VM dies without a shutdown, the orders stay on the book until they
expire, as [The heartbeat warning](farmer.md#the-heartbeat-warning)
describes for a farmer without `--heartbeat`.

If the data disk fails to mount, the console does not start, because the
databases are on that disk.

### Automatic updates

Ubuntu installs security updates by itself and does not reboot for them.
After an update, Ubuntu restarts the services that still use an old copy of
a library. `bootstrap.sh` exempts `polymarket-farmer` and `polymarket-worker`
from those restarts, because a restart would lose the key. The console can be
restarted. Restart the farmer and the worker yourself, at a time you choose,
to load the updated libraries.

## Back up and remove the server on AWS

### Backups

AWS takes a snapshot of the data volume every day at 03:00 UTC and keeps the
last 7. A snapshot is a copy of the disk as it was at that moment, taken
while the processes run.

List the snapshots in PowerShell, from the `infra` directory:

```powershell
aws ec2 describe-snapshots --owner-ids self --region eu-west-1 --filters "Name=volume-id,Values=$(pulumi stack output dataVolumeId)" --query "Snapshots[].[SnapshotId,StartTime]" --output table
```

### Restore a snapshot

Nobody has run this procedure. It copies the databases out of a snapshot and
leaves the data volume that Pulumi manages in place.

1. Find the instance's availability zone:

   ```powershell
   aws ec2 describe-instances --instance-ids (pulumi stack output instanceId) --region eu-west-1 --query "Reservations[0].Instances[0].Placement.AvailabilityZone" --output text
   ```

2. Create a volume from the snapshot in that zone, and attach it. The first
   command prints the id of the new volume, which the second command needs.

   ```powershell
   aws ec2 create-volume --snapshot-id snap-0123456789abcdef0 --availability-zone eu-west-1a --volume-type gp3 --region eu-west-1 --query VolumeId --output text
   aws ec2 attach-volume --volume-id vol-0fedcba9876543210 --instance-id (pulumi stack output instanceId) --device /dev/sdg --region eu-west-1
   ```

3. On the server, stop the three units, and mount the new volume read-only.
   Its device name is the volume id without the dash.

   ```bash
   sudo systemctl stop polymarket-farmer polymarket-worker polymarket-console
   sudo mkdir -p /mnt/restore
   sudo mount -o ro /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0fedcba9876543210 /mnt/restore
   ```

4. Move the current databases aside and copy the old ones in. Copy each
   database together with its `-wal` file, which can hold its newest rows.

   ```bash
   sudo mkdir /var/lib/polymarket/before-restore
   sudo sh -c 'mv /var/lib/polymarket/*.db* /var/lib/polymarket/before-restore/'
   sudo sh -c 'cp -a /mnt/restore/*.db* /var/lib/polymarket/'
   sudo umount /mnt/restore
   ```

5. Detach and delete the new volume before the next reboot. It has the same
   filesystem id as the data volume, so a reboot could mount either one.

   ```powershell
   aws ec2 detach-volume --volume-id vol-0fedcba9876543210 --region eu-west-1
   aws ec2 delete-volume --volume-id vol-0fedcba9876543210 --region eu-west-1
   ```

6. Start the console, then run `sudo polymarket-start`.

### Remove the server

1. Stop the farmer, the worker, and the console, and copy the databases back
   to the desktop if you want them (see
   [Move the databases](#move-the-databases)).
2. Lift the two protections. Pulumi refuses to delete the instance and the
   data volume while they are protected, and AWS refuses to terminate the
   instance while termination protection is on. In PowerShell, from the
   `infra` directory:

   ```powershell
   pulumi state unprotect --all
   aws ec2 modify-instance-attribute --instance-id (pulumi stack output instanceId) --no-disable-api-termination --region eu-west-1
   ```

3. Delete everything:

   ```powershell
   pulumi destroy
   pulumi stack rm prod
   ```

`pulumi destroy` shuts the instance down before it detaches the data volume.
Deleting the data volume first takes a final snapshot of it. That snapshot
and the daily ones stay in your account and cost money until you delete them
with `aws ec2 delete-snapshot`.

## Try another provider

Where the VM is decides whether Polymarket accepts its orders and how long a
request takes. Before you choose a provider or a region, start a throwaway
Ubuntu VM there and run the probe on it:

```bash
curl -fsSL https://raw.githubusercontent.com/burrrrrr/polymarket-tools/master/infra/host/probe.sh | bash
```

`probe.sh` needs only `bash` and `curl`. It sends read-only requests and no
key. It prints:

- The Cloudflare site and the country that the order book's host sees your
  requests come from.
- Whether Polymarket's geoblock check refuses that location.
- The time of the first request to `https://clob.polymarket.com/time`, which
  includes the TLS handshake, and the minimum, median, and maximum of ten
  more requests on the same connection.

`infra/host/` runs unchanged on any Ubuntu 24.04 VM. A new provider is one
module beside `infra/aws.py` and one branch in `infra/__main__.py`. The module
must supply:

- A VM that runs Ubuntu 24.04.
- A firewall that exposes no port to the internet, or `sshd` alone if the
  provider has no tunnel of its own.
- A data disk, and the device path at which the VM sees the disk.
- Your SSH public key, installed for the image's default user.
- The first-boot script from `userdata.render()` in `infra/userdata.py`, which
  takes the repository URL, the device path, any extra packages, and any
  commands the provider needs before the first package install.
- A way to reach `sshd`. On AWS it is Session Manager.

## What is and is not verified

`bash infra/verify.sh` runs every check below on your machine. It needs
Docker and Git, and it calls no cloud API. It takes about 8 minutes, or 20 on
a machine that has to emulate arm64. It runs systemd in a privileged
container, with `/proc/sys` read-only so that the container cannot change the
kernel settings it shares with your machine.

Verified by `infra/verify.sh`:

- The Pulumi program, with Pulumi's mocks in place of AWS: the security group
  has no inbound rule and lets only TCP 443 out, IMDSv2 is required with a hop
  limit of 1, both volumes are encrypted, the instance role has only the
  Session Manager policy, no S3 bucket exists, the instance ignores changes to
  the image, the first-boot script, and the SSH key, and the instance and the
  data volume are protected.
- The first-boot script and `bootstrap.sh`, in an Ubuntu 24.04 container that
  runs systemd: a first boot while another `apt` run holds its lock, with an
  empty data disk that appears late, then a second run that changes nothing,
  a run on a disk that already holds data, and an update that pulls a new
  commit.
- The repository's test suite, on Linux and Python 3.12.
- The three units pass `systemd-analyze verify`. The console unit starts as
  `polymarket-console` and answers on `127.0.0.1:8787`.
- With the network cut and no key, the farmer's own code runs one dry-run
  cycle under its unit and writes the database the console has open. The
  worker's own code runs under its unit with `--no-key` and ends cleanly when
  systemd stops it.
- The key path, with a stub in place of the live farmer and the keyed worker.
  The stub receives the whole key on standard input and runs as `polymarket`
  inside the unit's sandbox. The key is not on the terminal, in the journal,
  or in the process's arguments or environment, and the key file is gone
  afterwards.
- The user `polymarket-console` cannot read the key file. The user
  `polymarket` can while `polymarket-start` runs, which is why the console
  does not run as `polymarket`.
- The read of the key that the farmer and the worker share, under a copy of
  the worker's unit: it takes the whole key, and standard input is then the
  null device, so the unit's own user reads nothing back from it.
- The console does not start when systemd cannot mount the data disk.
- Ubuntu's restart of services after a library update restarts the console
  and leaves the farmer and worker units running.
- The dependencies install on arm64, in an emulated container.
- `probe.sh` runs on Ubuntu when piped into `bash`.

Not verified:

- The Pulumi program has only been unit-tested with mocks. Nobody has run
  `pulumi preview` or `pulumi up` on it, so nothing here has been created on
  AWS.
- `ssh` through Session Manager has not been exercised. Both `ssh` clients
  parse the two config blocks above, and that is all.
- A live farmer and a keyed worker have never been started on a server. No
  real key has been typed into `polymarket-start`, and no order has been
  placed or cancelled from a server.
- The data disk checks use a loop device in a container. `bootstrap.sh` has
  not formatted or mounted an attached cloud disk.
- A reboot. In the container, `bootstrap.sh` mounts the data disk. systemd
  has never mounted it from `/etc/fstab` at boot.
- The backup, restore, and teardown procedures have not been run.
- The console has not been opened through an `ssh` tunnel.
- The Pulumi commands on this page have not been run. This page was written
  on a machine without the Pulumi CLI.
- No real crash has tested the core dump settings of the two keyed units. The
  container shows only that the units start with those settings applied.

## Security assumptions

- Anyone whose AWS credentials can start a Session Manager session on the
  instance is root on it, with or without your SSH key. Root can read the
  wallet key out of the memory of a running farmer or worker. Protect the AWS
  account with MFA.
- While a farmer or a worker runs, the key is in that process's memory, where
  root can read it. The process closes the key file as soon as it has read
  the key, so the deleted file is not reachable through the process's
  standard input. For the seconds that `polymarket-start` runs, the key file
  is readable by root and the user `polymarket`. The console runs as another
  user and cannot read it. Run nothing else as `polymarket`.
- The two keyed units write no core file, and a crash handler such as
  Ubuntu's apport gets a dump with no memory in it.
- Do not turn on Session Manager's session logging without thinking about it.
  It records what a Session Manager shell prints. The key prompt does not
  echo, so the key is not recorded, but everything else is.
- The console has no login. On the server it is reachable by anyone who can
  log in to the VM, which is why no other person should have an account there.
- `sshd` listens on the VM and accepts keys only. On AWS the security group
  has no inbound rule, so nothing on the internet can reach `sshd`.
- Outbound, the AWS security group allows TCP 443 only. The first-boot script
  therefore switches the VM's package sources to HTTPS.
