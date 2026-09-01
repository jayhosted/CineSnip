from __future__ import annotations

import posixpath

from app.settings import PathMapping


class NoPathMappingError(RuntimeError):
    def __init__(self, source_path: str):
        # Deliberately doesn't echo the raw source_path into the message —
        # a media server's reported path is internal/filesystem detail that
        # must never reach a Discord/web-facing error message
        # (pre-publication audit finding). Kept as an attribute so a caller
        # can still log it server-side for troubleshooting.
        super().__init__(
            "No path mapping configured for this title's source file. "
            "Check path_mappings in config.yaml."
        )
        self.source_path = source_path


def _normalize(path: str) -> str:
    path = path.replace("\\", "/")
    # Windows' extended-length path prefix (\\?\, or \\?\UNC\ for a network
    # share) is a Win32-API-only escape that lets a path exceed the
    # 260-character MAX_PATH limit — Plex reports it verbatim for titles
    # with a long enough combined path/filename, but it's never part of
    # the path an installer would write into path_mappings, so it must be
    # stripped before prefix-matching (see docs/build-notes/plex-integration.md).
    if path.startswith("//?/UNC/"):
        return "//" + path[len("//?/UNC/") :]
    if path.startswith("//?/"):
        return path[len("//?/") :]
    return path


def resolve_container_path(source_path: str, mappings: list[PathMapping]) -> str:
    normalized_path = _normalize(source_path)

    # Longest prefix first, so a more specific mapping wins over a broader
    # one that happens to also match (e.g. two mappings sharing a parent dir).
    sorted_mappings = sorted(mappings, key=lambda m: len(m.path_prefix), reverse=True)

    for mapping in sorted_mappings:
        normalized_prefix = _normalize(mapping.path_prefix)
        if normalized_path.startswith(normalized_prefix):
            remainder = normalized_path[len(normalized_prefix) :]
            container_root = mapping.container_path.rstrip("/")
            resolved = posixpath.normpath(container_root + remainder)
            # The media server's reported path is untrusted input (Section 9)
            # — collapse any ".." segments and confirm the result still sits
            # under the mapped root before returning it, so a compromised or
            # malicious media server can't traverse out of its bind mount
            # (e.g. into /app/.env) via a crafted file path.
            if resolved != container_root and not resolved.startswith(container_root + "/"):
                raise NoPathMappingError(source_path)
            return resolved

    raise NoPathMappingError(source_path)
