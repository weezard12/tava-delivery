"""Who is acting: a workspace account, or the service itself for background work."""

from dataclasses import dataclass
from typing import Literal

SYSTEM_ACTOR = "system"


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    role: Literal["admin", "user"] = "user"

    @property
    def admin(self) -> bool:
        return self.role == "admin"


SYSTEM = Principal(SYSTEM_ACTOR, "admin")
