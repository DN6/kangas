# -*- coding: utf-8 -*-
######################################################
#     _____                  _____      _     _      #
#    (____ \       _        |  ___)    (_)   | |     #
#     _   \ \ ____| |_  ____| | ___ ___ _  _ | |     #
#    | |  | )/ _  |  _)/ _  | |(_  / __) |/ || |     #
#    | |__/ ( ( | | | ( ( | | |__| | | | ( (_| |     #
#    |_____/ \_||_|___)\_||_|_____/|_| |_|\____|     #
#                                                    #
#    Copyright (c) 2022 Kangas Development Team      #
#    All rights reserved                             #
######################################################

import ast
import json
import logging
import math
import os
import re
import sqlite3
import string
import time
from collections import Counter, defaultdict

import numpy as np
from PIL import Image

from ..datatypes.utils import (
    generate_image,
    generate_thumbnail,
    image_to_fp,
    is_nan,
    pytype_to_dgtype,
)
from .computed_columns import update_state

LOGGER = logging.getLogger(__name__)
KANGAS_ROOT = os.environ.get("KANGAS_ROOT", ".")
MAX_CATEGORIES = 20
HISTOGRAM_BINS = 10

CUSTOM_CODE_INIT = """
import matplotlib.pyplot as plt
import matplotlib
from PIL import Image, ImageDraw
import json
import numpy as np
from traceback import format_exc
import math
"""

VALID_CHARS = string.ascii_letters + string.digits + "_"


def safe_builtin_funcs():
    return {
        "abs": abs,
        "all": all,
        "any": any,
        "ascii": ascii,
        "bin": bin,
        "bool": bool,
        "bytes": bytes,
        "callable": callable,
        "chr": chr,
        "complex": complex,
        "dict": dict,
        "dir": dir,
        "divmod": divmod,
        "enumerate": enumerate,
        "filter": filter,
        "float": float,
        "format": format,
        "hasattr": hasattr,
        "hash": hash,
        "hex": hex,
        "id": id,
        "int": int,
        "isinstance": isinstance,
        "issubclass": issubclass,
        "iter": iter,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "oct": oct,
        "ord": ord,
        "pow": pow,
        "range": range,
        "repr": repr,
        "reversed": reversed,
        "round": round,
        "set": set,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "type": type,
        "zip": zip,
    }


try:
    import RestrictedPython
    import RestrictedPython.Eval
    import RestrictedPython.Guards

    def safe_compile(source):
        return RestrictedPython.compile_restricted_eval(source).code

    def safe_builtins():
        env = RestrictedPython.Guards.safe_builtins.copy()
        env.update(safe_builtin_funcs())
        return env

    def safe_env(**kwargs):
        env = {
            "_getattr_": getattr,
            "_getitem_": RestrictedPython.Eval.default_guarded_getitem,
            "_getiter_": RestrictedPython.Eval.default_guarded_getiter,
            "_iter_unpack_sequence_": RestrictedPython.Guards.guarded_iter_unpack_sequence,
            "__name__": "restricted namespace",
            "__builtins__": safe_builtins(),
        }
        env.update(kwargs)
        return env

except Exception:

    def safe_compile(source):
        return compile(source, "<string>", "eval")

    def safe_env(**kwargs):
        env = {
            "__builtins__": safe_builtins(),
        }
        env.update(kwargs)
        return env

    def safe_builtins():
        env = {}
        env.update(safe_builtin_funcs())
        return env


def parse_comma_separated_values(string):
    retval = []
    for value in string.split(","):
        value = value.replace("&comma;", ",")

        if value in ["", "null", "None"]:
            retval.append(None)
            continue

        # Scientific notation
        match = re.match(r"^([-+]?[\d]+\.?[\d]*[Ee](?:[-+]?[\d]+)?)$", value)
        if match:
            try:
                retval.append(float(match.groups()[0]))
            except Exception:
                retval.append(value)
        else:
            # integers
            match = re.match(r"^([-+]?[\d]+)$", value)
            if match:
                try:
                    retval.append(int(match.groups()[0]))
                except Exception:
                    retval.append(value)
            else:
                # floating point numbers
                match = re.match(r"^([-+]?[\d]+\.?[\d]*)$", value)
                if match:
                    try:
                        retval.append(float(match.groups()[0]))
                    except Exception:
                        retval.append(value)
                else:  # string
                    retval.append(value)
    return retval


# based on: https://stackoverflow.com/questions/2298339/standard-deviation-for-sqlite
class StdevFunc:
    def __init__(self):
        self.M = 0.0
        self.S = 0.0
        self.k = 0

    def step(self, value):
        try:
            value = float(value)
            tM = self.M
            self.k += 1
            self.M += (value - tM) / self.k
            self.S += (value - tM) * (value - self.M)
        except Exception:
            pass

    def finalize(self):
        if self.k <= 1:
            return None
        return math.sqrt(self.S / (self.k - 1))  # To use MySQL version, change to k-2


def FLATTEN(lists):
    if lists:
        try:
            return str(
                [item for sublist in ast.literal_eval(lists) for item in sublist]
            )
        except Exception:
            pass

    return "[]"


def SPLIT(string, delim=None, maxsplit=-1):
    return str(string.split(delim, maxsplit))


def KEYS_OF(obj):
    if obj:
        try:
            return str(list(ast.literal_eval(obj).keys()))
        except Exception:
            pass

    return "[]"


def VALUES_OF(obj):
    if obj:
        try:
            return str(list(ast.literal_eval(obj).values()))
        except Exception:
            pass

    return "[]"


def LENGTH(string_or_obj):
    ## Comes in as a string, but might be "[...]"
    if string_or_obj:
        try:
            return len(ast.literal_eval(string_or_obj))
        except Exception:
            return len(string_or_obj)
    return 0


def IN_OBJ(item, string_or_obj):
    if string_or_obj:
        try:
            return item in list(ast.literal_eval(string_or_obj))
        except Exception:
            return item in string_or_obj
    return False


