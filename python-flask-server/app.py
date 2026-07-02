import logging

from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from utils.db_utils import (
    get_gene_set_data,
    get_gene_set_graph,
    get_gene_set_provenance,
    list_gene_sets,
    search_gene_sets,
)
from utils.web_utils import fetch_provenance_node_content


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
logging.basicConfig(level=logging.INFO)


def _external_request_url() -> str:
    original_uri = (
        request.headers.get("X-Original-URI")
        or request.headers.get("X-Forwarded-Uri")
        or request.environ.get("REQUEST_URI")
    )
    if original_uri:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}{original_uri}"

    forwarded_prefix = request.headers.get("X-Forwarded-Prefix", "")
    return f"{request.scheme}://{request.host}{forwarded_prefix}{request.full_path.rstrip('?')}"


@app.before_request
def log_request_url() -> None:
    app.logger.info("request_url=%s", _external_request_url())


@app.get("/genetics_provider/geneset_extractor/gene-set/")
@app.get("/genetics_provider/geneset_extractor/gene-set")
@app.get("/gene-set/")
@app.get("/gene-set")
def gene_set() -> tuple:
    gene_set_identifier = request.args.get("gene_set_id", type=str)
    if not gene_set_identifier:
        return jsonify({"error": "gene_set_id query parameter is required"}), 400

    data = get_gene_set_data(gene_set_identifier)
    if data is None:
        return jsonify({"error": f"gene_set_id {gene_set_identifier} not found"}), 404

    return jsonify(data), 200


@app.get("/genetics_provider/geneset_extractor/gene-sets/")
@app.get("/genetics_provider/geneset_extractor/gene-sets")
@app.get("/gene-sets/")
@app.get("/gene-sets")
def gene_sets() -> tuple:
    return jsonify(list_gene_sets(2000)), 200


@app.get("/genetics_provider/geneset_extractor/gene_set_provenance/")
@app.get("/genetics_provider/geneset_extractor/gene_set_provenance")
@app.get("/gene_set_provenance/")
@app.get("/gene_set_provenance")
def gene_set_provenance() -> tuple:
    gene_set_id = request.args.get("gene_set_id", type=int)
    if gene_set_id is None:
        return jsonify({"error": "gene_set_id query parameter is required"}), 400

    data = get_gene_set_provenance(gene_set_id)
    if data is None:
        return jsonify({"error": f"gene_set_id {gene_set_id} not found"}), 404

    return jsonify(data), 200


@app.get("/genetics_provider/geneset_extractor/gene_set_graph/")
@app.get("/genetics_provider/geneset_extractor/gene_set_graph")
@app.get("/gene_set_graph/")
@app.get("/gene_set_graph")
def gene_set_graph() -> tuple:
    gene_set_id = request.args.get("gene_set_id", type=int)
    if gene_set_id is None:
        return jsonify({"error": "gene_set_id query parameter is required"}), 400

    data = get_gene_set_graph(gene_set_id)
    if data is None:
        return jsonify({"error": f"gene_set_id {gene_set_id} not found"}), 404

    return jsonify(data), 200


@app.get("/genetics_provider/geneset_extractor/search/")
@app.get("/genetics_provider/geneset_extractor/search")
@app.get("/genetics_provider/search/")
@app.get("/genetics_provider/search")
@app.get("/search/")
@app.get("/search")
def search() -> tuple:
    search_string = request.args.get("q", type=str)
    if not search_string:
        return jsonify({"error": "q query parameter is required"}), 400

    limit = request.args.get("limit", default=200, type=int)
    return jsonify(search_gene_sets(search_string, limit)), 200


@app.get("/genetics_provider/geneset_extractor/provenance_node/<int:provenance_node_id>/")
@app.get("/genetics_provider/geneset_extractor/provenance_node/<int:provenance_node_id>")
@app.get("/genetics_provider/provenance_node/<int:provenance_node_id>/")
@app.get("/genetics_provider/provenance_node/<int:provenance_node_id>")
@app.get("/provenance_node/<int:provenance_node_id>/")
@app.get("/provenance_node/<int:provenance_node_id>")
def provenance_node(provenance_node_id: int):
    return fetch_provenance_node_content(provenance_node_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
