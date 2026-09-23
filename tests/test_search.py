"""
The filterset wire format.

These pin the SHAPE, because the shape is the contract — the semantics are
SAPI's to own. Equivalence was checked against a live publication of 5777 clips:
sending a filterset and sending a hand-compiled ``fq`` return identical counts
(published 4361, title contains "koert" 1, hasInteractivity 868, published AND
video 3679).
"""
import json
from urllib.parse import parse_qs, urlparse

import responses as resp_lib

import pytest

from bb_sapi import KNOWN_OPERATORS, Filter, FilterSet, SapiClient

BASE_URL = "https://test.bbvms.com"
SECRET = "490-deadbeef"


def make_client() -> SapiClient:
    return SapiClient(BASE_URL, SECRET, timeout=5)


class TestFilterSet:
    def test_one_condition_becomes_a_group_of_one(self):
        assert FilterSet().where("status", "is", "published").to_list() == [
            {"filters": [{"field": "status", "operator": "is", "value": "published"}]}
        ]

    def test_each_where_gets_its_own_group_so_they_are_anded(self):
        filter_set = FilterSet().where("status", "is", "published").where("mediatype", "is", "video")
        assert len(filter_set.to_list()) == 2

    def test_filters_passed_together_share_a_group_so_they_are_ored(self):
        filter_set = FilterSet().and_group(
            Filter("status", "is", "published"),
            Filter("status", "is", "draft"),
        )
        groups = filter_set.to_list()
        assert len(groups) == 1
        assert len(groups[0]["filters"]) == 2

    def test_an_entity_type_is_carried_but_omitted_when_absent(self):
        with_type = FilterSet().where("status", "is", "published", "mediaclip").to_list()
        without_type = FilterSet().where("status", "is", "published").to_list()

        assert with_type[0]["filters"][0]["type"] == "mediaclip"
        assert "type" not in without_type[0]["filters"][0]

    def test_several_values_are_carried_as_a_list(self):
        filter_set = FilterSet().where("status", "isAnyOf", ["published", "draft"])
        assert filter_set.to_list()[0]["filters"][0]["value"] == ["published", "draft"]

    def test_a_filter_with_nothing_to_match_on_is_dropped(self):
        assert FilterSet().where("status", "is", "   ").to_list() == []
        assert FilterSet().where("status", "is").is_empty()

    def test_presence_operators_carry_the_placeholder_the_backend_requires(self):
        # The backend's compiler skips ANY filter whose value is empty —
        # presence tests included — so a bare isEmpty silently never fires
        # (verified live: it returned the full unfiltered publication). OVP6
        # sends '*' with the comment "backend needs a value to work".
        filter_set = FilterSet().where("author", "isEmpty")

        assert not filter_set.is_empty()
        assert filter_set.to_list()[0]["filters"][0]["value"] == "*"

    def test_the_placeholder_overrides_whatever_value_a_caller_supplied(self):
        filter_set = FilterSet().where("author", "isNotEmpty", "anything")

        assert filter_set.to_list()[0]["filters"][0]["value"] == "*"

    def test_numbers_and_booleans_are_normalised_to_backend_strings(self):
        # Verified live: a JSON number works, but a JSON boolean gets mangled
        # into "1" by the backend and matches NOTHING (hasInteractivity true as
        # a boolean returned 0 results; as the string 'true', 868). Previously
        # these values were silently DROPPED here, returning the full result
        # set — the exact failure class this SDK exists to remove.
        assert (
            FilterSet().where("views", "isGreaterThan", 100).to_list()[0]["filters"][0]["value"]
            == "100"
        )
        assert (
            FilterSet().where("hasInteractivity", "is", True).to_list()[0]["filters"][0]["value"]
            == "true"
        )
        assert (
            FilterSet().where("isImported", "is", False).to_list()[0]["filters"][0]["value"]
            == "false"
        )
        assert FilterSet().where("views", "isAnyOf", [1, 2.5, True]).to_list()[0]["filters"][0][
            "value"
        ] == ["1", "2.5", "true"]

    def test_non_scalar_members_are_dropped_not_stringified(self):
        filter_set = FilterSet.from_data(
            [{"filters": [{"field": "status", "operator": "is", "value": ["published", {"nested": 1}]}]}]
        )

        assert filter_set.to_list()[0]["filters"][0]["value"] == ["published"]

    def test_a_filter_without_an_operator_is_skipped_not_guessed(self):
        # The old behaviour defaulted a missing operator to "is", inventing a
        # condition the author never wrote.
        filter_set = FilterSet.from_data(
            [
                {"filters": [{"field": "status", "value": "published"}]},
                {"filters": [{"field": "status", "operator": "is", "value": "draft"}]},
            ]
        )

        groups = filter_set.to_list()
        assert len(groups) == 1
        assert groups[0]["filters"][0]["value"] == "draft"

    def test_a_group_whose_filters_is_not_a_list_is_skipped(self):
        filter_set = FilterSet.from_data([{"filters": "junk"}])

        assert filter_set.to_list() == []

    def test_a_group_left_with_no_filters_is_dropped(self):
        filter_set = FilterSet().and_group(Filter("status", "is", "")).where("mediatype", "is", "video")
        groups = filter_set.to_list()

        assert len(groups) == 1
        assert groups[0]["filters"][0]["field"] == "mediatype"

    def test_it_serialises_to_the_json_sapi_expects(self):
        assert FilterSet().where("status", "is", "published").to_json() == (
            '[{"filters":[{"field":"status","operator":"is","value":"published"}]}]'
        )

    def test_it_round_trips_from_the_ovp_envelope_a_bare_list_and_a_string(self):
        groups = [{"filters": [{"field": "status", "operator": "is", "value": "published"}]}]

        assert FilterSet.from_data({"type": "SearchRequest", "filterSet": groups}).to_list() == groups
        assert FilterSet.from_data(groups).to_list() == groups
        assert FilterSet.from_data(json.dumps(groups)).to_list() == groups

    def test_junk_produces_no_filters_rather_than_an_error(self):
        for junk in (42, None, {"nope": True}, ["not a dict"], [{"filters": "nope"}]):
            assert FilterSet.from_data(junk).to_list() == []

    def test_an_unknown_operator_is_rejected_not_forwarded(self):
        # FilterOperator is a Literal — advisory, and this repo runs no type
        # checker, so it stops nothing. An operator SAPI cannot read is not an
        # error there: it is ignored and the answer is HTTP 200 with neither
        # numfound nor items, indistinguishable from an empty library. The PHP
        # sibling raises InvalidArgumentException on the same input.
        with pytest.raises(ValueError, match="conatins"):
            FilterSet().where("title", "conatins", "koert")

        with pytest.raises(ValueError, match="equals"):
            Filter("status", "equals", "published")

        with pytest.raises(ValueError, match="equals"):
            FilterSet.from_data(
                [{"filters": [{"field": "status", "operator": "equals", "value": "published"}]}]
            )

    def test_every_advertised_operator_is_accepted(self):
        # Pins the Literal and the runtime set to one another: an operator added
        # to FilterOperator is accepted, one removed stops being.
        assert len(KNOWN_OPERATORS) == 17
        for operator in KNOWN_OPERATORS:
            assert FilterSet().where("status", operator, "x").to_list()

    def test_truthiness_follows_emptiness(self):
        assert not FilterSet()
        assert FilterSet().where("status", "is", "published")
        assert list(FilterSet().where("status", "is", "published"))


