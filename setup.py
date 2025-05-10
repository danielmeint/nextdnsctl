from setuptools import setup, find_packages

setup(
    name="nextdnsctl",
    version="0.2.0",
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
    python_requires=">=3.6",
    classifiers=[
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
