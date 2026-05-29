"""Compatibility exports for the shared session files service."""

from app.services.session_files_service import (
    BINARY_EXTENSIONS,
    FileTypeError,
    FilesRemoteTargetUnavailableError,
    FilesSearchError,
    FilesServiceError,
    LocalFilesProvider,
    PathDeniedError,
    PathValidationError,
    RemoteFilesProvider,
    RevisionConflictError,
    SessionFilesService,
    SessionNotFoundError,
    SessionUidRequiredError,
    _is_binary_by_extension,
)

FilesService = SessionFilesService

__all__ = [
    "BINARY_EXTENSIONS",
    "FileTypeError",
    "FilesRemoteTargetUnavailableError",
    "FilesSearchError",
    "FilesService",
    "FilesServiceError",
    "LocalFilesProvider",
    "PathDeniedError",
    "PathValidationError",
    "RemoteFilesProvider",
    "RevisionConflictError",
    "SessionFilesService",
    "SessionNotFoundError",
    "SessionUidRequiredError",
    "_is_binary_by_extension",
]
