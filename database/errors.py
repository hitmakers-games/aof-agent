"""Safe errors shared by database clients and gateway entry points."""


class StorageError(Exception):
    """Database failure without credentials, SQL parameters, or private records."""