def ANY_IN_GROUP(group):
    if group:
        try:
            decoded_group = ast.literal_eval(group)
        except Exception:
            decoded_group = None
        if isinstance(decoded_group, list):
            group_decoded = [x for x in decoded_group]
            return any(group_decoded)

    return False


def ALL_IN_GROUP(group):
    ## This varies from Python semantics: if you are looking for all
    ## then it doesn't make sense to return True if the list
    ## is empty
    if group:
        try:
            decoded_group = ast.literal_eval(group)
        except Exception:
            decoded_group = None
        if isinstance(decoded_group, list):
            group_decoded = [x for x in decoded_group]
        group_decoded = [x for x in decoded_group]
        return all(group_decoded)

    return False


def process_results(value):
    if value is True:
        return "1"
    elif value is False:
        return "0"
    else:
        return repr(value)


def unescape(string):
    if string:
        return string.replace("&#39;", "'").replace("&#34;", '"').replace("&#44;", ",")
    else:
        return ""


def ListComprehension(x, y, gen, ifs):
    ## [x for y in gen ifs]
    results = []
    gen = unescape(gen)
    if gen:
        x, y = unescape(x), unescape(y)
        code = safe_compile(x)
        env = safe_env()
        try:
            ## FIXME: a string that is a number is json-like
            decoded_gen = ast.literal_eval(gen)
        except Exception:
            decoded_gen = gen

        ## first, we prepare ifs:
        if ifs:
            decoded_ifs = [unescape(exp) for exp in ifs.split(",")]
        else:
            decoded_ifs = []
        compiled_ifs = [safe_compile(exp) for exp in decoded_ifs]

        # dict:
        if isinstance(decoded_gen, dict):
            env[y] = decoded_gen

            # Short circuit logic:
            doit = all(eval(exp, env) for exp in compiled_ifs)

            if doit:
                try:
                    result = eval(code, env)
                except Exception:
                    result = None
                if result is not None:
                    results.append(process_results(result))
        else:
            # List of dicts:
            for row in decoded_gen:
                # so that item.key will be found:
                env[y] = row

                doit = all([eval(exp, env) for exp in compiled_ifs])

                if not doit:
                    continue

                try:
                    result = eval(code, env)
                except Exception:
                    result = None
                if result is not None:
                    results.append(process_results(result))
    retval = "[" + (",".join(results)) + "]"
    return retval


def get_database_connection(dgid):
    db_path = get_dg_path(dgid)
    conn = sqlite3.connect(db_path)
    conn.create_aggregate("STDEV", 1, StdevFunc)
    conn.create_function("ANY_IN_GROUP", 1, ANY_IN_GROUP)
    conn.create_function("ALL_IN_GROUP", 1, ALL_IN_GROUP)
    conn.create_function("FLATTEN", 1, FLATTEN)
    conn.create_function("SPLIT", -1, SPLIT)
    conn.create_function("LENGTH", 1, LENGTH)
    conn.create_function("KEYS_OF", 1, KEYS_OF)
    conn.create_function("VALUES_OF", 1, VALUES_OF)
    conn.create_function("IN_OBJ", 2, IN_OBJ)
    conn.create_function("ListComprehension", 4, ListComprehension)
    return conn


def get_completions(dgid):
    db_path = get_dg_path(dgid)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT name, other from metadata;")
    results = defaultdict(set)
    for row in rows:
        name, other = row
        if other:
            try:
                other = json.loads(row[1])
            except Exception:
                continue
            if "completions" in other:
                all_comp = other["completions"].keys()
                for comp in all_comp:
                    comp = comp if comp != "" else "."
                    name = name if not name.endswith("--metadata") else name[:-10]
                    path, item = comp.rsplit(".", 1)
                    if item and all(ch in VALID_CHARS for ch in item):
                        if not path.startswith("."):
                            path = "." + path
                        if not path.endswith("."):
                            path = path + "."
                        results['{"%s"}%s' % (name, path)].add(item)
    return {key: sorted(list(value)) for key, value in results.items()}


def get_metadata(conn):
    try:
        return _get_metadata(conn)
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL ERROR: %s", exc)
        raise Exception(str(exc))


def _get_metadata(conn):
    """
    Get the metadata for all columns.

    Returns a dict indexed by the user's names for columns,
    mapped to the metadata.
    """
    metadata_sql = "SELECT * FROM metadata"
    metadata = conn.execute(metadata_sql).fetchall()
    return {
        row[0]: {
            "name": row[0],
            "field_name": row[1],
            "field_expr": row[1],
            "type": row[2],
            "minimum": row[3],
            "maximum": row[4],
            "average": row[5],
            "variance": row[6],
            "total": row[7],
            "stddev": row[8],
            "other": row[9],
        }
        for row in metadata
    }


def plural(count, noun):
    if count == 0 or count > 1:
        if noun.endswith("s"):
            return "%s %ses" % (count, noun)
        else:
            return "%s %ss" % (count, noun)
    else:
        return "1 %s" % noun


