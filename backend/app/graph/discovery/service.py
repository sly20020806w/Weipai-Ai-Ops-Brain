"""完整采集后一次事务刷新图；重试与重复执行复用既有原子 upsert。"""

from app.graph.discovery.models import DiscoveryResult, DiscoverySnapshot
from app.graph.service import GraphService


async def persist_snapshot(
    graph: GraphService, snapshot: DiscoverySnapshot, observed_at: str
) -> DiscoveryResult:
    snapshot = DiscoverySnapshot.model_validate(snapshot)
    nodes = {}
    for item in snapshot.nodes:
        nodes[item.key] = await graph.upsert_node(
            kind=item.kind, external_id=item.external_id, name=item.name
        )
    for edge in snapshot.relations:
        await graph.upsert_edge(
            from_node_id=nodes[edge.origin.key].id,
            to_node_id=nodes[edge.target.key].id,
            relation=edge.relation,
            source=edge.source,
            confidence=edge.confidence,
            observed_at=edge.observed_at,
        )
    return DiscoveryResult(
        observed_at, len(nodes), len(snapshot.relations), snapshot.missing_bindings
    )
