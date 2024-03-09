import pytest

from dlt.common.typing import StrAny, DictStrAny
from dlt.common.normalizers.naming import NamingConvention
from dlt.common.schema.typing import TColumnName, TSimpleRegex
from dlt.common.utils import digest128, uniq_id
from dlt.common.schema import Schema
from dlt.common.schema.utils import new_table

from dlt.common.normalizers.json.relational import (
    RelationalNormalizerConfigPropagation,
    DataItemNormalizer as RelationalNormalizer,
    DLT_ID_LENGTH_BYTES,
)

# _flatten, _get_child_row_hash, _normalize_row, normalize_data_item,

from tests.utils import create_schema_with_name


@pytest.fixture
def norm() -> RelationalNormalizer:
    return Schema("default").data_item_normalizer  # type: ignore[return-value]


def test_flatten_fix_field_name(norm: RelationalNormalizer) -> None:
    row = {
        "f-1": "!  30",
        "f 2": [],
        "f!3": {"f4": "a", "f-5": "b", "f*6": {"c": 7, "c v": 8, "c x": []}},
    }
    flattened_row, lists = norm._flatten("mock_table", row, 0)
    assert "f_1" in flattened_row
    # assert "f_2" in flattened_row
    assert "f_3__f4" in flattened_row
    assert "f_3__f_5" in flattened_row
    assert "f_3__fx6__c" in flattened_row
    assert "f_3__fx6__c_v" in flattened_row
    # assert "f_3__f_6__c_x" in flattened_row
    assert "f_3" not in flattened_row

    assert ("f_2",) in lists
    assert (
        "f_3",
        "fx6",
        "c_x",
    ) in lists


def test_preserve_complex_value(norm: RelationalNormalizer) -> None:
    # add table with complex column
    norm.schema.update_table(
        new_table(
            "with_complex",
            columns=[
                {
                    "name": "value",
                    "data_type": "complex",
                    "nullable": "true",  # type: ignore[typeddict-item]
                }
            ],
        )
    )
    row_1 = {"value": 1}
    flattened_row, _ = norm._flatten("with_complex", row_1, 0)
    assert flattened_row["value"] == 1

    row_2 = {"value": {"complex": True}}
    flattened_row, _ = norm._flatten("with_complex", row_2, 0)
    assert flattened_row["value"] == row_2["value"]
    # complex value is not flattened
    assert "value__complex" not in flattened_row


def test_preserve_complex_value_with_hint(norm: RelationalNormalizer) -> None:
    # add preferred type for "value"
    norm.schema._settings.setdefault("preferred_types", {})[TSimpleRegex("re:^value$")] = "complex"
    norm.schema._compile_settings()

    row_1 = {"value": 1}
    flattened_row, _ = norm._flatten("any_table", row_1, 0)
    assert flattened_row["value"] == 1

    row_2 = {"value": {"complex": True}}
    flattened_row, _ = norm._flatten("any_table", row_2, 0)
    assert flattened_row["value"] == row_2["value"]
    # complex value is not flattened
    assert "value__complex" not in flattened_row


def test_child_table_linking(norm: RelationalNormalizer) -> None:
    row = {"f": [{"l": ["a", "b", "c"], "v": 120, "o": [{"a": 1}, {"a": 2}]}]}
    # request _dlt_root_id propagation
    add_dlt_root_id_propagation(norm)

    rows = list(norm._normalize_row(row, {}, ("table",)))
    # should have 7 entries (root + level 1 + 3 * list + 2 * object)
    assert len(rows) == 7
    # root elem will not have a root hash if not explicitly added, "extend" is added only to child
    root_row = next(t for t in rows if t[0][0] == "table")
    # root row must have parent table none
    assert root_row[0][1] is None

    root = root_row[1]
    assert "_dlt_root_id" not in root
    assert "_dlt_parent_id" not in root
    assert "_dlt_list_idx" not in root
    # record hash will be autogenerated
    assert "_dlt_id" in root
    row_id = root["_dlt_id"]
    # all child entries must have _dlt_root_id == row_id
    assert all(e[1]["_dlt_root_id"] == row_id for e in rows if e[0][0] != "table")
    # all child entries must have _dlt_id
    assert all("_dlt_id" in e[1] for e in rows if e[0][0] != "table")
    # all child entries must have parent hash and pos
    assert all("_dlt_parent_id" in e[1] for e in rows if e[0][0] != "table")
    assert all("_dlt_list_idx" in e[1] for e in rows if e[0][0] != "table")
    # filter 3 entries with list
    list_rows = [t for t in rows if t[0][0] == "table__f__l"]
    assert len(list_rows) == 3
    # all list rows must have table_f as parent
    assert all(r[0][1] == "table__f" for r in list_rows)
    # get parent for list
    f_row = next(t for t in rows if t[0][0] == "table__f")
    # parent of the list must be "table"
    assert f_row[0][1] == "table"
    f_row_v = f_row[1]
    # parent of "f" must be row_id
    assert f_row_v["_dlt_parent_id"] == row_id
    # all elems in the list must have proper parent
    assert all(e[1]["_dlt_parent_id"] == f_row_v["_dlt_id"] for e in list_rows)
    # all values are there
    assert [e[1]["value"] for e in list_rows] == ["a", "b", "c"]


