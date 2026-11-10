# vscheduler — deployment methods

Automated ways to deploy vScheduler with everything it talks to: one MariaDB
(report + Guacamole + LibreBooking databases), Guacamole, LibreBooking, and
dummy compute nodes reached over SSH and the management socket (:65001).

## Where it applies

vScheduler was first developed for HPC visualisation. This deployment is not
tied to that use. The scheduler manages a pool of SSH-reachable nodes:
- it partitions them into general and bookable pools;
- it allocates, reserves, drains and reports on them.

Those nodes can be any hosts, used in any way: shell, batch or service
workloads, or remote desktops. Guacamole offers browser-based remote access
where it's needed; it is one access path, not a requirement.

The same stack runs on:
- **public cloud**: managed Kubernetes or plain VMs;
- **private cloud**: on-prem Kubernetes, OpenStack or VMware VMs;
- **HPC sites**: alongside login and compute nodes.

The three methods map onto these platforms:
- **Kubernetes:** any conformant cluster (see "Beyond kind" below).
- **Ansible:** any Ubuntu hosts reachable over SSH. Point
  `ansible/inventory/site` at them.
- **Containers:** any Docker host.

## What is what

There are **three independent deployment methods**. Each has a **dev** and a
**prod** mode, and each takes the **MCP server** as an optional add-on.

| Method | Dev mode | Prod mode | MCP add-on | Entry point |
|---|---|---|---|---|
| **Containers** (Docker Compose) | ✅ | ✅ | `--mcp` flag | `./deploy compose <dev\|prod> …` |
| **Kubernetes** (local kind cluster) | ✅ | ✅ | `--mcp` flag | `./deploy k8s <dev\|prod> …` |
| **Ansible** (Multipass VMs) | ✅ | ✅ | `--mcp` flag | `./deploy ansible <dev\|prod> …` |

What **dev** and **prod** mean is the same in every method:

| | dev | prod |
|---|---|---|
| vScheduler source | live from `src/` (edit → next command uses it) | baked into the image |
| config | `src/vscheduler/lib/config.py` | `docker/config.prod.py` |
| DB users | `report_user`, `guacamole_user`, `lb_user` | `*_prod` users |
| human access | password SSH (`ubuntu`/`ubuntu`) **or** `shell` | `shell` only (no password SSH) |
| MCP default | read-write | read-only |

## Layout

```
deploy                  the one script for all three methods
compose/
  base.yml              shared topology (all services; mcp behind a profile)
  dev.yml  prod.yml     per-mode: image, credentials, ports, source mount
k8s/
  base/                 shared topology (Deployments, Services, PVCs)
  overlays/dev, prod/   per-mode: namespace, image, credentials, NodePorts
  mcp/base, dev, prod/  the MCP add-on, applied into a mode's namespace
docker/                 images used by BOTH compose and k8s
  Dockerfile.dev/.prod/.node/.mcp, entrypoints, config.prod.py
  setup.sh              generates keys + Guacamole schema (deploy runs it)
  db-init/              MariaDB init SQL (used by compose AND k8s)
  seed-demo-data.sql    sample report data (./deploy … seed)
mcp/                    MCP server source + command allowlist
ansible/                the third method: provision.yml + site.yml, roles, inventories
src/                    vScheduler source, consumed read-only (see Scope)
```

## Quick start

Needs Docker Desktop. For Kubernetes also `kind` and `kubectl`; for Ansible
`ansible` and Multipass.

```bash
./deploy compose dev up --mcp      # containers, dev mode, with MCP
./deploy compose dev seed          # optional: sample data so vinfo shows a table
./deploy compose dev shell         # then: vinfo, vmanage, vreport, …
./deploy compose dev status        # services + "MCP: ENABLED/DISABLED"

./deploy k8s dev up --mcp          # same, on Kubernetes
./deploy ansible dev up --mcp      # same, on Multipass VMs
./deploy status                    # overview of every method and mode
```

Every command works with `compose`, `k8s` or `ansible`, and `dev` or `prod`:

