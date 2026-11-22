"""Shared exception types for the pipeline scripts.

Importable loaders raise these instead of calling ``sys.exit`` so that they are
safe to use from worker threads and web handlers. CLI entry points convert them
back to ``sys.exit("Error: ...")`` so command-line behaviour is unchanged.
"""


class PipelineError(Exception):
    """Base class for recoverable pipeline errors."""


class InputError(PipelineError):
    """The user-supplied input (workbook, sheet, columns, terms file) is unusable."""


class ConfigError(PipelineError):
    """Required configuration (e.g. SCRAPINGBEE_API_KEY) is missing."""
