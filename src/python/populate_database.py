#!/usr/bin/env python3
"""
Populate GenSeCoDB database from GMT gene set files.

Usage:
    python populate_database.py --db-path database.db --schema-file schema.sql (--data-root /path/to/data | --s3-data-root s3://bucket/prefix) [--output-log /path/to/logfile.log]
"""

import argparse
import sqlite3
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - depends on runtime environment
    boto3 = None
    ClientError = None

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logger = logging.getLogger(__name__)


def configure_logging(output_log: Optional[str] = None):
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


@dataclass(frozen=True)
class DataFileRef:
    """Reference to either a local file or an S3 object."""
    location: str
    parent_location: str
    path_parts: Tuple[str, ...]
    is_s3: bool


class GeneSeCoDatabasePopulator:
    """Populate GenSeCoDB database from GMT files."""

    def __init__(
        self,
        db_path: str,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        aws_region: Optional[str] = None,
    ):
        """Initialize database connection."""
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self.s3_client = None
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_session_token = aws_session_token
        self.aws_region = aws_region
        self.next_available_node_id = 1  # Track next available node ID for spacing gene sets

    def connect(self):
        """Connect to database."""
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        # Enable foreign keys
        self.cursor.execute('PRAGMA foreign_keys = ON')
        logger.info(f"Connected to database: {self.db_path}")

    def disconnect(self):
        """Close database connection."""
        if self.conn:
            self.conn.commit()
            self.conn.close()
            logger.info("Database connection closed")

    def initialize_schema(self, schema_file: str):
        """Create database tables and indexes from schema file."""
        try:
            schema_path = Path(schema_file)
            if not schema_path.exists():
                raise FileNotFoundError(f"Schema file not found: {schema_file}")
            
            logger.info(f"Initializing database schema from {schema_file}...")
            with open(schema_path, 'r') as f:
                schema_sql = f.read()
            
            self.cursor.executescript(schema_sql)
            self.conn.commit()
            logger.info("Database schema initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing schema: {e}")
            raise

    def synchronize_next_available_node_id(self):
        """Sync the next reserved ID with the current database contents."""
        max_gene_set_id = 0
        max_provenance_node_id = 0

        try:
            self.cursor.execute('SELECT COALESCE(MAX(gene_set_id), 0) FROM gene_set')
            max_gene_set_id = self.cursor.fetchone()[0] or 0
        except sqlite3.OperationalError:
            pass

        try:
            self.cursor.execute('SELECT COALESCE(MAX(provenance_node_id), 0) FROM provenance_node')
            max_provenance_node_id = self.cursor.fetchone()[0] or 0
        except sqlite3.OperationalError:
            pass

        self.next_available_node_id = max(max_gene_set_id, max_provenance_node_id) + 1
        logger.info(f"Next available node ID initialized to {self.next_available_node_id}")

    def find_next_unused_node_id(self, start_id: Optional[int] = None) -> int:
        """Find the next unused ID across gene_set and provenance_node."""
        candidate_id = start_id or self.next_available_node_id

        while True:
            self.cursor.execute(
                'SELECT 1 FROM gene_set WHERE gene_set_id = ?',
                (candidate_id,)
            )
            gene_set_exists = self.cursor.fetchone() is not None

            self.cursor.execute(
                'SELECT 1 FROM provenance_node WHERE provenance_node_id = ?',
                (candidate_id,)
            )
            provenance_node_exists = self.cursor.fetchone() is not None

            if not gene_set_exists and not provenance_node_exists:
                return candidate_id

            candidate_id += 1

    def resolve_collection_name(self, data_file: DataFileRef, fallback_collection_name: str) -> str:
        """Resolve the collection/prefix name for a gene set source."""
        if data_file.is_s3 and len(data_file.path_parts) > 1:
            return data_file.path_parts[0]

        return fallback_collection_name

    def get_s3_client(self):
        """Lazily initialize the S3 client."""
        if boto3 is None:
            raise ImportError("boto3 is required when using --s3-data-root")

        if self.s3_client is None:
            client_kwargs = {}

            access_key_id = self.aws_access_key_id or os.getenv('AWS_ACCESS_KEY_ID')
            secret_access_key = self.aws_secret_access_key or os.getenv('AWS_SECRET_ACCESS_KEY')
            session_token = self.aws_session_token or os.getenv('AWS_SESSION_TOKEN')
            region = self.aws_region or os.getenv('AWS_DEFAULT_REGION') or os.getenv('AWS_REGION')

            if access_key_id and secret_access_key:
                client_kwargs['aws_access_key_id'] = access_key_id
                client_kwargs['aws_secret_access_key'] = secret_access_key
                if session_token:
                    client_kwargs['aws_session_token'] = session_token
                logger.info("Using explicit AWS access key credentials for S3 access")
            elif access_key_id or secret_access_key:
                raise ValueError(
                    "Both AWS access key ID and secret access key must be provided together "
                    "via CLI options or environment variables"
                )

            if region:
                client_kwargs['region_name'] = region

            self.s3_client = boto3.client('s3', **client_kwargs)

        return self.s3_client

    def parse_s3_uri(self, s3_uri: str) -> Tuple[str, str]:
        """Parse an S3 URI into bucket and key prefix."""
        if not s3_uri.startswith('s3://'):
            raise ValueError(f"Invalid S3 URI: {s3_uri}")

        remainder = s3_uri[5:]
        if not remainder or remainder.startswith('/'):
            raise ValueError(f"Invalid S3 URI: {s3_uri}")

        bucket, _, key = remainder.partition('/')
        return bucket, key.rstrip('/')

    def build_s3_uri(self, bucket: str, key: str = '') -> str:
        """Build a normalized S3 URI."""
        return f"s3://{bucket}/{key}" if key else f"s3://{bucket}"

    def build_child_location(self, base_location: str, child_name: str, is_s3: bool) -> str:
        """Build a child path or S3 URI from a base location."""
        if is_s3:
            bucket, key = self.parse_s3_uri(base_location)
            child_key = f"{key}/{child_name}" if key else child_name
            return self.build_s3_uri(bucket, child_key)

        return str(Path(base_location) / child_name)

    def read_text(self, location: str, is_s3: bool) -> str:
        """Read a text file from local disk or S3."""
        if is_s3:
            bucket, key = self.parse_s3_uri(location)
            response = self.get_s3_client().get_object(Bucket=bucket, Key=key)
            return response['Body'].read().decode('utf-8', errors='ignore')

        with open(location, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()

    def read_json_file(self, location: str, is_s3: bool) -> Optional[str]:
        """Read JSON content from local disk or S3, returning None for missing files."""
        try:
            data = self.read_text(location, is_s3)
        except FileNotFoundError:
            return None
        except Exception as e:
            if is_s3 and ClientError is not None and isinstance(e, ClientError):
                error_code = e.response.get('Error', {}).get('Code')
                if error_code in {'404', 'NoSuchKey', 'NotFound'}:
                    return None
            raise

        parsed = json.loads(data)
        return json.dumps(parsed)

    def find_gmt_files(
        self,
        root_path: Optional[str] = None,
        s3_root: Optional[str] = None
    ) -> List[DataFileRef]:
        """Find all .gmt files under a local path or S3 prefix."""
        if bool(root_path) == bool(s3_root):
            raise ValueError("Provide exactly one of root_path or s3_root")

        if s3_root:
            bucket, prefix = self.parse_s3_uri(s3_root)
            paginator = self.get_s3_client().get_paginator('list_objects_v2')
            gmt_files = []

            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if not key.endswith('.gmt'):
                        continue

                    key_path = PurePosixPath(key)
                    parent_key = '' if str(key_path.parent) == '.' else str(key_path.parent)
                    gmt_files.append(
                        DataFileRef(
                            location=self.build_s3_uri(bucket, key),
                            parent_location=self.build_s3_uri(bucket, parent_key),
                            path_parts=key_path.parts,
                            is_s3=True,
                        )
                    )

            logger.info(f"Found {len(gmt_files)} .gmt files in {s3_root}")
            return gmt_files

        root = Path(root_path)
        if not root.exists():
            raise ValueError(f"Data root path does not exist: {root_path}")
        
        gmt_files = [
            DataFileRef(
                location=str(path),
                parent_location=str(path.parent),
                path_parts=path.parts,
                is_s3=False,
            )
            for path in root.rglob('*.gmt')
        ]
        logger.info(f"Found {len(gmt_files)} .gmt files in {root_path}")
        return gmt_files

    def parse_gmt_file(self, gmt_file: DataFileRef) -> List[Tuple[str, List[str]]]:
        """
        Parse GMT file.
        
        Format: <gene_set_name>TAB<gene_1> <gene_2> ... <gene_n>
        
        Returns:
            List of (gene_set_name, gene_list) tuples
        """
        gene_sets = []
        try:
            for line_num, line in enumerate(self.read_text(gmt_file.location, gmt_file.is_s3).splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                
                parts = line.split('\t')
                if len(parts) < 2:
                    logger.warning(f"{gmt_file.location}:{line_num} - Invalid line format (missing tab)")
                    continue
                
                gene_set_name = parts[0]
                # Second column is typically description (optional in some GMT files)
                # Genes are space-separated, starting from the second column
                genes_str = '\t'.join(parts[1:])
                genes = genes_str.split()
                
                if not genes:
                    logger.warning(f"{gmt_file.location}:{line_num} - No genes found for {gene_set_name}")
                    continue
                
                gene_sets.append((gene_set_name, genes))
            
            logger.info(f"Parsed {len(gene_sets)} gene sets from {gmt_file.location}")
            return gene_sets
        except Exception as e:
            logger.error(f"Error parsing {gmt_file.location}: {e}")
            return []

    def insert_species(self, species_code: str, species_name: str) -> int:
        """Insert or get species ID."""
        try:
            self.cursor.execute(
                'SELECT species_id FROM species WHERE species_code = ?',
                (species_code,)
            )
            result = self.cursor.fetchone()
            if result:
                return result[0]
            
            self.cursor.execute(
                'INSERT INTO species (species_code, species_name) VALUES (?, ?)',
                (species_code, species_name)
            )
            self.conn.commit()
            return self.cursor.lastrowid
        except sqlite3.IntegrityError as e:
            logger.warning(f"Species insert error: {e}")
            # Try to get existing ID
            self.cursor.execute(
                'SELECT species_id FROM species WHERE species_code = ?',
                (species_code,)
            )
            result = self.cursor.fetchone()
            return result[0] if result else None

    def insert_namespace(self, label: str, species_code: str) -> int:
        """Insert or get namespace ID."""
        try:
            self.cursor.execute(
                'SELECT namespace_id FROM namespace WHERE label = ? AND species_code = ?',
                (label, species_code)
            )
            result = self.cursor.fetchone()
            if result:
                return result[0]
            
            self.cursor.execute(
                'INSERT INTO namespace (label, species_code) VALUES (?, ?)',
                (label, species_code)
            )
            self.conn.commit()
            return self.cursor.lastrowid
        except sqlite3.IntegrityError as e:
            logger.warning(f"Namespace insert error: {e}")
            self.cursor.execute(
                'SELECT namespace_id FROM namespace WHERE label = ? AND species_code = ?',
                (label, species_code)
            )
            result = self.cursor.fetchone()
            return result[0] if result else None

    def insert_gene_symbol(self, symbol: str, namespace_id: int, ncbi_id: str = None) -> int:
        """Insert or get gene symbol ID."""
        try:
            self.cursor.execute(
                'SELECT gene_symbol_id FROM gene_symbol WHERE symbol = ? AND namespace_id = ?',
                (symbol, namespace_id)
            )
            result = self.cursor.fetchone()
            if result:
                return result[0]
            
            self.cursor.execute(
                'INSERT INTO gene_symbol (symbol, namespace_id, NCBI_id) VALUES (?, ?, ?)',
                (symbol, namespace_id, ncbi_id)
            )
            self.conn.commit()
            return self.cursor.lastrowid
        except sqlite3.IntegrityError as e:
            logger.warning(f"Gene symbol insert error: {e}")
            self.cursor.execute(
                'SELECT gene_symbol_id FROM gene_symbol WHERE symbol = ? AND namespace_id = ?',
                (symbol, namespace_id)
            )
            result = self.cursor.fetchone()
            return result[0] if result else None

    def insert_collection(self, collection_name: str, full_name: str = None, description: str = None) -> int:
        """Insert or get collection ID."""
        try:
            self.cursor.execute(
                'SELECT collection_id FROM collection WHERE collection_name = ?',
                (collection_name,)
            )
            result = self.cursor.fetchone()
            if result:
                return result[0]
            
            self.cursor.execute(
                'INSERT INTO collection (collection_name, full_name, description) VALUES (?, ?, ?)',
                (collection_name, full_name or collection_name, description or '')
            )
            self.conn.commit()
            return self.cursor.lastrowid
        except sqlite3.IntegrityError as e:
            logger.warning(f"Collection insert error: {e}")
            self.cursor.execute(
                'SELECT collection_id FROM collection WHERE collection_name = ?',
                (collection_name,)
            )
            result = self.cursor.fetchone()
            return result[0] if result else None

    def insert_gene_set_license(self, license_code: str, license_name: str = None) -> int:
        """Insert or get gene set license ID."""
        try:
            self.cursor.execute(
                'SELECT gene_set_license_id FROM gene_set_license WHERE license_code = ?',
                (license_code,)
            )
            result = self.cursor.fetchone()
            if result:
                return result[0]
            
            self.cursor.execute(
                'INSERT INTO gene_set_license (license_code, license_name) VALUES (?, ?)',
                (license_code, license_name or license_code)
            )
            self.conn.commit()
            return self.cursor.lastrowid
        except sqlite3.IntegrityError as e:
            logger.warning(f"License insert error: {e}")
            self.cursor.execute(
                'SELECT gene_set_license_id FROM gene_set_license WHERE license_code = ?',
                (license_code,)
            )
            result = self.cursor.fetchone()
            return result[0] if result else None

    def insert_gene_set(
        self,
        standard_name: str,
        collection_name: str,
        license_code: str,
        tags: str = None,
        gene_set_id: int = None
    ) -> Tuple[Optional[int], bool]:
        """Insert gene set with optional explicit ID.

        Returns:
            Tuple of (gene_set_id, was_inserted).
        """
        try:
            if gene_set_id is not None:
                # Insert with explicit ID
                self.cursor.execute(
                    'INSERT INTO gene_set (gene_set_id, standard_name, collection_name, license_code, tags) VALUES (?, ?, ?, ?, ?)',
                    (gene_set_id, standard_name, collection_name, license_code, tags)
                )
                return (gene_set_id, True)
            else:
                # Auto-increment ID
                self.cursor.execute(
                    'INSERT INTO gene_set (standard_name, collection_name, license_code, tags) VALUES (?, ?, ?, ?)',
                    (standard_name, collection_name, license_code, tags)
                )
                self.conn.commit()
                return (self.cursor.lastrowid, True)
        except sqlite3.IntegrityError as e:
            logger.warning(f"Gene set insert error for {standard_name}: {e}")
            # Try to get existing ID
            self.cursor.execute(
                'SELECT gene_set_id FROM gene_set WHERE standard_name = ?',
                (standard_name,)
            )
            result = self.cursor.fetchone()
            return (result[0], False) if result else (None, False)

    def insert_gene_set_gene_symbol(self, gene_set_id: int, gene_symbol_id: int):
        """Insert gene set gene symbol association."""
        try:
            self.cursor.execute(
                'INSERT INTO gene_set_gene_symbol (gene_set_id, gene_symbol_id) VALUES (?, ?)',
                (gene_set_id, gene_symbol_id)
            )
        except sqlite3.IntegrityError:
            pass  # Duplicate entry

    def load_provenance_and_metadata(
        self, 
        gene_set_name: str, 
        root_location: str,
        is_s3: bool
    ) -> Optional[Tuple[str, str, Optional[str]]]:
        """
        Load provenance_graph, geneset_metadata, and run_summary for a gene set.
        
        Looks for:
        - geneset.provenance.json
        - geneset.meta.json
        - run_summary.json (optional)
        
        Returns:
            Tuple of (provenance_graph, geneset_metadata, run_summary) if required files exist.
            run_summary is optional. None returned if required files are missing.
        """
        try:
            provenance_file = self.build_child_location(root_location, "geneset.provenance.json", is_s3)
            metadata_file = self.build_child_location(root_location, "geneset.meta.json", is_s3)
            run_summary_file = self.build_child_location(root_location, "run_summary.json", is_s3)

            provenance_graph = self.read_json_file(provenance_file, is_s3)
            geneset_metadata = self.read_json_file(metadata_file, is_s3)
            
            if provenance_graph is None or geneset_metadata is None:
                return None

            run_summary = self.read_json_file(run_summary_file, is_s3)
            return (provenance_graph, geneset_metadata, run_summary)
        
        except Exception as e:
            logger.debug(f"Error loading provenance/metadata for {gene_set_name}: {e}")
            return None

    def insert_gene_set_details(
        self,
        gene_set_id: int,
        gene_set_name: str,
        metadata_json_str: str,
        species_code: str,
        primary_namespace_id: int,
        contrib_organization: str = None
    ):
        """
        Insert gene_set_details record from parsed metadata JSON.
        
        Maps fields from geneset.meta.json into the gene_set_details table:
        - systematic_name: gene_set_name (unique, e.g. AC10__blood__pos)
        - description_brief / description_full: from gene_set.description
        - exact_source: signature_name + variant + geneset_id
        - external_details_URL: converter.code.repo_url (or notebook_url)
        - source_species_code, primary_namespace_id, num_namespaces: provided
        """
        try:
            try:
                metadata = json.loads(metadata_json_str) if metadata_json_str else {}
            except json.JSONDecodeError:
                metadata = {}
            
            gene_set_meta = metadata.get('gene_set', {}) if isinstance(metadata, dict) else {}
            converter = metadata.get('converter', {}) if isinstance(metadata, dict) else {}
            code = converter.get('code', {}) if isinstance(converter, dict) else {}
            params = converter.get('parameters', {}) if isinstance(converter, dict) else {}
            
            description_full = gene_set_meta.get('description') or ''
            # Brief description: first sentence or first 250 chars
            description_brief = description_full.split('. ')[0].strip()
            if description_brief and not description_brief.endswith('.'):
                description_brief += '.'
            if len(description_brief) > 250:
                description_brief = description_brief[:247] + '...'
            
            # exact_source: identifier from upstream (signature + emitted variant)
            signature_name = params.get('signature_name') if isinstance(params, dict) else None
            geneset_uid = metadata.get('geneset_id') if isinstance(metadata, dict) else None
            exact_source_parts = []
            if signature_name:
                exact_source_parts.append(signature_name)
            # Derive variant suffix from gene_set_name (e.g. AC10__blood__pos -> pos)
            if signature_name and gene_set_name.startswith(signature_name):
                suffix = gene_set_name[len(signature_name):].lstrip('_')
                if suffix:
                    exact_source_parts.append(suffix)
            if geneset_uid:
                exact_source_parts.append(f"geneset:{geneset_uid}")
            exact_source = ' | '.join(exact_source_parts) if exact_source_parts else None
            
            # external URL: prefer notebook_url, fall back to repo_url
            external_url = (code.get('notebook_url')
                            or code.get('script_url')
                            or code.get('repo_url')) if isinstance(code, dict) else None
            
            self.cursor.execute(
                '''INSERT OR REPLACE INTO gene_set_details
                   (gene_set_id, description_brief, description_full, systematic_name,
                    exact_source, external_details_URL, source_species_code,
                    primary_namespace_id, num_namespaces, contrib_organization)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    gene_set_id,
                    description_brief or None,
                    description_full or None,
                    gene_set_name,
                    exact_source,
                    external_url,
                    species_code,
                    primary_namespace_id,
                    1,
                    contrib_organization,
                )
            )
        except sqlite3.IntegrityError as e:
            logger.warning(f"gene_set_details insert error for {gene_set_name} (id={gene_set_id}): {e}")
        except Exception as e:
            logger.error(f"Error inserting gene_set_details for {gene_set_name}: {e}")

    def insert_provenance(
        self,
        gene_set_id: int,
        provenance_graph: str,
        geneset_metadata: str,
        run_summary: str = None
    ):
        """Insert provenance record."""
        try:
            self.cursor.execute(
                'INSERT INTO provenance (gene_set_id, provenance_graph, geneset_metadata, run_summary) VALUES (?, ?, ?, ?)',
                (gene_set_id, provenance_graph, geneset_metadata, run_summary)
            )
        except sqlite3.IntegrityError as e:
            logger.warning(f"Provenance insert error for gene_set_id {gene_set_id}: {e}")

    def insert_provenance_nodes_and_edges(
        self,
        gene_set_id: int,
        provenance_json_str: str,
        geneset_node_name: str = None,
        geneset_node_name_match: str = None
    ) -> int:
        """
        Insert provenance nodes and edges from provenance JSON string.
        
        The GeneSet node gets provenance_node_id = gene_set_id.
        Other nodes get sequential auto-assigned IDs.
        
        Args:
            gene_set_id: ID of the gene set (also used for the GeneSet node)
            provenance_json_str: Full provenance JSON as string (parsed from geneset.provenance.json)
            geneset_node_name: If provided, override the GeneSet node's `name` field with this value
                (used to keep gene_set.standard_name and provenance_node.name in sync).
            geneset_node_name_match: Reserved for future variant-disambiguation; currently unused.
        
        Returns:
            The maximum provenance_node_id used, or 0 if no nodes/edges were inserted.
        """
        max_node_id_used = 0
        
        try:
            provenance_data = json.loads(provenance_json_str)
            logger.debug(f"Provenance JSON type: {type(provenance_data).__name__}")
            
            # Handle different JSON structures
            nodes_to_process = []
            edges_to_process = []
            
            if isinstance(provenance_data, dict):
                # Check if nodes/edges are at top level
                if 'nodes' in provenance_data and 'edges' in provenance_data:
                    nodes_to_process = provenance_data.get('nodes', [])
                    edges_to_process = provenance_data.get('edges', [])
                    logger.debug(f"Found nodes/edges at top level: {len(nodes_to_process)} nodes, {len(edges_to_process)} edges")
                else:
                    # Otherwise iterate through top-level keys looking for nodes/edges
                    for gs_key, gs_data in provenance_data.items():
                        if isinstance(gs_data, dict):
                            if 'nodes' in gs_data or 'edges' in gs_data:
                                nodes_to_process.extend(gs_data.get('nodes', []))
                                edges_to_process.extend(gs_data.get('edges', []))
                    logger.debug(f"Found nodes/edges in nested structure: {len(nodes_to_process)} nodes, {len(edges_to_process)} edges")
            elif isinstance(provenance_data, list):
                logger.debug("Provenance JSON is a list - may need different handling")
                return max_node_id_used
            
            if not nodes_to_process and not edges_to_process:
                logger.debug(f"No nodes or edges found in provenance JSON for gene_set_id {gene_set_id}")
                return max_node_id_used
            
            # Reorder: insert GeneSet nodes FIRST so their reserved provenance_node_id
            # (= gene_set_id) is taken before any non-GeneSet node gets auto-assigned.
            # Otherwise, a non-GeneSet node processed first would receive id=gene_set_id
            # and then be overwritten by the GeneSet node's INSERT OR REPLACE.
            nodes_to_process = sorted(
                nodes_to_process,
                key=lambda n: 0 if n.get('type') == 'GeneSet' else 1
            )
            
            # Mapping from original JSON node ID to new provenance_node_id
            node_id_mapping = {}
            
            # Insert nodes
            nodes_inserted = 0
            for node in nodes_to_process:
                original_id = node.get('id')
                node_type = node.get('type', '')
                name = node.get('name', '')
                # Override GeneSet node name to keep it in sync with gene_set.standard_name
                if node_type == 'GeneSet' and geneset_node_name:
                    name = geneset_node_name
                description = node.get('description', '')
                dcc_url = node.get('dcc_url')
                drc_url = node.get('drc_url')
                
                # Collect all properties except the main ones we extracted
                additional_props = {
                    'original_id': original_id
                }
                
                # Store all other fields (c2m2_properties, analysis, etc.)
                for key, value in node.items():
                    if key not in ['id', 'type', 'name', 'description', 'dcc_url', 'drc_url']:
                        additional_props[key] = value
                
                additional_properties = json.dumps(additional_props)
                
                try:
                    # For GeneSet nodes, use gene_set_id as the provenance_node_id (use INSERT OR REPLACE to handle re-runs)
                    if node_type == 'GeneSet':
                        self.cursor.execute(
                            '''INSERT OR REPLACE INTO provenance_node 
                               (provenance_node_id, gene_set_id, node_type, name, description, dcc_url, drc_url, additional_properties)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                            (gene_set_id, gene_set_id, node_type, name, description, dcc_url, drc_url, additional_properties)
                        )
                        new_node_id = gene_set_id
                    else:
                        self.cursor.execute(
                            '''INSERT INTO provenance_node 
                               (gene_set_id, node_type, name, description, dcc_url, drc_url, additional_properties)
                               VALUES (?, ?, ?, ?, ?, ?, ?)''',
                            (gene_set_id, node_type, name, description, dcc_url, drc_url, additional_properties)
                        )
                        new_node_id = self.cursor.lastrowid
                    
                    node_id_mapping[original_id] = new_node_id
                    max_node_id_used = max(max_node_id_used, new_node_id)
                    nodes_inserted += 1
                except sqlite3.IntegrityError as e:
                    logger.warning(f"Error inserting node {original_id} for gene_set_id {gene_set_id}: {e}")
                    continue
            
            logger.debug(f"Inserted {nodes_inserted} nodes for gene_set_id {gene_set_id}, max node ID used: {max_node_id_used}")
            
            # Insert edges
            edges_inserted = 0
            for edge in edges_to_process:
                source_original_id = edge.get('source')
                target_original_id = edge.get('target')
                label = edge.get('label', '')
                description = edge.get('description', '')
                
                # Look up new node IDs
                source_node_id = node_id_mapping.get(source_original_id)
                target_node_id = node_id_mapping.get(target_original_id)
                
                if not source_node_id or not target_node_id:
                    logger.debug(
                        f"Skipping edge: source={source_original_id}, target={target_original_id} - "
                        f"nodes not found in mapping"
                    )
                    continue
                
                # Collect all properties except the main ones
                additional_props = {
                    'original_id': edge.get('id')
                }
                
                for key, value in edge.items():
                    if key not in ['id', 'source', 'target', 'label', 'description']:
                        additional_props[key] = value
                
                additional_properties = json.dumps(additional_props) if additional_props else None
                
                try:
                    self.cursor.execute(
                        '''INSERT INTO provenance_edge
                           (gene_set_id, source_node_id, target_node_id, label, description, additional_properties)
                           VALUES (?, ?, ?, ?, ?, ?)''',
                        (gene_set_id, source_node_id, target_node_id, label, description, additional_properties)
                    )
                    edges_inserted += 1
                except sqlite3.IntegrityError as e:
                    logger.warning(f"Error inserting edge {edge.get('id')}: {e}")
                    continue
            
            logger.debug(f"Inserted {edges_inserted} edges for gene_set_id {gene_set_id}")
        
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding provenance JSON for gene_set_id {gene_set_id}: {e}")
            logger.debug(f"JSON string (first 500 chars): {provenance_json_str[:500]}")
        except Exception as e:
            logger.error(f"Error inserting provenance nodes/edges for gene_set_id {gene_set_id}: {e}", exc_info=True)
        
        return max_node_id_used

    def populate_from_gmt_files(
        self,
        root_path: Optional[str] = None,
        s3_root: Optional[str] = None,
        collection_name: str = 'GTEx',
        species_code: str = 'Homo_sapiens',
        species_name: str = 'Homo sapiens',
        namespace_label: str = 'HGNC',
        license_code: str = 'CC-BY-4.0',
        require_provenance: bool = True,
        contrib_organization: str = None
    ):
        """Populate database from GMT files."""
        try:
            self.synchronize_next_available_node_id()

            # Find all GMT files
            gmt_files = self.find_gmt_files(root_path=root_path, s3_root=s3_root)
            if not gmt_files:
                logger.warning("No .gmt files found")
                return
            
            # Initialize reference data
            logger.info("Initializing reference data...")
            species_id = self.insert_species(species_code, species_name)
            namespace_id = self.insert_namespace(namespace_label, species_code)
            license_id = self.insert_gene_set_license(license_code)
            initialized_collection_names = set()
            
            # Process each GMT file
            total_gene_sets = 0
            total_genes = 0
            skipped_gene_sets = 0
            
            for gmt_file in gmt_files:
                logger.info(f"Processing GMT file: {gmt_file.location}")
                gene_sets = self.parse_gmt_file(gmt_file)
                effective_collection_name = self.resolve_collection_name(gmt_file, collection_name)

                if effective_collection_name not in initialized_collection_names:
                    self.insert_collection(effective_collection_name)
                    initialized_collection_names.add(effective_collection_name)
                
                for gene_set_name, genes in gene_sets:
                    try:
                        # Derive tissue from path: .../genesets/{tissue}/models/{model_id}/tissue_extractor/genesets.gmt
                        tissue = None
                        for idx, part in enumerate(gmt_file.path_parts):
                            if part == 'models' and idx > 0:
                                tissue = gmt_file.path_parts[idx - 1]
                                break
                        
                        # Build unified standard name: {collection}__{tissue}__{gene_set_name}
                        if tissue:
                            standard_name = f"{effective_collection_name}__{tissue}__{gene_set_name}"
                        else:
                            standard_name = f"{effective_collection_name}__{gene_set_name}"
                        
                        # Load provenance and metadata if required
                        provenance_data = None
                        if require_provenance:
                            provenance_data = self.load_provenance_and_metadata(
                                gene_set_name,
                                gmt_file.parent_location,
                                gmt_file.is_s3,
                            )
                            if not provenance_data:
                                logger.debug(f"Skipping {gene_set_name} - provenance/metadata files not found")
                                skipped_gene_sets += 1
                                continue
                        
                        # Assign gene_set_id = next_available_node_id (for ID spacing)
                        requested_gene_set_id = self.find_next_unused_node_id()
                        gene_set_id = requested_gene_set_id
                        
                        # Insert gene set with explicit ID
                        gene_set_id, gene_set_was_inserted = self.insert_gene_set(
                            standard_name=standard_name,
                            collection_name=effective_collection_name,
                            license_code=license_code,
                            gene_set_id=gene_set_id
                        )
                        
                        if not gene_set_id:
                            logger.warning(f"Failed to insert gene set: {standard_name}")
                            continue
                        
                        # Insert genes and create associations
                        for gene_symbol in genes:
                            gene_symbol_id = self.insert_gene_symbol(
                                symbol=gene_symbol,
                                namespace_id=namespace_id
                            )
                            
                            if gene_symbol_id:
                                self.insert_gene_set_gene_symbol(gene_set_id, gene_symbol_id)
                                total_genes += 1
                        
                        # Only insert provenance for newly created gene sets.
                        max_node_id = 0
                        if provenance_data and gene_set_was_inserted:
                            provenance_graph, geneset_metadata, run_summary = provenance_data
                            self.insert_provenance(gene_set_id, provenance_graph, geneset_metadata, run_summary)
                            # Populate gene_set_details from metadata JSON
                            self.insert_gene_set_details(
                                gene_set_id=gene_set_id,
                                gene_set_name=standard_name,
                                metadata_json_str=geneset_metadata,
                                species_code=species_code,
                                primary_namespace_id=namespace_id,
                                contrib_organization=contrib_organization,
                            )
                            if provenance_graph:
                                max_node_id = self.insert_provenance_nodes_and_edges(
                                    gene_set_id, provenance_graph,
                                    geneset_node_name=standard_name,
                                    geneset_node_name_match=gene_set_name
                                )
                        elif provenance_data and not gene_set_was_inserted:
                            logger.info(
                                f"Gene set '{standard_name}' already exists (id={gene_set_id}); "
                                "skipping provenance insertion"
                            )
                        
                        # Always advance next_available_node_id past this gene set's used IDs,
                        # so the next gene set never collides (even if provenance was missing/empty).
                        self.next_available_node_id = max(requested_gene_set_id, gene_set_id, max_node_id) + 1
                        
                        total_gene_sets += 1
                        logger.info(
                            f"Loaded gene set '{standard_name}' (id={gene_set_id}, "
                            f"{len(genes)} genes) from {gmt_file.location}"
                        )
                        
                        if total_gene_sets % 100 == 0:
                            logger.info(f"Progress: {total_gene_sets} gene sets, {total_genes} gene associations so far")
                            self.conn.commit()
                    
                    except Exception as e:
                        logger.error(f"Error processing gene set {gene_set_name}: {e}")
                        continue
            
            # Final commit
            self.conn.commit()
            logger.info(f"Population complete: {total_gene_sets} gene sets, {total_genes} gene associations")
            if skipped_gene_sets > 0:
                logger.info(f"Skipped {skipped_gene_sets} gene sets (missing provenance/metadata)")
        
        except Exception as e:
            logger.error(f"Error during population: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(
        description='Populate GenSeCoDB database from GMT gene set files'
    )
    parser.add_argument(
        '--db-path',
        required=True,
        help='Path to SQLite database file'
    )
    parser.add_argument(
        '--schema-file',
        required=True,
        help='Path to database schema SQL file'
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        '--data-root',
        help='Root directory containing GMT files'
    )
    input_group.add_argument(
        '--s3-data-root',
        help='S3 URI containing GMT files, for example s3://geneset-marc-test'
    )
    parser.add_argument(
        '--output-log',
        help='Optional path to a log file. If omitted, logs are only written to stderr.'
    )
    parser.add_argument(
        '--aws-access-key-id',
        help='Optional AWS access key ID for S3 access. Takes precedence over AWS_ACCESS_KEY_ID.'
    )
    parser.add_argument(
        '--aws-secret-access-key',
        help='Optional AWS secret access key for S3 access. Takes precedence over AWS_SECRET_ACCESS_KEY.'
    )
    parser.add_argument(
        '--aws-session-token',
        help='Optional AWS session token for temporary S3 credentials. Takes precedence over AWS_SESSION_TOKEN.'
    )
    parser.add_argument(
        '--aws-region',
        help='Optional AWS region for the S3 client. Takes precedence over AWS_DEFAULT_REGION and AWS_REGION.'
    )
    parser.add_argument(
        '--collection-name',
        default='GTEx',
        help='Collection name (default: GTEx)'
    )
    parser.add_argument(
        '--species-code',
        default='Homo_sapiens',
        help='Species code (default: Homo_sapiens)'
    )
    parser.add_argument(
        '--species-name',
        default='Homo sapiens',
        help='Species full name (default: Homo sapiens)'
    )
    parser.add_argument(
        '--namespace-label',
        default='HGNC',
        help='Gene namespace label (default: HGNC)'
    )
    parser.add_argument(
        '--license-code',
        default='CC-BY-4.0',
        help='License code (default: CC-BY-4.0)'
    )
    parser.add_argument(
        '--contrib-organization',
        default='GTEx Consortium',
        help='Contributing organization for gene_set_details (default: GTEx Consortium)'
    )
    parser.add_argument(
        '--require-provenance',
        action='store_true',
        default=True,
        help='Only include gene sets with provenance/metadata files (default: True)'
    )
    parser.add_argument(
        '--skip-provenance-check',
        action='store_true',
        help='Include all gene sets regardless of provenance/metadata availability'
    )
    
    args = parser.parse_args()
    
    configure_logging(args.output_log)

    # Determine whether to require provenance
    require_provenance = not args.skip_provenance_check
    
    # Populate database
    populator = GeneSeCoDatabasePopulator(
        args.db_path,
        aws_access_key_id=args.aws_access_key_id,
        aws_secret_access_key=args.aws_secret_access_key,
        aws_session_token=args.aws_session_token,
        aws_region=args.aws_region,
    )
    populator.connect()
    
    try:
        # Initialize schema
        populator.initialize_schema(args.schema_file)
        
        # Populate data
        populator.populate_from_gmt_files(
            root_path=args.data_root,
            s3_root=args.s3_data_root,
            collection_name=args.collection_name,
            species_code=args.species_code,
            species_name=args.species_name,
            namespace_label=args.namespace_label,
            license_code=args.license_code,
            require_provenance=require_provenance,
            contrib_organization=args.contrib_organization,
        )
    finally:
        populator.disconnect()


if __name__ == '__main__':
    main()
