"""
investigation.py
Persistent state for an iterative OSINT investigation: seeds accumulated
across cycles, hits found, and which hits were selected as pivots.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Lead:
    """A single accepted pivot point: a hit the user chose to fold back
    into the seed pool."""
    source: str          # e.g. "hit:username_a:github"
    value: str            # the actual new seed token (username/email/loc)
    kind: str             # "username" | "email" | "location" | "name" | "handle"
    added_cycle: int
    note: str = ""


@dataclass
class CycleRecord:
    """What happened in one discovery cycle — kept for audit trail."""
    cycle: int
    timestamp: str
    seeds_snapshot: dict[str, Any]
    candidates_checked: int
    hits_found: int
    pivots_selected: list[str] = field(default_factory=list)


@dataclass
class Investigation:
    case_name: str
    created: str
    cycle: int = 0
    names: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    birth_year: str | None = None
    leads: list[Lead] = field(default_factory=list)
    all_hits: dict[str, Any] = field(default_factory=dict)
    history: list[CycleRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Investigation":
        d = dict(d)
        d["leads"] = [Lead(**l) for l in d.get("leads", [])]
        d["history"] = [CycleRecord(**h) for h in d.get("history", [])]
        return cls(**d)


def load_or_create(path: str | Path, case_name: str) -> Investigation:
    p = Path(path)
    if p.exists():
        return Investigation.from_dict(json.loads(p.read_text(encoding="utf-8")))
    return Investigation(case_name=case_name, created=datetime.now(timezone.utc).isoformat())


def save(investigation: Investigation, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(investigation.to_dict(), indent=2), encoding="utf-8")