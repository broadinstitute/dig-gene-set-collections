#!/usr/bin/env python3
"""
Export gene sets from the SQLite database to a tab-delimited GMT-style file.

usage: python3 src/python/create_geneset_gmt.py --output_file output/genesets.gmt --db_file path/to/database.db --log_file output/export.log

  python3 src/python/create_geneset_gmt.py --db_file path/to/database.db --output_file output/genesets.gmt

  python3 src/python/create_geneset_gmt.py --db_file path/to/database.db --output_dir output/collections
  
"""

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, TextIO, Tuple

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
QUERY = """
select gset.collection_name, gset.standard_name, gset.gmt_gene_set_description, gene.symbol
from gene_set gset, gene_symbol gene, gene_set_gene_symbol link
where gset.gene_set_id = link.gene_set_id
and gene.gene_symbol_id = link.gene_symbol_id
order by gset.collection_name, gset.standard_name, gene.symbol
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


def fetch_gene_set_rows(db_path: str) -> Iterable[Tuple[str, str, str, str]]:
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
    output_handle: TextIO,
    gene_set_name: str,
    gene_set_description: str,
    genes: List[str],
    seen_gene_sets: set[str],
) -> bool:
    """Write one gene set row unless the name has already been written."""
    if gene_set_name in seen_gene_sets:
        logger.warning(f"Skipping duplicate gene set name: {gene_set_name}")
        return False

    output_handle.write('\t'.join([gene_set_name, gene_set_description, *genes]) + '\n')
    seen_gene_sets.add(gene_set_name)
    logger.info(f"Created gene set row '{gene_set_name}' with {len(genes)} genes")
    return True


def finalize_gene_set(
    output_handle: TextIO,
    current_gene_set: Optional[str],
    current_gene_set_description: Optional[str],
    current_genes: List[str],
    seen_gene_sets: set[str],
) -> bool:
    """Write the current gene set if one is buffered."""
    if current_gene_set is None or current_gene_set_description is None:
        return False

    return write_gene_set(
        output_handle,
        current_gene_set,
        current_gene_set_description,
        current_genes,
        seen_gene_sets,
    )


def export_gene_sets(db_path: str, output_file: str) -> int:
    """Export all gene sets to a single tab-delimited file."""
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    current_gene_set: Optional[str] = None
    current_gene_set_description: Optional[str] = None
    current_genes: List[str] = []
    seen_gene_sets: set[str] = set()
    written_gene_sets = 0

    with output_path.open('w', encoding='utf-8') as output_handle:
        for _, gene_set_name, gene_set_description, gene_symbol in fetch_gene_set_rows(db_path):
            if current_gene_set is None:
                current_gene_set = gene_set_name
                current_gene_set_description = gene_set_description

            if gene_set_name != current_gene_set:
                if finalize_gene_set(
                    output_handle,
                    current_gene_set,
                    current_gene_set_description,
                    current_genes,
                    seen_gene_sets,
                ):
                    written_gene_sets += 1
                current_gene_set = gene_set_name
                current_gene_set_description = gene_set_description
                current_genes = []

            current_genes.append(gene_symbol)

        if finalize_gene_set(
            output_handle,
            current_gene_set,
            current_gene_set_description,
            current_genes,
            seen_gene_sets,
        ):
            written_gene_sets += 1

    logger.info(f"Wrote {written_gene_sets} gene sets to {output_path}")
    return written_gene_sets


def build_collection_output_path(output_dir: str, collection_name: str) -> Path:
    """Build an output path for one collection GMT file."""
    safe_name = ''.join(
        character if character.isalnum() or character in {'-', '_', '.'} else '_'
        for character in collection_name
    ).strip('._')
    file_name = f"{safe_name or 'collection'}.gmt"
    return Path(output_dir) / file_name


def export_gene_sets_by_collection(db_path: str, output_dir: str) -> int:
    """Export one GMT file per collection_name."""
    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)

    current_collection: Optional[str] = None
    current_gene_set: Optional[str] = None
    current_gene_set_description: Optional[str] = None
    current_genes: List[str] = []
    current_output_handle: Optional[TextIO] = None
    current_output_path: Optional[Path] = None
    seen_gene_sets: set[str] = set()
    written_gene_sets = 0

    try:
        for collection_name, gene_set_name, gene_set_description, gene_symbol in fetch_gene_set_rows(db_path):
            if current_collection != collection_name:
                if current_output_handle is not None:
                    if finalize_gene_set(
                        current_output_handle,
                        current_gene_set,
                        current_gene_set_description,
                        current_genes,
                        seen_gene_sets,
                    ):
                        written_gene_sets += 1
                    current_output_handle.close()
                    logger.info(f"Wrote collection GMT file to {current_output_path}")

                current_collection = collection_name
                current_output_path = build_collection_output_path(output_dir, collection_name)
                current_output_path.parent.mkdir(parents=True, exist_ok=True)
                current_output_handle = current_output_path.open('w', encoding='utf-8')
                seen_gene_sets = set()
                current_gene_set = None
                current_gene_set_description = None
                current_genes = []

            if current_gene_set is None:
                current_gene_set = gene_set_name
                current_gene_set_description = gene_set_description

            if gene_set_name != current_gene_set:
                if finalize_gene_set(
                    current_output_handle,
                    current_gene_set,
                    current_gene_set_description,
                    current_genes,
                    seen_gene_sets,
                ):
                    written_gene_sets += 1
                current_gene_set = gene_set_name
                current_gene_set_description = gene_set_description
                current_genes = []

            current_genes.append(gene_symbol)

        if current_output_handle is not None:
            if finalize_gene_set(
                current_output_handle,
                current_gene_set,
                current_gene_set_description,
                current_genes,
                seen_gene_sets,
            ):
                written_gene_sets += 1
            current_output_handle.close()
            logger.info(f"Wrote collection GMT file to {current_output_path}")
    finally:
        if current_output_handle is not None and not current_output_handle.closed:
            current_output_handle.close()

    logger.info(f"Wrote {written_gene_sets} gene sets across collection GMT files in {output_dir_path}")
    return written_gene_sets


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Create a tab-delimited gene set GMT file from a SQLite database'
    )
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        '--output_file',
        help='Path to the output GMT file'
    )
    output_group.add_argument(
        '--output_dir',
        help='Directory where one GMT file per collection_name will be written'
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
    if args.output_file:
        export_gene_sets(args.db_file, args.output_file)
    else:
        export_gene_sets_by_collection(args.db_file, args.output_dir)


if __name__ == '__main__':
    main()
