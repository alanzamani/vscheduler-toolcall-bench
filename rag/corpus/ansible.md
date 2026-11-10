# vScheduler — Ansible method

The third deployment method, independent of `compose/` and `k8s/`. It has the
same **dev** and **prod** modes and the same optional **MCP** add-on, on VMs
instead of containers.

```bash
./deploy ansible dev up --mcp     # from the repo root: VMs + config + MCP
./deploy ansible dev status       # VM states, mgmt_server, "MCP: ENABLED/DISABLED"
./deploy ansible dev shell        # then: vinfo, vmanage, vreport, …
./deploy ansible dev mcp off      # remove MCP, leave the rest
```

Every `./deploy` command works here: `up [--mcp]`, `down`, `wipe`, `status`,
`mcp on|off`, `shell`, `ssh` (dev), `seed`, `logs [vscheduler|vscheduler-mcp]`.
Needs `brew install ansible` and `brew install --cask multipass`. Run the
Multipass install in a normal terminal: its installer asks for your password.

## What it builds (local lab)

| VM | Role | Size |
|---|---|---|
| `vs-dev` / `vs-prod` | vScheduler server: mgmt_server (systemd), CLI tools, MariaDB + Guacamole + LibreBooking in Docker, MCP (optional) | 2 CPU, 2 GB |
| `node01..04` | compute nodes, **shared by both modes** (01/02 general pool, 03/04 bookable) | 1 CPU, 512 MB |

All VMs run Ubuntu 22.04, the same base as the container images.

The compute nodes are shared, as at a real site. Each node's login hook asks the
dev server first and only moves on to prod when dev answers "dev". If the
first server is unreachable, the hook kills the session (production
fail-closed). Ansible therefore points the hooks at whichever servers exist,
and `wipe` re-points them at the remaining one.

Endpoints are on the server VM's own IP, which `up` and `status` print:
- SSH: port 22
- Guacamole: `:8080/guacamole`
- LibreBooking: `:8081/Web/index.php`
- MCP: `:8930/mcp`

## dev vs prod

| | dev (`group_vars/dev.yml`) | prod (`group_vars/prod.yml`) |
|---|---|---|
| install | editable, `~/vscheduler/src` + `~/vscheduler/.venv` | editable, `/opt/vscheduler` + Environment Modules |
| config | `config.py` rendered from group_vars, dev DB users | same template, `*_prod` users |
| human SSH | password (`ubuntu`/`ubuntu`) or key | key only (`./deploy ansible prod shell`) |
| MCP default | read-write | read-only |

**Config:** `roles/server/templates/config.py.j2` renders
`vscheduler/lib/config.py`, the class the code imports, from the inventory's
group_vars. It replaces the old `config.yml.j2`, which rendered a
`config.yml` that nothing in vScheduler ever read. Change a value in
group_vars and re-run `up`.

**Source:** cloned on the controller from `repository_url`@`git_branch`
(`vscheduler_source: fork`, the default), then copied to the VM. The fork is
private: the clone uses your git credentials (e.g. `gh auth setup-git`) or
`GIT_TOKEN`. The VMs never see a token. Set `vscheduler_source: local` to
deploy this repo's `src/` instead.

**Credentials:** plain throwaway values in group_vars, the same as
compose/k8s. Set `use_vaultwarden: true` to fetch them with `bw` instead,
from items `<dev|prod>-booked-local`, `-guacamole-local` and `-report`.

## MCP add-on

`up --mcp`, or `mcp on|off` on a running server, runs `site.yml` with
`-e mcp_state=present|absent`. Without `mcp_state` the add-on is left as it
is.

When on, `roles/mcp` installs the server from this repo's `mcp/` as a
`vscheduler-mcp` systemd service under its own `mcp` user. It:
- SSHes to `ubuntu@localhost` with its own key, which is authorised only from
  127.0.0.1;
- verifies the VM's pinned host key.

`mcp off` stops it and revokes the key. `status` reports ENABLED only after a
real MCP handshake and a `vscheduler_health` call, as in the other methods.

## Layout

```
provision.yml            Multipass VMs for a mode -> inventory/local/hosts.yml (generated)
site.yml                 servers (services, server, [mcp]) then compute nodes (client)
inventory/local/         the lab: group_vars all/dev/prod; hosts.yml written by provision.yml
inventory/site/          hand-maintained real hosts (Vaultwarden on, services external)
roles/services/          Docker: MariaDB + Guacamole + LibreBooking, init SQL from ../docker/db-init
roles/server/            source, config.py, venv, CLI on PATH, mgmt_server unit, /etc/hosts
roles/mcp/               the MCP add-on (present/absent)
roles/client/            server keys, login/logout hooks, atd
docs/external-services.md  running Guacamole/LibreBooking/Vaultwarden yourself (real site)
```

## Without ./deploy

```bash
cd ansible
ansible-playbook provision.yml -e mode=dev
ansible-playbook site.yml -l localhost,dev,clients [-e mcp_state=present]
# a real site (no provisioning; Vaultwarden; services already running):
ansible-playbook -i inventory/site site.yml -l localhost,prod,clients \
    -e "service_user=rocky service_group=rocky" --ask-become-pass
```

`localhost` is in the limit because the first play prepares the source on the
controller.

## Limits

- **Ubuntu/Debian targets only.** The earlier tasks had partial Rocky Linux
  branches (dnf packages, SELinux); this rewrite does not.
- **Guacamole 1.5.5 runs under amd64 emulation on Apple Silicon.** Its images
  are amd64-only. Docker Desktop emulates them silently, which is why
  compose/k8s work, so `roles/services` installs `qemu-user-static` on arm64
  VMs. Guacamole 1.6.0 ships arm64 images; moving every method to it would
  remove the emulation.
- **LibreBooking and Guacamole start empty**, as in the other methods (see the
  top-level README's Known gaps).
