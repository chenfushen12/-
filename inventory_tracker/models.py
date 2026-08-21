from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class IssueLevel(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class QualityIssue:
    level: IssueLevel
    code: str
    message: str
    row: int | None = None
    field: str | None = None


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)

    def add(
        self,
        level: IssueLevel,
        code: str,
        message: str,
        *,
        row: int | None = None,
        field: str | None = None,
    ) -> None:
        self.issues.append(QualityIssue(level, code, message, row, field))

    @property
    def blocking(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.level is IssueLevel.BLOCKING]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.level is IssueLevel.WARNING]

    @property
    def infos(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.level is IssueLevel.INFO]

    @property
    def can_commit(self) -> bool:
        return not self.blocking

    def extend(self, other: "QualityReport") -> None:
        self.issues.extend(other.issues)


@dataclass(frozen=True)
class TrackerConfig:
    growth_threshold: float = 0.07
    moh30_threshold: float = 2.5
    moh90_threshold: float = 2.5
    beijing_codes: tuple[str, ...] = ("CB", "CE", "CS", "CT", "CL", "FS")


@dataclass(frozen=True)
class ImportPreview:
    kind: str
    source_path: str
    file_hash: str
    frame: Any
    report: QualityReport
    imported_dates: tuple[date, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotMetadata:
    snapshot_date: date
    template_version_id: int
    inventory_version_id: int | None
    status: str
    calculated_at: datetime
    threshold_growth: float
    threshold_moh30: float
    threshold_moh90: float
    beijing_codes: tuple[str, ...]


@dataclass(frozen=True)
class CommitResult:
    import_id: int
    message: str
    report: QualityReport

