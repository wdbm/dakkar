"""
Maintain URI navigation history.
"""

from __future__ import annotations


class LocationHistory:
    """
    Maintain browser-style back and forward history without duplicate pushes.
    """

    def __init__(self) -> None:
        self._uris: list[str] = []
        self._position = -1

    @property
    def current(self) -> str | None:
        if self._position < 0:
            return None
        return self._uris[self._position]

    @property
    def can_go_back(self) -> bool:
        return self._position > 0

    @property
    def can_go_forward(self) -> bool:
        return 0 <= self._position < len(self._uris) - 1

    def push(self, uri: str) -> bool:
        """
        Add a newly visited URI and discard the previous forward branch.
        """

        if not uri:
            raise ValueError("A history URI must not be empty.")
        if self.current == uri:
            return False
        del self._uris[self._position + 1 :]
        self._uris.append(uri)
        self._position = len(self._uris) - 1
        return True

    def back(self) -> str | None:
        if not self.can_go_back:
            return None
        self._position -= 1
        return self._uris[self._position]

    def forward(self) -> str | None:
        if not self.can_go_forward:
            return None
        self._position += 1
        return self._uris[self._position]

    def snapshot(self) -> tuple[tuple[str, ...], int]:
        return tuple(self._uris), self._position

    def restore(self, uris: tuple[str, ...], position: int) -> None:
        """
        Replace the current history with a previously captured snapshot.
        """

        if not uris:
            raise ValueError("A restored history must contain at least one URI.")
        if any(not uri for uri in uris):
            raise ValueError("A history URI must not be empty.")
        if position < 0 or position >= len(uris):
            raise ValueError("The restored history position is outside its URI list.")
        self._uris = list(uris)
        self._position = position
