# relduty-deployer

A terminal dashboard for the Mozilla Release Engineering deploy rotation. It
fetches every RelEng project it knows about, shows how far each environment is
behind, and performs the fast-forward push that deploys it.

## Install

```bash
uv sync
uv run relduty-deployer
```

## Deploy matrix

| project | source branch | staging | production | strategy |
|---|---|---|---|---|
| scriptworker-scripts | `master` | `dev` | `production` | branch push |
| shipit | `main` | `dev` | `production` | branch push |
| k8s-autoscale | `main` | `dev` | `production` | branch push |
| tooltool | `master` | `staging` | `production` | branch push |
| balrog | `main` | GitHub release | ArgoCD | balrog (status only) |

## Configuration

Machine-local settings live in `~/.config/relduty-deployer-config.json` — the
checkout path, the git remote to push to, and whether the project is shown. The
deploy knowledge itself (source branch, target branches, strategy) is part of the
program, not the config file.

## Safety

The tool only ever fast-forwards. If a deploy branch has commits the source
branch does not, its button turns red and cannot be pressed — resolve that by
hand. `--force` is never passed.