def test_child_table_linking_primary_key(norm: RelationalNormalizer) -> None:
    row = {
        "id": "level0",
        "f": [{"id": "level1", "l": ["a", "b", "c"], "v": 120, "o": [{"a": 1}, {"a": 2}]}],
    }
    norm.schema.merge_hints({"primary_key": [TSimpleRegex("id")]})
    norm.schema._compile_settings()

    rows = list(norm._normalize_row(row, {}, ("table",)))
    root = next(t for t in rows if t[0][0] == "table")[1]
    # record hash is random for primary keys, not based on their content
    # this is a change introduced in dlt 0.2.0a30
    assert root["_dlt_id"] != digest128("level0", DLT_ID_LENGTH_BYTES)

    # table at "f"
    t_f = next(t for t in rows if t[0][0] == "table__f")[1]
    assert t_f["_dlt_id"] != digest128("level1", DLT_ID_LENGTH_BYTES)
    # we use primary key to link to parent
    assert "_dlt_parent_id" not in t_f
    assert "_dlt_list_idx" not in t_f
    assert "_dlt_root_id" not in t_f

    list_rows = [t for t in rows if t[0][0] == "table__f__l"]
    assert all(
        e[1]["_dlt_parent_id"] != digest128("level1", DLT_ID_LENGTH_BYTES) for e in list_rows
    )
    assert all(r[0][1] == "table__f" for r in list_rows)
    obj_rows = [t for t in rows if t[0][0] == "table__f__o"]
    assert all(e[1]["_dlt_parent_id"] != digest128("level1", DLT_ID_LENGTH_BYTES) for e in obj_rows)
    assert all(r[0][1] == "table__f" for r in obj_rows)


def test_yields_parents_first(norm: RelationalNormalizer) -> None:
    row = {
        "id": "level0",
        "f": [{"id": "level1", "l": ["a", "b", "c"], "v": 120, "o": [{"a": 1}, {"a": 2}]}],
        "g": [{"id": "level2_g", "l": ["a"]}],
    }
    rows = list(norm._normalize_row(row, {}, ("table",)))
    tables = list(r[0][0] for r in rows)
    # child tables are always yielded before parent tables
    expected_tables = [
        "table",
        "table__f",
        "table__f__l",
        "table__f__l",
        "table__f__l",
        "table__f__o",
        "table__f__o",
        "table__g",
        "table__g__l",
    ]
    assert expected_tables == tables


def test_yields_parent_relation(norm: RelationalNormalizer) -> None:
    row = {
        "id": "level0",
        "f": [
            {
                "id": "level1",
                "l": ["a"],
                "o": [{"a": 1}],
                "b": {
                    "a": [{"id": "level5"}],
                },
            }
        ],
        "d": {
            "a": [{"id": "level4"}],
            "b": {
                "a": [{"id": "level5"}],
            },
            "c": "x",
        },
        "e": [
            {
                "o": [{"a": 1}],
                "b": {
                    "a": [{"id": "level5"}],
                },
            }
        ],
    }
    rows = list(norm._normalize_row(row, {}, ("table",)))
    # normalizer must return parent table first and move in order of the list elements when yielding child tables
    # the yielding order if fully defined
    expected_parents = [
        ("table", None),
        ("table__f", "table"),
        ("table__f__l", "table__f"),
        ("table__f__o", "table__f"),
        # "table__f__b" is not yielded as it is fully flattened into table__f
        ("table__f__b__a", "table__f"),
        # same for table__d -> fully flattened into table
        ("table__d__a", "table"),
        ("table__d__b__a", "table"),
        # table__e is yielded it however only contains linking information
        ("table__e", "table"),
        ("table__e__o", "table__e"),
        ("table__e__b__a", "table__e"),
    ]
    parents = list(r[0] for r in rows)
    assert parents == expected_parents

    # make sure that table__e is just linking
    table__e = [r[1] for r in rows if r[0][0] == "table__e"][0]
    assert all(f.startswith("_dlt") for f in table__e.keys()) is True

    # check if linking is correct when not directly derived
    table__e__b__a = [r[1] for r in rows if r[0][0] == "table__e__b__a"][0]
    assert table__e__b__a["_dlt_parent_id"] == table__e["_dlt_id"]

    table__f = [r[1] for r in rows if r[0][0] == "table__f"][0]
    table__f__b__a = [r[1] for r in rows if r[0][0] == "table__f__b__a"][0]
    assert table__f__b__a["_dlt_parent_id"] == table__f["_dlt_id"]


