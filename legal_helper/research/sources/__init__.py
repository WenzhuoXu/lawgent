"""Research source adapters, one module per jurisdiction or database.

Each module exports ``SOURCES``: a list of ``research.base.Source`` instances.
The registry imports every module here; a module whose import fails is
reported in the catalogue and skipped.
"""
