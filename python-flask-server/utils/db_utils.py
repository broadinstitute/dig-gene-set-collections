from __future__ import annotations

import json
import sqlite3
from pathlib import Path


# DB_PATH = Path(__file__).resolve().parents[1] / "data" / "genseco_v0.sqlite"
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "genseco_v20260528.sqlite"


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _parse_json_field(field_name: str, raw_value: str | None) -> tuple[object | None, str | None]:
    if raw_value is None:
        return None, None

    try:
        return json.loads(raw_value), None
    except json.JSONDecodeError as exc:
        return None, f"{field_name} contains invalid JSON: {exc.msg}"


def _build_gene_set_provenance_response(
    gene_set_row: sqlite3.Row,
    geneset_metadata: dict | None,
    provenance_graph: dict | None,
) -> dict:
    metadata = geneset_metadata or {}
    gene_set_meta = metadata.get("gene_set", {})
    provenance_meta = metadata.get("provenance", {})
    converter_meta = metadata.get("converter", {})
    input_meta = metadata.get("input", {})
    lineage_meta = metadata.get("lineage", {})
    output_meta = metadata.get("output", {})

    graph_id = next(iter(provenance_graph), None) if provenance_graph else None
    graph_body = provenance_graph.get(graph_id, {}) if graph_id is not None else {}

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
        "knowledge_graph": {
            "graph_id": graph_id,
            "nodes": graph_body.get("nodes", []),
            "edges": graph_body.get("edges", []),
        },
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


def list_gene_sets(limit: int = 20) -> list[dict]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
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
            (limit,),
        ).fetchall()

        return [_row_to_dict(row) for row in rows]
    finally:
        connection.close()


def get_gene_set_provenance(gene_set_id: int) -> dict | None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        row = connection.execute(
            """
            SELECT
                gs.gene_set_id,
                gs.standard_name,
                gs.collection_name,
                p.provenance_graph,
                p.geneset_metadata
            FROM gene_set AS gs
            LEFT JOIN provenance AS p
                ON p.gene_set_id = gs.gene_set_id
            WHERE gs.gene_set_id = ?
            """,
            (gene_set_id,),
        ).fetchone()

        if row is None:
            return None

        geneset_metadata, geneset_metadata_error = _parse_json_field(
            "geneset_metadata", row["geneset_metadata"]
        )
        provenance_graph, provenance_graph_error = _parse_json_field(
            "provenance_graph", row["provenance_graph"]
        )

        response = _build_gene_set_provenance_response(row, geneset_metadata, provenance_graph)
        if geneset_metadata_error is not None:
            response["geneset_metadata_error"] = geneset_metadata_error
        if provenance_graph_error is not None:
            response["provenance_graph_error"] = provenance_graph_error

        return response
    finally:
        connection.close()
