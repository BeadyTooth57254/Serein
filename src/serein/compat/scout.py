"""Programmatic Arc revision scan plus material access for manual theme discovery."""

from pathlib import Path
from datetime import datetime, timezone

from .background import SceneReader, germany_config
from .narratives import narrative_transaction, RevisionInbox, Uploads
from .events import Events
from .diaries import Diaries
from .germany.narrative_scan import NarrativeScout
from .germany.narrative_http import NarrativeHTTP
from .narrative_cleanup import cleanup_retired_bindings
from ..core.store import Store


class StoreCalls:
    def __init__(self, database, kind):
        self.database, self.kind = database, kind

    def __getattr__(self, name):
        def call(*args, **kwargs):
            write = self.kind == 'inbox' and name not in ('list', 'scan_metadata', 'candidate_contexts')
            with narrative_transaction(self.database, write=write) as rolls:
                target = RevisionInbox(rolls.store) if self.kind == 'inbox' else rolls
                return getattr(target, name)(*args, **kwargs)
        return call


class Scout(NarrativeScout):
    def __init__(self, settings):
        self.settings = settings
        self.config = germany_config(settings)
        self.rolls = StoreCalls(settings.database, 'rolls')
        self.inbox = StoreCalls(settings.database, 'inbox')
        self.events = Events(settings.database)
        self.diaries = Diaries(settings.database)
        self.scenes = SceneReader(settings.database)

    async def _scan_narrative_revision_inbox(self, *, include_external=True, force_external=False):
        """Check published rolls against their already-linked materials, without a model."""
        with narrative_transaction(self.settings.database, write=True) as rolls:
            cleaned = cleanup_retired_bindings(rolls)
            retired = RevisionInbox(rolls.store).retire_model_candidates()
        scan_timezone = self._narrative_revision_scan_settings()['timezone']
        stale_created = []
        stale_ids = set()
        checked = 0
        for summary in self.rolls.revision_targets():
            narrative = await self.read_narrative(summary['narrative_id'])
            if narrative.get('status') != 'ok':
                continue
            published = self._narrative_timestamp(narrative.get('published_at'), scan_timezone)
            if published is None:
                continue
            checked += 1
            sources = await self._narrative_material_freshness(narrative, scan_timezone)
            if not sources:
                continue
            latest = max(sources, key=lambda item: self._narrative_timestamp(item.get('updated_at'), scan_timezone)
                         or datetime.min.replace(tzinfo=timezone.utc))
            latest_at = self._narrative_timestamp(latest.get('updated_at'), scan_timezone)
            if latest_at and latest_at > published:
                stale_ids.add(summary['narrative_id'])
                stale_created.extend(self.inbox.consider_stale_roll(
                    narrative, latest_material=latest, material_count=len(sources)))
        removed = self.inbox.reconcile_stale_rolls(stale_ids)
        result = {
            'status': 'ok', 'checked_rolls': checked,
            'stale_roll_hints_created': len(stale_created),
            'stale_roll_hints_removed': len(removed),
            'model_candidates_retired': len(retired),
            'writes_performed': [
                {'type': 'narrative_revision_hint', 'proposal_id': item['proposal_id'],
                 'proposal_kind': 'existing_roll_update'} for item in stale_created
            ] + [
                {'type': 'narrative_revision_hint_removed', 'proposal_id': proposal_id,
                 'proposal_kind': 'existing_roll_update'} for proposal_id in removed
            ] + [
                {'type': 'narrative_candidate_retired', 'proposal_id': proposal_id,
                 'proposal_kind': 'new_roll_candidate'} for proposal_id in retired
            ],
        }
        self.inbox.record_scan(result)
        result['retired_material_bindings_removed'] = cleaned
        result['narrative_writes_performed'] = [
            {'type': 'retired_material_cleanup', **change, 'body_unchanged': True} for change in cleaned]
        return result

    async def read_narrative(self, key):
        with narrative_transaction(self.settings.database) as rolls:
            api = NarrativeHTTP()
            api.rolls, api.events, api.uploads = rolls, self.events, Uploads(rolls.store)
            return await api._read_narrative_memory(key)

    async def _narrative_material_freshness(self, narrative, scan_timezone):
        materials = await super()._narrative_material_freshness(narrative, scan_timezone)
        with Store(self.settings.database, read_only=True) as store:
            return [item for item in materials if item['source_type'] != 'event'
                    or not store.promoted_scene(item['source_id'])]

    async def _active_narrative_material_inventory(self):
        materials = await super()._active_narrative_material_inventory()
        with Store(self.settings.database, read_only=True) as store:
            return [item for item in materials if item['source_type'] != 'event'
                    or not store.promoted_scene(item['source_id'])]

    def role_rules(self):
        filename = self.config.get('narrative_rolls',{}).get('scout_role_file') or Path(__file__).resolve().parents[1]/'resources'/'narrative-scout.md'
        rules = Path(filename).read_text('utf-8').strip()
        if not rules:
            raise ValueError('Narrative Scout role file is empty')
        from ..deployment import identity
        from .germany.identity import render_identity_template
        return render_identity_template(rules, identity(self.settings.database))

    async def run_due(self, current=None):
        config = self._narrative_revision_scan_settings()
        current = current or datetime.now(config['timezone'])
        previous = self._narrative_timestamp(self.inbox.scan_metadata().get('last_scan_at'), config['timezone'])
        target = current.replace(hour=config['hour'], minute=config['minute'], second=0, microsecond=0)
        if (not config['enabled'] or current < target or
                previous and previous.astimezone(config['timezone']).date() == current.date()):
            return {'status': 'not_due'}
        return await self._scan_narrative_revision_inbox()
