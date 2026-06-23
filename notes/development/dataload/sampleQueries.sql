


.headers on
.mode column

select count(*) from gene_set;


select * from gene_set where gene_set_id > 60 limit 20;


select provenance_node_id, dcc_url from provenance_node where gene_set_id = 167;

select * from gene_set_gene_symbol where gene_set_id = 167;


select provenance_node_id, description from provenance_node where gene_set_id = 167;


select * from provenance_edge where gene_set_id = 723;


select pedge.gene_set_id, pedge.provenance_edge_id as edge_id, pedge.label as edge_name,
pedge.source_node_id as source_id, snode.node_type as source_type, snode.name as source_name,
pedge.target_node_id as target_id, tnode.node_type as target_type, tnode.name as target_name
from provenance_node snode, provenance_node tnode, provenance_edge pedge
where pedge.gene_set_id = 723
and snode.provenance_node_id = pedge.source_node_id
and tnode.provenance_node_id = pedge.target_node_id;