# def test_child_table_linking_compound_primary_key(norm: RelationalNormalizer) -> None:
#     row = {
#         "id": "level0",
#         "offset": 12102.45,
#         "f": [{
#             "id": "level1",
#             "item_no": 8129173987192873,
#             "l": ["a", "b", "c"],
#             "v": 120,
#             "o": [{"a": 1}, {"a": 2}]
#         }]
#     }
#     norm.schema.merge_hints({"primary_key": ["id", "offset", "item_no"]})
#     norm.schema._compile_settings()

#     rows = list(norm._normalize_row(row, {}, ("table", )))
#     root = next(t for t in rows if t[0][0] == "table")[1]
#     # record hash must be derived from natural key
#     assert root["_dlt_id"] == digest128("level0_12102.45", DLT_ID_LENGTH_BYTES)
#     t_f = next(t for t in rows if t[0][0] == "table__f")[1]
#     assert t_f["_dlt_id"] == digest128("level1_8129173987192873", DLT_ID_LENGTH_BYTES)


def test_list_position(norm: RelationalNormalizer) -> None:
    row: DictStrAny = {
        "f": [{"l": ["a", "b", "c"], "v": 120, "lo": [{"e": "a"}, {"e": "b"}, {"e": "c"}]}]
    }
    rows = list(norm._normalize_row(row, {}, ("table",)))
    # root has no pos
    root = [t for t in rows if t[0][0] == "table"][0][1]
    assert "_dlt_list_idx" not in root

    # all other have pos
    others = [t for t in rows if t[0][0] != "table"]
    assert all("_dlt_list_idx" in e[1] for e in others)

    # f_l must be ordered as it appears in the list
    for pos, elem in enumerate(["a", "b", "c"]):
        row_1 = next(t[1] for t in rows if t[0][0] == "table__f__l" and t[1]["value"] == elem)
        assert row_1["_dlt_list_idx"] == pos

    # f_lo must be ordered - list of objects
    for pos, elem in enumerate(["a", "b", "c"]):
        row_2 = next(t[1] for t in rows if t[0][0] == "table__f__lo" and t[1]["e"] == elem)
        assert row_2["_dlt_list_idx"] == pos


# def test_list_of_lists(norm: RelationalNormalizer) -> None:
#     row = {
#         "l":[
#             ["a", "b", "c"],
#             [
#                 ["a", "b", "b"]
#             ],
#             "a", 1, 1.1
#         ]
#     }
#     rows = list(norm._normalize_row(row, {}, ("table", )))
#     print(rows)


def test_control_descending(norm: RelationalNormalizer) -> None:
    row: StrAny = {
        "f": [{"l": ["a", "b", "c"], "v": 120, "lo": [[{"e": "a"}, {"e": "b"}, {"e": "c"}]]}],
        "g": "val",
    }

    # break at first row
    rows_gen = norm.normalize_data_item(row, "load_id", "table")
    rows_gen.send(None)
    # won't yield anything else
    with pytest.raises(StopIteration):
        rows_gen.send(False)

    # prevent yielding descendants of "f" but yield all else
    rows_gen = norm.normalize_data_item(row, "load_id", "table")
    rows_gen.send(None)
    (table, _), _ = rows_gen.send(True)
    assert table == "table__f"
    # won't yield anything else
    with pytest.raises(StopIteration):
        rows_gen.send(False)

    # descend into "l"
    rows_gen = norm.normalize_data_item(row, "load_id", "table")
    rows_gen.send(None)
    rows_gen.send(True)
    (table, _), one_row = rows_gen.send(True)
    assert table == "table__f__l"
    assert one_row["value"] == "a"
    # get next element in the list - even with sending False - we do not descend
    (table, _), one_row = rows_gen.send(False)
    assert table == "table__f__l"
    assert one_row["value"] == "b"

    # prevent descending into list of lists
    rows_gen = norm.normalize_data_item(row, "load_id", "table")
    rows_gen.send(None)
    rows_gen.send(True)
    # yield "l"
    next(rows_gen)
    next(rows_gen)
    next(rows_gen)
    (table, _), one_row = rows_gen.send(True)
    assert table == "table__f__lo"
    # do not descend into lists
    with pytest.raises(StopIteration):
        rows_gen.send(False)


