from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.jurisdiction.states import normalize_state, state_name
from parlay_bot.storage.hot import SQLiteHotStore


class ZipResolver(Protocol):
    async def resolve(self, zip_code: str) -> str: ...


@dataclass(frozen=True)
class OnboardingLocation:
    state_code: str
    state_name: str
    eligible_books: tuple[str, ...]
    source: str


class LocationOnboardingService:
    """Persist only a user's selected/resolved state, never their ZIP code."""

    def __init__(
        self,
        *,
        store: SQLiteHotStore,
        registry: JurisdictionRegistry,
        zip_resolver: ZipResolver,
    ) -> None:
        self._store = store
        self._registry = registry
        self._zip_resolver = zip_resolver

    def _save(
        self,
        *,
        platform: str,
        user_id: str,
        state_code: str,
        source: str,
    ) -> OnboardingLocation:
        self._store.set_user_jurisdiction(
            platform=platform,
            user_id=user_id,
            state_code=state_code,
            source=source,
        )
        return OnboardingLocation(
            state_code=state_code,
            state_name=state_name(state_code),
            eligible_books=tuple(self._registry.eligible_books(state_code)),
            source=source,
        )

    def get_saved(self, *, platform: str, user_id: str) -> OnboardingLocation | None:
        saved = self._store.get_user_jurisdiction(platform=platform, user_id=user_id)
        if saved is None:
            return None

        state_code = str(saved["state_code"])
        return OnboardingLocation(
            state_code=state_code,
            state_name=state_name(state_code),
            eligible_books=tuple(self._registry.eligible_books(state_code)),
            source=str(saved["source"]),
        )

    def choose_state(self, *, platform: str, user_id: str, state: str) -> OnboardingLocation:
        return self._save(
            platform=platform,
            user_id=user_id,
            state_code=normalize_state(state),
            source="state",
        )

    async def choose_zip(
        self,
        *,
        platform: str,
        user_id: str,
        zip_code: str,
    ) -> OnboardingLocation:
        state_code = await self._zip_resolver.resolve(zip_code)
        return self._save(
            platform=platform,
            user_id=user_id,
            state_code=state_code,
            source="zip",
        )
