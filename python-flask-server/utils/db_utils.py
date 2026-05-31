from __future__ import annotations

import json
import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "data" / "genseco_v0.sqlite"


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _parse_json_field(field_name: str, raw_value: str | None) -> tuple[object | None, str | None]:
    if raw_value is None:
        return None, None

    try:
        return json.loads(raw_value), None
    except json.JSONDecodeError as exc:
        return None, f"{field_name} contains invalid JSON: {exc.msg}"


def get_gene_set_data(gene_set_id: int) -> dict | None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        gene_set_row = connection.execute(
            """
            SELECT
                gs.gene_set_id,
                gs.standard_name,
                gs.collection_name,
                gs.tags,
                gs.license_code,
                p.provenance_graph,
                p.geneset_metadata
            FROM gene_set AS gs
            LEFT JOIN provenance AS p
                ON p.gene_set_id = gs.gene_set_id
            WHERE gs.gene_set_id = ?
            """,
            (gene_set_id,),
        ).fetchone()

        if gene_set_row is None:
            return None

        gene_symbol_rows = connection.execute(
            """
            SELECT
                gsgs.gene_symbol_id,
                gsym.symbol,
                gsym.NCBI_id,
                gsym.namespace_id
            FROM gene_set_gene_symbol AS gsgs
            JOIN gene_symbol AS gsym
                ON gsym.gene_symbol_id = gsgs.gene_symbol_id
            WHERE gsgs.gene_set_id = ?
            ORDER BY gsgs.gene_symbol_id
            """,
            (gene_set_id,),
        ).fetchall()

        gene_set_data = _row_to_dict(gene_set_row)
        provenance_graph, provenance_error = _parse_json_field(
            "provenance_graph", gene_set_data.get("provenance_graph")
        )
        gene_set_data["provenance_graph"] = provenance_graph
        if provenance_error is not None:
            gene_set_data["provenance_graph_error"] = provenance_error

        geneset_metadata, geneset_metadata_error = _parse_json_field(
            "geneset_metadata", gene_set_data.get("geneset_metadata")
        )
        gene_set_data["geneset_metadata"] = geneset_metadata
        if geneset_metadata_error is not None:
            gene_set_data["geneset_metadata_error"] = geneset_metadata_error

        gene_set_data["gene_symbols"] = [_row_to_dict(row) for row in gene_symbol_rows]
        return gene_set_data
    finally:
        connection.close()
