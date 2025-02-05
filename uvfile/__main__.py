import argparse
import os
import shlex

# nosec: We intentionally allow usage of subprocess here, verifying command inputs
import subprocess  # noqa: S404
import sys
import tomllib as toml
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which

from packaging.requirements import Requirement

DEFAULT_UVFILE_PATH = Path("UVFile")
UV_EXE = which("uv") or "uv"


def debug(*args, verbose: bool) -> None:
    """
    Print debug messages to stderr if verbose is True.

    :param args: Arguments to print.
    :param verbose: Whether to print the debug message.
    """
    if verbose:
        print(*args, file=sys.stderr)


@dataclass(frozen=True)
class RequirementSpec:  # noqa: PLW1641
    """
    Represents a single requirement, with all its possible sources.

    Instead of separate git/editable/directory fields, we unify them under `url`
    and a boolean `editable`.
    """

    name: str | None = None
    version: str | None = None
    extras: list[str] = field(default_factory=list)
    url: str | None = None  # Could be a VCS URL, local directory, etc.
    editable: bool = False

    def to_install_args(self, *, as_with: bool = False) -> list[str]:
        """
        Convert the requirement into an install command fragment.

        If `as_with` is True, formats this as a `--with` or `--with-editable` arg.
        """
        if self.url:
            # If we have a URL, handle editable vs. non-editable
            if self.editable:
                # Editable install
                if as_with:
                    return ["--with-editable", f"{self.name}@{self.url}"]
                return [f"{self.name}@{self.url}", "--editable"]
            # Non-editable install from a URL
            if as_with:
                return ["--with", f"{self.name}@{self.url}"]
            return [f"{self.name}@{self.url}"]

        # No URL: typical requirement with optional extras and version
        base = self.name or ""
        if self.extras:
            base += f"[{','.join(self.extras)}]"
        if self.version:
            base += self.version

        return ["--with", base] if as_with else [base]

    def __eq__(self, other: object) -> bool:
        """Check if two requirements match in all aspects."""
        if not isinstance(other, RequirementSpec):
            return NotImplemented
        return (
            self.name == other.name
            and self.version == other.version
            and sorted(self.extras) == sorted(other.extras)
            and self.url == other.url
            and self.editable == other.editable
        )


@dataclass(frozen=True)
class Tool:  # noqa: PLW1641
    """Represents a single tool with all its dependencies and metadata."""

    primary: RequirementSpec
    additional: list[RequirementSpec] = field(default_factory=list)
    python_version: str | None = None

    def install_args(self, *, reinstall: bool = False) -> list[str]:
        """
        Construct the full installation command for this tool.

        :param reinstall: Whether to force reinstallation of this tool.
        :return: A list of command arguments for `uv tool install`.
        """
        command = self.primary.to_install_args()
        for req in self.additional:
            command.extend(req.to_install_args(as_with=True))
        if self.python_version:
            command.extend(["--python", self.python_version])
        if reinstall:
            command.append("--reinstall")
        return command

    def __eq__(self, other: object) -> bool:
        """Check if two tools match, including their dependencies."""
        if not isinstance(other, Tool):
            return NotImplemented
        if self.primary != other.primary:
            return False
        if sorted(self.additional, key=lambda r: (r.name or "", r.version or "")) != sorted(
            other.additional, key=lambda r: (r.name or "", r.version or "")
        ):
            return False
        return self.python_version == other.python_version


def parse_uv_receipt(receipt_path: Path) -> Tool | None:
    """
    Parse a uv-receipt.toml file into a Tool object.

    :param receipt_path: Path to the uv-receipt.toml file.
    :return: A Tool object if parsing is successful, else None.
    """
    if not receipt_path.exists():
        return None
    receipt = toml.loads(receipt_path.read_text())
    requirements = receipt["tool"]["requirements"]
    primary_req = parse_requirement(requirements[0])
    additional_reqs = [parse_requirement(req) for req in requirements[1:]]
    python_version = receipt["tool"].get("python")
    return Tool(
        primary=primary_req,
        additional=additional_reqs,
        python_version=python_version,
    )


def parse_requirement(requirement: dict) -> RequirementSpec:
    """
    Parse a single requirement dictionary from uv-receipt.toml.

    We unify git/directory/other URL types into `url`, and store editable as bool.
    """
    # If multiple are present, pick in priority order:
    url = requirement.get("git") or requirement.get("directory") or requirement.get("editable")
    editable = bool(requirement.get("editable"))

    return RequirementSpec(
        name=requirement.get("name"),
        version=requirement.get("specifier"),
        extras=requirement.get("extras", []),
        url=url,
        editable=editable,
    )


