from importlib.metadata import version

# Set at build time from the git tag (see [tool.hatch.version] in pyproject.toml).
__version__ = version("shakedown")