def test_list_in_list() -> None:
    chats = {
        "_dlt_id": "123456",
        "created_at": "2023-05-12T12:34:56Z",
        "ended_at": "2023-05-12T13:14:32Z",
        "webpath": [
            [
                {"url": "https://www.website.com/", "timestamp": "2023-05-12T12:35:01Z"},
                {"url": "https://www.website.com/products", "timestamp": "2023-05-12T12:38:45Z"},
                {
                    "url": "https://www.website.com/products/item123",
                    "timestamp": "2023-05-12T12:42:22Z",
                },
                [
                    {
                        "url": "https://www.website.com/products/item1234",
                        "timestamp": "2023-05-12T12:42:22Z",
                    }
                ],
            ],
            [1, 2, 3],
        ],
    }
    schema = create_schema_with_name("other")
    # root
    rows = list(schema.normalize_data_item(chats, "1762162.1212", "zen"))
    assert len(rows) == 11
    # check if intermediary table was created
    zen__webpath = [row for row in rows if row[0][0] == "zen__webpath"]
    # two rows in web__zenpath for two lists
    assert len(zen__webpath) == 2
    assert zen__webpath[0][0] == ("zen__webpath", "zen")
    # _dlt_id was hardcoded in the original row
    assert zen__webpath[0][1]["_dlt_parent_id"] == "123456"
    assert zen__webpath[0][1]["_dlt_list_idx"] == 0
    assert zen__webpath[1][1]["_dlt_list_idx"] == 1
    assert zen__webpath[1][0] == ("zen__webpath", "zen")
    # inner lists
    zen__webpath__list = [row for row in rows if row[0][0] == "zen__webpath__list"]
    # actually both list of objects and list of number will be in the same table
    assert len(zen__webpath__list) == 7
    assert zen__webpath__list[0][1]["_dlt_parent_id"] == zen__webpath[0][1]["_dlt_id"]
    # 4th list is itself a list
    zen__webpath__list__list = [row for row in rows if row[0][0] == "zen__webpath__list__list"]
    assert zen__webpath__list__list[0][1]["_dlt_parent_id"] == zen__webpath__list[3][1]["_dlt_id"]

    # test the same setting webpath__list to complex
    zen_table = new_table("zen")
    schema.update_table(zen_table)

    path_table = new_table(
        "zen__webpath", parent_table_name="zen", columns=[{"name": "list", "data_type": "complex"}]
    )
    schema.update_table(path_table)
    rows = list(schema.normalize_data_item(chats, "1762162.1212", "zen"))
    # both lists are complex types now
    assert len(rows) == 3
    zen__webpath = [row for row in rows if row[0][0] == "zen__webpath"]
    assert all("list" in row[1] for row in zen__webpath)


def test_child_row_deterministic_hash(norm: RelationalNormalizer) -> None:
    row_id = uniq_id()
    # directly set record hash so it will be adopted in normalizer as top level hash
    row = {
        "_dlt_id": row_id,
        "f": [{"l": ["a", "b", "c"], "v": 120, "lo": [{"e": "a"}, {"e": "b"}, {"e": "c"}]}],
    }
    rows = list(norm._normalize_row(row, {}, ("table",)))
    children = [t for t in rows if t[0][0] != "table"]
    # all hashes must be different
    distinct_hashes = set([ch[1]["_dlt_id"] for ch in children])
    assert len(distinct_hashes) == len(children)

    # compute hashes for all children
    for (table, _), ch in children:
        expected_hash = digest128(
            f"{ch['_dlt_parent_id']}_{table}_{ch['_dlt_list_idx']}", DLT_ID_LENGTH_BYTES
        )
        assert ch["_dlt_id"] == expected_hash

    # direct compute one of the
    el_f = next(t[1] for t in rows if t[0][0] == "table__f" and t[1]["_dlt_list_idx"] == 0)
    f_lo_p2 = next(t[1] for t in rows if t[0][0] == "table__f__lo" and t[1]["_dlt_list_idx"] == 2)
    assert f_lo_p2["_dlt_id"] == digest128(f"{el_f['_dlt_id']}_table__f__lo_2", DLT_ID_LENGTH_BYTES)

    # same data with same table and row_id
    rows_2 = list(norm._normalize_row(row, {}, ("table",)))
    children_2 = [t for t in rows_2 if t[0][0] != "table"]
    # corresponding hashes must be identical
    assert all(ch[0][1]["_dlt_id"] == ch[1][1]["_dlt_id"] for ch in zip(children, children_2))

    # change parent table and all child hashes must be different
    rows_4 = list(norm._normalize_row(row, {}, ("other_table",)))
    children_4 = [t for t in rows_4 if t[0][0] != "other_table"]
    assert all(ch[0][1]["_dlt_id"] != ch[1][1]["_dlt_id"] for ch in zip(children, children_4))

    # change parent hash and all child hashes must be different
    row["_dlt_id"] = uniq_id()
    rows_3 = list(norm._normalize_row(row, {}, ("table",)))
    children_3 = [t for t in rows_3 if t[0][0] != "table"]
    assert all(ch[0][1]["_dlt_id"] != ch[1][1]["_dlt_id"] for ch in zip(children, children_3))