def get_installed_tools(uv_tools_dir: Path) -> list[Tool]:
    """
    Fetch the list of installed tools and their versions using.

    `uv tool list --show-paths`.
    """
    result = subprocess.run(  # noqa: S603
        [UV_EXE, "tool", "list", "--show-paths"],
        text=True,
        capture_output=True,
        check=True,
    )
    tools: list[Tool] = []
    for line in result.stdout.strip().splitlines():
        if line.startswith("-"):
            continue
        # The line looks like: 'mypkg 1.2.3 (/path/to/mypkg)'
        # name_version is not needed, so skip with _
        _, path = line.rsplit(" ", 1)
        receipt_path = Path(path.strip("()")) / "uv-receipt.toml"
        receipt = parse_uv_receipt(receipt_path)
        if receipt:
            tools.append(receipt)
    return tools


def collect_tool_metadata(uvfile_path: Path) -> list[Tool]:
    """
    Parse the UVFile and return a list of tools.

    :param uvfile_path: Path to the UVFile.
    :return: A list of Tool objects described in the file.
    """
    tools: list[Tool] = []
    if not uvfile_path.exists():
        return tools

    for raw_line in uvfile_path.read_text().splitlines():
        line_stripped = raw_line.strip()
        if not line_stripped or line_stripped.startswith("#"):
            continue

        requirement, *extra_args = shlex.split(line_stripped)
        req = Requirement(requirement)

        parser = argparse.ArgumentParser()
        parser.add_argument("--python")
        parser.add_argument("--editable", action="store_true")
        parser.add_argument("--with", action="append", dest="additional", default=[])
        parser.add_argument(
            "--with-editable",
            action="append",
            dest="additional_editable",
            default=[],
        )

        namespace = parser.parse_args(extra_args)

        # Primary requirement
        primary = RequirementSpec(
            name=req.name,
            version=str(req.specifier) if req.specifier else None,
            extras=list(req.extras),
            url=req.url,
            editable=namespace.editable,
        )

        # Additional requirements
        additional: list[RequirementSpec] = []
        for requirement_str in namespace.additional:
            additional_req = Requirement(requirement_str)
            additional.append(
                RequirementSpec(
                    name=additional_req.name,
                    version=str(additional_req.specifier) if additional_req.specifier else None,
                    extras=list(additional_req.extras),
                    url=additional_req.url,
                    editable=False,
                )
            )
        for requirement_str in namespace.additional_editable:
            additional_req = Requirement(requirement_str)
            additional.append(
                RequirementSpec(
                    name=additional_req.name,
                    version=str(additional_req.specifier) if additional_req.specifier else None,
                    extras=list(additional_req.extras),
                    url=additional_req.url,
                    editable=True,
                )
            )

        tools.append(
            Tool(
                primary=primary,
                additional=additional,
                python_version=namespace.python,
            )
        )
    return tools


def write_uvfile(tools: list[Tool], uvfile_path: Path) -> None:
    """
    Write the UVFile with the list of tools and their metadata.

    :param tools: A list of Tool objects to serialize.
    :param uvfile_path: Destination path for the UVFile.
    """
    lines: list[str] = []
    for tool in tools:
        command = tool.install_args()
        lines.append(" ".join(command))
    uvfile_path.write_text(
        "# UVFile: Auto-generated file to track installed uv tools\n\n" + "\n".join(lines)
    )


def install_from_uvfile(
    *,
    force: bool,
    clean: bool,
    pin: bool,
    dry_run: bool,
    verbose: bool,
    uvfile_path: Path,
) -> None:
    """
    Install dependencies listed in the UVFile.

    :param force: If True, reinstall all tools from UVFile.
    :param clean: If True, remove tools not in the UVFile.
    :param pin: If True, reinstall if the installed tool differs from the UVFile spec.
    :param dry_run: If True, only show what would be done.
    :param verbose: If True, print debug messages.
    :param uvfile_path: Path to the UVFile.
    """
    installed_tools = get_installed_tools(Path.home() / ".local/share/uv/tools")
    uvfile_tools = collect_tool_metadata(uvfile_path)

    # Handle clean mode (remove unlisted tools)
    if clean:
        installed_names = {t.primary.name for t in installed_tools}
        uvfile_names = {t.primary.name for t in uvfile_tools}
        tools_to_remove = installed_names - uvfile_names
        for tool_name in tools_to_remove:
            command = [UV_EXE, "tool", "uninstall", tool_name]
            if dry_run:
                print(f"Would run: {' '.join(('uv', *command[1:]))}")
            else:
                debug(f"Uninstalling: {tool_name}", verbose=verbose)
                subprocess.run(command, check=True)  # noqa: S603

    # Install or skip tools from the UVFile
    for tool in uvfile_tools:
        matching_installed_tool = next(
            (t for t in installed_tools if t.primary.name == tool.primary.name),
            None,
        )
        reinstall = force or (pin and tool != matching_installed_tool)
        needs_install = matching_installed_tool is None or reinstall

        if not needs_install:
            debug(f"Skipping {tool.primary.name}, already installed.", verbose=verbose)
            continue

        command = [UV_EXE, "tool", "install", *tool.install_args(reinstall=reinstall)]

        if dry_run:
            print(f"Would run: {' '.join(('uv', *command[1:]))}")
        else:
            debug(f"Installing: {tool.primary.name}", verbose=verbose)
            subprocess.run(command, check=True)  # noqa: S603


