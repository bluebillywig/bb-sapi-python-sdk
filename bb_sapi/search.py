"""
Filtersets: the structure the OVP builds in its filter UI, and the shape SAPI's
``filterset`` parameter takes.

Deliberately NOT compiled to a Solr query here. SAPI compiles filtersets itself,
using the same ``SearchRequestHelper`` that serves the OVP, so compiling
client-side would be a second implementation of semantics the server owns — free
to drift, with a failure mode that is invisible: a filter SAPI cannot read is
ignored, and the response is HTTP 200 carrying neither ``numfound`` nor
``items``, which reads exactly like an empty library.

Mirrors ``app/services/filter-set.types.ts`` in OVP6, so a filterset moves
between the UI, the API and any SDK unchanged.

Server-side quirks a caller inherits (the compiler is formatengine's):

- A filter whose value is the string '0' is dropped by the backend's
  empty-value guard, so "views is 0" cannot be expressed as a filterset.
- In values, + becomes a space and " is stripped before compilation.
- An unknown FIELD is not an error: it queries a non-existent index field and
  returns numfound=0 — a typo'd field name looks like an empty library.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Literal, Optional, Sequence, Union

FilterOperator = Literal[
    "is",
    "isNot",
    "isAnyOf",
    "isNotAnyOf",
    "isEmpty",
    "isNotEmpty",
    "contains",
    "containsAnyOf",
    "containsAllOf",
    "doesNotContain",
    "doesNotContainAnyOf",
    "isBefore",
    "isAfter",
    "isSmallerThan",
    "isGreaterThan",
    "isInTheLast",
    "isNotInTheLast",
]

#: Operators that test presence, so they mean something without a value.
VALUELESS_OPERATORS = frozenset({"isEmpty", "isNotEmpty"})

#: Numbers and booleans are accepted and normalised to strings on the wire:
#: the backend's compiler mangles a JSON true into "1" (which matches
#: nothing, silently) and its empty-value guard drops false outright, while
#: numbers work but only ever appear as strings in what OVP6 sends.
FilterScalar = Union[str, int, float, bool]
FilterValue = Union[FilterScalar, Sequence[FilterScalar], None]


def _normalize_scalar(value: FilterScalar) -> str:
    # bool first: bool subclasses int, so isinstance(True, int) is True.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Filter:
    """One condition inside a group."""

    __slots__ = ("field", "operator", "value", "type")

    def __init__(
        self,
        field: str,
        operator: FilterOperator,
        value: FilterValue = None,
        type: Optional[str] = None,
    ) -> None:
        self.field = field
        self.operator = operator
        self.value = value
        self.type = type

    def has_value(self) -> bool:
        """A filter with nothing to match on is dropped, except presence tests."""
        if self.operator in VALUELESS_OPERATORS:
            return True
        values = self.value if isinstance(self.value, (list, tuple)) else [self.value]
        return any(
            isinstance(v, (int, float)) or (isinstance(v, str) and v.strip() != "")
            for v in values
        )

    def to_dict(self) -> dict[str, Any]:
        """Normalise to the wire shape: what the OVP sends and the backend reads."""
        payload: dict[str, Any] = {"field": self.field, "operator": self.operator}
        if self.operator in VALUELESS_OPERATORS:
            # The backend's compiler skips ANY filter whose value is empty —
            # presence tests included — so isEmpty/isNotEmpty must carry a
            # placeholder or they silently never fire (verified live: a bare
            # isEmpty returned the full unfiltered publication). '*' is what
            # OVP6 sends ("backend needs a value to work"), and it overrides
            # whatever the caller supplied.
            payload["value"] = "*"
        elif isinstance(self.value, (list, tuple)):
            payload["value"] = [
                _normalize_scalar(v) for v in self.value if isinstance(v, (str, int, float))
            ]
        elif isinstance(self.value, (str, int, float)):
            payload["value"] = _normalize_scalar(self.value)
        if self.type:
            payload["type"] = self.type
        return payload


class FilterSet:
    """
    A filterset: groups of conditions.

    Groups are AND-ed, filters within a group OR-ed::

        filter_set = (
            FilterSet()
            .where("status", "is", "published")
            .where("title", "contains", "koert")
        )

        client.mediaclip.search(filter_set)
    """

    __slots__ = ("_groups",)

    def __init__(self, groups: Optional[Sequence[Sequence[Filter]]] = None) -> None:
        self._groups: list[list[Filter]] = [list(group) for group in (groups or [])]

    @classmethod
    def from_data(cls, data: Any) -> "FilterSet":
        """
        Build from raw data: a bare list of groups, or the ``SearchRequest``
        envelope OVP6 sends.
        """
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict) and data.get("type") == "SearchRequest":
            data = data.get("filterSet") or []
        if not isinstance(data, list):
            return cls()

        groups: list[list[Filter]] = []
        for group in data:
            if not isinstance(group, dict):
                continue
            raw_filters = group.get("filters", [])
            if not isinstance(raw_filters, list):
                raw_filters = []
            filters = []
            for f in raw_filters:
                if not isinstance(f, dict):
                    continue
                operator = f.get("operator")
                if not isinstance(operator, str) or operator == "":
                    # A filter without an operator is junk — skipping it beats
                    # silently guessing "is", which would invent a condition
                    # the author never wrote.
                    continue
                filters.append(
                    Filter(str(f.get("field", "")), operator, f.get("value"), f.get("type") or None)
                )
            groups.append(filters)
        return cls(groups)

    def where(
        self,
        field: str,
        operator: FilterOperator,
        value: FilterValue = None,
        type: Optional[str] = None,
    ) -> "FilterSet":
        """Add a condition as its own group, so it is AND-ed with the rest."""
        return self.and_group(Filter(field, operator, value, type))

    def and_group(self, *filters: Filter) -> "FilterSet":
        """Add several conditions as one group, so they are OR-ed with each other."""
        return FilterSet([*self._groups, list(filters)])

    def to_list(self) -> list[dict[str, Any]]:
        """The wire format: what SAPI's ``filterset`` parameter expects."""
        groups: list[dict[str, Any]] = []
        for group in self._groups:
            filters = [f.to_dict() for f in group if f.has_value()]
            if filters:
                groups.append({"filters": filters})
        return groups

    def to_json(self) -> str:
        return json.dumps(self.to_list(), separators=(",", ":"))

    def is_empty(self) -> bool:
        return not self.to_list()

    def __iter__(self) -> Iterable[dict[str, Any]]:
        return iter(self.to_list())

    def __bool__(self) -> bool:
        return not self.is_empty()