def test_keeps_dlt_id(norm: RelationalNormalizer) -> None:
    h = uniq_id()
    row = {"a": "b", "_dlt_id": h}
    rows = list(norm._normalize_row(row, {}, ("table",)))
    root = [t for t in rows if t[0][0] == "table"][0][1]
    assert root["_dlt_id"] == h


def test_propagate_hardcoded_context(norm: RelationalNormalizer) -> None:
    row = {"level": 1, "list": ["a", "b", "c"], "comp": [{"_timestamp": "a"}]}
    rows = list(
        norm._normalize_row(row, {"_timestamp": 1238.9, "_dist_key": "SENDER_3000"}, ("table",))
    )
    # context is not added to root element
    root = next(t for t in rows if t[0][0] == "table")[1]
    assert "_timestamp" in root
    assert "_dist_key" in root
    # the original _timestamp field will be overwritten in children and added to lists
    assert all(
        e[1]["_timestamp"] == 1238.9 and e[1]["_dist_key"] == "SENDER_3000"
        for e in rows
        if e[0][0] != "table"
    )


def test_propagates_root_context(norm: RelationalNormalizer) -> None:
    add_dlt_root_id_propagation(norm)
    # add timestamp propagation
    norm.schema._normalizers_config["json"]["config"]["propagation"]["root"][
        "timestamp"
    ] = "_partition_ts"
    # add propagation for non existing element
    norm.schema._normalizers_config["json"]["config"]["propagation"]["root"][
        "__not_found"
    ] = "__not_found"

    row = {
        "_dlt_id": "###",
        "timestamp": 12918291.1212,
        "dependent_list": [1, 2, 3],
        "dependent_objects": [{"vx": "ax"}],
    }
    normalized_rows = list(norm._normalize_row(row, {}, ("table",)))
    # all non-root rows must have:
    non_root = [r for r in normalized_rows if r[0][1] is not None]
    assert all(r[1]["_dlt_root_id"] == "###" for r in non_root)
    assert all(r[1]["_partition_ts"] == 12918291.1212 for r in non_root)
    assert all("__not_found" not in r[1] for r in non_root)


@pytest.mark.parametrize("add_pk,add_dlt_id", [(False, False), (True, False), (True, True)])
def test_propagates_table_context(
    norm: RelationalNormalizer, add_pk: bool, add_dlt_id: bool
) -> None:
    add_dlt_root_id_propagation(norm)
    prop_config: RelationalNormalizerConfigPropagation = norm.schema._normalizers_config["json"][
        "config"
    ]["propagation"]
    prop_config["root"][TColumnName("timestamp")] = TColumnName("_partition_ts")
    # for table "table__lvl1" request to propagate "vx" and "partition_ovr" as "_partition_ts" (should overwrite root)
    prop_config["tables"]["table__lvl1"] = {
        TColumnName("vx"): TColumnName("__vx"),
        TColumnName("partition_ovr"): TColumnName("_partition_ts"),
        TColumnName("__not_found"): TColumnName("__not_found"),
    }

    if add_pk:
        # also add primary key on "vx" to reproduce bug where propagation was disabled for tables with PK
        norm.schema.merge_hints({"primary_key": [TSimpleRegex("vx")]})

    row = {
        "_dlt_id": "###",
        "timestamp": 12918291.1212,
        "lvl1": [
            {"vx": "ax", "partition_ovr": 1283.12, "lvl2": [{"_partition_ts": "overwritten"}]}
        ],
    }
    if add_dlt_id:
        # to reproduce a bug where rows with _dlt_id set were not extended
        row["lvl1"][0]["_dlt_id"] = "row_id_lvl1"  # type: ignore[index]

    normalized_rows = list(norm._normalize_row(row, {}, ("table",)))
    non_root = [r for r in normalized_rows if r[0][1] is not None]
    # _dlt_root_id in all non root
    assert all(r[1]["_dlt_root_id"] == "###" for r in non_root)
    # __not_found nowhere
    assert all("__not_found" not in r[1] for r in non_root)
    # _partition_ts == timestamp only at lvl1
    assert all(r[1]["_partition_ts"] == 12918291.1212 for r in non_root if r[0][0] == "table__lvl1")
    # _partition_ts == partition_ovr and __vx only at lvl2
    assert all(
        r[1]["_partition_ts"] == 1283.12 and r[1]["__vx"] == "ax"
        for r in non_root
        if r[0][0] == "table__lvl1__lvl2"
    )
    assert (
        any(
            r[1]["_partition_ts"] == 1283.12 and r[1]["__vx"] == "ax"
            for r in non_root
            if r[0][0] != "table__lvl1__lvl2"
        )
        is False
    )