| Command | Does |
|---|---|
| `up [--mcp]` | build images, start the stack (plus MCP) |
| `down` | stop, **keep** data |
| `wipe` | stop and **delete** data (DB volume / namespace / VM) |
| `status` | what's running, plus the MCP check (below) |
| `mcp on` / `mcp off` | add/remove MCP without touching the stack |
| `shell` | shell as `ubuntu` inside vscheduler |
| `ssh` | password SSH into vscheduler (dev only) |
| `seed` | load `docker/seed-demo-data.sql` |
| `logs [service]` | follow logs (default `vscheduler`) |

All stacks can run at the same time. They use separate Compose projects
(`vs-dev`, `vs-prod`), namespaces (`vscheduler-dev`, `vscheduler-prod`) or VMs
(`vs-dev`, `vs-prod`). Containers and k8s use these localhost ports; Ansible
uses the same services on each VM's own IP (:22, :8080, :8081, :8930):

| Host port | compose dev | compose prod | k8s dev | k8s prod |
|---|---|---|---|---|
| vscheduler SSH | 2222 | — | 2232 | — |
| Guacamole `/guacamole` | 8080 | 8090 | 8180 | 8190 |
| LibreBooking `/Web/index.php` | 8081 | 8091 | 8181 | 8191 |
| MCP `/mcp` | 8930 | 8931 | 8940 | 8941 |

---

## 1) Containers (Docker Compose)

`compose/base.yml` describes the stack once. `compose/dev.yml` or
`compose/prod.yml` is layered on top. `./deploy` runs
`docker compose -f compose/base.yml -f compose/<mode>.yml …` for you, and you
can run that yourself too.

- **dev**: `vscheduler-dev` image; the repo is bind-mounted at
  `/workspaces/vs` and installed editable at start.
- **prod**: `vscheduler-prod` image with the source and `config.prod.py` baked
  in. No SSH port on the host.

## 2) Kubernetes (kind)

`./deploy k8s <mode> up` creates a local kind cluster named **`vscheduler`**
on first use (context `kind-vscheduler`; other clusters are untouched). It
then builds the images, loads them into kind and applies `k8s/overlays/<mode>`.

- One cluster serves both modes, as namespaces `vscheduler-dev` and
  `vscheduler-prod`.
- **dev** mounts this checkout into the kind node, then into the vscheduler
  pod. `src/` edits are live, exactly as in Compose dev.
- Services are NodePorts that kind maps to the localhost ports above, so no
  `port-forward` is needed.
- MariaDB init reads `docker/db-init/*.sql` directly, the same files Compose
  uses. This is why `./deploy` renders with
  `kubectl kustomize --load-restrictor LoadRestrictionsNone`. A plain
  `kubectl apply -k` refuses files outside the kustomization directory.
- compute nodes are reached as `nodeNN.<namespace>.svc.cluster.local`. The
  overlay sets `VS_DOMAIN` to match.
- Remove everything with `kind delete cluster --name vscheduler`.

**Beyond kind.** `k8s/base` is plain Kubernetes: Deployments, Services,
PVCs on the default StorageClass, ConfigMaps and Secrets. Nothing in it is
kind-specific. To run it on a managed or on-prem cluster:
1. Push the images to a registry and point the overlay's `images:` at it.
2. Swap the NodePort patches for a LoadBalancer or an Ingress.
3. Use a prod-style overlay. The dev overlay's hostPath source mount only
   works on a node that holds the checkout.

## MCP add-on

The MCP server exposes the allowlisted `v*` commands as MCP tools, so Claude
(or any MCP client) can drive vscheduler in natural language. It holds no
vscheduler code: it SSHes into vscheduler and runs one allowlisted command per
call. Details, the tool table and client setup are in
[mcp/README.md](mcp/README.md).

**Toggle:** `up --mcp`, or on a running stack `mcp on` / `mcp off`.
- Compose: the `mcp` service sits behind the Compose profile `mcp`.
- Kubernetes: `k8s/mcp/<mode>` is applied into, or deleted from, the mode's
  namespace.
- Ansible: `roles/mcp` with `-e mcp_state=present|absent` installs or removes
  a `vscheduler-mcp` systemd service on the server VM, and revokes its key
  when removed.

Either way the rest of the stack is untouched.

**Is it enabled?** `./deploy <method> <mode> status` ends with one of:

