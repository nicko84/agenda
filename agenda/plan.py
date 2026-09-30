"""The order Nick hears things in: project by project, feature by feature, like a meeting.

Within a feature: blocking items, answers to his own questions, quick sign-offs (high-confidence
recommendations he can approve together), things to discuss, hands-on tasks, then updates. Projects and
features with blocking items go first, then the busiest. Items nobody has filed yet go last, under
"Not filed yet", so a gap in filing is visible rather than hidden.

The page and the voice agent both use this, so what Nick sees and what he hears are in the same order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import Agenda, Item, queue_rank

UNFILED = "Not filed yet"
BUCKETS = ("blocking", "answers", "quick", "discuss", "tasks", "updates")
BUCKET_TITLES = {
    "blocking": "Blocking",
    "answers": "Answers to your questions",
    "quick": "Quick sign-offs",
    "discuss": "To discuss",
    "tasks": "Needs you at a computer",
    "updates": "Updates",
}


def bucket(item: Item) -> str:
    if item.blocking:
        return "blocking"
    if item.from_nick or item.answers_nicks_question:
        return "answers"
    if item.kind == "task":
        return "tasks"
    if item.kind == "fyi":
        return "updates"
    if item.quick and item.recommendation and not item.nick_has_answered:
        return "quick"
    return "discuss"


@dataclass
class Feature:
    name: str
    items: list[Item] = field(default_factory=list)

    def buckets(self) -> list[tuple[str, list[Item]]]:
        grouped = {name: [] for name in BUCKETS}
        for item in self.items:
            grouped[bucket(item)].append(item)
        return [(name, sorted(items, key=_order)) for name, items in grouped.items() if items]

    @property
    def blocking(self) -> int:
        return sum(i.blocking for i in self.items)


@dataclass
class Project:
    name: str
    brief: str | None
    features: list[Feature] = field(default_factory=list)

    @property
    def items(self) -> list[Item]:
        return [i for f in self.features for i in f.items]

    @property
    def blocking(self) -> int:
        return sum(f.blocking for f in self.features)


def plan(agenda: Agenda, items: list[Item] | None = None) -> list[Project]:
    items = agenda.queue() if items is None else items
    projects: dict[str, dict[str, Feature]] = {}
    for item in items:
        name = item.project or UNFILED
        feature = item.feature or ("General" if item.project else UNFILED)
        projects.setdefault(name, {}).setdefault(feature, Feature(feature)).items.append(item)
    out = []
    for name, features in projects.items():
        known = agenda.project(name) if name != UNFILED else None
        ordered = sorted(features.values(), key=lambda f: (-f.blocking, -len(f.items), f.name.lower()))
        out.append(Project(known.name if known else name, known.brief if known else None, ordered))
    return sorted(out, key=lambda p: (p.name == UNFILED, -p.blocking, -len(p.items), p.name.lower()))


def ordered_ids(projects: list[Project]) -> list[int]:
    return [i.id for p in projects for f in p.features for _, items in f.buckets() for i in items]


def _order(item: Item) -> tuple:
    return (queue_rank(item), item.waiting_since or "", item.id)