def histogram(cur, metadata, values, column):
    statistics = {
        "count": 0,
        "min": 0,
        "max": 0,
        "mean": 0,
        "median": 0,
        "std": 0,
        "25%": 0,
        "50%": 0,
        "75%": 0,
        ## "count (NaN)": 0,
        "sum": 0,
    }
    stats = metadata[column]
    column_type = stats["type"]
    name = stats.get("name", column)

    if values:
        np_values = np.array(values, dtype=np.float)
        np_values = np_values[~np.isnan(np_values)]
    else:
        # How can this happen? The field changed
        # probably using random.random()
        np_values = np.array([], dtype=np.float)

    if stats["minimum"] is None:
        LOGGER.info(
            "column %r does not have pre-computed stats; computing on the fly", column
        )
        if values:
            minimum = np_values.min().item()
            maximum = np_values.max().item()
        else:
            minimum = maximum = 0
    else:
        minimum = stats["minimum"]
        maximum = stats["maximum"]

    range = (minimum, maximum)
    LOGGER.info("Computing histogram...")
    counts, labels = np.histogram(np_values, bins=HISTOGRAM_BINS, range=range)

    # Compute stats for this set:
    if values:
        try:
            quantiles = np.nanquantile(np_values, q=[0.25, 0.50, 0.75], axis=0)
            std = np.nanstd(np_values, axis=0, ddof=1).item()
            if is_nan(std):
                std = 0.0
            statistics = {
                "count": len(np_values),  # ok
                "min": np_values.min().item(),  # ok
                "max": np_values.max().item(),  # ok
                "mean": np.nanmean(np_values).item(),  # ok
                "median": np.nanmedian(np_values).item(),  # ok
                "std": std,  # ok
                "25%": quantiles[0].item(),  # ok
                "50%": quantiles[1].item(),  # ok
                "75%": quantiles[2].item(),  # ok
                ## "count (NaN)": "FIXME",
                "sum": np_values.sum().item(),  # ok
            }
        except Exception:
            LOGGER.info("failed in computing statistics")

    LOGGER.info("Done!")
    return {
        "type": "histogram",
        "bins": counts.tolist(),
        "labels": labels.tolist(),
        "min": minimum,
        "max": maximum,
        "columnType": column_type,
        "column": name,
        "statistics": statistics,
    }


def quote_value(value):
    # Escape single quote for SQL
    return "'%s'" % value.replace("'", "''")


def get_column_value(value):
    if value == "NULL" or value is None:
        return "NULL"
    elif isinstance(value, str):
        return quote_value(value)
    else:
        return value


def get_field_name(column, metadata):
    """
    Get field_name
    """
    if column in metadata:
        return metadata[column]["field_name"]
    else:
        return None


def get_field_expr(column, metadata):
    """
    Get field_expr
    """
    if column in metadata:
        return metadata[column]["field_expr"]
    else:
        raise Exception("no such column: %r" % column)


def get_column_type(column, metadata):
    if column in metadata:
        return metadata[column]["type"]
    raise Exception("no such column: %r" % column)


def get_dg_path(dgid):
    return os.path.join(KANGAS_ROOT, dgid)


def get_value_column_name(row, column_name, columns):
    index = columns.index(column_name)
    return row[index]


def get_type_column_name(column_name, columns, column_types):
    index = columns.index(column_name)
    column_type = column_types[index]
    if column_type.endswith("-ASSET"):
        name, asset = column_type.split("-", 1)
        return name.lower()
    else:
        raise Exception("Invalid use of get_type_column_name")


def get_group_by_rows(
    cur,
    group_by_field_name,
    group_by_field_expr,
    field_name,
    field_expr,
    column_value,
    where,
    databases,
    select_expr_as,
    distinct=False,
):
    env = {
        "group_by_field_name": group_by_field_name,
        "group_by_field_expr": group_by_field_expr,
        "field_name": field_name,
        "field_expr": field_expr,
        "column_value": get_column_value(column_value),
        "where": where,
        "databases": ", ".join(databases),
        "select_expr_as": ", ".join(select_expr_as),
        "distinct": "DISTINCT " if distinct else "",
    }

    select_sql = "SELECT value FROM (SELECT {select_expr_as}, {group_by_field_expr} AS {group_by_field_name}, GROUP_CONCAT({distinct}REPLACE(IFNULL({field_expr},'None'), ',', '&comma;')) as value FROM {databases} WHERE {where} GROUP BY {group_by_field_name}) WHERE {group_by_field_name} is {column_value}"
    selection_sql = select_sql.format(**env)
    LOGGER.info("SQL %s", selection_sql)
    start_time = time.time()
    cur.execute(selection_sql)
    LOGGER.info("SQL %s seconds", time.time() - start_time)
    rows = cur.fetchall()
    return rows


def select_histogram(
    dgid,
    group_by,
    where,
    column_name,
    column_value,
    where_description,
    computed_columns,
    where_expr,
):
    conn = get_database_connection(dgid)
    cur = conn.cursor()

    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]

    if computed_columns or where_expr:
        where_sql = update_state(
            dgid,
            computed_columns,
            metadata,
            databases,
            columns,
            select_expr_as,
            where_expr,
        )
        if where_sql:
            where = where_sql

    where = where if where else "1"

    field_name = get_field_name(column_name, metadata)
    field_expr = metadata[column_name]["field_expr"]
    group_by_field_name = get_field_name(group_by, metadata)
    group_by_field_expr = get_field_expr(group_by, metadata)

    try:
        rows = get_group_by_rows(
            cur,
            group_by_field_name,
            group_by_field_expr,
            field_name,
            field_expr,
            column_value,
            where,
            databases,
            select_expr_as,
        )
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL: %s", exc)
        raise Exception(str(exc))

    # These should be numbers:
    values = []
    if rows:
        row = rows[0]
        if row:
            if row[0]:
                LOGGER.info("Converting to list of values...")
                values = parse_comma_separated_values(row[0])
                LOGGER.info("Done!")
            if not isinstance(values, (list, tuple)):
                values = [values]

    results_json = histogram(cur, metadata, values, column_name)

    results_json["groupBy"] = group_by
    results_json["groupByValue"] = column_value
    results_json["whereDescription"] = where_description

    return results_json


def select_metadata(dgid):
    conn = get_database_connection(dgid)

    metadata = get_metadata(conn)

    return metadata