```
MCP:  DISABLED  (enable: ./deploy k8s dev mcp on)
MCP:  ENABLED  http://localhost:8940/mcp  (10 tools, read-write; SSH to vscheduler OK)
MCP:  ENABLED  http://localhost:8941/mcp  (5 tools, read-only; SSH to vscheduler OK)
```

"ENABLED" is only printed after a real MCP handshake (`initialize` →
`tools/list` → `tools/call vscheduler_health`). The line therefore proves three
things:
- the server answers;
- whether it is read-only or read-write;
- it can actually SSH into vscheduler.

Connect Claude Code:

```bash
claude mcp add --transport http vscheduler http://localhost:8930/mcp   # pick the port
```

The MCP server verifies vscheduler's SSH host key (no `StrictHostKeyChecking=no`).
`docker/setup.sh` generates one fixed host key, bakes it into both vscheduler
images, and writes the matching `docker/keys/known_hosts`.

---

## compute-node login/logout hooks

The nodes carry the production `vis_client_login.py` / `vis_client_logout.py`
hooks, **enabled by default** (`VS_ENABLE_LOGIN_HOOK=1`):
- the login hook is prepended to `/etc/profile`;
- the logout hook is appended to `/etc/bash.bash_logout`.

An *interactive* login on a node therefore calls `vscheduler:65001`, as in
production. vscheduler's own calls into the nodes use non-login exec shells,
so they never trigger the hooks. Set `VS_ENABLE_LOGIN_HOOK=0` on a node to
disable it.

## Credentials (throwaway, local only)

| | dev user / pass | prod user / pass | database |
|--|--|--|--|
| report | `report_user` / `reportpass` | `report_user_prod` / `reportpass_prod` | `report` |
| guacamole | `guacamole_user` / `guacpass` | `guacamole_user_prod` / `guacpass_prod` | `guacamole_db` |
| librebooking | `lb_user` / `lbpass` | `lb_user_prod` / `lbpass_prod` | `librebooking` |
| MariaDB root | `rootpass` | `rootpass_prod` | — |

Guacamole web login: `guacadmin` / `guacadmin`. LibreBooking installer password:
`install_pw_dev` / `install_pw_prod`. Keys in `docker/keys/` are generated per
checkout and git-ignored.

## Scaling compute nodes

1. Add a node:
   - **Compose:** copy a `nodeNN` block in `compose/base.yml`.
   - **Kubernetes:** copy a Deployment+Service pair in `k8s/base/nodes.yaml`.
2. Widen `linux_general_range` / `linux_booking_range` in
   `src/vscheduler/lib/config.py` (dev) and `docker/config.prod.py` (prod).

## Known gaps (data, not deployment)

- **LibreBooking is empty.** Its web installer has never been run, so
  `vmanage` reports `node … is not a resource in booked`.
- **Guacamole has no pool user-groups.** When a node's logout hook fires,
  `mgmt_server` crashes in `modules/guaca/revert_user.py` (`IndexError`)
  looking up the pool group. The socket path itself works; it needs Guacamole
  demo data.

---

## 3) Ansible (Multipass VMs)

`./deploy ansible <mode> up` runs `ansible/provision.yml`, which creates
Multipass VMs: `vs-<mode>` plus `node01..04`, shared by both modes. It
then runs `ansible/site.yml`:
- installs the server, with Guacamole, LibreBooking and MariaDB in Docker
  using the same images and init SQL as above;
- renders `config.py` from the mode's group_vars;
- wires the compute nodes' login hooks.

It also runs on its own with `ansible-playbook`, including against real hosts
via `ansible/inventory/site` (Vaultwarden credentials, external services).
Details are in [ansible/README.md](ansible/README.md).

## Scope and source

This repository holds **deployment mechanisms only**. `src/` tracks the `dev`
branch of
[alanzamani/vScheduler-fork-redevelopment](https://github.com/alanzamani/vScheduler-fork-redevelopment)
(backup on Gitea at `alan/vScheduler-fork-redevelopment`) and is consumed
read-only; nothing here pushes back to it.

Three files carry the containerisation changes and are committed here
(Ansible clones the fork itself and renders its own `config.py`):
- `lib/config.py`: reads `VS_*` env vars;
- the two `socket/vis_client_*.py` hooks: target the `vscheduler` hostname.