def test_propagates_table_context_to_lists(norm: RelationalNormalizer) -> None:
    add_dlt_root_id_propagation(norm)
    prop_config: RelationalNormalizerConfigPropagation = norm.schema._normalizers_config["json"][
        "config"
    ]["propagation"]
    prop_config["root"][TColumnName("timestamp")] = TColumnName("_partition_ts")

    row = {"_dlt_id": "###", "timestamp": 12918291.1212, "lvl1": [1, 2, 3, [4, 5, 6]]}
    normalized_rows = list(norm._normalize_row(row, {}, ("table",)))
    # _partition_ts == timestamp on all child tables
    non_root = [r for r in normalized_rows if r[0][1] is not None]
    assert all(r[1]["_partition_ts"] == 12918291.1212 for r in non_root)
    # just make sure that list of lists are present
    assert len([r for r in non_root if r[0][0] == "table__lvl1__list"]) == 3
    assert len(non_root) == 7


def test_removes_normalized_list(norm: RelationalNormalizer) -> None:
    # after normalizing the list that got normalized into child table must be deleted
    row = {"comp": [{"_timestamp": "a"}]}
    # get iterator
    normalized_rows_i = norm._normalize_row(row, {}, ("table",))
    # yield just one item
    root_row = next(normalized_rows_i)
    # root_row = next(r for r in normalized_rows if r[0][1] is None)
    assert "comp" not in root_row[1]


def test_preserves_complex_types_list(norm: RelationalNormalizer) -> None:
    # the exception to test_removes_normalized_list
    # complex types should be left as they are
    # add table with complex column
    norm.schema.update_table(
        new_table(
            "event_slot",
            columns=[
                {
                    "name": "value",
                    "data_type": "complex",
                    "nullable": "true",  # type: ignore[typeddict-item]
                }
            ],
        )
    )
    row = {"value": ["from", {"complex": True}]}
    normalized_rows = list(norm._normalize_row(row, {}, ("event_slot",)))
    # make sure only 1 row is emitted, the list is not normalized
    assert len(normalized_rows) == 1
    # value is kept in root row -> market as complex
    root_row = next(r for r in normalized_rows if r[0][1] is None)
    assert root_row[1]["value"] == row["value"]

    # same should work for a list
    row = {"value": ["from", ["complex", True]]}  # type: ignore[list-item]
    normalized_rows = list(norm._normalize_row(row, {}, ("event_slot",)))
    # make sure only 1 row is emitted, the list is not normalized
    assert len(normalized_rows) == 1
    # value is kept in root row -> market as complex
    root_row = next(r for r in normalized_rows if r[0][1] is None)
    assert root_row[1]["value"] == row["value"]


def test_wrap_in_dict(norm: RelationalNormalizer) -> None:
    # json normalizer wraps in dict
    row = list(norm.schema.normalize_data_item(1, "load_id", "simplex"))[0][1]
    assert row["value"] == 1
    assert row["_dlt_load_id"] == "load_id"
    # wrap a list
    rows = list(norm.schema.normalize_data_item([1, 2, 3, 4, "A"], "load_id", "listex"))
    assert len(rows) == 6
    assert rows[0][0] == (
        "listex",
        None,
    )
    assert rows[1][0] == ("listex__value", "listex")
    assert rows[-1][1]["value"] == "A"


