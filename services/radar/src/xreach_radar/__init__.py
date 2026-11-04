"""REA-backed live radar for X-Reach."""

from .alerts import (
    AlertEvent,
    AlertRule,
    AlertRuleEngine,
    WebhookDispatcher,
    WebhookTarget,
)
from .archive_ingest import (
    ArchiveIngestor,
    ArchiveIngestReport,
    ArchivePost,
    AuthorBaseline,
    FormatBreakdown,
    ingest_archive,
)
from .cdp_client import ReaClient, ReaCommandError
from .daemon import (
    ApiEventSink,
    CaptureFileSource,
    DaemonCycle,
    RadarDaemon,
    ReaScenarioSource,
)
from .graphql_sniffer import GraphQLDocument, iter_graphql_documents
from .seed_scout import ScoutConfig, SeedScout, ViralSeed
from .simclusters import (
    ActiveSimCluster,
    AlignedViralSeed,
    ClusterMatch,
    SimClusterIndex,
    SimClusterScout,
    semantic_id,
    shared_prefix_levels,
)

__all__ = [
    "ActiveSimCluster",
    "AlertEvent",
    "AlertRule",
    "AlertRuleEngine",
    "AlignedViralSeed",
    "ApiEventSink",
    "ArchiveIngestReport",
    "ArchiveIngestor",
    "ArchivePost",
    "AuthorBaseline",
    "CaptureFileSource",
    "ClusterMatch",
    "DaemonCycle",
    "FormatBreakdown",
    "GraphQLDocument",
    "RadarDaemon",
    "ReaClient",
    "ReaCommandError",
    "ReaScenarioSource",
    "ScoutConfig",
    "SeedScout",
    "SimClusterIndex",
    "SimClusterScout",
    "ViralSeed",
    "WebhookDispatcher",
    "WebhookTarget",
    "ingest_archive",
    "iter_graphql_documents",
    "semantic_id",
    "shared_prefix_levels",
]
