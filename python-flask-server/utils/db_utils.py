from __future__ import annotations

import json
import sqlite3
from pathlib import Path


# DB_PATH = Path(__file__).resolve().parents[1] / "data" / "genseco_v0.sqlite"
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "genseco_v20260528.sqlite"


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _normalize_gene_set_identifier(gene_set_identifier: int | str) -> int | str:
    if isinstance(gene_set_identifier, int):
        return gene_set_identifier

    normalized_identifier = gene_set_identifier.strip()
    if normalized_identifier.isdigit():
        return int(normalized_identifier)

    return normalized_identifier


def _escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_json_field(field_name: str, raw_value: str | None) -> tuple[object | None, str | None]:
    if raw_value is None:
        return None, None

    try:
        return json.loads(raw_value), None
    except json.JSONDecodeError as exc:
        return None, f"{field_name} contains invalid JSON: {exc.msg}"


def _get_gene_set_row_by_identifier(
    connection: sqlite3.Connection,
    gene_set_identifier: int | str,
    selected_columns: str,
) -> sqlite3.Row | None:
    normalized_identifier = _normalize_gene_set_identifier(gene_set_identifier)
    if isinstance(normalized_identifier, int):
        where_clause = "gs.gene_set_id = ?"
    else:
        where_clause = "gs.standard_name = ?"

    return connection.execute(
        f"""
        SELECT
            {selected_columns}
        FROM gene_set AS gs
        LEFT JOIN provenance AS p
            ON p.gene_set_id = gs.gene_set_id
        WHERE {where_clause}
        """,
        (normalized_identifier,),
    ).fetchone()


def _build_gene_set_provenance_response(
    gene_set_row: sqlite3.Row,
    geneset_metadata: dict | None,
    knowledge_graph: dict,
) -> dict:
    metadata = geneset_metadata or {}
    gene_set_meta = metadata.get("gene_set", {})
    provenance_meta = metadata.get("provenance", {})
    converter_meta = metadata.get("converter", {})
    input_meta = metadata.get("input", {})
    lineage_meta = metadata.get("lineage", {})
    output_meta = metadata.get("output", {})

    return {
        "access_route": None,
        "card_id": gene_set_row["standard_name"],
        "comparison_space_organism": gene_set_meta.get("organism"),
        "contrast_label": gene_set_meta.get("description"),
        "dataset_unit_title": gene_set_meta.get("name") or gene_set_row["standard_name"],
        "dataset_unit_type": gene_set_meta.get("data_type"),
        "extractor": converter_meta,
        "extractor_input": input_meta,
        "extractor_lineage": lineage_meta,
        "extractor_notes": None,
        "extractor_output_files": output_meta.get("files", []),
        "focus_node": provenance_meta.get("focus_node_id"),
        "geneset_provenance_path": provenance_meta.get("path"),
        "knowledge_graph": knowledge_graph,
        "landing_page": None,
        "meta_path": output_meta.get("files", [{}])[-1].get("path") if output_meta.get("files") else None,
        "modality": gene_set_meta.get("assay"),
        "organism": gene_set_meta.get("organism"),
        "primary_access_url": None,
        "provenance_graph_path": provenance_meta.get("path"),
        "publication_ids": [],
        "resource_name": gene_set_row["collection_name"],
        "signature_path": gene_set_meta.get("primary_artifact", {}).get("path"),
        "source_dataset_unit": gene_set_row["standard_name"],
        "source_files": input_meta.get("files", []),
        "source_resource": gene_set_row["collection_name"],
        "tissue_or_system": None,
    }


def _parse_json_blob(field_name: str, raw_value: str | None) -> object | None:
    parsed, _ = _parse_json_field(field_name, raw_value)
    return parsed


def _build_knowledge_graph(
    provenance_graph: dict | None,
    provenance_node_rows: list[sqlite3.Row],
    provenance_edge_rows: list[sqlite3.Row],
) -> dict:
    graph_id = next(iter(provenance_graph), None) if provenance_graph else None

    nodes = []
    for row in provenance_node_rows:
        properties = _parse_json_blob("additional_properties", row["additional_properties"]) or {}
        original_id = properties.get("original_id")
        node = {
            "id": original_id or f"provenance_node:{row['provenance_node_id']}",
            "name": row["name"],
            "type": row["node_type"],
            "description": row["description"],
            "dcc_url": row["dcc_url"],
            "drc_url": row["drc_url"],
        }
        for key, value in properties.items():
            if key != "original_id":
                node[key] = value
        nodes.append(node)

    node_id_map = {
        row["provenance_node_id"]: (
            (_parse_json_blob("additional_properties", row["additional_properties"]) or {}).get("original_id")
            or f"provenance_node:{row['provenance_node_id']}"
        )
        for row in provenance_node_rows
    }

    edges = []
    for row in provenance_edge_rows:
        properties = _parse_json_blob("additional_properties", row["additional_properties"]) or {}
        original_id = properties.get("original_id")
        edge = {
            "id": original_id or f"provenance_edge:{row['provenance_edge_id']}",
            "source": node_id_map.get(row["source_node_id"], f"provenance_node:{row['source_node_id']}"),
            "target": node_id_map.get(row["target_node_id"], f"provenance_node:{row['target_node_id']}"),
            "label": row["label"],
            "description": row["description"],
        }
        for key, value in properties.items():
            if key != "original_id":
                edge[key] = value
        edges.append(edge)

    return {
        "graph_id": graph_id,
        "nodes": nodes,
        "edges": edges,
    }