def test_complex_types_for_recursion_level(norm: RelationalNormalizer) -> None:
    add_dlt_root_id_propagation(norm)
    # if max recursion depth is set, nested elements will be kept as complex
    row = {
        "_dlt_id": "row_id",
        "f": [
            {
                "l": ["a"],  # , "b", "c"
                "v": 120,
                "lo": [{"e": {"v": 1}}],  # , {"e": {"v": 2}}, {"e":{"v":3 }}
            }
        ],
    }
    n_rows_nl = list(norm.schema.normalize_data_item(row, "load_id", "default"))
    # all nested elements were yielded
    assert ["default", "default__f", "default__f__l", "default__f__lo"] == [
        r[0][0] for r in n_rows_nl
    ]

    # set max nesting to 0
    set_max_nesting(norm, 0)
    n_rows = list(norm.schema.normalize_data_item(row, "load_id", "default"))
    # the "f" element is left as complex type and not normalized
    assert len(n_rows) == 1
    assert n_rows[0][0][0] == "default"
    assert "f" in n_rows[0][1]
    assert type(n_rows[0][1]["f"]) is list

    # max nesting 1
    set_max_nesting(norm, 1)
    n_rows = list(norm.schema.normalize_data_item(row, "load_id", "default"))
    assert len(n_rows) == 2
    assert ["default", "default__f"] == [r[0][0] for r in n_rows]
    # on level f, "l" and "lo" are not normalized
    assert "l" in n_rows[1][1]
    assert type(n_rows[1][1]["l"]) is list
    assert "lo" in n_rows[1][1]
    assert type(n_rows[1][1]["lo"]) is list

    # max nesting 2
    set_max_nesting(norm, 2)
    n_rows = list(norm.schema.normalize_data_item(row, "load_id", "default"))
    assert len(n_rows) == 4
    # in default__f__lo the dicts that would be flattened are complex types
    last_row = n_rows[3]
    assert last_row[1]["e"] == {"v": 1}

    # max nesting 3
    set_max_nesting(norm, 3)
    n_rows = list(norm.schema.normalize_data_item(row, "load_id", "default"))
    assert n_rows_nl == n_rows


def test_extract_with_table_name_meta() -> None:
    row = {
        "id": "817949077341208606",
        "type": 4,
        "name": "Moderation",
        "position": 0,
        "flags": 0,
        "parent_id": None,
        "guild_id": "815421435900198962",
        "permission_overwrites": [],
    }
    # force table name
    rows = list(create_schema_with_name("discord").normalize_data_item(row, "load_id", "channel"))
    # table is channel
    assert rows[0][0][0] == "channel"
    normalized_row = rows[0][1]
    assert normalized_row["guild_id"] == "815421435900198962"
    assert "_dlt_id" in normalized_row
    assert normalized_row["_dlt_load_id"] == "load_id"


def test_table_name_meta_normalized() -> None:
    row = {
        "id": "817949077341208606",
    }
    # force table name
    rows = list(
        create_schema_with_name("discord").normalize_data_item(row, "load_id", "channelSURFING")
    )
    # table is channel
    assert rows[0][0][0] == "channel_surfing"


def test_parse_with_primary_key() -> None:
    schema = create_schema_with_name("discord")
    schema.merge_hints({"primary_key": ["id"]})  # type: ignore[list-item]
    schema._compile_settings()
    add_dlt_root_id_propagation(schema.data_item_normalizer)  # type: ignore[arg-type]

    row = {"id": "817949077341208606", "w_id": [{"id": 9128918293891111, "wo_id": [1, 2, 3]}]}
    rows = list(schema.normalize_data_item(row, "load_id", "discord"))
    # get root
    root = next(t[1] for t in rows if t[0][0] == "discord")
    assert root["_dlt_id"] != digest128("817949077341208606", DLT_ID_LENGTH_BYTES)
    assert "_dlt_parent_id" not in root
    assert "_dlt_root_id" not in root
    assert root["_dlt_load_id"] == "load_id"

    el_w_id = next(t[1] for t in rows if t[0][0] == "discord__w_id")
    # this also has primary key
    assert el_w_id["_dlt_id"] != digest128("9128918293891111", DLT_ID_LENGTH_BYTES)
    assert "_dlt_parent_id" not in el_w_id
    assert "_dlt_list_idx" not in el_w_id
    # if enabled, dlt_root is always propagated
    assert "_dlt_root_id" in el_w_id

    # this must have deterministic child key
    f_wo_id = next(
        t[1] for t in rows if t[0][0] == "discord__w_id__wo_id" and t[1]["_dlt_list_idx"] == 2
    )
    assert f_wo_id["value"] == 3
    assert f_wo_id["_dlt_root_id"] != digest128("817949077341208606", DLT_ID_LENGTH_BYTES)
    assert f_wo_id["_dlt_parent_id"] != digest128("9128918293891111", DLT_ID_LENGTH_BYTES)
    assert f_wo_id["_dlt_id"] == RelationalNormalizer._get_child_row_hash(
        f_wo_id["_dlt_parent_id"], "discord__w_id__wo_id", 2
    )