def select_description(
    dgid,
    group_by,
    where,
    column_name,
    column_value,
    where_description,
    computed_columns,
    where_expr,
):
    conn = get_database_connection(dgid)
    cur = conn.cursor()

    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]

    if computed_columns or where_expr:
        where_sql = update_state(
            dgid,
            computed_columns,
            metadata,
            databases,
            columns,
            select_expr_as,
            where_expr,
        )
        if where_sql:
            where = where_sql

    where = where if where else "1"

    column_type = metadata[column_name]["type"]
    field_name = get_field_name(column_name, metadata)
    field_expr = metadata[column_name]["field_expr"]
    group_by_field_name = get_field_name(group_by, metadata)
    group_by_field_expr = get_field_expr(group_by, metadata)

    try:
        rows = get_group_by_rows(
            cur,
            group_by_field_name,
            group_by_field_expr,
            field_name,
            field_expr,
            column_value,
            where,
            databases,
            select_expr_as,
        )
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL: %s", exc)
        raise Exception(str(exc))

    results_json = {"type": "verbatim", "value": "", "columnType": column_type}

    if rows:
        row = rows[0]
        if row:
            if row[0]:
                delims = row[0].count(",")
                if delims == 0:
                    results_json["value"] = row[0]
                else:
                    results_json["value"] = plural(delims + 1, column_name)

    return results_json


def select_category(
    dgid,
    group_by,
    where,
    column_name,
    column_value,
    where_description,
    computed_columns,
    where_expr,
):
    """
    column_value is the value of the group_by column.
    """
    conn = get_database_connection(dgid)
    cur = conn.cursor()

    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]

    if computed_columns or where_expr:
        where_sql = update_state(
            dgid,
            computed_columns,
            metadata,
            databases,
            columns,
            select_expr_as,
            where_expr,
        )
        if where_sql:
            where = where_sql

    where = where if where else "1"

    column_type = metadata[column_name]["type"]
    field_name = get_field_name(column_name, metadata)
    field_expr = get_field_expr(column_name, metadata)
    group_by_field_name = get_field_name(group_by, metadata)
    group_by_field_expr = get_field_expr(group_by, metadata)

    try:
        rows = get_group_by_rows(
            cur,
            group_by_field_name,
            group_by_field_expr,
            field_name,
            field_expr,
            column_value,
            where,
            databases,
            select_expr_as,
        )
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL: %s", exc)
        raise Exception(str(exc))

    # These are categories (ints or strings):
    results_json = {"type": "verbatim", "value": "", "columnType": column_type}
    if rows:
        row = rows[0]
        if row:
            raw_value = row[0]
            if raw_value:
                values = [v.replace("&comma;", ",") for v in raw_value.split(",")]
            else:
                values = []

            counts = Counter(values)
            length = len(values)
            unique_values = list(counts.keys())
            ulength = len(unique_values)

            if length == 0:
                results_json = {
                    "type": "verbatim",
                    "value": plural(length, column_name),
                    "columnType": column_type,
                }
            elif length == 1:
                results_json = {
                    "type": "verbatim",
                    "value": values[0],
                    "columnType": column_type,
                }
            elif ulength > MAX_CATEGORIES:
                if length == ulength:
                    results_json = {
                        "type": "verbatim",
                        "value": plural(length, column_name) + " unique values",
                        "columnType": column_type,
                    }
                else:
                    results_json = {
                        "type": "verbatim",
                        "value": plural(length, column_name)
                        + ", "
                        + str(ulength)
                        + " unique values",
                        "columnType": column_type,
                    }
            else:
                LOGGER.info("Sorting counts...")
                counts = {
                    key: value
                    for (key, value) in sorted(counts.items(), key=lambda item: item[1])
                }
                LOGGER.info("Done!")
                # values: {"Animal": 37, "Plant": 12}
                results_json = {
                    "type": "category",
                    "values": counts,
                    "column": column_name,
                    "columnType": column_type,
                    "groupBy": group_by,
                    "groupByValue": column_value,
                    "whereDescription": where_description,
                }

    return results_json


