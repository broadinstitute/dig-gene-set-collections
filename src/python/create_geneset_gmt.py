#!/usr/bin/env python3
"""
Export gene sets from the SQLite database to a tab-delimited GMT-style file.

usage: python3 src/python/create_geneset_gmt.py --output_file output/genesets.gmt --db_file path/to/database.db --log_file output/export.log

"""

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
QUERY = """
select gset.standard_name, gene.symbol
from gene_set gset, gene_symbol gene, gene_set_gene_symbol link
where gset.gene_set_id = link.gene_set_id
and gene.gene_symbol_id = link.gene_symbol_id
order by gset.standard_name, gene.symbol
"""

logger = logging.getLogger(__name__)


def configure_logging(output_log: Optional[str] = None) -> None:
    """Configure console logging and optional file logging."""
    handlers = [logging.StreamHandler()]

    if output_log:
        log_path = Path(output_log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


def fetch_gene_set_rows(db_path: str) -> Iterable[Tuple[str, str]]:
    """Yield gene set and gene symbol rows from the database."""
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute(QUERY)
        for row in cursor:
            yield row
    finally:
        connection.close()


def write_gene_set(
    output_handle,
    gene_set_name: str,
    genes: List[str],
    seen_gene_sets: set[str],
) -> bool:
    """Write one gene set row unless the name has already been written."""
    if gene_set_name in seen_gene_sets:
        logger.warning(f"Skipping duplicate gene set name: {gene_set_name}")
        return False

    output_handle.write('\t'.join([gene_set_name, *genes]) + '\n')
    seen_gene_sets.add(gene_set_name)
    logger.info(f"Created gene set row '{gene_set_name}' with {len(genes)} genes")
    return True


def export_gene_sets(db_path: str, output_file: str) -> int:
    """Export grouped gene sets to a tab-delimited file."""
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    current_gene_set: Optional[str] = None
    current_genes: List[str] = []
    seen_gene_sets: set[str] = set()
    written_gene_sets = 0

    with output_path.open('w', encoding='utf-8') as output_handle:
        for gene_set_name, gene_symbol in fetch_gene_set_rows(db_path):
            if current_gene_set is None:
                current_gene_set = gene_set_name

            if gene_set_name != current_gene_set:
                if write_gene_set(output_handle, current_gene_set, current_genes, seen_gene_sets):
                    written_gene_sets += 1
                current_gene_set = gene_set_name
                current_genes = []

            current_genes.append(gene_symbol)

        if current_gene_set is not None:
            if write_gene_set(output_handle, current_gene_set, current_genes, seen_gene_sets):
                written_gene_sets += 1

    logger.info(f"Wrote {written_gene_sets} gene sets to {output_path}")
    return written_gene_sets


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Create a tab-delimited gene set GMT file from a SQLite database'
    )
    parser.add_argument(
        '--output_file',
        required=True,
        help='Path to the output GMT file'
    )
    parser.add_argument(
        '--db_file',
        required=True,
        help='Path to the SQLite database file'
    )
    parser.add_argument(
        '--log_file',
        help='Optional path to a log file. If omitted, logs are only written to stderr.'
    )

    args = parser.parse_args()
    configure_logging(args.log_file)
    export_gene_sets(args.db_file, args.output_file)


if __name__ == '__main__':
    main()
