# uvfile

Tools management for [uv](https://github.com/astral-sh/uv). Think Brewfile, but for Python CLI
tools.

> **Note:** Most of this tool, was written
> by [Pseudocoder](https://chatgpt.com/g/g-Z5tFTQt5G-pseudocoder) and the most of the readme was
> written by [Claude](https://anthropic.com/claude) based on my design and requirements. It works,
> but the implementation has room for improvement. I focused on the interface and user experience
> while delegating the implementation details to the LLM.

```shell
uv tool install uvfile
```


## Why?

You want to track and sync your globally installed Python CLI tools across different environments,
but `uv tool install` doesn't provide a way to save and restore tool states. With `uvfile` you can:

- Initialize a manifest of your system-wide Python tools
- Sync tools across different machines
- Track tool versions and their extras
- Share common tooling within teams while allowing personal tools


## Usage

### Initialize UVFile from currently installed tools
```shell
uvfile init
```

### Install tools from UVFile

```shell
uvfile sync                  # Install missing tools
uvfile sync --pin            # Install and match exact versions
uvfile sync --force          # Force reinstall all tools in UVFile
uvfile sync --clean          # Remove tools unlisted in UVFile
uvfile sync --pin --clean
uvfile sync --force --clean
```
### Combine `sync` options
```shell
uvfile sync --pin --clean
uvfile sync --force --clean
```

### Preview changes
Add `--dry-run` to `sync` commands
```shell
uvfile sync --force --clean --dry-run
```
```shell
Would run: uv tool uninstall cowsay
Would run: uv tool install aider-chat --python python3.12 --reinstall
Would run: uv tool install cookiecutter --reinstall
Would run: uv tool install lefthook --reinstall
Would run: uv tool install poetry --reinstall
Would run: uv tool install project-mapper@https://github.com/blakesims/project-mapper.git --reinstall
Would run: uv tool install pypyp --reinstall
Would run: uv tool install ruff --reinstall
Would run: uv tool install type-ignore@https://github.com/cleder/type-ignore.git --reinstall
Would run: uv tool install uvfile --reinstall
```

## Shell Integration

Add this to your shell config:

```shell
if command -v uvfile >/dev/null 2>&1; then
  source <(uvfile env)
fi
```

This will:

- Auto-update UVFile after `uv tool install/upgrade`
- Add `uv file` command as an alias for uvfile
- Add shell completions

## Use Cases

### Multiple Machines

Keep tools in sync across different computers:

#### On main machine

```shell
uvfile init
git add UVFile
git commit -m "Add my local uv tools via UVFile"
git push
````

#### On other machines

```shell
git pull
uvfile sync --pin    # Install while preserving existing tools
```

### Team Standards

Share common tooling while allowing personal tools:

```shell
uvfile --uvfile ~/work/project/UVFile sync --pin   # Install team tools, keep personal ones
```

### CI/CD

Ensure consistent tooling in CI:

```yaml
steps:
  - uses: actions/checkout@v4
  - run: |
      curl -L https://github.com/astral-sh/uv/releases/latest/download/uv-installer.sh | sh
      uvfile sync
```

## UVFile Format

UVFile uses a simple format that mirrors `uv tool install` commands:

```
ruff>=0.2.0 --python 3.11
mypy==1.8.0
black --with tomli --with typing-extensions>=4.0
pdm --with-editable ./dev-tools/pdm-plugin
```

Supports:

- Version constraints
- Python version specification
- Additional dependencies via `--with`
- Editable installs via `--with-editable`
- Git repositories
- Local directories

## Environment Variables

- `UVFILE_PATH`: Override the default UVFile location (defaults to `./UVFile`)

## Limitations

- Doesn't provide a way to merge different UVFiles: you can use multiple UVFiles via different sync
  options, but there's no way to partially update UVFile, except for manual edits, of course (PRs
  welcome).
- Doesn't support all extra arguments (PRs welcome).