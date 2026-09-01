"""Logging configuration for the ov02c10_camera package."""

import logging


def configure_logging(verbose: bool = False) -> None:
    """Configure root logging format/level for the whole package.

    Args:
        verbose: If True, set the root logger to DEBUG instead of INFO.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