def test_keeps_none_values() -> None:
    row = {"a": None, "timestamp": 7}
    rows = list(create_schema_with_name("other").normalize_data_item(row, "1762162.1212", "other"))
    table_name = rows[0][0][0]
    assert table_name == "other"
    normalized_row = rows[0][1]
    assert normalized_row["a"] is None
    assert normalized_row["_dlt_load_id"] == "1762162.1212"


def test_normalize_and_shorten_deterministically() -> None:
    schema = create_schema_with_name("other")
    # shorten at 16 chars
    schema.naming.max_length = 16

    data = {
        "short>ident:1": {
            "short>ident:2": {"short>ident:3": "a"},
        },
        "LIST+ident:1": {"LIST+ident:2": {"LIST+ident:3": [1]}},
        "long+long:SO+LONG:_>16": True,
    }
    rows = list(schema.normalize_data_item(data, "1762162.1212", "s"))
    # all identifiers are 16 chars or shorter
    for tables, row in rows:
        assert len(tables[0]) <= 16
        assert len(tables[1] or "") <= 16
        assert all(len(name) <= 16 for name in row.keys())
        print(tables[0])

    # all contain tags based on the full non shortened path
    root_data = rows[0][1]
    root_data_keys = list(root_data.keys())
    # "short:ident:2": "a" will be flattened into root
    tag = NamingConvention._compute_tag(
        "short_ident_1__short_ident_2__short_ident_3", NamingConvention._DEFAULT_COLLISION_PROB
    )
    assert tag in root_data_keys[0]
    # long:SO+LONG:_>16 shortened on normalized name
    tag = NamingConvention._compute_tag(
        "long+long:SO+LONG:_>16", NamingConvention._DEFAULT_COLLISION_PROB
    )
    assert tag in root_data_keys[1]
    # table name in second row
    table_name = rows[1][0][0]
    tag = NamingConvention._compute_tag(
        "s__lis_txident_1__lis_txident_2__lis_txident_3", NamingConvention._DEFAULT_COLLISION_PROB
    )
    assert tag in table_name


def test_normalize_empty_keys() -> None:
    schema = create_schema_with_name("other")
    # root
    data: DictStrAny = {"a": 1, "": 2}
    rows = list(schema.normalize_data_item(data, "1762162.1212", "s"))
    assert rows[0][1]["_empty"] == 2
    # flatten
    data = {"a": 1, "b": {"": 2}}
    rows = list(schema.normalize_data_item(data, "1762162.1212", "s"))
    assert rows[0][1]["b___empty"] == 2
    # nested
    data = {"a": 1, "b": [{"": 2}]}
    rows = list(schema.normalize_data_item(data, "1762162.1212", "s"))
    assert rows[1][1]["_empty"] == 2


# could also be in schema tests
def test_propagation_update_on_table_change(norm: RelationalNormalizer):
    # append does not have propagated columns
    table_1 = new_table("table_1", write_disposition="append")
    norm.schema.update_table(table_1)
    assert "config" not in norm.schema._normalizers_config["json"]

    # change table to merge
    table_1["write_disposition"] = "merge"
    norm.schema.update_table(table_1)
    assert norm.schema._normalizers_config["json"]["config"]["propagation"]["tables"][
        table_1["name"]
    ] == {"_dlt_id": "_dlt_root_id"}

    # add subtable
    table_2 = new_table("table_2", parent_table_name="table_1")
    norm.schema.update_table(table_2)
    assert (
        "table_2" not in norm.schema._normalizers_config["json"]["config"]["propagation"]["tables"]
    )

    # test merging into existing propagation
    norm.schema._normalizers_config["json"]["config"]["propagation"]["tables"]["table_3"] = {
        "prop1": "prop2"
    }
    table_3 = new_table("table_3", write_disposition="merge")
    norm.schema.update_table(table_3)
    assert norm.schema._normalizers_config["json"]["config"]["propagation"]["tables"][
        "table_3"
    ] == {"_dlt_id": "_dlt_root_id", "prop1": "prop2"}


def set_max_nesting(norm: RelationalNormalizer, max_nesting: int) -> None:
    RelationalNormalizer.update_normalizer_config(norm.schema, {"max_nesting": max_nesting})
    norm._reset()


def add_dlt_root_id_propagation(norm: RelationalNormalizer) -> None:
    RelationalNormalizer.update_normalizer_config(
        norm.schema,
        {
            "propagation": {
                "root": {"_dlt_id": "_dlt_root_id"},  # type: ignore[dict-item]
                "tables": {},
            }
        },
    )
    norm._reset()