def select_asset_group_thumbnail(
    dgid,
    group_by,
    where,
    column_name,
    column_value,
    column_offset,
    computed_columns,
    where_expr,
    gallery_size,
    background_color,
    image_size,
    border_width,
    distinct,
):
    # get a gallery of images

    gallery_cols, gallery_rows = gallery_size
    column_limit = gallery_cols * gallery_rows
    background_color = tuple(background_color)
    image_size = tuple(image_size)

    results_json = select_asset_group(
        dgid,
        group_by,
        where,
        column_name,
        column_value,
        column_offset,
        column_limit,
        computed_columns,
        where_expr,
        distinct,
    )

    gallery_pixel_size = (
        (image_size[0] + border_width) * gallery_cols + border_width,
        (image_size[1] + border_width) * gallery_rows + border_width,
    )
    images = []
    for asset_id in results_json["values"]:
        image_data = select_asset(dgid, asset_id, thumbnail=True)
        image = generate_image(generate_thumbnail(image_data))
        background = Image.new(mode="RGBA", size=image_size, color=background_color)
        left = (background.size[0] - image.size[0]) // 2
        top = (background.size[1] - image.size[1]) // 2
        background.paste(image, (left, top))
        images.append(background)

    gallery_image = Image.new(
        mode="RGBA",
        size=gallery_pixel_size,
        color=background_color,
    )

    for i, image in enumerate(images):
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        location = (
            int((i % gallery_cols) * (image_size[0] + border_width)) + border_width,
            int((i // gallery_cols) * (image_size[1] + border_width)) + border_width,
        )
        gallery_image.paste(image, location)

    fp = image_to_fp(gallery_image, "png")
    return fp.read()


def select_asset_group(
    dgid,
    group_by,
    where,
    column_name,
    column_value,
    column_offset,
    column_limit,
    computed_columns,
    where_expr,
    distinct,
):
    conn = get_database_connection(dgid)
    cur = conn.cursor()

    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]

    if computed_columns or where_expr:
        where_sql = update_state(
            dgid,
            computed_columns,
            metadata,
            databases,
            columns,
            select_expr_as,
            where_expr,
        )
        if where_sql:
            where = where_sql

    where = where if where else "1"

    column_types = [metadata[key]["type"] for key in columns]
    group_by_field_name = get_field_name(group_by, metadata)
    group_by_field_expr = get_field_expr(group_by, metadata)
    field_name = get_field_name(column_name, metadata)
    field_expr = get_field_expr(column_name, metadata)

    try:
        rows = get_group_by_rows(
            cur,
            group_by_field_name,
            group_by_field_expr,
            field_name,
            field_expr,
            column_value,
            where,
            databases,
            select_expr_as,
            distinct,
        )
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL: %s", exc)
        raise Exception(str(exc))

    env = {
        "group_by_field_name": group_by_field_name,
        "field_name": field_name,
        "column_value": get_column_value(column_value),
        "where": where,
        "databases": ", ".join(databases),
        "select_expr_as": ", ".join(select_expr_as),
    }
    # These are assetIds (strings):
    select_sql = "SELECT value FROM (SELECT {select_expr_as}, COUNT({field_name}) as value FROM {databases} WHERE {where} GROUP BY {group_by_field_name}) WHERE {group_by_field_name} is {column_value};"
    selection_sql = select_sql.format(**env)
    LOGGER.info("SQL %s", selection_sql)
    start_time = time.time()
    try:
        cur.execute(selection_sql)
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL: %s; %s", selection_sql, exc)
        raise Exception(str(exc))

    LOGGER.info("SQL %s seconds", time.time() - start_time)
    total_rows = cur.fetchall()
    total = 0
    if total_rows:
        total_row = total_rows[0]
        if total_row:
            total = total_row[0]

    results_json = {
        "type": "asset-group",
        "assetType": get_type_column_name(column_name, columns, column_types),
        "values": [],
        "total": total,
    }
    if rows:
        row = rows[0]
        if row and row[0]:
            values = row[0].split(",")
            results_json = {
                "type": "asset-group",
                "assetType": get_type_column_name(column_name, columns, column_types),
                "values": values[column_offset : column_offset + column_limit]
                if column_limit is not None
                else values,
                "total": total,
            }
    return results_json


def select_asset_group_metadata(
    dgid,
    group_by,
    where,
    column_name,
    column_value,
    column_offset,
    column_limit,
    computed_columns,
    where_expr,
    distinct,
    metadata_path,
):
    conn = get_database_connection(dgid)
    cur = conn.cursor()

    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]

    if computed_columns or where_expr:
        where_sql = update_state(
            dgid,
            computed_columns,
            metadata,
            databases,
            columns,
            select_expr_as,
            where_expr,
        )
        if where_sql:
            where = where_sql

    where = where if where else "1"

    group_by_field_name = get_field_name(group_by, metadata)
    group_by_field_expr = get_field_expr(group_by, metadata)
    field_name = get_field_name(column_name, metadata)
    field_expr = get_field_expr(column_name, metadata)

    try:
        rows = get_group_by_rows(
            cur,
            group_by_field_name,
            group_by_field_expr,
            field_name,
            field_expr,
            column_value,
            where,
            databases,
            select_expr_as,
            distinct,
        )
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL: %s", exc)
        raise Exception(str(exc))

    results = set()
    if rows:
        row = rows[0]
        if row and row[0]:
            # asset_ids:
            values = row[0].split(",")
            if column_limit is not None:
                values = str(
                    tuple(values[column_offset : column_offset + column_limit])
                )
            else:
                values = str(tuple(values))
            metadata_field_name = get_field_name(column_name + "--metadata", metadata)
            if metadata_field_name is None:
                return []

            env = {
                "metadata_field_name": metadata_field_name,
                "field_name": field_name,
                "values": values,
            }
            sql = """SELECT {metadata_field_name} FROM datagrid WHERE {field_name} IN {values}""".format(
                **env
            )
            cur.execute(sql)
            all_asset_metadata = cur.fetchall()
            for asset_metadata in all_asset_metadata:
                json_metadata = json.loads(asset_metadata[0])
                ## FIXME: currently metadata_path is just a key; make work with a nested path:
                if metadata_path in json_metadata:
                    results.update(json_metadata[metadata_path])

    return sorted(list(results))


def verify_where(
    dgid,
    computed_columns,
    where_expr,
):
    conn = get_database_connection(dgid)
    cur = conn.cursor()

    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]
    where = None

    # FIXME:
    # Add the where_expr as a computed column, and return that
    # too in order to give some auto-completion hints

    # Expand to include computed columns
    if computed_columns or where_expr:
        try:
            where_sql = update_state(
                dgid,
                computed_columns,
                metadata,
                databases,
                columns,
                select_expr_as,
                where_expr,
            )
            if where_sql:
                where = where_sql
        except Exception as exc:
            return {
                "valid": False,
                "message": repr(exc),
            }

    where = where if where else "1"

    env = {
        "where": where,
        "select_expr_as": ", ".join(select_expr_as),
        "databases": ", ".join(databases),
    }
    select_sql = "SELECT {select_expr_as} FROM {databases} WHERE {where} LIMIT 1;"

    selection_sql = select_sql.format(**env)
    LOGGER.info("SQL %s", selection_sql)

    try:
        cur.execute(selection_sql)
    except sqlite3.OperationalError as exc:
        return {
            "valid": False,
            "message": repr(exc),
        }

    return {"valid": True, "message": "Query is valid"}


def query_sql(
    datagrid,
    column_name_map,
    where_expr,
    sort_by,
    sort_desc,
    count,
    computed_columns=None,
    limit=None,
    offset=0,
):
    dgid = datagrid.filename
    select_columns = datagrid.get_columns()
    if computed_columns:
        computed_columns = {
            key: {"field_expr": value, "field_name": "cc%d" % i, "type": "STRING"}
            for i, (key, value) in enumerate(computed_columns.items())
        }
        select_columns.extend(list(computed_columns.keys()))
    else:
        computed_columns = {}

    if count:
        results = select_query_count(
            dgid=dgid,
            group_by=None,
            computed_columns=computed_columns,
            where_expr=where_expr,
        )
        return results
    else:
        results = select_query_page(
            dgid=dgid,
            offset=offset,
            group_by=None,
            sort_by=sort_by,
            sort_desc=sort_desc,
            where=None,
            limit=limit,
            select_columns=select_columns,
            computed_columns=computed_columns,
            where_expr=where_expr,
        )
        return results["rows"]