def get_gene_set_data(gene_set_identifier: int | str) -> dict | None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        gene_set_row = _get_gene_set_row_by_identifier(
            connection,
            gene_set_identifier,
            """
            gs.gene_set_id,
            gs.standard_name,
            gs.collection_name,
            gs.tags,
            gs.license_code,
            p.provenance_graph,
            p.geneset_metadata
            """,
        )

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
            (gene_set_row["gene_set_id"],),
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


def list_gene_sets(limit: int = 20, collection: str | None = None) -> list[dict]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        safe_limit = max(1, limit)
        if collection:
            rows = connection.execute(
                """
                SELECT
                    gene_set_id,
                    standard_name,
                    collection_name,
                    tags,
                    license_code
                FROM gene_set
                WHERE collection_name = ?
                ORDER BY gene_set_id
                LIMIT ?
                """,
                (collection, safe_limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT
                    gene_set_id,
                    standard_name,
                    collection_name,
                    tags,
                    license_code
                FROM gene_set
                ORDER BY gene_set_id
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        return [_row_to_dict(row) for row in rows]
    finally:
        connection.close()


def get_gene_set_provenance(gene_set_identifier: int | str) -> dict | None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        row = _get_gene_set_row_by_identifier(
            connection,
            gene_set_identifier,
            """
            gs.gene_set_id,
            gs.standard_name,
            gs.collection_name,
            p.provenance_graph,
            p.geneset_metadata
            """,
        )

        if row is None:
            return None

        geneset_metadata, geneset_metadata_error = _parse_json_field(
            "geneset_metadata", row["geneset_metadata"]
        )
        provenance_graph, provenance_graph_error = _parse_json_field(
            "provenance_graph", row["provenance_graph"]
        )
        provenance_node_rows = connection.execute(
            """
            SELECT
                provenance_node_id,
                node_type,
                name,
                description,
                dcc_url,
                drc_url,
                additional_properties
            FROM provenance_node
            WHERE gene_set_id = ?
            ORDER BY provenance_node_id
            """,
            (row["gene_set_id"],),
        ).fetchall()
        provenance_edge_rows = connection.execute(
            """
            SELECT
                provenance_edge_id,
                source_node_id,
                target_node_id,
                label,
                description,
                additional_properties
            FROM provenance_edge
            WHERE gene_set_id = ?
            ORDER BY provenance_edge_id
            """,
            (row["gene_set_id"],),
        ).fetchall()
        knowledge_graph = _build_knowledge_graph(
            provenance_graph, provenance_node_rows, provenance_edge_rows
        )

        response = _build_gene_set_provenance_response(row, geneset_metadata, knowledge_graph)
        if geneset_metadata_error is not None:
            response["geneset_metadata_error"] = geneset_metadata_error
        if provenance_graph_error is not None:
            response["provenance_graph_error"] = provenance_graph_error

        return response
    finally:
        connection.close()


def get_gene_set_graph(gene_set_identifier: int | str) -> list[dict] | None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        gene_set_row = _get_gene_set_row_by_identifier(
            connection,
            gene_set_identifier,
            "gs.gene_set_id",
        )

        if gene_set_row is None:
            return None

        rows = connection.execute(
            """
            SELECT
                pedge.gene_set_id,
                pedge.provenance_edge_id AS edge_id,
                pedge.label AS edge_name,
                pedge.source_node_id AS source_id,
                snode.node_type AS source_type,
                snode.name AS source_name,
                pedge.target_node_id AS target_id,
                tnode.node_type AS target_type,
                tnode.name AS target_name
            FROM provenance_node AS snode,
                 provenance_node AS tnode,
                 provenance_edge AS pedge
            WHERE pedge.gene_set_id = ?
              AND snode.provenance_node_id = pedge.source_node_id
              AND tnode.provenance_node_id = pedge.target_node_id
            ORDER BY pedge.provenance_edge_id
            """,
            (gene_set_row["gene_set_id"],),
        ).fetchall()

        return [_row_to_dict(row) for row in rows]
    finally:
        connection.close()


def search_gene_sets(search_string: str, limit: int = 200) -> list[dict]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        safe_limit = max(1, limit)
        pattern = f"%{_escape_like_pattern(search_string)}%"
        rows = connection.execute(
            """
            SELECT
                gene_set_id,
                standard_name,
                collection_name,
                tags,
                license_code
            FROM gene_set
            WHERE standard_name LIKE ? ESCAPE '\\'
            ORDER BY standard_name
            LIMIT ?
            """,
            (pattern, safe_limit),
        ).fetchall()

        return [_row_to_dict(row) for row in rows]
    finally:
        connection.close()


def list_collections() -> list[dict]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute(
            """
            SELECT
                collection_name AS name,
                COUNT(*) AS number
            FROM gene_set
            WHERE collection_name IS NOT NULL
            GROUP BY collection_name
            ORDER BY collection_name
            """
        ).fetchall()

        return [_row_to_dict(row) for row in rows]
    finally:
        connection.close()
