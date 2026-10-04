#!/usr/bin/env python3
"""Generate the Homebrew formula for a released nextdnsctl version.

    python scripts/homebrew_formula.py 2.0.0 > Formula/nextdnsctl.rb

Resolves the dependency tree by installing that exact release from PyPI into a
throwaway virtualenv, then pins every dependency as a `resource` using its sdist
(Homebrew builds from source). Only the standard library is used.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PACKAGE = "nextdnsctl"
PYTHON_FORMULA = "python@3.13"
SKIP = {PACKAGE, "pip", "setuptools", "wheel"}

TEMPLATE = """\
class Nextdnsctl < Formula
  include Language::Python::Virtualenv

  desc "Command-line tool to manage NextDNS profiles declaratively"
  homepage "https://github.com/danielmeint/nextdnsctl"
  url "{url}"
  sha256 "{sha256}"
  license "MIT"

  depends_on "libyaml"
  depends_on "{python}"
{resources}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/nextdnsctl --version")
    # Without an API key, commands fail cleanly instead of with a traceback.
    output = shell_output("env -u NEXTDNS_API_KEY HOME=#{{testpath}} XDG_CONFIG_HOME=#{{testpath}} " \\
                          "#{{bin}}/nextdnsctl profile list 2>&1", 1)
    assert_match "No API key found", output
  end
end
"""

RESOURCE = """
  resource "{name}" do
    url "{url}"
    sha256 "{sha256}"
  end
"""


def pypi(name: str, version: str | None = None) -> dict:
    path = f"{name}/{version}/json" if version else f"{name}/json"
    with urllib.request.urlopen(f"https://pypi.org/pypi/{path}", timeout=30) as response:
        return json.load(response)


def sdist(name: str, version: str) -> tuple[str, str]:
    for attempt in range(10):
        try:
            files = pypi(name, version)["urls"]
            break
        except urllib.error.HTTPError as e:
            if e.code != 404 or attempt == 9:
                raise
            time.sleep(15)  # a release can take a moment to appear on PyPI
    for file in files:
        if file["packagetype"] == "sdist":
            return file["url"], file["digests"]["sha256"]
    raise SystemExit(f"{name} {version} has no sdist on PyPI")


def resolve(version: str) -> list[tuple[str, str]]:
    """(name, version) of every runtime dependency of nextdnsctl==version."""
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = venv / "bin" / "python"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", f"{PACKAGE}=={version}"],
            check=True,
        )
        listing = subprocess.run(
            [str(python), "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    packages = [(p["name"], p["version"]) for p in json.loads(listing)]
    return sorted(((n, v) for n, v in packages if n.lower() not in SKIP), key=lambda p: p[0].lower())


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: homebrew_formula.py VERSION")
    version = sys.argv[1].lstrip("v")
    url, sha256 = sdist(PACKAGE, version)
    resources = ""
    for name, dep_version in resolve(version):
        dep_url, dep_sha = sdist(name, dep_version)
        resources += RESOURCE.format(name=name.lower(), url=dep_url, sha256=dep_sha)
    sys.stdout.write(TEMPLATE.format(url=url, sha256=sha256, python=PYTHON_FORMULA, resources=resources))


if __name__ == "__main__":
    main()