def select_query_count(
    dgid,
    group_by,
    computed_columns,
    where_expr=None,
):
    conn = get_database_connection(dgid)
    cur = conn.cursor()
    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]
    where = None

    if computed_columns or where_expr:
        where_sql = update_state(
            dgid,
            computed_columns,
            metadata,
            databases,
            columns,
            select_expr_as,
            where_expr,
        )
        if where_sql:
            where = where_sql

    where = where if where else "1"

    env = {
        "where": where,
        "select_expr_as": ", ".join(select_expr_as),
        "databases": ", ".join(databases),
    }

    if group_by:
        env["group_by_field_name"] = get_field_name(group_by, metadata)
        total_sql = "SELECT COUNT() from (SELECT {select_expr_as} FROM {databases} GROUP BY {group_by_field_name});"
    else:
        total_sql = "SELECT COUNT() FROM (SELECT {select_expr_as} FROM {databases} WHERE {where});"
    selection_sql = total_sql.format(**env)
    LOGGER.info("SQL %s", selection_sql)
    start_time = time.time()
    total_rows = cur.execute(selection_sql).fetchone()[0]
    LOGGER.info("SQL %s seconds", time.time() - start_time)
    return total_rows


def select_query(
    dgid,
    offset,
    group_by,
    sort_by,
    sort_desc,
    where,
    limit,
    select_columns,
    computed_columns,
    where_expr=None,
):
    result = select_query_page(
        dgid,
        offset,
        group_by,
        sort_by,
        sort_desc,
        where,
        limit,
        select_columns,
        computed_columns,
        where_expr,
    )
    result["total"] = select_query_count(
        dgid,
        group_by,
        computed_columns,
        where_expr,
    )
    return result


def select_query_page(
    dgid,
    offset,
    group_by,
    sort_by,
    sort_desc,
    where,
    limit,
    select_columns,
    computed_columns,
    where_expr=None,
):
    sort_desc = "DESC" if sort_desc else "ASC"
    conn = get_database_connection(dgid)
    cur = conn.cursor()

    metadata = get_metadata(conn)
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]

    # NOTE: use Image.attr to get metadata

    # Expand to include computed columns
    if computed_columns or where_expr:
        where_sql = update_state(
            dgid,
            computed_columns,
            metadata,
            databases,
            columns,
            select_expr_as,
            where_expr,
        )
        if where_sql:
            where = where_sql

    where = where if where else "1"
    limit = ("LIMIT %s OFFSET %s" % (limit, offset)) if limit is not None else ""

    # Metadata now has computed_columns:
    if select_columns:
        select_fields = [get_field_name(column, metadata) for column in select_columns]
    else:
        select_fields = [get_field_name(column, metadata) for column in columns]
        select_columns = columns

    sort_by_field_name = get_field_name(sort_by, metadata) if sort_by else "rowid"
    remove_columns = []

    if group_by:
        if group_by not in select_columns:
            select_columns.append(group_by)
            select_fields.append(get_field_name(group_by, metadata))
            remove_columns.append(group_by)

        group_by_field_name = get_field_name(group_by, metadata)
        env = {
            "limit": limit,
            "group_by_field_name": group_by_field_name,
            "sort_by_field_name": sort_by_field_name,
            "where": where,
            "sort_desc": sort_desc,
            "select_expr_as": ", ".join(select_expr_as),
            "select_fields": ", ".join(select_fields),
            "databases": ", ".join(databases),
        }
        select_sql = "SELECT {select_expr_as} FROM {databases} WHERE {where} GROUP BY {group_by_field_name} ORDER BY {sort_by_field_name} {sort_desc} {limit}"
    else:
        env = {
            "limit": limit,
            "sort_by_field_name": sort_by_field_name,
            "where": where,
            "sort_desc": sort_desc,
            "select_expr_as": ", ".join(select_expr_as),
            "select_fields": ", ".join(select_fields),
            "databases": ", ".join(databases),
        }
        select_sql = "SELECT {select_expr_as} FROM {databases} WHERE {where} ORDER BY {sort_by_field_name} {sort_desc} {limit}"

    if len(select_columns) != len(columns):
        select_sql = "SELECT {select_fields} FROM (%s);" % select_sql
    else:
        select_sql = "%s;" % select_sql
    selection_sql = select_sql.format(**env)
    LOGGER.info("SQL %s", selection_sql)
    start_time = time.time()
    try:
        cur.execute(selection_sql)
    except sqlite3.OperationalError as exc:
        LOGGER.error("SQL: %s; %s", selection_sql, exc)
        raise Exception(str(exc))

    LOGGER.info("SQL %s seconds", time.time() - start_time)
    rows = cur.fetchall()

    if group_by:
        group_by_field_name = get_field_name(group_by, metadata)
        # Add cell messages for groups and assets:
        rows = list(rows)
        for r in range(len((rows))):
            row = dict(zip(select_columns, rows[r]))
            group_by_value = row[group_by]
            for select_column in select_columns:
                column_type = get_column_type(select_column, metadata)
                column_value = row[select_column]
                column_field_name = get_field_name(select_column, metadata)
                if column_field_name == group_by_field_name:
                    if column_type.endswith("-ASSET"):
                        asset_type = column_type.split("-", 1)[0].lower()
                        row[select_column] = {
                            "type": "asset",
                            "assetType": asset_type,
                            "assetId": column_value,
                        }
                    else:
                        pass  # don't change value of group-by column
                else:  # all of the rest should be grouped
                    cell = {
                        "dgid": dgid,
                        "groupBy": group_by,
                        "columnName": select_column,
                        "columnValue": group_by_value,
                        "whereExpr": where_expr,
                    }
                    if column_type == "ROW_ID":
                        cell["type"] = "row-group"
                    elif column_type == "FLOAT":
                        cell["type"] = "float-group"
                    elif column_type == "TEXT":
                        cell["type"] = "text-group"
                    elif column_type == "INTEGER":
                        cell["type"] = "integer-group"
                    elif column_type == "DATETIME":
                        cell["type"] = "datetime-group"
                    elif column_type == "BOOLEAN":
                        cell["type"] = "boolean-group"
                    elif column_type == "JSON":
                        cell["type"] = "json-group"
                    else:  # Asset types
                        asset_type = column_type.split("-", 1)[0].lower()
                        cell["type"] = "asset-group"
                        cell["assetType"] = asset_type
                    row[select_column] = cell

            for column in remove_columns:
                row.pop(column)

            rows[r] = row
    else:
        # Add asset messages:
        rows = list(rows)
        for r in range(len((rows))):
            row = dict(zip(select_columns, rows[r]))
            for select_column in select_columns:
                column_type = get_column_type(select_column, metadata)
                column_value = row[select_column]
                if column_type.endswith("-ASSET"):
                    asset_type = column_type.split("-", 1)[0].lower()
                    row[select_column] = {
                        "type": "asset",
                        "assetType": asset_type,
                        "assetId": column_value,
                    }
                elif column_type == "JSON":
                    try:
                        row[select_column] = json.loads(row[select_column])
                    except Exception:
                        # FIXME: invalid JSON; ignore?
                        pass

            for column in remove_columns:
                row.pop(column)

            rows[r] = row

    for column in remove_columns:
        select_columns.remove(column)

    return {
        "columns": select_columns,
        "columnTypes": [
            get_column_type(select_column, metadata) for select_column in select_columns
        ],
        "nrows": len(rows),
        "ncols": len(select_columns),
        "rows": rows,
    }


