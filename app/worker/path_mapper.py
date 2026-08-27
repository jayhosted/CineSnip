from __future__ import annotations

from app.settings import PathMapping


class NoPathMappingError(RuntimeError):
    def __init__(self, plex_path: str):
        super().__init__(
            f"No path mapping configured for '{plex_path}'. "
            f"Check path_mappings in config.yaml."
        )
        self.plex_path = plex_path


def _normalize(path: str) -> str:
    path = path.replace("\\", "/")
    # Windows' extended-length path prefix (\\?\, or \\?\UNC\ for a network
    # share) is a Win32-API-only escape sequence that lets a path exceed the
    # 260-character MAX_PATH limit — Plex reports it verbatim for titles
    # with a long enough combined path/filename, but it's never part of the
    # path an installer would actually write into path_mappings, so it must
    # be stripped before prefix-matching. Confirmed on the real library:
    # without this, several long-named titles (e.g. a Borat film, a handful
    # of multi-episode Avatar: The Last Airbender files) failed to resolve
    # at all despite an otherwise-correct path_mappings entry.
    if path.startswith("//?/UNC/"):
        return "//" + path[len("//?/UNC/") :]
    if path.startswith("//?/"):
        return path[len("//?/") :]
    return path


def resolve_container_path(plex_path: str, mappings: list[PathMapping]) -> str:
    normalized_path = _normalize(plex_path)

    # Longest prefix first, so a more specific mapping wins over a broader
    # one that happens to also match (e.g. two mappings sharing a parent dir).
    sorted_mappings = sorted(mappings, key=lambda m: len(m.plex_prefix), reverse=True)

    for mapping in sorted_mappings:
        normalized_prefix = _normalize(mapping.plex_prefix)
        if normalized_path.startswith(normalized_prefix):
            remainder = normalized_path[len(normalized_prefix) :]
            return mapping.container_path.rstrip("/") + remainder

    raise NoPathMappingError(plex_path)
