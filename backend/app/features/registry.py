"""Feature Registry (Volume 3, Chapter 5) and dependency graph (Chapter 7).

Holds every registered FeatureDefinition in memory and mirrors it to the
feature_registry / feature_versions / feature_dependencies tables. The
dependency graph yields a topological order so downstream features can be
recomputed correctly when an upstream feature changes.
"""

from collections import deque
from collections.abc import Callable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.database.tables import FeatureDependencyRow, FeatureRegistryRow, FeatureVersion
from app.features.schema import FeatureDefinition

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]


class FeatureRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, FeatureDefinition] = {}

    def register(self, definition: FeatureDefinition) -> None:
        self._definitions[definition.feature_name] = definition

    def get(self, feature_name: str) -> FeatureDefinition | None:
        return self._definitions.get(feature_name)

    def list_definitions(self, category: str | None = None) -> list[FeatureDefinition]:
        definitions = [
            d for d in self._definitions.values()
            if category is None or d.category == category
        ]
        return sorted(definitions, key=lambda d: d.feature_name)

    def dependency_order(self) -> list[str]:
        """Kahn topological sort: upstream features come before their dependents."""
        in_degree = {name: 0 for name in self._definitions}
        dependents: dict[str, list[str]] = {name: [] for name in self._definitions}
        for name, definition in self._definitions.items():
            for upstream in definition.dependencies:
                if upstream in self._definitions:
                    in_degree[name] += 1
                    dependents[upstream].append(name)

        queue = deque(sorted(name for name, deg in in_degree.items() if deg == 0))
        ordered: list[str] = []
        while queue:
            name = queue.popleft()
            ordered.append(name)
            for downstream in sorted(dependents[name]):
                in_degree[downstream] -= 1
                if in_degree[downstream] == 0:
                    queue.append(downstream)
        if len(ordered) != len(self._definitions):
            cyclic = sorted(set(self._definitions) - set(ordered))
            raise ValueError(f"feature dependency cycle involving: {cyclic}")
        return ordered

    def dependents_of(self, feature_name: str) -> list[str]:
        """Transitive downstream features, in recomputation order."""
        direct: dict[str, set[str]] = {name: set() for name in self._definitions}
        for name, definition in self._definitions.items():
            for upstream in definition.dependencies:
                if upstream in direct:
                    direct[upstream].add(name)

        reached: set[str] = set()
        queue = deque([feature_name])
        while queue:
            current = queue.popleft()
            for downstream in direct.get(current, ()):
                if downstream not in reached:
                    reached.add(downstream)
                    queue.append(downstream)
        return [name for name in self.dependency_order() if name in reached]

    async def sync_to_db(self, session_factory: SessionFactory) -> dict[str, int]:
        """Upsert definitions, version history, and dependency edges."""
        definitions = self.list_definitions()
        if not definitions:
            return {"features": 0, "dependencies": 0}

        registry_rows = [
            {
                "feature_name": d.feature_name,
                "category": d.category,
                "description": d.description,
                "version": d.version,
                "calculation_frequency": d.calculation_frequency,
                "owner": d.owner,
                "quality_threshold": d.quality_threshold,
                "unit": d.unit,
                "expected_min": d.expected_range[0],
                "expected_max": d.expected_range[1],
                "enabled": True,
            }
            for d in definitions
        ]
        version_rows = [
            {"feature_name": d.feature_name, "version": d.version, "description": d.description}
            for d in definitions
        ]
        dependency_rows = [
            {"feature_name": d.feature_name, "depends_on": upstream}
            for d in definitions
            for upstream in d.dependencies
        ]

        async with session_factory() as session:
            registry_stmt = pg_insert(FeatureRegistryRow).values(registry_rows)
            await session.execute(
                registry_stmt.on_conflict_do_update(
                    index_elements=["feature_name"],
                    set_={
                        column: getattr(registry_stmt.excluded, column)
                        for column in (
                            "category", "description", "version", "calculation_frequency",
                            "owner", "quality_threshold", "unit", "expected_min",
                            "expected_max", "enabled",
                        )
                    },
                )
            )
            # Versions are append-only history: never updated, only added.
            await session.execute(
                pg_insert(FeatureVersion)
                .values(version_rows)
                .on_conflict_do_nothing(index_elements=["feature_name", "version"])
            )
            if dependency_rows:
                await session.execute(
                    pg_insert(FeatureDependencyRow)
                    .values(dependency_rows)
                    .on_conflict_do_nothing(index_elements=["feature_name", "depends_on"])
                )
            await session.commit()

        logger.info(
            "feature registry synced",
            extra={"features": len(definitions), "dependencies": len(dependency_rows)},
        )
        return {"features": len(definitions), "dependencies": len(dependency_rows)}