## Query Builder Interface


def datatype_to_qbtype(datatype):
    if datatype == "ROW_ID":
        return "number"
    elif datatype == "INTEGER":
        return "number"
    elif datatype == "FLOAT":
        return "number"
    elif datatype == "BOOLEAN":
        return "boolean"
    elif datatype == "TEXT":
        return "text"
    elif datatype == "DATETIME":
        return "datetime"
    elif datatype == "JSON":
        # Just to allow building of the subfields:
        return "JSON"
    ## FIXME: QB also has: date, time, select,
    ##   multiselect, treeselect, treemultiselect
    else:
        return None


def get_fields(dgid, metadata=None, computed_columns=None):
    """
    Get the fields from the metadata and return needed
    information to construct the QueryBuilder.
    """
    # NOTE: metadata does not contain computed_columns yet
    if metadata is None:
        conn = get_database_connection(dgid)
        metadata = get_metadata(conn)

    # Used to evaluate computed columns
    columns = list(metadata.keys())
    select_expr_as = [get_field_name(column, metadata) for column in columns]
    databases = ["datagrid"]

    if computed_columns:
        # Only passed in when calling from endpoint
        update_state(
            dgid, computed_columns, metadata, databases, columns, select_expr_as
        )
        # Now metadata has computed columns

    fields = {}
    for column in metadata:
        datatype = metadata[column]["type"]
        field_name = get_field_name(column, metadata)
        qbtype = datatype_to_qbtype(datatype)
        if qbtype is None:
            continue

        if datatype in ["FLOAT", "INTEGER", "ROW_ID"]:
            fields[field_name] = {
                "label": column,
                "field": field_name,
                "type": qbtype,
                "tooltip": "The '%s' column (type '%s') from the data grid"
                % (column, qbtype),
            }
            # name, datatype, min, max, avg, variance, total, stddev, other
            if (metadata[column]["minimum"] is not None) and (
                metadata[column]["minimum"] is not None
            ):
                min_value = metadata[column]["minimum"]
                max_value = metadata[column]["maximum"]
                fields[field_name]["fieldSettings"] = {
                    "min": min_value,
                    "max": max_value,
                }
            fields[field_name]["valueSources"] = ["value", "field"]

        elif datatype == "DATETIME":
            field_exp = "datetime(%s, 'unixepoch')" % field_name
            fields[field_exp] = {
                "label": column,
                "field": field_name,
                "type": qbtype,
                "tooltip": "The '%s' column (type '%s') from the data grid"
                % (column, qbtype),
            }
            if (metadata[column]["minimum"] is not None) and (
                metadata[column]["minimum"] is not None
            ):
                min_value = metadata[column]["minimum"]
                max_value = metadata[column]["maximum"]
                fields[field_exp]["fieldSettings"] = {
                    "min": min_value,
                    "max": max_value,
                    # "dateFormat": "DD-MM-YYYY",
                    # "timeFormat":
                    # "valueFormat":
                }
            fields[field_exp]["valueSources"] = [
                "value",
                "field",
                "func",
            ]  # adds Now, and Relative

        elif datatype == "BOOLEAN":
            fields[field_name] = {
                "label": column,
                "field": field_name,
                "type": qbtype,
                "tooltip": "The '%s' column (type '%s') from the data grid"
                % (column, qbtype),
            }
            fields[field_name]["fieldSettings"] = {
                "labelYes": "True",
                "labelNo": "False",
            }
            fields[field_name]["valueSources"] = ["value", "field"]

        elif datatype == "TEXT":
            fields[field_name] = {
                "label": column,
                "field": field_name,
                "type": qbtype,
                "tooltip": "The '%s' column (type '%s') from the data grid"
                % (column, qbtype),
            }
            fields[field_name]["valueSources"] = ["value", "field"]

        elif datatype == "JSON":
            # Asset metadata columns are named
            # 'COLUMN_NAME.metadata' or 'COLUMN_NAME--metadata'
            fields[field_name] = {
                "label": column.replace(".metadata", "").replace("--metadata", ""),
                "field": field_name,
                "tooltip": "The '%s' column (type 'JSON') from the data grid"
                % (column,),
                "type": "!struct",
                "subfields": {},
            }
            subfields = ast.literal_eval(metadata[column]["other"])
            # Only filterable keys are in subfields
            for key in subfields:
                # Query Builder filter types: "text", "number", "boolean", or "list-of-text"
                qbtype = subfields[key]["type"]
                if qbtype == "list-of-text":
                    field_exp = "json_extract(%s, '$.%s')" % (field_name, key)
                    fields[field_name]["subfields"][field_exp] = {
                        "type": "text",
                        "label": key,
                        "field": field_name,
                        "tableName": "1",  # special signal for JSON queries in our QueryBuilder
                        "operators": ["like"],
                    }
                else:
                    field_exp = "json_extract(%s, '$.%s')" % (field_name, key)
                    fields[field_name]["subfields"][field_exp] = {
                        "type": qbtype,
                        "label": key,
                        "field": field_name,
                        "tableName": "1",  # special signal for JSON queries in our QueryBuilder
                    }
                    if "values" in subfields[key]:
                        fields[field_name]["subfields"][field_exp]["type"] = "select"
                        fields[field_name]["subfields"][field_exp]["fieldSettings"] = {
                            "listValues": sorted(subfields[key]["values"])
                        }

    return fields


