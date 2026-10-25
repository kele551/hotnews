# hotnews — a local news digest for Windows

> Author: **HaiFeng (kele551)** · GitCode (primary): <https://gitcode.com/kele551/hotnews> · GitHub (mirror): <https://github.com/kele551/hotnews> · AtomGit (code mirror): <https://atomgit.com/kele551/hotnews>
> 中文说明见 [../README.md](../README.md)

A **local web app in a single exe** (no installer). It fetches **public headlines and links** from Chinese
news sources, groups them into domestic / international columns, and serves the page at
`http://localhost:8000` in your browser. You can search, browse columns, and click through to each original article.

## What it does

- Aggregates **public titles and links only** — every item links back to the original article
- Domestic / international columns, cross-source de-duplication, in-page search
- Images are proxied locally through a **domain whitelist** (re-checked after redirects)
- Online updates are **signed** (SHA256 + Ed25519) and fail closed
- Logs and caches stay on your machine (`%LOCALAPPDATA%\hotnews`)

## What it deliberately does not do

- ❌ It does not store or republish article bodies (no full-text copy of anyone's content)
- ❌ It does not collect or upload user data
- ❌ It does not run any server-side redistribution, and it is not a news publishing service

## Download and run

- **GitCode (primary, faster in mainland China)**: <https://gitcode.com/kele551/hotnews/releases> — download `hotnews-v1.5.5.exe`
- **GitHub (mirror)**: <https://github.com/kele551/hotnews/releases>

Double-click the exe; the browser opens automatically. Exit from the page's top-right "退出程序" button.

## Build and verify

```powershell
pip install -r requirements.txt
python app.py                      # run from source
python build.py --list             # check the packaging manifest
python tools/preflight_release.py  # release gate — must be all green before publishing
```

## Repository layout & contributing

- Structure and "where do I change X": [ARCHITECTURE.md](ARCHITECTURE.md)
- Claimable tasks with acceptance commands: [../TASKS.md](../TASKS.md)
- Contributing guide: [CONTRIBUTING.md](CONTRIBUTING.md) · Security policy: [SECURITY.md](SECURITY.md) · Compliance: [COMPLIANCE.md](COMPLIANCE.md)
- ⚠ Working on the update chain (`version.json` / `updater.py`)? Read [CONTRIBUTING.md](CONTRIBUTING.md) §5.2 first.
- For AI coding agents: [../AGENTS.md](../AGENTS.md)

## License

MIT — see [../LICENSE](../LICENSE).
