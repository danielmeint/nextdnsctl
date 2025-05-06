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
    description="A CLI tool for managing NextDNS profiles",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    license="MIT",
    url="https://github.com/danielmeint/nextdnsctl",
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
    ],
)