class TestMediaClipSearchByFilterSet:
    @resp_lib.activate
    def test_it_sends_the_filterset_as_json_for_sapi_to_compile(self):
        resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip", json={"items": []}, status=200)
        client = make_client()

        client.mediaclip.search_by_filterset(
            FilterSet().where("status", "is", "published"),
            limit=25,
            offset=50,
            sort="title asc",
            query="holiday",
        )

        query = parse_qs(urlparse(resp_lib.calls[0].request.url).query)
        assert query["q"] == ["holiday"]
        assert query["limit"] == ["25"]
        assert query["offset"] == ["50"]
        assert query["sort"] == ["title asc"]
        assert query["filterset"] == [
            '[{"filters":[{"field":"status","operator":"is","value":"published"}]}]'
        ]

    @resp_lib.activate
    def test_it_sends_no_filter_parameters_when_nothing_is_filtered(self):
        resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip", json={"items": []}, status=200)
        client = make_client()

        client.mediaclip.search_by_filterset(FilterSet())

        query = parse_qs(urlparse(resp_lib.calls[0].request.url).query)
        assert "filterset" not in query
        assert "fq[0]" not in query

    @resp_lib.activate
    def test_raw_filter_queries_keep_their_indexed_encoding(self):
        # SAPI accepts fq[0]=; it ignores a plain fq= and a nested fq[][0]=, in
        # both cases silently, so this encoding is pinned.
        resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip", json={"items": []}, status=200)
        client = make_client()

        client.mediaclip.search_by_filterset(FilterSet(), filter_queries=['statusSort:"published"'])

        query = parse_qs(urlparse(resp_lib.calls[0].request.url).query)
        assert query["fq[0]"] == ['statusSort:"published"']
        assert "fq" not in query

class TestGetPosterPath:
    def test_it_uses_the_ovp_thumbnail_route(self):
        client = make_client()

        assert client.mediaclip.get_poster_path(1234, 320, 180) == (
            f"{BASE_URL}/mediaclip/1234/spthumbnail/320/180.webp"
        )

    def test_it_lets_the_service_choose_dimensions_by_default(self):
        client = make_client()

        assert client.mediaclip.get_poster_path(1234) == (
            f"{BASE_URL}/mediaclip/1234/spthumbnail/default/default.webp"
        )

    def test_a_non_numeric_dimension_falls_back_to_default(self):
        # A dimension is part of the URL path, so anything that is not a plain
        # number must not enter it.
        client = make_client()

        assert client.mediaclip.get_poster_path(1234, "320/../../etc", "auto") == (
            f"{BASE_URL}/mediaclip/1234/spthumbnail/default/default.webp"
        )

    def test_an_rpc_token_rides_along_for_draft_clips(self):
        client = make_client()

        assert client.mediaclip.get_poster_path(1234, rpc_token="12-345678") == (
            f"{BASE_URL}/mediaclip/1234/spthumbnail/default/default.webp"
            "?useSession=true&rpctoken=12-345678"
        )