def select_asset(dgid, asset_id, thumbnail=False):
    conn = get_database_connection(dgid)
    cur = conn.cursor()
    selection = 'SELECT asset_data, asset_type, asset_thumbnail from assets where asset_id = "{asset_id}";'
    env = {"asset_id": asset_id}
    selection_sql = selection.format(**env)
    LOGGER.info("SQL %s", selection_sql)
    start_time = time.time()
    row = cur.execute(selection_sql).fetchone()
    LOGGER.info("SQL %s seconds", time.time() - start_time)
    if row:
        asset_data, asset_type, asset_thumbnail = row
        if thumbnail and asset_type in ["Image"]:
            if asset_thumbnail:
                return asset_thumbnail
            else:
                return generate_thumbnail(asset_data)
        else:
            return asset_data

    return None


def select_asset_metadata(dgid, asset_id):
    conn = get_database_connection(dgid)
    cur = conn.cursor()
    selection = 'SELECT asset_metadata from assets where asset_id = "{asset_id}";'
    env = {"asset_id": asset_id}
    selection_sql = selection.format(**env)
    LOGGER.info("SQL %s", selection_sql)
    start_time = time.time()
    row = cur.execute(selection_sql).fetchone()
    LOGGER.info("SQL %s seconds", time.time() - start_time)
    if row:
        return row[0]
    return None


def walk(top, maxdepth=3):
    dirs, nondirs = [], []
    try:
        for entry in os.scandir(top):
            (dirs if entry.is_dir() else nondirs).append(entry.path)
    except Exception:
        pass
    yield top, dirs, sorted(nondirs)
    if maxdepth > 1:
        for path in dirs:
            yield from walk(path, maxdepth - 1)


def list_datagrids():
    """
    List all datagrids, recursively, starting in KANGAS_ROOT.
    Defaults to the directory in which datagrid server was started.
    """
    # In stand-alone mode, dgid is filename and label
    filenames = [
        filename
        for directory, dirs, files in walk(KANGAS_ROOT)
        for filename in files
        if filename.endswith(".datagrid")
    ]

    return [
        {
            "value": filename,
            "label": filename,
            "timestamp": os.path.getmtime(filename),
        }
        for filename in filenames
    ]


def get_datagrid_timestamp(dgid):
    # In stand-alone mode, dgid is filename and label
    db_path = get_dg_path(dgid)
    timestamp = os.path.getmtime(db_path)
    result = {"dgid": dgid, "label": dgid, "timestamp": timestamp}
    return result


def make_limited_env():
    local_env = {}
    byte_code = compile(CUSTOM_CODE_INIT, "<inline>", "exec")
    exec(byte_code, globals(), local_env)
    return local_env


def custom_output(input, code):
    """
    A restricted environment for executing user code.  To be
    completely secure, you'd want a remote container to execute user
    code.

    `input` is a dict representing the contents of the cell
    in the format:

    ```
    {
     "value": VALUE,
     "type": TYPE,
     "metadata": METADATA,
    }
    ```

    where:

        value: the contents of the cell
        type: `IMAGE`, `TEXT`, `FLOAT`, etc.
        metadata: (optional) a dict of user-supplied info

    `code` has access to common data science stack, already
    imported. Assign the computed output to the variable
    `output`. The output should be in the same form as
    the input.

    """
    from RestrictedPython import compile_restricted
    from RestrictedPython.Eval import default_guarded_getitem
    from RestrictedPython.PrintCollector import PrintCollector
    from RestrictedPython.Utilities import utility_builtins

    # from RestrictedPython import safe_globals
    # from RestrictedPython.Guards import safe_builtins

    local_env = make_limited_env()
    local_env["_print_"] = PrintCollector

    local_env["input"] = input
    local_env["_getattr_"] = getattr
    local_env["_getitem_"] = default_guarded_getitem
    local_env["Exception"] = Exception

    code = """
try:
    {code}
except Exception as exc:
    STDERR = format_exc()
STDOUT = printed
""".format(
        code="\n".join(["    %s" % line for line in code.split("\n")])
    )

    restricted_globals = dict(__builtins__=utility_builtins)

    try:
        byte_code = compile_restricted(code, "<inline>", "exec")
        exec(byte_code, restricted_globals, local_env)
    except Exception as exc:
        return {
            "value": None,
            "type": None,
            "metadata": None,
            "stderr": str(exc),
            "stdout": "",
        }

    output = local_env.get("output", {"value": None, "type": None, "metadata": None})
    if not isinstance(output, dict):
        output = {
            "value": output,
            "type": pytype_to_dgtype(output),
            "metadata": None,
        }
    else:
        if "value" not in output:
            output["value"] = None

        if "type" not in output:
            output["type"] = pytype_to_dgtype(output["value"])

        if "metadata" not in output:
            output["metadata"] = None

    if "STDERR" in local_env:
        output["stderr"] = local_env["STDERR"]
    else:
        output["stderr"] = ""
    output["stdout"] = local_env.get("STDOUT", "")
    return output
