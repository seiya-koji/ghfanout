"""Exception types used throughout ghfanout."""


class GhfanoutError(Exception):
    """Base class for unrecoverable errors that ghfanout reports to the user."""


class ConfigError(GhfanoutError):
    """The config files (ghfanout.yaml / manifest.yaml) are invalid or misplaced."""


class BuildError(GhfanoutError):
    """Failed to compose (build) the base profile."""


class DeployError(GhfanoutError):
    """Failed to distribute (deploy) to GitHub."""
