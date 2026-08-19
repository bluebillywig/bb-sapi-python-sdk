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

from bb_sapi import Filter, FilterSet, SapiClient

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

    def test_presence_operators_survive_without_a_value(self):
        filter_set = FilterSet().where("author", "isEmpty")

        assert not filter_set.is_empty()
        assert "value" not in filter_set.to_list()[0]["filters"][0]

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
