from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal


ReportMode = Literal["range", "as_of"]


@dataclass(slots=True, frozen=True)
class Metric:
    label: str
    value: str
    tone: Literal["default", "warning"] = "default"


@dataclass(slots=True, frozen=True)
class TableSection:
    title: str
    columns: list[str]
    rows: list[dict[str, object]]
    empty_message: str = "No data"


@dataclass(slots=True, frozen=True)
class DashboardSummary:
    business_name: str
    ledger_dir: Path
    as_of: date
    metrics: list[Metric] = field(default_factory=list)
    sections: list[TableSection] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ReportView:
    key: str
    title: str
    mode: ReportMode
    start: date | None = None
    end: date | None = None
    as_of: date | None = None
    metrics: list[Metric] = field(default_factory=list)
    sections: list[TableSection] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class StatusView:
    as_of: date
    packet_year: int
    sections: list[TableSection] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ExportResult:
    title: str
    output_dir: Path
    files: list[str] = field(default_factory=list)
    zip_path: Path | None = None


@dataclass(slots=True, frozen=True)
class HelpCommand:
    title: str
    description: str
    command: str