def init_uvfile(*, force: bool, uvfile_path: Path) -> None:
    """
    Generate a new UVFile from currently installed tools.

    :param force: Overwrite existing UVFile without prompting.
    :param uvfile_path: Where to write the UVFile.
    """
    if uvfile_path.exists() and not force:
        confirmation = input(f"{uvfile_path} already exists. Overwrite? [y/N]: ").strip().lower()
        if confirmation != "y":
            print("Aborted.")
            return

    installed_tools = get_installed_tools(Path.home() / ".local/share/uv/tools")
    write_uvfile(installed_tools, uvfile_path)
    print(f"UVFile initialized with {len(installed_tools)} tools.")


def generate_uvfile_env_script() -> str:
    """
    Generate the Bash script for wrapping the uv command.

    :return: Bash script contents as a string.
    """
    return r"""
uv () {
  local exe=("command" "uv")

  # Check if uvfile exists
  if ! type uvfile >/dev/null 2>&1; then
    "${exe[@]}" "$@"
    return
  fi

  local nargs=0
  local cmd=$1
  for arg in "$@"; do
    if [[ ! "$arg" =~ ^- ]]; then
      ((nargs++))
    fi
  done

  case "$cmd" in
    tool)
      local cmd2=$2
      if [[ "$cmd2" =~ ^(install|upgrade|uninstall)$ ]]; then
        "${exe[@]}" "$@"
        local ret=$?
        if [ $ret -eq 0 ]; then
          uvfile init --force
        fi
        return $ret
      fi
      ;;
    file)
      shift
      uvfile "$@"
      return $?
      ;;
  esac

  "${exe[@]}" "$@"
}

# Enable uv command completion
if type -a _uv >/dev/null 2>&1; then
  _uv_completion_wrap() {
    local cword=$COMP_CWORD
    local cur=${COMP_WORDS[cword]}
    local cmd=${COMP_WORDS[1]}

    if [ "$cmd" = "tool" ]; then
      COMPREPLY=($(compgen -W "install upgrade list uninstall" -- "$cur"))
    else
      _uv
    fi
  }
  complete -o bashdefault -o default -F _uv_completion_wrap uv
fi
"""


def env() -> None:
    """Handle the uvfile env command to output the wrapper script."""
    print(generate_uvfile_env_script())


def main() -> None:
    """Main entry point: parse args and dispatch subcommands."""
    parser = argparse.ArgumentParser(
        description="Manage uv tools with a UVFile.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--uvfile",
        type=Path,
        default=Path(os.getenv("UVFILE_PATH", DEFAULT_UVFILE_PATH)),
        help=("Path to the UVFile (default: UVFile in the current directory or $UVFILE_PATH)."),
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("env", help="Generate a Bash script for wrapping the uv command.")

    # Init command
    init_parser = subparsers.add_parser(
        "init", help="Generate a UVFile from currently installed tools."
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the UVFile if it already exists.",
    )

    # Sync command
    sync_parser = subparsers.add_parser(
        "sync",
        help="Install dependencies from the UVFile.",
    )
    sync_parser.add_argument("--force", action="store_true", help="Reinstall tools from UVFile.")
    sync_parser.add_argument(
        "--clean", action="store_true", help="Remove tools not listed in the UVFile."
    )
    sync_parser.add_argument(
        "--pin", action="store_true", help="Match exact versions listed in UVFile."
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be installed/uninstalled."
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()
    if args.command == "init":
        init_uvfile(force=args.force, uvfile_path=args.uvfile)
    elif args.command == "sync":
        install_from_uvfile(
            force=args.force,
            clean=args.clean,
            pin=args.pin,
            dry_run=args.dry_run,
            verbose=args.verbose,
            uvfile_path=args.uvfile,
        )
    elif args.command == "env":
        env()


if __name__ == "__main__":
    main()
