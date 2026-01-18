import re
from setuptools import setup, find_packages


def get_version():
    """Read version from nextdnsctl/__init__.py without importing."""
    with open("nextdnsctl/__init__.py") as f:
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        if match:
            return match.group(1)
        raise RuntimeError("Version not found")


setup(
    name="nextdnsctl",
    version=get_version(),
    packages=find_packages(),
    install_requires=[
        "requests",
        "click",
    ],
    entry_points={
        "console_scripts": [
            "nextdnsctl = nextdnsctl.nextdnsctl:cli",
        ],
    },
    author="Daniel Meint",
    author_email="pilots-4-trilogy@icloud.com",
    description="A CLI tool for managing NextDNS profiles",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    license="MIT",
    url="https://github.com/danielmeint/nextdnsctl",
    keywords=["nextdns", "cli", "dns", "security", "networking"],
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
