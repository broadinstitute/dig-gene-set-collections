from flask import Flask, jsonify, request

from utils.db_utils import get_gene_set_data


app = Flask(__name__)


@app.get("/gene-set")
def gene_set() -> tuple:
    gene_set_id = request.args.get("gene_set_id", type=int)
    if gene_set_id is None:
        return jsonify({"error": "gene_set_id query parameter is required"}), 400

    data = get_gene_set_data(gene_set_id)
    if data is None:
        return jsonify({"error": f"gene_set_id {gene_set_id} not found"}), 404

    return jsonify(data), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
