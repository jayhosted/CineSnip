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
    return path.replace("\\", "/")


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
